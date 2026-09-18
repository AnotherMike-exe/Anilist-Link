"""Crunchyroll's season title outranks its season number.

CR's season *numbers* count everything it lists for a franchise, including movies
and arcs AniList publishes as separate entries, so the two numbering schemes
drift apart. Demon Slayer is the clearest case: a 2026-09-18 run logged refusals
for CR seasons 6 and 7 against a map covering 1-5, and CR's "season 5" carried 11
episodes where AniList's season 5 has 8 — because CR's season 5 is AniList's
season 4.

The season *title* has no such problem: CR's arc names line up with AniList's
English titles ("… Hashira Training Arc" ↔ ``Kimetsu no Yaiba: Hashira
Geiko-hen``). When one season matches near-exactly and unambiguously, it is the
better answer.

The threshold matters. The scorer floors any substring-containment pair at 0.90,
so a movie arc absent from the TV map — Infinity Castle — scores 0.90 against
season 1 purely because season 1's title is a substring of it. Requiring 0.95
keeps that from being folded onto season 1.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.Matching.TitleMatcher import TitleMatcher, season_from_cr_season_title


def _media(
    anilist_id: int,
    romaji: str,
    english: str,
    episodes: int | None,
    year: int,
    month: int,
    format: str = "TV",
) -> dict[str, Any]:
    return {
        "id": anilist_id,
        "format": format,
        "episodes": episodes,
        "startDate": {"year": year, "month": month, "day": 1},
        "title": {"romaji": romaji, "english": english, "native": ""},
        "synonyms": [],
    }


CR_TITLE = "Demon Slayer: Kimetsu no Yaiba"

DEMON_SLAYER_SEARCH = [
    _media(101922, "Kimetsu no Yaiba", CR_TITLE, 26, 2019, 4),
    _media(
        142329,
        "Kimetsu no Yaiba: Mugen Ressha-hen (TV)",
        f"{CR_TITLE} Mugen Train Arc",
        7,
        2021,
        10,
    ),
    _media(
        145139,
        "Kimetsu no Yaiba: Yuukaku-hen",
        f"{CR_TITLE} Entertainment District Arc",
        11,
        2021,
        12,
    ),
    _media(
        166240,
        "Kimetsu no Yaiba: Katanakaji no Sato-hen",
        f"{CR_TITLE} Swordsmith Village Arc",
        11,
        2023,
        4,
    ),
    _media(
        176496,
        "Kimetsu no Yaiba: Hashira Geiko-hen",
        f"{CR_TITLE} Hashira Training Arc",
        8,
        2024,
        5,
    ),
    # The movies are excluded from the TV season map.
    _media(
        112151,
        "Kimetsu no Yaiba: Mugen Ressha-hen",
        "Demon Slayer: Mugen Train",
        1,
        2020,
        10,
        "MOVIE",
    ),
    _media(
        186922,
        "Kimetsu no Yaiba: Mugen Jou-hen",
        "Demon Slayer: Infinity Castle",
        1,
        2025,
        7,
        "MOVIE",
    ),
]


@pytest.fixture
def demon_slayer() -> dict[int, dict[str, Any]]:
    return TitleMatcher().build_season_structure(DEMON_SLAYER_SEARCH, CR_TITLE)


class TestSeasonFromCrSeasonTitle:
    @pytest.mark.parametrize(
        "cr_season_title, expected_season",
        [
            (f"{CR_TITLE}", 1),
            (f"{CR_TITLE} Mugen Train Arc", 2),
            (f"{CR_TITLE} Entertainment District Arc", 3),
            (f"{CR_TITLE} Swordsmith Village Arc", 4),
            (f"{CR_TITLE} Hashira Training Arc", 5),
        ],
    )
    def test_each_arc_resolves_to_its_season(
        self, demon_slayer, cr_season_title: str, expected_season: int
    ) -> None:
        assert season_from_cr_season_title(cr_season_title, demon_slayer) == (
            expected_season
        )

    def test_movie_arc_absent_from_the_map_is_declined(self, demon_slayer) -> None:
        """Infinity Castle is a movie — it must not be folded onto season 1.

        It still scores 0.90 against season 1 by substring containment, which is
        exactly why the threshold sits above that floor.
        """
        assert (
            season_from_cr_season_title(f"{CR_TITLE} Infinity Castle Arc", demon_slayer)
            is None
        )

    @pytest.mark.parametrize("cr_season_title", ["", "   ", "Season 4", "S4", "Part 2"])
    def test_generic_or_empty_titles_are_declined(
        self, demon_slayer, cr_season_title: str
    ) -> None:
        """Nothing to identify — the caller keeps CR's own number."""
        assert season_from_cr_season_title(cr_season_title, demon_slayer) is None

    def test_unrelated_title_is_declined(self, demon_slayer) -> None:
        assert (
            season_from_cr_season_title("Frieren: Beyond Journey's End", demon_slayer)
            is None
        )

    def test_empty_structure_is_safe(self) -> None:
        assert season_from_cr_season_title(f"{CR_TITLE} Mugen Train Arc", {}) is None


