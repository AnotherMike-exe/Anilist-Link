"""Tests for AniListClient score mutation and viewer scoreFormat passthrough."""

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from src.Clients.AnilistClient import SET_SCORE_MUTATION, AniListClient


@pytest_asyncio.fixture
async def client() -> AniListClient:
    c = AniListClient(client_id="test-id", client_secret="test-secret")
    c._execute_query = AsyncMock()
    yield c
    await c.close()


@pytest.mark.asyncio
async def test_update_anime_score_sends_correct_variables(
    client: AniListClient,
) -> None:
    client._execute_query.return_value = {"SaveMediaListEntry": {}}

    await client.update_anime_score(42, "token", 8.5)

    client._execute_query.assert_awaited_once_with(
        SET_SCORE_MUTATION,
        {"mediaId": 42, "score": 8.5},
        "token",
        high_priority=True,
    )


@pytest.mark.asyncio
async def test_update_anime_score_returns_save_media_list_entry(
    client: AniListClient,
) -> None:
    client._execute_query.return_value = {
        "SaveMediaListEntry": {"id": 1, "status": "COMPLETED", "score": 8.5}
    }

    result = await client.update_anime_score(42, "token", 8.5)

    assert result == {"id": 1, "status": "COMPLETED", "score": 8.5}


@pytest.mark.asyncio
async def test_get_viewer_score_format_passthrough(client: AniListClient) -> None:
    client._execute_query.return_value = {
        "Viewer": {
            "id": 1,
            "name": "test",
            "mediaListOptions": {"scoreFormat": "POINT_5"},
        }
    }

    viewer = await client.get_viewer("token")

    assert viewer["mediaListOptions"]["scoreFormat"] == "POINT_5"
