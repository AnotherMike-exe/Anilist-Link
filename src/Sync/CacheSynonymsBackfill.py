"""One-shot backfill for the anilist_cache.synonyms column.

When the ``synonyms`` column was added to ``anilist_cache``, every existing
row was filled with the literal placeholder ``'[]'``.  The metadata scanners
short-circuit on cache hits, so without this task those rows would never be
re-fetched and the unified library search bar wouldn't match synonyms even
after a full re-scan.

This module re-fetches each row with an empty synonyms value from AniList
and updates the cache in place.  It is idempotent and safe to run repeatedly
— a row that already has non-empty synonyms is skipped.
"""

from __future__ import annotations

import json
import logging

from src.Clients.AnilistClient import AniListClient
from src.Database.Connection import DatabaseManager

logger = logging.getLogger(__name__)


async def backfill_cache_synonyms(
    db: DatabaseManager,
    anilist_client: AniListClient,
    *,
    batch_log_every: int = 25,
) -> int:
    """Populate ``synonyms`` for every cached row currently sitting at ``'[]'``.

    Returns the number of rows updated.  Honors the AniList client's
    token-bucket rate limiter so a large backfill won't exceed the
    90-req/min cap.
    """
    rows = await db.fetch_all(
        "SELECT anilist_id FROM anilist_cache"
        " WHERE synonyms IS NULL OR synonyms='' OR synonyms='[]'"
    )
    if not rows:
        logger.debug("Synonyms backfill: nothing to do")
        return 0

    logger.info(
        "Synonyms backfill: %d cached row(s) missing synonyms — fetching from AniList",
        len(rows),
    )

    updated = 0
    for i, row in enumerate(rows, start=1):
        anilist_id = row["anilist_id"]
        try:
            entry = await anilist_client.get_anime_by_id(anilist_id)
        except Exception:
            logger.debug(
                "Synonyms backfill: AniList fetch failed for %d",
                anilist_id,
                exc_info=True,
            )
            continue
        if not entry:
            continue
        synonyms = [s for s in (entry.get("synonyms") or []) if s]
        try:
            await db.execute(
                "UPDATE anilist_cache SET synonyms=? WHERE anilist_id=?",
                (json.dumps(synonyms), anilist_id),
            )
            updated += 1
        except Exception:
            logger.debug(
                "Synonyms backfill: DB update failed for %d",
                anilist_id,
                exc_info=True,
            )
            continue

        if i % batch_log_every == 0:
            logger.info("Synonyms backfill: progress %d/%d", i, len(rows))

    logger.info(
        "Synonyms backfill complete: %d row(s) updated of %d candidate(s)",
        updated,
        len(rows),
    )
    return updated
