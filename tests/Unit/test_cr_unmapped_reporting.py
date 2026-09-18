"""The syncers must report a CR season they cannot place, and retire the report.

End-to-end through WatchSyncer._process_series_entry with AniList and Crunchyroll
stubbed, so the recording path is exercised the way a real sync hits it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import aiosqlite
import pytest_asyncio

from src.Database.Connection import DatabaseManager
from src.Database.Migrations import run_migrations
from src.Matching.TitleMatcher import TitleMatcher
from src.Sync.WatchSyncer import WatchSyncer
from src.Utils.Config import AppConfig

USER = {"user_id": "anilist_393115", "access_token": "tok", "anilist_id": 393115}
REZERO = "Re:ZERO -Starting Life in Another World-"


def _media(
    id: int, romaji: str, english: str, episodes: int | None, year: int, month: int
) -> dict[str, Any]:
    return {
        "id": id,
        "format": "TV",
        "episodes": episodes,
        "startDate": {"year": year, "month": month, "day": 1},
        "title": {"romaji": romaji, "english": english, "native": ""},
        "synonyms": [],
    }


REZERO_MEDIA = [
    _media(
        21355,
        "Re:Zero kara Hajimeru Isekai Seikatsu",
        "Re:ZERO -Starting Life in Another World-",
        25,
        2016,
        4,
    ),
    _media(
        108632,
        "Re:Zero kara Hajimeru Isekai Seikatsu 2nd Season",
        "Re:ZERO -Starting Life in Another World- Season 2",
        13,
        2020,
        7,
    ),
    _media(
        119661,
        "Re:Zero kara Hajimeru Isekai Seikatsu 2nd Season Part 2",
        "Re:ZERO -Starting Life in Another World- Season 2 Part 2",
        12,
        2021,
        1,
    ),
    _media(
        163134,
        "Re:Zero kara Hajimeru Isekai Seikatsu 3rd Season",
        "Re:ZERO -Starting Life in Another World- Season 3",
        16,
        2024,
        10,
    ),
    _media(
        189046,
        "Re:Zero kara Hajimeru Isekai Seikatsu 4th Season",
        "Re:ZERO -Starting Life in Another World- Season 4",
        19,
        2026,
        4,
    ),
]


@pytest_asyncio.fixture
async def db():
    manager = DatabaseManager(db_path=Path(":memory:"))
    manager._db = await aiosqlite.connect(":memory:")
    manager._db.row_factory = aiosqlite.Row
    await run_migrations(manager._db)
    yield manager
    await manager._db.close()


def _syncer(
    db: DatabaseManager,
    media: list[dict[str, Any]],
    list_entry: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> WatchSyncer:
    anilist = MagicMock()

    async def search_anime(query: str, *a: Any, **k: Any) -> list[dict[str, Any]]:
        return list(media)

    async def get_anime_list_entry(*a: Any, **k: Any) -> dict[str, Any] | None:
        return list_entry

    async def update_anime_progress(*a: Any, **k: Any) -> dict[str, Any]:
        return {"id": 1}

    anilist.search_anime = search_anime
    anilist.get_anime_list_entry = get_anime_list_entry
    anilist.update_anime_progress = update_anime_progress

    syncer = WatchSyncer(
        db=db,
        anilist_client=anilist,
        title_matcher=TitleMatcher(),
        cr_client=MagicMock(),
        config=AppConfig(),
        dry_run=dry_run,
    )
    syncer._reset_results()
    return syncer


class TestUnknownSeasonIsReported:
    async def test_season_beyond_the_map_is_recorded(self, db: DatabaseManager) -> None:
        """CR season 9 has no AniList entry — the user must be able to see that."""
        syncer = _syncer(db, REZERO_MEDIA)
        handled = await syncer._process_series_entry(REZERO, 9, 3, USER)

        assert handled is False
        rows = await db.get_cr_unmapped(USER["user_id"])
        assert len(rows) == 1
        assert rows[0]["series_title"] == REZERO
        assert rows[0]["cr_season"] == 9
        assert rows[0]["cr_episode"] == 3
        assert rows[0]["reason"] == "unknown_season"

    async def test_detail_names_the_seasons_that_are_known(
        self, db: DatabaseManager
    ) -> None:
        syncer = _syncer(db, REZERO_MEDIA)
        await syncer._process_series_entry(REZERO, 9, 3, USER)
        rows = await db.get_cr_unmapped(USER["user_id"])
        assert "season 9" in rows[0]["detail"]
        assert "1, 2, 3, 4" in rows[0]["detail"]

    async def test_no_anilist_results_is_recorded(self, db: DatabaseManager) -> None:
        syncer = _syncer(db, [])
        handled = await syncer._process_series_entry("Totally Unknown Show", 1, 1, USER)

        assert handled is False
        rows = await db.get_cr_unmapped(USER["user_id"])
        assert len(rows) == 1
        assert rows[0]["reason"] == "no_anilist_results"

    async def test_dry_run_records_nothing(self, db: DatabaseManager) -> None:
        """A dry run must not write to the database."""
        syncer = _syncer(db, REZERO_MEDIA, dry_run=True)
        await syncer._process_series_entry(REZERO, 9, 3, USER)
        assert await db.get_cr_unmapped(USER["user_id"]) == []


class TestMappableSeasonClearsTheReport:
    async def test_a_season_that_resolves_is_not_reported(
        self, db: DatabaseManager
    ) -> None:
        """CR season 4 resolves to 189046, so nothing should be flagged."""
        syncer = _syncer(
            db, REZERO_MEDIA, list_entry={"progress": 15, "status": "CURRENT"}
        )
        await syncer._process_series_entry(REZERO, 4, 15, USER)
        assert await db.get_cr_unmapped(USER["user_id"]) == []

    async def test_an_open_report_is_retired_once_mapping_works(
        self, db: DatabaseManager
    ) -> None:
        """After the matcher fix, a previously-unmappable season must clear."""
        await db.record_cr_unmapped(
            USER["user_id"], REZERO, 4, 15, "unknown_season", "stale report"
        )
        syncer = _syncer(
            db, REZERO_MEDIA, list_entry={"progress": 15, "status": "CURRENT"}
        )
        await syncer._process_series_entry(REZERO, 4, 15, USER)
        assert await db.get_cr_unmapped(USER["user_id"], include_resolved=True) == []

    async def test_an_unrelated_open_report_survives(self, db: DatabaseManager) -> None:
        await db.record_cr_unmapped(
            USER["user_id"], REZERO, 9, 3, "unknown_season", "still broken"
        )
        syncer = _syncer(
            db, REZERO_MEDIA, list_entry={"progress": 15, "status": "CURRENT"}
        )
        await syncer._process_series_entry(REZERO, 4, 15, USER)
        rows = await db.get_cr_unmapped(USER["user_id"])
        assert len(rows) == 1
        assert rows[0]["cr_season"] == 9
