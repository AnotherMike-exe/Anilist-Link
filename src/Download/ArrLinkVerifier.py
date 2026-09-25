"""Re-checks stored Sonarr/Radarr links against the services themselves.

Two kinds of drift accumulate after an entry is linked:

* the series/movie is deleted in Sonarr/Radarr, but our mapping still claims
  it is tracked, so the UI offers no way to re-add it;
* the files are moved (by the restructurer) after the link was made, leaving
  the service pointing at the old location and reporting nothing downloaded.

Linking an entry fixes the path at that moment, but nothing re-checks it
afterwards.  This module does, for one entry or for every stored mapping.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from src.Clients.RadarrClient import RadarrClient
from src.Clients.SonarrClient import SonarrClient
from src.Database.Connection import DatabaseManager
from src.Download.ArrPostProcessor import ArrPostProcessor
from src.Utils.Config import AppConfig

logger = logging.getLogger(__name__)


@dataclass
class VerifyResult:
    """Outcome of verifying one AniList entry's *arr link."""

    anilist_id: int
    service: str = ""  # "sonarr" | "radarr" | "" when not linked
    arr_id: int | None = None
    linked: bool = False  # a mapping existed when we started
    still_present: bool = True  # the service still has it
    removed: bool = False  # mapping cleared because it's gone from *arr
    path_action: str = ""  # updated | already_correct | target_missing | ...
    path_from: str = ""
    path_to: str = ""
    checked_folders: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def changed(self) -> bool:
        return self.removed or self.path_action == "updated"

    def summary(self) -> str:
        """Human-readable one-liner for the UI."""
        if self.error:
            return f"Error: {self.error}"
        if not self.linked:
            return "Not linked to Sonarr or Radarr."
        name = self.service.capitalize()
        if self.removed:
            return (
                f"No longer in {name} — the link has been cleared, "
                "so it can be added again."
            )
        if self.path_action == "updated":
            return f"{name} path updated to {self.path_to}"
        if self.path_action == "already_correct":
            return f"{name} path is already correct."
        if self.path_action == "target_missing":
            checked = ", ".join(self.checked_folders) or "?"
            return (
                f"Still in {name}, but no library folder was found for it "
                f"(looked for: {checked}). Path left unchanged."
            )
        if self.path_action == "no_library_path":
            return "No library path configured, so the path can't be checked."
        if self.path_action == "no_title":
            return "No AniList title cached, so the folder name can't be resolved."
        return f"Still in {name}; no path change needed."


