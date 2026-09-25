# Changelog

All notable changes to this project are recorded here.

The format is [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-09-25

First tagged release.

Upgrade note: the image moves to `ghcr.io/anothermike-exe/anilist-link`. Docker Hub
(`dogberttech/anilist-link`) gets no more builds. Change the `image:` line in your
compose file. The `UMASK` default is now `002`. Set `UMASK=000` if you need the old
behavior.

### Added
- File organization: a wizard that renames folders, renames files, or restructures a
  full library from AniList series data, with preview and conflict handling
- Smart Move: relocate one library item that Sonarr or Radarr does not track
- Metadata: AniList titles, summaries, posters, genres and ratings for Plex and
  Jellyfin, with series groups built from AniList sequel and prequel links
- Watch sync: Crunchyroll to AniList with preview, approve and undo, and two-way sync
  with Plex and Jellyfin
- A list of Crunchyroll episodes that the sync could not place, with resolve and
  dismiss actions, and a repair path for entries that an older sync mapped wrong
- Downloads: Sonarr and Radarr add requests with AniList alternative titles,
  post-download file organization, and webhook automation
- Manual import of media from an import folder
- "Rate Your Completed Shows" dashboard card, with an optional Glance widget
- AniList outage handling: a dashboard banner, paused jobs, and automatic recovery
- A first-run onboarding wizard for every service
- Container images for `linux/amd64` and `linux/arm64`

### Fixed
- `/api/status` and the page footer showed version `0.1.0`
- `/api/webhook/info` returned an error instead of the Sonarr and Radarr webhook URLs

### Security
- The AniList sign-in result page escapes its query values. A crafted link could run
  script in the dashboard before this fix.
- Starlette 1.x and Jinja2 3.1.6 or later, which close open security advisories

[Unreleased]: https://github.com/AnotherMike-exe/Anilist-Link/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/AnotherMike-exe/Anilist-Link/releases/tag/v1.0.0
