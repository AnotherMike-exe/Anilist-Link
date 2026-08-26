"""Bringing manually-acquired media into the library.

Two ways in, both preview-first and both ending with Sonarr/Radarr told where
the files went:

* ``/api/import/entry/*`` re-scans one library entry's own folder, for files
  dropped next to a show that is already there; and
* ``/api/import/folder/*`` works through a configured import folder, matching
  each dropped folder to an AniList entry for review before anything moves.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from src.Download.ImportProcessor import ImportProcessor, count_media, has_media
from src.Download.SonarrEpisodeMapper import MappingError, SonarrEpisodeMapper
from src.Matching.Normalizer import extract_year_from_name
from src.Scanner.LibraryScanner import LibraryScanner
from src.Web.Routes.Helpers import create_title_matcher

logger = logging.getLogger(__name__)

router = APIRouter(tags=["import"])


async def _import_root(db: Any) -> str:
    return (await db.get_setting("library.import_path") or "").strip()


async def _library_output_root(db: Any) -> str:
    """The library directory imports land in — same one the restructurer uses."""
    split = (await db.get_setting("library.split_movies_tv") or "").lower() in (
        "true",
        "1",
        "yes",
    )
    if split:
        tv = (await db.get_setting("library.tv_output_path") or "").strip()
        if tv:
            return tv
    libraries = await db.get_all_libraries()
    if libraries:
        import json

        paths = json.loads(libraries[0].get("paths", "[]") or "[]")
        if paths:
            return paths[0]
    return ""


async def _resolve_entry_folder(db: Any, anilist_id: int) -> dict[str, Any] | None:
    row = await db.fetch_one(
        "SELECT id, library_id, folder_path, folder_name, anilist_id"
        " FROM library_items WHERE anilist_id=? AND folder_path != ''"
        " ORDER BY id LIMIT 1",
        (anilist_id,),
    )
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Path 1 — re-scan one entry's folder
# ---------------------------------------------------------------------------


@router.post("/api/import/entry/preview")
async def import_entry_preview(request: Request) -> JSONResponse:
    """Show what re-scanning an entry's folder would move. Body: {anilist_id}."""
    app_state = request.app.state
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    anilist_id = int(body.get("anilist_id", 0))
    if not anilist_id:
        return JSONResponse({"error": "anilist_id required"}, status_code=400)

    item = await _resolve_entry_folder(app_state.db, anilist_id)
    if not item:
        return JSONResponse(
            {"error": "No on-disk folder is recorded for this entry."},
            status_code=404,
        )
    folder = item["folder_path"]
    if not os.path.isdir(folder):
        return JSONResponse(
            {"error": f"Folder not found on disk: {folder}"}, status_code=400
        )

    processor = ImportProcessor(
        db=app_state.db,
        config=app_state.config,
        anilist_client=app_state.anilist_client,
        app_state=app_state,
    )
    try:
        plan = await processor.plan_folder(
            folder, anilist_id, await _library_output_root(app_state.db)
        )
    except Exception as exc:
        logger.exception("Import preview failed for anilist_id=%d", anilist_id)
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse(
        {
            "ok": True,
            "folder": folder,
            "anilist_id": anilist_id,
            "anilist_title": plan.anilist_title,
            "target_folder": plan.target_folder,
            "media_count": plan.media_count,
            "moves": plan.moves,
            "warnings": plan.warnings,
            "ready": plan.ready,
        }
    )


@router.post("/api/import/entry/execute")
async def import_entry_execute(request: Request) -> JSONResponse:
    """Re-scan an entry's folder and file its contents. Body: {anilist_id}."""
    app_state = request.app.state
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    anilist_id = int(body.get("anilist_id", 0))
    if not anilist_id:
        return JSONResponse({"error": "anilist_id required"}, status_code=400)

    item = await _resolve_entry_folder(app_state.db, anilist_id)
    if not item or not os.path.isdir(item["folder_path"]):
        return JSONResponse(
            {"error": "No usable on-disk folder for this entry."}, status_code=404
        )

    processor = ImportProcessor(
        db=app_state.db,
        config=app_state.config,
        anilist_client=app_state.anilist_client,
        app_state=app_state,
    )
    try:
        result = await processor.execute_folder(
            item["folder_path"],
            anilist_id,
            await _library_output_root(app_state.db),
        )
    except Exception as exc:
        logger.exception("Import execute failed for anilist_id=%d", anilist_id)
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse(result)