class ArrLinkVerifier:
    """Verifies stored AniList↔Sonarr/Radarr links against the live services."""

    def __init__(
        self,
        db: DatabaseManager,
        config: AppConfig,
        app_state: object | None = None,
    ) -> None:
        self._db = db
        self._config = config
        self._app_state = app_state

    # ------------------------------------------------------------------
    # Single entry
    # ------------------------------------------------------------------

    async def verify_entry(
        self,
        anilist_id: int,
        sonarr: SonarrClient | None = None,
        radarr: RadarrClient | None = None,
    ) -> VerifyResult:
        """Re-check one entry: is it still in *arr, and is its path right?"""
        sonarr_row = await self._db.fetch_one(
            "SELECT sonarr_id FROM anilist_sonarr_mapping"
            " WHERE anilist_id=? AND in_sonarr=1",
            (anilist_id,),
        )
        if sonarr_row and sonarr_row["sonarr_id"]:
            return await self._verify_sonarr(
                anilist_id, int(sonarr_row["sonarr_id"]), sonarr
            )

        radarr_row = await self._db.fetch_one(
            "SELECT radarr_id FROM anilist_radarr_mapping"
            " WHERE anilist_id=? AND in_radarr=1",
            (anilist_id,),
        )
        if radarr_row and radarr_row["radarr_id"]:
            return await self._verify_radarr(
                anilist_id, int(radarr_row["radarr_id"]), radarr
            )

        return VerifyResult(anilist_id=anilist_id, linked=False)

    async def _verify_sonarr(
        self,
        anilist_id: int,
        sonarr_id: int,
        sonarr: SonarrClient | None = None,
    ) -> VerifyResult:
        result = VerifyResult(
            anilist_id=anilist_id, service="sonarr", arr_id=sonarr_id, linked=True
        )
        if not self._config.sonarr.url or not self._config.sonarr.api_key:
            result.error = "Sonarr not configured"
            return result

        owns = sonarr is None
        client = sonarr or SonarrClient(
            url=self._config.sonarr.url, api_key=self._config.sonarr.api_key
        )
        try:
            try:
                series = await client.get_series_by_id(sonarr_id)
            except Exception as exc:
                # A transport error must not be read as "deleted" — leaving the
                # mapping alone is always recoverable, clearing it wrongly is not.
                result.error = str(exc)
                return result

            if not series:
                await self._clear_sonarr_mapping(anilist_id, sonarr_id)
                result.still_present = False
                result.removed = True
                return result

            processor = ArrPostProcessor(
                db=self._db, config=self._config, app_state=self._app_state
            )
            sync = await processor.sync_sonarr_series_path(
                sonarr_id, anilist_id, sonarr=client
            )
            result.path_action = str(sync.get("action", ""))
            result.path_from = str(sync.get("from", ""))
            result.path_to = str(sync.get("to", "") or sync.get("path", ""))
            result.checked_folders = list(sync.get("checked", []) or [])
            return result
        finally:
            if owns:
                await client.close()

    async def _verify_radarr(
        self,
        anilist_id: int,
        radarr_id: int,
        radarr: RadarrClient | None = None,
    ) -> VerifyResult:
        result = VerifyResult(
            anilist_id=anilist_id, service="radarr", arr_id=radarr_id, linked=True
        )
        if not self._config.radarr.url or not self._config.radarr.api_key:
            result.error = "Radarr not configured"
            return result

        owns = radarr is None
        client = radarr or RadarrClient(
            url=self._config.radarr.url, api_key=self._config.radarr.api_key
        )
        try:
            try:
                movie = await client.get_movie_by_id(radarr_id)
            except Exception as exc:
                result.error = str(exc)
                return result

            if not movie:
                await self._clear_radarr_mapping(anilist_id)
                result.still_present = False
                result.removed = True
                return result

            processor = ArrPostProcessor(
                db=self._db, config=self._config, app_state=self._app_state
            )
            sync = await processor.sync_radarr_movie_path(
                radarr_id, anilist_id, radarr=client
            )
            result.path_action = str(sync.get("action", ""))
            result.path_from = str(sync.get("from", ""))
            result.path_to = str(sync.get("to", "") or sync.get("path", ""))
            result.checked_folders = list(sync.get("checked", []) or [])
            return result
        finally:
            if owns:
                await client.close()

    # ------------------------------------------------------------------
    # Mapping cleanup
    # ------------------------------------------------------------------

    async def _clear_sonarr_mapping(self, anilist_id: int, sonarr_id: int) -> None:
        """Mark an entry as no longer in Sonarr, keeping the resolved TVDB ID.

        Clearing the flag rather than deleting the row means a later re-add
        doesn't have to resolve the TVDB ID from scratch, while every read path
        (which filters on ``in_sonarr=1``) correctly treats it as untracked.
        """
        await self._db.execute(
            "UPDATE anilist_sonarr_mapping SET in_sonarr=0, sonarr_monitored=0,"
            " last_verified_at=datetime('now'), updated_at=datetime('now')"
            " WHERE anilist_id=?",
            (anilist_id,),
        )
        await self._db.execute(
            "DELETE FROM anilist_sonarr_season_mapping"
            " WHERE sonarr_id=? AND anilist_id=?",
            (sonarr_id, anilist_id),
        )
        logger.info(
            "anilist_id=%d is no longer in Sonarr (sonarr_id=%d) — link cleared",
            anilist_id,
            sonarr_id,
        )

    async def _clear_radarr_mapping(self, anilist_id: int) -> None:
        await self._db.execute(
            "UPDATE anilist_radarr_mapping SET in_radarr=0, radarr_monitored=0,"
            " last_verified_at=datetime('now'), updated_at=datetime('now')"
            " WHERE anilist_id=?",
            (anilist_id,),
        )
        logger.info("anilist_id=%d is no longer in Radarr — link cleared", anilist_id)

    # ------------------------------------------------------------------
    # Every mapping
    # ------------------------------------------------------------------

    async def verify_all(self) -> dict[str, Any]:
        """Verify every stored link. Returns a summary plus the changed entries."""
        sonarr_client: SonarrClient | None = None
        radarr_client: RadarrClient | None = None
        if self._config.sonarr.url and self._config.sonarr.api_key:
            sonarr_client = SonarrClient(
                url=self._config.sonarr.url, api_key=self._config.sonarr.api_key
            )
        if self._config.radarr.url and self._config.radarr.api_key:
            radarr_client = RadarrClient(
                url=self._config.radarr.url, api_key=self._config.radarr.api_key
            )

        checked = removed = repointed = errors = 0
        changes: list[dict[str, Any]] = []

        try:
            rows = await self._db.fetch_all(
                "SELECT anilist_id, sonarr_id FROM anilist_sonarr_mapping"
                " WHERE in_sonarr=1 AND sonarr_id IS NOT NULL"
            )
            for row in rows:
                checked += 1
                res = await self._verify_sonarr(
                    int(row["anilist_id"]), int(row["sonarr_id"]), sonarr_client
                )
                if res.error:
                    errors += 1
                elif res.removed:
                    removed += 1
                elif res.path_action == "updated":
                    repointed += 1
                if res.changed or res.error:
                    changes.append(
                        {
                            "anilist_id": res.anilist_id,
                            "service": res.service,
                            "summary": res.summary(),
                        }
                    )

            rows = await self._db.fetch_all(
                "SELECT anilist_id, radarr_id FROM anilist_radarr_mapping"
                " WHERE in_radarr=1 AND radarr_id IS NOT NULL"
            )
            for row in rows:
                checked += 1
                res = await self._verify_radarr(
                    int(row["anilist_id"]), int(row["radarr_id"]), radarr_client
                )
                if res.error:
                    errors += 1
                elif res.removed:
                    removed += 1
                elif res.path_action == "updated":
                    repointed += 1
                if res.changed or res.error:
                    changes.append(
                        {
                            "anilist_id": res.anilist_id,
                            "service": res.service,
                            "summary": res.summary(),
                        }
                    )
        finally:
            if sonarr_client:
                await sonarr_client.close()
            if radarr_client:
                await radarr_client.close()

        logger.info(
            "Link verification complete: %d checked, %d removed, %d repointed,"
            " %d errors",
            checked,
            removed,
            repointed,
            errors,
        )
        return {
            "ok": True,
            "checked": checked,
            "removed": removed,
            "repointed": repointed,
            "errors": errors,
            "changes": changes,
        }
