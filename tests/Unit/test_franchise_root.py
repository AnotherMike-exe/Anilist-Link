"""Unit tests for resolve_franchise_root_id (PREQUEL-chain walk to base)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.Utils.NamingTranslator import resolve_franchise_root_id


def _relations_map(chain: dict[int, int]):
    """Return a fake fetch_relations_and_tvdb: child -> prequel via `chain`."""

    async def _fetch(anilist_id: int, client):
        prequel = chain.get(anilist_id)
        rels = [("PREQUEL", prequel)] if prequel else []
        return rels, None

    return _fetch


@pytest.mark.asyncio
async def test_walks_prequels_to_base() -> None:
    # movie 3 -> arc 2 -> base 1
    chain = {3: 2, 2: 1}
    with patch(
        "src.Utils.NamingTranslator.fetch_relations_and_tvdb",
        new=AsyncMock(side_effect=_relations_map(chain)),
    ):
        root = await resolve_franchise_root_id(3, None)  # type: ignore[arg-type]
    assert root == 1


@pytest.mark.asyncio
async def test_returns_self_when_no_prequel() -> None:
    with patch(
        "src.Utils.NamingTranslator.fetch_relations_and_tvdb",
        new=AsyncMock(side_effect=_relations_map({})),
    ):
        root = await resolve_franchise_root_id(42, None)  # type: ignore[arg-type]
    assert root == 42


@pytest.mark.asyncio
async def test_stops_on_cycle() -> None:
    # 2 -> 1 -> 2 (cycle); must terminate and not loop forever
    chain = {2: 1, 1: 2}
    with patch(
        "src.Utils.NamingTranslator.fetch_relations_and_tvdb",
        new=AsyncMock(side_effect=_relations_map(chain)),
    ):
        root = await resolve_franchise_root_id(2, None)  # type: ignore[arg-type]
    assert root == 1
