"""Regression tests for the Mushoku Tensei Crunchyroll→AniList mis-mapping.

Crunchyroll season 3 progress was being written onto AniList entry 108465 —
Mushoku Tensei season 1, 11 episodes — because the season map the watch syncer
builds only ever contained two seasons. Two independent defects caused that:

1. ``extract_base_series_title`` left the Roman numeral attached when a
   subtitle followed it ("Mushoku Tensei II: Isekai Ittara Honki Dasu" →
   "Mushoku Tensei II"), so the sequels landed in a different series group from
   the season 1 entries and were dropped when the primary group was picked.
2. ``_detect_season_from_anilist_entry`` read "Part N" before the Roman
   numeral, so every cour-2 entry in the franchise claimed season 2, and
   ``build_season_structure`` silently discarded the loser of each collision.

The titles, AniList IDs, episode counts and air dates below are the real ones.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.Matching.Normalizer import extract_base_series_title
from src.Matching.TitleMatcher import (
    TitleMatcher,
    _detect_season_from_anilist_entry,
)

CR_TITLE = "Mushoku Tensei: Jobless Reincarnation"

# (anilist_id, romaji, english, episodes, year, month)
MUSHOKU_ENTRIES: list[tuple[int, str, str, int | None, int, int]] = [
    (
        108465,
        "Mushoku Tensei: Isekai Ittara Honki Dasu",
        "Mushoku Tensei: Jobless Reincarnation",
        11,
        2021,
        1,
    ),
    (
        127720,
        "Mushoku Tensei: Isekai Ittara Honki Dasu Part 2",
        "Mushoku Tensei: Jobless Reincarnation Part 2",
        12,
        2021,
        10,
    ),
    (
        146065,
        "Mushoku Tensei II: Isekai Ittara Honki Dasu",
        "Mushoku Tensei: Jobless Reincarnation Season 2",
        12,
        2023,
        7,
    ),
    (
        166873,
        "Mushoku Tensei II: Isekai Ittara Honki Dasu Part 2",
        "Mushoku Tensei: Jobless Reincarnation Season 2 Part 2",
        12,
        2024,
        4,
    ),
    (
        178789,
        "Mushoku Tensei III: Isekai Ittara Honki Dasu",
        "Mushoku Tensei: Jobless Reincarnation Season 3",
        None,  # currently airing
        2026,
        7,
    ),
]


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


@pytest.fixture
def mushoku_search_results() -> list[dict[str, Any]]:
    """The AniList search results the watch syncer sees for the CR title."""
    results = [
        _entry(i, romaji, english, eps, year, month)
        for i, romaji, english, eps, year, month in MUSHOKU_ENTRIES
    ]
    # The franchise SPECIAL is in the real results and must stay excluded.
    results.append(
        _entry(
            141534,
            "Mushoku Tensei: Isekai Ittara Honki Dasu Part 2 - Eris no Goblin "
            "Toubatsu",
            episodes=1,
            year=2022,
            month=3,
            format="SPECIAL",
        )
    )
    return results


@pytest.fixture
def mushoku_structure(mushoku_search_results) -> dict[int, dict[str, Any]]:
    return TitleMatcher().build_season_structure(mushoku_search_results, CR_TITLE)


# ----------------------------------------------------------------------
# Defect 1 — series grouping
# ----------------------------------------------------------------------


class TestFranchiseGrouping:
    @pytest.mark.parametrize(
        "title",
        [romaji for _, romaji, _, _, _, _ in MUSHOKU_ENTRIES],
    )
    def test_every_entry_shares_one_base_group(self, title: str) -> None:
        """A Roman numeral before a subtitle must not split the franchise."""
        assert extract_base_series_title(title) == "Mushoku Tensei"

    def test_roman_numeral_stripped_before_colon_split(self) -> None:
        assert (
            extract_base_series_title("Mushoku Tensei III: Isekai Ittara Honki Dasu")
            == "Mushoku Tensei"
        )

    def test_unrelated_colon_titles_unaffected(self) -> None:
        """The lookahead must not over-strip ordinary subtitled titles."""
        assert (
            extract_base_series_title("Jujutsu Kaisen: Shimetsu Kaiyuu")
            == "Jujutsu Kaisen"
        )
        assert extract_base_series_title("Re:Zero kara Hajimeru Isekai Seikatsu") == (
            "Re:Zero kara Hajimeru Isekai Seikatsu"
        )


# ----------------------------------------------------------------------
# Defect 2 — season detection
# ----------------------------------------------------------------------


class TestSeasonDetection:
    def test_roman_numeral_outranks_part(self) -> None:
        """ "Mushoku Tensei II … Part 2" is season 2, not "Part 2" → season 2.

        The value coincides here; the ordering matters for season 3 onward.
        """
        assert (
            _detect_season_from_anilist_entry(
                _entry(166873, "Mushoku Tensei II: Isekai Ittara Honki Dasu Part 2"),
                "Mushoku Tensei",
            )
            == 2
        )

    def test_part_two_of_third_season_is_season_three(self) -> None:
        """Regression: with "Part N" checked first this returned 2."""
        assert (
            _detect_season_from_anilist_entry(
                _entry(0, "Mushoku Tensei III: Isekai Ittara Honki Dasu Part 2"),
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
# The season map the watch syncer builds
# ----------------------------------------------------------------------


class TestMushokuSeasonStructure:
    def test_all_five_cours_are_present(self, mushoku_structure) -> None:
        """Before the fix this contained only seasons 1 and 2."""
        assert sorted(mushoku_structure) == [1, 2, 3, 4, 5]

    def test_seasons_map_to_the_right_anilist_ids(self, mushoku_structure) -> None:
        assert [mushoku_structure[sn]["id"] for sn in sorted(mushoku_structure)] == [
            108465,
            127720,
            146065,
            166873,
            178789,
        ]

    def test_slots_stay_in_release_order(self, mushoku_structure) -> None:
        orders = [
            mushoku_structure[sn]["release_order"] for sn in sorted(mushoku_structure)
        ]
        assert orders == sorted(orders)

    def test_special_is_excluded(self, mushoku_structure) -> None:
        assert 141534 not in {sd["id"] for sd in mushoku_structure.values()}


# ----------------------------------------------------------------------
# End-to-end: the CR season/episode the log captured
# ----------------------------------------------------------------------


class TestMushokuEpisodeMapping:
    def test_season_three_no_longer_lands_on_season_one(
        self, mushoku_structure
    ) -> None:
        """The exact regression: CR's newest season must not write to 108465."""
        entry, season, episode = TitleMatcher.determine_correct_entry_and_episode(
            CR_TITLE, cr_season=5, cr_episode=10, season_structure=mushoku_structure
        )
        assert entry is not None
        assert entry["id"] == 178789
        assert (season, episode) == (5, 10)

    def test_airing_season_episode_is_not_capped(self, mushoku_structure) -> None:
        """The airing season has no episode count; the episode must pass through."""
        entry, _, episode = TitleMatcher.determine_correct_entry_and_episode(
            CR_TITLE, cr_season=5, cr_episode=11, season_structure=mushoku_structure
        )
        assert entry is not None and entry["id"] == 178789
        assert episode == 11

    @pytest.mark.parametrize(
        "cr_season, cr_episode, expected_id",
        [
            (1, 5, 108465),
            (2, 3, 127720),
            (3, 10, 146065),
            (4, 12, 166873),
        ],
    )
    def test_earlier_seasons_still_map_correctly(
        self, mushoku_structure, cr_season, cr_episode, expected_id
    ) -> None:
        entry, season, episode = TitleMatcher.determine_correct_entry_and_episode(
            CR_TITLE, cr_season, cr_episode, mushoku_structure
        )
        assert entry is not None
        assert entry["id"] == expected_id
        assert (season, episode) == (cr_season, cr_episode)

    def test_unknown_season_is_refused(self, mushoku_structure) -> None:
        """A season beyond the map must report no match, not write to Season 1."""
        assert TitleMatcher.determine_correct_entry_and_episode(
            CR_TITLE, cr_season=9, cr_episode=4, season_structure=mushoku_structure
        ) == (None, 0, 0)


