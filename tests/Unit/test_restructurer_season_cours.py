"""Season/cour grouping for the library restructurer.

The 2026-09-18 restructure analysis shows what numbering each cour separately
does to a split franchise:

    Mushoku Tensei III … (2026) - S03E01 -> Season 03/Mushoku Tensei II … - S03E01
    ReZero … 4th Season (2026) - S04E08 -> Season 04/ReZero … 3rd Season - S04E08
    ReZero … 4th Season (2026) - S04E17 -> Season 05/ReZero … - S05E01

Season 3 pointed at "Mushoku Tensei II" (so season-3 files were named after
season 2 and collided with its overflow), and Re:Zero gained a phantom season 5.
Cours of one season must share that season's number.
"""

from __future__ import annotations

import pytest

from src.Scanner.LibraryRestructurer import (
    _anilist_season_numbers,
    _build_tv_season_cour_map,
    _build_tv_season_map,
    _cumulative_season_episodes,
)


def _row(
    anilist_id: int,
    season_order: int,
    romaji: str,
    english: str = "",
    episodes: int | None = 12,
    format: str = "TV",
) -> dict:
    return {
        "anilist_id": anilist_id,
        "season_order": season_order,
        "display_title": romaji,
        "title_romaji": romaji,
        "title_english": english,
        "format": format,
        "episodes": episodes,
    }


MUSHOKU_GROUP = [
    _row(108465, 1, "Mushoku Tensei: Isekai Ittara Honki Dasu", episodes=11),
    _row(127720, 2, "Mushoku Tensei: Isekai Ittara Honki Dasu Part 2"),
    _row(
        141534,
        3,
        "Mushoku Tensei: Isekai Ittara Honki Dasu Part 2 - Eris no Goblin Toubatsu",
        episodes=1,
        format="SPECIAL",
    ),
    _row(146065, 4, "Mushoku Tensei II: Isekai Ittara Honki Dasu"),
    _row(166873, 5, "Mushoku Tensei II: Isekai Ittara Honki Dasu Part 2"),
    _row(178789, 6, "Mushoku Tensei III: Isekai Ittara Honki Dasu", episodes=None),
]

REZERO_GROUP = [
    _row(21355, 1, "Re:Zero kara Hajimeru Isekai Seikatsu", episodes=25),
    _row(108632, 2, "Re:Zero kara Hajimeru Isekai Seikatsu 2nd Season", episodes=13),
    _row(
        119661,
        3,
        "Re:Zero kara Hajimeru Isekai Seikatsu 2nd Season Part 2",
        episodes=12,
    ),
    _row(163134, 4, "Re:Zero kara Hajimeru Isekai Seikatsu 3rd Season", episodes=16),
    _row(189046, 5, "Re:Zero kara Hajimeru Isekai Seikatsu 4th Season", episodes=19),
]


class TestSeasonCourMap:
    def test_mushoku_five_tv_entries_make_three_seasons(self) -> None:
        cour_map = _build_tv_season_cour_map(MUSHOKU_GROUP)
        assert sorted(cour_map) == [1, 2, 3]
        assert [e["anilist_id"] for e in cour_map[1]] == [108465, 127720]
        assert [e["anilist_id"] for e in cour_map[2]] == [146065, 166873]
        assert [e["anilist_id"] for e in cour_map[3]] == [178789]

    def test_rezero_five_tv_entries_make_four_seasons(self) -> None:
        """Regression: the fifth cour created a phantom season 5."""
        cour_map = _build_tv_season_cour_map(REZERO_GROUP)
        assert sorted(cour_map) == [1, 2, 3, 4]
        assert [e["anilist_id"] for e in cour_map[2]] == [108632, 119661]
        assert [e["anilist_id"] for e in cour_map[4]] == [189046]

    def test_special_is_excluded(self) -> None:
        cour_map = _build_tv_season_cour_map(MUSHOKU_GROUP)
        ids = {e["anilist_id"] for cours in cour_map.values() for e in cours}
        assert 141534 not in ids

    def test_season_three_represents_the_third_season(self) -> None:
        """Regression: season 3 used to resolve to "Mushoku Tensei II"."""
        season_map = _build_tv_season_map(MUSHOKU_GROUP)
        assert season_map[3]["anilist_id"] == 178789
        assert season_map[3]["title_romaji"].startswith("Mushoku Tensei III")

    def test_rezero_season_four_represents_the_fourth_season(self) -> None:
        """Regression: season 4 used to resolve to the 3rd Season entry."""
        season_map = _build_tv_season_map(REZERO_GROUP)
        assert season_map[4]["anilist_id"] == 189046
        assert "4th Season" in season_map[4]["title_romaji"]

    def test_empty_group_is_safe(self) -> None:
        assert _build_tv_season_cour_map([]) == {}
        assert _build_tv_season_map([]) == {}


class TestAnilistSeasonNumbers:
    @pytest.mark.parametrize(
        "anilist_id, expected_season",
        [
            (108465, 1),
            (127720, 1),  # cour 2 of season 1, not season 2
            (146065, 2),
            (166873, 2),
            (178789, 3),  # was 5 when every cour took a number
        ],
    )
    def test_mushoku_entries_map_to_their_season(
        self, anilist_id: int, expected_season: int
    ) -> None:
        assert _anilist_season_numbers(MUSHOKU_GROUP)[anilist_id] == expected_season

    @pytest.mark.parametrize(
        "anilist_id, expected_season",
        [(21355, 1), (108632, 2), (119661, 2), (163134, 3), (189046, 4)],
    )
    def test_rezero_entries_map_to_their_season(
        self, anilist_id: int, expected_season: int
    ) -> None:
        assert _anilist_season_numbers(REZERO_GROUP)[anilist_id] == expected_season

    def test_non_tv_entry_keeps_its_chronological_order(self) -> None:
        """Specials/movies must not be renumbered into a TV season slot."""
        numbers = _anilist_season_numbers(MUSHOKU_GROUP)
        assert numbers[141534] == 3  # its season_order, untouched


class TestCumulativeSeasonEpisodes:
    def test_counts_every_cour_of_earlier_seasons(self) -> None:
        """Season 1 is 11 + 12, so anything before season 2 totals 23."""
        cour_map = _build_tv_season_cour_map(MUSHOKU_GROUP)
        assert _cumulative_season_episodes(2, cour_map) == 23

    def test_counts_across_two_split_seasons(self) -> None:
        """Before season 3: 11+12 then 12+12."""
        cour_map = _build_tv_season_cour_map(MUSHOKU_GROUP)
        assert _cumulative_season_episodes(3, cour_map) == 47

    def test_first_season_has_nothing_before_it(self) -> None:
        cour_map = _build_tv_season_cour_map(MUSHOKU_GROUP)
        assert _cumulative_season_episodes(1, cour_map) == 0

    def test_unknown_cour_count_bails_out(self) -> None:
        """Season 3 is still airing, so totals past it are not computable."""
        cour_map = _build_tv_season_cour_map(MUSHOKU_GROUP)
        assert _cumulative_season_episodes(4, cour_map) is None

    def test_missing_season_bails_out(self) -> None:
        assert _cumulative_season_episodes(3, {1: [{"episodes": 12}]}) is None

    def test_rezero_split_season_two_counts_both_cours(self) -> None:
        cour_map = _build_tv_season_cour_map(REZERO_GROUP)
        assert _cumulative_season_episodes(3, cour_map) == 50  # 25 + (13+12)
