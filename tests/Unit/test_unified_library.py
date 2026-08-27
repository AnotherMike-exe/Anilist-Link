"""Unit tests for UnifiedLibrary aggregation (virtual series-group entries)."""

from __future__ import annotations

from src.Web.Routes.UnifiedLibrary import _aggregate_by_anilist_id


def _entry(**kw):
    base = {
        "sources": ["local"],
        "anilist_id": 112151,
        "cover_url": None,
        "match_confidence": 1.0,
        "match_method": "manual",
        "virtual": False,
        "episodes": None,
        "folder_path": "",
        "folder_name": "",
        "source_id": "",
        "title_romaji": "Movie",
    }
    base.update(kw)
    return base


def test_real_row_overrides_virtual_episodes_and_path() -> None:
    """A real on-disk row's episode count + path win over a virtual expansion."""
    virtual = _entry(
        virtual=True,
        episodes=26,  # leaked from the S1 parent
        folder_path="/media/anime/Kimetsu no Yaiba (2019)",
        folder_name="Kimetsu no Yaiba (2019)",
    )
    real = _entry(
        virtual=False,
        episodes=1,  # the movie's own count
        folder_path="/media/anime/Kimetsu no Yaiba (2019)/Mugen Ressha-hen (2020)",
        folder_name="Mugen Ressha-hen (2020)",
    )
    # Virtual is seen first (the bug ordering)
    out = _aggregate_by_anilist_id([virtual, real])
    assert len(out) == 1
    card = out[0]
    assert card["episodes"] == 1
    assert card["folder_path"].endswith("Mugen Ressha-hen (2020)")
    assert card["virtual"] is False


def test_virtual_only_keeps_its_own_episodes() -> None:
    """With no real row, the virtual entry keeps whatever episodes it was given."""
    virtual = _entry(virtual=True, episodes=1, folder_path="/x")
    out = _aggregate_by_anilist_id([virtual])
    assert out[0]["episodes"] == 1
    assert out[0]["virtual"] is True
