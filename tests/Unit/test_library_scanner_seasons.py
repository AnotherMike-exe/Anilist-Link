"""Season-subdir expansion in LibraryScanner.

A restructured show lives at ``Show/Season Name/``, but the scan only walks
top-level folders.  That left every non-root season without a library_items row
— and the prune step then deleted the per-season rows the restructurer had
written, so those entries showed no path in the watchlist.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from src.Scanner.LibraryScanner import LibraryScanner


def _scanner(
    *,
    group: dict[str, Any] | None,
    entries: list[dict[str, Any]],
) -> tuple[LibraryScanner, list[dict[str, Any]]]:
    db = MagicMock()
    upserts: list[dict[str, Any]] = []

    async def get_series_group_by_anilist_id(anilist_id: int):
        return group

    async def get_series_group_entries_with_titles(group_id: int):
        return entries

    async def get_cached_metadata(anilist_id: int):
        return {"format": "TV", "episodes": 12, "year": 2020, "cover_image": ""}

    async def upsert_library_item(**kwargs: Any) -> None:
        upserts.append(kwargs)

    db.get_series_group_by_anilist_id = get_series_group_by_anilist_id
    db.get_series_group_entries_with_titles = get_series_group_entries_with_titles
    db.get_cached_metadata = get_cached_metadata
    db.upsert_library_item = upsert_library_item

    return (
        LibraryScanner(db=db, anilist_client=MagicMock(), title_matcher=MagicMock()),
        upserts,
    )


def _entry(aid: int, title: str, year: str) -> dict[str, Any]:
    return {
        "anilist_id": aid,
        "display_title": title,
        "title_romaji": title,
        "title_english": title,
        "start_date": f"{year}-01-01",
        "season_order": aid,
    }


@pytest.mark.asyncio
async def test_season_subdirs_each_get_their_own_row(tmp_path) -> None:
    """Each season folder is matched to its own group entry."""
    show = tmp_path / "Re Zero"
    (show / "Re Zero (2016)").mkdir(parents=True)
    (show / "Re Zero Season 2 (2020)").mkdir(parents=True)

    scanner, upserts = _scanner(
        group={"id": 5},
        entries=[_entry(1, "Re Zero", "2016"), _entry(2, "Re Zero Season 2", "2020")],
    )

    written = await scanner._expand_season_subdirs(1, str(show), 1)

    assert len(written) == 2
    assert {u["anilist_id"] for u in upserts} == {1, 2}
    assert all(u["match_method"] == "season_subdir" for u in upserts)
    assert all(u["series_group_id"] == 5 for u in upserts)


@pytest.mark.asyncio
async def test_single_entry_group_is_left_alone(tmp_path) -> None:
    """A standalone show keeps just its root row — no spurious season rows."""
    show = tmp_path / "Cowboy Bebop"
    (show / "Season 1").mkdir(parents=True)

    scanner, upserts = _scanner(
        group={"id": 7}, entries=[_entry(1, "Cowboy Bebop", "1998")]
    )

    written = await scanner._expand_season_subdirs(1, str(show), 1)

    assert written == set()
    assert upserts == []


@pytest.mark.asyncio
async def test_ungrouped_show_is_left_alone(tmp_path) -> None:
    """No series group means nothing to expand into."""
    show = tmp_path / "Some Show"
    (show / "Season 1").mkdir(parents=True)

    scanner, upserts = _scanner(group=None, entries=[])

    written = await scanner._expand_season_subdirs(1, str(show), 1)

    assert written == set()
    assert upserts == []


@pytest.mark.asyncio
async def test_missing_folder_is_a_noop(tmp_path) -> None:
    """A folder that vanished mid-scan must not raise."""
    scanner, upserts = _scanner(
        group={"id": 5},
        entries=[_entry(1, "A", "2016"), _entry(2, "B", "2020")],
    )

    written = await scanner._expand_season_subdirs(1, str(tmp_path / "gone"), 1)

    assert written == set()
    assert upserts == []


# ---------------------------------------------------------------------------
# Shared *arr state helpers (used by both the watchlist and the library)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_arr_state_reports_sonarr_radarr_and_untracked() -> None:
    """Every arr_* field is always set, whatever the entry's tracking state."""
    from src.Web.Routes.Helpers import apply_arr_state

    sonarr = {
        1: {
            "sonarr_id": 7,
            "sonarr_season": 2,
            "sonarr_monitored": True,
            "monitor_type": "all",
        }
    }
    radarr = {2: {"radarr_id": 9, "radarr_monitored": False, "monitor_type": "future"}}

    tv: dict[str, Any] = {}
    apply_arr_state(tv, 1, sonarr, radarr)
    assert tv["arr_service"] == "sonarr"
    assert tv["arr_status"] == "monitored"
    assert tv["sonarr_id"] == 7 and tv["sonarr_season"] == 2
    assert tv["radarr_id"] is None

    movie: dict[str, Any] = {}
    apply_arr_state(movie, 2, sonarr, radarr)
    assert movie["arr_service"] == "radarr"
    # Not monitored in Radarr — tracked, not monitored.
    assert movie["arr_status"] == "tracked"
    assert movie["radarr_id"] == 9 and movie["sonarr_id"] is None

    none: dict[str, Any] = {}
    apply_arr_state(none, 999, sonarr, radarr)
    assert none["arr_status"] == "untracked"
    assert none["arr_service"] == ""
    # Fields still present, so templates never hit an undefined.
    assert none["sonarr_id"] is None and none["radarr_id"] is None
    assert none["sonarr_season"] is None and none["monitor_type"] == "future"


@pytest.mark.asyncio
async def test_apply_arr_state_handles_unmatched_items() -> None:
    """A library row with no AniList match is simply untracked."""
    from src.Web.Routes.Helpers import apply_arr_state

    item: dict[str, Any] = {}
    apply_arr_state(item, None, {1: {}}, {})
    assert item["arr_status"] == "untracked"
