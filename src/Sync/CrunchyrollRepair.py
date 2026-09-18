"""Find Crunchyroll sync writes that landed on the wrong AniList entry.

Before the season map understood cours, a Crunchyroll season with no slot was
folded onto the franchise's season 1 entry, and the raw CR episode number was
written there. Mushoku Tensei season 3 episode 10 became entry 108465 — season
1, 11 episodes — at progress 10.

That damage cannot be spotted from the database alone: 108465 at episode 10 is a
perfectly legal state for someone actually watching season 1. What makes it wrong
is *which season the user was really watching*, and only Crunchyroll history
knows that. So detection cross-references three things:

* what CR history says was watched, per (series, season);
* where the current season map says each of those belongs;
* which AniList entries the sync actually wrote to (``cr_sync_log``).

A write is suspect when it hit a *different* entry of the same franchise at
exactly the episode number the correct entry should have received — the
signature of an episode number applied to the wrong season.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CorrectTarget:
    """Where a watched (series, CR season) belongs under the current season map."""

    anilist_id: int
    episode: int


@dataclass
class SuspectWrite:
    """An active cr_sync_log row that looks like a mis-mapped season."""

    log_id: int
    anilist_id: int
    show_title: str
    after_progress: int
    after_status: str
    applied_at: str
    before_progress: int
    before_status: str
    series_title: str
    cr_season: int
    correct_anilist_id: int
    correct_episode: int
    reason: str = field(default="")

    def as_dict(self) -> dict[str, Any]:
        return {
            "log_id": self.log_id,
            "anilist_id": self.anilist_id,
            "show_title": self.show_title,
            "after_progress": self.after_progress,
            "after_status": self.after_status,
            "applied_at": self.applied_at,
            "before_progress": self.before_progress,
            "before_status": self.before_status,
            "series_title": self.series_title,
            "cr_season": self.cr_season,
            "correct_anilist_id": self.correct_anilist_id,
            "correct_episode": self.correct_episode,
            "reason": self.reason,
        }


def find_suspect_writes(
    correct_targets: dict[tuple[str, int], CorrectTarget],
    group_members: dict[int, set[int]],
    log_rows: list[dict[str, Any]],
) -> list[SuspectWrite]:
    """Return active sync writes that look like a mis-mapped CR season.

    ``correct_targets`` maps a watched (series title, CR season) to the entry and
    episode the current season map resolves it to. ``group_members`` maps an
    AniList id to every id in its series group, so "same franchise" is decided by
    the relation graph rather than by title similarity. ``log_rows`` are
    ``cr_sync_log`` rows; already-undone rows are ignored.

    A row is reported when it wrote to a *sibling* of the correct entry at
    exactly the episode the correct entry should hold. Both halves matter: the
    sibling check keeps unrelated shows out, and the episode check is what
    separates a mis-applied season number from someone genuinely rewatching an
    earlier season.
    """
    suspects: list[SuspectWrite] = []
    seen_log_ids: set[int] = set()

    for (series_title, cr_season), target in correct_targets.items():
        family = group_members.get(target.anilist_id) or {target.anilist_id}

        for row in log_rows:
            if row.get("undone_at"):
                continue

            log_id = int(row.get("id") or 0)
            anilist_id = int(row.get("anilist_id") or 0)
            if not log_id or log_id in seen_log_ids:
                continue
            if anilist_id == target.anilist_id:
                # Landed where it belongs.
                continue
            if anilist_id not in family:
                continue
            if int(row.get("after_progress") or 0) != target.episode:
                continue

            suspects.append(
                SuspectWrite(
                    log_id=log_id,
                    anilist_id=anilist_id,
                    show_title=str(row.get("show_title") or ""),
                    after_progress=int(row.get("after_progress") or 0),
                    after_status=str(row.get("after_status") or ""),
                    applied_at=str(row.get("applied_at") or ""),
                    before_progress=int(row.get("before_progress") or 0),
                    before_status=str(row.get("before_status") or ""),
                    series_title=series_title,
                    cr_season=cr_season,
                    correct_anilist_id=target.anilist_id,
                    correct_episode=target.episode,
                    reason=(
                        f"Crunchyroll {series_title} season {cr_season} episode "
                        f"{target.episode} belongs to AniList {target.anilist_id}, "
                        f"but episode {target.episode} was written to "
                        f"{anilist_id} in the same series group"
                    ),
                )
            )
            seen_log_ids.add(log_id)

    suspects.sort(key=lambda s: (s.series_title, s.cr_season, s.log_id))
    return suspects
