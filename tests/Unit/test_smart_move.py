"""Unit tests for the Smart Move helper logic."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.Web.Routes.SmartMove import (
    _build_show_input,
    _library_root_for,
    _validate,
)


@pytest.mark.asyncio
async def test_library_root_prefers_ancestor_path() -> None:
    db = MagicMock()

    async def get_library(library_id: int) -> dict[str, Any]:
        return {"paths": json.dumps(["/anime", "/movies"])}

    db.get_library = get_library
    item = {"library_id": 1, "folder_path": "/anime/Mugen Train"}
    assert await _library_root_for(db, item) == "/anime"


@pytest.mark.asyncio
async def test_library_root_falls_back_to_parent() -> None:
    db = MagicMock()

    async def get_library(library_id: int) -> dict[str, Any]:
        return {"paths": json.dumps(["/somewhere/else"])}

    db.get_library = get_library
    item = {"library_id": 1, "folder_path": "/anime/Mugen Train"}
    # No configured root is an ancestor → parent of the item folder
    assert await _library_root_for(db, item) == "/anime"


@pytest.mark.asyncio
async def test_build_show_input_pulls_year_from_cache() -> None:
    db = MagicMock()

    async def get_cached_metadata(anilist_id: int) -> dict[str, Any]:
        return {
            "title_romaji": "Kimetsu no Yaiba: Mugen Ressha-hen",
            "title_english": "Demon Slayer: Mugen Train",
            "year": 2020,
            "episodes": 1,
        }

    db.get_cached_metadata = get_cached_metadata
    item = {
        "folder_path": "/anime/Mugen Train",
        "folder_name": "Mugen Train",
        "anilist_id": 55555,
        "anilist_title": "Mugen Train",
        "anilist_format": "MOVIE",
        "year": 0,
    }
    show = await _build_show_input(db, item)
    assert show.anilist_id == 55555
    assert show.local_path == "/anime/Mugen Train"
    assert show.source_id == "/anime/Mugen Train"
    assert show.year == 2020
    assert show.anilist_format == "MOVIE"


def test_validate_rejects_missing_item() -> None:
    assert _validate(None) is not None
    assert _validate({"folder_path": ""}) is not None


def test_validate_rejects_unmatched_item() -> None:
    resp = _validate({"folder_path": "/anime/X", "anilist_id": 0})
    assert resp is not None


def test_validate_rejects_missing_folder_on_disk() -> None:
    resp = _validate({"folder_path": "/no/such/dir", "anilist_id": 42})
    assert resp is not None


def test_validate_passes_for_real_dir(tmp_path) -> None:
    resp = _validate({"folder_path": str(tmp_path), "anilist_id": 42})
    assert resp is None
