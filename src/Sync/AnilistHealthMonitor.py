"""Background monitor for AniList API availability.

Owns the two things :class:`~src.Clients.AnilistHealth.AniListHealth` cannot
do on its own:

* sending the periodic recovery probe while the API is down, so nothing
  else has to poll a dead endpoint to find out it is dead, and
* persisting outage state to ``app_settings`` so a container restart does
  not reset "down for three hours" back to zero, and posting a dashboard
  notification when the API comes back.
"""

from __future__ import annotations

import asyncio
import json
import logging

from src.Clients.AnilistClient import AniListClient
from src.Clients.AnilistHealth import STATE_DOWN, AniListHealth
from src.Database.Connection import DatabaseManager

logger = logging.getLogger(__name__)

#: How often the monitor wakes to check whether a probe is due. The probe
#: cadence itself lives in AniListHealth (hourly); this only bounds how
#: promptly a due probe fires and how quickly a state change is persisted.
MONITOR_TICK_SECONDS = 60

#: app_settings key holding the persisted outage snapshot.
HEALTH_SETTING_KEY = "anilist.health"


def format_duration(seconds: float) -> str:
    """Human-readable outage length, e.g. ``2h 14m`` or ``45s``."""
    total = int(max(0, seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


async def restore_health_state(db: DatabaseManager, health: AniListHealth) -> None:
    """Rehydrate a persisted outage so downtime survives a restart."""
    raw = await db.get_setting(HEALTH_SETTING_KEY)
    if not raw:
        return
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(payload, dict):
        return

    health.restore(
        down_since=payload.get("down_since"),
        reason=str(payload.get("reason") or ""),
        detail=str(payload.get("detail") or ""),
        reduced_limit=payload.get("reduced_limit"),
    )


async def persist_health_state(db: DatabaseManager, health: AniListHealth) -> None:
    """Write the outage snapshot to app_settings."""
    try:
        await db.set_setting(HEALTH_SETTING_KEY, json.dumps(health.persist_payload()))
    except Exception:
        logger.exception("Failed to persist AniList health state")


async def anilist_health_monitor(
    db: DatabaseManager,
    anilist_client: AniListClient,
    tick_seconds: int = MONITOR_TICK_SECONDS,
) -> None:
    """Probe AniList while it is down and persist every state transition.

    Runs for the lifetime of the app. While AniList is healthy this does
    nothing but sleep — no requests are made until an outage is recorded
    by a real request somewhere else in the app.
    """
    health = anilist_client.health

    await restore_health_state(db, health)
    previous_state = health.state
    last_version = health.version

    # Check before the first sleep: a restored outage schedules its probe
    # immediately, and waiting out a tick would delay it for no reason.
    while True:
        try:
            if health.is_probe_due():
                await anilist_client.probe()

            if health.version != last_version:
                last_version = health.version
                await persist_health_state(db, health)
                await _announce_transition(db, health, previous_state)
                previous_state = health.state
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("AniList health monitor iteration failed")

        try:
            await asyncio.sleep(tick_seconds)
        except asyncio.CancelledError:
            return


async def _announce_transition(
    db: DatabaseManager,
    health: AniListHealth,
    previous_state: str,
) -> None:
    """Post a dashboard notification when AniList comes back online.

    Ongoing outages and reduced-limit warnings are shown by the always-on
    status banner instead, so only the recovery — which the user may miss
    while away from the dashboard — becomes a persistent notification.
    """
    if previous_state != STATE_DOWN or health.is_down:
        return

    await db.add_notification(
        notification_type="success",
        message=("AniList API is back online — paused syncs and scans have resumed."),
    )
    logger.info("AniList recovery notification posted")
