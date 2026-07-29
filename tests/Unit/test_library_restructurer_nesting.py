"""Unit tests for restructurer franchise-root folder rendering."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from src.Scanner.LibraryRestructurer import LibraryRestructurer


def _restructurer(db: Any, folder_template: str = "{title} ({year})",
                  title_pref: str = "romaji") -> LibraryRestructurer:
    return LibraryRestructurer(
        db=db,
        group_builder=MagicMock(),
        folder_template=folder_template,
        title_pref=title_pref,
    )


def _db_with_group(display_title: str, root_id: int, cache: dict | None) -> MagicMock:
    db = MagicMock()

    async def fetch_one(query: str, params: tuple = ()) -> dict[str, Any]:
        return {"display_title": display_title, "root_anilist_id": root_id}

    async def get_cached_metadata(anilist_id: int) -> dict[str, Any] | None:
        return cache

    db.fetch_one = fetch_one
    db.get_cached_metadata = get_cached_metadata
    return db


@pytest.mark.asyncio
async def test_root_folder_uses_year_and_romaji() -> None:
    db = _db_with_group(
        "Demon Slayer",
        101,
        {"title_romaji": "Kimetsu no Yaiba", "title_english": "Demon Slayer",
         "year": 2019},
    )
    r = _restructurer(db)
    assert await r._render_group_root_folder(5) == "Kimetsu no Yaiba (2019)"


@pytest.mark.asyncio
async def test_root_folder_english_pref() -> None:
    db = _db_with_group(
        "Demon Slayer",
        101,
        {"title_romaji": "Kimetsu no Yaiba", "title_english": "Demon Slayer",
         "year": 2019},
    )
    r = _restructurer(db, title_pref="english")
    assert await r._render_group_root_folder(5) == "Demon Slayer (2019)"


@pytest.mark.asyncio
async def test_root_folder_falls_back_to_display_title_without_cache() -> None:
    db = _db_with_group("Demon Slayer", 101, None)
    r = _restructurer(db, folder_template="{title}")
    assert await r._render_group_root_folder(5) == "Demon Slayer"


@pytest.mark.asyncio
async def test_root_folder_empty_when_group_missing() -> None:
    db = MagicMock()

    async def fetch_one(query: str, params: tuple = ()) -> None:
        return None

    db.fetch_one = fetch_one
    r = _restructurer(db)
    assert await r._render_group_root_folder(999) == ""
