"""Tests for cr_unmapped_episodes — Crunchyroll history the sync cannot place.

cr_sync_log records only successful writes, so an episode with no AniList target
used to leave no trace a user could see. Re:Zero season 4 Part 2 vanished exactly
that way: the old code folded the unknown season onto season 1, which was already
COMPLETED, so nothing was written and nothing was logged above DEBUG.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest_asyncio

from src.Database.Connection import DatabaseManager
from src.Database.Migrations import LATEST_VERSION, run_migrations

USER = "anilist_393115"
REZERO = "Re:ZERO -Starting Life in Another World-"
MUSHOKU = "Mushoku Tensei: Jobless Reincarnation"


@pytest_asyncio.fixture
async def db():
    manager = DatabaseManager(db_path=Path(":memory:"))
    manager._db = await aiosqlite.connect(":memory:")
    manager._db.row_factory = aiosqlite.Row
    await run_migrations(manager._db)
    yield manager
    await manager._db.close()


class TestMigration:
    async def test_schema_is_at_latest_version(self, db: DatabaseManager) -> None:
        row = await db.fetch_one("SELECT MAX(version) AS v FROM schema_version")
        assert row is not None
        assert row["v"] == LATEST_VERSION

    async def test_table_and_index_exist(self, db: DatabaseManager) -> None:
        table = await db.fetch_one(
            "SELECT name FROM sqlite_master"
            " WHERE type='table' AND name='cr_unmapped_episodes'"
        )
        assert table is not None
        index = await db.fetch_one(
            "SELECT name FROM sqlite_master"
            " WHERE type='index' AND name='idx_cr_unmapped_open'"
        )
        assert index is not None


class TestRecording:
    async def test_records_an_unmappable_season(self, db: DatabaseManager) -> None:
        await db.record_cr_unmapped(
            USER, REZERO, 5, 3, "unknown_season", "season map covers 1, 2, 3, 4"
        )
        rows = await db.get_cr_unmapped(USER)
        assert len(rows) == 1
        assert rows[0]["series_title"] == REZERO
        assert rows[0]["cr_season"] == 5
        assert rows[0]["cr_episode"] == 3
        assert rows[0]["reason"] == "unknown_season"
        assert rows[0]["resolved_at"] is None

    async def test_repeated_syncs_update_one_row(self, db: DatabaseManager) -> None:
        """A weekly sync must not pile up a row per run."""
        for episode in (3, 4, 5):
            await db.record_cr_unmapped(
                USER, REZERO, 5, episode, "unknown_season", "detail"
            )
        rows = await db.get_cr_unmapped(USER)
        assert len(rows) == 1
        assert rows[0]["cr_episode"] == 5

    async def test_keeps_the_highest_episode_seen(self, db: DatabaseManager) -> None:
        """CR history is newest-first, so a lower episode may arrive later."""
        await db.record_cr_unmapped(USER, REZERO, 5, 9, "unknown_season", "d")
        await db.record_cr_unmapped(USER, REZERO, 5, 2, "unknown_season", "d")
        rows = await db.get_cr_unmapped(USER)
        assert rows[0]["cr_episode"] == 9

    async def test_seasons_are_tracked_separately(self, db: DatabaseManager) -> None:
        await db.record_cr_unmapped(USER, REZERO, 5, 3, "unknown_season", "d")
        await db.record_cr_unmapped(USER, REZERO, 6, 1, "unknown_season", "d")
        assert len(await db.get_cr_unmapped(USER)) == 2

    async def test_series_are_tracked_separately(self, db: DatabaseManager) -> None:
        await db.record_cr_unmapped(USER, REZERO, 5, 3, "unknown_season", "d")
        await db.record_cr_unmapped(USER, MUSHOKU, 3, 11, "unknown_season", "d")
        assert len(await db.get_cr_unmapped(USER)) == 2

    async def test_season_title_is_kept(self, db: DatabaseManager) -> None:
        await db.record_cr_unmapped(
            USER, REZERO, 5, 3, "unknown_season", "d", season_title="Season 4 Part 2"
        )
        rows = await db.get_cr_unmapped(USER)
        assert rows[0]["season_title"] == "Season 4 Part 2"

    async def test_other_users_rows_are_not_returned(self, db: DatabaseManager) -> None:
        await db.record_cr_unmapped(USER, REZERO, 5, 3, "unknown_season", "d")
        await db.record_cr_unmapped("anilist_other", MUSHOKU, 3, 1, "x", "d")
        rows = await db.get_cr_unmapped(USER)
        assert len(rows) == 1
        assert rows[0]["series_title"] == REZERO


class TestResolving:
    async def test_resolved_rows_are_hidden_by_default(
        self, db: DatabaseManager
    ) -> None:
        await db.record_cr_unmapped(USER, REZERO, 5, 3, "unknown_season", "d")
        row = (await db.get_cr_unmapped(USER))[0]
        await db.resolve_cr_unmapped(row["id"], 189046)
        assert await db.get_cr_unmapped(USER) == []
        assert len(await db.get_cr_unmapped(USER, include_resolved=True)) == 1

    async def test_resolution_records_the_chosen_entry(
        self, db: DatabaseManager
    ) -> None:
        await db.record_cr_unmapped(USER, REZERO, 5, 3, "unknown_season", "d")
        row = (await db.get_cr_unmapped(USER))[0]
        await db.resolve_cr_unmapped(row["id"], 189046)
        after = await db.get_cr_unmapped_entry(row["id"])
        assert after is not None
        assert after["resolved_anilist_id"] == 189046
        assert after["resolved_at"]

    async def test_dismissal_records_no_entry(self, db: DatabaseManager) -> None:
        await db.record_cr_unmapped(USER, REZERO, 5, 3, "unknown_season", "d")
        row = (await db.get_cr_unmapped(USER))[0]
        await db.resolve_cr_unmapped(row["id"], None)
        after = await db.get_cr_unmapped_entry(row["id"])
        assert after is not None
        assert after["resolved_anilist_id"] is None
        assert after["resolved_at"]

    async def test_still_broken_season_reopens_the_report(
        self, db: DatabaseManager
    ) -> None:
        """A season that still cannot be mapped is still a problem."""
        await db.record_cr_unmapped(USER, REZERO, 5, 3, "unknown_season", "d")
        row = (await db.get_cr_unmapped(USER))[0]
        await db.resolve_cr_unmapped(row["id"], 189046)
        await db.record_cr_unmapped(USER, REZERO, 5, 4, "unknown_season", "d")
        rows = await db.get_cr_unmapped(USER)
        assert len(rows) == 1
        assert rows[0]["cr_episode"] == 4
        assert rows[0]["resolved_at"] is None

    async def test_clear_removes_the_row_when_mapping_works_again(
        self, db: DatabaseManager
    ) -> None:
        """Once the matcher can place the season, the report should disappear."""
        await db.record_cr_unmapped(USER, MUSHOKU, 3, 11, "unknown_season", "d")
        await db.clear_cr_unmapped(USER, MUSHOKU, 3)
        assert await db.get_cr_unmapped(USER, include_resolved=True) == []

    async def test_clear_only_touches_the_named_season(
        self, db: DatabaseManager
    ) -> None:
        await db.record_cr_unmapped(USER, MUSHOKU, 3, 11, "unknown_season", "d")
        await db.record_cr_unmapped(USER, MUSHOKU, 4, 1, "unknown_season", "d")
        await db.clear_cr_unmapped(USER, MUSHOKU, 3)
        rows = await db.get_cr_unmapped(USER)
        assert len(rows) == 1
        assert rows[0]["cr_season"] == 4

    async def test_clear_on_a_clean_series_is_a_no_op(
        self, db: DatabaseManager
    ) -> None:
        await db.clear_cr_unmapped(USER, MUSHOKU, 3)
        assert await db.get_cr_unmapped(USER) == []

    async def test_missing_entry_lookup_returns_none(self, db: DatabaseManager) -> None:
        assert await db.get_cr_unmapped_entry(9999) is None
