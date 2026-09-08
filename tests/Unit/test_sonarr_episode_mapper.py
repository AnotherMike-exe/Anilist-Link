"""Translating AniList episode numbering into Sonarr's, without moving files."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from src.Download.SeasonRangeMapper import RebuildResult
from src.Download.SonarrEpisodeMapper import (
    MappingError,
    SonarrEpisodeMapper,
    _donor_quality,
    _is_unknown_quality,
    _quality_from_definitions,
    _resolution_of,
    _under,
)
from src.Utils.Config import AppConfig, SonarrConfig

SERIES_PATH = "/tv/GATE"


@pytest.fixture
def no_rebuild(monkeypatch):
    """Stub the self-heal so a test can assert on the stored mapping alone."""

    async def _none(db, config, anilist, sonarr_id, seed=None):
        return RebuildResult(reason="nothing to go on")

    monkeypatch.setattr("src.Download.SonarrEpisodeMapper.rebuild_season_ranges", _none)


def _config() -> AppConfig:
    return AppConfig(sonarr=SonarrConfig(url="http://s:8989", api_key="k"))


def _db(
    sonarr_id: int | None = 272,
    season_row: dict[str, Any] | None = None,
    season_order: int | None = 2,
) -> MagicMock:
    db = MagicMock()

    async def fetch_one(query: str, params: tuple = ()) -> dict[str, Any] | None:
        if "anilist_sonarr_season_mapping" in query:
            return season_row
        if "anilist_sonarr_mapping" in query:
            return {"sonarr_id": sonarr_id} if sonarr_id else None
        if "series_group_entries" in query:
            return {"season_order": season_order} if season_order else None
        return None

    db.fetch_one = fetch_one
    return db


def _sonarr(
    candidates: list[dict[str, Any]],
    episodes: list[dict[str, Any]],
    episode_files: list[dict[str, Any]] | None = None,
):
    client = MagicMock()
    sent: dict[str, Any] = {}

    async def get_series_by_id(sid: int) -> dict[str, Any]:
        return {"id": sid, "title": "GATE", "path": SERIES_PATH}

    async def get_episodes(sid: int) -> list[dict[str, Any]]:
        return episodes

    async def get_manual_import_candidates(*a: Any, **kw: Any) -> list[dict[str, Any]]:
        return candidates

    async def get_episode_files(sid: int) -> list[dict[str, Any]]:
        return episode_files or []

    async def manual_import(files: list[dict[str, Any]], import_mode: str = "Auto"):
        sent["files"] = files
        sent["import_mode"] = import_mode
        return {"id": 999}

    async def close() -> None:
        return None

    client.get_series_by_id = get_series_by_id
    client.get_episodes = get_episodes
    client.get_manual_import_candidates = get_manual_import_candidates
    client.get_episode_files = get_episode_files
    client.manual_import = manual_import
    client.close = close
    client.sent = sent
    return client


def _episodes(season: int, count: int) -> list[dict[str, Any]]:
    return [
        {"id": 1000 + n, "seasonNumber": season, "episodeNumber": n, "title": f"Ep {n}"}
        for n in range(1, count + 1)
    ]


def _candidate(name: str, folder: str = SERIES_PATH + "/Season 02") -> dict[str, Any]:
    return {
        "path": f"{folder}/{name}",
        "quality": {"quality": {"id": 6, "name": "Bluray-1080p"}},
        "languages": [{"id": 1, "name": "English"}],
        "releaseGroup": "SubGroup",
        "indexerFlags": 0,
        "releaseType": "singleEpisode",
        "episodes": [],
    }


# ---------------------------------------------------------------------------
# The translation itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_split_cour_offsets_onto_sonarrs_numbering() -> None:
    """Our S02E01 is Sonarr's S01E13 when the cour was split at 12."""
    mapper = SonarrEpisodeMapper(
        db=_db(season_row={"season_number": 1, "episode_start": 13, "episode_end": 24}),
        config=_config(),
        sonarr=_sonarr(
            [_candidate("GATE - S02E01.mkv"), _candidate("GATE - S02E02.mkv")],
            _episodes(1, 24),
        ),
    )
    plan = await mapper.plan(21364)

    assert [m["sonarr_episode"] for m in plan["matched"]] == [13, 14]
    assert [m["episode_id"] for m in plan["matched"]] == [1013, 1014]
    assert plan["skipped"] == []


