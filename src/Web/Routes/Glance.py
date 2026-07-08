"""Glance dashboard integration — key-gated rating widget for embedding.

Every other route in this app is unauthenticated by design (local-network
trust, see CLAUDE.md). These two routes are the exception: they're meant to
be reached from outside a normal browser session (a Glance iframe widget on
a separate dashboard), so they're gated by a shared API key generated from
Settings.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.responses import Response

from src.Web.Routes.Helpers import enrich_watchlist_entries, submit_anilist_rating

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/glance", tags=["glance"])


async def _check_key(request: Request, key: str) -> bool:
    db = request.app.state.db
    expected = await db.get_setting("glance.api_key")
    if not expected or not key:
        return False
    return secrets.compare_digest(key, expected)


@router.get("/rate-completed", response_class=HTMLResponse, response_model=None)
async def glance_rate_completed(request: Request, key: str = "") -> Response:
    """Standalone, iframe-sized page listing completed-but-unrated entries."""
    if not await _check_key(request, key):
        return HTMLResponse("Forbidden — invalid or missing key", status_code=403)

    db = request.app.state.db
    templates = request.app.state.templates

    users = await db.get_users_by_service("anilist")
    unrated_completed: list[dict[str, Any]] = []
    if users:
        raw_completed = await db.get_watchlist(
            users[0]["user_id"], list_statuses=["COMPLETED"]
        )
        enriched = await enrich_watchlist_entries(db, raw_completed)
        unrated_completed = [e for e in enriched if not e["score"]]

    score_format = await db.get_setting("anilist.score_format") or "POINT_10"

    return templates.TemplateResponse(
        "glance_rate.html",
        {
            "request": request,
            "unrated_completed": unrated_completed,
            "score_format": score_format,
            "key": key,
        },
    )


@router.post("/rate-completed/submit", response_model=None)
async def glance_rate_completed_submit(request: Request) -> JSONResponse:
    """Submit a rating from the Glance widget. Body JSON: { key, anilist_id, score }."""
    db = request.app.state.db
    anilist_client = request.app.state.anilist_client

    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    key = str(body.get("key", ""))
    if not await _check_key(request, key):
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    anilist_id: int = int(body.get("anilist_id", 0))
    try:
        score = float(body.get("score", 0))
    except (TypeError, ValueError):
        return JSONResponse({"error": "score must be a number"}, status_code=400)

    if not anilist_id:
        return JSONResponse({"error": "anilist_id required"}, status_code=400)

    users = await db.get_users_by_service("anilist")
    if not users:
        return JSONResponse({"error": "No linked AniList user"}, status_code=400)

    try:
        await submit_anilist_rating(db, anilist_client, users[0], anilist_id, score)
        return JSONResponse({"ok": True, "anilist_id": anilist_id, "score": score})
    except Exception as exc:
        logger.exception(
            "Glance rating submission failed for anilist_id=%s", anilist_id
        )
        return JSONResponse({"error": str(exc)}, status_code=500)
