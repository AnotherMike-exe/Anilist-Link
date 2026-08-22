"""Unit tests for MappingResolver — ID overrides and *arr path reconciliation."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.Download.MappingResolver import AddResult, MappingResolver
from src.Utils.Config import AppConfig, SonarrConfig


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


# ---------------------------------------------------------------------------
# Path reconciliation on link/add
# ---------------------------------------------------------------------------


def _make_config(library_unused: str = "") -> AppConfig:
    return AppConfig(
        sonarr=SonarrConfig(
            url="http://sonarr:8989",
            api_key="testkey",
            path_prefix="",
            local_path_prefix="",
        )
    )


def _make_db(library_path: str, title: str) -> MagicMock:
    db = MagicMock()
    db.executed: list[tuple[str, tuple]] = []

    async def execute(query: str, params: tuple = ()) -> None:
        db.executed.append((query, params))

    async def fetch_all(query: str, params: tuple = ()) -> list[dict[str, Any]]:
        return []

    async def fetch_one(query: str, params: tuple = ()) -> dict[str, Any] | None:
        return None

    async def get_setting(key: str) -> str | None:
        if key == "app.title_display":
            return "romaji"
        return None

    async def get_all_libraries() -> list[dict[str, Any]]:
        return [{"id": 1, "name": "Anime", "paths": json.dumps([library_path])}]

    async def get_cached_metadata(anilist_id: int) -> dict[str, Any]:
        return {"title_romaji": title}

    async def get_users_by_service(service: str) -> list:
        return []

    async def get_series_group_by_anilist_id(anilist_id: int) -> None:
        return None

    db.execute = execute
    db.fetch_all = fetch_all
    db.fetch_one = fetch_one
    db.get_setting = get_setting
    db.get_all_libraries = get_all_libraries
    db.get_cached_metadata = get_cached_metadata
    db.get_users_by_service = get_users_by_service
    db.get_series_group_by_anilist_id = get_series_group_by_anilist_id
    return db


def _make_sonarr(existing_path: str) -> MagicMock:
    client = MagicMock()
    client.updated_to = []
    client.rescanned = []

    async def get_series_by_tvdb_id(tvdb_id: int) -> dict[str, Any]:
        return {"id": 7, "monitored": True, "path": existing_path}

    async def get_series_by_id(series_id: int) -> dict[str, Any]:
        return {"id": series_id, "path": existing_path}

    async def update_series_path(series_id: int, new_path: str) -> dict[str, Any]:
        client.updated_to.append((series_id, new_path))
        return {}

    async def rescan_series(series_id: int) -> dict[str, Any]:
        client.rescanned.append(series_id)
        return {}

    client.get_series_by_tvdb_id = get_series_by_tvdb_id
    client.get_series_by_id = get_series_by_id
    client.update_series_path = update_series_path
    client.rescan_series = rescan_series
    return client


@pytest.mark.asyncio
async def test_linking_existing_series_repoints_stale_sonarr_path(tmp_path) -> None:
    """Linking a series already in Sonarr fixes its pre-restructure path.

    This is the reported bug: the series was in Sonarr, the restructurer had
    already moved its files, and linking it in the app left Sonarr pointing at
    the old empty folder — so Sonarr reported nothing downloaded.
    """
    library = tmp_path / "anime"
    (library / "Cowboy Bebop").mkdir(parents=True)

    db = _make_db(str(library), "Cowboy Bebop")
    sonarr = _make_sonarr("/old/tv/Cowboy Bebop")
    resolver = MappingResolver(
        db=db,
        anilist_client=MagicMock(),
        sonarr_client=sonarr,
        config=_make_config(),
    )

    result = await resolver.add_to_sonarr(
        anilist_id=1,
        title="Cowboy Bebop",
        tvdb_id=76885,
        quality_profile_id=1,
        root_folder_path="/old/tv",
    )

    assert result.ok
    assert sonarr.updated_to == [(7, str(library / "Cowboy Bebop"))]
    assert sonarr.rescanned == [7]


@pytest.mark.asyncio
async def test_linking_without_config_skips_path_sync(tmp_path) -> None:
    """Callers that don't supply config keep the previous behaviour."""
    library = tmp_path / "anime"
    (library / "Cowboy Bebop").mkdir(parents=True)

    sonarr = _make_sonarr("/old/tv/Cowboy Bebop")
    resolver = MappingResolver(
        db=_make_db(str(library), "Cowboy Bebop"),
        anilist_client=MagicMock(),
        sonarr_client=sonarr,
    )

    result = await resolver.add_to_sonarr(
        anilist_id=1,
        title="Cowboy Bebop",
        tvdb_id=76885,
        quality_profile_id=1,
        root_folder_path="/old/tv",
    )

    assert result.ok
    assert sonarr.updated_to == []


@pytest.mark.asyncio
async def test_path_sync_failure_does_not_fail_the_link(tmp_path) -> None:
    """A broken path sync must not turn a successful link into an error."""
    library = tmp_path / "anime"
    (library / "Cowboy Bebop").mkdir(parents=True)

    sonarr = _make_sonarr("/old/tv/Cowboy Bebop")

    async def boom(series_id: int, new_path: str) -> dict[str, Any]:
        raise RuntimeError("Sonarr returned 500")

    sonarr.update_series_path = boom

    resolver = MappingResolver(
        db=_make_db(str(library), "Cowboy Bebop"),
        anilist_client=MagicMock(),
        sonarr_client=sonarr,
        config=_make_config(),
    )

    result = await resolver.add_to_sonarr(
        anilist_id=1,
        title="Cowboy Bebop",
        tvdb_id=76885,
        quality_profile_id=1,
        root_folder_path="/old/tv",
    )

    assert result.ok
    assert result.arr_id == 7