@pytest.mark.asyncio
async def test_first_entry_maps_one_to_one() -> None:
    """A season starting at episode 1 is an identity mapping, not a no-op path."""
    mapper = SonarrEpisodeMapper(
        db=_db(
            season_row={"season_number": 1, "episode_start": 1, "episode_end": 12},
            season_order=1,
        ),
        config=_config(),
        sonarr=_sonarr([_candidate("GATE - S01E05.mkv")], _episodes(1, 24)),
    )
    plan = await mapper.plan(20994)

    assert plan["matched"][0]["sonarr_episode"] == 5
    assert plan["matched"][0]["episode_id"] == 1005


@pytest.mark.asyncio
async def test_files_from_another_season_are_left_alone() -> None:
    """Sonarr lists the whole series folder; only this entry's files are ours."""
    mapper = SonarrEpisodeMapper(
        db=_db(season_row={"season_number": 1, "episode_start": 13, "episode_end": 24}),
        config=_config(),
        sonarr=_sonarr(
            [_candidate("GATE - S01E03.mkv"), _candidate("GATE - S02E01.mkv")],
            _episodes(1, 24),
        ),
    )
    plan = await mapper.plan(21364)

    assert len(plan["matched"]) == 1
    assert plan["matched"][0]["sonarr_episode"] == 13
    assert "season 1" in plan["skipped"][0]["reason"]


@pytest.mark.asyncio
async def test_episode_past_the_mapped_range_is_skipped() -> None:
    """Better an unmapped file than one filed as the wrong episode."""
    mapper = SonarrEpisodeMapper(
        db=_db(season_row={"season_number": 1, "episode_start": 13, "episode_end": 24}),
        config=_config(),
        sonarr=_sonarr([_candidate("GATE - S02E20.mkv")], _episodes(1, 24)),
    )
    plan = await mapper.plan(21364)

    assert plan["matched"] == []
    assert "past the mapped range" in plan["skipped"][0]["reason"]


@pytest.mark.asyncio
async def test_specials_are_skipped() -> None:
    mapper = SonarrEpisodeMapper(
        db=_db(season_row={"season_number": 1, "episode_start": 13, "episode_end": 24}),
        config=_config(),
        sonarr=_sonarr([_candidate("GATE - S02OVA01.mkv")], _episodes(1, 24)),
    )
    plan = await mapper.plan(21364)

    assert plan["matched"] == []
    assert plan["skipped"][0]["reason"] == "special/extra, not an episode"


@pytest.mark.asyncio
async def test_two_files_never_claim_the_same_episode() -> None:
    mapper = SonarrEpisodeMapper(
        db=_db(season_row={"season_number": 1, "episode_start": 13, "episode_end": 24}),
        config=_config(),
        sonarr=_sonarr(
            [_candidate("GATE - S02E01.mkv"), _candidate("GATE - S02E01 (1080p).mkv")],
            _episodes(1, 24),
        ),
    )
    plan = await mapper.plan(21364)

    assert len(plan["matched"]) == 1
    assert "already claimed" in plan["skipped"][0]["reason"]


# ---------------------------------------------------------------------------
# The guard that keeps Sonarr from moving anything
# ---------------------------------------------------------------------------


def test_under_rejects_sibling_prefixes() -> None:
    """/tv/GATE must not swallow /tv/GATE-Extras."""
    assert _under("/tv/GATE", "/tv/GATE/Season 02/ep.mkv") is True
    assert _under("/tv/GATE", "/tv/GATE-Extras/ep.mkv") is False
    assert _under("/tv/GATE", "/downloads/ep.mkv") is False
    assert _under("/tv/GATE", "/tv/GATE") is False


