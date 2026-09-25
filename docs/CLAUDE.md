# CLAUDE.md — Anilist-Link

Project memory for Claude Code. Keep it true: a line that is wrong is worse than a line
that is missing, because it is trusted.

## What this is

Anilist-Link is a self-hosted web service that connects AniList with media servers
(Plex, Jellyfin), Crunchyroll, and download managers (Sonarr, Radarr). One person runs
it for a home anime library. It does four jobs: it renames and reorganizes anime files
with AniList data, writes AniList metadata to Plex and Jellyfin, syncs watch progress to
AniList, and sends add requests to Sonarr and Radarr with AniList alternative titles.
The design is in `docs/ARCHITECTURE.md`.

**Stage**: v1.0.0, the first tagged release
**Deployed on**: Docker (Unraid or any Docker host)

## Stack

| Part | Choice |
|---|---|
| Language | Python 3.11 or newer (development uses 3.12) |
| Framework | FastAPI with server-rendered Jinja2 templates |
| Data store | SQLite through aiosqlite, `/config/anilist_link.db` in the container, `./data/anilist_link.db` locally |
| Jobs | APScheduler |
| HTTP client | httpx (async). Crunchyroll auth uses Selenium and Chromium |
| Matching | rapidfuzz |
| Runtime | Docker, `ghcr.io/anothermike-exe/anilist-link`, linux/amd64 and linux/arm64 |

## Layout

```
src/Clients/     one client class for each external API (AniList, Plex, Jellyfin, Crunchyroll, Sonarr, Radarr, Prowlarr, qBittorrent, TVMaze)
src/Database/    the only code that touches SQLite: Connection.py, Models.py, Migrations.py
src/Matching/    title normalization, fuzzy matching, season and cour parsing
src/Scanner/     metadata scan pipeline, series groups, library restructurer
src/Sync/        watch sync (Crunchyroll, Plex, Jellyfin), download sync, AniList health monitor
src/Download/    Sonarr and Radarr orchestration and post-processing
src/Scheduler/   APScheduler job registration (Jobs.py)
src/Web/         FastAPI app factory (App.py), Routes/, Templates/, Static/
src/Utils/       Config.py, logging, naming templates, path translation
src/Main.py      entry point: config, database, migrations, scheduler, uvicorn
tests/Unit/      the test suite
scripts/dev-tools/  container entrypoint and dev-only database scripts
docs/            every document except README.md
_resources/      dev references, never in git
```

## Commands

```bash
pip install -e ".[dev]"      # install (in a Python 3.12 venv)
python -m src.Main           # run locally on http://localhost:9876
pytest                       # test (774 pass)
ruff check src/              # lint
black --check src/           # format check. Run black src/ first, then ruff
mypy src/                    # type check
docker build -t anilist-link .                              # release image
docker build --build-arg BUILD_VARIANT=dev -t anilist-link . # dev image with dev-tools
docker compose up -d         # run the container
```

`src/Web/App.py` has no module-level `app`. `create_app()` needs the config, the
database, the AniList client and the scheduler, so start the app through `src.Main`.

## Conventions

**Naming**: files, directories and classes are PascalCase (`AnilistClient.py`,
`TitleMatcher`). Functions and variables are `snake_case`, because PEP 8 binds them and
every Python tool expects it. Test files are `test_*.py`, because pytest finds them by
that pattern. Constants and environment variables are `UPPER_SNAKE_CASE`. Settings keys
in `app_settings` are dotted `snake_case` (`plex.watch_sync_enabled`).

**Errors**: raise a specific exception class (`AniListUnavailableError`,
`SeriesAlreadyExistsError`), and log it with the client, the operation and the item. Never
swallow an exception. If one platform fails, the others continue.

**Project-specific rules**:
- All external HTTP calls go through a class in `src/Clients/`. No raw requests
  anywhere else.
- Only `src/Database/` imports `sqlite3` or `aiosqlite`. Use parameterized queries.
- Read configuration through `src/Utils/Config.py`. Business logic never reads an
  environment variable. The priority is environment variable, then the database
  setting, then the code default.
- Async for all I/O. Type hints on every function signature.
- A schema change is a new numbered migration in `src/Database/Migrations.py` plus the
  table change in `src/Database/Models.py`. The schema is at version 5, with 29 tables
  plus `schema_version`. Migrations run at startup.
- Never log an OAuth token or an API key.

## How to work here

**Plan mode** for anything beyond a single-file edit: a refactor, a schema change,
anything touching Docker or CI. Show the plan and wait for a yes.

**A subagent** for work that parallelizes — a research spike, reading a long reference,
an independent audit. The main thread integrates the result.

**Ask early.** An architecture question answered before the work costs a minute. The
same question answered after costs the work.

Standing rules for this repo: rebase, never merge. `_resources/` never enters git. CI
green before merge. Work lands on `dev`, and `dev` goes to `main` through a pull
request.

## The phase loop

