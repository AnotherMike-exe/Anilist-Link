"""Tests for the unmapped-CR-episode repair endpoints.

These are the user-facing fix path for history the sync dropped: pick the AniList
entry a Crunchyroll season belongs to, and the episode reached is written to it
through the same cr_sync_log audit trail as a normal sync (so it stays undoable).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import aiosqlite
import pytest_asyncio

from src.Database.Connection import DatabaseManager
from src.Database.Migrations import run_migrations
from src.Web.Routes.CrunchyrollSync import (
    dismiss_unmapped_episode,
    map_unmapped_episode,
)

USER_ID = "anilist_393115"
REZERO = "Re:ZERO -Starting Life in Another World-"


@pytest_asyncio.fixture
async def db():
    manager = DatabaseManager(db_path=Path(":memory:"))
    manager._db = await aiosqlite.connect(":memory:")
    manager._db.row_factory = aiosqlite.Row
    await run_migrations(manager._db)
    yield manager
    await manager._db.close()


def _request(
    db: DatabaseManager,
    payload: dict[str, Any] | None = None,
    *,
    media: dict[str, Any] | None = None,
    list_entry: dict[str, Any] | None = None,
    update_ok: bool = True,
    users: list[dict[str, Any]] | None = None,
) -> MagicMock:
    """A Request stand-in carrying app.state the handlers read."""
    anilist = MagicMock()
    calls: list[tuple[int, int, str]] = []

    async def get_anime_by_id(anilist_id: int) -> dict[str, Any] | None:
        return media

    async def get_anime_list_entry(*a: Any, **k: Any) -> dict[str, Any] | None:
        return list_entry

    async def update_anime_progress(
        anilist_id: int, token: str, progress: int, status: str
    ) -> dict[str, Any] | None:
        calls.append((anilist_id, progress, status))
        return {"id": anilist_id} if update_ok else None

    anilist.get_anime_by_id = get_anime_by_id
    anilist.get_anime_list_entry = get_anime_list_entry
    anilist.update_anime_progress = update_anime_progress

    async def get_users_by_service(service: str) -> list[dict[str, Any]]:
        if users is not None:
            return users
        return [{"user_id": USER_ID, "access_token": "tok", "anilist_id": 393115}]

    db.get_users_by_service = get_users_by_service  # type: ignore[method-assign]

    request = MagicMock()
    request.app.state.db = db
    request.app.state.anilist_client = anilist

    async def _json() -> dict[str, Any]:
        return payload or {}

    request.json = _json
    request.updates = calls
    return request


async def _seed(db: DatabaseManager, season: int = 5, episode: int = 3) -> int:
    await db.record_cr_unmapped(
        USER_ID, REZERO, season, episode, "unknown_season", "season map covers 1-4"
    )
    return (await db.get_cr_unmapped(USER_ID))[0]["id"]


def _body(response: Any) -> dict[str, Any]:
    return json.loads(response.body)


class TestMapUnmappedEpisode:
    async def test_writes_progress_and_closes_the_report(
        self, db: DatabaseManager
    ) -> None:
        row_id = await _seed(db, season=5, episode=3)
        request = _request(
            db,
            {"anilist_id": 189046},
            media={
                "id": 189046,
                "episodes": 19,
                "title": {"romaji": "Re:Zero … 4th Season"},
            },
            list_entry={"progress": 0, "status": "PLANNING"},
        )
        response = await map_unmapped_episode(request, row_id)
        body = _body(response)

        assert body["ok"] is True
        assert body["progress"] == 3
        assert body["status"] == "CURRENT"
        assert request.updates == [(189046, 3, "CURRENT")]
        assert await db.get_cr_unmapped(USER_ID) == []

    async def test_writes_an_undoable_audit_row(self, db: DatabaseManager) -> None:
        """The fix must be undoable from History like any other sync write."""
        row_id = await _seed(db, episode=3)
        request = _request(
            db,
            {"anilist_id": 189046},
            media={"id": 189046, "episodes": 19, "title": {"romaji": "Re:Zero S4"}},
            list_entry={"progress": 1, "status": "CURRENT"},
        )
        await map_unmapped_episode(request, row_id)

        log = await db.get_cr_sync_log(user_id=USER_ID)
        assert len(log) == 1
        assert log[0]["anilist_id"] == 189046
        assert log[0]["before_progress"] == 1
        assert log[0]["after_progress"] == 3
        assert log[0]["undone_at"] is None

    async def test_completes_the_entry_at_its_final_episode(
        self, db: DatabaseManager
    ) -> None:
        row_id = await _seed(db, episode=19)
        request = _request(
            db,
            {"anilist_id": 189046},
            media={"id": 189046, "episodes": 19, "title": {"romaji": "Re:Zero S4"}},
            list_entry={"progress": 5, "status": "CURRENT"},
        )
        body = _body(await map_unmapped_episode(request, row_id))
        assert body["status"] == "COMPLETED"

    async def test_unknown_episode_count_stays_current(
        self, db: DatabaseManager
    ) -> None:
        """An airing entry has no total, so it must never be marked COMPLETED."""
        row_id = await _seed(db, episode=11)
        request = _request(
            db,
            {"anilist_id": 178789},
            media={"id": 178789, "episodes": None, "title": {"romaji": "Mushoku III"}},
            list_entry={"progress": 0, "status": "PLANNING"},
        )
        body = _body(await map_unmapped_episode(request, row_id))
        assert body["status"] == "CURRENT"

    async def test_caller_can_override_the_episode(self, db: DatabaseManager) -> None:
        row_id = await _seed(db, episode=3)
        request = _request(
            db,
            {"anilist_id": 189046, "episode": 7},
            media={"id": 189046, "episodes": 19, "title": {"romaji": "Re:Zero S4"}},
            list_entry={"progress": 0, "status": "PLANNING"},
        )
        body = _body(await map_unmapped_episode(request, row_id))
        assert body["progress"] == 7
        assert request.updates == [(189046, 7, "CURRENT")]

    async def test_completed_entry_is_never_walked_backwards(
        self, db: DatabaseManager
    ) -> None:
        """Same guard the syncers use — a finished show must not regress."""
        row_id = await _seed(db, episode=3)
        request = _request(
            db,
            {"anilist_id": 21355},
            media={"id": 21355, "episodes": 25, "title": {"romaji": "Re:Zero"}},
            list_entry={"progress": 25, "status": "COMPLETED"},
        )
        body = _body(await map_unmapped_episode(request, row_id))

        assert body["ok"] is True
        assert body["skipped"] is True
        assert request.updates == []
        # Closed anyway — the report has been dealt with.
        assert await db.get_cr_unmapped(USER_ID) == []

    async def test_missing_anilist_id_is_rejected(self, db: DatabaseManager) -> None:
        row_id = await _seed(db)
        response = await map_unmapped_episode(_request(db, {}), row_id)
        assert response.status_code == 400
        assert await db.get_cr_unmapped(USER_ID)

    async def test_unknown_row_is_a_404(self, db: DatabaseManager) -> None:
        response = await map_unmapped_episode(
            _request(db, {"anilist_id": 189046}), 9999
        )
        assert response.status_code == 404

    async def test_already_resolved_row_is_rejected(self, db: DatabaseManager) -> None:
        row_id = await _seed(db)
        await db.resolve_cr_unmapped(row_id, 189046)
        response = await map_unmapped_episode(
            _request(db, {"anilist_id": 189046}), row_id
        )
        assert response.status_code == 400

    async def test_missing_user_is_a_404(self, db: DatabaseManager) -> None:
        row_id = await _seed(db)
        response = await map_unmapped_episode(
            _request(db, {"anilist_id": 189046}, users=[]), row_id
        )
        assert response.status_code == 404

    async def test_failed_anilist_write_leaves_the_report_open(
        self, db: DatabaseManager
    ) -> None:
        """A failed write must not be reported as fixed."""
        row_id = await _seed(db)
        request = _request(
            db,
            {"anilist_id": 189046},
            media={"id": 189046, "episodes": 19, "title": {"romaji": "Re:Zero S4"}},
            list_entry={"progress": 0, "status": "PLANNING"},
            update_ok=False,
        )
        response = await map_unmapped_episode(request, row_id)
        assert response.status_code == 500
        assert await db.get_cr_unmapped(USER_ID)
        assert await db.get_cr_sync_log(user_id=USER_ID) == []

    async def test_negative_episode_is_rejected(self, db: DatabaseManager) -> None:
        row_id = await _seed(db)
        response = await map_unmapped_episode(
            _request(db, {"anilist_id": 189046, "episode": -1}), row_id
        )
        assert response.status_code == 400


class TestDismissUnmappedEpisode:
    async def test_closes_the_report_without_writing(self, db: DatabaseManager) -> None:
        row_id = await _seed(db)
        request = _request(db)
        body = _body(await dismiss_unmapped_episode(request, row_id))

        assert body["ok"] is True
        assert await db.get_cr_unmapped(USER_ID) == []
        assert await db.get_cr_sync_log(user_id=USER_ID) == []
        row = await db.get_cr_unmapped_entry(row_id)
        assert row is not None
        assert row["resolved_anilist_id"] is None

    async def test_unknown_row_is_a_404(self, db: DatabaseManager) -> None:
        response = await dismiss_unmapped_episode(_request(db), 9999)
        assert response.status_code == 404
