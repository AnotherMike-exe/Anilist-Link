"""Tests for the repair-scan endpoint and its input builders.

The scan reads a preview run (produced by the current matcher) as the authority
on where each watched Crunchyroll season belongs, then checks the sync log for
writes that landed on a sibling entry at that same episode number.
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
    _build_correct_targets,
    _build_group_members,
    repair_scan,
)

USER_ID = "anilist_393115"
MUSHOKU = "Mushoku Tensei: Jobless Reincarnation"
RUN = "run-1"


@pytest_asyncio.fixture
async def db():
    manager = DatabaseManager(db_path=Path(":memory:"))
    manager._db = await aiosqlite.connect(":memory:")
    manager._db.row_factory = aiosqlite.Row
    await run_migrations(manager._db)
    yield manager
    await manager._db.close()


async def _seed_group(db: DatabaseManager, root: int, members: list[int]) -> int:
    group_id = await db.upsert_series_group(
        root_anilist_id=root, display_title="G", entry_count=len(members)
    )
    for order, anilist_id in enumerate(members, 1):
        await db.upsert_series_group_entry(
            group_id=group_id,
            anilist_id=anilist_id,
            season_order=order,
            display_title=f"Entry {anilist_id}",
            title_romaji=f"Entry {anilist_id}",
            title_english="",
            format="TV",
            episodes=12,
            start_date="2021-01-01",
        )
    return group_id


async def _seed_preview(
    db: DatabaseManager,
    anilist_id: int,
    progress: int,
    cr_season: int,
    cr_title: str = MUSHOKU,
) -> None:
    await db.insert_cr_preview_rows(
        [
            {
                "user_id": USER_ID,
                "run_id": RUN,
                "cr_title": cr_title,
                "anilist_id": anilist_id,
                "anilist_title": "T",
                "confidence": 0.99,
                "proposed_status": "CURRENT",
                "proposed_progress": progress,
                "current_status": "",
                "current_progress": 0,
                "action": "update",
                "episodes_json": json.dumps(
                    [{"cr_season": cr_season, "cr_episode": progress}]
                ),
            }
        ]
    )


def _request(db: DatabaseManager, users: list[dict[str, Any]] | None = None):
    async def get_users_by_service(service: str) -> list[dict[str, Any]]:
        if users is not None:
            return users
        return [{"user_id": USER_ID, "access_token": "t", "anilist_id": 1}]

    db.get_users_by_service = get_users_by_service  # type: ignore[method-assign]
    request = MagicMock()
    request.app.state.db = db
    return request


def _body(response: Any) -> dict[str, Any]:
    return json.loads(response.body)


class TestBuildCorrectTargets:
    async def test_reads_season_and_episode_from_a_preview_row(
        self, db: DatabaseManager
    ) -> None:
        await _seed_preview(db, 178789, 10, cr_season=3)
        targets = await _build_correct_targets(db, RUN, USER_ID)
        assert (MUSHOKU, 3) in targets
        assert targets[(MUSHOKU, 3)].anilist_id == 178789
        assert targets[(MUSHOKU, 3)].episode == 10

    async def test_row_with_broken_episodes_json_still_yields_a_target(
        self, db: DatabaseManager
    ) -> None:
        """A malformed blob must not lose the row — it falls back to season 0."""
        await db.insert_cr_preview_rows(
            [
                {
                    "user_id": USER_ID,
                    "run_id": RUN,
                    "cr_title": MUSHOKU,
                    "anilist_id": 178789,
                    "anilist_title": "T",
                    "confidence": 1.0,
                    "proposed_status": "CURRENT",
                    "proposed_progress": 10,
                    "current_status": "",
                    "current_progress": 0,
                    "action": "update",
                    "episodes_json": "{not json",
                }
            ]
        )
        targets = await _build_correct_targets(db, RUN, USER_ID)
        assert targets[(MUSHOKU, 0)].anilist_id == 178789

    async def test_rows_without_an_anilist_id_are_skipped(
        self, db: DatabaseManager
    ) -> None:
        await _seed_preview(db, 0, 10, cr_season=3)
        assert await _build_correct_targets(db, RUN, USER_ID) == {}

    async def test_empty_run_yields_no_targets(self, db: DatabaseManager) -> None:
        assert await _build_correct_targets(db, "missing-run", USER_ID) == {}


class TestBuildGroupMembers:
    async def test_every_member_maps_to_the_whole_family(
        self, db: DatabaseManager
    ) -> None:
        await _seed_group(db, 108465, [108465, 127720, 178789])
        members = await _build_group_members(db, {178789})
        assert members[178789] == {108465, 127720, 178789}
        assert members[108465] == {108465, 127720, 178789}

    async def test_entry_with_no_group_maps_to_itself(
        self, db: DatabaseManager
    ) -> None:
        members = await _build_group_members(db, {999999})
        assert members[999999] == {999999}

    async def test_two_franchises_stay_separate(self, db: DatabaseManager) -> None:
        await _seed_group(db, 108465, [108465, 178789])
        await _seed_group(db, 21355, [21355, 189046])
        members = await _build_group_members(db, {178789, 189046})
        assert members[178789] == {108465, 178789}
        assert members[189046] == {21355, 189046}


class TestRepairScanEndpoint:
    async def test_flags_the_mushoku_mis_write(self, db: DatabaseManager) -> None:
        """The reference case: season 3 episode 10 written to season 1 (108465)."""
        await _seed_group(db, 108465, [108465, 127720, 178789])
        await _seed_preview(db, 178789, 10, cr_season=3)
        await db.insert_cr_sync_log_entry(
            user_id=USER_ID,
            anilist_id=108465,
            show_title="Mushoku Tensei",
            before_status="CURRENT",
            before_progress=3,
            after_status="CURRENT",
            after_progress=10,
            sync_run_id="old-run",
        )

        body = _body(await repair_scan(_request(db)))

        assert body["ok"] is True
        assert body["checked"] == 1
        assert len(body["suspects"]) == 1
        suspect = body["suspects"][0]
        assert suspect["anilist_id"] == 108465
        assert suspect["correct_anilist_id"] == 178789
        assert suspect["correct_episode"] == 10

    async def test_correct_write_is_not_flagged(self, db: DatabaseManager) -> None:
        await _seed_group(db, 108465, [108465, 127720, 178789])
        await _seed_preview(db, 178789, 10, cr_season=3)
        await db.insert_cr_sync_log_entry(
            user_id=USER_ID,
            anilist_id=178789,
            show_title="Mushoku Tensei III",
            before_status="",
            before_progress=0,
            after_status="CURRENT",
            after_progress=10,
            sync_run_id="new-run",
        )
        body = _body(await repair_scan(_request(db)))
        assert body["suspects"] == []

    async def test_undone_write_is_not_flagged_again(self, db: DatabaseManager) -> None:
        """Once the user reverts a row it must drop out of the report."""
        await _seed_group(db, 108465, [108465, 178789])
        await _seed_preview(db, 178789, 10, cr_season=3)
        log_id = await db.insert_cr_sync_log_entry(
            user_id=USER_ID,
            anilist_id=108465,
            show_title="Mushoku Tensei",
            before_status="CURRENT",
            before_progress=3,
            after_status="CURRENT",
            after_progress=10,
            sync_run_id="old-run",
        )
        await db.mark_cr_sync_log_undone(log_id)
        body = _body(await repair_scan(_request(db)))
        assert body["suspects"] == []

    async def test_no_preview_run_explains_what_to_do(
        self, db: DatabaseManager
    ) -> None:
        response = await repair_scan(_request(db))
        assert response.status_code == 400
        assert "preview" in _body(response)["error"].lower()

    async def test_no_linked_account_is_rejected(self, db: DatabaseManager) -> None:
        response = await repair_scan(_request(db, users=[]))
        assert response.status_code == 400

    async def test_explicit_run_id_is_honoured(self, db: DatabaseManager) -> None:
        await _seed_group(db, 108465, [108465, 178789])
        await _seed_preview(db, 178789, 10, cr_season=3)
        body = _body(await repair_scan(_request(db), run_id=RUN))
        assert body["run_id"] == RUN