@pytest.mark.asyncio
async def test_file_outside_the_series_folder_is_never_imported() -> None:
    """Outside the series path Sonarr calls it a new download and moves it."""
    mapper = SonarrEpisodeMapper(
        db=_db(season_row={"season_number": 1, "episode_start": 13, "episode_end": 24}),
        config=_config(),
        sonarr=_sonarr(
            [_candidate("GATE - S02E01.mkv", folder="/downloads/complete")],
            _episodes(1, 24),
        ),
    )
    plan = await mapper.plan(21364)

    assert plan["matched"] == []
    assert plan["skipped"][0]["reason"] == "outside the series folder"


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_sends_episode_ids_and_keeps_paths() -> None:
    client = _sonarr(
        [_candidate("GATE - S02E01.mkv"), _candidate("GATE - S02E02.mkv")],
        _episodes(1, 24),
    )
    mapper = SonarrEpisodeMapper(
        db=_db(season_row={"season_number": 1, "episode_start": 13, "episode_end": 24}),
        config=_config(),
        sonarr=client,
    )
    result = await mapper.apply(21364)

    assert result["imported"] == 2
    assert result["command_id"] == 999
    files = client.sent["files"]
    assert [f["episodeIds"] for f in files] == [[1013], [1014]]
    # The path sent back is the path Sonarr gave us — untouched.
    assert files[0]["path"] == SERIES_PATH + "/Season 02/GATE - S02E01.mkv"
    assert files[0]["seriesId"] == 272
    assert files[0]["releaseGroup"] == "SubGroup"
    assert client.sent["import_mode"] == "Auto"


@pytest.mark.asyncio
async def test_apply_with_nothing_to_map_calls_no_command() -> None:
    client = _sonarr([], _episodes(1, 24))
    mapper = SonarrEpisodeMapper(
        db=_db(season_row={"season_number": 1, "episode_start": 13, "episode_end": 24}),
        config=_config(),
        sonarr=client,
    )
    result = await mapper.apply(21364)

    assert result["imported"] == 0
    assert "files" not in client.sent


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unlinked_entry_is_refused() -> None:
    mapper = SonarrEpisodeMapper(db=_db(sonarr_id=None), config=_config())
    with pytest.raises(MappingError, match="isn't linked"):
        await mapper.plan(21364)


@pytest.mark.asyncio
async def test_missing_mapping_is_rebuilt_before_giving_up(monkeypatch) -> None:
    """No stored range is a job to do, not a chore to hand back to the user."""
    calls: list[tuple] = []
    stored: dict[str, Any] = {"row": None}

    async def fake_rebuild(db, config, anilist, sonarr_id, seed=None):
        calls.append((sonarr_id, seed))
        stored["row"] = {"season_number": 1, "episode_start": 13, "episode_end": 24}
        return RebuildResult(written=1)

    monkeypatch.setattr(
        "src.Download.SonarrEpisodeMapper.rebuild_season_ranges", fake_rebuild
    )

    db = MagicMock()

    async def fetch_one(query: str, params: tuple = ()) -> dict[str, Any] | None:
        if "anilist_sonarr_season_mapping" in query:
            return stored["row"]
        if "anilist_sonarr_mapping" in query:
            return {"sonarr_id": 272}
        if "series_group_entries" in query:
            return {"season_order": 2}
        return None

    db.fetch_one = fetch_one
    mapper = SonarrEpisodeMapper(
        db=db,
        config=_config(),
        sonarr=_sonarr([_candidate("GATE - S02E01.mkv")], _episodes(1, 24)),
    )
    plan = await mapper.plan(21364)

    assert calls == [(272, 21364)]
    assert plan["matched"][0]["sonarr_episode"] == 13


@pytest.mark.asyncio
async def test_unrebuildable_mapping_explains_itself(no_rebuild) -> None:
    mapper = SonarrEpisodeMapper(db=_db(season_row=None), config=_config())
    with pytest.raises(MappingError, match="couldn't work out"):
        await mapper.plan(21364)


