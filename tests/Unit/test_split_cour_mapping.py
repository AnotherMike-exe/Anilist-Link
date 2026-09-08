"""Split-cour season mapping.

A Sonarr season can contain two AniList entries when a cour is split — e.g.
Mushoku Tensei S2 Part 1 (eps 1-12) and Part 2 (eps 13-24) are one Sonarr
season 2.  The old schema keyed mappings by (sonarr_id, season_number), so the
second part silently replaced the first and every season-2 file was routed to
whichever entry happened to be written last.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from src.Download.ArrPostProcessor import ArrPostProcessor
from src.Utils.Config import AppConfig, SonarrConfig


def _config() -> AppConfig:
    return AppConfig(sonarr=SonarrConfig(url="http://s:8989", api_key="k"))


def _db(rows: list[dict[str, Any]], series_level: int | None = None) -> MagicMock:
    """Mock DB whose season table returns *rows* for any season query."""
    db = MagicMock()

    async def fetch_all(query: str, params: tuple = ()) -> list[dict[str, Any]]:
        if "anilist_sonarr_season_mapping" in query and "season_number=?" in query:
            season = params[1]
            return [r for r in rows if r["season_number"] == season]
        return []

    async def fetch_one(query: str, params: tuple = ()) -> dict[str, Any] | None:
        if "anilist_sonarr_season_mapping" in query and "LIMIT 1" in query:
            return {"1": 1} if rows else None
        if "anilist_sonarr_mapping" in query and "sonarr_id=?" in query:
            return {"anilist_id": series_level} if series_level else None
        return None

    db.fetch_all = fetch_all
    db.fetch_one = fetch_one
    return db


def _range(season: int, aid: int, start: int, end: int | None) -> dict[str, Any]:
    return {
        "season_number": season,
        "anilist_id": aid,
        "episode_start": start,
        "episode_end": end,
    }


@pytest.mark.asyncio
async def test_split_cour_routes_each_part_to_its_own_entry() -> None:
    """The reported bug: both parts of a split season resolve independently."""
    rows = [
        _range(2, 111, 1, 12),  # S2 Part 1
        _range(2, 222, 13, 24),  # S2 Part 2
    ]
    processor = ArrPostProcessor(db=_db(rows), config=_config())

    assert await processor._resolve_sonarr_anilist_id(1, 2, 1) == 111
    assert await processor._resolve_sonarr_anilist_id(1, 2, 12) == 111
    assert await processor._resolve_sonarr_anilist_id(1, 2, 13) == 222
    assert await processor._resolve_sonarr_anilist_id(1, 2, 24) == 222


@pytest.mark.asyncio
async def test_whole_season_mapping_is_unchanged() -> None:
    """A 1:1 season (episode_end NULL) still matches every episode."""
    processor = ArrPostProcessor(db=_db([_range(1, 999, 1, None)]), config=_config())

    for ep in (1, 5, 99):
        assert await processor._resolve_sonarr_anilist_id(1, 1, ep) == 999


@pytest.mark.asyncio
async def test_no_episode_number_falls_back_to_first_range() -> None:
    """Callers without an episode number keep the previous behaviour."""
    rows = [_range(2, 111, 1, 12), _range(2, 222, 13, 24)]
    processor = ArrPostProcessor(db=_db(rows), config=_config())

    assert await processor._resolve_sonarr_anilist_id(1, 2) == 111


@pytest.mark.asyncio
async def test_episode_past_every_range_uses_the_last_part() -> None:
    """An episode beyond the mapped chain lands in the final part, not nowhere.

    Better a late episode joins the last known part than gets skipped and left
    sitting in Sonarr's import folder.
    """
    rows = [_range(2, 111, 1, 12), _range(2, 222, 13, 24)]
    processor = ArrPostProcessor(db=_db(rows), config=_config())

    assert await processor._resolve_sonarr_anilist_id(1, 2, 25) == 222


@pytest.mark.asyncio
async def test_unmapped_season_with_no_table_uses_series_level() -> None:
    """A 1:1 show with no season table still resolves via the series mapping."""
    processor = ArrPostProcessor(db=_db([], series_level=321), config=_config())

    assert await processor._resolve_sonarr_anilist_id(1, 1, 3) == 321
