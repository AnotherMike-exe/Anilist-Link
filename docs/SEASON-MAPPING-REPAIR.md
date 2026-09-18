# Repairing Crunchyroll season mis-mappings

A guide for cleaning up AniList entries affected by the season-mapping bug fixed
in the cour-model change. Run this once after deploying; nothing here needs
repeating.

## What went wrong

The season map the Crunchyroll syncer built numbered every **cour** as its own
season. Two things followed from that:

1. **A franchise's later seasons fell off the end of the map.** Mushoku Tensei's
   five TV entries collapsed into two slots, both of them season 1, so a
   Crunchyroll season 3 episode had nowhere to go.
2. **An unplaceable season was folded onto the season 1 entry**, and the raw
   Crunchyroll episode number was written there.

The damage took two shapes, depending on the state of the season 1 entry:

| Season 1 entry was… | What happened | Example |
| --- | --- | --- |
| `CURRENT` or unwatched | The episode number was **written to the wrong entry** | Mushoku Tensei — AniList `108465` (season 1, 11 episodes) pushed to episode 10 by season 3 |
| `COMPLETED` | The write was **declined and silently dropped** — no AniList change, no history row, nothing above DEBUG | Re:Zero — season 4 Part 2 progress never recorded anywhere |

The second shape is why episodes you watched appear nowhere in the Crunchyroll
history: that tab only records writes that actually happened.

## Before you start

- Deploy a build containing the cour model (`Model seasons as cours…`) **and**
  the correction that follows it. The first season-map commit alone mis-maps
  Re:Zero.
- The database migrates itself to schema v5 on startup, adding the table behind
  the new **Unmapped** tab. Check the log for
  `Migration v5 applied: cr_unmapped_episodes created`.
- AniList must be reachable. If the dashboard banner says the API is down, wait —
  the syncer halts during an outage and a scan will do nothing.

## Step 1 — restore progress that was never written

1. Open **Crunchyroll → Preview** and click **New scan**.
2. The scan now resolves seasons it previously could not, so the preview lists
   the writes that were missed. Expect rows for any franchise whose later season
   you were watching — Mushoku Tensei season 3 and Re:Zero season 4 in the
   reference case.
3. Review the rows, then **Approve** and **Apply**.

Every applied row is written to `cr_sync_log`, so anything that looks wrong
afterwards can be reverted individually with **Undo** on the History tab.

> Crunchyroll history is paginated and the scan stops early once a page is almost
> entirely already-synced. If a show you watched a while ago is missing from the
> preview, raise `crunchyroll.max_pages` in Settings and scan again.

## Step 2 — find writes that landed on the wrong entry

1. Open **Crunchyroll → Unmapped**.
2. Click **Run repair scan**.

The scan compares the latest preview run — produced by the fixed matcher, so it
is the authority on where each watched season belongs — against your sync
history. It flags a write when it hit a **different entry of the same series
group** at **exactly the episode number the correct entry should hold**. Both
halves matter: same-franchise alone would flag legitimate rewatches, and the
episode match is what identifies a season number applied to the wrong entry.

This needs no extra Crunchyroll or AniList calls, and reports only — it never
changes anything on its own.

For each row it reports:

1. Note the entry under **Written to** and its progress.
2. Go to **History**, find that entry (the search box filters by title), and
   click **Undo**. That restores the progress and status the entry had before the
   bad write.
3. The undo is sticky for 90 days against that exact target, so a later sync
   will not re-apply the same wrong value.

Do step 1 before step 2. The repair scan reads a preview run, and the undo in
step 2 is scoped to an exact (entry, progress, status) target — so reverting
first and scanning afterwards can leave you undoing a value the correct write
has already superseded.

## Step 3 — deal with anything still unmapped

The **Unmapped** tab lists Crunchyroll history the syncer still cannot place,
with the reason and the seasons the map does cover. Rows appear here instead of
vanishing, and they clear themselves once a season starts resolving.

- **Map to AniList…** — search for the right entry and pick it. The episode
  reached on Crunchyroll is written to that entry, guarded so a `COMPLETED` entry
  is never walked backwards, and recorded in `cr_sync_log` so it stays undoable.
- **Dismiss** — close a row without touching AniList, for history you do not want
  tracked (a series you dropped, a dub listing you do not follow).

An entry whose AniList season structure is genuinely wrong — a missing sequel
relation, say — belongs in **Manual overrides** (`/mappings`) instead, so every
future sync uses your mapping.

## Verifying

After both steps:

- **Unmapped** shows nothing, or only rows you deliberately dismissed.
- **Run repair scan** reports no mis-mapped writes.
- Each affected franchise's airing season carries your real progress on AniList,
  and its season 1 entry is back to whatever it was before.

In the application log a healthy sync no longer prints
`Falling back to Season 1` — that path is gone. Its replacement names the
problem explicitly:

```
No AniList entry for <series> season <n> (season map covers seasons 1, 2, 3)
— refusing to fall back to Season 1
```

A line like that means a row was written to the Unmapped tab rather than a wrong
entry on AniList. Watch the per-run `No matches found:` count too: it now
includes seasons the syncer declined to guess at, so a jump after this deploy is
the guardrail working, not a new failure.

## If a franchise still maps wrong

Season and cour are read from AniList's own titles — a Roman numeral or
`Nth Season` names the season, a bare `Part N` names a cour inside it. A
franchise that labels its entries differently can still land wrong. To report
one, capture:

- the Crunchyroll series title, season number, and episode you watched;
- the AniList ids for the whole franchise;
- the `Season N: …` debug lines the sync logs for that series (set `DEBUG=true`).

Those three together are enough to reproduce the mapping in a unit test; see
`tests/Unit/test_cr_season_mapping.py`, which pins both reference franchises.
