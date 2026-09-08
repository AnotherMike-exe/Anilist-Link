"""Schema migration utilities for Anilist-Link.

Version 1 represents the consolidated 1.0 schema baseline.
All tables, indexes, and constraints are defined in Models.py (TABLES, INDEXES).
"""

from __future__ import annotations

import logging

import aiosqlite

from src.Database.Models import INDEXES, TABLES

logger = logging.getLogger(__name__)

LATEST_VERSION = 4


async def run_migrations(db: aiosqlite.Connection) -> None:
    """Check current schema version and apply pending migrations."""
    current = await _get_current_version(db)
    logger.info("Database schema version: %d (latest: %d)", current, LATEST_VERSION)

    if current < 1:
        await _apply_v1(db)
    if current < 2:
        await _apply_v2(db)
    if current < 3:
        await _apply_v3(db)
    if current < 4:
        await _apply_v4(db)


async def _get_current_version(db: aiosqlite.Connection) -> int:
    """Return the current schema version, or 0 if the table doesn't exist."""
    try:
        cursor = await db.execute("SELECT MAX(version) FROM schema_version")
        row = await cursor.fetchone()
        if row and row[0] is not None:
            return int(row[0])
    except aiosqlite.OperationalError:
        pass
    return 0


async def _apply_v1(db: aiosqlite.Connection) -> None:
    """Create all tables and indexes for the 1.0 schema baseline."""
    logger.info("Applying migration v1: creating 1.0 schema")

    for table_name, ddl in TABLES.items():
        await db.execute(ddl)
        logger.debug("Created table: %s", table_name)

    for index_ddl in INDEXES:
        await db.execute(index_ddl)

    await db.execute("INSERT INTO schema_version (version) VALUES (?)", (1,))
    await db.commit()
    logger.info("Migration v1 applied: 1.0 schema created successfully")


async def _apply_v2(db: aiosqlite.Connection) -> None:
    """Add title_native and title_synonyms columns to user_watchlist."""
    logger.info("Applying migration v2: user_watchlist synonyms columns")

    cursor = await db.execute("PRAGMA table_info(user_watchlist)")
    cols = {row[1] for row in await cursor.fetchall()}

    if "title_native" not in cols:
        await db.execute(
            "ALTER TABLE user_watchlist ADD COLUMN title_native TEXT"
            " NOT NULL DEFAULT ''"
        )
    if "title_synonyms" not in cols:
        await db.execute(
            "ALTER TABLE user_watchlist ADD COLUMN title_synonyms TEXT"
            " NOT NULL DEFAULT '[]'"
        )

    await db.execute("INSERT INTO schema_version (version) VALUES (?)", (2,))
    await db.commit()
    logger.info("Migration v2 applied: user_watchlist synonyms columns added")


async def _apply_v3(db: aiosqlite.Connection) -> None:
    """Add synonyms column to anilist_cache."""
    logger.info("Applying migration v3: anilist_cache synonyms column")

    cursor = await db.execute("PRAGMA table_info(anilist_cache)")
    cols = {row[1] for row in await cursor.fetchall()}

    if "synonyms" not in cols:
        await db.execute(
            "ALTER TABLE anilist_cache ADD COLUMN synonyms TEXT"
            " NOT NULL DEFAULT '[]'"
        )

    await db.execute("INSERT INTO schema_version (version) VALUES (?)", (3,))
    await db.commit()
    logger.info("Migration v3 applied: anilist_cache synonyms column added")


async def _apply_v4(db: aiosqlite.Connection) -> None:
    """Let a Sonarr season hold more than one AniList entry.

    The old primary key was (sonarr_id, season_number), so a split cour — two
    AniList entries inside one Sonarr season — could only store one of them;
    the second silently replaced the first and every episode in that season was
    filed under whichever won.  Adds an episode range and widens the key.

    Existing rows become whole-season mappings (1 → end), which is exactly what
    they meant under the old schema.
    """
    logger.info("Applying migration v4: per-episode-range season mappings")

    cursor = await db.execute("PRAGMA table_info(anilist_sonarr_season_mapping)")
    cols = {row[1] for row in await cursor.fetchall()}

    if "episode_start" not in cols:
        # SQLite can't alter a primary key, so rebuild the table.
        await db.execute("""CREATE TABLE anilist_sonarr_season_mapping_v4 (
                   sonarr_id     INTEGER NOT NULL,
                   season_number INTEGER NOT NULL,
                   anilist_id    INTEGER NOT NULL,
                   episode_start INTEGER NOT NULL DEFAULT 1,
                   episode_end   INTEGER,
                   created_at    TEXT DEFAULT (datetime('now')),
                   PRIMARY KEY (sonarr_id, season_number, episode_start)
               )""")
        await db.execute("""INSERT INTO anilist_sonarr_season_mapping_v4
                   (sonarr_id, season_number, anilist_id,
                    episode_start, episode_end, created_at)
               SELECT sonarr_id, season_number, anilist_id, 1, NULL, created_at
               FROM anilist_sonarr_season_mapping""")
        await db.execute("DROP TABLE anilist_sonarr_season_mapping")
        await db.execute(
            "ALTER TABLE anilist_sonarr_season_mapping_v4"
            " RENAME TO anilist_sonarr_season_mapping"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_assm_sonarr"
            " ON anilist_sonarr_season_mapping(sonarr_id)"
        )

    await db.execute("INSERT INTO schema_version (version) VALUES (?)", (4,))
    await db.commit()
    logger.info("Migration v4 applied: season mappings now carry episode ranges")
