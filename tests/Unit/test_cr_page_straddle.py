"""Two bugs found reviewing a 2026-09-18 preview run.

Demon Slayer's Crunchyroll history straddled a page boundary — page 1 ended at
episode 8, page 2 ran from episode 7 down to 1 — and the run produced two rows
for one franchise, neither right:

    Episode 11 (absolute) maps to S1E11      <- page 1, wrong entry entirely
    (silent)                                 <- page 2, Geiko-hen at episode 7

Bug A: the highest-episode-per-season map was rebuilt per page, so a straddling
series was evaluated twice, and the later page's *lower* episode produced a
second row. Meanwhile the episode detail attached to a row kept accumulating
across pages, so a row could claim episode 7 while listing episodes 1-11.

Bug B: ``cr_episode > target_season_eps`` was taken as proof of absolute
numbering. Episode 11 of an 8-episode season 5 resolved to season 1 episode 11 —
earlier than the season Crunchyroll itself reported, which disproves the
hypothesis rather than confirming it.

Jujutsu Kaisen is the control: it genuinely uses absolute numbering (24 + 23
episodes precede the Zenpen cour), so CR season 3 episode 59 must still resolve
to Zenpen episode 12.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.Matching.TitleMatcher import TitleMatcher


def _season_structure(
    seasons: dict[int, tuple[int, int | None]],
) -> dict[int, dict[str, Any]]:
    """Build a season structure from {season: (anilist_id, episodes)}."""
    structure: dict[int, dict[str, Any]] = {}
    for season, (anilist_id, episodes) in seasons.items():
        entry = {
            "id": anilist_id,
            "episodes": episodes,
            "title": {"romaji": f"Season {season}", "english": ""},
        }
        structure[season] = {
            "entry": entry,
            "episodes": episodes,
            "title": f"Season {season}",
            "id": anilist_id,
            "similarity": 1.0,
            "release_order": 20200000 + season,
            "cours": [
                {
                    "entry": entry,
                    "episodes": episodes,
                    "title": f"Season {season}",
                    "id": anilist_id,
                    "cour": 1,
                }
            ],
        }
    return structure


# Real maps from the log.
DEMON_SLAYER = _season_structure(
    {
        1: (101922, 26),  # Kimetsu no Yaiba
        2: (142329, 7),  # Mugen Ressha-hen (TV)
        3: (145139, 11),  # Yuukaku-hen
        4: (166240, 11),  # Katanakaji no Sato-hen
        5: (176496, 8),  # Hashira Geiko-hen
    }
)

JUJUTSU_KAISEN = _season_structure(
    {
        1: (113415, 24),  # Jujutsu Kaisen
        2: (145064, 23),  # 2nd Season
        3: (190000, 12),  # Shimetsu Kaiyuu - Zenpen
        4: (190001, None),  # Shimetsu Kaiyuu - Kouhen (airing)
    }
)


class TestAbsoluteHypothesisGuard:
    """Bug B — an absolute reading must not resolve before the reported season."""

    def test_demon_slayer_season_five_stays_in_season_five(self) -> None:
        """Regression: episode 11 of season 5 resolved to season 1 episode 11."""
        entry, season, episode = TitleMatcher.determine_correct_entry_and_episode(
            "Demon Slayer: Kimetsu no Yaiba",
            cr_season=5,
            cr_episode=11,
            season_structure=DEMON_SLAYER,
        )
        assert entry is not None
        assert season == 5
        assert entry["id"] == 176496
        # Capped at the season's real length rather than folded elsewhere.
        assert episode == 8

    def test_never_resolves_earlier_than_the_reported_season(self) -> None:
        """Crunchyroll would not file a season-1 episode under season 5."""
        for cr_episode in range(9, 27):
            _, season, _ = TitleMatcher.determine_correct_entry_and_episode(
                "Demon Slayer: Kimetsu no Yaiba",
                cr_season=5,
                cr_episode=cr_episode,
                season_structure=DEMON_SLAYER,
            )
            assert season >= 5, f"episode {cr_episode} resolved to season {season}"

    def test_genuine_absolute_numbering_still_works(self) -> None:
        """Control: JJK really does number continuously across the franchise."""
        entry, season, episode = TitleMatcher.determine_correct_entry_and_episode(
            "JUJUTSU KAISEN",
            cr_season=3,
            cr_episode=59,
            season_structure=JUJUTSU_KAISEN,
        )
        assert entry is not None
        assert entry["id"] == 190000
        assert (season, episode) == (3, 12)  # 59 - (24 + 23)

    @pytest.mark.parametrize(
        "cr_episode, expected_episode",
        [(48, 1), (54, 7), (58, 11), (59, 12)],
    )
    def test_jjk_absolute_episodes_across_the_cour(
        self, cr_episode: int, expected_episode: int
    ) -> None:
        _, season, episode = TitleMatcher.determine_correct_entry_and_episode(
            "JUJUTSU KAISEN",
            cr_season=3,
            cr_episode=cr_episode,
            season_structure=JUJUTSU_KAISEN,
        )
        assert (season, episode) == (3, expected_episode)

    def test_absolute_from_season_one_still_walks_forward(self) -> None:
        """A franchise-wide count reported under season 1 must still resolve."""
        _, season, episode = TitleMatcher.determine_correct_entry_and_episode(
            "Demon Slayer: Kimetsu no Yaiba",
            cr_season=1,
            cr_episode=30,
            season_structure=DEMON_SLAYER,
        )
        assert (season, episode) == (2, 4)  # 30 - 26

    def test_absolute_into_an_airing_season_still_resolves(self) -> None:
        """The airing season has no total, so the remainder belongs to it."""
        entry, season, episode = TitleMatcher.determine_correct_entry_and_episode(
            "JUJUTSU KAISEN",
            cr_season=4,
            cr_episode=62,
            season_structure=JUJUTSU_KAISEN,
        )
        assert entry is not None
        assert entry["id"] == 190001
        assert (season, episode) == (4, 3)  # 62 - (24 + 23 + 12)

    def test_within_season_episode_is_untouched(self) -> None:
        _, season, episode = TitleMatcher.determine_correct_entry_and_episode(
            "Demon Slayer: Kimetsu no Yaiba",
            cr_season=5,
            cr_episode=6,
            season_structure=DEMON_SLAYER,
        )
        assert (season, episode) == (5, 6)


class TestPageStraddle:
    """Bug A — a series spanning two pages must be evaluated once, at its max."""

    @staticmethod
    def _episode(series: str, season: int, number: int) -> Any:
        from src.Clients.CrunchyrollClient import CrunchyrollEpisode

        return CrunchyrollEpisode(
            series_title=series,
            season=season,
            episode_number=number,
            episode_title=f"E{number}",
            season_title=f"Season {season}",
            watch_date="2026-09-17",
        )

    def _runner(self) -> Any:
        from unittest.mock import MagicMock

        from src.Sync.CrunchyrollPreviewRunner import CrunchyrollPreviewRunner
        from src.Utils.Config import AppConfig

        runner = CrunchyrollPreviewRunner(
            db=MagicMock(),
            anilist_client=MagicMock(),
            title_matcher=TitleMatcher(),
            cr_client=MagicMock(),
            config=AppConfig(),
        )
        runner._series_max.clear()
        return runner

    def test_grouping_is_still_page_local(self) -> None:
        """_group_episodes reports this page only — the run-level max is layered
        on top of it, so its own contract is unchanged."""
        runner = self._runner()
        page_one = [self._episode("Demon Slayer", 5, n) for n in (11, 10, 9, 8)]
        page_two = [self._episode("Demon Slayer", 5, n) for n in (7, 6, 5)]

        assert runner._group_episodes(page_one) == {("Demon Slayer", 5): 11}
        assert runner._group_episodes(page_two) == {("Demon Slayer", 5): 7}

    def test_episode_detail_accumulates_across_pages(self) -> None:
        """The raw episode list spans the whole run, which is why a row built
        mid-run had to be refreshed before insert."""
        runner = self._runner()
        runner._group_episodes([self._episode("Demon Slayer", 5, n) for n in (11, 10)])
        runner._group_episodes([self._episode("Demon Slayer", 5, n) for n in (7, 6)])

        numbers = {e["cr_episode"] for e in runner._raw_episodes[("Demon Slayer", 5)]}
        assert numbers == {6, 7, 10, 11}

    def test_refresh_attaches_the_full_episode_list(self) -> None:
        """Regression: a row claimed episode 7 while listing episodes 1-11."""
        import json

        runner = self._runner()
        runner._group_episodes([self._episode("Demon Slayer", 5, n) for n in (11, 10)])
        runner._preview_rows.append(
            {
                "cr_title": "Demon Slayer",
                "cr_season": 5,
                "episodes_json": json.dumps(
                    [{"cr_season": 5, "cr_episode": 11, "episode_title": "E11"}]
                ),
            }
        )
        # A later page adds earlier episodes.
        runner._group_episodes([self._episode("Demon Slayer", 5, n) for n in (7, 6)])
        runner._refresh_episode_details()

        listed = [
            e["cr_episode"]
            for e in json.loads(runner._preview_rows[0]["episodes_json"])
        ]
        assert listed == [6, 7, 10, 11]

    def test_refresh_leaves_unknown_rows_alone(self) -> None:
        import json

        runner = self._runner()
        runner._preview_rows.append(
            {"cr_title": "Nothing Collected", "cr_season": 1, "episodes_json": "[]"}
        )
        runner._refresh_episode_details()
        assert json.loads(runner._preview_rows[0]["episodes_json"]) == []

    def test_movie_rows_carry_season_zero(self) -> None:
        """Movies are keyed under season 0 so the refresh can find them."""
        import json

        runner = self._runner()
        runner._group_episodes(
            [
                type(
                    "Ep",
                    (),
                    {
                        "series_title": "A Movie",
                        "season": 0,
                        "episode_number": 0,
                        "episode_title": "The Movie",
                        "season_title": "",
                        "watch_date": "2026-09-17",
                        "is_movie": True,
                    },
                )()
            ]
        )
        runner._preview_rows.append(
            {"cr_title": "A Movie", "cr_season": 0, "episodes_json": "[]"}
        )
        runner._refresh_episode_details()
        listed = json.loads(runner._preview_rows[0]["episodes_json"])
        assert len(listed) == 1
        assert listed[0]["is_movie"] is True


class TestStraddlingSeriesIsEvaluatedOnce:
    """The behavioural fix: two pages, one evaluation, at the highest episode."""

    @staticmethod
    def _episode(series: str, season: int, number: int) -> Any:
        from src.Clients.CrunchyrollClient import CrunchyrollEpisode

        return CrunchyrollEpisode(
            series_title=series,
            season=season,
            episode_number=number,
            episode_title=f"E{number}",
            season_title=f"Season {season}",
            watch_date="2026-09-17",
        )

    def _runner(self, calls: list[tuple[str, int, int]]) -> Any:
        from unittest.mock import MagicMock

        from src.Sync.CrunchyrollPreviewRunner import CrunchyrollPreviewRunner
        from src.Utils.Config import AppConfig

        anilist = MagicMock()
        anilist.health.is_down = False

        runner = CrunchyrollPreviewRunner(
            db=MagicMock(),
            anilist_client=anilist,
            title_matcher=TitleMatcher(),
            cr_client=MagicMock(),
            config=AppConfig(),
        )

        async def _process_series(
            series_title: str, cr_season: int, cr_episode: int, user: Any
        ) -> bool:
            calls.append((series_title, cr_season, cr_episode))
            return True

        runner._process_series = _process_series  # type: ignore[method-assign]
        return runner

    async def test_later_page_does_not_re_evaluate_at_a_lower_episode(self) -> None:
        """Regression: page 2's episode 7 produced a second, lower proposal."""
        calls: list[tuple[str, int, int]] = []
        runner = self._runner(calls)
        user = {"user_id": "u", "access_token": "t", "anilist_id": 1}

        # Page 1 carries the newest episodes, page 2 the earlier ones.
        await runner._process_page(
            [self._episode("Demon Slayer", 5, n) for n in (11, 10, 9, 8)], user
        )
        await runner._process_page(
            [self._episode("Demon Slayer", 5, n) for n in (7, 6, 5, 4)], user
        )

        assert calls == [("Demon Slayer", 5, 11)]

    async def test_a_higher_episode_on_a_later_page_is_honoured(self) -> None:
        """Out-of-order history must still raise the proposal."""
        calls: list[tuple[str, int, int]] = []
        runner = self._runner(calls)
        user = {"user_id": "u", "access_token": "t", "anilist_id": 1}

        await runner._process_page([self._episode("Show", 1, 5)], user)
        await runner._process_page([self._episode("Show", 1, 9)], user)

        assert calls == [("Show", 1, 5), ("Show", 1, 9)]

    async def test_distinct_seasons_are_evaluated_separately(self) -> None:
        calls: list[tuple[str, int, int]] = []
        runner = self._runner(calls)
        user = {"user_id": "u", "access_token": "t", "anilist_id": 1}

        await runner._process_page(
            [self._episode("Show", 1, 12), self._episode("Show", 2, 3)], user
        )
        assert sorted(calls) == [("Show", 1, 12), ("Show", 2, 3)]

    async def test_repeat_page_counts_as_skipped(self) -> None:
        """The skip feeds the early-stop ratio, so it must be reported."""
        calls: list[tuple[str, int, int]] = []
        runner = self._runner(calls)
        user = {"user_id": "u", "access_token": "t", "anilist_id": 1}

        await runner._process_page([self._episode("Show", 1, 9)], user)
        skipped = await runner._process_page([self._episode("Show", 1, 4)], user)
        assert skipped == 1


