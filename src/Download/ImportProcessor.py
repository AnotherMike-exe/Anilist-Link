"""Bring manually-acquired media into the library, then tell *arr about it.

Sonarr and Radarr can't always find a release — a split cour numbered by
AniList rather than TVDB is the usual case — so files get fetched by hand and
dropped somewhere.  Two entry points bring them in:

* an entry's own folder is re-scanned in place, for files dropped next to a
  show that is already in the library; and
* a watched import folder, for anything dropped in without a home yet.

Both share the back half implemented here: run the restructurer so naming and
placement match everything else, then point *arr at the result and rescan so it
stops reporting the episodes as missing.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from src.Clients.RadarrClient import RadarrClient
from src.Clients.SonarrClient import SonarrClient
from src.Database.Connection import DatabaseManager
from src.Scanner.LibraryRestructurer import (
    LibraryRestructurer,
    RestructureProgress,
    ShowInput,
)
from src.Utils.Config import AppConfig

logger = logging.getLogger(__name__)

MEDIA_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".avi",
    ".m4v",
    ".mov",
    ".wmv",
    ".flv",
    ".webm",
    ".ts",
    ".m2ts",
}


def has_media(directory: str) -> bool:
    """True when *directory* contains at least one video file, at any depth."""
    for _root, _dirs, files in os.walk(directory):
        for name in files:
            if os.path.splitext(name)[1].lower() in MEDIA_EXTENSIONS:
                return True
    return False


def count_media(directory: str) -> int:
    """Number of video files under *directory*."""
    total = 0
    for _root, _dirs, files in os.walk(directory):
        for name in files:
            if os.path.splitext(name)[1].lower() in MEDIA_EXTENSIONS:
                total += 1
    return total


@dataclass
class ImportPlan:
    """What an import would do, before anything is touched."""

    source_folder: str
    anilist_id: int = 0
    anilist_title: str = ""
    match_confidence: float = 0.0
    match_method: str = ""
    target_folder: str = ""
    media_count: int = 0
    moves: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """True when this plan has everything it needs to run."""
        return bool(self.anilist_id and self.target_folder and self.moves)


class ImportProcessor:
    """Restructures dropped-in media and notifies Sonarr/Radarr."""

    def __init__(
        self,
        db: DatabaseManager,
        config: AppConfig,
        anilist_client: Any = None,
        app_state: Any = None,
    ) -> None:
        self._db = db
        self._config = config
        self._anilist = anilist_client
        self._app_state = app_state

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------

    async def plan_folder(
        self,
        folder: str,
        anilist_id: int,
        output_dir: str | None = None,
    ) -> ImportPlan:
        """Work out where *folder*'s contents belong, without moving anything.

        The AniList entry is given rather than guessed — the caller either knows
        it (re-scanning a library entry) or has had it confirmed by the user
        (the import folder review step).
        """
        plan = ImportPlan(source_folder=folder, anilist_id=anilist_id)
        plan.media_count = count_media(folder)
        if not plan.media_count:
            plan.warnings.append("No video files found in this folder.")
            return plan

        info = await self._db.get_cached_metadata(anilist_id) or {}
        plan.anilist_title = info.get("title_romaji") or info.get("title_english") or ""

        show = ShowInput(
            title=os.path.basename(folder.rstrip("/")),
            local_path=folder,
            source_id=folder,
            anilist_id=anilist_id,
            anilist_title=plan.anilist_title,
            year=int(info.get("year") or 0),
            anilist_title_romaji=info.get("title_romaji") or "",
            anilist_title_english=info.get("title_english") or "",
            anilist_format=info.get("format") or "",
            anilist_episodes=info.get("episodes"),
        )

        restructurer = await LibraryRestructurer.from_settings(self._db, self._anilist)
        progress = RestructureProgress(status="running")
        rplan = await restructurer.analyze(
            [show],
            progress,
            level="full_restructure",
            output_dir=output_dir or None,
            force_franchise_root=True,
        )
        if not rplan.groups:
            plan.warnings.append(
                "Nothing to do — the files are already in their standard location."
            )
            return plan

        group = rplan.groups[0]
        plan.target_folder = group.target_folder
        for mv in group.file_moves:
            plan.moves.append(
                {
                    "from": mv.source,
                    "to": mv.destination,
                    "renamed_to": mv.renamed_filename,
                }
            )
        plan.warnings.extend(group.warnings)
        return plan

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute_folder(
        self,
        folder: str,
        anilist_id: int,
        output_dir: str | None = None,
    ) -> dict[str, Any]:
        """Restructure *folder* into the library, then notify *arr.

        Returns a summary including what *arr was told.  A failure to notify
        never fails the import — the files have already moved correctly, and a
        stale *arr view is recoverable with Verify Link.
        """
        info = await self._db.get_cached_metadata(anilist_id) or {}
        show = ShowInput(
            title=os.path.basename(folder.rstrip("/")),
            local_path=folder,
            source_id=folder,
            anilist_id=anilist_id,
            anilist_title=(info.get("title_romaji") or info.get("title_english") or ""),
            year=int(info.get("year") or 0),
            anilist_title_romaji=info.get("title_romaji") or "",
            anilist_title_english=info.get("title_english") or "",
            anilist_format=info.get("format") or "",
            anilist_episodes=info.get("episodes"),
        )

        restructurer = await LibraryRestructurer.from_settings(self._db, self._anilist)
        progress = RestructureProgress(status="running")
        rplan = await restructurer.analyze(
            [show],
            progress,
            level="full_restructure",
            output_dir=output_dir or None,
            force_franchise_root=True,
        )
        if not rplan.groups:
            # Nothing to move, but that does not mean nothing to do: the files
            # can be correctly placed and *arr still unaware of them, which is
            # exactly the case this path exists to fix.  Tell it anyway.
            arr = await self.notify_arr(anilist_id)
            return {
                "ok": True,
                "moved": 0,
                "message": "Files were already in the correct location.",
                "arr": arr,
            }

        target = rplan.groups[0].target_folder
        stats = await restructurer.execute(rplan, progress)
        moved = int(stats.get("files_moved", 0) or 0)
        errors = int(stats.get("errors", 0) or 0)

        arr = await self.notify_arr(anilist_id)
        return {
            "ok": True,
            "moved": moved,
            "errors": errors,
            "target_folder": target,
            "arr": arr,
        }

    # ------------------------------------------------------------------
    # Telling *arr the files exist
    # ------------------------------------------------------------------

    async def notify_arr(self, anilist_id: int) -> dict[str, Any]:
        """Point Sonarr/Radarr at the library folder and rescan.

        A rescan is always issued, even when the stored path was already right
        — the whole point is that files appeared which *arr has never seen.
        """
        from src.Download.ArrPostProcessor import ArrPostProcessor

        processor = ArrPostProcessor(
            db=self._db, config=self._config, app_state=self._app_state
        )

        row = await self._db.fetch_one(
            "SELECT sonarr_id FROM anilist_sonarr_mapping"
            " WHERE anilist_id=? AND in_sonarr=1",
            (anilist_id,),
        )
        if row and row["sonarr_id"]:
            sonarr_id = int(row["sonarr_id"])
            if not self._config.sonarr.url or not self._config.sonarr.api_key:
                return {"service": "sonarr", "action": "not_configured"}
            client = SonarrClient(
                url=self._config.sonarr.url, api_key=self._config.sonarr.api_key
            )
            try:
                sync = await processor.sync_sonarr_series_path(
                    sonarr_id, anilist_id, sonarr=client
                )
                await client.rescan_series(sonarr_id)
                logger.info(
                    "Told Sonarr id=%d to rescan after import (path: %s)",
                    sonarr_id,
                    sync.get("action"),
                )
                return {
                    "service": "sonarr",
                    "arr_id": sonarr_id,
                    "path_action": sync.get("action"),
                    "rescanned": True,
                }
            except Exception as exc:
                logger.warning("Sonarr notify failed after import: %s", exc)
                return {"service": "sonarr", "error": str(exc)}
            finally:
                await client.close()

        row = await self._db.fetch_one(
            "SELECT radarr_id FROM anilist_radarr_mapping"
            " WHERE anilist_id=? AND in_radarr=1",
            (anilist_id,),
        )
        if row and row["radarr_id"]:
            radarr_id = int(row["radarr_id"])
            if not self._config.radarr.url or not self._config.radarr.api_key:
                return {"service": "radarr", "action": "not_configured"}
            client_r = RadarrClient(
                url=self._config.radarr.url, api_key=self._config.radarr.api_key
            )
            try:
                sync = await processor.sync_radarr_movie_path(
                    radarr_id, anilist_id, radarr=client_r
                )
                await client_r.rescan_movie(radarr_id)
                logger.info(
                    "Told Radarr id=%d to rescan after import (path: %s)",
                    radarr_id,
                    sync.get("action"),
                )
                return {
                    "service": "radarr",
                    "arr_id": radarr_id,
                    "path_action": sync.get("action"),
                    "rescanned": True,
                }
            except Exception as exc:
                logger.warning("Radarr notify failed after import: %s", exc)
                return {"service": "radarr", "error": str(exc)}
            finally:
                await client_r.close()

        return {"service": "", "action": "not_linked"}