@router.post("/api/import/entry/rescan-arr")
async def import_entry_rescan_arr(request: Request) -> JSONResponse:
    """Tell Sonarr/Radarr to re-look at an entry's folder. Body: {anilist_id}.

    For files that are already correctly placed but which *arr has never seen —
    moved by an earlier restructure, or dropped in by hand — so it reports them
    missing.  Nothing on disk is touched: the stored path is corrected if it
    drifted, then a rescan is issued.
    """
    app_state = request.app.state
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    anilist_id = int(body.get("anilist_id", 0))
    if not anilist_id:
        return JSONResponse({"error": "anilist_id required"}, status_code=400)

    processor = ImportProcessor(
        db=app_state.db,
        config=app_state.config,
        anilist_client=app_state.anilist_client,
        app_state=app_state,
    )
    try:
        arr = await processor.notify_arr(anilist_id)
    except Exception as exc:
        logger.exception("Rescan request failed for anilist_id=%d", anilist_id)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    if arr.get("action") == "not_linked":
        return JSONResponse(
            {"ok": False, "error": "This entry isn't linked to Sonarr or Radarr."},
            status_code=400,
        )
    if arr.get("error"):
        return JSONResponse({"ok": False, "error": arr["error"]}, status_code=502)

    return JSONResponse({"ok": True, "arr": arr})


@router.post("/api/import/entry/map-episodes")
async def import_entry_map_episodes(request: Request) -> JSONResponse:
    """Associate an entry's on-disk files with Sonarr episodes.

    Body: ``{anilist_id, dry_run}``.  For a split cour, where our files are
    numbered by AniList (``S02E01``) and Sonarr numbers the same run as one
    long season, so it lists the files with no season or episode at all.  With
    ``dry_run`` the mapping is only reported; either way nothing on disk is
    renamed or moved.
    """
    app_state = request.app.state
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    anilist_id = int(body.get("anilist_id", 0))
    if not anilist_id:
        return JSONResponse({"error": "anilist_id required"}, status_code=400)
    dry_run = bool(body.get("dry_run", True))

    mapper = SonarrEpisodeMapper(
        db=app_state.db,
        config=app_state.config,
        anilist_client=app_state.anilist_client,
    )
    try:
        result = (
            await mapper.plan(anilist_id) if dry_run else await mapper.apply(anilist_id)
        )
    except MappingError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:
        logger.exception("Episode mapping failed for anilist_id=%d", anilist_id)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    return JSONResponse({"ok": True, "dry_run": dry_run, **result})


# ---------------------------------------------------------------------------
# Path 2 — the import folder
# ---------------------------------------------------------------------------


@router.get("/api/import/folder/scan")
async def import_folder_scan(request: Request) -> JSONResponse:
    """List what's sitting in the import folder, matched to AniList for review.

    Matching is a suggestion only — nothing moves until the user confirms each
    row, which is what makes a wrong sub-season match correctable rather than
    something discovered afterwards on disk.
    """
    app_state = request.app.state
    db = app_state.db

    root = await _import_root(db)
    if not root:
        return JSONResponse(
            {"error": "No import folder configured (Settings → Library)."},
            status_code=400,
        )
    if not os.path.isdir(root):
        return JSONResponse(
            {"error": f"Import folder not found: {root}"}, status_code=400
        )

    scanner = LibraryScanner(
        db=db,
        anilist_client=app_state.anilist_client,
        title_matcher=create_title_matcher(),
    )

    entries: list[dict[str, Any]] = []
    try:
        names = sorted(os.listdir(root))
    except OSError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    for name in names:
        if name.startswith("."):
            continue
        full = os.path.join(root, name)
        is_dir = os.path.isdir(full)
        if is_dir:
            if not has_media(full):
                continue
            media_count = count_media(full)
        else:
            if os.path.splitext(name)[1].lower() not in {
                ".mkv",
                ".mp4",
                ".avi",
                ".m4v",
                ".mov",
                ".ts",
                ".m2ts",
                ".webm",
            }:
                continue
            media_count = 1

        row: dict[str, Any] = {
            "name": name,
            "path": full,
            "is_dir": is_dir,
            "media_count": media_count,
            "anilist_id": 0,
            "anilist_title": "",
            "confidence": 0.0,
        }
        try:
            match = await scanner._search_and_match(name, extract_year_from_name(name))
            if match:
                entry, score = match
                row["anilist_id"] = entry.get("id", 0)
                title = entry.get("title") or {}
                row["anilist_title"] = title.get("romaji") or title.get("english") or ""
                row["confidence"] = round(float(score), 1)
        except Exception:
            logger.debug("Import match failed for %s", name, exc_info=True)

        entries.append(row)

    return JSONResponse({"ok": True, "root": root, "entries": entries})


