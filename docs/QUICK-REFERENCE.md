# Quick Reference: Anilist-Link

> The commands, variables, paths and endpoints of this project. This file owns these
> tables. Other documents link here and do not repeat them.

---

## Daily commands

```bash
# Container
docker compose up -d                  # start
docker compose down                   # stop
docker compose pull                   # get the newest image
docker logs -f AnilistLink            # follow the container log
docker exec AnilistLink tail -f /config/logs/anilist_link.log   # follow the app log

# Local development (Python 3.12 venv in .venv/)
pip install -e ".[dev]"               # install with dev tools
python -m src.Main                    # run on http://localhost:9876

# Tests
pytest                                # all tests
pytest tests/Unit/test_title_matcher.py -x   # one file, stop on the first failure
pytest --cov=src                      # with coverage

# Lint and format (run black first, then ruff)
black src/
ruff check src/                       # add --fix for imports and unused names
mypy src/

# Image
docker build -t anilist-link .                                # release variant
docker build --build-arg BUILD_VARIANT=dev -t anilist-link .  # dev variant, adds dev-tools and sqlite3
```

Start the app through `src.Main`. `src/Web/App.py` has no module-level `app`, so
`uvicorn src.Web.App:app` does not work.

## Image and tags

Image: `ghcr.io/anothermike-exe/anilist-link`

| Tag | Built from | Variant |
|---|---|---|
| `latest` | push to `main`, and each `v*` tag | release |
| `1.0.0` (semver) | a `v1.0.0` tag | release |
| `dev` | push to `dev` | dev (adds `/config/dev-tools/` scripts and `sqlite3`) |

Platforms: `linux/amd64` and `linux/arm64`.

## Endpoints and ports

The dashboard and the API use port `9876` (TCP). Change it with `PORT`.

| What | Where |
|---|---|
| Dashboard | `http://localhost:9876/` |
| First-run wizard | `/onboarding` (the dashboard sends you here until onboarding is complete. Add `?skip_onboarding=1` to go past it) |
| Settings | `/settings` |
| OpenAPI docs | `/docs` |
| System status | `GET /api/status` |
| Background task progress | `GET /api/progress` |
| AniList availability | `GET /api/anilist/status`, `POST /api/anilist/check-now` |
| AniList account link | `GET /auth/anilist`, callback at `/auth/anilist/callback` |
| Connection tests | `POST /api/test/{anilist,plex,jellyfin,crunchyroll,sonarr,radarr}` |
| Library browsers | `/library`, `/plex`, `/jellyfin` |
| File restructure wizard | `/restructure` |
| Import of hand-fetched media | `/import` |
| Manual overrides | `/mappings` |
| Crunchyroll sync | `/crunchyroll`, `/crunchyroll/preview`, `/crunchyroll/history` |
| Plex and Jellyfin watch sync | `/watch-sync` |
| AniList watchlist | `/watchlist` |
| Downloads | `/download` |
| Admin tools | `/tools` |
| Sonarr webhook target | `POST /api/webhook/sonarr` |
| Radarr webhook target | `POST /api/webhook/radarr` |
| Jellyfin webhook target | `POST /jellyfin/webhook` |
| Glance widget (key required) | `GET /glance/rate-completed?key=<key>` |

The routes are in `src/Web/Routes/`. `/docs` lists every one.

## Configuration

### Container variables

| Variable | Default | Purpose |
|---|---|---|
| `PUID` / `PGID` | `99` / `100` | Host user and group that own the volumes and the media (`id -u`, `id -g`). `99`/`100` is `nobody`/`users` on Unraid |
| `UMASK` | `002` | File creation mask. `002` gives group-writable files |
| `TZ` | `UTC` | Time zone for the dashboard and the scheduled jobs, for example `America/New_York` |
| `DEBUG` | `false` | Verbose logs |
| `PORT` | `9876` | Listen port |
| `HOST` | `0.0.0.0` | Listen address |

### Application variables

You do not need these. The onboarding wizard and `/settings` save each value in the
database. If you set a variable, it overrides the database value and the settings page
shows the field as locked. The priority is: environment variable, then the database
setting, then the default.