Each phase of the build runs the same five steps.

1. `/resume-session` — restore the state the last phase left
2. Build. Name `tdd-workflow` only for a bug fix, where the test is the reproducer
3. `verification-loop` — the gate at the end of the phase
4. Review in parallel: `code-reviewer`, `security-reviewer` and `python-reviewer`
5. `/learn-eval`, then `/save-session`

Commit at the end of every phase. A local commit costs nothing and gives the review a
diff to read.

`blueprint` holds the phase plan between sessions. Each step in it carries a brief that a
fresh session can execute cold.

## Docker

Volumes, environment variables and ports are in `docs/QUICK-REFERENCE.md`. Read that
file rather than repeating it here.

What is true of this project and not of every Binhex container:

- The base image is `python:3.11-slim-bookworm`, not Alpine. Crunchyroll auth needs
  Chromium and chromedriver, and the Debian base keeps them compatible.
- There is no supervisord. `scripts/dev-tools/entrypoint.sh` applies `PUID`, `PGID`,
  `UMASK` and `TZ`, then starts the app as `PUID:PGID` through `gosu`.
- `BUILD_VARIANT=dev` adds the scripts in `scripts/dev-tools/` and `sqlite3`. The
  entrypoint copies the scripts to `/config/dev-tools/`.
- The media library and the import folder mount under `/media`. Sonarr and Radarr must
  see files that the app writes as the same `PUID:PGID` owner.
- Compose sets `shm_size: "2g"` for Chromium. Without Crunchyroll, `512m` is enough.

First place to look when the container misbehaves: `docker logs AnilistLink`, then
`/config/logs/anilist_link.log`. Set `DEBUG=true` for verbose logs.

## Gotchas

- **Season vs cour.** A Roman numeral or `Nth Season` in an AniList title names the
  season. A bare `Part N` or `Cour N` names a cour inside that season.
  `parse_season_and_cour()` in `src/Matching/TitleMatcher.py` is the single source of
  truth for the Crunchyroll season map and the restructurer. Cours share the season
  number of their parent, and episode numbers run on across the cours of a season. If
  you number each cour as its own season, every later season maps wrong.
  `tests/Unit/test_cr_season_mapping.py` pins Mushoku Tensei and Re:Zero together, so a
  fix that renumbers one breaks the other.
- **Crunchyroll season identity is the title, not the number.** Crunchyroll counts
  movies and arcs that AniList publishes as separate entries, so its season numbers
  drift. Demon Slayer has 7 Crunchyroll seasons against 5 AniList TV entries.
  `season_from_cr_season_title()` overrides the number when one season title matches
  near-exactly. The 0.95 threshold is deliberate. The scorer gives any substring pair
  0.90, so a lower threshold folds a movie arc (Infinity Castle) onto season 1.
- **Crunchyroll episode numbers** are per-season or franchise-absolute. An absolute
  reading that resolves to a season earlier than the reported one is wrong, so reject
  it. History is newest-first and paginated. Track the highest episode for each
  (series, season) across the whole run, never for each page.
- **The Crunchyroll API is reverse-engineered.** It can break without notice. Look in
  `_resources/Research/` for the latest findings.
- **AniList rate limit and outages.** AniList allows 90 requests a minute, and the
  client throttles with a token bucket. AniList sometimes disables its API (403
  "temporarily disabled") or cuts the limit. The circuit breaker in
  `src/Clients/AnilistHealth.py` then raises `AniListUnavailableError` at once. Do not
  write your own retry loop around an AniList call. A new batch loop over AniList calls
  must check `anilist_client.health.is_down` and stop.
- **Plex multi-user tokens.** The server admin token cannot track watch state for each
  user. Each user needs a token from the Plex.tv API (`plex_users` table).
- **Jellyfin NFO writer and LockData.** If the Jellyfin NFO saver is on, Jellyfin
  overwrites our NFO data. If LockData is set on a series or season, our changes do not
  propagate. Turn off the NFO saver, clear the locks, then do a "Replace all metadata"
  rescan.
- **Jellyfin virtual seasons** come back after `DELETE /Items/{id}` returns 204,
  because the refresh queue recreates them. Stop the scan task first
  (`JellyfinClient._stop_scan_task()`), then delete.
- **Jellyfin images.** Items in a mixed library are `Type=Movie`. An item fetch must
  request `ParentId` and `IsFolder` in `Fields`, or the hierarchy walk cannot find the
  show folder. Call `_refresh_item_images` only after a `folder.jpg` write, never after
  `RemoteImages/Download`.

## Where the rules live

The Plum Solutions standards — naming, Binhex Docker layout, documentation rules, git
workflow — are in the `plum-standards` skill, not copied here. A copy in every repo is a
copy that goes stale.

This file holds only what is true of **this** project. Endpoints are in
`docs/QUICK-REFERENCE.md` and in the FastAPI docs at `http://localhost:9876/docs`.
Open work is in `docs/TODO.md`.
