"""Regression tests for Crunchyroll season → AniList entry mapping.

Crunchyroll numbers a franchise's seasons the way AniList labels them — by
"Nth Season", not by cour. A "Part 2" cour belongs to its parent season rather
than taking a season number of its own. The expectations below are pinned to
real observed behaviour:

* **Re:Zero** — the 2026-09-06 sync log recorded
  ``Re:ZERO -Starting Life in Another World- S4E15`` resolving to AniList
  ``189046`` (``Re:Zero … 4th Season``), even though the franchise has five
  cours. That proves CR's season 4 is AniList's *4th Season* and that the
  "2nd Season Part 2" cour must not consume a season slot.
* **Mushoku Tensei** — the same rule puts CR season 3 on ``178789``
  (``Mushoku Tensei III``), which is the entry the watch syncer previously
  never reached.

Both franchises are covered together because a change that fixes one by
renumbering slots breaks the other.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.Matching.Normalizer import extract_base_series_title
from src.Matching.TitleMatcher import (
    TitleMatcher,
    _detect_season_from_anilist_entry,
)


def _entry(
    id: int,
    romaji: str,
    english: str = "",
    episodes: int | None = 12,
    year: int = 2021,
    month: int = 1,
    format: str = "TV",
) -> dict[str, Any]:
    return {
        "id": id,
        "title": {"romaji": romaji, "english": english, "native": ""},
        "synonyms": [],
        "format": format,
        "episodes": episodes,
        "startDate": {"year": year, "month": month, "day": 1},
    }


MUSHOKU_TITLE = "Mushoku Tensei: Jobless Reincarnation"
REZERO_TITLE = "Re:ZERO -Starting Life in Another World-"


@pytest.fixture
def mushoku_results() -> list[dict[str, Any]]:
    """Real AniList entries for the Mushoku Tensei franchise."""
    return [
        _entry(
            108465,
            "Mushoku Tensei: Isekai Ittara Honki Dasu",
            "Mushoku Tensei: Jobless Reincarnation",
            11,
            2021,
            1,
        ),
        _entry(
            127720,
            "Mushoku Tensei: Isekai Ittara Honki Dasu Part 2",
            "Mushoku Tensei: Jobless Reincarnation Part 2",
            12,
            2021,
            10,
        ),
        _entry(
            146065,
            "Mushoku Tensei II: Isekai Ittara Honki Dasu",
            "Mushoku Tensei: Jobless Reincarnation Season 2",
            12,
            2023,
            7,
        ),
        _entry(
            166873,
            "Mushoku Tensei II: Isekai Ittara Honki Dasu Part 2",
            "Mushoku Tensei: Jobless Reincarnation Season 2 Part 2",
            12,
            2024,
            4,
        ),
        _entry(
            178789,
            "Mushoku Tensei III: Isekai Ittara Honki Dasu",
            "Mushoku Tensei: Jobless Reincarnation Season 3",
            None,  # currently airing
            2026,
            7,
        ),
        # The franchise SPECIAL is in the real results and must stay excluded.
        _entry(
            141534,
            "Mushoku Tensei: Isekai Ittara Honki Dasu Part 2 - Eris no Goblin "
            "Toubatsu",
            episodes=1,
            year=2022,
            month=3,
            format="SPECIAL",
        ),
    ]


@pytest.fixture
def rezero_results() -> list[dict[str, Any]]:
    """Real AniList entries for the Re:Zero franchise."""
    return [
        _entry(
            21355,
            "Re:Zero kara Hajimeru Isekai Seikatsu",
            "Re:ZERO -Starting Life in Another World-",
            25,
            2016,
            4,
        ),
        _entry(
            108632,
            "Re:Zero kara Hajimeru Isekai Seikatsu 2nd Season",
            "Re:ZERO -Starting Life in Another World- Season 2",
            13,
            2020,
            7,
        ),
        _entry(
            119661,
            "Re:Zero kara Hajimeru Isekai Seikatsu 2nd Season Part 2",
            "Re:ZERO -Starting Life in Another World- Season 2 Part 2",
            12,
            2021,
            1,
        ),
        _entry(
            163134,
            "Re:Zero kara Hajimeru Isekai Seikatsu 3rd Season",
            "Re:ZERO -Starting Life in Another World- Season 3",
            16,
            2024,
            10,
        ),
        _entry(
            189046,
            "Re:Zero kara Hajimeru Isekai Seikatsu 4th Season",
            "Re:ZERO -Starting Life in Another World- Season 4",
            19,
            2026,
            4,
        ),
    ]


# ----------------------------------------------------------------------
# Grouping: a Roman numeral before a subtitle must not split the franchise
# ----------------------------------------------------------------------


class TestFranchiseGrouping:
    @pytest.mark.parametrize(
        "title",
        [
            "Mushoku Tensei: Isekai Ittara Honki Dasu",
            "Mushoku Tensei: Isekai Ittara Honki Dasu Part 2",
            "Mushoku Tensei II: Isekai Ittara Honki Dasu",
            "Mushoku Tensei II: Isekai Ittara Honki Dasu Part 2",
            "Mushoku Tensei III: Isekai Ittara Honki Dasu",
        ],
    )
    def test_every_entry_shares_one_base_group(self, title: str) -> None:
        """Regression: "Mushoku Tensei III: …" used to group as
        "Mushoku Tensei III", which excluded the airing season from the
        franchise's primary group entirely.
        """
        assert extract_base_series_title(title) == "Mushoku Tensei"

    def test_unrelated_colon_titles_unaffected(self) -> None:
        """The colon-aware Roman-numeral strip must not over-strip."""
        assert (
            extract_base_series_title("Jujutsu Kaisen: Shimetsu Kaiyuu")
            == "Jujutsu Kaisen"
        )
        assert extract_base_series_title("Re:Zero kara Hajimeru Isekai Seikatsu") == (
            "Re:Zero kara Hajimeru Isekai Seikatsu"
        )
        assert extract_base_series_title("Steins;Gate 0") == "Steins;Gate 0"


# ----------------------------------------------------------------------
# Season detection: a Roman numeral outranks "Part N"
# ----------------------------------------------------------------------


class TestSeasonDetection:
    def test_part_two_of_third_season_is_season_three(self) -> None:
        """Regression: with "Part N" checked first this returned 2."""
        assert (
            _detect_season_from_anilist_entry(
                _entry(0, "Mushoku Tensei III: Isekai Ittara Honki Dasu Part 2"),
                "Mushoku Tensei",
            )
            == 3
        )

    def test_roman_numeral_entry_is_its_own_season(self) -> None:
        assert (
            _detect_season_from_anilist_entry(
                _entry(178789, "Mushoku Tensei III: Isekai Ittara Honki Dasu"),
                "Mushoku Tensei",
            )
            == 3
        )

    def test_part_without_roman_numeral_still_detected(self) -> None:
        assert (
            _detect_season_from_anilist_entry(
                _entry(127720, "Mushoku Tensei: Isekai Ittara Honki Dasu Part 2"),
                "Mushoku Tensei",
            )
            == 2
        )

    def test_ordinal_season_still_detected(self) -> None:
        assert (
            _detect_season_from_anilist_entry(
                _entry(0, "Kaijuu 8-gou 2nd Season"), "Kaijuu 8-gou"
            )
            == 2
        )


# ----------------------------------------------------------------------
# Mushoku Tensei — the airing season must be reachable
# ----------------------------------------------------------------------


class TestMushokuMapping:
    def test_airing_season_is_in_the_map(self, mushoku_results) -> None:
        """Regression: the map used to stop at season 2, so CR season 3 had
        nowhere to go and was folded onto season 1.
        """
        structure = TitleMatcher().build_season_structure(
            mushoku_results, MUSHOKU_TITLE
        )
        assert 3 in structure
        assert structure[3]["id"] == 178789

    def test_season_three_maps_to_the_airing_entry(self, mushoku_results) -> None:
        """The exact regression: CR season 3 must not write to 108465."""
        structure = TitleMatcher().build_season_structure(
            mushoku_results, MUSHOKU_TITLE
        )
        entry, season, episode = TitleMatcher.determine_correct_entry_and_episode(
            MUSHOKU_TITLE, cr_season=3, cr_episode=10, season_structure=structure
        )
        assert entry is not None
        assert entry["id"] == 178789
        assert (season, episode) == (3, 10)

    def test_airing_season_episode_is_not_capped(self, mushoku_results) -> None:
        """The airing entry has no episode count; the episode passes through."""
        structure = TitleMatcher().build_season_structure(
            mushoku_results, MUSHOKU_TITLE
        )
        entry, _, episode = TitleMatcher.determine_correct_entry_and_episode(
            MUSHOKU_TITLE, cr_season=3, cr_episode=11, season_structure=structure
        )
        assert entry is not None and entry["id"] == 178789
        assert episode == 11

    def test_season_one_still_maps_to_season_one(self, mushoku_results) -> None:
        structure = TitleMatcher().build_season_structure(
            mushoku_results, MUSHOKU_TITLE
        )
        entry, season, episode = TitleMatcher.determine_correct_entry_and_episode(
            MUSHOKU_TITLE, cr_season=1, cr_episode=5, season_structure=structure
        )
        assert entry is not None
        assert entry["id"] == 108465
        assert (season, episode) == (1, 5)

    def test_special_is_excluded(self, mushoku_results) -> None:
        structure = TitleMatcher().build_season_structure(
            mushoku_results, MUSHOKU_TITLE
        )
        assert 141534 not in {sd["id"] for sd in structure.values()}


# ----------------------------------------------------------------------
# Re:Zero — must keep working; a renumbering fix for Mushoku broke this
# ----------------------------------------------------------------------


class TestRezeroMapping:
    def test_cr_season_four_maps_to_fourth_season_entry(self, rezero_results) -> None:
        """Pinned to the 2026-09-06 log: CR S4E15 resolved to AniList 189046.

        Giving the "2nd Season Part 2" cour its own season slot shifts every
        later season down one and sends CR season 4 to the 3rd Season entry
        (163134). Cours must stay folded into their parent season.
        """
        structure = TitleMatcher().build_season_structure(rezero_results, REZERO_TITLE)
        entry, season, episode = TitleMatcher.determine_correct_entry_and_episode(
            REZERO_TITLE, cr_season=4, cr_episode=15, season_structure=structure
        )
        assert entry is not None
        assert entry["id"] == 189046
        assert (season, episode) == (4, 15)

    def test_cour_does_not_consume_a_season_slot(self, rezero_results) -> None:
        """The five cours must occupy four season slots, not five."""
        structure = TitleMatcher().build_season_structure(rezero_results, REZERO_TITLE)
        assert sorted(structure) == [1, 2, 3, 4]
        assert [structure[sn]["id"] for sn in (1, 2, 3, 4)] == [
            21355,
            108632,
            163134,
            189046,
        ]


# ----------------------------------------------------------------------
# Guardrail: an unknown CR season must never be folded onto season 1
# ----------------------------------------------------------------------


class TestUnknownSeasonGuardrail:
    def test_mushoku_unknown_season_is_refused(self, mushoku_results) -> None:
        structure = TitleMatcher().build_season_structure(
            mushoku_results, MUSHOKU_TITLE
        )
        assert TitleMatcher.determine_correct_entry_and_episode(
            MUSHOKU_TITLE, cr_season=9, cr_episode=4, season_structure=structure
        ) == (None, 0, 0)

    def test_rezero_unknown_season_is_refused(self, rezero_results) -> None:
        """Re:Zero season 1 is normally COMPLETED, so the old fallback skipped
        silently — no AniList write and no history row to notice.
        """
        structure = TitleMatcher().build_season_structure(rezero_results, REZERO_TITLE)
        assert TitleMatcher.determine_correct_entry_and_episode(
            REZERO_TITLE, cr_season=9, cr_episode=3, season_structure=structure
        ) == (None, 0, 0)