@pytest.mark.asyncio
async def test_stale_whole_season_mapping_is_refused() -> None:
    """A sequel still mapped to the whole season would file over cour one."""
    mapper = SonarrEpisodeMapper(
        db=_db(
            season_row={"season_number": 1, "episode_start": 1, "episode_end": None}
        ),
        config=_config(),
        sonarr=_sonarr([_candidate("GATE - S02E01.mkv")], _episodes(1, 24)),
    )
    with pytest.raises(MappingError, match="whole season"):
        await mapper.plan(21364)


@pytest.mark.asyncio
async def test_episode_that_already_has_a_file_is_skipped() -> None:
    """Importing over an existing file is an upgrade, and upgrades delete."""
    episodes = _episodes(1, 24)
    episodes[12]["hasFile"] = True  # S01E13
    mapper = SonarrEpisodeMapper(
        db=_db(season_row={"season_number": 1, "episode_start": 13, "episode_end": 24}),
        config=_config(),
        sonarr=_sonarr(
            [_candidate("GATE - S02E01.mkv"), _candidate("GATE - S02E02.mkv")],
            episodes,
        ),
    )
    plan = await mapper.plan(21364)

    assert [m["sonarr_episode"] for m in plan["matched"]] == [14]
    assert "already has a file" in plan["skipped"][0]["reason"]


# ---------------------------------------------------------------------------
# Quality backfill
# ---------------------------------------------------------------------------


def test_unknown_quality_is_recognised_in_every_shape() -> None:
    assert _is_unknown_quality(None) is True
    assert _is_unknown_quality({}) is True
    assert _is_unknown_quality({"quality": {"id": 0, "name": "Unknown"}}) is True
    assert _is_unknown_quality({"quality": {"id": 3, "name": "unknown"}}) is True
    assert _is_unknown_quality({"quality": {"id": 6, "name": "Bluray-1080p"}}) is False


def test_resolution_is_read_from_the_string_the_api_actually_returns() -> None:
    """MediaInfoResource has no height field — it renders one "1920x1080"."""
    assert _resolution_of({"resolution": "1920x1080"}) == 1080
    assert _resolution_of({"resolution": "3840x2160"}) == 2160
    assert _resolution_of({"resolution": "1280x720"}) == 720
    assert _resolution_of({"resolution": "1920x1076"}) == 1080
    assert _resolution_of({"resolution": "1440x1080"}) == 1080
    assert _resolution_of({"resolution": ""}) == 0
    assert _resolution_of({"resolution": "unknown"}) == 0
    assert _resolution_of({"resolution": "1920xWIDE"}) == 0


def test_real_heights_round_to_the_resolution_sonarr_names() -> None:
    """Anime is full of 1076p and 810p encodes; they are still 1080p and 720p."""
    assert _resolution_of({"height": 1080}) == 1080
    assert _resolution_of({"height": 1076}) == 1080
    assert _resolution_of({"height": 2160}) == 2160
    assert _resolution_of({"height": 810}) == 720
    assert _resolution_of({"height": 480}) == 480
    assert _resolution_of({"height": 0}) == 0
    assert _resolution_of(None) == 0
    assert _resolution_of({"height": "not a number"}) == 0
    assert _resolution_of({}) == 0


def test_quality_is_borrowed_from_a_sibling_at_the_same_resolution() -> None:
    """The rest of the series came from the same place — use its source."""
    files = [
        {"quality": {"quality": {"id": 0, "name": "Unknown", "resolution": 0}}},
        {"quality": {"quality": {"id": 4, "name": "HDTV-720p", "resolution": 720}}},
        {"quality": {"quality": {"id": 6, "name": "Bluray-1080p", "resolution": 1080}}},
    ]
    got = _donor_quality(files, 1080)
    assert got is not None
    assert got["quality"]["name"] == "Bluray-1080p"
    assert _donor_quality(files, 720)["quality"]["name"] == "HDTV-720p"
    assert _donor_quality(files, 2160) is None
    assert _donor_quality(files, 0) is None


