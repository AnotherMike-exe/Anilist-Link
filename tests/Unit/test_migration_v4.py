"""Migration v4 — season mappings gain episode ranges without losing rows."""

from __future__ import annotations

import aiosqlite
import pytest

from src.Database.Migrations import LATEST_VERSION, run_migrations


@pytest.mark.asyncio
async def test_v4_preserves_existing_mappings_as_whole_season() -> None:
    """Pre-v4 rows meant 'this entry owns the season' — they must keep meaning it."""
    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        # Build the pre-v4 shape and seed it.
        await db.execute("""CREATE TABLE anilist_sonarr_season_mapping (
                   sonarr_id     INTEGER NOT NULL,
                   season_number INTEGER NOT NULL,
                   anilist_id    INTEGER NOT NULL,
                   created_at    TEXT DEFAULT (datetime('now')),
                   PRIMARY KEY (sonarr_id, season_number)
               )""")
        await db.execute("CREATE TABLE schema_version (version INTEGER)")
        await db.execute("INSERT INTO schema_version (version) VALUES (3)")
        await db.executemany(
            "INSERT INTO anilist_sonarr_season_mapping"
            " (sonarr_id, season_number, anilist_id) VALUES (?, ?, ?)",
            [(7, 1, 101), (7, 2, 202), (9, 1, 303)],
        )
        await db.commit()

        await run_migrations(db)

        cursor = await db.execute(
            "SELECT sonarr_id, season_number, anilist_id, episode_start, episode_end"
            " FROM anilist_sonarr_season_mapping ORDER BY sonarr_id, season_number"
        )
        rows = [dict(r) for r in await cursor.fetchall()]

    assert len(rows) == 3, "no mapping may be lost in the rebuild"
    assert [r["anilist_id"] for r in rows] == [101, 202, 303]
    # Whole-season semantics: from episode 1, with no end.
    assert all(r["episode_start"] == 1 for r in rows)
    assert all(r["episode_end"] is None for r in rows)


@pytest.mark.asyncio
async def test_v4_allows_two_entries_in_one_season() -> None:
    """The point of the migration: a split cour can now be stored."""
    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        await run_migrations(db)

        await db.executemany(
            "INSERT INTO anilist_sonarr_season_mapping"
            " (sonarr_id, season_number, anilist_id, episode_start, episode_end)"
            " VALUES (?, ?, ?, ?, ?)",
            [(7, 2, 111, 1, 12), (7, 2, 222, 13, 24)],
        )
        await db.commit()

        cursor = await db.execute(
            "SELECT anilist_id FROM anilist_sonarr_season_mapping"
            " WHERE sonarr_id=7 AND season_number=2 ORDER BY episode_start"
        )
        ids = [r["anilist_id"] for r in await cursor.fetchall()]

    assert ids == [111, 222], "both parts of the season must coexist"


@pytest.mark.asyncio
async def test_migrations_are_idempotent() -> None:
    """Running migrations twice must not fail or duplicate rows."""
    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        await run_migrations(db)
        await run_migrations(db)

        cursor = await db.execute("SELECT MAX(version) AS v FROM schema_version")
        row = await cursor.fetchone()

    assert row["v"] == LATEST_VERSION
