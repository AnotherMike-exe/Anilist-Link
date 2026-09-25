# Anilist-Link

> Connects AniList to Plex, Jellyfin, Crunchyroll, Sonarr and Radarr from one self-hosted container.

Anilist-Link is for anime collectors who track their shows on AniList and keep a
local library. It renames and reorganizes anime files from AniList data, writes
AniList metadata to Plex and Jellyfin, and syncs watch progress to AniList. It also
sends add requests to Sonarr and Radarr with the AniList alternative titles.

- **File organization**: a wizard that renames folders and files, or restructures the full library
- **Metadata**: AniList titles, summaries, posters, genres and ratings in Plex and Jellyfin
- **Watch sync**: Crunchyroll to AniList, and two-way sync with Plex and Jellyfin
- **Downloads**: Sonarr and Radarr add requests, with post-download file organization
- **Rate completed shows**: a dashboard card for completed AniList entries that have no score

---

## Usage

1. Save this as `docker-compose.yml`:

   ```yaml
   services:
     AnilistLink:
       image: ghcr.io/anothermike-exe/anilist-link:latest
       container_name: AnilistLink
       restart: unless-stopped
       shm_size: "2g"
       volumes:
         - /mnt/user/appdata/AnilistLink:/config
         - /mnt/user/media/anime:/media/anime
       environment:
         - PUID=99
         - PGID=100
         - UMASK=002
         - TZ=America/New_York
       ports:
         - "9876:9876"
   ```

2. Start the container:

   ```bash
   docker compose up -d
   ```

3. Open `http://localhost:9876`. The onboarding wizard starts on the first visit.
4. Follow the wizard to add your media folders and link AniList, Plex, Jellyfin,
   Crunchyroll, Sonarr and Radarr. Each service is optional.

Working when: the dashboard at `http://localhost:9876` shows your linked AniList
account.

## Details

The dashboard runs on port 9876. The database and the logs live in `/config`
(`anilist_link.db` and `logs/anilist_link.log`). Set `PUID` and `PGID` to the host
user that owns your media, or renames fail with permission errors. The full tables of
variables, volumes, ports and endpoints are in
[docs/QUICK-REFERENCE.md](docs/QUICK-REFERENCE.md).

Known limitations:

- **Media path alignment.** Anilist-Link moves the files that Plex or Jellyfin
  report. Mount your anime library at the same container path that your media server
  uses, or the reported paths do not exist inside this container.
- **Shared memory for Crunchyroll.** The Crunchyroll client runs Chromium, which
  needs `shm_size: "2g"`. If you do not use Crunchyroll, you can decrease it to
  `512m` or remove it.
- **AniList outages.** When AniList disables its API, a banner shows on the
  dashboard and all scans and syncs pause. Anilist-Link checks one time each hour
  and continues on its own when AniList recovers.
- **Glance.** The "Rate Your Completed Shows" card can show in a
  [Glance](https://github.com/glanceapp/glance) dashboard. The setup is in
  [docs/QUICK-REFERENCE.md](docs/QUICK-REFERENCE.md#glance-integration).

## Documentation

- [Architecture](docs/ARCHITECTURE.md): system design and the decisions behind it
- [Dev setup](docs/DEV-SETUP.md): from a clone to a running copy
- [Quick reference](docs/QUICK-REFERENCE.md): commands, variables, ports, endpoints and troubleshooting
- [Season mapping repair](docs/SEASON-MAPPING-REPAIR.md): a one-time cleanup for AniList entries that the Crunchyroll season-mapping bug changed

## Attributions

- [AniList API](https://anilist.gitbook.io/anilist-apiv2-docs): the source of all metadata and the target of watch sync
- Crunchyroll-Anilist-Sync: the predecessor project by the same author. Anilist-Link replaces it.
- [FastAPI](https://fastapi.tiangolo.com/): the web dashboard and API
- [httpx](https://www.python-httpx.org/): the HTTP client for all external services
- [APScheduler](https://github.com/agronholm/apscheduler): the scheduled scans and syncs
- [rapidfuzz](https://github.com/rapidfuzz/RapidFuzz): fuzzy title comparison
- [Selenium](https://www.selenium.dev/) and [undetected-chromedriver](https://github.com/ultrafunkamsterdam/undetected-chromedriver): the Crunchyroll login

MIT — see [LICENSE](LICENSE)

---

**Repository**: https://github.com/AnotherMike-exe/Anilist-Link · **Maintainer**: Plum Solutions
