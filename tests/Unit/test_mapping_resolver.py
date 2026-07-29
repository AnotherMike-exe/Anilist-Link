"""Unit tests for MappingResolver.resolve_and_add ID-override behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.Download.MappingResolver import AddResult, MappingResolver


def _make_resolver() -> MappingResolver:
    return MappingResolver(
        db=AsyncMock(),
        anilist_client=AsyncMock(),
        sonarr_client=AsyncMock(),
        radarr_client=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_tmdb_override_skips_resolution() -> None:
    """A supplied tmdb_id_override must bypass AniList TMDB resolution."""
    resolver = _make_resolver()
    resolver.add_to_radarr = AsyncMock(  # type: ignore[method-assign]
        return_value=AddResult(
            ok=True, anilist_id=1, service="radarr", external_id=999, arr_id=5
        )
    )

    with patch(
        "src.Download.MappingResolver.resolve_tmdb_id", new=AsyncMock()
    ) as mock_resolve:
        result = await resolver.resolve_and_add(
            anilist_id=1,
            anilist_format="MOVIE",
            anilist_media={"title": {"romaji": "Some Movie"}, "synonyms": []},
            quality_profile_id=1,
            root_folder_path="/movies",
            tmdb_id_override=999,
        )

    mock_resolve.assert_not_awaited()
    resolver.add_to_radarr.assert_awaited_once()
    assert resolver.add_to_radarr.await_args.kwargs["tmdb_id"] == 999
    assert result.ok is True


@pytest.mark.asyncio
async def test_movie_without_tmdb_requests_disambiguation() -> None:
    """When TMDB can't be resolved and no override is given, ask to disambiguate."""
    resolver = _make_resolver()

    with patch(
        "src.Download.MappingResolver.resolve_tmdb_id",
        new=AsyncMock(return_value=None),
    ):
        result = await resolver.resolve_and_add(
            anilist_id=2,
            anilist_format="MOVIE",
            anilist_media={"title": {"romaji": "Unlinked Movie"}, "synonyms": []},
            quality_profile_id=1,
            root_folder_path="/movies",
        )

    assert result.ok is False
    assert result.needs_disambiguation is True
    assert result.service == "radarr"
