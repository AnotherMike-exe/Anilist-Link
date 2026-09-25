# Architecture — Anilist-Link

How Anilist-Link is built, and why. `README.md` says what it does. This file says what
somebody must understand before they change it. Update it in the same change as the
code it describes.

**Last update**: 2026-09-25 (v1.0.0)

---

## 1. Shape

Anilist-Link is one Python process in one Docker container. The container is for a
home server, for example Unraid. FastAPI serves a server-rendered dashboard on port
9876. APScheduler and a small number of asyncio loops run the background work in the
same process. All state is in one SQLite file in `/config`. The app connects AniList
with media servers (Plex, Jellyfin), Crunchyroll, and download managers (Sonarr,
Radarr).

The project replaces Crunchyroll-Anilist-Sync, an earlier container by the same
author that did only the Crunchyroll-to-AniList sync.

### 1.1. The four pillars

The features are in four pillars. The pillar numbers are historical. The build order
was P2, P3, P1, then P4.

| # | Pillar | Summary | Build order | Status |
|---|--------|---------|----------|--------|
| 2 | **File Organization** | Rename and reorganize anime files into a standard folder structure with AniList data | 1st | Complete (L1/L2/L3) |
| 3 | **Metadata from AniList** | Write AniList metadata (titles, descriptions, posters, genres, ratings) to Plex and Jellyfin | 2nd | Complete (Plex and Jellyfin) |
| 1 | **Watch Status Sync** | Sync watch progress between Crunchyroll, Plex, Jellyfin and AniList | 3rd | Complete (Crunchyroll, Plex, Jellyfin) |
| 4 | **Download Management** | Send add requests to Sonarr and Radarr with AniList alternative titles | 4th | Complete |

All four pillars use one shared foundation: the AniList client, the title matcher, the
series group builder, the database layer, and the web dashboard.

---

## 2. System diagram

```
                         ┌──────────────────────────────────────────────────────────────┐
                         │                     Anilist-Link Service                      │
                         │                                                              │
                         │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
                         │  │  P2: File   │  │  P3: Meta-  │  │  P1: Watch Status   │  │
                         │  │  Organize   │  │  data Write │  │  Sync               │  │
                         │  └──────┬──────┘  └──────┬──────┘  └──────┬──────────────┘  │
                         │         │                │                │                  │
  [Plex Server]    <───> │  ┌──────┴────────────────┴────────────────┴──────┐          │
                         │  │              Shared Foundation                 │          │
  [Jellyfin]       <───> │  │  AniList Client · Title Matcher · Series      │          │
                         │  │  Group Builder · Database · Scheduler         │          │
  [Crunchyroll]    ───>  │  └──────────────────────┬────────────────────────┘          │
                         │                         │                                    │
  [Sonarr/Radarr]  <───> │  ┌──────────────────────┴──────────────────────┐           │
                         │  │  P4: Download        ┌──────────────┐       │           │
                         │  │  Management          │  Web UI      │       │           │
                         │  └──────────────────────┤  (FastAPI)   │───────┘           │
  [Browser]        <───> │                         └──────────────┘                    │
  [Glance]         <───  │                                                              │
                         └──────────────────────────────────────────────────────────────┘
                                                   │
                                                   ▼
                                          [AniList GraphQL API]
```

---

## 3. Components

One row for each part that can fail on its own.

| Component | Does | Where |
|---|---|---|
| AniList client | GraphQL queries and mutations, OAuth2, rate limiting at 90 requests per minute | `src/Clients/AnilistClient.py` |
| AniList health tracker | Circuit breaker for AniList outages and reduced rate limits | `src/Clients/AnilistHealth.py` |
| AniList health monitor | Hourly recovery probe during an outage. Keeps the outage state across restarts | `src/Sync/AnilistHealthMonitor.py` |
| Plex client | Library enumeration, metadata writes, watch status | `src/Clients/PlexClient.py` |
| Jellyfin client | Library access, metadata writes, watch status, virtual season cleanup | `src/Clients/JellyfinClient.py` |
| Jellyfin event listener | WebSocket connection that reports Jellyfin scan completion and new items | `src/Clients/JellyfinEventListener.py` |
| Crunchyroll client | Login and watch history, through Selenium and Chromium | `src/Clients/CrunchyrollClient.py` |
| Sonarr and Radarr clients | API v3 lookup, add, rescan, path updates | `src/Clients/SonarrClient.py`, `RadarrClient.py`, `ServarrBaseClient.py` |
| TVMaze client | TVDB/IMDB/TVMaze IDs for the Jellyfin NFO files | `src/Clients/TVMazeClient.py` |
| Prowlarr and qBittorrent clients | Indexer search and torrent handoff. No other module uses them at this time | `src/Clients/` |
| Title matcher | Fuzzy title matching, anime-specific normalization, season and cour parsing | `src/Matching/` |
| Series group builder | Walks AniList SEQUEL/PREQUEL relations to build one logical show | `src/Scanner/SeriesGroupBuilder.py` |
| Metadata scanners | Scan, match, cache and apply for Plex and for Jellyfin | `src/Scanner/MetadataScanner.py`, `JellyfinMetadataScanner.py` |
| Library restructurer | Folder rename, file rename and full restructure into Structure A | `src/Scanner/LibraryRestructurer.py` |
| Crunchyroll watch syncer | Crunchyroll to AniList, with status transitions PLANNING → CURRENT → COMPLETED | `src/Sync/WatchSyncer.py` |
| Crunchyroll preview runner | Preview, approve and undo for the Crunchyroll sync | `src/Sync/CrunchyrollPreviewRunner.py` |
| Crunchyroll repair | Finds past Crunchyroll writes that went to the wrong entry of a series group | `src/Sync/CrunchyrollRepair.py` |
| Plex and Jellyfin watch syncers | Two-way watch sync with a COMPLETED guard and an undo log | `src/Sync/PlexWatchSyncer.py`, `JellyfinWatchSyncer.py`, `WatchSyncBase.py` |
| Watchlist refresh | Keeps the `user_watchlist` cache current for all linked AniList users | `src/Sync/WatchlistRefresh.py` |
| Download manager | AniList entry to Sonarr or Radarr add request | `src/Download/DownloadManager.py` |
| Mapping resolver | Keeps the AniList-to-Sonarr/Radarr mappings | `src/Download/MappingResolver.py` |
| Arr post-processor | Moves a finished download into the library, renames it, updates the Sonarr/Radarr path | `src/Download/ArrPostProcessor.py` |
| Season range mapper | Decides which Sonarr season and episode range each AniList entry occupies | `src/Download/SeasonRangeMapper.py` |
| Sonarr episode mapper | Links files that are already in place to Sonarr episodes, without a rename | `src/Download/SonarrEpisodeMapper.py` |
| Arr link verifier | Re-checks stored Sonarr/Radarr links for deleted items and moved paths | `src/Download/ArrLinkVerifier.py` |
| Import processor | Brings hand-fetched media into the library, then tells Sonarr/Radarr | `src/Download/ImportProcessor.py` |
| Media server sync | One debounced Plex/Jellyfin refresh after a batch of Sonarr/Radarr moves | `src/Download/MediaServerSync.py` |
| Download syncer | Adds AniList watchlist entries to Sonarr/Radarr on a schedule | `src/Sync/DownloadSyncer.py` |
| Scheduler | APScheduler job registration | `src/Scheduler/Jobs.py` |
| Web dashboard | FastAPI app factory, routes, Jinja2 templates, static JavaScript | `src/Web/` |
| Database layer | The only code that touches SQLite | `src/Database/` |
| Config | Frozen dataclasses from environment variables and database settings | `src/Utils/Config.py` |

