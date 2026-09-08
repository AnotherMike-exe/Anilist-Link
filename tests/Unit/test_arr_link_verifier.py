"""Unit tests for ArrLinkVerifier — removal detection and path re-checking."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.Download.ArrLinkVerifier import ArrLinkVerifier
from src.Utils.Config import AppConfig, RadarrConfig, SonarrConfig


def _config() -> AppConfig:
    return AppConfig(
        sonarr=SonarrConfig(url="http://sonarr:8989", api_key="k"),
        radarr=RadarrConfig(url="http://radarr:7878", api_key="k"),
    )


def _db(
    library_path: str,
    *,
    sonarr_id: int | None = 7,
    radarr_id: int | None = None,
    title: str = "Cowboy Bebop",
) -> MagicMock:
    db = MagicMock()
    db.executed: list[tuple[str, tuple]] = []

    async def execute(query: str, params: tuple = ()) -> None:
        db.executed.append((query, params))

    async def fetch_one(query: str, params: tuple = ()) -> dict[str, Any] | None:
        if "anilist_sonarr_mapping" in query and "in_sonarr=1" in query:
            return {"sonarr_id": sonarr_id} if sonarr_id else None
        if "anilist_radarr_mapping" in query and "in_radarr=1" in query:
            return {"radarr_id": radarr_id} if radarr_id else None
        return None

    async def fetch_all(query: str, params: tuple = ()) -> list[dict[str, Any]]:
        return []

    async def get_setting(key: str) -> str | None:
        return "romaji" if key == "app.title_display" else None

    async def get_all_libraries() -> list[dict[str, Any]]:
        return [{"id": 1, "name": "Anime", "paths": json.dumps([library_path])}]

    async def get_cached_metadata(anilist_id: int) -> dict[str, Any]:
        return {"title_romaji": title}

    async def get_users_by_service(service: str) -> list:
        return []

    async def get_series_group_by_anilist_id(anilist_id: int) -> None:
        return None

    db.execute = execute
    db.fetch_one = fetch_one
    db.fetch_all = fetch_all
    db.get_setting = get_setting
    db.get_all_libraries = get_all_libraries
    db.get_cached_metadata = get_cached_metadata
    db.get_users_by_service = get_users_by_service
    db.get_series_group_by_anilist_id = get_series_group_by_anilist_id
    return db


def _sonarr(series: dict[str, Any] | None, *, raises: Exception | None = None):
    client = MagicMock()
    client.updated_to = []

    async def get_series_by_id(series_id: int):
        if raises:
            raise raises
        return series

    async def update_series_path(series_id: int, new_path: str):
        client.updated_to.append((series_id, new_path))
        return {}

    async def rescan_series(series_id: int):
        return {}

    client.get_series_by_id = get_series_by_id
    client.update_series_path = update_series_path
    client.rescan_series = rescan_series
    return client


@pytest.mark.asyncio
async def test_series_deleted_in_sonarr_clears_the_link(tmp_path) -> None:
    """The reported gap: a series removed in Sonarr must stop showing as tracked."""
    db = _db(str(tmp_path / "anime"))
    verifier = ArrLinkVerifier(db=db, config=_config())

    result = await verifier.verify_entry(1, sonarr=_sonarr(None))

    assert result.removed is True
    assert result.still_present is False
    # in_sonarr=0 is what every read path filters on, so the UI now offers Add.
    updates = [q for q, _ in db.executed if "in_sonarr=0" in q]
    assert len(updates) == 1
    assert "No longer in Sonarr" in result.summary()


@pytest.mark.asyncio
async def test_transport_error_never_clears_the_link(tmp_path) -> None:
    """A Sonarr outage must not be mistaken for a deletion."""
    db = _db(str(tmp_path / "anime"))
    verifier = ArrLinkVerifier(db=db, config=_config())

    client = _sonarr(None, raises=RuntimeError("connection refused"))
    result = await verifier.verify_entry(1, sonarr=client)

    assert result.removed is False
    assert "connection refused" in result.error
    assert db.executed == []  # nothing cleared


@pytest.mark.asyncio
async def test_still_present_entry_gets_its_stale_path_corrected(tmp_path) -> None:
    """The other reported gap: fix the path of an already-linked entry."""
    library = tmp_path / "anime"
    (library / "Cowboy Bebop").mkdir(parents=True)

    db = _db(str(library))
    verifier = ArrLinkVerifier(db=db, config=_config())
    client = _sonarr({"id": 7, "path": "/old/tv/Cowboy Bebop"})

    result = await verifier.verify_entry(1, sonarr=client)

    assert result.removed is False
    assert result.path_action == "updated"
    assert result.changed is True
    assert client.updated_to == [(7, str(library / "Cowboy Bebop"))]


@pytest.mark.asyncio
async def test_correct_entry_reports_no_change(tmp_path) -> None:
    """A healthy link reports cleanly and changes nothing."""
    library = tmp_path / "anime"
    show = library / "Cowboy Bebop"
    show.mkdir(parents=True)

    db = _db(str(library))
    verifier = ArrLinkVerifier(db=db, config=_config())
    client = _sonarr({"id": 7, "path": str(show)})

    result = await verifier.verify_entry(1, sonarr=client)

    assert result.path_action == "already_correct"
    assert result.changed is False
    assert client.updated_to == []


@pytest.mark.asyncio
async def test_unlinked_entry_is_a_noop(tmp_path) -> None:
    """An entry with no mapping reports 'not linked' and touches nothing."""
    db = _db(str(tmp_path / "anime"), sonarr_id=None)
    verifier = ArrLinkVerifier(db=db, config=_config())

    result = await verifier.verify_entry(1)

    assert result.linked is False
    assert result.changed is False
    assert db.executed == []


@pytest.mark.asyncio
async def test_missing_library_folder_leaves_the_path_alone(tmp_path) -> None:
    """Safety carried over: never repoint at a folder that isn't there."""
    library = tmp_path / "anime"
    library.mkdir(parents=True)

    db = _db(str(library))
    verifier = ArrLinkVerifier(db=db, config=_config())
    client = _sonarr({"id": 7, "path": "/old/tv/Cowboy Bebop"})

    result = await verifier.verify_entry(1, sonarr=client)

    assert result.path_action == "target_missing"
    assert client.updated_to == []
    assert "no library folder was found" in result.summary()
