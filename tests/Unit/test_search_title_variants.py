"""Tests for punctuation-restoring AniList search variants.

A media folder name cannot contain a colon, so "Re:Zero kara Hajimeru Isekai
Seikatsu" is stored on disk as "ReZero kara Hajimeru Isekai Seikatsu". The
2026-09-18 log shows the Jellyfin scanner giving up on exactly that:

    [no results] ReZero kara Hajimeru Isekai Seikatsu 4th Season (2026)
                 (searched: 'ReZero kara Hajimeru Isekai Seikatsu')

The season qualifier was already stripped correctly — only the missing colon
defeated the search.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.Clients.AnilistClient import AniListClient
from src.Matching.Normalizer import search_title_variants


class TestSearchTitleVariants:
    def test_rezero_folder_name_yields_colon_variant(self) -> None:
        variants = search_title_variants(
            "ReZero kara Hajimeru Isekai Seikatsu 4th Season (2026)"
        )
        assert variants[0] == "ReZero kara Hajimeru Isekai Seikatsu"
        assert "Re:Zero kara Hajimeru Isekai Seikatsu" in variants

    def test_original_is_always_tried_first(self) -> None:
        """The unmodified cleaned title must stay the first candidate."""
        variants = search_title_variants("SteinsGate")
        assert variants[0] == "SteinsGate"

    @pytest.mark.parametrize(
        "title",
        [
            "Re:Zero kara Hajimeru Isekai Seikatsu",
            "Mushoku Tensei Isekai Ittara Honki Dasu",
            "Kaguya-sama wa Kokurasetai",
            "K-On!",
            "18if",
        ],
    )
    def test_titles_without_a_boundary_yield_one_variant(self, title: str) -> None:
        """No lowercase→uppercase boundary means nothing to guess at."""
        assert len(search_title_variants(title)) == 1

    def test_no_duplicate_variants(self) -> None:
        variants = search_title_variants("ReZero kara Hajimeru")
        assert len(variants) == len(set(v.lower() for v in variants))

    def test_empty_title_is_safe(self) -> None:
        assert search_title_variants("") == []

    def test_ambiguous_multi_boundary_token_is_left_alone(self) -> None:
        """Two boundaries in one token is a guess too far."""
        assert search_title_variants("AbCdEf") == ["AbCdEf"]


class _FakeClient:
    """Exercises search_anime_with_variants without touching the network."""

    def __init__(self, responder: Any) -> None:
        self.responder = responder
        self.queries: list[str] = []

    async def search_anime(
        self, query: str, page: int = 1, per_page: int = 10
    ) -> list[dict[str, Any]]:
        self.queries.append(query)
        return self.responder(query)

    search_anime_with_variants = AniListClient.search_anime_with_variants


class TestSearchAnimeWithVariants:
    async def test_falls_through_to_the_colon_variant(self) -> None:
        """Regression: the run-together folder name found nothing and stopped."""
        hit = [{"id": 189046}]

        def responder(query: str) -> list[dict[str, Any]]:
            return hit if query == "Re:Zero kara Hajimeru Isekai Seikatsu" else []

        client = _FakeClient(responder)
        results, used = await client.search_anime_with_variants(
            "ReZero kara Hajimeru Isekai Seikatsu 4th Season (2026)"
        )
        assert results == hit
        assert used == "Re:Zero kara Hajimeru Isekai Seikatsu"
        assert client.queries[0] == "ReZero kara Hajimeru Isekai Seikatsu"

    async def test_stops_at_the_first_hit(self) -> None:
        """A title that matches as-is must not cost extra API calls."""
        client = _FakeClient(lambda q: [{"id": 21355}])
        results, used = await client.search_anime_with_variants(
            "Re:Zero kara Hajimeru Isekai Seikatsu"
        )
        assert results
        assert used == "Re:Zero kara Hajimeru Isekai Seikatsu"
        assert len(client.queries) == 1

    async def test_reports_the_original_when_nothing_matches(self) -> None:
        client = _FakeClient(lambda q: [])
        results, used = await client.search_anime_with_variants("ReZero kara Hajimeru")
        assert results == []
        assert used == "ReZero kara Hajimeru"
        assert len(client.queries) == 3

    async def test_passes_paging_through(self) -> None:
        seen: list[tuple[int, int]] = []

        def responder(query: str) -> list[dict[str, Any]]:
            return [{"id": 1}]

        class _Paging(_FakeClient):
            async def search_anime(
                self, query: str, page: int = 1, per_page: int = 10
            ) -> list[dict[str, Any]]:
                seen.append((page, per_page))
                return responder(query)

        client = _Paging(responder)
        await client.search_anime_with_variants("Show", page=2, per_page=15)
        assert seen == [(2, 15)]
