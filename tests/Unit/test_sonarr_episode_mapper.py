"""Translating AniList episode numbering into Sonarr's, without moving files."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from src.Download.SonarrEpisodeMapper import (
    MappingError,
    SonarrEpisodeMapper,
    _under,
)
from src.Utils.Config import AppConfig, SonarrConfig

SERIES_PATH = "/tv/GATE"


@pytest.fixture
def no_rebuild(monkeypatch):
    """Stub the self-heal so a test can assert on the stored mapping alone."""

    async def _none(db, config, anilist, sonarr_id, seed=None):
        return 0

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


def _sonarr(candidates: list[dict[str, Any]], episodes: list[dict[str, Any]]):
    client = MagicMock()
    sent: dict[str, Any] = {}

    async def get_series_by_id(sid: int) -> dict[str, Any]:
        return {"id": sid, "title": "GATE", "path": SERIES_PATH}

    async def get_episodes(sid: int) -> list[dict[str, Any]]:
        return episodes

    async def get_manual_import_candidates(*a: Any, **kw: Any) -> list[dict[str, Any]]:
        return candidates

    async def manual_import(files: list[dict[str, Any]], import_mode: str = "Auto"):
        sent["files"] = files
        sent["import_mode"] = import_mode
        return {"id": 999}

    async def close() -> None:
        return None

    client.get_series_by_id = get_series_by_id
    client.get_episodes = get_episodes
    client.get_manual_import_candidates = get_manual_import_candidates
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
        return 1

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
