"""Import of manually-acquired media, and the guard on the import path."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from src.Download.ImportProcessor import ImportProcessor, count_media, has_media
from src.Utils.Config import AppConfig, RadarrConfig, SonarrConfig
from src.Web.Routes.Import import _validate_import_path


def _config() -> AppConfig:
    return AppConfig(
        sonarr=SonarrConfig(url="http://s:8989", api_key="k"),
        radarr=RadarrConfig(url="http://r:7878", api_key="k"),
    )


def _db(sonarr_id: int | None = None, radarr_id: int | None = None) -> MagicMock:
    db = MagicMock()

    async def fetch_one(query: str, params: tuple = ()) -> dict[str, Any] | None:
        if "anilist_sonarr_mapping" in query:
            return {"sonarr_id": sonarr_id} if sonarr_id else None
        if "anilist_radarr_mapping" in query:
            return {"radarr_id": radarr_id} if radarr_id else None
        return None

    async def get_setting(key: str) -> str | None:
        return None

    db.fetch_one = fetch_one
    db.get_setting = get_setting
    return db


# ---------------------------------------------------------------------------
# Media detection
# ---------------------------------------------------------------------------


def test_media_detection_walks_subfolders(tmp_path) -> None:
    """A release dropped as Show/Season 1/ep.mkv still counts as media."""
    deep = tmp_path / "Show" / "Season 1"
    deep.mkdir(parents=True)
    (deep / "ep01.mkv").write_text("x")
    (deep / "ep02.mkv").write_text("x")
    (tmp_path / "Show" / "readme.txt").write_text("x")

    assert has_media(str(tmp_path / "Show")) is True
    assert count_media(str(tmp_path / "Show")) == 2


def test_folder_without_video_is_not_media(tmp_path) -> None:
    """Artwork and nfo alone must not look like something to import."""
    d = tmp_path / "Empty"
    d.mkdir()
    (d / "poster.jpg").write_text("x")
    (d / "tvshow.nfo").write_text("x")

    assert has_media(str(d)) is False
    assert count_media(str(d)) == 0


# ---------------------------------------------------------------------------
# Import path guard — the path arrives from the browser
# ---------------------------------------------------------------------------


def _guard_db(root: str) -> MagicMock:
    db = MagicMock()

    async def get_setting(key: str) -> str | None:
        return root if key == "library.import_path" else None

    db.get_setting = get_setting
    return db


@pytest.mark.asyncio
async def test_path_outside_the_import_folder_is_rejected(tmp_path) -> None:
    """A path elsewhere on the host must not be accepted for restructuring."""
    root = tmp_path / "import"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    err = await _validate_import_path(_guard_db(str(root)), str(outside), 1)

    assert err is not None
    assert err.status_code == 400


@pytest.mark.asyncio
async def test_traversal_out_of_the_import_folder_is_rejected(tmp_path) -> None:
    """.. must not escape the configured root."""
    root = tmp_path / "import"
    root.mkdir()
    (tmp_path / "secret").mkdir()

    err = await _validate_import_path(
        _guard_db(str(root)), str(root / ".." / "secret"), 1
    )

    assert err is not None
    assert err.status_code == 400


@pytest.mark.asyncio
async def test_sibling_prefix_is_not_treated_as_inside(tmp_path) -> None:
    """/data/import-old must not pass as being under /data/import."""
    root = tmp_path / "import"
    root.mkdir()
    sneaky = tmp_path / "import-old"
    sneaky.mkdir()

    err = await _validate_import_path(_guard_db(str(root)), str(sneaky), 1)

    assert err is not None


@pytest.mark.asyncio
async def test_path_inside_the_import_folder_is_allowed(tmp_path) -> None:
    root = tmp_path / "import"
    (root / "Some Release").mkdir(parents=True)

    err = await _validate_import_path(
        _guard_db(str(root)), str(root / "Some Release"), 1
    )

    assert err is None


@pytest.mark.asyncio
async def test_missing_anilist_id_is_rejected(tmp_path) -> None:
    root = tmp_path / "import"
    root.mkdir()
    err = await _validate_import_path(_guard_db(str(root)), str(root), 0)
    assert err is not None


# ---------------------------------------------------------------------------
# Telling *arr about the import
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_arr_reports_not_linked(tmp_path) -> None:
    """An unlinked entry imports fine and simply reports that nothing to tell."""
    proc = ImportProcessor(db=_db(), config=_config())
    result = await proc.notify_arr(1)
    assert result["action"] == "not_linked"


@pytest.mark.asyncio
async def test_already_correct_files_still_notify_arr(tmp_path, monkeypatch) -> None:
    """Correctly-placed files that *arr has never seen must still trigger a rescan.

    This is the reported case: the restructure moved the files, so there is
    nothing left to move — but Sonarr still lists the episodes as missing, and
    returning early without telling it left no way to fix that.
    """
    folder = tmp_path / "GATE" / "Season 2"
    folder.mkdir(parents=True)
    (folder / "ep01.mkv").write_text("x")

    notified: list[int] = []

    class _NoOpPlan:
        groups: list = []

    class _Restructurer:
        async def analyze(self, *a, **kw):
            return _NoOpPlan()

        async def execute(self, *a, **kw):  # pragma: no cover - never reached
            raise AssertionError("nothing should be moved")

    async def _from_settings(db, client):
        return _Restructurer()

    import src.Download.ImportProcessor as IP

    monkeypatch.setattr(IP.LibraryRestructurer, "from_settings", _from_settings)

    db = _db()

    async def get_cached_metadata(anilist_id: int):
        return {"title_romaji": "GATE", "year": 2015}

    db.get_cached_metadata = get_cached_metadata

    proc = ImportProcessor(db=db, config=_config())

    async def _fake_notify(anilist_id: int):
        notified.append(anilist_id)
        return {"service": "sonarr", "rescanned": True}

    proc.notify_arr = _fake_notify  # type: ignore[method-assign]

    result = await proc.execute_folder(str(folder), 42)

    assert result["ok"] is True
    assert result["moved"] == 0
    assert notified == [42], "*arr must be told even when nothing moved"
    assert result["arr"]["rescanned"] is True
