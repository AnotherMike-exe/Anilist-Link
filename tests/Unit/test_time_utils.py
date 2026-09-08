"""Tests for UTC -> configured-timezone rendering of stored timestamps."""

from datetime import datetime, timedelta, timezone

import pytest

from src.Utils.Time import get_timezone, now_local, parse_utc, to_local, to_local_date


class TestGetTimezone:
    def test_named_zone(self):
        tz = get_timezone("America/Los_Angeles")
        assert str(tz) == "America/Los_Angeles"

    def test_reads_tz_env_var(self, monkeypatch):
        monkeypatch.setenv("TZ", "America/New_York")
        assert str(get_timezone()) == "America/New_York"

    def test_unknown_zone_falls_back(self):
        # Must not raise — an operator typo should degrade, not crash the UI.
        assert get_timezone("Not/AZone") is not None

    def test_empty_env_falls_back(self, monkeypatch):
        monkeypatch.delenv("TZ", raising=False)
        assert get_timezone() is not None


class TestParseUtc:
    @pytest.mark.parametrize(
        "raw",
        [
            "2026-08-24 09:00:00",  # SQLite datetime('now')
            "2026-08-24T09:00:00",  # ISO with T
            "2026-08-24T09:00:00Z",
            "2026-08-24T09:00:00+00:00",
            "2026-08-24 09:00:00.123456",
        ],
    )
    def test_parses_utc_forms(self, raw):
        parsed = parse_utc(raw)
        assert parsed is not None
        assert parsed.astimezone(timezone.utc).replace(microsecond=0) == datetime(
            2026, 8, 24, 9, 0, 0, tzinfo=timezone.utc
        )

    def test_naive_values_are_treated_as_utc(self):
        assert parse_utc("2026-08-24 09:00:00").tzinfo == timezone.utc

    def test_existing_offset_is_respected(self):
        parsed = parse_utc("2026-08-24T09:00:00-07:00")
        assert parsed.utcoffset() == timedelta(hours=-7)

    @pytest.mark.parametrize("raw", [None, "", "   ", "not a date"])
    def test_unparseable(self, raw):
        assert parse_utc(raw) is None


class TestToLocal:
    def test_utc_converted_to_configured_zone(self):
        # The reported bug: a 02:00 local run stored as 09:00 UTC must render
        # as 02:00, not as an hour that has not happened yet.
        assert (
            to_local("2026-08-24 09:00:00", tz_name="America/Los_Angeles")
            == "2026-08-24 02:00:00"
        )

    def test_conversion_can_cross_the_date_boundary(self):
        assert (
            to_local("2026-08-24 02:00:00", tz_name="America/Los_Angeles")
            == "2026-08-23 19:00:00"
        )

    def test_respects_dst(self):
        # PST (-08:00) in January, PDT (-07:00) in August.
        assert (
            to_local("2026-01-24 09:00:00", tz_name="America/Los_Angeles")
            == "2026-01-24 01:00:00"
        )

    def test_utc_zone_is_a_no_op(self):
        assert to_local("2026-08-24 09:00:00", tz_name="UTC") == "2026-08-24 09:00:00"

    def test_iso_input(self):
        assert (
            to_local("2026-08-24T09:00:00Z", tz_name="America/New_York")
            == "2026-08-24 05:00:00"
        )

    def test_truncating_to_16_chars_still_reads_as_local(self):
        # Templates render `(value | localtime)[:16]`.
        assert (
            to_local("2026-08-24 09:00:00", tz_name="America/Los_Angeles")[:16]
            == "2026-08-24 02:00"
        )

    def test_none_renders_empty(self):
        assert to_local(None) == ""

    def test_unparseable_passes_through(self):
        assert to_local("garbage", tz_name="UTC") == "garbage"

    def test_accepts_datetime(self):
        dt = datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc)
        assert to_local(dt, tz_name="America/Los_Angeles") == "2026-08-24 02:00:00"

    def test_custom_format(self):
        assert (
            to_local("2026-08-24 09:00:00", fmt="%H:%M", tz_name="America/Los_Angeles")
            == "02:00"
        )


class TestToLocalDate:
    def test_date_only(self):
        assert to_local_date("2026-08-24 09:00:00", tz_name="UTC") == "2026-08-24"

    def test_date_shifts_with_zone(self):
        assert (
            to_local_date("2026-08-24 02:00:00", tz_name="America/Los_Angeles")
            == "2026-08-23"
        )

    def test_none_renders_empty(self):
        assert to_local_date(None) == ""


class TestNowLocal:
    def test_is_aware_and_in_configured_zone(self):
        now = now_local("America/Los_Angeles")
        assert now.tzinfo is not None
        assert now.utcoffset() in (timedelta(hours=-7), timedelta(hours=-8))


class TestSchedulerTimezone:
    """The cron trigger must fire in the configured zone, not the system one."""

    def test_cron_trigger_uses_configured_zone(self, monkeypatch):
        from datetime import datetime

        from src.Scheduler.Jobs import _cr_trigger
        from src.Utils.Config import SchedulerConfig

        monkeypatch.setenv("TZ", "UTC")  # deliberately not the display zone
        tz = get_timezone("America/Los_Angeles")
        trigger = _cr_trigger(SchedulerConfig(cr_sync_time="02:00"), tz)

        nxt = trigger.get_next_fire_time(None, datetime(2026, 8, 24, 12, 0, tzinfo=tz))
        assert nxt.hour == 2
        assert nxt.utcoffset() == timedelta(hours=-7)
        # 02:00 PDT is 09:00 UTC — the timestamp the bug report saw rendered raw.
        assert nxt.astimezone(timezone.utc).hour == 9

    def test_scheduler_resolves_timezone_from_argument(self):
        from src.Scheduler.Jobs import JobScheduler
        from src.Utils.Config import SchedulerConfig

        scheduler = JobScheduler(SchedulerConfig(), timezone="America/Los_Angeles")
        assert str(scheduler._timezone) == "America/Los_Angeles"