# ----------------------------------------------------------------------
# The collision fix must not disturb duplicate listings of one broadcast
# ----------------------------------------------------------------------


class TestDuplicateBroadcastListings:
    def test_tv_listing_replaces_ona_for_the_same_season(self) -> None:
        """An ONA and a TV listing of one broadcast collapse to a single season.

        Both carry an explicit season marker, so both claim slot 2; sharing a
        start month marks them as one broadcast and the TV entry wins.
        """
        results = [
            _entry(1, "Show", episodes=12, year=2021, month=1),
            _entry(2, "Show 2nd Season", episodes=12, year=2022, month=1, format="ONA"),
            _entry(3, "Show 2nd Season", episodes=12, year=2022, month=1),
        ]
        structure = TitleMatcher().build_season_structure(results, "Show")
        assert sorted(structure) == [1, 2]
        assert structure[2]["id"] == 3

    def test_separate_cours_are_not_collapsed(self) -> None:
        """Two cours of one season are distinct slots, not duplicates.

        Both detect as season 2, but they air years apart, so the second takes
        the next free slot instead of being discarded.
        """
        results = [
            _entry(1, "Show", episodes=12, year=2021, month=1),
            _entry(2, "Show II", episodes=12, year=2022, month=1),
            _entry(3, "Show II Part 2", episodes=12, year=2023, month=7),
        ]
        structure = TitleMatcher().build_season_structure(results, "Show")
        assert sorted(structure) == [1, 2, 3]
        assert [structure[sn]["id"] for sn in (1, 2, 3)] == [1, 2, 3]
