"""Tests for the mis-mapped-write detector.

The Mushoku Tensei case from the 2026-09-06 log is the reference: Crunchyroll
season 3 episode 10 was written to AniList 108465 (season 1, 11 episodes) because
the season map stopped at season 2.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.Sync.CrunchyrollRepair import CorrectTarget, find_suspect_writes

MUSHOKU = "Mushoku Tensei: Jobless Reincarnation"
REZERO = "Re:ZERO -Starting Life in Another World-"

# 108465 S1c1, 127720 S1c2, 146065 S2c1, 166873 S2c2, 178789 S3
MUSHOKU_IDS = {108465, 127720, 146065, 166873, 178789}
REZERO_IDS = {21355, 108632, 119661, 163134, 189046}

GROUPS: dict[int, set[int]] = {
    **{i: MUSHOKU_IDS for i in MUSHOKU_IDS},
    **{i: REZERO_IDS for i in REZERO_IDS},
}


def _log(
    log_id: int,
    anilist_id: int,
    after_progress: int,
    *,
    show_title: str = "Show",
    after_status: str = "CURRENT",
    before_progress: int = 0,
    undone_at: str | None = None,
) -> dict[str, Any]:
    return {
        "id": log_id,
        "anilist_id": anilist_id,
        "show_title": show_title,
        "after_progress": after_progress,
        "after_status": after_status,
        "before_progress": before_progress,
        "before_status": "CURRENT",
        "applied_at": "2026-09-06 02:00:34",
        "undone_at": undone_at,
    }


class TestMushokuRegression:
    def test_season_three_written_to_season_one_is_flagged(self) -> None:
        targets = {(MUSHOKU, 3): CorrectTarget(178789, 10)}
        rows = [_log(1, 108465, 10, show_title="Mushoku Tensei")]

        suspects = find_suspect_writes(targets, GROUPS, rows)

        assert len(suspects) == 1
        assert suspects[0].log_id == 1
        assert suspects[0].anilist_id == 108465
        assert suspects[0].correct_anilist_id == 178789
        assert suspects[0].correct_episode == 10
        assert suspects[0].cr_season == 3

    def test_reason_names_both_entries(self) -> None:
        targets = {(MUSHOKU, 3): CorrectTarget(178789, 10)}
        suspects = find_suspect_writes(targets, GROUPS, [_log(1, 108465, 10)])
        assert "178789" in suspects[0].reason
        assert "108465" in suspects[0].reason
        assert "season 3" in suspects[0].reason

    def test_as_dict_round_trips_the_fields_the_ui_needs(self) -> None:
        targets = {(MUSHOKU, 3): CorrectTarget(178789, 10)}
        row = find_suspect_writes(targets, GROUPS, [_log(1, 108465, 10)])[0].as_dict()
        for key in (
            "log_id",
            "anilist_id",
            "show_title",
            "after_progress",
            "correct_anilist_id",
            "correct_episode",
            "reason",
        ):
            assert key in row


class TestNoFalsePositives:
    def test_correct_write_is_not_flagged(self) -> None:
        """The entry that should have received the write is left alone."""
        targets = {(MUSHOKU, 3): CorrectTarget(178789, 10)}
        rows = [_log(1, 178789, 10)]
        assert find_suspect_writes(targets, GROUPS, rows) == []

    def test_unrelated_franchise_is_not_flagged(self) -> None:
        """A different show at the same episode number must not match."""
        targets = {(MUSHOKU, 3): CorrectTarget(178789, 10)}
        rows = [_log(1, 21355, 10)]  # Re:Zero season 1
        assert find_suspect_writes(targets, GROUPS, rows) == []

    def test_entry_outside_any_known_group_is_not_flagged(self) -> None:
        targets = {(MUSHOKU, 3): CorrectTarget(178789, 10)}
        rows = [_log(1, 999999, 10)]
        assert find_suspect_writes(targets, GROUPS, rows) == []

    def test_different_episode_number_is_not_flagged(self) -> None:
        """A genuine season-1 rewatch at a different episode stays untouched.

        This is the check that keeps the detector honest: being in the same
        franchise is not enough, the episode number has to match too.
        """
        targets = {(MUSHOKU, 3): CorrectTarget(178789, 10)}
        rows = [_log(1, 108465, 4)]
        assert find_suspect_writes(targets, GROUPS, rows) == []

    def test_already_undone_row_is_ignored(self) -> None:
        targets = {(MUSHOKU, 3): CorrectTarget(178789, 10)}
        rows = [_log(1, 108465, 10, undone_at="2026-09-10 12:00:00")]
        assert find_suspect_writes(targets, GROUPS, rows) == []

    def test_no_targets_means_nothing_to_check(self) -> None:
        assert find_suspect_writes({}, GROUPS, [_log(1, 108465, 10)]) == []

    def test_no_log_rows_means_nothing_to_report(self) -> None:
        targets = {(MUSHOKU, 3): CorrectTarget(178789, 10)}
        assert find_suspect_writes(targets, GROUPS, []) == []

    def test_row_without_an_id_is_skipped(self) -> None:
        targets = {(MUSHOKU, 3): CorrectTarget(178789, 10)}
        rows = [_log(0, 108465, 10)]
        assert find_suspect_writes(targets, GROUPS, rows) == []


class TestMultipleFranchises:
    def test_each_franchise_is_judged_independently(self) -> None:
        targets = {
            (MUSHOKU, 3): CorrectTarget(178789, 10),
            (REZERO, 5): CorrectTarget(189046, 3),
        }
        rows = [
            _log(1, 108465, 10, show_title="Mushoku Tensei"),
            _log(2, 21355, 3, show_title="Re:Zero"),
            _log(3, 163134, 9, show_title="Re:Zero S3"),  # legitimate
        ]
        suspects = find_suspect_writes(targets, GROUPS, rows)
        assert {s.log_id for s in suspects} == {1, 2}

    def test_a_row_is_reported_once_across_several_targets(self) -> None:
        """Two watched seasons landing on the same wrong row yields one report."""
        targets = {
            (MUSHOKU, 3): CorrectTarget(178789, 10),
            (MUSHOKU, 4): CorrectTarget(178789, 10),
        }
        rows = [_log(1, 108465, 10)]
        assert len(find_suspect_writes(targets, GROUPS, rows)) == 1

    def test_results_are_ordered_for_stable_display(self) -> None:
        targets = {
            (REZERO, 5): CorrectTarget(189046, 3),
            (MUSHOKU, 3): CorrectTarget(178789, 10),
        }
        rows = [_log(9, 21355, 3), _log(1, 108465, 10)]
        suspects = find_suspect_writes(targets, GROUPS, rows)
        assert [s.series_title for s in suspects] == [MUSHOKU, REZERO]

    def test_missing_group_membership_falls_back_to_the_entry_itself(self) -> None:
        """With no group data, only an exact-entry match could apply — so none."""
        targets = {(MUSHOKU, 3): CorrectTarget(178789, 10)}
        assert find_suspect_writes(targets, {}, [_log(1, 108465, 10)]) == []


@pytest.mark.parametrize("progress", [0, 1, 9, 11, 23])
def test_only_the_exact_episode_matches(progress: int) -> None:
    targets = {(MUSHOKU, 3): CorrectTarget(178789, 10)}
    rows = [_log(1, 108465, progress)]
    assert find_suspect_writes(targets, GROUPS, rows) == []