class TestDemonSlayerRewatch:
    """The reported scenario: a full rewatch, with CR's numbering offset by one."""

    @pytest.mark.parametrize(
        "cr_season, cr_episode, cr_season_title, expected_id, expected_episode",
        [
            (1, 26, f"{CR_TITLE}", 101922, 26),
            (3, 7, f"{CR_TITLE} Mugen Train Arc", 142329, 7),
            (4, 11, f"{CR_TITLE} Entertainment District Arc", 145139, 11),
            (5, 11, f"{CR_TITLE} Swordsmith Village Arc", 166240, 11),
            (6, 8, f"{CR_TITLE} Hashira Training Arc", 176496, 8),
        ],
    )
    def test_offset_seasons_resolve_by_title(
        self,
        demon_slayer,
        cr_season: int,
        cr_episode: int,
        cr_season_title: str,
        expected_id: int,
        expected_episode: int,
    ) -> None:
        entry, _season, episode = TitleMatcher.determine_correct_entry_and_episode(
            CR_TITLE,
            cr_season,
            cr_episode,
            demon_slayer,
            cr_season_title=cr_season_title,
        )
        assert entry is not None
        assert entry["id"] == expected_id
        assert episode == expected_episode

    def test_eleven_episodes_no_longer_overrun_an_eight_episode_season(
        self, demon_slayer
    ) -> None:
        """The exact symptom: CR season 5 episode 11 against an 8-episode map slot.

        By title it is the Swordsmith Village Arc, which really does have 11.
        """
        entry, season, episode = TitleMatcher.determine_correct_entry_and_episode(
            CR_TITLE,
            cr_season=5,
            cr_episode=11,
            season_structure=demon_slayer,
            cr_season_title=f"{CR_TITLE} Swordsmith Village Arc",
        )
        assert entry is not None
        assert entry["id"] == 166240
        assert (season, episode) == (4, 11)

    def test_infinity_castle_is_reported_not_guessed(self, demon_slayer) -> None:
        """A movie arc has no TV slot, so it must reach the unmapped report."""
        assert TitleMatcher.determine_correct_entry_and_episode(
            CR_TITLE,
            cr_season=7,
            cr_episode=1,
            season_structure=demon_slayer,
            cr_season_title=f"{CR_TITLE} Infinity Castle Arc",
        ) == (None, 0, 0)

    def test_movies_stay_out_of_the_tv_season_map(self, demon_slayer) -> None:
        ids = {sd["id"] for sd in demon_slayer.values()}
        assert 112151 not in ids
        assert 186922 not in ids


class TestOtherFranchisesUnaffected:
    """A season title must never move a franchise whose numbering already agrees."""

    def _structure(
        self, media: list[dict[str, Any]], search: str
    ) -> dict[int, dict[str, Any]]:
        return TitleMatcher().build_season_structure(media, search)

    REZERO_SEARCH = [
        _media(
            21355,
            "Re:Zero kara Hajimeru Isekai Seikatsu",
            "Re:ZERO -Starting Life in Another World-",
            25,
            2016,
            4,
        ),
        _media(
            108632,
            "Re:Zero kara Hajimeru Isekai Seikatsu 2nd Season",
            "Re:ZERO -Starting Life in Another World- Season 2",
            13,
            2020,
            7,
        ),
        _media(
            119661,
            "Re:Zero kara Hajimeru Isekai Seikatsu 2nd Season Part 2",
            "Re:ZERO -Starting Life in Another World- Season 2 Part 2",
            12,
            2021,
            1,
        ),
        _media(
            163134,
            "Re:Zero kara Hajimeru Isekai Seikatsu 3rd Season",
            "Re:ZERO -Starting Life in Another World- Season 3",
            16,
            2024,
            10,
        ),
        _media(
            189046,
            "Re:Zero kara Hajimeru Isekai Seikatsu 4th Season",
            "Re:ZERO -Starting Life in Another World- Season 4",
            19,
            2026,
            4,
        ),
    ]

    def test_rezero_season_four_is_unchanged_with_a_title(self) -> None:
        title = "Re:ZERO -Starting Life in Another World-"
        structure = self._structure(self.REZERO_SEARCH, title)
        entry, season, episode = TitleMatcher.determine_correct_entry_and_episode(
            title,
            4,
            15,
            structure,
            cr_season_title=f"{title} Season 4",
        )
        assert entry is not None
        assert entry["id"] == 189046
        assert (season, episode) == (4, 15)

    def test_rezero_season_four_is_unchanged_without_a_title(self) -> None:
        """Callers that pass no title keep the previous behaviour exactly."""
        title = "Re:ZERO -Starting Life in Another World-"
        structure = self._structure(self.REZERO_SEARCH, title)
        with_title = TitleMatcher.determine_correct_entry_and_episode(
            title, 4, 15, structure, cr_season_title=f"{title} Season 4"
        )
        without = TitleMatcher.determine_correct_entry_and_episode(
            title, 4, 15, structure
        )
        assert with_title[1:] == without[1:]
        assert with_title[0] is not None and without[0] is not None
        assert with_title[0]["id"] == without[0]["id"]

    JJK_SEARCH = [
        _media(113415, "Jujutsu Kaisen", "JUJUTSU KAISEN", 24, 2020, 10),
        _media(
            145064,
            "Jujutsu Kaisen 2nd Season",
            "JUJUTSU KAISEN Season 2",
            23,
            2023,
            7,
        ),
        _media(
            190000,
            "Jujutsu Kaisen: Shimetsu Kaiyuu - Zenpen",
            "JUJUTSU KAISEN Culling Game Arc Part 1",
            12,
            2026,
            1,
        ),
    ]

    def test_jjk_absolute_numbering_survives_a_season_title(self) -> None:
        """CR really does number JJK continuously — episode 59 is Zenpen 12."""
        structure = self._structure(self.JJK_SEARCH, "JUJUTSU KAISEN")
        entry, season, episode = TitleMatcher.determine_correct_entry_and_episode(
            "JUJUTSU KAISEN",
            3,
            59,
            structure,
            cr_season_title="JUJUTSU KAISEN Culling Game Arc Part 1",
        )
        assert entry is not None
        assert entry["id"] == 190000
        assert (season, episode) == (3, 12)
