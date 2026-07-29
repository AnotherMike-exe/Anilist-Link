"""Unit tests for prune_orphaned_dir — orphan cleanup after a move."""

from __future__ import annotations

import os

from src.Scanner.LibraryRestructurer import prune_orphaned_dir


def _touch(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("x")


def test_prunes_media_free_folder_with_support_files(tmp_path) -> None:
    """An old movie folder left with only nfo/artwork is removed."""
    old = tmp_path / "Infinity Castle"
    _touch(str(old / "poster.jpg"))
    _touch(str(old / "movie.nfo"))
    removed = prune_orphaned_dir(str(old), protected=[str(tmp_path)])
    assert removed is True
    assert not old.exists()


def test_keeps_folder_that_still_has_media(tmp_path) -> None:
    """A folder that still holds a video file is never removed."""
    old = tmp_path / "Some Show"
    _touch(str(old / "episode.mkv"))
    _touch(str(old / "poster.jpg"))
    removed = prune_orphaned_dir(str(old), protected=[str(tmp_path)])
    assert removed is False
    assert old.exists()


def test_never_removes_protected_target(tmp_path) -> None:
    """The move target (protected) is never deleted even if media-free."""
    target = tmp_path / "Demon Slayer" / "Infinity Castle"
    _touch(str(target / "poster.jpg"))
    removed = prune_orphaned_dir(str(target), protected=[str(target)])
    assert removed is False
    assert target.exists()


def test_never_removes_ancestor_of_protected(tmp_path) -> None:
    """A dir that is an ancestor of the target is never deleted."""
    root = tmp_path / "anime"
    target = root / "Demon Slayer" / "Infinity Castle"
    _touch(str(target / "video.mkv"))
    # root has no direct media but contains the target — must be protected
    removed = prune_orphaned_dir(str(root), protected=[str(target)])
    assert removed is False
    assert root.exists()


def test_missing_dir_is_noop(tmp_path) -> None:
    removed = prune_orphaned_dir(str(tmp_path / "does-not-exist"), protected=[])
    assert removed is False