class TestWatchSyncerStraddle:
    """The apply path carries the same guard as the preview path."""

    @staticmethod
    def _episode(series: str, season: int, number: int) -> Any:
        from src.Clients.CrunchyrollClient import CrunchyrollEpisode

        return CrunchyrollEpisode(
            series_title=series,
            season=season,
            episode_number=number,
            episode_title=f"E{number}",
            season_title=f"Season {season}",
            watch_date="2026-09-17",
        )

    def _syncer(self, calls: list[tuple[str, int, int]]) -> Any:
        from unittest.mock import MagicMock

        from src.Sync.WatchSyncer import WatchSyncer
        from src.Utils.Config import AppConfig

        anilist = MagicMock()
        anilist.health.is_down = False

        syncer = WatchSyncer(
            db=MagicMock(),
            anilist_client=anilist,
            title_matcher=TitleMatcher(),
            cr_client=MagicMock(),
            config=AppConfig(),
        )
        syncer._reset_results()

        async def _process_series_entry(
            series_title: str, cr_season: int, cr_episode: int, user: Any
        ) -> bool:
            calls.append((series_title, cr_season, cr_episode))
            return True

        syncer._process_series_entry = _process_series_entry  # type: ignore[method-assign]
        return syncer

    async def test_later_page_does_not_re_sync_at_a_lower_episode(self) -> None:
        calls: list[tuple[str, int, int]] = []
        syncer = self._syncer(calls)
        users = [{"user_id": "u", "access_token": "t", "anilist_id": 1}]

        await syncer._process_page_episodes(
            [self._episode("Demon Slayer", 5, n) for n in (11, 10)], users
        )
        await syncer._process_page_episodes(
            [self._episode("Demon Slayer", 5, n) for n in (7, 6)], users
        )

        assert calls == [("Demon Slayer", 5, 11)]

    async def test_reset_clears_the_run_level_maximum(self) -> None:
        """A second sync run must not inherit the previous run's ceiling."""
        calls: list[tuple[str, int, int]] = []
        syncer = self._syncer(calls)
        users = [{"user_id": "u", "access_token": "t", "anilist_id": 1}]

        await syncer._process_page_episodes([self._episode("Show", 1, 9)], users)
        syncer._reset_results()
        await syncer._process_page_episodes([self._episode("Show", 1, 9)], users)

        assert calls == [("Show", 1, 9), ("Show", 1, 9)]