def test_definition_fallback_prefers_web_over_other_sources() -> None:
    definitions = [
        {
            "quality": {
                "id": 9,
                "name": "HDTV-1080p",
                "source": "television",
                "resolution": 1080,
            }
        },
        {
            "quality": {
                "id": 3,
                "name": "WEBDL-1080p",
                "source": "webdl",
                "resolution": 1080,
            }
        },
        {
            "quality": {
                "id": 7,
                "name": "Bluray-1080p",
                "source": "bluray",
                "resolution": 1080,
            }
        },
    ]
    got = _quality_from_definitions(definitions, 1080)
    assert got["quality"]["name"] == "WEBDL-1080p"
    assert got["revision"] == {"version": 1, "real": 0, "isRepack": False}
    assert _quality_from_definitions(definitions, 2160) is None


@pytest.mark.asyncio
async def test_backfill_sets_quality_on_the_imported_files_only() -> None:
    """Files that already have a quality, and files we didn't import, are
    left exactly as they are."""
    ours = SERIES_PATH + "/Season 02/GATE - S02E01.mkv"
    theirs = SERIES_PATH + "/Season 01/GATE - S01E01.mkv"
    files = [
        {
            "id": 501,
            "path": theirs,
            "quality": {
                "quality": {"id": 6, "name": "Bluray-1080p", "resolution": 1080}
            },
            "mediaInfo": {"resolution": "1920x1080"},
        },
        {
            "id": 502,
            "path": ours,
            "quality": {"quality": {"id": 0, "name": "Unknown", "resolution": 0}},
            "mediaInfo": {"resolution": "1920x1080"},
        },
    ]
    client = _sonarr([], [])
    sent: dict[str, Any] = {}

    async def get_episode_files(sid: int) -> list[dict[str, Any]]:
        return files

    async def set_quality(ids: list[int], quality: dict[str, Any]) -> Any:
        sent["ids"] = ids
        sent["quality"] = quality
        return {}

    client.get_episode_files = get_episode_files
    client.set_episode_files_quality = set_quality

    mapper = SonarrEpisodeMapper(db=_db(), config=_config(), sonarr=client)
    result = await mapper.backfill_quality(272, [ours], client=client)

    assert result["updated"] == 1
    assert sent["ids"] == [502]
    assert sent["quality"]["quality"]["name"] == "Bluray-1080p"


@pytest.mark.asyncio
async def test_backfill_reports_a_file_with_no_media_info() -> None:
    ours = SERIES_PATH + "/Season 02/GATE - S02E01.mkv"
    files = [
        {
            "id": 502,
            "path": ours,
            "quality": {"quality": {"id": 0, "name": "Unknown", "resolution": 0}},
            "mediaInfo": {},
        }
    ]
    client = _sonarr([], [])
    called: list[Any] = []

    async def get_episode_files(sid: int) -> list[dict[str, Any]]:
        return files

    async def get_quality_definitions() -> list[dict[str, Any]]:
        return []

    async def set_quality(ids: list[int], quality: dict[str, Any]) -> Any:
        called.append(ids)
        return {}

    client.get_episode_files = get_episode_files
    client.get_quality_definitions = get_quality_definitions
    client.set_episode_files_quality = set_quality

    mapper = SonarrEpisodeMapper(db=_db(), config=_config(), sonarr=client)
    result = await mapper.backfill_quality(272, [ours], client=client)

    assert result["updated"] == 0
    assert called == []
    assert "media info" in result["unresolved"][0]["reason"]


# ---------------------------------------------------------------------------
# Re-running after the files are already registered
# ---------------------------------------------------------------------------


def _registered(ep_number: int, file_id: int, quality: dict[str, Any] | None):
    """An episode Sonarr already has a file for, and that file's record."""
    ep = {
        "id": 1000 + ep_number,
        "seasonNumber": 1,
        "episodeNumber": ep_number,
        "title": f"Ep {ep_number}",
        "hasFile": True,
        "episodeFileId": file_id,
    }
    f = {
        "id": file_id,
        "path": f"{SERIES_PATH}/Season 02/GATE - S02E{ep_number - 12:02d}.mkv",
        "quality": quality,
        "mediaInfo": {"resolution": "1920x1080"},
    }
    return ep, f


