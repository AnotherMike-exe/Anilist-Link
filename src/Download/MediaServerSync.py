"""Post-arr media server sync.

After a Sonarr/Radarr download is moved into the AniList-structured path,
Plex/Jellyfin need to (a) re-scan so the new files are indexed and (b)
get our AniList metadata + NFO written so the item is recognized as a
series rather than a generic folder.

This module owns a debounced, app-wide background task that batches many
back-to-back episode webhooks into a single sync pass.  Each successful
file move calls :func:`request_arr_media_sync`; the underlying worker
sleeps a short delay, drains pending requests, and then runs the
Plex/Jellyfin refresh + metadata-scan pipeline once.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.Clients.JellyfinClient import JellyfinClient
from src.Clients.PlexClient import PlexClient
from src.Scanner.JellyfinMetadataScanner import JellyfinMetadataScanner
from src.Scanner.MetadataScanner import MetadataScanner
from src.Utils.Config import AppConfig
from src.Web.App import spawn_background_task
from src.Web.Routes.Helpers import create_group_builder, create_title_matcher

logger = logging.getLogger(__name__)

_DEFAULT_DEBOUNCE_SECONDS = 30.0
_MANAGER_ATTR = "_arr_media_sync_manager"


class _ArrMediaSyncManager:
    """Debounced worker that runs the media-server sync after arr downloads."""

    def __init__(self) -> None:
        self._dirty = False
        self._task: asyncio.Task[None] | None = None

    def request(self, app_state: object, delay: float) -> None:
        self._dirty = True
        if self._task and not self._task.done():
            return
        self._task = spawn_background_task(app_state, self._loop(app_state, delay))

    async def _loop(self, app_state: object, delay: float) -> None:
        try:
            while self._dirty:
                self._dirty = False
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    return
                # Coalesce any requests that arrived during the sleep —
                # if more came in, keep waiting another debounce window.
                if self._dirty:
                    continue
                try:
                    await _run_media_server_sync(app_state)
                except Exception:
                    logger.exception("Post-arr media server sync failed")
        finally:
            self._task = None


def request_arr_media_sync(
    app_state: object, delay: float = _DEFAULT_DEBOUNCE_SECONDS
) -> None:
    """Schedule a debounced media-server sync after an arr download moved files.

    Multiple calls within *delay* seconds collapse into a single sync run.
    Safe to call from any post-move code path (webhook handlers, manual
    reprocess, etc.).
    """
    manager: _ArrMediaSyncManager | None = getattr(app_state, _MANAGER_ATTR, None)
    if manager is None:
        manager = _ArrMediaSyncManager()
        setattr(app_state, _MANAGER_ATTR, manager)
    manager.request(app_state, delay)


async def _run_media_server_sync(app_state: object) -> None:
    """Refresh and apply AniList metadata to Plex/Jellyfin for any new items."""
    config: AppConfig = app_state.config  # type: ignore[attr-defined]
    db = app_state.db  # type: ignore[attr-defined]
    anilist_client = app_state.anilist_client  # type: ignore[attr-defined]

    plex_enabled = bool(config.plex.url and config.plex.token)
    jellyfin_enabled = bool(config.jellyfin.url and config.jellyfin.api_key)

    if not plex_enabled and not jellyfin_enabled:
        logger.debug("Post-arr sync skipped — no Plex/Jellyfin configured")
        return

    logger.info(
        "Post-arr media server sync starting (plex=%s, jellyfin=%s)",
        plex_enabled,
        jellyfin_enabled,
    )

    if plex_enabled:
        try:
            await _sync_plex(app_state, config, db, anilist_client)
        except Exception:
            logger.exception("Post-arr Plex sync failed")

    if jellyfin_enabled:
        try:
            await _sync_jellyfin(app_state, config, db, anilist_client)
        except Exception:
            logger.exception("Post-arr Jellyfin sync failed")

    logger.info("Post-arr media server sync complete")


async def _sync_plex(
    app_state: object,
    config: AppConfig,
    db: Any,
    anilist_client: Any,
) -> None:
    library_keys = (
        list(config.plex.anime_library_keys) if config.plex.anime_library_keys else None
    )

    plex_client = PlexClient(url=config.plex.url, token=config.plex.token)
    try:
        keys_to_refresh = library_keys
        if not keys_to_refresh:
            libs = await plex_client.get_libraries()
            keys_to_refresh = [lib.key for lib in libs]

        for key in keys_to_refresh:
            try:
                await plex_client.refresh_library_and_wait(key, poll_interval=3.0)
            except Exception:
                logger.exception("Plex library %s refresh failed", key)

        title_matcher = create_title_matcher()
        group_builder = create_group_builder(db, anilist_client)
        scanner = MetadataScanner(
            db, anilist_client, title_matcher, plex_client, config, group_builder
        )
        await scanner.run_scan(dry_run=False, library_keys=library_keys)
    finally:
        await plex_client.close()


async def _sync_jellyfin(
    app_state: object,
    config: AppConfig,
    db: Any,
    anilist_client: Any,
) -> None:
    library_ids = (
        list(config.jellyfin.anime_library_ids)
        if config.jellyfin.anime_library_ids
        else None
    )

    jellyfin_client = JellyfinClient(
        url=config.jellyfin.url, api_key=config.jellyfin.api_key
    )
    listener = getattr(app_state, "jellyfin_listener", None)
    try:
        if listener:
            listener.suppress_callbacks = True

        await jellyfin_client.refresh_and_wait(app_state, library_ids=library_ids)

        title_matcher = create_title_matcher()
        group_builder = create_group_builder(db, anilist_client)
        scanner = JellyfinMetadataScanner(
            db, anilist_client, title_matcher, jellyfin_client, config, group_builder
        )
        results = await scanner.run_scan(preview=False, library_ids=library_ids)

        if results.matched > 0:
            # NFOs were written — refresh so Jellyfin reads them, then
            # recursively refresh episode metadata so providers fill in
            # per-episode data using the IDs we just wrote.
            await jellyfin_client.refresh_and_wait(app_state, library_ids=library_ids)
            for lib_id in library_ids or []:
                series_ids = await jellyfin_client.get_series_ids_in_library(lib_id)
                for sid in series_ids:
                    try:
                        await jellyfin_client.refresh_item_metadata(
                            sid, recursive=True, replace_all=True
                        )
                    except Exception:
                        logger.debug(
                            "refresh_item_metadata failed for %s", sid, exc_info=True
                        )

        try:
            await jellyfin_client.delete_virtual_seasons(library_ids)
        except Exception:
            logger.debug("delete_virtual_seasons failed", exc_info=True)
    finally:
        if listener:
            listener.suppress_callbacks = False
        await jellyfin_client.close()
