"""Work out which Sonarr season and episode range each AniList entry occupies.

AniList and TVDB disagree about what a season is.  A split cour is two AniList
entries where Sonarr sees one long season; a series AniList splits across a
sequel is often one TVDB season and vice versa.  Everything downstream — the
post-processor routing a file, the episode mapper translating numbering — needs
the same answer to that question, so it is computed in one place here.

Two assignments are possible, in order of confidence:

* **1:1** — the sequel chain and Sonarr's season list are the same length, so
  each entry owns a whole season.
* **cumulative** — episode counts are known for every entry, so the chain is
  laid end to end against Sonarr's per-season totals and each entry claims the
  slice of a season it lands in.

When neither holds, entries are left unassigned rather than guessed at: a wrong
season sends files to the wrong show.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# anilist_id -> (episode_start, episode_end) within its Sonarr season
EpisodeRanges = dict[int, tuple[int, int | None]]


def assign_season_ranges(
    chain: list[int],
    episode_counts: dict[int, int | None],
    sonarr_seasons: list[int],
    sonarr_season_totals: dict[int, int],
) -> tuple[dict[int, int | None], EpisodeRanges]:
    """Map each AniList entry in *chain* to a Sonarr season and episode range.

    *chain* is chronological.  Returns ``(season_map, episode_ranges)``, where a
    ``None`` season means "couldn't tell" and every assigned entry has a range
    expressed in its season's own numbering — which is what Sonarr labels
    episodes with.
    """
    season_map: dict[int, int | None] = {}
    episode_ranges: EpisodeRanges = {}

    if sonarr_seasons and len(chain) == len(sonarr_seasons):
        # Perfect 1:1 chronological assignment — each entry owns its season.
        for idx, aid in enumerate(chain):
            season_map[aid] = sonarr_seasons[idx]
            episode_ranges[aid] = (1, None)
        return season_map, episode_ranges

    if sonarr_seasons and all(episode_counts.get(aid) for aid in chain):
        # Lay the chain end to end and see which season each entry falls in.
        sonarr_ranges: list[tuple[int, int, int]] = []  # (start, end, season)
        cum = 1
        for sn in sonarr_seasons:
            total = sonarr_season_totals.get(sn, 0)
            if total > 0:
                sonarr_ranges.append((cum, cum + total - 1, sn))
                cum += total

        anilist_start = 1
        for aid in chain:
            eps = episode_counts.get(aid) or 0
            assigned: int | None = None
            for s_start, s_end, sn in sonarr_ranges:
                if anilist_start <= s_end:
                    assigned = sn
                    # Absolute chain position -> the season's own numbering.
                    rel_start = max(1, anilist_start - s_start + 1)
                    episode_ranges[aid] = (
                        rel_start,
                        rel_start + eps - 1 if eps else None,
                    )
                    break
            season_map[aid] = assigned
            anilist_start += eps
        return season_map, episode_ranges

    for aid in chain:
        season_map[aid] = None
    return season_map, episode_ranges


async def fetch_sonarr_seasons(
    sonarr: Any, sonarr_id: int
) -> tuple[list[int], dict[int, int]]:
    """Return (sorted season numbers, per-season episode totals) for a series.

    Season 0 is left out — specials are not part of the chronology.
    """
    seasons: list[int] = []
    totals: dict[int, int] = {}
    series = await sonarr.get_series_by_id(sonarr_id)
    if not series:
        return seasons, totals
    for s in series.get("seasons") or []:
        sn = s.get("seasonNumber", 0)
        if sn == 0:
            continue
        seasons.append(sn)
        stats = s.get("statistics") or {}
        totals[sn] = stats.get("totalEpisodeCount", 0)
    return sorted(seasons), totals


async def load_episode_counts(db: Any) -> dict[int, int | None]:
    """AniList episode counts, watchlist first and the metadata cache behind it.

    The cache is what covers a chain entry the user never added — a season one
    they finished years ago still has to be counted to place season two.
    """
    counts: dict[int, int | None] = {}
    users = await db.get_users_by_service("anilist")
    if users:
        rows = await db.fetch_all(
            "SELECT anilist_id, anilist_episodes FROM user_watchlist WHERE user_id=?",
            (users[0]["user_id"],),
        )
        for row in rows:
            counts[row["anilist_id"]] = row["anilist_episodes"]
    for row in await db.fetch_all("SELECT anilist_id, episodes FROM anilist_cache"):
        if row["anilist_id"] not in counts:
            counts[row["anilist_id"]] = row["episodes"]
    return counts


async def persist_season_ranges(
    db: Any,
    sonarr_id: int,
    season_map: dict[int, int | None],
    episode_ranges: EpisodeRanges,
    chain: list[int],
) -> int:
    """Replace this series' season mappings with a freshly computed set.

    The replacement is wholesale: a re-run may split a season that was
    previously stored as one whole-season row, and leaving the old row behind
    would keep it matching every episode.  Nothing is deleted when the new
    assignment is empty — an unusable answer must not destroy a usable one.
    """
    assigned = [aid for aid in chain if season_map.get(aid) is not None]
    if not assigned:
        return 0

    await db.execute(
        "DELETE FROM anilist_sonarr_season_mapping WHERE sonarr_id=?", (sonarr_id,)
    )
    for aid in assigned:
        ep_start, ep_end = episode_ranges.get(aid, (1, None))
        await db.execute(
            """INSERT OR REPLACE INTO anilist_sonarr_season_mapping
                   (sonarr_id, season_number, anilist_id, episode_start, episode_end)
               VALUES (?, ?, ?, ?, ?)""",
            (sonarr_id, season_map[aid], aid, ep_start, ep_end),
        )
    logger.info(
        "Persisted %d season mapping(s) for sonarr_id=%d: %s",
        len(assigned),
        sonarr_id,
        {aid: (season_map[aid], episode_ranges.get(aid)) for aid in assigned},
    )
    return len(assigned)


async def rebuild_season_ranges(
    db: Any,
    config: Any,
    anilist_client: Any,
    sonarr_id: int,
    seed_anilist_id: int | None = None,
) -> int:
    """Recompute and store the season ranges for one Sonarr series.

    Walks the AniList sequel chain afresh, so it also picks up a cour that
    aired after the series was linked.  Returns how many mappings were stored;
    zero means the assignment could not be made and nothing was changed.
    """
    from src.Clients.SonarrClient import SonarrClient
    from src.Utils.NamingTranslator import (
        collect_series_chain,
        resolve_tvdb_id,
        resolve_tvdb_via_prequel_chain,
    )

    if not config.sonarr.url or not config.sonarr.api_key:
        return 0

    # Any mapped entry seeds the walk — collect_series_chain follows sequel and
    # prequel edges both ways, so it does not have to be season one.
    seed = seed_anilist_id
    tvdb_id = 0
    row = await db.fetch_one(
        "SELECT anilist_id, tvdb_id FROM anilist_sonarr_mapping"
        " WHERE sonarr_id=? AND in_sonarr=1 AND tvdb_id IS NOT NULL"
        " ORDER BY anilist_id LIMIT 1",
        (sonarr_id,),
    )
    if row:
        seed = seed or int(row["anilist_id"])
        tvdb_id = int(row["tvdb_id"] or 0)
    if not seed:
        return 0

    if not tvdb_id:
        tvdb_id = await resolve_tvdb_id(seed, anilist_client) or 0
    if not tvdb_id:
        resolved, _ = await resolve_tvdb_via_prequel_chain(seed, anilist_client)
        tvdb_id = resolved or 0
    if not tvdb_id:
        logger.info(
            "Can't rebuild season ranges for sonarr_id=%d: no TVDB id", sonarr_id
        )
        return 0

    chain = await collect_series_chain(seed, tvdb_id, anilist_client)
    if not chain:
        return 0

    sonarr = SonarrClient(url=config.sonarr.url, api_key=config.sonarr.api_key)
    try:
        seasons, totals = await fetch_sonarr_seasons(sonarr, sonarr_id)
    finally:
        await sonarr.close()

    counts = await load_episode_counts(db)
    season_map, ranges = assign_season_ranges(chain, counts, seasons, totals)
    return await persist_season_ranges(db, sonarr_id, season_map, ranges, chain)
