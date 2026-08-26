"""Assigning AniList entries to Sonarr seasons and episode ranges."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from src.Download.SeasonRangeMapper import (
    RebuildResult,
    assign_season_ranges,
    fetch_sonarr_seasons,
    persist_season_ranges,
)


def test_one_to_one_gives_each_entry_a_whole_season() -> None:
    season_map, ranges = assign_season_ranges(
        chain=[100, 200, 300],
        episode_counts={100: 12, 200: 12, 300: 12},
        sonarr_seasons=[1, 2, 3],
        sonarr_season_totals={1: 12, 2: 12, 3: 12},
    )
    assert season_map == {100: 1, 200: 2, 300: 3}
    assert ranges == {100: (1, None), 200: (1, None), 300: (1, None)}


def test_split_cour_shares_one_season_with_offset_ranges() -> None:
    """GATE: two AniList entries, one 24-episode Sonarr season."""
    season_map, ranges = assign_season_ranges(
        chain=[20994, 21364],
        episode_counts={20994: 12, 21364: 12},
        sonarr_seasons=[1],
        sonarr_season_totals={1: 24},
    )
    assert season_map == {20994: 1, 21364: 1}
    assert ranges == {20994: (1, 12), 21364: (13, 24)}


def test_ranges_restart_at_one_in_the_next_season() -> None:
    """Episode numbers are per-season, which is how Sonarr labels them."""
    season_map, ranges = assign_season_ranges(
        chain=[1, 2, 3],
        episode_counts={1: 12, 2: 12, 3: 13},
        sonarr_seasons=[1, 2],
        sonarr_season_totals={1: 24, 2: 13},
    )
    assert season_map == {1: 1, 2: 1, 3: 2}
    assert ranges == {1: (1, 12), 2: (13, 24), 3: (1, 13)}


def test_no_sonarr_seasons_assigns_nothing() -> None:
    season_map, _ = assign_season_ranges([1, 2], {1: 12, 2: 12}, [], {})
    assert season_map == {1: None, 2: None}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _recording_db() -> MagicMock:
    db = MagicMock()
    db.calls = []

    async def execute(query: str, params: tuple = ()) -> None:
        db.calls.append((query.strip().split()[0].upper(), params))

    db.execute = execute
    return db


@pytest.mark.asyncio
async def test_persist_replaces_the_series_wholesale() -> None:
    """A re-run may split a season stored as one row; the old row must go."""
    db = _recording_db()
    written = await persist_season_ranges(
        db,
        272,
        {20994: 1, 21364: 1},
        {20994: (1, 12), 21364: (13, 24)},
        [20994, 21364],
    )
    assert written == 2
    assert db.calls[0][0] == "DELETE"
    assert db.calls[0][1] == (272,)
    assert [c[1] for c in db.calls[1:]] == [
        (272, 1, 20994, 1, 12),
        (272, 1, 21364, 13, 24),
    ]


@pytest.mark.asyncio
async def test_persist_keeps_existing_rows_when_it_has_no_answer() -> None:
    """An unusable assignment must not destroy a usable stored one."""
    db = _recording_db()
    written = await persist_season_ranges(db, 272, {1: None, 2: None}, {}, [1, 2])
    assert written == 0
    assert db.calls == []


# ---------------------------------------------------------------------------
# Reading Sonarr's season list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_specials_are_not_part_of_the_chronology() -> None:
    client = MagicMock()

    async def get_series_by_id(sid: int) -> dict[str, Any]:
        return {
            "seasons": [
                {"seasonNumber": 0, "statistics": {"totalEpisodeCount": 4}},
                {"seasonNumber": 2, "statistics": {"totalEpisodeCount": 13}},
                {"seasonNumber": 1, "statistics": {"totalEpisodeCount": 24}},
            ]
        }

    client.get_series_by_id = get_series_by_id
    seasons, totals = await fetch_sonarr_seasons(client, 272)

    assert seasons == [1, 2]
    assert totals == {1: 24, 2: 13}


# ---------------------------------------------------------------------------
# Saying why a rebuild found nothing
# ---------------------------------------------------------------------------


def test_detail_names_every_input_it_had() -> None:
    """The message has to be enough to diagnose from, without a log dive."""
    r = RebuildResult(
        tvdb_id=110811,
        chain=[20994, 21364],
        sonarr_seasons=[1],
        season_totals={1: 24},
        missing_counts=[21364],
    )
    detail = r.detail()
    assert "tvdb=110811" in detail
    assert "chain=2" in detail
    assert "Sonarr seasons=[1]" in detail
    assert "no episode count for [21364]" in detail


def test_detail_is_readable_when_nothing_resolved() -> None:
    detail = RebuildResult().detail()
    assert "tvdb=none" in detail
    assert "Sonarr seasons=none" in detail


# ---------------------------------------------------------------------------
# An unaired sequel in the chain
# ---------------------------------------------------------------------------


def test_unaired_sequel_does_not_block_the_entries_before_it() -> None:
    """GATE: two aired cours in one Sonarr season, plus an announced third.

    The third has no episode count on AniList yet, and Sonarr has no season for
    it. That must not cost the two entries that place perfectly well.
    """
    season_map, ranges = assign_season_ranges(
        chain=[20994, 21364, 195496],
        episode_counts={20994: 12, 21364: 12, 195496: None},
        sonarr_seasons=[1],
        sonarr_season_totals={1: 24},
    )
    assert season_map == {20994: 1, 21364: 1, 195496: None}
    assert ranges == {20994: (1, 12), 21364: (13, 24)}


def test_an_unknown_count_stops_everything_after_it() -> None:
    """The unknown entry's length is what the next one's start is measured
    from, so past it we are guessing — and a guess files the wrong episodes."""
    season_map, ranges = assign_season_ranges(
        chain=[1, 2, 3],
        episode_counts={1: 12, 2: None, 3: 12},
        sonarr_seasons=[1, 2],
        sonarr_season_totals={1: 24, 2: 24},
    )
    assert season_map[1] == 1
    assert ranges[1] == (1, 12)
    # Entry 2 still gets placed — its start is known, only its end is not.
    assert season_map[2] == 1
    assert ranges[2] == (13, None)
    # Entry 3 is past the point where the offset is knowable.
    assert season_map[3] is None
    assert 3 not in ranges


def test_no_count_on_the_first_entry_still_assigns_nothing() -> None:
    """Without a length for entry one there is no origin to measure from."""
    season_map, ranges = assign_season_ranges(
        chain=[1, 2],
        episode_counts={1: None, 2: 12},
        sonarr_seasons=[1],
        sonarr_season_totals={1: 24},
    )
    assert season_map == {1: None, 2: None}
    assert ranges == {}
