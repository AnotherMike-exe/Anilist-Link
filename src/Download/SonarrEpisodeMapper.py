"""Associate already-placed files with Sonarr episodes, without touching them.

Our library is numbered the way AniList numbers things: a split cour is two
entries, so the second half is written as ``S02E01…`` even where TVDB — and
therefore Sonarr — calls the whole run season 1.  Sonarr sees those files but
cannot place them, so the episodes keep reading as missing and the interactive
import dialog lists them with no season or episode at all.

The fix is a translation, not a rename.  ``anilist_sonarr_season_mapping``
already records which Sonarr season and episode range each AniList entry
occupies, so an entry-relative episode number converts straight across::

    sonarr_episode = episode_start + (anilist_episode - 1)

with the result handed to Sonarr's manual import so it records the file exactly
where it already sits.  Sonarr only moves a file it considers a new download,
and it decides that by whether the file is already inside the series folder —
so every file is checked against that path before being sent, and anything
outside is refused rather than quietly relocated.

The mapping an entry needs may not exist yet — a series linked before episode
ranges were recorded has only a whole-season row — so it is rebuilt on demand
from the same AniList chain walk that linking runs, rather than asked of the
user.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from src.Clients.SonarrClient import SonarrClient
from src.Database.Connection import DatabaseManager
from src.Download.SeasonRangeMapper import rebuild_season_ranges
from src.Scanner.LibraryRestructurer import _extract_episode_info
from src.Utils.Config import AppConfig

logger = logging.getLogger(__name__)

# Sonarr rejects a manual import with no quality attached. Files it could not
# parse come back with none, so stand in the same "Unknown" it uses internally.
_UNKNOWN_QUALITY: dict[str, Any] = {
    "quality": {"id": 0, "name": "Unknown", "source": "unknown", "resolution": 0},
    "revision": {"version": 1, "real": 0, "isRepack": False},
}


class MappingError(Exception):
    """The mapping cannot be attempted, with a reason worth showing the user."""


def _under(parent: str, child: str) -> bool:
    """True when *child* sits inside *parent* (both arr-side paths)."""
    if not parent or not child:
        return False
    p = os.path.normpath(parent).replace("\\", "/").rstrip("/")
    c = os.path.normpath(child).replace("\\", "/")
    return c.startswith(p + "/")


class SonarrEpisodeMapper:
    """Translates AniList episode numbering into Sonarr's, then registers it."""

    def __init__(
        self,
        db: DatabaseManager,
        config: AppConfig,
        sonarr: SonarrClient | None = None,
        anilist_client: Any = None,
    ) -> None:
        self._db = db
        self._config = config
        self._sonarr = sonarr
        self._anilist = anilist_client

    # ------------------------------------------------------------------
    # Database lookups
    # ------------------------------------------------------------------

    async def _sonarr_id(self, anilist_id: int) -> int:
        row = await self._db.fetch_one(
            "SELECT sonarr_id FROM anilist_sonarr_mapping"
            " WHERE anilist_id=? AND in_sonarr=1",
            (anilist_id,),
        )
        if not row or not row["sonarr_id"]:
            raise MappingError("This entry isn't linked to a Sonarr series.")
        return int(row["sonarr_id"])

    async def _stored_range(
        self, sonarr_id: int, anilist_id: int
    ) -> tuple[int, int, int | None] | None:
        """The stored (sonarr_season, episode_start, episode_end), if any."""
        row = await self._db.fetch_one(
            "SELECT season_number, episode_start, episode_end"
            " FROM anilist_sonarr_season_mapping"
            " WHERE sonarr_id=? AND anilist_id=?"
            " ORDER BY season_number, episode_start LIMIT 1",
            (sonarr_id, anilist_id),
        )
        if not row:
            return None
        end = row["episode_end"]
        return (
            int(row["season_number"]),
            int(row["episode_start"] or 1),
            int(end) if end is not None else None,
        )

    async def _episode_range(
        self, sonarr_id: int, anilist_id: int, our_season: int
    ) -> tuple[int, int, int | None]:
        """Return (sonarr_season, episode_start, episode_end) for this entry.

        A missing mapping, or a sequel still holding a whole-season row from
        before episode ranges existed, is rebuilt here rather than handed back
        to the user as a chore — the chain walk that produces it is the same one
        linking the series ran, and it needs no input.
        """
        stored = await self._stored_range(sonarr_id, anilist_id)
        # A sequel whose range still starts at episode 1 with no end is a
        # whole-season row: offsetting by it would file cour two over cour one.
        stale = bool(stored and our_season > 1 and stored[1] == 1 and stored[2] is None)

        if stored and not stale:
            return stored

        rebuilt = 0
        try:
            rebuilt = await rebuild_season_ranges(
                self._db, self._config, self._anilist, sonarr_id, anilist_id
            )
        except Exception as exc:
            logger.warning(
                "Season range rebuild failed for sonarr_id=%d: %s", sonarr_id, exc
            )

        if rebuilt:
            fresh = await self._stored_range(sonarr_id, anilist_id)
            if fresh and not (our_season > 1 and fresh[1] == 1 and fresh[2] is None):
                return fresh

        if stale:
            raise MappingError(
                f"This entry is season {our_season} of its series, but the only"
                " Sonarr mapping we can work out covers the whole season — so we"
                " can't tell which episode it starts at. Check that the AniList"
                " entries in this series all have episode counts, then try again."
            )
        raise MappingError(
            "We couldn't work out which Sonarr season and episodes this entry"
            " covers. That usually means the series' AniList sequel chain or its"
            " TVDB link is incomplete — re-link the entry to Sonarr and try again."
        )

    async def _anilist_season_order(self, anilist_id: int) -> int:
        """The season number our own filenames use — the entry's place in the
        series group.  A standalone entry is season 1."""
        row = await self._db.fetch_one(
            "SELECT season_order FROM series_group_entries"
            " WHERE anilist_id=? ORDER BY season_order LIMIT 1",
            (anilist_id,),
        )
        if row and row["season_order"]:
            return int(row["season_order"])
        return 1

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------

    async def plan(self, anilist_id: int) -> dict[str, Any]:
        """Work out which files map to which Sonarr episodes. Changes nothing."""
        if not self._config.sonarr.url or not self._config.sonarr.api_key:
            raise MappingError("Sonarr isn't configured.")

        sonarr_id = await self._sonarr_id(anilist_id)
        our_season = await self._anilist_season_order(anilist_id)
        season, ep_start, ep_end = await self._episode_range(
            sonarr_id, anilist_id, our_season
        )

        client = self._sonarr or SonarrClient(
            url=self._config.sonarr.url, api_key=self._config.sonarr.api_key
        )
        owns_client = self._sonarr is None
        try:
            series = await client.get_series_by_id(sonarr_id)
            if not series:
                raise MappingError(f"Sonarr no longer has series {sonarr_id}.")
            series_path = series.get("path") or ""
            if not series_path:
                raise MappingError("Sonarr has no path recorded for this series.")

            episodes = await client.get_episodes(sonarr_id)
            by_number: dict[tuple[int, int], dict[str, Any]] = {}
            for ep in episodes:
                try:
                    key = (int(ep["seasonNumber"]), int(ep["episodeNumber"]))
                except (KeyError, TypeError, ValueError):
                    continue
                by_number[key] = ep

            candidates = await client.get_manual_import_candidates(
                series_path, series_id=sonarr_id, filter_existing_files=True
            )
        finally:
            if owns_client:
                await client.close()

        matched: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        used_episode_ids: set[int] = set()

        for cand in candidates:
            path = cand.get("path") or ""
            name = os.path.basename(path)
            if not path:
                continue
            if not _under(series_path, path):
                # Importing this would make Sonarr treat it as a new download
                # and move it. Refuse rather than relocate anything.
                skipped.append(
                    {"file": name, "path": path, "reason": "outside the series folder"}
                )
                continue

            info = _extract_episode_info(name)
            if not info:
                skipped.append(
                    {"file": name, "path": path, "reason": "no episode number found"}
                )
                continue
            if info.source_season == 0:
                skipped.append(
                    {
                        "file": name,
                        "path": path,
                        "reason": "special/extra, not an episode",
                    }
                )
                continue
            file_season = info.source_season if info.source_season is not None else 1
            if file_season != our_season:
                skipped.append(
                    {
                        "file": name,
                        "path": path,
                        "reason": f"belongs to season {file_season}, not this entry",
                    }
                )
                continue

            try:
                our_ep = int(float(info.number))
            except (TypeError, ValueError):
                skipped.append(
                    {"file": name, "path": path, "reason": "unparseable episode number"}
                )
                continue

            sonarr_ep = ep_start + (our_ep - 1)
            if ep_end is not None and sonarr_ep > ep_end:
                skipped.append(
                    {
                        "file": name,
                        "path": path,
                        "reason": (
                            f"episode {our_ep} falls past the mapped range"
                            f" (S{season:02d}E{ep_start}-{ep_end})"
                        ),
                    }
                )
                continue

            episode = by_number.get((season, sonarr_ep))
            if not episode:
                skipped.append(
                    {
                        "file": name,
                        "path": path,
                        "reason": f"Sonarr has no S{season:02d}E{sonarr_ep:02d}",
                    }
                )
                continue
            if episode.get("hasFile"):
                # Importing over an episode Sonarr already has a file for is an
                # upgrade, and an upgrade deletes the file being replaced. This
                # tool exists to fill gaps, never to swap files out.
                skipped.append(
                    {
                        "file": name,
                        "path": path,
                        "reason": f"S{season:02d}E{sonarr_ep:02d} already has a file",
                    }
                )
                continue
            episode_id = int(episode["id"])
            if episode_id in used_episode_ids:
                skipped.append(
                    {
                        "file": name,
                        "path": path,
                        "reason": f"S{season:02d}E{sonarr_ep:02d} already claimed"
                        " by another file",
                    }
                )
                continue
            used_episode_ids.add(episode_id)

            matched.append(
                {
                    "file": name,
                    "path": path,
                    "our_episode": our_ep,
                    "sonarr_season": season,
                    "sonarr_episode": sonarr_ep,
                    "episode_id": episode_id,
                    "episode_title": episode.get("title") or "",
                    "quality": cand.get("quality") or _UNKNOWN_QUALITY,
                    "languages": cand.get("languages") or [],
                    "release_group": cand.get("releaseGroup") or "",
                    "indexer_flags": cand.get("indexerFlags") or 0,
                    "release_type": cand.get("releaseType") or "unknown",
                }
            )

        return {
            "anilist_id": anilist_id,
            "sonarr_id": sonarr_id,
            "series_title": series.get("title") or "",
            "series_path": series_path,
            "our_season": our_season,
            "sonarr_season": season,
            "episode_start": ep_start,
            "episode_end": ep_end,
            "matched": matched,
            "skipped": skipped,
        }

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def apply(self, anilist_id: int) -> dict[str, Any]:
        """Plan, then hand the matched files to Sonarr's manual import."""
        plan = await self.plan(anilist_id)
        matched = plan["matched"]
        if not matched:
            return {**plan, "imported": 0, "command_id": None}

        series_path = plan["series_path"]
        files: list[dict[str, Any]] = []
        for m in matched:
            # Re-check rather than trust the plan: this is the single condition
            # that keeps Sonarr from moving the file.
            if not _under(series_path, m["path"]):
                raise MappingError(
                    f"Refusing to import {m['file']}: it is not inside the series"
                    " folder, so Sonarr would move it."
                )
            files.append(
                {
                    "path": m["path"],
                    "seriesId": plan["sonarr_id"],
                    "episodeIds": [m["episode_id"]],
                    "quality": m["quality"],
                    "languages": m["languages"],
                    "releaseGroup": m["release_group"],
                    "indexerFlags": m["indexer_flags"],
                    "releaseType": m["release_type"],
                }
            )

        client = self._sonarr or SonarrClient(
            url=self._config.sonarr.url, api_key=self._config.sonarr.api_key
        )
        owns_client = self._sonarr is None
        try:
            command = await client.manual_import(files, import_mode="Auto")
        finally:
            if owns_client:
                await client.close()

        logger.info(
            "Mapped %d file(s) to Sonarr series %d as S%02dE%02d–E%02d"
            " (anilist_id=%d, no files moved)",
            len(files),
            plan["sonarr_id"],
            plan["sonarr_season"],
            matched[0]["sonarr_episode"],
            matched[-1]["sonarr_episode"],
            anilist_id,
        )
        return {
            **plan,
            "imported": len(files),
            "command_id": command.get("id") if isinstance(command, dict) else None,
        }