### 3.1. Design patterns

- **Pipeline**: the metadata scanners run scan → match → cache → apply.
- **Strategy**: the matcher has more than one path (title, season-aware, movie), and each call selects the path.
- **Observer**: webhook and WebSocket handlers react to Sonarr, Radarr and Jellyfin events.
- **Repository**: the database layer keeps SQLite behind one `DatabaseManager` interface.

---

## 4. Shared foundation

### 4.1. AniList client (`src/Clients/AnilistClient.py`)

GraphQL client for all AniList interactions:

- **Public queries**: search by title, fetch by ID, relations with `relationType(version: 2)`, and external links (for TVDB and TMDB IDs)
- **Authenticated mutations**: watch status and score updates, with a per-user OAuth2 token
- **OAuth2 flow**: authorization URL, token exchange, and viewer profile. At link time the client also caches `Viewer.mediaListOptions.scoreFormat`. The watchlist refresh updates it each 24 hours.
- **Rate limiting**: token bucket, 90 capacity, 1.5 tokens each second (`RateLimiter`)
- **Retries**: exponential backoff on 429 and 5xx responses
- **Availability circuit breaker**: see 4.1.1

#### 4.1.1. AniList availability (`src/Clients/AnilistHealth.py`, `src/Sync/AnilistHealthMonitor.py`)

