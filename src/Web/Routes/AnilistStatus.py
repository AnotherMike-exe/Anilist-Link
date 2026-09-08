"""AniList API availability endpoints backing the dashboard status banner."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.Clients.AnilistHealth import STATE_DEGRADED, STATE_DOWN
from src.Sync.AnilistHealthMonitor import format_duration, persist_health_state

logger = logging.getLogger(__name__)

router = APIRouter()


def _banner_text(snapshot: dict) -> tuple[str, str]:
    """Return ``(headline, detail)`` for the status banner."""
    state = snapshot.get("state")

    if state == STATE_DOWN:
        headline = (
            "AniList API is unavailable — syncs, scans, and downloads are paused."
        )
        detail = str(snapshot.get("reason") or "")
        down_for = format_duration(float(snapshot.get("down_seconds") or 0))
        detail = f"{detail} Down for {down_for}." if detail else f"Down for {down_for}."
        return headline, detail

    if state == STATE_DEGRADED:
        reduced = snapshot.get("reduced_limit")
        if reduced:
            headline = (
                f"AniList has reduced its rate limit to {reduced} requests/minute."
            )
            detail = "Scans and syncs will run slower than usual."
        else:
            headline = "AniList is rate limiting requests."
            detail = "Requests are being throttled; work will take longer."
        return headline, detail

    return "", ""


@router.get("/api/anilist/status")
async def anilist_status(request: Request) -> JSONResponse:
    """Current AniList availability, for the persistent status banner."""
    health = request.app.state.anilist_client.health
    snapshot = health.snapshot()
    headline, detail = _banner_text(snapshot)
    snapshot["headline"] = headline
    snapshot["detail_text"] = detail
    snapshot["down_for"] = format_duration(float(snapshot.get("down_seconds") or 0))
    return JSONResponse(snapshot)


@router.post("/api/anilist/check-now")
async def anilist_check_now(request: Request) -> JSONResponse:
    """Probe AniList immediately instead of waiting for the backoff timer."""
    client = request.app.state.anilist_client
    health = client.health

    if not health.is_down:
        # Nothing to recover from; report current state as-is.
        return JSONResponse({"ok": True, "recovered": True, **health.snapshot()})

    recovered = await client.probe()
    # Persist right away so a restart moments later doesn't resurrect a
    # cleared outage. The recovery notification is left to the health
    # monitor, which watches the same state transition.
    await persist_health_state(request.app.state.db, health)
    return JSONResponse({"ok": True, "recovered": recovered, **health.snapshot()})
