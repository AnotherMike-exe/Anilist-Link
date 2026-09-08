"""Circuit-breaker style health tracking for the AniList API.

AniList periodically takes its public API offline outright — responding
``403`` with *"The AniList API has been temporarily disabled due to severe
stability issues."* — or leaves it up but drops the rate limit from the
normal 90 req/min to a reduced ceiling (30/min has been observed).  Both
states used to be invisible to the app: every caller kept retrying with
exponential backoff, so a dead endpoint was hammered by every scan, sync,
and refresh in the container.

This module records that state once, centrally, so that:

* callers fail fast instead of retrying against a known-dead endpoint,
* the dashboard can show how long the outage has lasted, and
* recovery is detected by a single cheap probe on a backoff schedule
  rather than by whichever job happens to run next.

The tracker holds only *facts* (last success, last failure, current server
rate limit).  :attr:`AniListHealth.state` is derived from those facts, so
there is no state machine to get out of sync.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

STATE_OK = "ok"
STATE_DEGRADED = "degraded"
STATE_DOWN = "down"

#: AniList's documented normal rate limit (requests/minute).  Anything the
#: server reports below this is treated as a deliberate reduction.
NORMAL_RATE_LIMIT = 90

#: How long a request is spaced out after each consecutive failed probe.
#: Index 0 is used for the first probe after an outage starts.
PROBE_BACKOFF_SECONDS = (30, 60, 120, 300, 600, 900)

#: A 429 keeps the app in "degraded" for this long after the last one, so a
#: burst of throttling is visible in the UI instead of flashing past.
THROTTLE_DEGRADE_WINDOW = 300.0

#: Response body fragments that mean "the API itself is off", not "your
#: request was bad".  Matched case-insensitively against the response body.
OUTAGE_BODY_MARKERS = (
    "temporarily disabled",
    "severe stability",
    "api has been disabled",
    "under maintenance",
    "temporarily unavailable",
    "service unavailable",
)


class AniListUnavailableError(RuntimeError):
    """Raised instead of issuing a request while AniList is known to be down.

    Callers that can degrade gracefully should catch this; it means "we did
    not talk to AniList at all", never "AniList said no".
    """

    def __init__(self, message: str, *, reason: str = "", down_seconds: float = 0.0):
        super().__init__(message)
        self.reason = reason
        self.down_seconds = down_seconds


def looks_like_outage(status_code: int, body: str) -> bool:
    """True if a response means the AniList API itself is unavailable.

    A 403 carrying one of :data:`OUTAGE_BODY_MARKERS` is AniList's own
    "API disabled" response.  A bare 403 is far more likely to be a bad
    token or a query that needs auth, so it is *not* treated as an outage
    here — the client escalates that only after its normal retries.
    """
    if status_code in (403, 503):
        haystack = (body or "").lower()
        return any(marker in haystack for marker in OUTAGE_BODY_MARKERS)
    return False


class AniListHealth:
    """Tracks AniList API availability and the current rate-limit ceiling.

    Thread-safety is not needed: everything runs on a single asyncio loop
    and all mutations are synchronous.
    """

    def __init__(self) -> None:
        self._down_since: float | None = None  # wall clock (epoch seconds)
        self._reason: str = ""
        self._detail: str = ""
        self._last_success: float | None = None
        self._last_failure: float | None = None
        self._last_429_at: float | None = None
        self._retry_after: int = 0
        self._reduced_limit: int | None = None
        self._consecutive_failures: int = 0
        self._outage_count: int = 0
        self._probe_index: int = 0
        self._next_probe_at: float | None = None  # monotonic
        self._last_probe_at: float | None = None  # wall clock
        # Bumped on every change worth persisting / notifying about, so the
        # monitor loop can detect transitions without diffing snapshots.
        self.version: int = 0

    # ------------------------------------------------------------------
    # Derived state
    # ------------------------------------------------------------------

    @property
    def state(self) -> str:
        if self._down_since is not None:
            return STATE_DOWN
        if self._reduced_limit is not None:
            return STATE_DEGRADED
        if (
            self._last_429_at is not None
            and time.time() - self._last_429_at < THROTTLE_DEGRADE_WINDOW
        ):
            return STATE_DEGRADED
        return STATE_OK

    @property
    def is_down(self) -> bool:
        return self._down_since is not None

    @property
    def is_degraded(self) -> bool:
        return self.state == STATE_DEGRADED

    @property
    def reason(self) -> str:
        if self._down_since is not None:
            return self._reason or "AniList API is unavailable"
        if self._reduced_limit is not None:
            return (
                f"AniList has reduced its rate limit to "
                f"{self._reduced_limit} requests/minute"
            )
        if self.state == STATE_DEGRADED:
            return "AniList is rate limiting requests"
        return ""

    @property
    def down_seconds(self) -> float:
        if self._down_since is None:
            return 0.0
        return max(0.0, time.time() - self._down_since)

    @property
    def rate_limit(self) -> int:
        return self._reduced_limit or NORMAL_RATE_LIMIT

    def seconds_until_probe(self) -> float:
        """Seconds until the next recovery probe (0 when one is due)."""
        if self._next_probe_at is None:
            return 0.0
        return max(0.0, self._next_probe_at - time.monotonic())

    def should_block(self) -> bool:
        """True if a normal request must not be sent right now."""
        return self._down_since is not None

    def is_probe_due(self) -> bool:
        """True if the outage has lasted long enough to retry once."""
        return self._down_since is not None and self.seconds_until_probe() <= 0.0

    def raise_if_down(self) -> None:
        """Raise :class:`AniListUnavailableError` while the API is down."""
        if self._down_since is None:
            return
        raise AniListUnavailableError(
            f"AniList API unavailable ({self.reason}); "
            f"down for {int(self.down_seconds)}s, "
            f"next check in {int(self.seconds_until_probe())}s",
            reason=self.reason,
            down_seconds=self.down_seconds,
        )

    # ------------------------------------------------------------------
    # Recording outcomes
    # ------------------------------------------------------------------

    def record_success(self) -> None:
        """Record a request that came back with usable data."""
        self._last_success = time.time()
        self._consecutive_failures = 0
        if self._down_since is not None:
            outage = self.down_seconds
            logger.info(
                "AniList API recovered after %.0fs of downtime (%s)",
                outage,
                self._reason or "unknown cause",
            )
            self._down_since = None
            self._reason = ""
            self._detail = ""
            self._probe_index = 0
            self._next_probe_at = None
            self.version += 1

    def record_outage(self, reason: str, detail: str = "") -> None:
        """Record that the API is unusable, opening the circuit.

        Repeated calls keep the original ``down_since`` so the UI shows
        total downtime. The probe interval only lengthens when a failure
        arrives *after* the scheduled probe time — otherwise a handful of
        requests already in flight when the outage began would all report
        it at once and fast-forward the backoff to its longest interval
        before the first probe was ever sent.
        """
        now = time.time()
        now_mono = time.monotonic()
        self._last_failure = now
        self._consecutive_failures += 1

        first = self._down_since is None
        if first:
            self._down_since = now
            self._outage_count += 1
            self._probe_index = 0
            logger.error(
                "AniList API marked DOWN: %s%s",
                reason,
                f" — {detail}" if detail else "",
            )
        elif self._next_probe_at is not None and now_mono < self._next_probe_at:
            # Still inside the current probe window — same outage, already
            # scheduled. Nothing to reschedule.
            changed = reason != self._reason
            self._reason = reason
            self._detail = detail
            if changed:
                self.version += 1
            return
        else:
            self._probe_index = min(
                self._probe_index + 1, len(PROBE_BACKOFF_SECONDS) - 1
            )

        changed = first or reason != self._reason
        self._reason = reason
        self._detail = detail
        wait = PROBE_BACKOFF_SECONDS[self._probe_index]
        self._next_probe_at = now_mono + wait
        logger.info("Next AniList recovery probe in %ds", wait)
        if changed:
            self.version += 1

    def record_rate_limit(self, limit: int) -> None:
        """Record the rate-limit ceiling AniList reported on a response."""
        if limit <= 0:
            return
        reduced = limit if limit < NORMAL_RATE_LIMIT else None
        if reduced != self._reduced_limit:
            if reduced is not None:
                logger.warning(
                    "AniList is running with a reduced rate limit: "
                    "%d req/min (normal is %d)",
                    reduced,
                    NORMAL_RATE_LIMIT,
                )
            else:
                logger.info(
                    "AniList rate limit back to normal (%d req/min)", NORMAL_RATE_LIMIT
                )
            self._reduced_limit = reduced
            self.version += 1

    def record_throttled(self, retry_after: int) -> None:
        """Record a 429 so the UI can show that AniList is throttling us."""
        was_degraded = self.state == STATE_DEGRADED
        self._last_429_at = time.time()
        self._retry_after = retry_after
        if not was_degraded:
            self.version += 1

    def record_probe_attempt(self) -> None:
        """Mark that a recovery probe is being sent now."""
        self._last_probe_at = time.time()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def restore(
        self,
        *,
        down_since: float | None,
        reason: str = "",
        detail: str = "",
        reduced_limit: int | None = None,
    ) -> None:
        """Rehydrate state saved before a restart.

        A container restart should not reset "down for 3 hours" back to
        zero — the outage is a property of AniList, not of this process.
        The probe schedule always restarts at the shortest interval so a
        restart re-checks promptly.
        """
        if down_since:
            self._down_since = float(down_since)
            self._reason = reason or "AniList API is unavailable"
            self._detail = detail
            self._probe_index = 0
            self._next_probe_at = time.monotonic() + PROBE_BACKOFF_SECONDS[0]
            logger.warning(
                "Restored AniList outage state from storage — down for %.0fs (%s)",
                self.down_seconds,
                self._reason,
            )
        if reduced_limit:
            self._reduced_limit = int(reduced_limit)

    def persist_payload(self) -> dict[str, object]:
        """The subset of state worth surviving a restart."""
        return {
            "down_since": self._down_since,
            "reason": self._reason,
            "detail": self._detail,
            "reduced_limit": self._reduced_limit,
            "saved_at": time.time(),
        }

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, object]:
        """A JSON-serialisable view of current health, for the API/UI."""
        return {
            "state": self.state,
            "reason": self.reason,
            "detail": self._detail,
            "blocked": self.should_block(),
            "down_since": self._down_since,
            "down_seconds": round(self.down_seconds),
            "next_probe_in": round(self.seconds_until_probe()),
            "last_probe_at": self._last_probe_at,
            "last_success": self._last_success,
            "last_failure": self._last_failure,
            "consecutive_failures": self._consecutive_failures,
            "outage_count": self._outage_count,
            "rate_limit": self.rate_limit,
            "normal_rate_limit": NORMAL_RATE_LIMIT,
            "reduced_limit": self._reduced_limit,
            "retry_after": self._retry_after,
            "version": self.version,
        }
