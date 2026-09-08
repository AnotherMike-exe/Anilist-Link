"""Tests for the AniList health monitor loop and status endpoint helpers."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from src.Clients.AnilistHealth import AniListHealth
from src.Sync.AnilistHealthMonitor import (
    HEALTH_SETTING_KEY,
    anilist_health_monitor,
    format_duration,
    persist_health_state,
    restore_health_state,
)
from src.Web.Routes.AnilistStatus import _banner_text


class FakeDB:
    """Minimal stand-in for DatabaseManager's settings + notification API."""

    def __init__(self, settings: dict | None = None) -> None:
        self.settings = settings or {}
        self.notifications: list[dict] = []

    async def get_setting(self, key: str):
        return self.settings.get(key)

    async def set_setting(self, key: str, value: str) -> None:
        self.settings[key] = value

    async def add_notification(self, **kwargs) -> str:
        self.notifications.append(kwargs)
        return "nid"


class TestFormatDuration:
    def test_seconds(self) -> None:
        assert format_duration(45) == "45s"

    def test_minutes(self) -> None:
        assert format_duration(125) == "2m 5s"

    def test_hours(self) -> None:
        assert format_duration(3 * 3600 + 4 * 60) == "3h 4m"

    def test_days(self) -> None:
        assert format_duration(50 * 3600) == "2d 2h"

    def test_negative_clamped(self) -> None:
        assert format_duration(-10) == "0s"


@pytest.mark.asyncio
class TestPersistence:
    async def test_persist_writes_setting(self) -> None:
        db = FakeDB()
        health = AniListHealth()
        health.record_outage("API disabled")

        await persist_health_state(db, health)

        stored = json.loads(db.settings[HEALTH_SETTING_KEY])
        assert stored["down_since"] is not None
        assert stored["reason"] == "API disabled"

    async def test_restore_reads_setting(self) -> None:
        db = FakeDB(
            {
                HEALTH_SETTING_KEY: json.dumps(
                    {"down_since": 1000.0, "reason": "API disabled"}
                )
            }
        )
        health = AniListHealth()

        await restore_health_state(db, health)

        assert health.is_down
        # Downtime is measured from the persisted timestamp, not from now.
        assert health.down_seconds > 0

    async def test_restore_tolerates_garbage(self) -> None:
        db = FakeDB({HEALTH_SETTING_KEY: "not json"})
        health = AniListHealth()

        await restore_health_state(db, health)

        assert not health.is_down

    async def test_restore_with_no_saved_state(self) -> None:
        health = AniListHealth()
        await restore_health_state(FakeDB(), health)
        assert not health.is_down


@pytest.mark.asyncio
class TestMonitorLoop:
    async def _run_ticks(self, db, client, ticks: int = 3) -> None:
        task = asyncio.create_task(anilist_health_monitor(db, client, tick_seconds=0))
        for _ in range(ticks):
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_probes_when_due(self) -> None:
        health = AniListHealth()
        health.record_outage("API disabled")
        health._next_probe_at = 0.0
        client = AsyncMock()
        client.health = health
        client.probe = AsyncMock(return_value=False)

        await self._run_ticks(FakeDB(), client)

        assert client.probe.await_count >= 1

    async def test_does_not_probe_while_healthy(self) -> None:
        client = AsyncMock()
        client.health = AniListHealth()
        client.probe = AsyncMock(return_value=True)

        await self._run_ticks(FakeDB(), client)

        client.probe.assert_not_awaited()

    async def test_restored_outage_is_probed_before_the_first_sleep(self) -> None:
        # A restart makes a probe due immediately; the monitor must not sit
        # out a full tick (an hour of downtime is already on the clock).
        db = FakeDB(
            {
                HEALTH_SETTING_KEY: json.dumps(
                    {"down_since": 1000.0, "reason": "API disabled"}
                )
            }
        )
        client = AsyncMock()
        client.health = AniListHealth()
        client.probe = AsyncMock(return_value=False)

        task = asyncio.create_task(
            anilist_health_monitor(db, client, tick_seconds=3600)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        client.probe.assert_awaited_once()

    async def test_recovery_posts_notification_and_persists(self) -> None:
        health = AniListHealth()
        health.record_outage("API disabled")
        health._next_probe_at = 0.0
        db = FakeDB()

        client = AsyncMock()
        client.health = health

        async def _probe() -> bool:
            health.record_success()
            return True

        client.probe = _probe

        await self._run_ticks(db, client)

        assert not health.is_down
        assert len(db.notifications) == 1
        assert "back online" in db.notifications[0]["message"]
        assert json.loads(db.settings[HEALTH_SETTING_KEY])["down_since"] is None

    async def test_no_notification_while_still_down(self) -> None:
        health = AniListHealth()
        health.record_outage("API disabled")
        health._next_probe_at = 0.0
        db = FakeDB()

        client = AsyncMock()
        client.health = health

        async def _probe() -> bool:
            health.record_outage("API disabled")
            return False

        client.probe = _probe

        await self._run_ticks(db, client)

        assert db.notifications == []


class TestBannerText:
    def test_down_banner_mentions_paused_work(self) -> None:
        health = AniListHealth()
        health.record_outage("The AniList API has been temporarily disabled")
        headline, detail = _banner_text(health.snapshot())
        assert "unavailable" in headline
        assert "paused" in headline
        assert "Down for" in detail

    def test_degraded_banner_reports_reduced_limit(self) -> None:
        health = AniListHealth()
        health.record_rate_limit(30)
        headline, detail = _banner_text(health.snapshot())
        assert "30 requests/minute" in headline
        assert detail

    def test_throttled_banner_without_reduced_limit(self) -> None:
        health = AniListHealth()
        health.record_throttled(60)
        headline, _ = _banner_text(health.snapshot())
        assert "rate limiting" in headline

    def test_healthy_banner_is_empty(self) -> None:
        headline, detail = _banner_text(AniListHealth().snapshot())
        assert headline == ""
        assert detail == ""