AniList sometimes switches its public API off (HTTP 403, *"The AniList API has been
temporarily disabled due to severe stability issues."*). At other times the API stays
up with a reduced rate limit of 30 requests per minute. Before this tracker, each scan,
sync and refresh found the outage on its own, three retries at a time.

`AniListHealth` is one tracker for the whole process. `Main.py` creates it and gives it
to `AniListClient`. It stores only facts (last success, last failure, the rate limit
that the server advertises) and derives one of three states:

| State | Meaning | Effect |
|-------|---------|--------|
| `ok` | Normal operation | — |
| `degraded` | Reduced rate limit, or recent 429s | Warning banner. Work continues, slower |
| `down` | API disabled, unreachable, persistent 5xx, or sustained 429s | Circuit open. All AniList calls fail fast |

**Fail fast, then probe.** While the state is `down`, `_execute_query()` raises
`AniListUnavailableError` before it sends a request. The health monitor finds the
recovery with `AniListClient.probe()`, one small `Media(id: 1)` query each hour
(`PROBE_INTERVAL_SECONDS = 3600`). A job that runs next never probes. A 429 on a probe
counts as "alive". AniList outages last for hours, so a shorter interval gives no
benefit. A user who wants an answer now clicks **Check now** in the banner. After a
container restart, a probe is due immediately, so a stale outage does not stay for up
to an hour.

**Halting work.** Scheduled jobs check `anilist_available()` (`Main.py`) and skip. The
Plex and Jellyfin scanners, `WatchSyncer` and `DownloadSyncer` stop their per-item
loops, so they do not mark each remaining item as unmatched. The watchlist refresh and
the synonyms backfill stop early.

**Surfacing it.** `GET /api/anilist/status` feeds a banner in `base.html`. The page
polls it each 60 seconds, and the counters tick locally between polls. The banner shows
the reason, the total downtime and the time to the next check, with a **Check now**
button (`POST /api/anilist/check-now`). The outage state is kept in `app_settings`
under `anilist.health`, so a restart reports the true downtime. A recovery posts a
dashboard notification. An app-level exception handler turns an
`AniListUnavailableError` in a route into a `503`, not a bare `500`.

### 4.2. Title matcher (`src/Matching/TitleMatcher.py`, `src/Matching/Normalizer.py`)

- Similarity from `difflib.SequenceMatcher`, with a threshold (default 0.75). The
  matcher keeps `difflib` on purpose, to keep the tested behavior. `rapidfuzz` is used
  in `NamingTranslator` and the TVMaze client.
- Anime-specific normalization in `Normalizer.py`: season numbers, punctuation, common
  prefixes and suffixes, year and tag extraction.
- Season-aware matching: `find_best_match_with_season()` finds season indicators
  (ordinals, Roman numerals, "Part N", "Season N").
- **Season and cour**: `parse_season_and_cour()` is the single source of truth. A Roman
  numeral or `Nth Season` names the season. A bare `Part N` names a cour inside that
  season. Cours share the season number of their parent, and episode numbers run on
  across the cours. The Crunchyroll season map and the restructurer both use it.
- A separate path for movies, `_find_best_movie_match()`.
- Format filter: MOVIE, OVA and SPECIAL are out by default. A call can change this.
- `get_primary_title()` gets the best available title from AniList data.

Used by: P1, P2, P3.

### 4.3. Naming template and translator (`src/Utils/NamingTemplate.py`, `src/Utils/NamingTranslator.py`)

- **`NamingTemplate.py`**: renders file and folder names from tokens. `parse_quality()`
  reads resolution, source and codec from a file name.
- **`NamingTranslator.py`**: `resolve_tvdb_id()` and `resolve_tmdb_id()` read IDs from
  AniList external links. `is_movie_format()` routes an entry to Sonarr or Radarr.
  `get_preferred_title()` selects the title. `resolve_franchise_root_id()` walks the
  PREQUEL chain to the first entry.

Used by: P2, P4.

### 4.4. Path translator (`src/Utils/PathTranslator.py`)

When Plex or Jellyfin mount a host directory at a different container path, the paths
they report do not exist in this container. `PathTranslator` maps a service prefix to a
local prefix. The longest prefix wins. The Jellyfin scanner builds one from the Jellyfin
library locations and the local library paths. Sonarr and Radarr use the remote and
local path settings (`sonarr.path_prefix`, `sonarr.local_path_prefix`, and the same
keys for Radarr).

### 4.5. Series group builder (`src/Scanner/SeriesGroupBuilder.py`)

- Breadth-first search (BFS) that follows only SEQUEL/PREQUEL edges where `type == ANIME`
- Does not follow SIDE_STORY, SPIN_OFF, ALTERNATIVE and the other relation types. Those are separate groups.
- Sorts entries by `startDate`
- Keeps groups in `series_groups` and `series_group_entries`, with a 168-hour TTL
- Returns `(group_id, entries_list)`

Used by: P2, P3, and P1 for episode-to-entry resolution. Section 9 has the full model.

### 4.6. Database layer (`src/Database/`)

- **`Connection.py`**: `DatabaseManager`, the async SQLite manager through aiosqlite. WAL mode, foreign keys on, CRUD for all tables.
- **`Models.py`**: the `TABLES` dict (DDL) and the `INDEXES` list.
- **`Migrations.py`**: v1 creates the full 1.0 schema. v2 to v5 are incremental. Migrations run at startup.

Only `src/Database/` imports `sqlite3` or `aiosqlite`. Section 11 lists the tables.

### 4.7. Web dashboard (`src/Web/`)

FastAPI with Jinja2 templates:

- **`App.py`**: `create_app()` factory with a lifespan for startup and shutdown. It
  needs the config, the database, the AniList client and the scheduler, so there is no
  module-level `app`. Start the app with `python -m src.Main`.
- **Dashboard** (`/`): status, job triggers, linked accounts. It sends a new user to
  `/onboarding` until `onboarding.status` is `completed`.
- **Onboarding** (`/onboarding`): 4-step first-run wizard with service setup and a
  first restructure.
- **Settings** (`/settings`): all credentials, paths, templates and intervals. A field
  that an environment variable sets is disabled.
- **Connection tests** (`/api/test/*`): live checks for each service.
- **Floating progress widget**: in `base.html`, polls `GET /api/progress` each 2 seconds.
- **AniList status banner**: in `base.html`. See 4.1.1.
- **Rate Your Completed Shows**: a dashboard card that lists AniList `COMPLETED`
  entries with `score == 0`. A rating goes to AniList through `SaveMediaListEntry(score)`
  and to the local cache. The setting `app.show_unrated_completed` turns it on or off
  (default on). `rating-widget.js` adapts to the `scoreFormat` of the account.
- **Glance integration** (`/glance/rate-completed`): a small page for an iframe in a
  [Glance](https://github.com/glanceapp/glance) dashboard, with the same list and rating
  action. A shared key (`glance.api_key`) protects it. See section 16.
- **Activity tracker** (`src/Web/ActivityTracker.py`): middleware records the time of
  the last user request. The watchlist refresh reads it and skips AniList calls while
  nobody uses the dashboard.

### 4.8. Scheduler and background loops (`src/Scheduler/Jobs.py`)

`JobScheduler` wraps an `AsyncIOScheduler`. Each trigger has an explicit `tzinfo`.
`trigger_job(job_id)` runs a job now. `get_job_status()` reports the jobs.

| Job | Trigger | Does |
|---|---|---|
| `crunchyroll_sync` | Daily at `CR_SYNC_TIME`, or each `SYNC_INTERVAL` minutes if that is empty | Crunchyroll → AniList sync |
| `plex_metadata_scan` | Each `SCAN_INTERVAL` hours (24) | Plex scan and metadata apply |
| `download_sync` | Each `DOWNLOAD_SYNC_INTERVAL` minutes (60) | AniList watchlist → Sonarr/Radarr |
| `library_reindex` | Each `LIBRARY_REINDEX_INTERVAL` hours (6) | Reindex of the local libraries |
| `plex_watch_sync` | Each 15 minutes | Plex ↔ AniList. Off by default |
| `jellyfin_watch_sync` | Each 15 minutes | Jellyfin ↔ AniList. Off by default |

Background loops that are not APScheduler jobs:

- **Watchlist refresh** (`watchlist_activity_loop` in `src/Sync/WatchlistRefresh.py`):
  runs at startup, then each `WATCHLIST_REFRESH_INTERVAL` minutes (30), but only while
  the dashboard has recent activity. Long jobs, for example a Crunchyroll sync, also
  refresh it during the run. It also runs after a Crunchyroll sync or preview apply.
- **AniList health monitor**: idle while AniList is healthy. See 4.1.1.
- **Jellyfin event listener**: a WebSocket to Jellyfin. See 6.5.
- **Cache synonyms backfill** (`src/Sync/CacheSynonymsBackfill.py`): one pass at
  startup that fills `anilist_cache.synonyms` for rows cached before migration v3.

### 4.9. Config (`src/Utils/Config.py`)

Frozen dataclasses: `AppConfig`, `AniListConfig`, `CrunchyrollConfig`, `PlexConfig`,
`JellyfinConfig`, `SonarrConfig`, `RadarrConfig`, `DatabaseConfig`, `SchedulerConfig`,
`DownloadSyncConfig`. `load_config_from_db_settings()` resolves each field in this
order: environment variable, then the `app_settings` row, then the code default.
`SETTINGS_MAP` connects each settings key to its environment variable. Secrets
(`SECRET_KEYS`) are stored as plain text in the database. The settings page never
shows them. Business logic never reads an
environment variable directly. The variable list is in `docs/QUICK-REFERENCE.md`.

---

## 5. Pillar 2: File organization

### 5.1. Overview

The restructurer moves anime files into a standard folder structure. That structure
gives clean 1:1 mappings between Plex or Jellyfin shows and AniList entries. It is the
recommended first step for a new user.

### 5.2. Status

| Component | Status |
|-----------|--------|
| `LibraryRestructurer` (`src/Scanner/LibraryRestructurer.py`) | Complete, all 3 levels |
| Folder rename only (L1) | Complete |
| Folder and file rename (L2) | Complete |
| Full restructure with moves (L3) | Complete |
| Wizard UI (`src/Web/Routes/Restructure.py`) | Complete: analyze, preview, execute |
| Multi-source restructure with conflict detection | Complete |
| Saved plans (`restructure_plans`) and re-run | Complete |
| `restructure_log` audit table | Complete |
| Plex and Jellyfin refresh after execute | Complete |
| `PlexShowProvider` / `JellyfinShowProvider` | Complete |
| Smart Move ("Fix Location") | Complete |
| Import of hand-fetched media (`/import`) | Complete |

### 5.3. Operation levels

1. **L1, folder rename**: rename show folders to the AniList titles. No file moves.
2. **L2, folder and file rename**: also rename episode files with the naming templates.
3. **L3, full restructure**: move files between directories into the series group structure, with season subfolders.

**Workflow**: wizard → select source → analyze → preview → resolve conflicts → execute → log → refresh.

### 5.4. Target structure

The recommended output (Structure A) is one folder for each AniList entry:

```
/Anime/
  Demon Slayer Kimetsu no Yaiba/
    Demon Slayer - S01E01.mkv
    ...
  Kimetsu no Yaiba Mugen Train/
    Kimetsu no Yaiba Mugen Train.mkv
  Kimetsu no Yaiba Entertainment District Arc/
    Kimetsu no Yaiba Entertainment District - S01E01.mkv
    ...
```

### 5.5. Routes

- `GET /restructure`: wizard start page
- `POST /api/restructure/analyze`, `GET /api/restructure/progress`: analysis and its progress
- `GET /restructure/preview`: planned moves for approval
- `POST /restructure/execute`, `POST /restructure/cancel`: run or stop the moves
- `GET /restructure/results`, `GET /restructure/report`: results and the audit log
- `GET /restructure/plan/{plan_id}`, `POST /api/restructure/plan/{plan_id}/rerun`: saved plans
- `POST /onboarding/restructure/analyze`, `POST /onboarding/restructure/execute`: the same flow inside onboarding
- `POST /api/smart-move/preview`, `POST /api/smart-move/execute`: **Smart Move (Fix
  Location)**. Runs the restructurer on one library item that Sonarr/Radarr do not
  track. It builds a one-element `ShowInput` from the `library_items` row and analyzes
  with `force_franchise_root=True`. On execute it moves only that item, updates the row
  and the local mapping, and refreshes the media server. The button shows on the
  library detail page and the watchlist page.
- `/import` and `/api/import/*`: two ways to bring in media fetched by hand. One scans
  the folder of an entry already in the library again. The other works through the
  import folder (`LIBRARY_IMPORT_PATH`) and matches each dropped folder to an AniList
  entry for review. Both run the restructurer, then point Sonarr/Radarr at the result
  and rescan (`src/Download/ImportProcessor.py`).

### 5.6. Franchise-root nesting for movies

A franchise movie (for example a Demon Slayer film) goes under the ROOT folder of its
series group, next to the TV seasons (Structure A). It does not go in a separate
top-level directory. Both movers find the franchise root the same way:

1. Use the `root_anilist_id` of the series group when it points to a *different* root.
2. If not, walk the AniList **PREQUEL** chain back to the first entry
   (`resolve_franchise_root_id` in `NamingTranslator`). This finds the root even when
   the stored group is stale or points to itself.

- **Restructurer** (`LibraryRestructurer._analyze_full_restructure`): a lone franchise
  entry goes under the rendered root folder. A full library pass walks the chain only
  for MOVIE format. A Smart Move always walks it. A move to a new parent counts as a
  change even when the folder name stays the same (the full path is compared, not the
  base name).
- **Arr post-processor** (`ArrPostProcessor`): the Sonarr/Radarr "Move to Library" puts
  the movie under the root in its own title folder, not a season folder. A backup name
  prevents a clash with a TV season of the same name. It also fills the cached year of
  the root, writes the group `tvshow.nfo`, and removes the empty source folder
  (`prune_orphaned_dir`).

---

## 6. Pillar 1: Watch status sync

### 6.1. Overview

The syncers find new watch progress on a media platform and update the AniList entry
of each linked user with the correct episode count and status.

### 6.2. Status

| Component | Status |
|-----------|--------|
| `WatchSyncer` (Crunchyroll → AniList) | Implemented |
| `CrunchyrollClient` (login and history) | Implemented |
| `CrunchyrollPreviewRunner` (preview, approve, undo) | Implemented |
| Unmapped episode report (`cr_unmapped_episodes`) | Implemented |
| Repair scan for mis-mapped writes (`CrunchyrollRepair`) | Implemented |
| `PlexWatchSyncer` / `JellyfinWatchSyncer` (two-way) | Implemented |
| Watch sync log with undo (`watch_sync_log`) | Implemented |
| Per-source on/off switch | Implemented, off by default |
| COMPLETED status guard | Implemented |
| Circular sync prevention | Implemented |
| Jellyfin webhook (`POST /jellyfin/webhook`) | Implemented |
| Jellyfin virtual season cleanup | Implemented |
| Plex webhook (real-time sync) | Not implemented. Plex is polled |
| AniList token refresh | Not implemented |

### 6.3. Crunchyroll → AniList

`WatchSyncer`:

- Reads the Crunchyroll watch history page by page and stops early when it reaches known history
- Matches titles to AniList (fuzzy match and series group resolution)
- Resolves an episode to an entry with cumulative episode counts
- Moves the status PLANNING → CURRENT → COMPLETED
- Records progress for each user and item in `sync_state`
- Has a dry-run mode that makes no changes

`CrunchyrollPreviewRunner` adds preview, approve and undo:

- Puts proposed changes in `cr_sync_preview` before it applies them
- Records applied changes in `cr_sync_log` for undo
- Routes: `/crunchyroll`, `/crunchyroll/preview`, `/crunchyroll/history`, and `/api/crunchyroll/*`
- With `CR_AUTO_APPROVE=true` (the default), the scheduled sync applies without a manual approval

**Season identity.** Crunchyroll season numbers are not AniList season numbers.
Crunchyroll counts movies and arcs that AniList publishes as separate entries. For
Demon Slayer, Crunchyroll shows 7 seasons against 5 AniList TV entries. The season
*title* is the reliable key. `season_from_cr_season_title()` overrides the number when
one title matches near-exactly and without ambiguity (threshold 0.95).

**Episode numbers.** Crunchyroll reports either per-season or franchise-absolute
episode numbers. An absolute reading that resolves to a season *before* the reported
one is rejected. The history is newest-first and paginated, so the syncer groups the
highest episode for each (series, season) across the whole run, not for each page.

**Unmapped episodes.** An episode with no AniList target goes into
`cr_unmapped_episodes`. The user maps it to an entry
(`POST /api/crunchyroll/unmapped/{id}/map`, logged in `cr_sync_log` for undo) or
dismisses it.

**Repair.** `POST /api/crunchyroll/repair/scan` compares the latest preview with the
sync history and reports writes that went to the wrong entry of a series group. The
procedure is in [SEASON-MAPPING-REPAIR.md](SEASON-MAPPING-REPAIR.md).

`CrunchyrollClient` has no official API. It uses Selenium with undetected-chromedriver
through `asyncio.to_thread()`. The session is cached in `cr_session_cache` for 30 days,
which decreases the number of browser logins. A change to the Crunchyroll website can
stop it.

### 6.4. Plex and Jellyfin watch sync

`PlexWatchSyncer` and `JellyfinWatchSyncer` share `WatchSyncBase` and sync in two
directions.

**Forward sync (media server → AniList)**:

- Polls watched episode counts for the `media_mappings` entries
- Compares them with the `sync_state` baseline, and skips an item with no new progress
- COMPLETED guard: reads the `user_watchlist` cache first, and never downgrades a COMPLETED AniList entry
- A scheduled run refreshes the watchlist live (`live_check=True`), so the guard uses current data
- Records each change in `watch_sync_log`, with the state before and after, for undo

**Backfill (AniList → media server)**:

- Reads `user_watchlist` entries with progress above 0
- Marks the matching Plex or Jellyfin episodes as watched
- Always writes the AniList progress into `sync_state` after the backfill. If not, the next forward sync reads the backfilled episodes as new activity (the circular sync fix).

**On/off**: both sources are off by default (`plex.watch_sync_enabled`,
`jellyfin.watch_sync_enabled`). The Watch Sync page (`/watch-sync`) changes them
without a restart. `PLEX_WATCH_SYNC_ENABLED` and `JELLYFIN_WATCH_SYNC_ENABLED` override
them.

**Undo**: the Watch Sync page can revert any forward-sync update. It calls AniList to
restore the earlier state.

### 6.5. Jellyfin virtual season cleanup

Jellyfin makes "virtual" seasons (`LocationType=Virtual`) when its metadata providers
list seasons that are not on disk. These duplicate the real season folders that have
our metadata. Neither `<lockdata>true</lockdata>` in NFO files nor the Missing Episode
Fetcher setting stops this. It is a limit of Jellyfin.

**Root cause**: the Jellyfin `ProviderManager` refresh queue races with
`DELETE /Items/{id}`. A delete returns 204, and the refresh queue makes the item again
moments later.

**Solution**: stop the library scan task (`DELETE /ScheduledTasks/Running/{taskId}`),
wait about 3 seconds for the refresh queue to drain, then delete. This is in
`JellyfinClient._stop_scan_task()` and `delete_virtual_seasons()`.

**Triggers**:

1. **After our own scans**: at the end of a live scan, a scan apply, an apply-all, and the refresh after a restructure.
2. **Jellyfin event listener**: a WebSocket to `/socket` subscribes to `ScheduledTasksInfo`. When the Jellyfin `RefreshLibrary` task completes, or new items arrive, the cleanup runs. No plugin is necessary.
3. **Webhook**: `POST /jellyfin/webhook` accepts `TaskCompleted` events from the Jellyfin Webhook plugin. The plugin does not send them reliably, so the listener is the main trigger.

**Diagnostic endpoints** (`src/Web/Routes/JellyfinLibrary.py`):

- `GET /api/jellyfin/virtual-items?series_id=`: all seasons, with the virtual ones flagged
- `GET /api/jellyfin/virtual-items?item_id=`: one item
- `GET /api/jellyfin/delete-virtual?item_id=`: delete one virtual item
- `GET /api/jellyfin/cleanup-virtual`: run the cleanup now

---

## 7. Pillar 3: Metadata from AniList

### 7.1. Overview

The scanners write AniList metadata to Plex and Jellyfin anime libraries. They replace
the default metadata (often from TVDB or TMDB) with AniList titles, descriptions, cover
art, genres, ratings and studios.

### 7.2. Status

| Component | Status |
|-----------|--------|
| `MetadataScanner` (Plex scan, match, apply) | Complete |
| `JellyfinMetadataScanner` (Jellyfin scan, match, apply) | Complete |
| `PlexClient` metadata writes (show and season) | Complete |
| `JellyfinClient` metadata writes (item and season) | Complete |
| Plex and Jellyfin library browsers (`/plex`, `/jellyfin`) | Complete |
| Unified library browser (`/library`) | Complete |
| `plex_media` / `jellyfin_media` snapshot tables | Complete |
| Manual overrides (`/mappings`) | Complete: list, add, delete |
| Staff and credits to Plex | Deferred |
| GUID-based high-confidence matching | Deferred |

### 7.3. Scanner pipeline

```
Enumerate shows from server → Match titles (TitleMatcher) → Detect structure (A/B/C)
    → Build series groups → Cache AniList metadata → Write to server
```

**Modes**: a preview (dry run) that shows the changes without applying them, and a live
scan that applies them with Server-Sent Events (SSE) progress.

**Metadata written**: show title (romaji or English), summary, genres, rating (AniList
score scaled to the platform), poster (from the AniList CDN URL), and season names
(the AniList title of each entry).

**Jellyfin specifics**: the client authenticates with the header
`MediaBrowser Client="AnilistLink", Token="{api_key}"`. Poster upload uses
`RemoteImages/Download`, because a direct upload to `/Items/{id}/Images/Primary`
returns 500. After an image upload the client locks the show folder, so a Jellyfin
scheduled scan does not revert the artwork.

### 7.4. Routes

**Plex**:

- `GET /plex`: library browser
- `POST /plex/update-match`, `POST /plex/remove-match`: mapping management
- `POST /plex/scan/preview`, `POST /plex/scan/live`: scan modes
- `GET /plex/scan/progress`, `GET /plex/scan/results`: progress and results
- `POST /plex/apply-all`, `POST /plex/apply-single`: apply metadata
- `/scan/plex/*` and `/api/scan/plex/*`: the older scan flow, rematch, and AniList search

**Jellyfin**: `/jellyfin/*` has the same routes as Plex.

**Unified** (`/library/*`): one library manager for Plex and Jellyfin items, with a
series group detail view.

---

## 8. Pillar 4: Download management

### 8.1. Overview

Integrates with Sonarr and Radarr. It resolves AniList entries to TVDB or TMDB IDs,
sends TV series to Sonarr and movies to Radarr, and pushes the AniList alternative
titles for better indexer matching.

### 8.2. Status

| Component | Status |
|-----------|--------|
| `SonarrClient`, `RadarrClient` (API v3) | Implemented |
| `DownloadManager` | Implemented |
| `MappingResolver` | Implemented |
| `ArrPostProcessor` | Implemented |
| `SeasonRangeMapper`, `SonarrEpisodeMapper` | Implemented |
| `ArrLinkVerifier` | Implemented |
| `DownloadSyncer` | Implemented |
| Download page (`/download`) | Implemented |
| Manual grab (`/grab/{anilist_id}`), through the Sonarr/Radarr indexers | Implemented |
| Webhook receivers (`/api/webhook/sonarr`, `/api/webhook/radarr`) | Implemented, with auto-registration |
| Watchlist browser (`/watchlist`) | Implemented |
| Disambiguation picker (TVDB and TMDB) | Implemented |
| Auto-search on a new CURRENT entry | Partial. `DownloadSyncer` exists |

### 8.3. Flow

**ID resolution** (`src/Utils/NamingTranslator.py`):

1. Get the AniList external links for the entry.
2. Read the TVDB ID (Sonarr) or the TMDB ID (Radarr).
3. If there is no ID, search Sonarr or Radarr by title (`lookup_series`, `lookup_movie`). The search also tries the title variants of season 1 for a sequel.
4. If the result is still not clear, `POST /api/library/add-to-arr` returns `needs_disambiguation` with candidates, and the UI shows a picker.
5. `is_movie_format()` sends MOVIE, ONA, SPECIAL and MUSIC to Radarr, and all other formats to Sonarr.

**`MappingResolver`**: keeps AniList-to-Sonarr/Radarr mappings in
`anilist_sonarr_mapping` and `anilist_radarr_mapping`, with the tracked flag, the
monitor status and the confidence.

**`SeasonRangeMapper`**: AniList and TVDB disagree about seasons. A split cour is two
AniList entries in one Sonarr season. This module computes which Sonarr season and
episode range each entry occupies, and stores it in `anilist_sonarr_season_mapping`
(an episode range since migration v4).

**`ArrPostProcessor`**: handles Sonarr/Radarr webhook events (download, upgrade). It
moves the file into the library with the naming templates and the series group
layout, updates the Sonarr/Radarr path and rescans. `MediaServerSync` then runs one
debounced Plex/Jellyfin refresh and metadata pass for the whole batch.

**`SonarrEpisodeMapper`**: our library numbers episodes the AniList way, so Sonarr
cannot place some files. This module links the files to Sonarr episodes through the
season ranges. It does not rename them.

**`ArrLinkVerifier`**: finds two kinds of drift. One is a series or movie that was
deleted in Sonarr/Radarr but is still mapped. The other is files that the restructurer
moved after the link was made.

**`DownloadSyncer`**: on a schedule, it adds AniList watchlist entries with the
configured statuses (`DOWNLOAD_AUTO_STATUSES`, default `CURRENT`) that are not yet in
Sonarr/Radarr.

---

## 9. Media mapping model

This model is the base of P2 and P3. It solves the mismatch between the AniList entry
for each season and the show-based layout of Plex and Jellyfin.

### 9.1. The mismatch

**AniList**: each season, part or cour is a **separate Media entry** with its own ID.
There is no parent series. Seasons connect only through **relation edges** (SEQUEL,
PREQUEL, and others). Demon Slayer is more than 5 entries in a chain, with TV seasons
and movies mixed.

**Plex and Jellyfin**: a show is one item with season folders. Episodes are numbered
in each season. "Demon Slayer" is usually one show with seasons 1 to 5.

The result is a many-to-many mapping problem. Series groups solve it.

### 9.2. Series groups

A **series group** is a set of AniList entries that together make one logical show for
the user.

**Definition**: all entries that you can reach by following only `SEQUEL` and
`PREQUEL` edges where `type == ANIME`. All formats are included: TV, MOVIE, OVA, ONA
and SPECIAL.

**Excluded relations**: `SIDE_STORY`, `SPIN_OFF`, `ALTERNATIVE`, `CHARACTER`,
`SUMMARY`, `COMPILATION`, `CONTAINS`, `SOURCE`, `ADAPTATION` and `OTHER`.

**Order**: by `startDate`.

```
Series Group: "Demon Slayer: Kimetsu no Yaiba"
  ├── Season 1: Demon Slayer: Kimetsu no Yaiba          (TV, 26 eps, Apr 2019)  → AniList 101922
  ├── Season 2: Kimetsu no Yaiba: Mugen Train            (MOVIE, 1 ep, Oct 2020) → AniList 112151
  ├── Season 3: Kimetsu no Yaiba: Mugen Train Arc        (TV, 7 eps, Oct 2021)   → AniList 129874
  ├── Season 4: Kimetsu no Yaiba: Entertainment District (TV, 11 eps, Dec 2021)  → AniList 142329
  ├── Season 5: Kimetsu no Yaiba: Swordsmith Village     (TV, 11 eps, Apr 2023)  → AniList 145139
  └── Season 6: Kimetsu no Yaiba: Hashira Training       (TV, 8 eps, May 2024)   → AniList 166240
```

### 9.3. Building a group

1. **Match one entry**: a fuzzy title match or a manual override finds one AniList entry.
2. **Walk the graph**: BFS over SEQUEL/PREQUEL edges with `relationType(version: 2)`.
3. **Filter**: keep only entries where `type == ANIME`.
4. **Sort**: by `startDate` (year, month, day).
5. **Number**: season numbers from 1, in date order. Cours of one season share its number (see 4.2).
6. **Cache**: in `series_groups` and `series_group_entries`, with a 168-hour TTL.

### 9.4. Library structures

The scanner handles three file layouts.

**Structure A: one folder for each AniList entry**

```
/Anime/
  Demon Slayer Kimetsu no Yaiba/          ← Plex show (rating_key: 1001)
  Demon Slayer Mugen Train/               ← Plex show (rating_key: 1002)
  Demon Slayer Entertainment District/    ← Plex show (rating_key: 1003)
```

Each server item maps to one AniList entry (1:1). This is the recommended structure.

**Structure B: one show with many seasons (TVDB style)**

```
/Anime/
  Demon Slayer (2019)/
    Season 01/  S01E01-E26  (26 eps)
    Season 02/  S02E01      (1 ep — movie)
    Season 03/  S03E01-E11  (11 eps)
```

Match the show title, walk the SEQUEL/PREQUEL chain, then map the seasons by position.

**Structure C: absolute numbering**

```
/Anime/
  Demon Slayer/
    Demon Slayer - 001.mkv through Demon Slayer - 057.mkv
```

Walk the series group and use cumulative episode counts to find the boundaries.

**Detection**:

```
1. Match show to AniList → build series group

2. If series group has only 1 entry:
     → Simple 1:1 mapping (show → entry)

3. If series group has multiple entries:
     a. If server show has multiple seasons:
          → Structure B (map seasons to group entries by position)
     b. If server show has 1 season with episode count > first entry's episodes:
          → Structure C (absolute numbering)
     c. If server show has 1 season with episode count ≈ first entry's episodes:
          → Structure A (this show is just one entry in the group)
```

---

## 10. Data flow

One cycle for each pillar, from the outside in.

- **P2 (File organization)**: user selects a library (Plex, Jellyfin or a local path) → `LibraryRestructurer` reads the shows through a show provider → `TitleMatcher` matches them to AniList → `SeriesGroupBuilder` builds groups → the restructurer makes a move plan → the user previews it → the moves run and go into `restructure_log` → Plex or Jellyfin refreshes.
- **P3 (Metadata)**: the scanner reads the Plex or Jellyfin shows → `TitleMatcher` finds the AniList entries → `SeriesGroupBuilder` walks the relations → the AniList metadata goes into `anilist_cache` → the client writes the metadata to the show and its seasons.
- **P1 (Watch sync)**: a scheduled job starts → the Crunchyroll history (or Plex/Jellyfin watch state) is read → episodes resolve to AniList entries through the mappings and series groups → a preview or a direct update goes to AniList for each linked user → the change goes into `cr_sync_log` or `watch_sync_log`.
- **P4 (Downloads)**: the user selects an AniList entry, or `DownloadSyncer` finds one → `NamingTranslator` resolves the TVDB or TMDB ID → `MappingResolver` stores the mapping → `DownloadManager` sends the add request with the alternative titles → Sonarr/Radarr downloads → the webhook starts `ArrPostProcessor` → the file moves into the library → `MediaServerSync` refreshes Plex/Jellyfin.

### 10.1. How the pillars build on each other

```
P2 (File Organization)
  │  Organizes files into clean Structure A
  │  → Produces reliable 1:1 show↔AniList mappings
  ▼
P3 (Metadata from AniList)
  │  Uses those mappings to write metadata to Plex/Jellyfin
  │  → Builds series groups, caches AniList data
  ▼
P1 (Watch Status Sync)
  │  Uses series groups + mappings to resolve episodes to AniList entries
  │  → Syncs watch progress per linked user
  ▼
P4 (Download Management)
  │  Uses AniList data to resolve TVDB/TMDB IDs
  │  → Sends add requests to Sonarr/Radarr with alt titles
```

Each pillar works alone. The order gives the most data reuse.

---

## 11. Data

| Store | Holds | Lives at | Survives a rebuild |
|---|---|---|---|
| SQLite | All state: mappings, users, tokens, settings, caches, logs | `/config/anilist_link.db` (container), `./data/anilist_link.db` (local) | Yes, `/config` is a volume |
| App log | Rotating log, 5 MB, three old files | `/config/logs/anilist_link.log` | Yes |
| Chromium profile | Crunchyroll browser data (`HOME=/config`) | `/config` | Yes |
| Memory | The rate limiter state, and short caches during a scan or sync | process | No |

On upgrade, migrations run at startup. The schema is at **version 5**. v1 creates the
full 1.0 baseline. v2 adds `user_watchlist.title_native` and `title_synonyms`. v3 adds
`anilist_cache.synonyms`. v4 gives `anilist_sonarr_season_mapping` an episode range and
a wider primary key, so one Sonarr season can hold a split cour. v5 adds
`cr_unmapped_episodes`.

To change the schema, add a numbered migration to `src/Database/Migrations.py` and the
table change to `src/Database/Models.py`. Test it on a copy of the database first.

### 11.1. Tables

`TABLES` in `src/Database/Models.py` has 30 entries: 29 tables plus `schema_version`.

| Table | Purpose |
|-------|---------|
| `schema_version` | Migration tracking |
| `media_mappings` | Plex/Jellyfin item → AniList ID, with confidence and match method. Unique on (source, source_id) |
| `users` | Linked AniList accounts with OAuth tokens |
| `sync_state` | Sync progress for each user and item. Index on (user_id, media_mapping_id) |
| `anilist_cache` | AniList metadata cache, 7-day TTL. Index on expires_at |
| `manual_overrides` | Title → AniList ID overrides from the user |
| `cr_session_cache` | Crunchyroll session, 30-day TTL |
| `app_settings` | GUI settings. Secrets are plain text, and the UI masks them |
| `plex_media` / `jellyfin_media` | Library item snapshots |
| `series_groups` / `series_group_entries` | Series groups and their ordered entries |
| `restructure_log` | File move audit trail |
| `restructure_plans` | Saved restructure plans |
| `libraries` / `library_items` | Local library definitions and their items |
| `plex_users` / `jellyfin_users` | Per-user Plex tokens and Jellyfin credentials |
| `cr_sync_preview` / `cr_sync_log` | Pending Crunchyroll changes, and applied changes with undo |
| `cr_unmapped_episodes` | Crunchyroll history the sync could not place |
| `watch_sync_log` | Plex/Jellyfin sync audit trail with undo |
| `download_requests` | Sonarr/Radarr add request tracking |
| `anilist_sonarr_mapping` / `anilist_radarr_mapping` | AniList ↔ Sonarr/Radarr mappings |
| `anilist_sonarr_season_mapping` | Sonarr season and episode range for each AniList entry |
| `anilist_arr_skip` | Entries that the auto-download skips |
| `sonarr_series_cache` / `radarr_movie_cache` | Cached Sonarr series (by TVDB ID) and Radarr movies (by TMDB ID) |
| `user_watchlist` | AniList watchlist cache for each linked user |

`app_settings` keys that have no table of their own: `anilist.score_format`,
`anilist.score_format_updated_at`, `app.show_unrated_completed`, `glance.api_key`,
`anilist.health`, `onboarding.status`, `onboarding.step`.

### 11.2. Timestamps and time zones

**All timestamps are stored in UTC.** Each `created_at`, `applied_at`, `executed_at`
and `synced_at` column defaults to SQLite `datetime('now')`. The Python writers use
`datetime.now(timezone.utc)`.

Conversion happens one time, at render time:

| Layer | Responsibility |
|-------|----------------|
| `src/Utils/Time.py` | `get_timezone()` resolves `TZ` through `zoneinfo`, then the system zone, then UTC. `parse_utc()` reads the SQLite form and ISO-8601. `to_local()` / `to_local_date()` format in the configured zone |
| `src/Web/App.py` | Registers the `localtime` and `localdate` Jinja filters |
| Templates | Render each stored timestamp through `\| localtime` or `\| localdate`, never raw |
| `src/Scheduler/Jobs.py` | The scheduler and each `CronTrigger` get an explicit `tzinfo`. APScheduler does not guess through tzlocal |
| `Dockerfile` / `entrypoint.sh` | Install `tzdata` and point `/etc/localtime` and `/etc/timezone` at `$TZ` |

A raw stored value is the bug this prevents. A job that ran at 02:00
America/Los_Angeles is stored as 09:00 UTC. Printed without conversion, it shows an
hour that is still in the future.

---

## 12. External dependencies

| Depends on | Client | For | Auth | When it is down |
|---|---|---|---|---|
| AniList GraphQL API | `AnilistClient` | Metadata source, watch status target | OAuth2 | Circuit opens. Jobs pause. The banner shows the downtime (4.1.1) |
| Plex Media Server | `PlexClient` | Library, metadata writes, watch status | X-Plex-Token | Plex jobs fail and log. Other platforms continue |
| Plex.tv | `ConnectionTest` | Plex Pass detection | X-Plex-Token | The Plex Pass flag stays unknown |
| Jellyfin | `JellyfinClient`, `JellyfinEventListener` | Library, metadata writes, watch status, events | API key | Jellyfin jobs fail and log. The listener reconnects |
| Crunchyroll | `CrunchyrollClient` | Watch history | Session through Selenium | The sync fails and logs. Nothing is written |
| Sonarr API v3 | `SonarrClient` | TV series add, search, path updates | API key | Download actions fail. The rest continues |
| Radarr API v3 | `RadarrClient` | Movie add, search, path updates | API key | Download actions fail. The rest continues |
| TVMaze API | `TVMazeClient` | TVDB/IMDB/TVMaze IDs for Jellyfin NFO files | None | The NFO files have fewer provider IDs |
| Prowlarr, qBittorrent | `ProwlarrClient`, `QBittorrentClient` | Not used at this time. Manual grab searches through the Sonarr/Radarr indexers | API key, session cookie | — |
| Glance | — (`/glance/rate-completed`) | Rating from an external dashboard | Shared key in the query | Not applicable. Glance calls us |

The rule: if one platform fails, the others continue. All HTTP calls go through a
class in `src/Clients/`.

---

## 13. Deployment

- **Image**: `ghcr.io/anothermike-exe/anilist-link`. Tags `latest`, `dev`, and a semver tag for each release (for example `1.0.0`).
- **Platforms**: `linux/amd64`, `linux/arm64`.
- **Base image**: `python:3.11-slim-bookworm`, multi-stage. It is not Alpine, because Crunchyroll needs Chromium and chromedriver, and Alpine does not supply them in a usable form.
- **Process start**: `scripts/dev-tools/entrypoint.sh` applies `UMASK`, sets the time zone, sets `HOME=/config`, changes the owner of `/config` to `PUID:PGID`, then starts `python -m src.Main` as `PUID:PGID` through `gosu`. There is no supervisord.
- **Defaults**: `PUID=99`, `PGID=100`, `UMASK=002`, `TZ=UTC`, `DEBUG=false`.
- **Port**: 9876.
- **Volumes**: `/config` (database, logs, Chromium profile) and `/data` (reserved). The media library and the import folder mount under `/media`.
- **Build variants**: `BUILD_VARIANT=release` (default) or `dev`. `dev` adds the scripts in `scripts/dev-tools/` and `sqlite3`. The entrypoint copies the scripts to `/config/dev-tools/`.
- **Host**: Unraid or any Docker host. Compose sets `shm_size: "2g"` for Chromium.
- **Rollback**: pin the previous semver tag in compose, then `docker compose up -d`. The `latest` tag cannot describe a rollback. A schema migration does not run backward, so keep a copy of `/config/anilist_link.db` before an upgrade.

The variable, volume and port tables are in `docs/QUICK-REFERENCE.md`.

---

## 14. CI/CD

| Workflow | Triggers on | Does |
|---|---|---|
| `.github/workflows/Review.yml` | push to `main` or `dev`, and each pull request | `ruff check`, `black --check`, `mypy`, `pytest`. A second job runs a Claude Code review, only when the `ANTHROPIC_API_KEY` secret exists |
| `.github/workflows/BuildImage.yml` | push to `main` | Builds the release variant and pushes `ghcr.io/anothermike-exe/anilist-link:latest` |
| | push to `dev` | Builds with `BUILD_VARIANT=dev` and pushes `:dev` |
| | tag `v*` | Builds the release variant and pushes the semver tag (for example `:1.0.0`) and `:latest` |

`BuildImage.yml` builds for `linux/amd64` and `linux/arm64`. It logs in to GHCR with
the built-in `GITHUB_TOKEN`, so it needs no other secret.

Other automation on the repository:

- **Dependabot**: weekly updates for `pip`, `docker` and `github-actions`, grouped into one pull request for each ecosystem.
- **CodeQL**: GitHub default setup.
- **Release notes**: GitHub generates them, grouped by the pull request labels in `.github/release.yml`.

| Secret | Used by | Required |
|---|---|---|
| `ANTHROPIC_API_KEY` | `Review.yml`, review job | No. Without it the review job skips, and the checks still run |
| `GITHUB_TOKEN` | `BuildImage.yml` | Supplied by GitHub |

---

## 15. Testing

774 tests in `tests/Unit/`, one file for each unit (`test_*.py`). They use an
in-memory SQLite database and make no external calls. `tests/Integration/` is empty.
`tests/conftest.py` has the shared fixtures (database, config, mocks).

```bash
pytest                          # all tests
pytest --cov=src                # with coverage
pytest -x                       # stop on the first failure
```

Coverage target: 70% on the core logic (matching, sync, scanner).

Some tests hold a regression together. `test_cr_season_mapping.py` pins Mushoku Tensei
and Re:Zero in one file. A fix that renumbers the seasons of one breaks the other.

---

## 16. Security model

- **Trust boundary**: the dashboard and the API have no authentication. The app trusts the local network. Do not expose port 9876 to the internet. Put a reverse proxy with authentication in front of it if remote access is necessary.
- **The exception**: `/glance/*` needs a shared key (`glance.api_key`, generated in Settings, compared with `secrets.compare_digest`), because an external dashboard reaches it outside a normal browser session. A new key stops the old one immediately.
- **AniList**: OAuth2, one token for each user. Each user changes only their own AniList list.
- **Plex, Jellyfin, Sonarr, Radarr**: token or API key.
- **Secrets at runtime**: in environment variables, or as plain text in `app_settings`. Protect `/config` like a password file. The settings page never shows a stored secret, and the logs never contain one. Users can unlink an account and delete its token from the dashboard.
- **Data**: nothing goes to a service other than the configured platforms.
- **Code**: parameterized queries everywhere, input validation on the dashboard, no secrets in git.
- **Runs as**: `PUID:PGID` through `gosu`, never root.

---

## 17. Known limits

- **Media path alignment**: the restructurer moves the paths that Plex or Jellyfin report. If the container mounts the media at a different path, the moves fail. `PathTranslator` covers Jellyfin and the Sonarr/Radarr path settings. A Plex path must match.
- **AniList rate limit**: 90 requests per minute, sometimes 30. A first full scan of a large library is slow because of this limit, not because of the app.
- **AniList token lifetime**: the tokens are long-lived but expire. The schema has `refresh_token` and `expires_at`, but no refresh runs. The user links the account again.
- **Crunchyroll**: no official API. A change to the Crunchyroll website can stop the sync until the client changes.
- **Plex real-time sync**: Plex is polled each 15 minutes. There is no Plex webhook handler, and Plex webhooks need Plex Pass.
- **Title variability**: romanization, English and romaji titles, and season numbers differ across platforms. The matcher handles most cases. Manual overrides handle the rest.
- **Single process**: one SQLite writer. Do not run two containers on the same `/config`.

---

## 18. Decisions

### Series groups over a single AniList entry for each show

**Chose**: a group of entries linked by SEQUEL/PREQUEL.
**Over**: mapping a Plex show to one AniList entry.
**Because**: AniList has no parent series. Each season is its own entry, so one show
maps to many entries.
**Revisit when**: AniList adds a series object.

### SQLite in `/config`

**Chose**: one SQLite file through aiosqlite.
**Over**: an external database.
**Because**: one container, one user, no extra service to run on Unraid.
**Revisit when**: more than one process must write at the same time.

### Settings in the database, environment variables as overrides

**Chose**: the onboarding wizard and `/settings` save to `app_settings`. An environment variable wins if it is set.
**Over**: environment variables only.
**Because**: a home-server user sets up the services in a browser, and Unraid templates stay short.
**Revisit when**: no plan to.

### Debian slim over Alpine

**Chose**: `python:3.11-slim-bookworm`.
**Over**: `python:3.11-alpine`, the house default.
**Because**: the Crunchyroll login needs Chromium and chromedriver.
**Revisit when**: Crunchyroll has an API that needs no browser.

### `difflib` in the title matcher

**Chose**: `difflib.SequenceMatcher` in `TitleMatcher`.
**Over**: rapidfuzz, which the project uses elsewhere.
**Because**: the tests pin the exact scores. A change of library changes the scores.
**Revisit when**: the matcher is rewritten with new tests.

### Crunchyroll season title over season number

**Chose**: the season title as the key, with a 0.95 threshold.
**Over**: the Crunchyroll season number.
**Because**: Crunchyroll counts movies and arcs as seasons, so the numbers drift from AniList.
**Revisit when**: Crunchyroll changes its season model.

---

## 19. Source layout

```
Anilist-Link/
├── src/
│   ├── Main.py                     # entry point: config, database, migrations, scheduler, uvicorn
│   ├── Clients/                    # one client for each external API (see section 3)
│   ├── Matching/                   # TitleMatcher.py, Normalizer.py
│   ├── Scanner/                    # scanners, SeriesGroupBuilder, LibraryRestructurer,
│   │                               # LibraryScanner, LocalDirectoryScanner, show providers
│   ├── Sync/                       # watch syncers, preview runner, repair, download syncer,
│   │                               # watchlist refresh, AniList health monitor, synonyms backfill
│   ├── Download/                   # DownloadManager, MappingResolver, ArrPostProcessor,
│   │                               # SeasonRangeMapper, SonarrEpisodeMapper, ArrLinkVerifier,
│   │                               # ImportProcessor, MediaServerSync
│   ├── Web/
│   │   ├── App.py                  # create_app() factory
│   │   ├── ActivityTracker.py      # last-request time for the watchlist refresh
│   │   ├── Routes/                 # one module for each page or API area
│   │   ├── Templates/              # Jinja2 templates
│   │   └── Static/                 # style.css, file-browser.js, directory-picker.js,
│   │                               # naming-templates.js, rating-widget.js, media-detail.js,
│   │                               # table-filter.js, img/
│   ├── Database/                   # Connection.py, Models.py, Migrations.py
│   ├── Scheduler/                  # Jobs.py
│   └── Utils/                      # Config, Logging, NamingTemplate, NamingTranslator,
│                                   # PathTranslator, Time, Version (APP_VERSION)
├── tests/
│   ├── conftest.py
│   ├── Unit/                       # test_*.py
│   └── Integration/                # empty
├── scripts/
│   ├── Setup.sh                    # create the venv and install
│   ├── reset_for_testing.py        # reset the local database for manual tests
│   ├── test_*.py                   # manual smoke scripts against live services
│   └── dev-tools/                  # entrypoint.sh, and the dev-image database scripts
├── docs/                           # every document except README.md
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml                  # version comes from src/Utils/Version.py
```

---

## 20. Glossary

- **AniList**: anime and manga tracking site with a public GraphQL API
- **Series group**: AniList entries linked by SEQUEL/PREQUEL that make one logical show (9.2)
- **Cour**: a broadcast block of about 12 episodes. AniList often publishes each cour as its own entry
- **Structure A/B/C**: the three library layouts the scanner adapts to (9.4)
- **Pillar**: one of the four feature areas (1.1)
- **L1/L2/L3**: restructurer levels: folder rename, folder and file rename, full restructure
- **Smart Move**: the "Fix Location" action for one library item
- **Virtual season**: a Jellyfin season with no files on disk
- **HAMA**: HTTP AniDB Metadata Agent, a community Plex agent that uses AniDB
- **ASS**: Absolute Series Scanner, a community Plex scanner for absolute episode numbers
- **AniDB**: an anime database. Its entries can map to AniList IDs
- **Sonarr / Radarr**: download managers for TV series and for movies
- **Binhex**: the container conventions for volume paths and environment variables
- **PUID/PGID**: the user and group IDs that the container runs as
- **TTL**: time to live, the age at which cached data expires
- **BFS**: breadth-first search, the graph walk for relations
- **SSE**: Server-Sent Events, for live progress in scans and restructures

---

## 21. Project identification

**Project**: Anilist-Link
**Version**: 1.0.0
**Repository**: https://github.com/AnotherMike-exe/Anilist-Link
**Image**: `ghcr.io/anothermike-exe/anilist-link`
**License**: MIT
**Maintainer**: Plum Solutions
