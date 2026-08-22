"""Post-processor for Sonarr/Radarr download events.

After a file is downloaded, moves it into the AniList-structured path
({series_path}/{anilist_entry_title}/{filename}) and updates the arr
service's file record via API so it stays fully linked — no rescan needed.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from src.Clients.RadarrClient import RadarrClient
from src.Clients.SonarrClient import SonarrClient
from src.Database.Connection import DatabaseManager
from src.Utils.Config import AppConfig

logger = logging.getLogger(__name__)


class ArrPostProcessor:
    """Moves downloaded files to AniList-structured paths and updates arr records."""

    def __init__(
        self,
        db: DatabaseManager,
        config: AppConfig,
        app_state: object | None = None,
    ) -> None:
        self._db = db
        self._config = config
        self._app_state = app_state
        # Per-run memo of franchise-root resolution (anilist_id -> root id) so a
        # per-file reprocess loop doesn't re-walk the PREQUEL chain repeatedly.
        self._root_cache: dict[int, int] = {}

    def _schedule_media_server_sync(self) -> None:
        """Queue a debounced Plex/Jellyfin refresh + metadata apply.

        Called after a file is moved into the library so the media servers
        re-index the new path and our AniList metadata (titles, posters,
        NFO, provider IDs) is written — without this hook a *arr-imported
        series shows up in Jellyfin as an untyped folder.
        """
        if self._app_state is None:
            return
        try:
            from src.Download.MediaServerSync import request_arr_media_sync

            request_arr_media_sync(self._app_state)
        except Exception:
            logger.debug("Failed to schedule post-move media sync", exc_info=True)

    async def _write_show_nfo_if_missing(
        self, local_series: str, anilist_id: int, fallback_title: str
    ) -> None:
        """Write a minimal tvshow.nfo into the show folder.

        Mirrors the restructurer's pre-refresh NFO write so Jellyfin
        classifies the freshly-created folder as a TV show on the next
        scan — without this hook the folder is filed as a generic
        ``Folder`` item and our metadata scanner never gets a chance to
        process it.

        Uses the series-group ROOT anilist_id when the entry belongs to a
        multi-season group, so the show folder always carries the S1 IDs
        (mirroring restructurer + scanner behaviour).  Idempotent — the
        underlying writer overwrites with current data on every call.
        """
        from src.Scanner.LibraryRestructurer import _write_tvshow_nfo

        if not local_series or not Path(local_series).is_dir():
            logger.debug(
                "Skipping tvshow.nfo write — folder %s does not exist", local_series
            )
            return

        root_id = anilist_id
        title = fallback_title
        group = await self._db.get_series_group_by_anilist_id(anilist_id)
        if group:
            grp_root = group.get("root_anilist_id")
            if grp_root:
                root_id = int(grp_root)
                root_info = await self._get_anilist_title_info(root_id)
                if root_info.get("title"):
                    title = root_info["title"]

        cached = await self._db.get_cached_metadata(root_id) or {}
        try:
            _write_tvshow_nfo(
                local_series,
                title,
                anilist_id=root_id,
                imdb_id=cached.get("imdb_id") or "",
                tvdb_id=cached.get("tvdb_id") or "",
                tvmaze_id=cached.get("tvmaze_id") or "",
            )
        except Exception:
            logger.debug(
                "Failed to write tvshow.nfo to %s", local_series, exc_info=True
            )

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    async def process_sonarr_download(self, payload: dict[str, Any]) -> None:
        """Handle a Sonarr 'Download' or 'EpisodeFileRenamed' webhook event."""
        event_type = payload.get("eventType", "")
        if event_type == "Test":
            logger.info("Sonarr webhook test received — OK")
            return
        if event_type not in ("Download",):
            logger.debug("Sonarr webhook event_type=%r ignored", event_type)
            return

        series = payload.get("series", {})
        episode_file = payload.get("episodeFile", {})
        episodes = payload.get("episodes", [])

        sonarr_id: int = series.get("id", 0)
        file_id: int = episode_file.get("id", 0)
        current_path: str = episode_file.get("path", "")
        series_path: str = series.get("path", "")
        season_number: int = episodes[0].get("seasonNumber", 1) if episodes else 1
        episode_number: int = episodes[0].get("episodeNumber", 0) if episodes else 0

        if not all([sonarr_id, file_id, current_path, series_path]):
            logger.warning(
                "Sonarr webhook payload missing required fields: %s", payload
            )
            return

        anilist_id = await self._resolve_sonarr_anilist_id(sonarr_id, season_number)
        if not anilist_id:
            logger.info(
                "No AniList mapping for sonarr_id=%d season=%d — skipping",
                sonarr_id,
                season_number,
            )
            return

        show_info, season_info = await self._get_show_and_season_info(anilist_id)
        if not season_info["title"]:
            logger.warning("No AniList title for anilist_id=%d — skipping", anilist_id)
            return

        original_name = Path(current_path).name
        filename = await self._get_file_name(
            original_name, season_info, season_number, episode_number
        )
        safe_dir = await self._get_folder_name(show_info)
        season_dir = await self._get_entry_subfolder(
            anilist_id, season_number, show_info, season_info
        )

        # Path prefix translation for Docker/remote setups
        arr_prefix = self._config.sonarr.path_prefix
        local_prefix = self._config.sonarr.local_path_prefix
        local_current = self._to_local(current_path, arr_prefix, local_prefix)

        # When a library path is configured it is the library root — the show
        # folder (safe_dir) is appended beneath it.  When falling back to
        # series_path, that IS already the show-level folder; appending safe_dir
        # again would create a nested structure (show/show/Season N).
        library_path = await self._get_library_output_path(anilist_format="TV")
        if library_path:
            local_series = str(
                Path(self._to_local(library_path, arr_prefix, local_prefix)) / safe_dir
            )
        else:
            local_series = self._to_local(series_path, arr_prefix, local_prefix)

        local_target = str(Path(local_series) / season_dir / filename)

        if Path(local_target).resolve() == Path(local_current).resolve():
            logger.debug("Sonarr file already at target path: %s", current_path)
            return

        if not self._move_file(local_current, local_target):
            return

        # Write tvshow.nfo BEFORE the media-server refresh runs so Jellyfin
        # classifies the new folder as a Series on its next scan.  Without
        # this, the folder lands as Type=Folder and our metadata scanner
        # (which queries Season,Movie) skips it entirely.
        await self._write_show_nfo_if_missing(
            local_series, anilist_id, show_info["title"]
        )

        arr_series_path = self._to_arr(local_series, arr_prefix, local_prefix)
        arr_target_path = self._to_arr(local_target, arr_prefix, local_prefix)
        relative_path = str(Path(local_target).relative_to(local_series))

        sonarr = SonarrClient(
            url=self._config.sonarr.url, api_key=self._config.sonarr.api_key
        )
        try:
            # Only update the series path when it has actually changed (e.g. first
            # episode after add, or library root moved) — skip on subsequent downloads
            # where Sonarr already points at the right show folder.
            if arr_series_path != series_path:
                await sonarr.update_series_path(sonarr_id, arr_series_path)
                logger.info(
                    "Sonarr series id=%d path updated → %s", sonarr_id, arr_series_path
                )
            # Tell Sonarr exactly where this file landed — no full rescan needed.
            await sonarr.update_episode_file(file_id, relative_path, arr_target_path)
            logger.info(
                "Sonarr episode file id=%d path updated → %s", file_id, arr_target_path
            )
        except Exception as exc:
            logger.error(
                "Failed to update Sonarr after move for id=%d: %s", sonarr_id, exc
            )
        finally:
            await sonarr.close()

        self._schedule_media_server_sync()

    async def process_radarr_download(self, payload: dict[str, Any]) -> None:
        """Handle a Radarr 'Download' webhook event."""
        event_type = payload.get("eventType", "")
        if event_type == "Test":
            logger.info("Radarr webhook test received — OK")
            return
        if event_type not in ("Download",):
            logger.debug("Radarr webhook event_type=%r ignored", event_type)
            return

        movie = payload.get("movie", {})
        movie_file = payload.get("movieFile", {})

        radarr_id: int = movie.get("id", 0)
        file_id: int = movie_file.get("id", 0)
        current_path: str = movie_file.get("path", "")
        # movie.folderPath is the movie's dedicated folder; its parent is the root
        folder_path: str = movie.get("folderPath", "")

        if not all([radarr_id, file_id, current_path]):
            logger.warning(
                "Radarr webhook payload missing required fields: %s", payload
            )
            return

        mapping = await self._db.fetch_one(
            "SELECT anilist_id FROM anilist_radarr_mapping WHERE radarr_id=?",
            (radarr_id,),
        )
        if not mapping:
            logger.info("No AniList mapping for radarr_id=%d — skipping", radarr_id)
            return

        anilist_id: int = mapping["anilist_id"]
        title_info = await self._get_anilist_title_info(anilist_id)
        if not title_info["title"]:
            logger.warning("No AniList title for anilist_id=%d — skipping", anilist_id)
            return

        original_name = Path(current_path).name
        filename = await self._get_movie_file_name(original_name, title_info)
        await self._ensure_series_group(anilist_id)
        await self._ensure_metadata_cached(anilist_id)
        # _get_movie_relative_dir resolves the franchise root (series group or
        # PREQUEL chain) and caches its title/year so nesting + naming match.
        rel_dir = await self._get_movie_relative_dir(anilist_id)

        # Use library output path as target root; fall back to Radarr movie root
        library_path = await self._get_library_output_path(anilist_format="MOVIE")
        arr_prefix = self._config.radarr.path_prefix
        local_prefix = self._config.radarr.local_path_prefix

        if library_path:
            target_root = library_path
        else:
            # Fall back to parent of movie folder
            arr_root = (
                Path(folder_path).parent
                if folder_path
                else Path(current_path).parent.parent
            )
            target_root = str(arr_root)

        local_root = Path(self._to_local(target_root, arr_prefix, local_prefix))
        local_current = self._to_local(current_path, arr_prefix, local_prefix)
        local_target = str(local_root / rel_dir / filename)

        if Path(local_target).resolve() == Path(local_current).resolve():
            logger.debug("Radarr file already at target path: %s", current_path)
            return

        if not self._move_file(local_current, local_target):
            return

        arr_movie_path = self._to_arr(
            str(local_root / rel_dir), arr_prefix, local_prefix
        )
        radarr = RadarrClient(
            url=self._config.radarr.url, api_key=self._config.radarr.api_key
        )
        try:
            await radarr.update_movie_path(radarr_id, arr_movie_path)
            logger.info(
                "Radarr movie id=%d path updated → %s", radarr_id, arr_movie_path
            )
            await radarr.rescan_movie(radarr_id)
            logger.info("Radarr rescan triggered for movie id=%d", radarr_id)
        except Exception as exc:
            logger.error(
                "Failed to update Radarr after move for id=%d: %s", radarr_id, exc
            )
        finally:
            await radarr.close()

        # Remove the movie's old folder so we don't orphan stale nfo/artwork
        self._prune_orphan_source(
            local_current, str(local_root / rel_dir), str(local_root)
        )

        # When nested under a group root, ensure the show folder carries a
        # tvshow.nfo so Jellyfin classifies + sorts it (mirrors the Sonarr path).
        if rel_dir.parent != Path("."):
            await self._write_show_nfo_if_missing(
                str(local_root / rel_dir.parent), anilist_id, title_info["title"]
            )

        self._schedule_media_server_sync()

    # ------------------------------------------------------------------
    # Manual reprocess (existing entries)
    # ------------------------------------------------------------------

    @staticmethod
    def _to_local(path: str, arr_prefix: str, local_prefix: str) -> str:
        """Translate an arr-side path to the locally-writable equivalent."""
        if arr_prefix and local_prefix and path.startswith(arr_prefix):
            return local_prefix + path[len(arr_prefix) :]
        return path

    @staticmethod
    def _to_arr(path: str, arr_prefix: str, local_prefix: str) -> str:
        """Translate a local path back to the arr-side path."""
        if arr_prefix and local_prefix and path.startswith(local_prefix):
            return arr_prefix + path[len(local_prefix) :]
        return path

    # ------------------------------------------------------------------
    # Path reconciliation
    # ------------------------------------------------------------------

    async def _candidate_folder_names(self, anilist_ids: list[int]) -> list[str]:
        """Render show folder names for *anilist_ids*, de-duplicated, order kept.

        Blank names are dropped — an empty folder name would resolve to the
        library root itself, which must never be treated as a show folder.
        """
        names: list[str] = []
        for aid in anilist_ids:
            try:
                show_info, _ = await self._get_show_and_season_info(aid)
            except Exception:
                continue
            if not show_info.get("title"):
                continue
            folder = (await self._get_folder_name(show_info) or "").strip(" /.")
            if folder and folder not in names:
                names.append(folder)
        return names

    async def _related_anilist_ids(self, sonarr_id: int, anilist_id: int) -> list[int]:
        """Return *anilist_id* first, then every other entry mapped to this series.

        The series group may not exist yet when a series is first linked, so the
        entry's own title can differ from the library folder (which is named
        after the group root).  Checking siblings covers that case.
        """
        ids: list[int] = [anilist_id]
        for sql in (
            "SELECT anilist_id FROM anilist_sonarr_season_mapping"
            " WHERE sonarr_id=? ORDER BY season_number",
            "SELECT anilist_id FROM anilist_sonarr_mapping WHERE sonarr_id=?",
        ):
            try:
                for row in await self._db.fetch_all(sql, (sonarr_id,)):
                    aid = int(row["anilist_id"])
                    if aid not in ids:
                        ids.append(aid)
            except Exception:
                logger.debug("Could not load related entries", exc_info=True)
        return ids

    async def sync_sonarr_series_path(
        self,
        sonarr_id: int,
        anilist_id: int,
        sonarr: SonarrClient | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Repoint a Sonarr series at its restructured library folder.

        Sonarr keeps whatever path it was given when the series was added, so a
        series whose files were later moved by the restructurer still points at
        the old location and reports every episode as missing.  This finds the
        show folder in our library and updates Sonarr's record to match, then
        rescans so the existing files are picked up.

        Only ever updates the stored path — files are never moved.  The update
        is applied *only* when the expected folder actually exists on disk, so
        a series with no files yet (or one that was never restructured) is left
        exactly as Sonarr has it.
        """
        if not self._config.sonarr.url or not self._config.sonarr.api_key:
            return {"ok": False, "action": "not_configured"}

        library_path = await self._get_library_output_path(anilist_format="TV")
        if not library_path:
            return {"ok": True, "action": "no_library_path"}

        arr_prefix = self._config.sonarr.path_prefix
        local_prefix = self._config.sonarr.local_path_prefix
        local_root = self._to_local(library_path, arr_prefix, local_prefix)

        candidates = await self._candidate_folder_names(
            await self._related_anilist_ids(sonarr_id, anilist_id)
        )
        if not candidates:
            return {"ok": True, "action": "no_title"}

        local_target: str = ""
        for folder in candidates:
            probe = Path(local_root) / folder
            if probe.is_dir():
                local_target = str(probe)
                break

        if not local_target:
            # Files aren't at the restructured location — leave Sonarr alone
            # rather than pointing it at a folder that doesn't exist.
            return {"ok": True, "action": "target_missing", "checked": candidates}

        arr_target = self._to_arr(local_target, arr_prefix, local_prefix)

        owns_client = sonarr is None
        client = sonarr or SonarrClient(
            url=self._config.sonarr.url, api_key=self._config.sonarr.api_key
        )
        try:
            series = await client.get_series_by_id(sonarr_id)
            if not series:
                return {"ok": False, "action": "not_found"}

            current = str(series.get("path", ""))
            if current.rstrip("/") == arr_target.rstrip("/"):
                return {"ok": True, "action": "already_correct", "path": current}

            if dry_run:
                return {
                    "ok": True,
                    "action": "would_update",
                    "from": current,
                    "to": arr_target,
                }

            await client.update_series_path(sonarr_id, arr_target)
            logger.info(
                "Sonarr series id=%d path %s → %s (restructured location)",
                sonarr_id,
                current or "(none)",
                arr_target,
            )
            try:
                await client.rescan_series(sonarr_id)
            except Exception as exc:
                logger.warning("Rescan failed for sonarr_id=%d: %s", sonarr_id, exc)

            return {
                "ok": True,
                "action": "updated",
                "from": current,
                "to": arr_target,
            }
        finally:
            if owns_client:
                await client.close()

    async def sync_radarr_movie_path(
        self,
        radarr_id: int,
        anilist_id: int,
        radarr: RadarrClient | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Repoint a Radarr movie at its restructured library folder.

        Radarr equivalent of :meth:`sync_sonarr_series_path` — same rules: the
        stored path only, never a file move, and only when the expected folder
        already exists on disk.
        """
        if not self._config.radarr.url or not self._config.radarr.api_key:
            return {"ok": False, "action": "not_configured"}

        library_path = await self._get_library_output_path(anilist_format="MOVIE")
        if not library_path:
            return {"ok": True, "action": "no_library_path"}

        arr_prefix = self._config.radarr.path_prefix
        local_prefix = self._config.radarr.local_path_prefix
        local_root = self._to_local(library_path, arr_prefix, local_prefix)

        title_info = await self._get_anilist_title_info(anilist_id)
        if not title_info["title"]:
            return {"ok": True, "action": "no_title"}
        folder = (await self._get_folder_name(title_info) or "").strip(" /.")
        if not folder:
            return {"ok": True, "action": "no_title"}

        local_target_path = Path(local_root) / folder
        if not local_target_path.is_dir():
            return {"ok": True, "action": "target_missing", "checked": [folder]}

        arr_target = self._to_arr(str(local_target_path), arr_prefix, local_prefix)

        owns_client = radarr is None
        client = radarr or RadarrClient(
            url=self._config.radarr.url, api_key=self._config.radarr.api_key
        )
        try:
            movie = await client.get_movie_by_id(radarr_id)
            if not movie:
                return {"ok": False, "action": "not_found"}

            current = str(movie.get("path", ""))
            if current.rstrip("/") == arr_target.rstrip("/"):
                return {"ok": True, "action": "already_correct", "path": current}

            if dry_run:
                return {
                    "ok": True,
                    "action": "would_update",
                    "from": current,
                    "to": arr_target,
                }

            await client.update_movie_path(radarr_id, arr_target)
            logger.info(
                "Radarr movie id=%d path %s → %s (restructured location)",
                radarr_id,
                current or "(none)",
                arr_target,
            )
            try:
                await client.rescan_movie(radarr_id)
            except Exception as exc:
                logger.warning("Rescan failed for radarr_id=%d: %s", radarr_id, exc)

            return {
                "ok": True,
                "action": "updated",
                "from": current,
                "to": arr_target,
            }
        finally:
            if owns_client:
                await client.close()

    async def reprocess_sonarr_series(
        self, sonarr_id: int, dry_run: bool = False
    ) -> dict[str, Any]:
        """Move all existing files for a Sonarr series into AniList-structured paths.

        For each episode file, resolves the AniList mapping per season, moves the
        file to {series_path}/{anilist_title}/{filename}, and updates Sonarr's record.
        Returns a summary with moved/skipped/error counts.
        When dry_run=True, returns the planned moves without executing them.
        """
        arr_prefix = self._config.sonarr.path_prefix
        local_prefix = self._config.sonarr.local_path_prefix

        sonarr = SonarrClient(
            url=self._config.sonarr.url, api_key=self._config.sonarr.api_key
        )
        try:
            series = await sonarr.get_series_by_id(sonarr_id)
            if not series:
                return {"ok": False, "error": f"Series {sonarr_id} not found in Sonarr"}

            series_path: str = series.get("path", "")
            if not series_path:
                return {"ok": False, "error": "Series has no path in Sonarr"}

            # When library_path is set it is the library root — safe_dir is appended.
            # When absent, series_path IS the show folder; don't nest safe_dir under it.
            library_path = await self._get_library_output_path(anilist_format="TV")
            if library_path:
                local_target_root: str | None = self._to_local(
                    library_path, arr_prefix, local_prefix
                )
                local_series_fallback: str | None = None
            else:
                local_target_root = None
                local_series_fallback = self._to_local(
                    series_path, arr_prefix, local_prefix
                )

            # Build episodeFileId → (seasonNumber, episodeNumber) map
            episodes = await sonarr.get_episodes(sonarr_id)
            file_season: dict[int, int] = {}
            file_episode: dict[int, int] = {}
            for ep in episodes:
                fid = ep.get("episodeFileId", 0)
                if fid:
                    file_season[fid] = ep.get("seasonNumber", 1)
                    file_episode[fid] = ep.get("episodeNumber", 0)

            episode_files = await sonarr.get_episode_files(sonarr_id)

            if dry_run:
                plan: list[dict[str, Any]] = []
                for ef in episode_files:
                    file_id: int = ef.get("id", 0)
                    arr_current_path: str = ef.get("path", "")
                    if not file_id or not arr_current_path:
                        continue

                    season_number = file_season.get(file_id, 1)
                    episode_number = file_episode.get(file_id, 0)
                    anilist_id = await self._resolve_sonarr_anilist_id(
                        sonarr_id, season_number
                    )
                    if not anilist_id:
                        continue

                    show_info, season_info = await self._get_show_and_season_info(
                        anilist_id
                    )
                    if not season_info["title"]:
                        continue

                    original_name = Path(arr_current_path).name
                    filename = await self._get_file_name(
                        original_name, season_info, season_number, episode_number
                    )
                    safe_dir = await self._get_folder_name(show_info)
                    season_dir = await self._get_entry_subfolder(
                        anilist_id, season_number, show_info, season_info
                    )

                    local_current = self._to_local(
                        arr_current_path, arr_prefix, local_prefix
                    )
                    if local_target_root:
                        local_series_dry = str(Path(local_target_root) / safe_dir)
                    else:
                        local_series_dry = local_series_fallback or ""
                    local_target = str(Path(local_series_dry) / season_dir / filename)
                    arr_target = self._to_arr(local_target, arr_prefix, local_prefix)

                    already_at_target = (
                        Path(local_target).resolve() == Path(local_current).resolve()
                    )
                    plan.append(
                        {
                            "file_id": file_id,
                            "season": season_number,
                            "anilist_id": anilist_id,
                            "anilist_title": season_info["title"],
                            "folder_name": safe_dir,
                            "arr_from": arr_current_path,
                            "arr_to": arr_target,
                            "local_from": local_current,
                            "local_to": local_target,
                            "action": "skip" if already_at_target else "move",
                        }
                    )
                return {
                    "ok": True,
                    "dry_run": True,
                    "series_path": series_path,
                    "files": plan,
                }

            moved = skipped = errors = 0
            # Track folders we've already written tvshow.nfo for in this run
            # so we don't repeat the write per episode.
            nfo_written: set[str] = set()

            # When a library path is configured, update the Sonarr series path once
            # upfront so it points at the correct show folder in our library.
            # When falling back to series_path, it is already the show folder — skip.
            if local_target_root and episode_files:
                for _probe in episode_files:
                    _probe_fid = _probe.get("id", 0)
                    _probe_sn = file_season.get(_probe_fid, 1)
                    _probe_aid = await self._resolve_sonarr_anilist_id(
                        sonarr_id, _probe_sn
                    )
                    if _probe_aid:
                        _probe_show, _ = await self._get_show_and_season_info(
                            _probe_aid
                        )
                        if _probe_show["title"]:
                            _probe_dir = await self._get_folder_name(_probe_show)
                            arr_series_path = self._to_arr(
                                str(Path(local_target_root) / _probe_dir),
                                arr_prefix,
                                local_prefix,
                            )
                            if arr_series_path != series_path:
                                try:
                                    await sonarr.update_series_path(
                                        sonarr_id, arr_series_path
                                    )
                                    series_path = arr_series_path
                                    logger.info(
                                        "Sonarr series id=%d path → %s",
                                        sonarr_id,
                                        arr_series_path,
                                    )
                                except Exception as exc:
                                    logger.warning(
                                        "Failed to update series path for id=%d: %s",
                                        sonarr_id,
                                        exc,
                                    )
                            break
                    break

            for ef in episode_files:
                file_id = ef.get("id", 0)
                arr_current_path = ef.get("path", "")
                if not file_id or not arr_current_path:
                    continue

                season_number = file_season.get(file_id, 1)
                episode_number = file_episode.get(file_id, 0)
                anilist_id = await self._resolve_sonarr_anilist_id(
                    sonarr_id, season_number
                )
                if not anilist_id:
                    logger.info(
                        "No AniList mapping for sonarr_id=%d season=%d — skipping %s",
                        sonarr_id,
                        season_number,
                        arr_current_path,
                    )
                    skipped += 1
                    continue

                show_info, season_info = await self._get_show_and_season_info(
                    anilist_id
                )
                if not season_info["title"]:
                    logger.warning(
                        "No title for anilist_id=%d — skipping %s",
                        anilist_id,
                        arr_current_path,
                    )
                    skipped += 1
                    continue

                original_name = Path(arr_current_path).name
                filename = await self._get_file_name(
                    original_name, season_info, season_number, episode_number
                )
                safe_dir = await self._get_folder_name(show_info)
                season_dir = await self._get_entry_subfolder(
                    anilist_id, season_number, show_info, season_info
                )

                # Paths for local move
                local_current = self._to_local(
                    arr_current_path, arr_prefix, local_prefix
                )
                if local_target_root:
                    local_series_for_file = str(Path(local_target_root) / safe_dir)
                else:
                    local_series_for_file = local_series_fallback or ""
                local_target = str(Path(local_series_for_file) / season_dir / filename)

                logger.info(
                    "Sonarr reprocess file_id=%d anilist_id=%d season=%d:"
                    " show_folder=%r subfolder=%r\n    from: %s\n    to:   %s",
                    file_id,
                    anilist_id,
                    season_number,
                    safe_dir,
                    season_dir,
                    local_current,
                    local_target,
                )

                if Path(local_target).resolve() == Path(local_current).resolve():
                    logger.info(
                        "Skipping file_id=%d — already at target location", file_id
                    )
                    skipped += 1
                    continue

                # Source gone but target exists = already moved previously
                if not Path(local_current).exists() and Path(local_target).exists():
                    logger.info(
                        "Skipping file_id=%d — source missing and target exists"
                        " (already moved)",
                        file_id,
                    )
                    skipped += 1
                    continue

                if not self._move_file(local_current, local_target):
                    errors += 1
                    continue
                moved += 1

                # Write tvshow.nfo to the show folder (once per series) so
                # Jellyfin classifies it as a Series on the next refresh.
                if local_series_for_file and local_series_for_file not in nfo_written:
                    await self._write_show_nfo_if_missing(
                        local_series_for_file, anilist_id, show_info["title"]
                    )
                    nfo_written.add(local_series_for_file)

                # Tell Sonarr exactly where this file landed — no rescan needed.
                arr_target_path = self._to_arr(local_target, arr_prefix, local_prefix)
                relative_path = str(
                    Path(local_target).relative_to(local_series_for_file)
                )
                try:
                    await sonarr.update_episode_file(
                        file_id, relative_path, arr_target_path
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to update episode file id=%d in Sonarr: %s",
                        file_id,
                        exc,
                    )

            if moved > 0:
                self._schedule_media_server_sync()
            logger.info(
                "Sonarr reprocess complete for sonarr_id=%d: moved=%d skipped=%d"
                " errors=%d",
                sonarr_id,
                moved,
                skipped,
                errors,
            )
            return {"ok": True, "moved": moved, "skipped": skipped, "errors": errors}
        finally:
            await sonarr.close()

    async def reprocess_radarr_movie(
        self, radarr_id: int, dry_run: bool = False
    ) -> dict[str, Any]:
        """Move the downloaded file for a Radarr movie into its AniList-structured path.

        When dry_run=True, returns the planned moves without executing them.
        """
        arr_prefix = self._config.radarr.path_prefix
        local_prefix = self._config.radarr.local_path_prefix

        radarr = RadarrClient(
            url=self._config.radarr.url, api_key=self._config.radarr.api_key
        )
        try:
            mapping = await self._db.fetch_one(
                "SELECT anilist_id FROM anilist_radarr_mapping WHERE radarr_id=?",
                (radarr_id,),
            )
            if not mapping:
                return {
                    "ok": False,
                    "error": f"No AniList mapping for radarr_id={radarr_id}",
                }

            anilist_id: int = mapping["anilist_id"]
            title_info = await self._get_anilist_title_info(anilist_id)
            if not title_info["title"]:
                return {
                    "ok": False,
                    "error": f"No title found for anilist_id={anilist_id}",
                }

            movie = await radarr.get_movie_by_id(radarr_id)
            if not movie:
                return {"ok": False, "error": f"Movie {radarr_id} not found in Radarr"}

            movie_files = await radarr.get_movie_files(radarr_id)
            if not movie_files:
                if dry_run:
                    return {"ok": True, "dry_run": True, "files": []}
                return {"ok": True, "moved": 0, "skipped": 0, "errors": 0}

            # Use library output path as target root; fall back to Radarr movie root
            library_path = await self._get_library_output_path(anilist_format="MOVIE")
            if library_path:
                target_root = library_path
            else:
                folder_path_str: str = movie.get("folderPath", "")
                arr_root = (
                    Path(folder_path_str).parent
                    if folder_path_str
                    else Path(movie_files[0].get("path", "")).parent.parent
                )
                target_root = str(arr_root)
            local_root = Path(self._to_local(target_root, arr_prefix, local_prefix))
            await self._ensure_series_group(anilist_id)
            await self._ensure_metadata_cached(anilist_id)
            rel_dir = await self._get_movie_relative_dir(anilist_id)

            if dry_run:
                plan: list[dict[str, Any]] = []
                for mf in movie_files:
                    file_id: int = mf.get("id", 0)
                    arr_current: str = mf.get("path", "")
                    if not file_id or not arr_current:
                        continue

                    original_name = Path(arr_current).name
                    filename = await self._get_movie_file_name(
                        original_name, title_info
                    )
                    local_current = self._to_local(
                        arr_current, arr_prefix, local_prefix
                    )
                    local_target = str(local_root / rel_dir / filename)
                    arr_target = self._to_arr(local_target, arr_prefix, local_prefix)

                    already_at_target = (
                        Path(local_target).resolve() == Path(local_current).resolve()
                    )
                    plan.append(
                        {
                            "file_id": file_id,
                            "anilist_id": anilist_id,
                            "anilist_title": title_info["title"],
                            "folder_name": str(rel_dir),
                            "arr_from": arr_current,
                            "arr_to": arr_target,
                            "local_from": local_current,
                            "local_to": local_target,
                            "action": "skip" if already_at_target else "move",
                        }
                    )
                return {"ok": True, "dry_run": True, "files": plan}

            moved = skipped = errors = 0
            movie_path_updated = False
            orphan_sources: list[str] = []

            for mf in movie_files:
                file_id = mf.get("id", 0)
                arr_current = mf.get("path", "")
                if not file_id or not arr_current:
                    continue

                original_name = Path(arr_current).name
                filename = await self._get_movie_file_name(original_name, title_info)
                local_current = self._to_local(arr_current, arr_prefix, local_prefix)
                local_target = str(local_root / rel_dir / filename)

                if Path(local_target).resolve() == Path(local_current).resolve():
                    skipped += 1
                    continue

                if not Path(local_current).exists() and Path(local_target).exists():
                    skipped += 1
                    continue

                if not self._move_file(local_current, local_target):
                    errors += 1
                    continue

                orphan_sources.append(local_current)

                # Update movie path in Radarr once
                if not movie_path_updated:
                    arr_movie_path = self._to_arr(
                        str(local_root / rel_dir), arr_prefix, local_prefix
                    )
                    try:
                        await radarr.update_movie_path(radarr_id, arr_movie_path)
                        logger.info(
                            "Radarr movie id=%d path → %s",
                            radarr_id,
                            arr_movie_path,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Failed to update movie path for id=%d: %s",
                            radarr_id,
                            exc,
                        )
                    movie_path_updated = True
                moved += 1

            # Remove each moved file's old folder if nothing but stale metadata
            # remains, so we don't orphan the previous movie directory.
            target_dir = str(local_root / rel_dir)
            for src in orphan_sources:
                self._prune_orphan_source(src, target_dir, str(local_root))

            # When nested under a group root, write the show folder's tvshow.nfo
            # so Jellyfin classifies + sorts it (mirrors the Sonarr path).
            if moved and rel_dir.parent != Path("."):
                await self._write_show_nfo_if_missing(
                    str(local_root / rel_dir.parent), anilist_id, title_info["title"]
                )

            # Always rescan so Radarr discovers files at their current paths
            try:
                await radarr.rescan_movie(radarr_id)
                logger.info("Radarr rescan triggered for movie id=%d", radarr_id)
            except Exception as exc:
                logger.warning(
                    "Failed to trigger Radarr rescan for id=%d: %s",
                    radarr_id,
                    exc,
                )

            if moved > 0:
                self._schedule_media_server_sync()
            return {"ok": True, "moved": moved, "skipped": skipped, "errors": errors}
        finally:
            await radarr.close()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _resolve_sonarr_anilist_id(
        self, sonarr_id: int, season_number: int
    ) -> int | None:
        """Return the AniList ID for a given Sonarr series + season."""
        # Per-season mapping takes precedence (multi-season TVDB series)
        row = await self._db.fetch_one(
            "SELECT anilist_id FROM anilist_sonarr_season_mapping"
            " WHERE sonarr_id=? AND season_number=?",
            (sonarr_id, season_number),
        )
        if row:
            return int(row["anilist_id"])

        # If season-specific mappings exist for this series but none matched,
        # don't fall back — routing to the wrong AniList entry is worse than
        # skipping. Only use the series-level mapping for 1:1 shows (no season table).
        any_season = await self._db.fetch_one(
            "SELECT 1 FROM anilist_sonarr_season_mapping WHERE sonarr_id=? LIMIT 1",
            (sonarr_id,),
        )
        if any_season:
            # A partial season table exists but this season isn't in it. This
            # happens when a new cour/season aired after the series was added
            # (so it was never in the sequel chain we built). Rather than
            # silently skip — which leaves the file unorganized in Sonarr's
            # raw import folder — try to rebuild the chain so the new season
            # picks up a mapping. Returns None if it still can't be resolved.
            healed = await self._heal_season_mappings(sonarr_id, season_number)
            return healed

        # Fall back to series-level mapping (covers 1:1 TVDB:AniList shows)
        row = await self._db.fetch_one(
            "SELECT anilist_id FROM anilist_sonarr_mapping WHERE sonarr_id=?",
            (sonarr_id,),
        )
        return int(row["anilist_id"]) if row else None

    async def _heal_season_mappings(
        self, sonarr_id: int, requested_season: int
    ) -> int | None:
        """Rebuild the sequel chain for a series and persist missing season maps.

        Called when a download arrives for a season that has no mapping but the
        series already has *some* season mappings — typically a newer cour that
        aired after the series was first added. Re-walks the AniList sequel/
        prequel chain from the earliest known entry and re-persists the full
        chain, then returns the AniList ID now mapped to *requested_season*
        (or ``None`` if the season still can't be resolved).
        """
        if not self._config.anilist.client_id:
            return None

        rows = await self._db.fetch_all(
            "SELECT season_number, anilist_id FROM anilist_sonarr_season_mapping"
            " WHERE sonarr_id=? ORDER BY season_number",
            (sonarr_id,),
        )
        if not rows:
            return None

        # Earliest known season = start of the chain (any chain member works —
        # collect_series_chain walks both sequel and prequel edges).
        seed_aid = int(rows[0]["anilist_id"])

        tvdb_row = await self._db.fetch_one(
            "SELECT tvdb_id FROM anilist_sonarr_mapping"
            " WHERE sonarr_id=? AND tvdb_id IS NOT NULL",
            (sonarr_id,),
        )
        tvdb_id = int(tvdb_row["tvdb_id"]) if tvdb_row and tvdb_row["tvdb_id"] else 0

        from src.Clients.AnilistClient import AniListClient
        from src.Utils.NamingTranslator import (
            collect_series_chain,
            resolve_tvdb_id,
            resolve_tvdb_via_prequel_chain,
        )

        anilist = AniListClient(
            client_id=self._config.anilist.client_id,
            client_secret=self._config.anilist.client_secret,
        )
        try:
            if not tvdb_id:
                tvdb_id = await resolve_tvdb_id(seed_aid, anilist) or 0
                if not tvdb_id:
                    resolved, _ = await resolve_tvdb_via_prequel_chain(
                        seed_aid, anilist
                    )
                    tvdb_id = resolved or 0
            if not tvdb_id:
                return None

            chain = await collect_series_chain(seed_aid, tvdb_id, anilist)
            if len(chain) < requested_season:
                # Chain still doesn't reach this season — nothing we can do.
                return None

            for idx, chain_aid in enumerate(chain):
                await self._db.execute(
                    "INSERT OR REPLACE INTO anilist_sonarr_season_mapping"
                    " (sonarr_id, season_number, anilist_id) VALUES (?, ?, ?)",
                    (sonarr_id, idx + 1, chain_aid),
                )
            logger.info(
                "Self-healed season mappings for sonarr_id=%d — %d seasons "
                "(triggered by unmapped season %d)",
                sonarr_id,
                len(chain),
                requested_season,
            )
            return int(chain[requested_season - 1])
        except Exception as exc:
            logger.warning(
                "Season self-heal failed for sonarr_id=%d season=%d: %s",
                sonarr_id,
                requested_season,
                exc,
            )
            return None
        finally:
            await anilist.close()

    async def _get_library_output_path(self, anilist_format: str = "") -> str | None:
        """Return the appropriate library output path.

        When movie/TV split is enabled and *anilist_format* is ``MOVIE``,
        returns the movie output directory; otherwise the TV directory.
        Falls back to the first configured library path.
        """
        split_enabled = (
            await self._db.get_setting("library.split_movies_tv") or ""
        ).lower() in ("true", "1", "yes")

        if split_enabled:
            movie_path = (
                await self._db.get_setting("library.movie_output_path") or ""
            ).strip()
            tv_path = (
                await self._db.get_setting("library.tv_output_path") or ""
            ).strip()
            if movie_path and tv_path:
                if (anilist_format or "").upper() == "MOVIE":
                    return movie_path
                return tv_path

        libraries = await self._db.get_all_libraries()
        if not libraries:
            return None
        import json

        paths = json.loads(libraries[0].get("paths", "[]"))
        return paths[0] if paths else None

    async def _get_show_and_season_info(self, anilist_id: int) -> tuple[dict, dict]:
        """Return (show_title_info, season_title_info) for an AniList entry.

        If the entry belongs to a series group, show_title_info uses the
        root entry's titles (for the top-level folder).  season_title_info
        always uses this entry's own titles (for the season subfolder).

        If no series group exists, both dicts are identical.

        The root is resolved via the series group when it already traces to a
        distinct root, else by walking the PREQUEL chain to the franchise base
        — so a self-rooted/stale group can't strand an entry (e.g. a Demon
        Slayer movie tracked in Sonarr) in a top-level folder.
        """
        entry_info = await self._get_anilist_title_info(anilist_id)

        root_id = await self._resolve_franchise_root(anilist_id)
        if root_id and root_id != anilist_id:
            await self._ensure_metadata_cached(root_id)
            root_info = await self._get_anilist_title_info(root_id)
            if root_info["title"]:
                return root_info, entry_info

        return entry_info, entry_info

    @staticmethod
    def _disambiguate_movie_folder(folder: str, title_info: dict) -> str:
        """Ensure a nested movie folder carries a distinguishing token.

        Under a shared franchise root a movie can otherwise collide with a
        same-named TV season (e.g. the Mugen Train movie vs the Mugen Train TV
        arc).  When the rendered folder already carries the year (the usual
        template) it is returned unchanged; otherwise the year — or a ``[Movie]``
        marker when no year is known — is appended as a backup.
        """
        if not folder:
            return folder
        year = title_info.get("year") or 0
        if year:
            if str(year) not in folder:
                return f"{folder} ({year})"
            return folder
        if not folder.rstrip().lower().endswith("[movie]"):
            return f"{folder} [Movie]"
        return folder

    async def _get_entry_subfolder(
        self,
        anilist_id: int,
        season_number: int,
        show_info: dict,
        season_info: dict,
    ) -> str:
        """Return the subfolder under the show/root folder for one Sonarr entry.

        - A movie nested under a franchise root gets its own (disambiguated)
          title folder, so it can't collide with a same-named TV season.
        - A standalone movie gets no subfolder (file sits in the show folder,
          mirroring Radarr's flat layout).
        - A TV entry keeps its season folder.
        """
        from src.Utils.NamingTranslator import is_movie_format

        fmt = await self._get_entry_format(anilist_id)
        if not is_movie_format(fmt):
            return await self._get_season_folder_name(season_number, season_info)

        # Movie: nested only when a distinct franchise root was resolved.
        if show_info is season_info:
            return ""
        folder = await self._get_folder_name(season_info)
        return self._disambiguate_movie_folder(folder, season_info)

    async def _get_movie_relative_dir(self, anilist_id: int) -> Path:
        """Return a Radarr movie's folder path relative to the library root.

        Mirrors the Sonarr layout: when the movie belongs to a series group,
        it is nested under the group ROOT folder (e.g. ``Demon Slayer/Infinity
        Castle``); otherwise it lands in a flat ``<movie folder>``.
        """
        movie_info = await self._get_anilist_title_info(anilist_id)
        movie_dir = await self._get_folder_name(movie_info)

        # Radarr entries are always movies — always allow the prequel walk.
        root_id = await self._resolve_franchise_root(anilist_id, force_walk=True)
        if root_id and root_id != anilist_id:
            # Make sure the root's title/year are cached so the folder renders
            # the same name as the metadata writer's master folder.
            await self._ensure_metadata_cached(root_id)
            root_info = await self._get_anilist_title_info(root_id)
            if root_info.get("title"):
                root_dir = await self._get_folder_name(root_info)
                if root_dir and root_dir != movie_dir:
                    # Guarantee the movie folder differs from any TV season
                    # under the same root.
                    movie_dir = self._disambiguate_movie_folder(movie_dir, movie_info)
                    return Path(root_dir) / movie_dir
        return Path(movie_dir)

    async def _resolve_franchise_root(
        self, anilist_id: int, force_walk: bool = False
    ) -> int:
        """Resolve the franchise ROOT AniList id for nesting an entry.

        Trusts the series group when it already traces to a distinct root;
        otherwise walks the PREQUEL chain back to the base (e.g. a Demon Slayer
        movie → base TV season) so a stale/self-rooted group can't strand the
        entry in a top-level folder.

        The prequel walk only runs for MOVIE-format entries (or when
        ``force_walk`` is set, as on the always-a-movie Radarr path).  TV
        entries keep the series-group behaviour so a normal multi-cour show
        isn't split when a new episode arrives.
        """
        if anilist_id in self._root_cache:
            return self._root_cache[anilist_id]

        group = await self._db.get_series_group_by_anilist_id(anilist_id)
        if group and group.get("root_anilist_id"):
            root = int(group["root_anilist_id"])
            if root != anilist_id:
                logger.info(
                    "Franchise root for anilist_id=%d = %d (via series group)",
                    anilist_id,
                    root,
                )
                self._root_cache[anilist_id] = root
                return root

        resolved = anilist_id
        should_walk = force_walk
        fmt = ""
        if not should_walk:
            from src.Utils.NamingTranslator import is_movie_format

            fmt = await self._get_entry_format(anilist_id)
            should_walk = is_movie_format(fmt)

        if should_walk:
            anilist_client = getattr(self._app_state, "anilist_client", None)
            if anilist_client is None:
                logger.warning(
                    "Cannot walk prequel chain for anilist_id=%d — no AniList"
                    " client on app_state",
                    anilist_id,
                )
            else:
                try:
                    from src.Utils.NamingTranslator import resolve_franchise_root_id

                    resolved = await resolve_franchise_root_id(
                        anilist_id, anilist_client
                    )
                except Exception as exc:
                    logger.warning(
                        "Prequel-root walk failed for anilist_id=%d: %s",
                        anilist_id,
                        exc,
                    )
            logger.info(
                "Franchise root for anilist_id=%d = %d (via PREQUEL walk,"
                " group_root=%s, format=%r)",
                anilist_id,
                resolved,
                (group or {}).get("root_anilist_id"),
                fmt,
            )
        else:
            logger.info(
                "No prequel walk for anilist_id=%d (format=%r, force_walk=%s,"
                " group_root=%s) — staying flat",
                anilist_id,
                fmt,
                force_walk,
                (group or {}).get("root_anilist_id"),
            )
        self._root_cache[anilist_id] = resolved
        return resolved

    async def _get_entry_format(self, anilist_id: int) -> str:
        """Return the AniList format (e.g. MOVIE/TV) from the user's watchlist."""
        try:
            users = await self._db.get_users_by_service("anilist")
            if users:
                entry = await self._db.get_watchlist_entry(
                    users[0]["user_id"], anilist_id
                )
                if entry and entry.get("anilist_format"):
                    return str(entry["anilist_format"])
        except Exception:
            logger.debug("format lookup failed for anilist_id=%d", anilist_id)
        return ""

    async def _ensure_series_group(self, anilist_id: int) -> None:
        """Build the series group for an entry if one isn't cached yet.

        Radarr adds don't populate series groups (only the Sonarr sibling
        auto-link does), so a movie may have no group on record — which would
        flatten its library folder instead of nesting it under the group root
        (e.g. ``Demon Slayer/Infinity Castle``).  Build one on demand when the
        AniList client is reachable via app_state.  Best-effort: any failure
        leaves the movie in a flat folder rather than blocking the move.
        """
        try:
            if await self._db.get_series_group_by_anilist_id(anilist_id):
                return
            anilist_client = getattr(self._app_state, "anilist_client", None)
            if anilist_client is None:
                return
            from src.Scanner.SeriesGroupBuilder import SeriesGroupBuilder

            builder = SeriesGroupBuilder(db=self._db, anilist_client=anilist_client)
            await builder.get_or_build_group(anilist_id)
            logger.info("Built series group on demand for anilist_id=%d", anilist_id)
        except Exception as exc:
            logger.warning(
                "Could not ensure series group for anilist_id=%d: %s", anilist_id, exc
            )

    async def _ensure_metadata_cached(self, anilist_id: int) -> None:
        """Backfill anilist_cache with the year when it's missing.

        Folder naming renders ``{year}`` from the cache; when an entry (or its
        group root) has no cached year the folder loses its ``(2019)`` suffix
        and no longer matches the metadata writer's folder — leaving the moved
        item in a differently-named, mis-sorted directory.  Fetch from AniList
        and upsert the year, preserving any provider IDs already cached.
        Best-effort — never blocks the move.
        """
        try:
            cached = await self._db.get_cached_metadata(anilist_id) or {}
            if cached.get("year"):
                return
            anilist_client = getattr(self._app_state, "anilist_client", None)
            if anilist_client is None:
                return
            media = await anilist_client.get_anime_by_id(anilist_id)
            if not media:
                return
            year = media.get("seasonYear") or (media.get("startDate") or {}).get("year")
            if not year:
                return
            import json as _json

            title = media.get("title") or {}
            await self._db.set_cached_metadata(
                anilist_id=anilist_id,
                title_romaji=title.get("romaji") or cached.get("title_romaji") or "",
                title_english=title.get("english") or cached.get("title_english") or "",
                title_native=title.get("native") or cached.get("title_native") or "",
                synonyms=[s for s in (media.get("synonyms") or []) if s],
                episodes=media.get("episodes"),
                cover_image=(media.get("coverImage") or {}).get("large", "")
                or cached.get("cover_image")
                or "",
                description=media.get("description") or cached.get("description") or "",
                genres=_json.dumps(media.get("genres") or []),
                status=media.get("status") or cached.get("status") or "",
                year=int(year),
                # Preserve fields the group builder / scanner may have populated
                rating=cached.get("rating"),
                studio=cached.get("studio") or "",
                imdb_id=cached.get("imdb_id") or "",
                tvdb_id=cached.get("tvdb_id") or "",
                tvmaze_id=cached.get("tvmaze_id") or "",
            )
            logger.info(
                "Backfilled cached year=%d for anilist_id=%d", int(year), anilist_id
            )
        except Exception as exc:
            logger.warning(
                "Could not backfill metadata for anilist_id=%d: %s", anilist_id, exc
            )

    async def _get_anilist_title_and_year(self, anilist_id: int) -> tuple[str, int]:
        """Return the best available (title, year) for an AniList entry."""
        info = await self._get_anilist_title_info(anilist_id)
        return info["title"], info["year"]

    async def _get_anilist_title_info(self, anilist_id: int) -> dict:
        """Return title variants and year for an AniList entry.

        Returns dict with keys: title, title_romaji, title_english, year.
        ``title`` is resolved according to the user's app.title_display pref.
        """
        title_pref = await self._db.get_setting("app.title_display") or "romaji"
        romaji = ""
        english = ""
        year = 0

        cached = await self._db.get_cached_metadata(anilist_id)
        if cached:
            romaji = cached.get("title_romaji") or ""
            english = cached.get("title_english") or ""
            year = int(cached.get("year") or 0)

        # Watchlist entry may have a better title (user-facing)
        users = await self._db.get_users_by_service("anilist")
        if users:
            entry = await self._db.get_watchlist_entry(users[0]["user_id"], anilist_id)
            if entry and entry.get("anilist_title"):
                # Watchlist stores a single display title — use as fallback
                if not romaji:
                    romaji = entry["anilist_title"]
                year = year or int(entry.get("start_year") or 0)

        # Resolve display title based on user preference
        if title_pref == "english" and english:
            title = english
        elif romaji:
            title = romaji
        else:
            title = english or romaji

        return {
            "title": title,
            "title_romaji": romaji,
            "title_english": english,
            "year": year,
        }

    @staticmethod
    def _prune_orphan_source(local_current: str, *protected: str) -> None:
        """Remove the moved file's old folder if no media remains there.

        Reuses the restructurer's cleanup so a move leaves behind no orphaned
        folder full of stale nfo/artwork.  ``protected`` guards the move target
        and library root from deletion.  Best-effort — never blocks the move.
        """
        try:
            from src.Scanner.LibraryRestructurer import prune_orphaned_dir

            old_dir = str(Path(local_current).parent)
            prune_orphaned_dir(old_dir, list(protected))
        except Exception:
            logger.debug(
                "Orphan-source prune failed for %s", local_current, exc_info=True
            )

    @staticmethod
    def _move_file(src: str, dst: str) -> bool:
        """Move src to dst, creating parent directories as needed. True on success."""
        try:
            parent = Path(dst).parent
            parent.mkdir(parents=True, exist_ok=True)
            # Ensure the directory (and all parents we just created) are group-writable
            # so that other containers sharing the same GID (e.g. Sonarr) can write.
            for p in [parent, *parent.parents]:
                try:
                    p.chmod(p.stat().st_mode | 0o775)
                except Exception:
                    break  # Stop at the first directory we don't own
            shutil.move(src, dst)
            # shutil.move preserves the source file's mode, which (coming from
            # the download client) may not be group-writable. Ensure the group
            # (shared with Sonarr/Radarr) can manage the file after the move.
            try:
                Path(dst).chmod(Path(dst).stat().st_mode | 0o664)
            except Exception:
                logger.debug("Could not adjust mode on %s", dst, exc_info=True)
            logger.info("Moved %s → %s", src, dst)
            return True
        except Exception as exc:
            logger.error("Failed to move %s → %s: %s", src, dst, exc)
            return False

    async def _get_season_folder_name(
        self, season_number: int, title_info: dict
    ) -> str:
        """Render the season subfolder name using the user's season folder template."""
        from src.Utils.NamingTemplate import (
            DEFAULT_ILLEGAL_CHAR_REPLACEMENT,
            DEFAULT_SEASON_FOLDER_TEMPLATE,
            NamingTemplate,
        )

        tmpl_str = await self._db.get_setting("naming.season_folder_template") or ""
        illegal_repl = (
            await self._db.get_setting("naming.illegal_char_replacement") or ""
        )
        tmpl = NamingTemplate(tmpl_str or DEFAULT_SEASON_FOLDER_TEMPLATE)
        year = title_info["year"]
        tokens = {
            "season": str(season_number),
            "season.name": title_info["title"],
            "year": str(year) if year else "",
        }
        rendered = tmpl.render(tokens)
        return (
            NamingTemplate.sanitize(
                rendered, illegal_repl or DEFAULT_ILLEGAL_CHAR_REPLACEMENT
            )
            or f"Season {season_number}"
        )

    async def _get_folder_name(self, title_info: dict) -> str:
        """Render the AniList subfolder name using the user's folder naming template."""
        from src.Utils.NamingTemplate import (
            DEFAULT_FOLDER_TEMPLATE,
            DEFAULT_ILLEGAL_CHAR_REPLACEMENT,
            NamingTemplate,
        )

        folder_tmpl_str = await self._db.get_setting("naming.folder_template") or ""
        illegal_repl = (
            await self._db.get_setting("naming.illegal_char_replacement") or ""
        )
        tmpl = NamingTemplate(folder_tmpl_str or DEFAULT_FOLDER_TEMPLATE)
        year = title_info["year"]
        tokens = {
            "title": title_info["title"],
            "title.romaji": title_info["title_romaji"] or title_info["title"],
            "title.english": title_info["title_english"] or title_info["title"],
            "year": str(year) if year else "",
        }
        rendered = tmpl.render(tokens)
        return NamingTemplate.sanitize(
            rendered, illegal_repl or DEFAULT_ILLEGAL_CHAR_REPLACEMENT
        ) or NamingTemplate.sanitize(title_info["title"])

    async def _get_file_name(
        self,
        original_filename: str,
        title_info: dict,
        season_number: int,
        episode_number: int,
    ) -> str:
        """Render the episode file name using the user's file template.

        Returns the original filename unchanged if no file template is set.
        """
        import os

        from src.Utils.NamingTemplate import (
            DEFAULT_FILE_TEMPLATE,
            DEFAULT_ILLEGAL_CHAR_REPLACEMENT,
            NamingTemplate,
            parse_quality,
        )

        tmpl_str = await self._db.get_setting("naming.file_template") or ""
        if not tmpl_str:
            return original_filename  # No template configured — keep original

        illegal_repl = (
            await self._db.get_setting("naming.illegal_char_replacement") or ""
        )
        _name, ext = os.path.splitext(original_filename)
        quality = parse_quality(original_filename)
        year = title_info["year"]

        tokens = {
            "title": title_info["title"],
            "title.romaji": title_info["title_romaji"] or title_info["title"],
            "title.english": title_info["title_english"] or title_info["title"],
            "year": str(year) if year else "",
            "season": f"{season_number:02d}",
            "episode": f"{episode_number:02d}",
            "quality": quality.full,
            "quality.resolution": quality.resolution,
            "quality.source": quality.source,
        }
        tmpl = NamingTemplate(tmpl_str or DEFAULT_FILE_TEMPLATE)
        rendered = tmpl.render(tokens)
        sanitized = NamingTemplate.sanitize(
            rendered, illegal_repl or DEFAULT_ILLEGAL_CHAR_REPLACEMENT
        )
        return (sanitized + ext) if sanitized else original_filename

    async def _get_movie_file_name(
        self, original_filename: str, title_info: dict
    ) -> str:
        """Render the movie file name using the user's movie file template.

        Returns the original filename unchanged if no template is set.
        """
        import os

        from src.Utils.NamingTemplate import (
            DEFAULT_ILLEGAL_CHAR_REPLACEMENT,
            DEFAULT_MOVIE_FILE_TEMPLATE,
            NamingTemplate,
            parse_quality,
        )

        tmpl_str = await self._db.get_setting("naming.movie_file_template") or ""
        if not tmpl_str:
            return original_filename

        illegal_repl = (
            await self._db.get_setting("naming.illegal_char_replacement") or ""
        )
        _name, ext = os.path.splitext(original_filename)
        quality = parse_quality(original_filename)
        year = title_info["year"]

        tokens = {
            "title": title_info["title"],
            "title.romaji": title_info["title_romaji"] or title_info["title"],
            "title.english": title_info["title_english"] or title_info["title"],
            "year": str(year) if year else "",
            "quality": quality.full,
            "quality.resolution": quality.resolution,
            "quality.source": quality.source,
        }
        tmpl = NamingTemplate(tmpl_str or DEFAULT_MOVIE_FILE_TEMPLATE)
        rendered = tmpl.render(tokens)
        sanitized = NamingTemplate.sanitize(
            rendered, illegal_repl or DEFAULT_ILLEGAL_CHAR_REPLACEMENT
        )
        return (sanitized + ext) if sanitized else original_filename
