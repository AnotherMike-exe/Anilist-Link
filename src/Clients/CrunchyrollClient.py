"""Crunchyroll watch history client.

Ported from the original Crunchyroll-Anilist-Sync codebase.  All browser
operations are wrapped in ``asyncio.to_thread()`` so the FastAPI event
loop is never blocked.  Auth session is persisted in the SQLite DB via
:class:`~src.Database.Connection.DatabaseManager` (replaces the old
file-based ``AuthCache``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

CRUNCHYROLL_BASE = "https://www.crunchyroll.com"
LOGIN_URL = f"{CRUNCHYROLL_BASE}/login"

# Chrome gets a throwaway profile per launch (see _prepare_chrome_profile).
_CHROME_PROFILE_PREFIX = "anilist-link-chrome-"
# Only profiles older than this are swept, so a concurrent run is never
# pulled out from under.
_STALE_PROFILE_AGE_SECONDS = 2 * 60 * 60


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class CrunchyrollEpisode:
    """A single episode parsed from the CR watch-history API."""

    series_title: str
    episode_number: int
    season: int = 1
    episode_title: str = ""
    season_title: str = ""
    raw_season_number: int | None = None
    season_display_number: str = ""
    fully_watched: bool = False
    is_movie: bool = False
    is_compilation: bool = False
    watch_date: str = ""
    raw_metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class CrunchyrollClient:
    """Crunchyroll watch history client using browser-based authentication."""

    def __init__(
        self,
        email: str,
        password: str,
        *,
        headless: bool = True,
        flaresolverr_url: str = "",
        max_pages: int = 10,
        db: Any | None = None,
    ) -> None:
        self._email = email
        self._password = password
        self._headless = headless
        self._flaresolverr_url = flaresolverr_url
        self._max_pages = max_pages
        self._db = db  # DatabaseManager for session persistence
        self._loop: asyncio.AbstractEventLoop | None = None
        self._driver: Any = None
        self._profile_dir: str = ""
        self._access_token: str = ""
        self._account_id: str = ""
        self._device_id: str = ""

    # ==================================================================
    # Public async API
    # ==================================================================

    async def authenticate(self) -> bool:
        """Authenticate with Crunchyroll via Selenium in a thread."""
        if not self._email or not self._password:
            logger.error("Crunchyroll credentials not configured")
            return False

        try:
            self._loop = asyncio.get_running_loop()
            return await asyncio.to_thread(self._sync_authenticate)
        except Exception:
            logger.exception("Crunchyroll authentication failed")
            return False

    async def get_watch_history_page(
        self, page_num: int = 1
    ) -> list[CrunchyrollEpisode]:
        """Fetch a single page of watch history from the CR API."""
        if not self._access_token or not self._account_id:
            logger.error("Not authenticated - call authenticate() first")
            return []

        try:
            return await asyncio.to_thread(self._sync_get_watch_history_page, page_num)
        except Exception:
            logger.exception("Failed to fetch watch history page %d", page_num)
            return []

    async def cleanup(self) -> None:
        """Close browser and release resources."""
        if self._driver:
            try:
                await asyncio.to_thread(self._driver.quit)
            except Exception:
                logger.debug("Error closing browser", exc_info=True)
            self._driver = None
        if self._profile_dir:
            shutil.rmtree(self._profile_dir, ignore_errors=True)
            self._profile_dir = ""
        self._access_token = ""
        self._account_id = ""

    # ==================================================================
    # Sync auth flow (runs in thread)
    # ==================================================================

    def _sync_authenticate(self) -> bool:
        """Full auth chain: cache -> verify -> fresh login fallback."""
        logger.info("Authenticating with Crunchyroll...")

        self._access_token = ""
        self._account_id = ""
        self._device_id = ""

        # Check for cached auth in DB
        has_cached = self._has_cached_auth()

        if not has_cached:
            logger.info("No cached auth found, performing fresh login...")
            self._setup_driver()
            if self._perform_fresh_authentication():
                logger.info("Fresh authentication successful")
                return True
            return False

        logger.info("Found cached auth, validating...")
        self._setup_driver()

        if self._try_cached_auth() and self._verify_authentication():
            logger.info("Using cached authentication")
            return True

        logger.info("Cached auth invalid, performing fresh authentication...")
        self._clear_cached_auth()

        if self._perform_fresh_authentication():
            logger.info("Fresh authentication successful after cache failure")
            return True

        logger.error("All authentication methods failed")
        return False

    def _perform_fresh_authentication(self) -> bool:
        """Perform fresh authentication with Crunchyroll."""
        logger.info("Performing fresh authentication...")

        if self._flaresolverr_url:
            logger.info("Using FlareSolverr for authentication")
            if self._authenticate_via_flaresolverr():
                return True

        if not self._authenticate_via_browser():
            logger.error("Browser authentication failed")
            return False

        self._capture_tokens_post_login()
        self._cache_authentication()
        return True

    def _authenticate_via_browser(self) -> bool:
        """Authenticate using browser automation."""
        from selenium.webdriver.support.ui import WebDriverWait

        try:
            self._driver.get(LOGIN_URL)
            time.sleep(3)

            if not self._handle_cloudflare_challenge():
                logger.warning("Cloudflare challenge handling timeout")

            wait = WebDriverWait(self._driver, 20)

            email_field = self._find_form_field(
                wait,
                ['input[type="email"]', 'input[name="email"]', "#email"],
            )
            password_field = self._find_form_field(
                wait,
                ['input[type="password"]', 'input[name="password"]', "#password"],
            )

            if not email_field or not password_field:
                logger.error("Could not locate login form fields")
                return False

            email_field.clear()
            email_field.send_keys(self._email)
            time.sleep(1)

            password_field.clear()
            password_field.send_keys(self._password)
            time.sleep(1)

            submit_button = self._find_form_field(
                wait,
                [
                    'button[type="submit"]',
                    "button.submit-button",
                    'input[type="submit"]',
                ],
                wait_for_presence=False,
            )

            if submit_button:
                submit_button.click()
            else:
                password_field.submit()

            time.sleep(12)

            if "login" in self._driver.current_url.lower():
                logger.error("Still on login page after submission")
                return False

            logger.info("Browser authentication successful")
            return True

        except Exception as exc:
            logger.error("Browser authentication error: %s", exc)
            return False

    def _authenticate_via_flaresolverr(self) -> bool:
        """FlareSolverr auth: CF bypass, cookie transfer, login, tokens, cache."""
        from selenium.webdriver.support.ui import WebDriverWait

        try:
            import httpx as _httpx

            logger.info("Step 1: Using FlareSolverr to bypass Cloudflare...")
            resp = _httpx.post(
                f"{self._flaresolverr_url}/v1",
                json={
                    "cmd": "request.get",
                    "url": f"{CRUNCHYROLL_BASE}/login",
                    "maxTimeout": 60000,
                },
                timeout=90,
            )
            if resp.status_code != 200:
                logger.error("FlareSolverr request failed: %d", resp.status_code)
                return False

            flare_solution = resp.json().get("solution", {})
            if not flare_solution:
                logger.error("No solution in FlareSolverr response")
                return False

            cloudflare_cookies = flare_solution.get("cookies", [])
            logger.info(
                "FlareSolverr bypassed Cloudflare, got %d cookies",
                len(cloudflare_cookies),
            )

            # Step 2: Transfer cookies to Selenium
            logger.info("Step 2: Transferring Cloudflare cookies to driver...")
            self._driver.get(CRUNCHYROLL_BASE)
            time.sleep(2)

            for cookie in cloudflare_cookies:
                try:
                    cookie_data: dict[str, Any] = {
                        "name": cookie.get("name"),
                        "value": cookie.get("value"),
                        "domain": cookie.get("domain", ".crunchyroll.com"),
                        "path": cookie.get("path", "/"),
                    }
                    if cookie.get("secure") is not None:
                        cookie_data["secure"] = cookie.get("secure")
                    if cookie.get("httpOnly") is not None:
                        cookie_data["httpOnly"] = cookie.get("httpOnly")
                    self._driver.add_cookie(cookie_data)
                except Exception as exc:
                    logger.debug("Failed to add cookie %s: %s", cookie.get("name"), exc)

            # Step 3: Login via Selenium with CF bypassed
            logger.info("Step 3: Performing login via Selenium...")
            self._driver.get(LOGIN_URL)
            time.sleep(3)

            page_source = self._driver.page_source.lower()
            if any(
                ind in page_source
                for ind in ["checking your browser", "cloudflare", "just a moment"]
            ):
                logger.warning("Still seeing Cloudflare challenge, waiting...")
                time.sleep(5)

            wait = WebDriverWait(self._driver, 20)

            email_field = self._find_form_field(
                wait,
                ['input[type="email"]', 'input[name="email"]', "#email"],
            )
            password_field = self._find_form_field(
                wait,
                ['input[type="password"]', 'input[name="password"]', "#password"],
            )

            if not email_field or not password_field:
                logger.error("Could not locate login form fields")
                return False

            email_field.clear()
            email_field.send_keys(self._email)
            time.sleep(1)

            password_field.clear()
            password_field.send_keys(self._password)
            time.sleep(1)

            submit_button = self._find_form_field(
                wait,
                [
                    'button[type="submit"]',
                    "button.submit-button",
                    'input[type="submit"]',
                ],
                wait_for_presence=False,
            )

            if submit_button:
                submit_button.click()
            else:
                password_field.submit()

            time.sleep(12)

            if "login" in self._driver.current_url.lower():
                logger.error("Still on login page after submission")
                return False

            logger.info("Login successful via FlareSolverr + Selenium")

            # Step 4: Capture tokens
            logger.info("Step 4: Capturing authentication tokens...")
            account_id = self._capture_tokens_post_login()
            if not account_id:
                logger.error("Failed to capture tokens after login")
                return False

            # Step 5: Cache authentication
            logger.info("Step 5: Caching authentication...")
            self._cache_authentication()

            logger.info("FlareSolverr authentication completed successfully")
            return True

        except Exception as exc:
            logger.error("FlareSolverr authentication failed: %s", exc)
            return False

    # ==================================================================
    # Token capture (critical JS scripts from old code)
    # ==================================================================

    def _capture_tokens_post_login(self) -> str | None:
        """POST to /auth/v1/token with etp_rt_cookie grant."""
        try:
            logger.info("Capturing authentication tokens via token endpoint...")
            device_id = self._get_or_create_device_id()

            token_response = self._driver.execute_script(
                """
                const deviceId = arguments[0];

                return fetch("https://www.crunchyroll.com/auth/v1/token", {
                    method: "POST",
                    headers: {
                        "accept": "*/*",
                        "accept-language": "en-US,en;q=0.9",
                        "authorization": "Basic bm9haWhkZXZtXzZpeWcwYThsMHE6",
                        "content-type": "application/x-www-form-urlencoded",
                        "sec-fetch-dest": "empty",
                        "sec-fetch-mode": "cors",
                        "sec-fetch-site": "same-origin"
                    },
                    referrer: "https://www.crunchyroll.com/history",
                    body: [
                        "device_id=" + deviceId,
                        "device_type=Chrome",
                        "grant_type=etp_rt_cookie"
                    ].join("&"),
                    mode: "cors",
                    credentials: "include"
                })
                .then(response => {
                    if (!response.ok) {
                        return {
                            success: false,
                            status: response.status,
                            statusText: response.statusText
                        };
                    }
                    return response.json().then(data => ({
                        success: true,
                        status: response.status,
                        data: data
                    }));
                })
                .catch(error => ({
                    success: false,
                    error: error.message
                }));
            """,
                device_id,
            )

            if not token_response or not token_response.get("success"):
                status = (
                    token_response.get("status", "unknown")
                    if token_response
                    else "no response"
                )
                logger.error("Browser token request failed: %s", status)
                return None

            data = token_response.get("data", {})
            account_id = data.get("account_id")
            self._access_token = data.get("access_token", "")
            self._account_id = account_id or ""
            self._device_id = device_id

            if account_id:
                logger.info("Got account ID via browser: %s...", account_id[:8])
            else:
                logger.error("No account_id in token response")

            return account_id

        except Exception as exc:
            logger.error("Error capturing tokens: %s", exc)
            return None

    def _verify_cached_token(self) -> bool:
        """Verify cached access token by test-fetching watch-history?page_size=1."""
        try:
            test_response = self._driver.execute_script(
                """
                const accountId = arguments[0];
                const accessToken = arguments[1];

                return fetch(
                    `https://www.crunchyroll.com/content/v2/${accountId}/watch-history?locale=en-US&page_size=1`,
                    {
                        headers: {
                            'Authorization': `Bearer ${accessToken}`,
                            'Accept': 'application/json'
                        },
                        credentials: 'include'
                    }
                )
                .then(response => ({
                    success: response.ok,
                    status: response.status
                }))
                .catch(error => ({
                    success: false,
                    error: error.message
                }));
            """,
                self._account_id,
                self._access_token,
            )

            if test_response and test_response.get("success"):
                return True

            logger.info("Cached token invalid, refreshing...")
            return self._refresh_access_token()

        except Exception:
            logger.debug("Error verifying cached token", exc_info=True)
            return self._refresh_access_token()

    def _refresh_access_token(self) -> bool:
        """Refresh the access token using the current session."""
        try:
            logger.info("Refreshing access token...")
            account_id = self._capture_tokens_post_login()
            if account_id:
                self._cache_authentication()
                logger.info("Access token refreshed successfully")
                return True
            logger.error("Failed to refresh access token")
            return False
        except Exception as exc:
            logger.error("Error refreshing access token: %s", exc)
            return False

    # ==================================================================
    # Device ID
    # ==================================================================

    def _get_or_create_device_id(self) -> str:
        """Get existing device_id from browser or create a consistent one."""
        try:
            if self._device_id:
                return self._device_id

            device_id = self._scan_device_id()
            if device_id:
                return device_id

            email_hash = hashlib.sha256(self._email.encode()).hexdigest()[:16]
            device_id = f"web-{email_hash}-{uuid.uuid4()}"
            logger.info("Created new device_id: %s...", device_id[:20])
            return device_id

        except Exception as exc:
            logger.error("Error getting device_id: %s", exc)
            return f"web-{uuid.uuid4()}"

    def _scan_device_id(self) -> str | None:
        """Scan localStorage for existing device_id."""
        try:
            device_id = self._driver.execute_script("""
                const storage = window.localStorage;
                const keys = Object.keys(storage);
                for (let key of keys) {
                    if (key.includes('device_id') || key.includes('deviceId')) {
                        return storage.getItem(key);
                    }
                }
                return null;
            """)
            return device_id
        except Exception:
            return None

    # ==================================================================
    # Session caching (DB-backed, replaces old file-based AuthCache)
    # ==================================================================

    def _run_db_coro(self, coro):  # type: ignore[type-arg]
        """Run an async DB coroutine on the main event loop from a worker thread."""
        if not self._loop:
            raise RuntimeError("Event loop not captured — call authenticate() first")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=10)

    def _has_cached_auth(self) -> bool:
        if not self._db:
            return False
        try:
            cached = self._run_db_coro(self._db.load_cr_session())
            if not cached:
                return False
            return bool(cached.get("cookies_json")) or bool(
                cached.get("access_token") and cached.get("account_id")
            )
        except Exception:
            return False

    def _try_cached_auth(self) -> bool:
        """Load cached cookies/tokens from DB and apply to browser."""
        cached = self._load_cached_sync()
        if not cached:
            return False

        logger.info("Testing cached authentication...")
        try:
            self._driver.get(CRUNCHYROLL_BASE)
            time.sleep(2)

            cookies = json.loads(cached.get("cookies_json", "[]"))
            logger.info("Loading %d cached cookies...", len(cookies))

            for cookie in cookies:
                try:
                    cookie_data: dict[str, Any] = {
                        "name": cookie.get("name"),
                        "value": cookie.get("value"),
                        "domain": cookie.get("domain", ".crunchyroll.com"),
                        "path": cookie.get("path", "/"),
                    }
                    for fld in ["secure", "httpOnly"]:
                        if cookie.get(fld) is not None:
                            cookie_data[fld] = cookie.get(fld)
                    self._driver.add_cookie(cookie_data)
                except Exception:
                    continue

            self._access_token = cached.get("access_token", "")
            self._account_id = cached.get("account_id", "")
            self._device_id = cached.get("device_id", "")

            if self._access_token and self._account_id:
                logger.info("Cached access token and account ID loaded")

            return True

        except Exception as exc:
            logger.error("Error loading cached auth: %s", exc)
            return False

    def _cache_authentication(self) -> None:
        """Persist auth data to the database."""
        if not self._db or not self._driver:
            return
        try:
            cookies = self._driver.get_cookies()
            cookies_json = json.dumps(cookies, default=str)

            self._run_db_coro(
                self._db.save_cr_session(
                    cookies_json=cookies_json,
                    access_token=self._access_token or "",
                    account_id=self._account_id or "",
                    device_id=self._device_id or "",
                )
            )
            logger.info("Authentication cached successfully")
        except Exception as exc:
            logger.error("Error caching authentication: %s", exc)

    def _clear_cached_auth(self) -> None:
        if not self._db:
            return
        try:
            self._run_db_coro(self._db.clear_cr_session())
        except Exception:
            pass

    def _load_cached_sync(self) -> dict[str, Any] | None:
        """Load cached session synchronously (called from within a thread)."""
        if not self._db:
            return None
        try:
            return self._run_db_coro(self._db.load_cr_session())
        except Exception:
            return None

    # ==================================================================
    # Verification
    # ==================================================================

    def _verify_authentication(self) -> bool:
        """Verify auth by checking /account page for logged-in indicators."""
        try:
            logger.info("Verifying authentication...")
            self._driver.get(f"{CRUNCHYROLL_BASE}/account")
            time.sleep(3)

            if "login" in self._driver.current_url.lower():
                logger.info("Redirected to login page - not authenticated")
                return False

            page_source = self._driver.page_source.lower()
            logged_in_indicators = [
                "account",
                "profile",
                "subscription",
                "settings",
                "logout",
                "sign out",
                "premium",
            ]

            indicators_found = [
                ind for ind in logged_in_indicators if ind in page_source
            ]
            if not indicators_found:
                logger.info("No logged-in indicators found")
                return False

            logger.info("Account access verified")

            if self._access_token and self._account_id:
                if self._verify_cached_token():
                    logger.info("Full authentication verification successful")
                    return True

            logger.info("Basic authentication verification successful")
            return True

        except Exception as exc:
            logger.error("Error verifying authentication: %s", exc)
            return False

    # ==================================================================
    # Driver setup (Docker-compatible, ported with all flags)
    # ==================================================================

    @staticmethod
    def _ensure_browser_home() -> str:
        """Return a writable base dir for driver state and point HOME/XDG at it.

        The container runs as a non-root user (PUID/PGID) whose HOME is often
        unset or ``/``.  ``undetected_chromedriver`` resolves its patched-driver
        cache via ``appdirs``/XDG, which then targets ``/.local`` and fails with
        ``PermissionError``.  Redirecting these to a writable location
        (``/config`` in the container, a temp dir otherwise) fixes driver setup.

        This base holds the *driver* cache, which is worth persisting between
        runs.  Chrome's own profile deliberately lives elsewhere -- see
        :meth:`_prepare_chrome_profile`.
        """
        base: str | None = None
        for candidate in ("/config", os.environ.get("HOME", "")):
            if candidate and os.path.isdir(candidate) and os.access(candidate, os.W_OK):
                base = os.path.join(candidate, ".anilist-link-browser")
                break
        if base is None:
            base = os.path.join(tempfile.gettempdir(), "anilist-link-browser")

        for sub in ("", "cache", "data", "config"):
            try:
                os.makedirs(os.path.join(base, sub) if sub else base, exist_ok=True)
            except OSError as exc:  # pragma: no cover - defensive
                logger.warning("Could not create browser dir %s: %s", base, exc)

        os.environ["HOME"] = base
        os.environ["XDG_CACHE_HOME"] = os.path.join(base, "cache")
        os.environ["XDG_DATA_HOME"] = os.path.join(base, "data")
        os.environ["XDG_CONFIG_HOME"] = os.path.join(base, "config")
        return base

    @staticmethod
    def _clear_singleton_locks(profile_dir: str) -> None:
        """Remove Chrome's singleton lock files from a profile directory.

        ``SingletonLock``/``SingletonSocket``/``SingletonCookie`` are how Chrome
        detects an already-running instance for a profile.  If a previous Chrome
        was killed uncleanly they survive, and the next launch hands its command
        line to the (dead) "existing" instance and exits immediately -- which
        surfaces as ``SessionNotCreatedException: ... chrome not reachable``.
        """
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            path = os.path.join(profile_dir, name)
            try:
                if os.path.islink(path) or os.path.exists(path):
                    os.unlink(path)
                    logger.debug("Removed stale Chrome lock %s", path)
            except OSError as exc:  # pragma: no cover - defensive
                logger.debug("Could not remove %s: %s", path, exc)

    @staticmethod
    def _sweep_stale_profiles(parent: str) -> None:
        """Delete leftover per-launch profile dirs from earlier runs.

        Only directories older than :data:`_STALE_PROFILE_AGE_SECONDS` are
        removed, so a concurrently running sync is never pulled out from under.
        """
        cutoff = time.time() - _STALE_PROFILE_AGE_SECONDS
        try:
            names = os.listdir(parent)
        except OSError:  # pragma: no cover - defensive
            return
        for name in names:
            if not name.startswith(_CHROME_PROFILE_PREFIX):
                continue
            path = os.path.join(parent, name)
            try:
                if not os.path.isdir(path) or os.path.getmtime(path) > cutoff:
                    continue
            except OSError:  # pragma: no cover - defensive
                continue
            shutil.rmtree(path, ignore_errors=True)
            logger.debug("Swept stale Chrome profile %s", path)

    @staticmethod
    def _purge_legacy_profile(browser_home: str) -> None:
        """Remove the old fixed profile directory from the config volume.

        Earlier versions pinned Chrome to a single permanent profile under the
        browser home.  On a mounted config volume (a FUSE/shfs share on Unraid)
        that directory accumulates stale locks and is re-``chown``ed on every
        container start.  It is disposable browser state, so drop it once.
        """
        legacy = os.path.join(browser_home, "chrome-profile")
        if not os.path.isdir(legacy):
            return
        try:
            shutil.rmtree(legacy)
            logger.info("Removed legacy Chrome profile directory %s", legacy)
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("Could not remove legacy profile %s: %s", legacy, exc)

    @classmethod
    def _prepare_chrome_profile(cls, browser_home: str) -> str:
        """Return a fresh, local, lock-free Chrome profile directory.

        Chrome's profile needs real file locking and is cheap to recreate, so it
        gets a new directory per launch on local disk rather than one permanent
        directory on the mounted config volume.  Nothing of value is lost: the
        Crunchyroll session is persisted in the ``cr_session_cache`` table, not
        in the browser profile.
        """
        cls._purge_legacy_profile(browser_home)

        parent = tempfile.gettempdir()
        if not (os.path.isdir(parent) and os.access(parent, os.W_OK)):
            parent = browser_home
        cls._sweep_stale_profiles(parent)

        profile_dir = tempfile.mkdtemp(prefix=_CHROME_PROFILE_PREFIX, dir=parent)
        cls._clear_singleton_locks(profile_dir)
        return profile_dir

    @staticmethod
    def _find_chrome_binary() -> str:
        """Return the path to a usable Chrome/Chromium binary, or ""."""
        candidates = [
            os.environ.get("CHROME_BIN", ""),
            "/usr/bin/google-chrome",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
        return next((p for p in candidates if p and os.path.exists(p)), "")

    @staticmethod
    def _detect_chrome_major_version(chrome_binary: str) -> int | None:
        """Return Chrome's major version, or ``None`` if it cannot be read."""
        import subprocess

        cmd = [chrome_binary or "google-chrome", "--version"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("Could not run '%s': %s", " ".join(cmd), exc)
            return None

        output = result.stdout.strip() or result.stderr.strip()
        # Match the first numeric version (e.g. "146.0.7680.177") so that
        # Debian build suffixes like "(bookworm)" are ignored.
        version_match = re.search(r"(\d+)\.\d+", output)
        if not version_match:
            logger.warning("Could not parse Chrome version from: %r", output)
            return None

        chrome_version = int(version_match.group(1))
        logger.info("Detected Chrome major version: %s", chrome_version)
        return chrome_version

    def _build_chrome_options(self, profile_dir: str, chrome_binary: str) -> Any:
        """Build a FRESH ``ChromeOptions`` for a single launch attempt.

        ``undetected_chromedriver`` stamps ``options._session`` onto the object
        it is handed and refuses to accept it a second time ("you cannot reuse
        the ChromeOptions object"), so a retry must never pass the same
        instance back in.
        """
        import undetected_chromedriver as uc

        options = uc.ChromeOptions()

        # Keep Chrome's profile in this launch's throwaway directory.
        options.add_argument(f"--user-data-dir={profile_dir}")

        if chrome_binary:
            options.binary_location = chrome_binary
            logger.info("Using Chrome binary: %s", chrome_binary)
        else:
            logger.info("Chrome binary not found in standard paths; using auto-detect")

        if self._headless:
            options.add_argument("--headless=new")

        # Critical Docker flags
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-software-rasterizer")

        # Window and user agent
        options.add_argument("--window-size=1920,1080")
        options.add_argument(
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        # Anti-detection
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-extensions")

        # Stability improvements for Docker.  Chrome keeps only the LAST value
        # of a repeated switch, so every --disable-features flag has to be a
        # single combined argument.
        options.add_argument("--disable-background-networking")
        options.add_argument("--disable-background-timer-throttling")
        options.add_argument("--disable-backgrounding-occluded-windows")
        options.add_argument("--disable-breakpad")
        options.add_argument("--disable-component-extensions-with-background-pages")
        options.add_argument(
            "--disable-features=TranslateUI,BlinkGenPropertyTrees,"
            "VizDisplayCompositor"
        )
        options.add_argument("--disable-ipc-flooding-protection")
        options.add_argument("--disable-renderer-backgrounding")
        options.add_argument("--enable-features=NetworkService,NetworkServiceInProcess")
        options.add_argument("--force-color-profile=srgb")
        options.add_argument("--hide-scrollbars")
        options.add_argument("--metrics-recording-only")
        options.add_argument("--mute-audio")

        # NB: deliberately no --remote-debugging-port.  undetected_chromedriver
        # picks a free port, appends its own flag and then polls that port; a
        # hardcoded one collides with that choice and can leave the driver
        # waiting on a port Chrome never opened ("chrome not reachable").

        return options

    def _teardown_driver(self) -> None:
        """Quit a (possibly half-built) driver, swallowing any error."""
        driver, self._driver = self._driver, None
        if driver is None:
            return
        try:
            driver.quit()
        except Exception:  # pragma: no cover - best effort
            logger.debug("Error quitting Chrome driver", exc_info=True)

    def _setup_driver(self) -> None:
        """Initialize undetected Chrome WebDriver with Docker-compatible flags."""
        # Redirect HOME/XDG to a writable location BEFORE importing uc, so the
        # driver-patcher cache lands somewhere writable.
        browser_home = self._ensure_browser_home()

        import undetected_chromedriver as uc

        # undetected_chromedriver caches its patched driver under
        # ``Patcher.data_path`` -- a CLASS attribute resolved from HOME via
        # ``expanduser("~/...")`` AT IMPORT TIME.  If HOME was ever empty at
        # import, that frozen value becomes ``/.local`` and every driver launch
        # fails with PermissionError.  Pin it explicitly to our writable base so
        # the outcome no longer depends on when/where uc was first imported.
        uc_cache = os.path.join(browser_home, "uc-driver")
        try:
            os.makedirs(uc_cache, exist_ok=True)
            uc.Patcher.data_path = uc_cache
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("Could not set undetected_chromedriver cache: %s", exc)

        chrome_binary = self._find_chrome_binary()
        chrome_version = self._detect_chrome_major_version(chrome_binary)

        attempts: list[tuple[str, dict[str, Any]]] = []
        if chrome_version is not None:
            attempts.append(
                (
                    f"driver pinned to Chrome {chrome_version}",
                    {"version_main": chrome_version},
                )
            )
        attempts.append(("driver version auto-detected", {}))

        last_exc: Exception | None = None
        for description, kwargs in attempts:
            # Every attempt gets its own profile AND its own options object;
            # neither can be reused after a failed launch.
            profile_dir = self._prepare_chrome_profile(browser_home)
            options = self._build_chrome_options(profile_dir, chrome_binary)
            try:
                self._driver = uc.Chrome(options=options, use_subprocess=True, **kwargs)
            except Exception as exc:
                last_exc = exc
                logger.warning("Chrome launch failed (%s): %s", description, exc)
                self._teardown_driver()
                shutil.rmtree(profile_dir, ignore_errors=True)
                continue
            self._profile_dir = profile_dir
            logger.info("Chrome started (%s), profile %s", description, profile_dir)
            break
        else:
            raise RuntimeError(
                "Could not start Chrome for Crunchyroll authentication"
            ) from last_exc

        try:
            # Anti-detection script
            self._driver.execute_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            self._driver.set_page_load_timeout(60)
        except Exception:
            # Never leak the browser process if post-launch setup fails.
            self._teardown_driver()
            raise
        logger.info("Chrome driver setup completed")

    # ==================================================================
    # Cloudflare handling
    # ==================================================================

    def _handle_cloudflare_challenge(self, max_wait: int = 60) -> bool:
        """Wait for Cloudflare challenge to complete (polling loop)."""
        start_time = time.time()

        while time.time() - start_time < max_wait:
            try:
                page_source = self._driver.page_source.lower()

                cf_indicators = [
                    "checking your browser",
                    "cloudflare",
                    "please wait",
                    "ddos protection",
                    "security check",
                    "just a moment",
                ]

                if any(ind in page_source for ind in cf_indicators):
                    logger.info("Cloudflare challenge detected, waiting...")
                    time.sleep(5)
                    continue

                if any(
                    ind in page_source
                    for ind in ["email", "password", "sign in", "login"]
                ):
                    logger.info("Cloudflare challenge completed")
                    return True

                time.sleep(2)

            except Exception:
                time.sleep(2)

        logger.warning("Cloudflare challenge timeout")
        return False

    def _find_form_field(
        self, wait: Any, selectors: list[str], wait_for_presence: bool = True
    ) -> Any | None:
        """Find a form field using multiple CSS selectors."""
        from selenium.common.exceptions import NoSuchElementException, TimeoutException
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC

        for selector in selectors:
            try:
                if wait_for_presence:
                    element = wait.until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                    )
                else:
                    element = self._driver.find_element(By.CSS_SELECTOR, selector)

                if element.is_displayed():
                    return element
            except (TimeoutException, NoSuchElementException):
                continue
        return None

    # ==================================================================
    # Watch history fetching
    # ==================================================================

    def _sync_get_watch_history_page(
        self, page_num: int, page_size: int = 50
    ) -> list[CrunchyrollEpisode]:
        """Fetch a single page of watch history via browser JS API call."""
        self._driver.get(CRUNCHYROLL_BASE)
        time.sleep(1)

        # Verify or refresh token before each page
        if not self._verify_cached_token():
            logger.error("Token validation failed before fetching page %d", page_num)
            return []

        try:
            api_response = self._driver.execute_script(
                """
                const accountId = arguments[0];
                const pageSize = arguments[1];
                const pageNum = arguments[2];
                const accessToken = arguments[3];

                const apiUrl = `https://www.crunchyroll.com/content/v2/${accountId}/watch-history`;
                const params = new URLSearchParams({
                    locale: 'en-US',
                    page: pageNum,
                    page_size: pageSize,
                    preferred_audio_language: 'ja-JP'
                });

                const fullUrl = `${apiUrl}?${params.toString()}`;

                const headers = {
                    'Accept': 'application/json',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'sec-fetch-dest': 'empty',
                    'sec-fetch-mode': 'cors',
                    'sec-fetch-site': 'same-origin'
                };

                if (accessToken) {
                    headers['Authorization'] = `Bearer ${accessToken}`;
                }

                return fetch(fullUrl, {
                    method: 'GET',
                    headers: headers,
                    credentials: 'include',
                    mode: 'cors'
                })
                .then(response => {
                    if (!response.ok) {
                        return { success: false, status: response.status };
                    }
                    return response.json().then(data => ({
                        success: true,
                        data: data,
                        itemCount: data?.data?.length || 0
                    }));
                })
                .catch(error => ({ success: false, error: error.message }));
            """,
                self._account_id,
                page_size,
                page_num,
                self._access_token,
            )

            if not api_response or not api_response.get("success"):
                status = (
                    api_response.get("status", "unknown")
                    if api_response
                    else "no response"
                )
                logger.error("API request failed: %s", status)
                return []

            data = api_response.get("data", {})
            items = data.get("data", [])

            if not items:
                return []

            episodes = self._parse_api_response(items)

            if episodes:
                first_ep = episodes[0]
                last_ep = episodes[-1]
                logger.info(
                    "   First: %s E%d  |  Last: %s E%d",
                    first_ep.series_title,
                    first_ep.episode_number,
                    last_ep.series_title,
                    last_ep.episode_number,
                )

            logger.info("Page %d: Retrieved %d episodes", page_num, len(episodes))
            return episodes

        except Exception:
            logger.exception("Error fetching page %d", page_num)
            return []

    # ==================================================================
    # Parsing (ported from CrunchyrollParser)
    # ==================================================================

    def _parse_api_response(
        self, items: list[dict[str, Any]]
    ) -> list[CrunchyrollEpisode]:
        """Parse episodes from API response items with proper season detection."""
        episodes: list[CrunchyrollEpisode] = []
        skipped = 0

        for item in items:
            try:
                panel = item.get("panel", {})
                ep_meta = panel.get("episode_metadata", {})

                series_title = ep_meta.get("series_title", "").strip()
                episode_number = ep_meta.get("episode_number", 0)
                episode_title = panel.get("title", "").strip()
                season_title = ep_meta.get("season_title", "").strip()

                is_movie = self._is_movie_or_special_content(ep_meta)

                if not series_title:
                    skipped += 1
                    continue

                if not is_movie and (not episode_number or episode_number <= 0):
                    skipped += 1
                    continue

                if is_movie and (not episode_number or episode_number <= 0):
                    episode_number = 1

                if not is_movie and self._is_compilation_or_recap_content(
                    season_title, episode_title, ep_meta
                ):
                    skipped += 1
                    continue

                detected_season = self._extract_correct_season_number(ep_meta)

                season_display_number = ep_meta.get("season_display_number", "").strip()
                raw_season_number: int | None = None
                if season_display_number and season_display_number.isdigit():
                    try:
                        raw_season_number = int(season_display_number)
                    except ValueError:
                        raw_season_number = None

                episodes.append(
                    CrunchyrollEpisode(
                        series_title=series_title,
                        episode_number=episode_number,
                        season=detected_season,
                        episode_title=episode_title,
                        season_title=season_title,
                        raw_season_number=raw_season_number,
                        season_display_number=season_display_number,
                        fully_watched=item.get("fully_watched", False),
                        is_movie=is_movie,
                        is_compilation=False,
                        watch_date=item.get("date_played", ""),
                        raw_metadata=item,
                    )
                )

            except Exception as exc:
                logger.debug("Error parsing episode item: %s", exc)
                skipped += 1
                continue

        if skipped > 0:
            logger.debug("Skipped %d invalid items from API response", skipped)

        return episodes

    @staticmethod
    def _is_movie_or_special_content(ep_meta: dict[str, Any]) -> bool:
        """Conservative detection using ``|M|`` identifier."""
        identifier = ep_meta.get("identifier", "")
        if identifier and "|M|" in identifier:
            return True
        episode_number = ep_meta.get("episode_number")
        if episode_number is None:
            return True
        return False

    @staticmethod
    def _is_compilation_or_recap_content(
        season_title: str, episode_title: str, ep_meta: dict[str, Any]
    ) -> bool:
        """Detect compilation / recap content that should be skipped."""
        season_lower = season_title.lower() if season_title else ""
        episode_lower = episode_title.lower() if episode_title else ""

        indicators = ["compilation", "recap", "summary", "special collection"]
        for ind in indicators:
            if ind in season_lower or ind in episode_lower:
                return True
        return False

    def _extract_correct_season_number(self, ep_meta: dict[str, Any]) -> int:
        """Multi-strategy season extraction: title → sequence → raw."""
        if self._is_movie_or_special_content(ep_meta):
            return 0

        season_title = ep_meta.get("season_title", "")
        if season_title:
            extracted = self._extract_season_from_title(season_title)
            if extracted > 1:
                return extracted

        season_sequence = ep_meta.get("season_sequence_number", 0)
        if isinstance(season_sequence, int) and 1 <= season_sequence <= 10:
            return season_sequence

        raw_season_number = ep_meta.get("season_number", 1)
        if isinstance(raw_season_number, int) and 1 <= raw_season_number <= 10:
            return raw_season_number

        return 1

    @staticmethod
    def _extract_season_from_title(season_title: str) -> int:
        """Extract season number from season title string."""
        season_title_lower = season_title.lower()

        patterns = [
            (r"season\s*(\d+)", 1),
            (r"s(\d+)", 1),
            (r"(\d+)(?:st|nd|rd|th)\s*season", 1),
            (r"part\s*(\d+)", 1),
        ]

        for pattern, group in patterns:
            match = re.search(pattern, season_title_lower)
            if match:
                try:
                    season_num = int(match.group(group))
                    if 1 <= season_num <= 20:
                        return season_num
                except (ValueError, IndexError):
                    continue

        return 1