| Variable | Default | Purpose |
|---|---|---|
| `ANILIST_CLIENT_ID` / `ANILIST_CLIENT_SECRET` | — | AniList OAuth app. Register it at [anilist.co/settings/developer](https://anilist.co/settings/developer) with the redirect URL `http://<host>:9876/auth/anilist/callback` |
| `APP_BASE_URL` | `http://localhost:9876` | The URL other services use to reach this app (webhooks) |
| `PLEX_URL` / `PLEX_TOKEN` | — | Plex server |
| `PLEX_ANIME_LIBRARIES` | `[]` | JSON list of Plex library keys to scan |
| `PLEX_WATCH_SYNC_ENABLED` | `false` | Plex watch sync |
| `JELLYFIN_URL` / `JELLYFIN_API_KEY` | — | Jellyfin server |
| `JELLYFIN_ANIME_LIBRARY_IDS` | `[]` | JSON list of Jellyfin library IDs to scan |
| `JELLYFIN_WATCH_SYNC_ENABLED` | `false` | Jellyfin watch sync |
| `CRUNCHYROLL_EMAIL` / `CRUNCHYROLL_PASSWORD` | — | Crunchyroll account |
| `FLARESOLVERR_URL` | — | Optional FlareSolverr for the Crunchyroll login |
| `HEADLESS_MODE` | `true` | Run Chromium without a window |
| `MAX_PAGES` | `10` | Crunchyroll history pages to read for each sync |
| `CR_AUTO_SYNC_ENABLED` | `true` | Scheduled Crunchyroll sync |
| `CR_AUTO_APPROVE` | `true` | Apply Crunchyroll changes without a manual preview approval |
| `CR_SYNC_TIME` | `02:00` | Daily time for the Crunchyroll sync (`HH:MM`). If it is empty, the sync runs each `SYNC_INTERVAL` minutes |
| `SONARR_URL` / `SONARR_API_KEY` | — | Sonarr server |
| `SONARR_ANIME_ROOT_FOLDER` | — | Sonarr root folder for anime |
| `SONARR_PATH_PREFIX` / `SONARR_LOCAL_PATH_PREFIX` | — | The same directory as Sonarr sees it, and as this container sees it |
| `RADARR_URL` / `RADARR_API_KEY` | — | Radarr server |
| `RADARR_ANIME_ROOT_FOLDER` | — | Radarr root folder for anime |
| `RADARR_PATH_PREFIX` / `RADARR_LOCAL_PATH_PREFIX` | — | The same directory as Radarr sees it, and as this container sees it |
| `SYNC_INTERVAL` | `15` | Minutes between Crunchyroll syncs when `CR_SYNC_TIME` is empty. Plex and Jellyfin watch syncs run each 15 minutes, a fixed value |
| `SCAN_INTERVAL` | `24` | Hours between scheduled Plex metadata scans |
| `LIBRARY_REINDEX_INTERVAL` | `6` | Hours between library reindexes |
| `WATCHLIST_REFRESH_INTERVAL` | `30` | Minutes between AniList watchlist refreshes. The refresh runs at startup, then only while the dashboard has recent activity, and during long syncs |
| `DOWNLOAD_SYNC_INTERVAL` | `60` | Minutes between watchlist-to-Sonarr/Radarr syncs |
| `DOWNLOAD_AUTO_STATUSES` | `CURRENT` | Comma-separated AniList statuses to add to Sonarr/Radarr on their own |
| `DOWNLOAD_MONITOR_MODE` | `future` | Sonarr monitor mode for new series |
| `DOWNLOAD_AUTO_SEARCH` | `false` | Start a search when a series is added |
| `TITLE_DISPLAY` | `romaji` | Title language in the dashboard |
| `APP_SHOW_UNRATED_COMPLETED` | `true` | Show the "Rate Your Completed Shows" card |
| `LIBRARY_IMPORT_PATH` | — | Folder that `/import` reads, for example `/media/import` |
| `LIBRARY_SPLIT_MOVIES_TV` | `false` | Put movies and TV in different output folders |
| `LIBRARY_MOVIE_OUTPUT_PATH` / `LIBRARY_TV_OUTPUT_PATH` | — | The output folders when the split is on |
| `NAMING_FILE_TEMPLATE` | `{title} - S{season}E{episode}` | Episode file name |
| `NAMING_FOLDER_TEMPLATE` | `{title}` | Show folder name |
| `NAMING_SEASON_FOLDER_TEMPLATE` | `Season {season}` | Season folder name |
| `NAMING_MOVIE_FILE_TEMPLATE` | `{title} [{year}]` | Movie file name |
| `NAMING_ILLEGAL_CHAR_REPLACEMENT` | — | Replacement for characters a file system does not accept |

The full map of settings keys to variables is `SETTINGS_MAP` in `src/Utils/Config.py`.

## Paths

| Path | Holds |
|---|---|
| `/config` | `anilist_link.db` (SQLite), `logs/anilist_link.log`, the Chromium profile for Crunchyroll, and `dev-tools/` on a dev image |
| `/data` | Reserved. The image declares it, but the app does not use it |
| `/media/anime` | Your anime library. Use the same container path that Plex or Jellyfin uses. It must be writable by `PUID:PGID` |
| `/media/import` | Optional. A drop folder for media you got by hand. Set `LIBRARY_IMPORT_PATH` to it |

The library path under `/media` is not fixed. Use any path, but make it agree with
your media server:

```
Host:          /mnt/user/media/anime
Plex:          /mnt/user/media/anime -> /media/anime
Jellyfin:      /mnt/user/media/anime -> /media/anime
Anilist-Link:  /mnt/user/media/anime -> /media/anime   <- must match
```

Logs: `docker logs AnilistLink` shows the container output. The app log is
`/config/logs/anilist_link.log`, which rotates at 5 MB and keeps three old files.
There is no supervisord.

## Glance integration

The "Rate Your Completed Shows" card can show in a
[Glance](https://github.com/glanceapp/glance) dashboard as an `iframe` widget.

1. In Anilist-Link, go to **Settings → Integrations** and click **Generate key**.
2. Click **Copy Glance snippet**.
3. Paste the first part of the snippet into `glance.yml`, under the top-level
   `document.head`. This listener resizes the iframe to its content. You add it one
   time, for all widgets of this type:

   ```yaml
   document:
     head: |
       <script>
       window.addEventListener('message', function (event) {
         if (!event.data || event.data.source !== 'anilist-link-glance') return;
         document.querySelectorAll('iframe[src*="/glance/rate-completed"]').forEach(function (frame) {
           var h = Math.max(60, event.data.height + 20);
           frame.style.height = h + 'px';
           frame.setAttribute('height', h);
         });
       });
       </script>
   ```

4. Paste the second part under the page or column that must show the card:

   ```yaml
   - type: iframe
     title: Rate Completed Shows
     source: http://<your-anilist-link-host>:9876/glance/rate-completed?key=<your-key>
     height: 60   # start value. The listener changes it after the page loads
   ```

5. Restart Glance.

The widget lists each AniList entry that is Completed and has no score. You can rate
it from the tile. The background is transparent, so the tile uses your Glance theme.

`/glance/*` is the only route that needs a key. The rest of the app trusts the local
network. When you generate a new key, the old key stops working immediately.

## Troubleshooting

### Renames or moves fail with "permission denied"
→ `PUID` and `PGID` must own the media on the host: `ls -ln /mnt/user/media/anime`
→ The entrypoint changes the owner of `/config` only. It does not change the media
library.

### The restructurer cannot find the files that Plex or Jellyfin reports
→ The media is mounted at a different container path here than in the media server.
Mount it at the same path. See [Paths](#paths).
→ For Sonarr and Radarr, set the remote path and the local path under
**Settings → Sonarr** or **Settings → Radarr**.

### Crunchyroll login fails or Chromium crashes
→ Set `shm_size: "2g"` in compose. The default shared memory is too small for Chromium.
→ Read `/config/logs/anilist_link.log` for the Selenium error. The Crunchyroll API is
not official, and it can change without notice.

### A banner says that AniList is down
→ AniList disabled its API or decreased its rate limit. Scans and syncs pause.
→ Anilist-Link checks one time each hour. Click **Check now** in the banner to check
immediately. You do not need to restart anything.

### Timestamps show the wrong hour
→ Set `TZ` to a valid zone name. At startup the entrypoint logs
`unknown timezone '<name>' - staying on UTC` if the name is wrong.

### A Crunchyroll season went to the wrong AniList entry
→ Follow [SEASON-MAPPING-REPAIR.md](SEASON-MAPPING-REPAIR.md).

### Jellyfin shows duplicate "virtual" seasons
→ The `jellyfin_virtual_cleanup` job removes them after each Jellyfin scan. To look at
one series, use `GET /api/jellyfin/virtual-items?series_id=<id>`.

### The container does not start
→ Read `docker logs AnilistLink` first.
→ Make sure that port 9876 is free: `lsof -i :9876`.

## Project links

- [Architecture](ARCHITECTURE.md)
- [Dev setup](DEV-SETUP.md)
- [Issues](https://github.com/AnotherMike-exe/Anilist-Link/issues)
