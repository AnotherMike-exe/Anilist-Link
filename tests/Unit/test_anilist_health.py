"""Tests for AniList outage tracking, fail-fast behaviour, and recovery probes."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.Clients.AnilistClient import AniListClient
from src.Clients.AnilistHealth import (
    NORMAL_RATE_LIMIT,
    PROBE_INTERVAL_SECONDS,
    STATE_DEGRADED,
    STATE_DOWN,
    STATE_OK,
    AniListHealth,
    AniListUnavailableError,
    looks_like_outage,
)

# The body AniList actually returns while the API is switched off.
DISABLED_BODY = (
    '{"errors":[{"message":"The AniList API has been temporarily disabled '
    'due to severe stability issues.","status":403}]}'
)


# ---------------------------------------------------------------------------
# Outage detection
# ---------------------------------------------------------------------------


class TestOutageDetection:
    def test_disabled_403_is_an_outage(self) -> None:
        assert looks_like_outage(403, DISABLED_BODY)

    def test_plain_403_is_not_an_outage(self) -> None:
        assert not looks_like_outage(403, '{"errors":[{"message":"Invalid token"}]}')

    def test_503_maintenance_is_an_outage(self) -> None:
        assert looks_like_outage(503, "Service Unavailable")

    def test_429_is_not_an_outage(self) -> None:
        assert not looks_like_outage(429, DISABLED_BODY)


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------


class TestHealthState:
    def test_starts_ok(self) -> None:
        assert AniListHealth().state == STATE_OK

    def test_outage_marks_down(self) -> None:
        h = AniListHealth()
        h.record_outage("API disabled")
        assert h.state == STATE_DOWN
        assert h.is_down

    def test_down_blocks_requests(self) -> None:
        h = AniListHealth()
        h.record_outage("API disabled")
        assert h.should_block()
        with pytest.raises(AniListUnavailableError):
            h.raise_if_down()

    def test_ok_does_not_block(self) -> None:
        h = AniListHealth()
        h.raise_if_down()  # must not raise
        assert not h.should_block()

    def test_success_clears_outage(self) -> None:
        h = AniListHealth()
        h.record_outage("API disabled")
        h.record_success()
        assert h.state == STATE_OK
        assert h.down_seconds == 0.0

    def test_repeated_outage_keeps_original_down_since(self) -> None:
        h = AniListHealth()
        h.record_outage("API disabled")
        first = h._down_since
        h.record_outage("API disabled")
        assert h._down_since == first

    def test_first_probe_is_an_hour_out(self) -> None:
        h = AniListHealth()
        h.record_outage("API disabled")
        assert 3500 < h.seconds_until_probe() <= PROBE_INTERVAL_SECONDS

    def test_concurrent_failures_do_not_move_the_probe(self) -> None:
        # Requests already in flight when the outage began all report it.
        # That is one outage, not five — the schedule must not shift.
        h = AniListHealth()
        h.record_outage("API disabled")
        scheduled = h._next_probe_at
        for _ in range(4):
            h.record_outage("API disabled")
        assert h._next_probe_at == scheduled

    def test_failed_probe_reschedules_a_full_interval_out(self) -> None:
        h = AniListHealth()
        h.record_outage("API disabled")
        h._next_probe_at = 0.0  # the probe window has elapsed; the probe failed
        h.record_outage("API disabled")
        assert 3500 < h.seconds_until_probe() <= PROBE_INTERVAL_SECONDS

    def test_reduced_rate_limit_marks_degraded(self) -> None:
        h = AniListHealth()
        h.record_rate_limit(30)
        assert h.state == STATE_DEGRADED
        assert h.rate_limit == 30

    def test_normal_rate_limit_stays_ok(self) -> None:
        h = AniListHealth()
        h.record_rate_limit(NORMAL_RATE_LIMIT)
        assert h.state == STATE_OK

    def test_rate_limit_recovery_clears_degraded(self) -> None:
        h = AniListHealth()
        h.record_rate_limit(30)
        h.record_rate_limit(NORMAL_RATE_LIMIT)
        assert h.state == STATE_OK

    def test_throttling_marks_degraded(self) -> None:
        h = AniListHealth()
        h.record_throttled(60)
        assert h.state == STATE_DEGRADED

    def test_down_outranks_degraded(self) -> None:
        h = AniListHealth()
        h.record_rate_limit(30)
        h.record_outage("API disabled")
        assert h.state == STATE_DOWN

    def test_success_does_not_clear_reduced_limit(self) -> None:
        # A working-but-throttled API is still worth warning about.
        h = AniListHealth()
        h.record_rate_limit(30)
        h.record_success()
        assert h.state == STATE_DEGRADED


class TestProbeScheduling:
    def test_probe_not_due_immediately_after_outage(self) -> None:
        h = AniListHealth()
        h.record_outage("API disabled")
        assert not h.is_probe_due()

    def test_probe_due_after_backoff_elapses(self) -> None:
        h = AniListHealth()
        h.record_outage("API disabled")
        h._next_probe_at = 0.0  # pretend the interval already passed
        assert h.is_probe_due()

    def test_healthy_client_never_probes(self) -> None:
        assert not AniListHealth().is_probe_due()

    def test_blocked_while_probe_pending(self) -> None:
        # The circuit stays closed to normal traffic even when a probe is due
        # — only the probe itself is allowed through.
        h = AniListHealth()
        h.record_outage("API disabled")
        h._next_probe_at = 0.0
        assert h.should_block()


class TestPersistence:
    def test_restore_preserves_downtime(self) -> None:
        h = AniListHealth()
        h.record_outage("API disabled")
        payload = h.persist_payload()

        fresh = AniListHealth()
        fresh.restore(
            down_since=payload["down_since"],
            reason=str(payload["reason"]),
            detail=str(payload["detail"]),
            reduced_limit=payload["reduced_limit"],
        )
        assert fresh.is_down
        assert fresh.reason == "API disabled"

    def test_restore_of_healthy_state_is_a_no_op(self) -> None:
        h = AniListHealth()
        h.restore(down_since=None)
        assert h.state == STATE_OK

    def test_restore_makes_a_probe_due_immediately(self) -> None:
        # One request per container start beats carrying a stale outage
        # for up to an hour.
        h = AniListHealth()
        h.restore(down_since=1.0, reason="API disabled")
        assert h.is_probe_due()

    def test_snapshot_is_json_friendly(self) -> None:
        h = AniListHealth()
        h.record_outage("API disabled", "body")
        snap = h.snapshot()
        assert snap["state"] == STATE_DOWN
        assert snap["blocked"] is True
        assert snap["down_seconds"] >= 0


# ---------------------------------------------------------------------------
# Client integration
# ---------------------------------------------------------------------------


def _response(status: int, body: str, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        text=body,
        headers=headers or {},
        request=httpx.Request("POST", "https://graphql.anilist.co"),
    )


@pytest.fixture
def client() -> AniListClient:
    return AniListClient(client_id="id", client_secret="secret")


@pytest.mark.asyncio
class TestClientCircuitBreaker:
    async def test_disabled_api_raises_immediately(self, client: AniListClient) -> None:
        client._http.post = AsyncMock(return_value=_response(403, DISABLED_BODY))

        with pytest.raises(AniListUnavailableError):
            await client.search_anime("Mushoku Tensei")

        assert client.health.is_down
        # No retry storm: the disabled response is believed the first time.
        assert client._http.post.await_count == 1
        await client.close()

    async def test_subsequent_calls_do_not_hit_the_network(
        self, client: AniListClient
    ) -> None:
        client._http.post = AsyncMock(return_value=_response(403, DISABLED_BODY))
        with pytest.raises(AniListUnavailableError):
            await client.search_anime("first")

        with pytest.raises(AniListUnavailableError):
            await client.search_anime("second")

        assert client._http.post.await_count == 1
        await client.close()

    async def test_reduced_limit_header_is_recorded(
        self, client: AniListClient
    ) -> None:
        client._http.post = AsyncMock(
            return_value=_response(
                200,
                '{"data":{"Page":{"media":[]}}}',
                {"X-RateLimit-Limit": "30", "X-RateLimit-Remaining": "29"},
            )
        )

        await client.search_anime("anything")

        assert client.health.state == STATE_DEGRADED
        assert client.health.rate_limit == 30
        await client.close()

    async def test_successful_call_keeps_state_ok(self, client: AniListClient) -> None:
        client._http.post = AsyncMock(
            return_value=_response(
                200,
                '{"data":{"Page":{"media":[]}}}',
                {"X-RateLimit-Limit": "90", "X-RateLimit-Remaining": "89"},
            )
        )

        await client.search_anime("anything")

        assert client.health.state == STATE_OK
        await client.close()

    async def test_persistent_5xx_opens_the_circuit(
        self, client: AniListClient
    ) -> None:
        client._http.post = AsyncMock(return_value=_response(502, "bad gateway"))

        with patch("asyncio.sleep", new=AsyncMock()):
            with pytest.raises(AniListUnavailableError):
                await client.search_anime("anything")

        assert client.health.is_down
        await client.close()

    async def test_authenticated_403_does_not_open_the_circuit(
        self, client: AniListClient
    ) -> None:
        # A stale user token must not halt the whole app.
        client._http.post = AsyncMock(
            return_value=_response(403, '{"errors":[{"message":"Invalid token"}]}')
        )

        with patch("asyncio.sleep", new=AsyncMock()):
            with pytest.raises(httpx.HTTPStatusError):
                await client._execute_query("query {}", {}, access_token="tok")

        assert not client.health.is_down
        await client.close()


@pytest.mark.asyncio
class TestClientProbe:
    async def test_successful_probe_clears_outage(self, client: AniListClient) -> None:
        client.health.record_outage("API disabled")
        client._http.post = AsyncMock(
            return_value=_response(200, '{"data":{"Media":{"id":1}}}')
        )

        assert await client.probe() is True
        assert client.health.state == STATE_OK
        await client.close()

    async def test_failed_probe_keeps_outage(self, client: AniListClient) -> None:
        client.health.record_outage("API disabled")
        client._http.post = AsyncMock(return_value=_response(403, DISABLED_BODY))

        assert await client.probe() is False
        assert client.health.is_down
        await client.close()

    async def test_probe_sends_exactly_one_request(self, client: AniListClient) -> None:
        client.health.record_outage("API disabled")
        client._http.post = AsyncMock(return_value=_response(403, DISABLED_BODY))

        await client.probe()

        assert client._http.post.await_count == 1
        await client.close()

    async def test_rate_limited_probe_counts_as_alive(
        self, client: AniListClient
    ) -> None:
        client.health.record_outage("API disabled")
        client._http.post = AsyncMock(
            return_value=_response(429, "slow down", {"Retry-After": "30"})
        )

        assert await client.probe() is True
        assert not client.health.is_down
        assert client.health.state == STATE_DEGRADED
        await client.close()

    async def test_network_failure_probe_keeps_outage(
        self, client: AniListClient
    ) -> None:
        client.health.record_outage("API disabled")
        client._http.post = AsyncMock(side_effect=httpx.ConnectError("no route"))

        assert await client.probe() is False
        assert client.health.is_down
        await client.close()
