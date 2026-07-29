"""Single-item "Smart Move" — restructure one on-disk library item.

Lets a user fix the location of a show/movie that already lives in the library
but is NOT tracked in Sonarr/Radarr (so the *arr reprocess move can't help it).
Reuses the LibraryRestructurer so nesting, naming, NFO writing and orphan
cleanup all match a full restructure — just scoped to a single item.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.Scanner.LibraryRestructurer import (
    LibraryRestructurer,
    RestructureProgress,
    ShowInput,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["smart-move"])


async def _resolve_item(db: Any, body: dict) -> dict | None:
    """Resolve a library_items row from a library_item_id or an anilist_id."""
    item_id = body.get("library_item_id")
    if item_id:
        return await db.get_library_item(int(item_id))
    anilist_id = body.get("anilist_id")
    if anilist_id:
        return await db.fetch_one(
            "SELECT * FROM library_items"
            " WHERE anilist_id=? AND folder_path IS NOT NULL AND folder_path != ''"
            " ORDER BY id LIMIT 1",
            (int(anilist_id),),
        )
    return None


async def _library_root_for(db: Any, item: dict) -> str:
    """Return the library root dir that contains the item's folder_path."""
    folder = (item.get("folder_path") or "").rstrip("/")
    library = await db.get_library(item.get("library_id") or 0)
    roots: list[str] = []
    if library and library.get("paths"):
        import json as _json

        try:
            roots = _json.loads(library["paths"]) or []
        except (ValueError, TypeError):
            roots = []
    for r in roots:
        rr = r.rstrip("/")
        if folder == rr or folder.startswith(rr + "/"):
            return r
    # Fallback: the item folder's parent, else the first configured root.
    return os.path.dirname(folder) or (roots[0] if roots else "")


async def _build_show_input(db: Any, item: dict) -> ShowInput:
    folder = item["folder_path"]
    aid = item.get("anilist_id") or 0
    cache = await db.get_cached_metadata(aid) if aid else None
    cache = cache or {}
    romaji = cache.get("title_romaji") or ""
    english = cache.get("title_english") or ""
    year = cache.get("year") or item.get("year") or 0
    episodes = cache.get("episodes") or item.get("anilist_episodes")
    return ShowInput(
        title=item.get("folder_name") or os.path.basename(folder.rstrip("/")),
        local_path=folder,
        source_id=folder,
        anilist_id=int(aid),
        anilist_title=item.get("anilist_title") or romaji or english or "",
        year=int(year or 0),
        anilist_title_romaji=romaji,
        anilist_title_english=english,
        anilist_format=item.get("anilist_format") or "",
        anilist_episodes=episodes,
    )


async def _analyze_one(
    app_state: Any, item: dict
) -> tuple[LibraryRestructurer, Any, str]:
    """Build a one-item plan. Returns (restructurer, plan, output_root)."""
    db = app_state.db
    anilist_client = app_state.anilist_client
    show = await _build_show_input(db, item)
    output_dir = await _library_root_for(db, item)
    restructurer = await LibraryRestructurer.from_settings(db, anilist_client)
    progress = RestructureProgress(status="running")
    plan = await restructurer.analyze(
        [show], progress, level="full_restructure", output_dir=output_dir or None
    )
    return restructurer, plan, output_dir


def _validate(item: dict | None) -> JSONResponse | None:
    """Return an error response if the item can't be smart-moved, else None."""
    if not item or not item.get("folder_path"):
        return JSONResponse(
            {"error": "No on-disk library item found for this entry."},
            status_code=404,
        )
    if not item.get("anilist_id"):
        return JSONResponse(
            {"error": "Item has no AniList match — set a match first."},
            status_code=400,
        )
    if not os.path.isdir(item["folder_path"]):
        return JSONResponse(
            {"error": f"Folder not found on disk: {item['folder_path']}"},
            status_code=400,
        )
    return None


@router.post("/api/smart-move/preview")
async def smart_move_preview(request: Request) -> JSONResponse:
    """Preview the standardized location for a single library item.

    Body JSON: { library_item_id? , anilist_id? }
    """
    db = request.app.state.db
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    item = await _resolve_item(db, body)
    err = _validate(item)
    if err:
        return err
    assert item is not None

    try:
        _, plan, _ = await _analyze_one(request.app.state, item)
    except Exception as exc:
        logger.exception("Smart-move preview failed")
        return JSONResponse({"error": str(exc)}, status_code=500)

    current_folder = item["folder_path"]
    if not plan.groups:
        return JSONResponse(
            {
                "ok": True,
                "needs_move": False,
                "message": "Already in the correct location.",
                "current_folder": current_folder,
            }
        )

    group = plan.groups[0]
    moves = [{"from": fm.source, "to": fm.destination} for fm in group.file_moves]
    return JSONResponse(
        {
            "ok": True,
            "needs_move": True,
            "display_title": group.display_title,
            "current_folder": current_folder,
            "target_folder": group.target_folder,
            "file_count": len(group.file_moves),
            "support_file_count": group.support_file_count,
            "moves": moves[:20],
            "truncated": len(moves) > 20,
        }
    )


@router.post("/api/smart-move/execute")
async def smart_move_execute(request: Request) -> JSONResponse:
    """Execute the standardized move for a single library item.

    Body JSON: { library_item_id? , anilist_id? }
    """
    app_state = request.app.state
    db = app_state.db
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    item = await _resolve_item(db, body)
    err = _validate(item)
    if err:
        return err
    assert item is not None

    old_folder = item["folder_path"]
    library_id = item.get("library_id") or 0

    try:
        restructurer, plan, _ = await _analyze_one(app_state, item)
    except Exception as exc:
        logger.exception("Smart-move analyze failed")
        return JSONResponse({"error": str(exc)}, status_code=500)

    if not plan.groups:
        return JSONResponse(
            {"ok": True, "moved": 0, "message": "Already in the correct location."}
        )

    group = plan.groups[0]
    progress = RestructureProgress(status="running")
    try:
        stats = await restructurer.execute(plan, progress)
    except Exception as exc:
        logger.exception("Smart-move execute failed")
        return JSONResponse({"error": str(exc)}, status_code=500)

    # Re-point the library_items row (and local mapping) at the new folder so
    # the UI reflects the move without a full re-index.
    new_folder = group.target_folder
    try:
        if library_id and new_folder and new_folder.rstrip("/") != old_folder.rstrip(
            "/"
        ):
            await db.execute(
                "UPDATE library_items"
                " SET folder_path=?, folder_name=?, series_group_id=?"
                " WHERE id=?",
                (
                    new_folder,
                    os.path.basename(new_folder.rstrip("/")),
                    group.series_group_id or None,
                    item["id"],
                ),
            )
            await db.execute(
                "UPDATE media_mappings SET source_id=?"
                " WHERE source='local' AND source_id=?",
                (new_folder, old_folder),
            )
    except Exception:
        logger.warning("Could not update library_items after smart move", exc_info=True)

    # Refresh Plex/Jellyfin + apply AniList metadata for the moved folder.
    try:
        from src.Download.MediaServerSync import request_arr_media_sync

        request_arr_media_sync(app_state)
    except Exception:
        logger.debug("Post-smart-move media sync scheduling failed", exc_info=True)

    logger.info(
        "Smart move: anilist_id=%s moved %d file(s) → %s",
        item.get("anilist_id"),
        stats.get("files_moved", 0),
        new_folder,
    )
    return JSONResponse(
        {
            "ok": True,
            "moved": stats.get("files_moved", 0),
            "errors": stats.get("errors", 0),
            "target_folder": new_folder,
        }
    )