@router.post("/api/import/folder/preview")
async def import_folder_preview(request: Request) -> JSONResponse:
    """Preview one import-folder item against a confirmed AniList entry.

    Body: { path, anilist_id }
    """
    app_state = request.app.state
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    path = str(body.get("path", ""))
    anilist_id = int(body.get("anilist_id", 0))
    err = await _validate_import_path(app_state.db, path, anilist_id)
    if err:
        return err

    processor = ImportProcessor(
        db=app_state.db,
        config=app_state.config,
        anilist_client=app_state.anilist_client,
        app_state=app_state,
    )
    try:
        plan = await processor.plan_folder(
            path, anilist_id, await _library_output_root(app_state.db)
        )
    except Exception as exc:
        logger.exception("Import folder preview failed for %s", path)
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse(
        {
            "ok": True,
            "path": path,
            "anilist_id": anilist_id,
            "anilist_title": plan.anilist_title,
            "target_folder": plan.target_folder,
            "media_count": plan.media_count,
            "moves": plan.moves,
            "warnings": plan.warnings,
            "ready": plan.ready,
        }
    )


@router.post("/api/import/folder/execute")
async def import_folder_execute(request: Request) -> JSONResponse:
    """Import one reviewed item. Body: { path, anilist_id }."""
    app_state = request.app.state
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    path = str(body.get("path", ""))
    anilist_id = int(body.get("anilist_id", 0))
    err = await _validate_import_path(app_state.db, path, anilist_id)
    if err:
        return err

    processor = ImportProcessor(
        db=app_state.db,
        config=app_state.config,
        anilist_client=app_state.anilist_client,
        app_state=app_state,
    )
    try:
        result = await processor.execute_folder(
            path, anilist_id, await _library_output_root(app_state.db)
        )
    except Exception as exc:
        logger.exception("Import folder execute failed for %s", path)
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse(result)


async def _validate_import_path(
    db: Any, path: str, anilist_id: int
) -> JSONResponse | None:
    """Reject anything outside the configured import folder.

    The path arrives from the browser, so it is checked against the configured
    root before the restructurer is pointed at it — otherwise this endpoint
    would move any directory on the host.
    """
    if not path or not anilist_id:
        return JSONResponse(
            {"error": "path and anilist_id are required"}, status_code=400
        )

    root = await _import_root(db)
    if not root:
        return JSONResponse({"error": "No import folder configured."}, status_code=400)

    real_root = os.path.realpath(root)
    real_path = os.path.realpath(path)
    if real_path != real_root and not real_path.startswith(real_root + os.sep):
        logger.warning("Rejected import path outside the import folder: %s", path)
        return JSONResponse(
            {"error": "Path is outside the configured import folder."},
            status_code=400,
        )
    if not os.path.exists(real_path):
        return JSONResponse({"error": f"Not found: {path}"}, status_code=404)
    return None


@router.get("/import", response_class=HTMLResponse)
async def import_page(request: Request) -> HTMLResponse:
    """Review and import media dropped into the import folder."""
    db = request.app.state.db
    return request.app.state.templates.TemplateResponse(
        "import.html",
        {
            "request": request,
            "import_root": await _import_root(db),
            "version": "0.1.0",
        },
    )
