# Dev Setup — Anilist-Link

From a fresh clone to a running, tested copy. If a step here fails, the project is
wrong, not the reader.

Target: 15 minutes on a machine that already has Python 3.12 and Docker.

## Prerequisites

| Tool | Version | Check |
|---|---|---|
| Python | 3.11 or newer (development uses 3.12) | `python3.12 --version` |
| Docker | 24 or newer | `docker --version` |
| git | any | `git --version` |
| Chromium or Chrome | any | needed only for Crunchyroll auth |

On macOS, the system Python can be older than 3.11. Install 3.12 with Homebrew
(`brew install python@3.12`).

## 1. Clone

```bash
git clone https://github.com/AnotherMike-exe/Anilist-Link.git
cd Anilist-Link
git config pull.rebase true
git switch dev
```

`pull.rebase` keeps history linear, which the ruleset on `main` enforces anyway. Setting
it locally means a plain `git pull` never creates a merge commit that is then refused.
Work lands on `dev`, and `dev` goes to `main` through a pull request.

## 2. Install

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Create `_resources/` if you keep working notes. It is in `.gitignore` and never reaches
the repository:

```bash
mkdir -p _resources/{Examples,Research,Assets,Notes}
```

## 3. Configure

```bash
cp .env.example .env
```

The app starts with an empty `.env`. The first run opens the onboarding wizard at
`/onboarding`, and settings saved there go into the database. The priority is the
environment variable, then the database setting, then the code default.

Fill these only for the service you work on:

| Variable | Needed for | Where to get it |
|---|---|---|
| `ANILIST_CLIENT_ID`, `ANILIST_CLIENT_SECRET` | account linking and any write to AniList | an API client at https://anilist.co/settings/developer |
| `PLEX_URL`, `PLEX_TOKEN` | Plex scan, metadata and watch sync | your Plex server |
| `JELLYFIN_URL`, `JELLYFIN_API_KEY` | Jellyfin scan, metadata and watch sync | Jellyfin dashboard, API keys |
| `SONARR_URL`, `SONARR_API_KEY` | downloads | Sonarr, Settings, General |
| `CRUNCHYROLL_EMAIL`, `CRUNCHYROLL_PASSWORD` | Crunchyroll watch sync | your Crunchyroll account |

The full list is in `.env.example` and `docs/QUICK-REFERENCE.md`.

Outside the container, the database is `./data/anilist_link.db` and the log is
`./data/logs/anilist_link.log`. If a `/config` directory exists on the machine, the app
uses `/config` instead.

## 4. Run

```bash
python -m src.Main
```

Working when: `curl -i http://localhost:9876/api/status` returns `200` with
`"status": "running"`.

Do not start the app with `uvicorn src.Web.App:app`. `src/Web/App.py` has no
module-level `app`, and `create_app()` needs objects that `src.Main` builds first.

Containerized instead:

```bash
docker build -t anilist-link .
docker build --build-arg BUILD_VARIANT=dev -t anilist-link:dev .   # adds dev-tools and sqlite3
docker compose up -d
docker compose logs -f
```

`docker-compose.yml` pulls `ghcr.io/anothermike-exe/anilist-link:latest`. To use a
local build, change its `image:` line to a `build:` block. The file has the lines
commented out.

## 5. Test

```bash
pytest                 # 774 tests, about 2 seconds
pytest --cov=src       # with coverage
ruff check src/
black --check src/     # run black src/ first to fix formatting
mypy src/
```

`tests/Integration/` holds no tests yet. CI runs the same commands. A change that passes
here passes there, and the reverse.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|

Add a row the first time somebody hits a problem. A troubleshooting table written in
advance guesses. One written from real failures does not.

## Where the rest is

| Question | Document |
|---|---|
| How it is built and why | `docs/ARCHITECTURE.md` |
| How Claude should work here | `docs/CLAUDE.md` |
| Commands, ports and paths at a glance | `docs/QUICK-REFERENCE.md` |
| Open work | `docs/TODO.md` |
| What it does, for a user | `README.md` |

Setting up a *new* Plum project rather than this one is the `new-project` skill's job,
and the Plum-wide standards are in `plum-standards`. Neither belongs in this file.