@pytest.mark.asyncio
async def test_rerun_finds_the_quality_gaps_sonarr_no_longer_offers() -> None:
    """Sonarr leaves out files it already tracks, so a second Map Episodes run
    sees no candidates at all — the quality still has to be reachable."""
    unknown = {"quality": {"id": 0, "name": "Unknown", "resolution": 0}}
    ep13, f13 = _registered(13, 901, unknown)
    ep14, f14 = _registered(14, 902, unknown)
    mapper = SonarrEpisodeMapper(
        db=_db(season_row={"season_number": 1, "episode_start": 13, "episode_end": 24}),
        config=_config(),
        # No candidates: everything is already imported.
        sonarr=_sonarr([], [ep13, ep14], episode_files=[f13, f14]),
    )
    plan = await mapper.plan(21364)

    assert plan["matched"] == []
    assert [g["file_id"] for g in plan["quality_gaps"]] == [901, 902]
    assert plan["quality_gaps"][0]["sonarr_episode"] == 13


@pytest.mark.asyncio
async def test_rerun_reports_no_gap_when_quality_is_already_set() -> None:
    good = {"quality": {"id": 6, "name": "Bluray-1080p", "resolution": 1080}}
    ep13, f13 = _registered(13, 901, good)
    mapper = SonarrEpisodeMapper(
        db=_db(season_row={"season_number": 1, "episode_start": 13, "episode_end": 24}),
        config=_config(),
        sonarr=_sonarr([], [ep13], episode_files=[f13]),
    )
    plan = await mapper.plan(21364)
    assert plan["quality_gaps"] == []


@pytest.mark.asyncio
async def test_gaps_ignore_episodes_outside_this_entrys_range() -> None:
    """Cour one's files belong to the other AniList entry, not this one."""
    unknown = {"quality": {"id": 0, "name": "Unknown", "resolution": 0}}
    ep01, f01 = _registered(1, 800, unknown)
    ep13, f13 = _registered(13, 901, unknown)
    mapper = SonarrEpisodeMapper(
        db=_db(season_row={"season_number": 1, "episode_start": 13, "episode_end": 24}),
        config=_config(),
        sonarr=_sonarr([], [ep01, ep13], episode_files=[f01, f13]),
    )
    plan = await mapper.plan(21364)
    assert [g["file_id"] for g in plan["quality_gaps"]] == [901]


@pytest.mark.asyncio
async def test_apply_with_nothing_to_import_still_fixes_quality() -> None:
    """The whole point: re-running Map Episodes repairs what it left behind."""
    unknown = {"quality": {"id": 0, "name": "Unknown", "resolution": 0}}
    good = {"quality": {"id": 6, "name": "Bluray-1080p", "resolution": 1080}}
    ep13, f13 = _registered(13, 901, unknown)
    # A sibling with a real quality to borrow the source from.
    sibling = {
        "id": 800,
        "path": f"{SERIES_PATH}/Season 01/GATE - S01E01.mkv",
        "quality": good,
        "mediaInfo": {"resolution": "1920x1080"},
    }
    client = _sonarr([], [ep13], episode_files=[sibling, f13])
    sent: dict[str, Any] = {}

    async def set_quality(ids: list[int], quality: dict[str, Any]) -> Any:
        sent["ids"] = ids
        sent["quality"] = quality
        return {}

    client.set_episode_files_quality = set_quality

    mapper = SonarrEpisodeMapper(
        db=_db(season_row={"season_number": 1, "episode_start": 13, "episode_end": 24}),
        config=_config(),
        sonarr=client,
    )
    result = await mapper.apply(21364)

    assert result["imported"] == 0
    assert result["quality"]["updated"] == 1
    assert sent["ids"] == [901]
    assert sent["quality"]["quality"]["name"] == "Bluray-1080p"
    # No import was attempted, so no command was issued.
    assert "files" not in client.sent
