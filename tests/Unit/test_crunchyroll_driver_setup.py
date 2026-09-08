"""Tests for Crunchyroll Chrome/driver bootstrap.

These cover the failure that stopped Crunchyroll syncing entirely: Chrome was
pinned to one permanent profile directory, so a stale lock left the driver
unable to reach the browser, and the retry path then blew up on
``RuntimeError: you cannot reuse the ChromeOptions object`` -- masking the real
error in the logs.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import types
from typing import Any

import pytest

from src.Clients.CrunchyrollClient import (
    _CHROME_PROFILE_PREFIX,
    _STALE_PROFILE_AGE_SECONDS,
    CrunchyrollClient,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeOptions:
    """Stand-in for ``uc.ChromeOptions``."""

    def __init__(self) -> None:
        self.arguments: list[str] = []
        self.binary_location = ""
        self._session: Any = None

    def add_argument(self, argument: str) -> None:
        self.arguments.append(argument)


class FakeDriver:
    def __init__(self) -> None:
        self.quit_called = False
        self.page_load_timeout: int | None = None
        self.scripts: list[str] = []

    def execute_script(self, script: str, *args: Any) -> None:
        self.scripts.append(script)

    def set_page_load_timeout(self, seconds: int) -> None:
        self.page_load_timeout = seconds

    def quit(self) -> None:
        self.quit_called = True


class FakePatcher:
    data_path = ""


def install_fake_uc(monkeypatch: pytest.MonkeyPatch, chrome: Any) -> types.ModuleType:
    """Register a fake ``undetected_chromedriver`` module."""
    module = types.ModuleType("undetected_chromedriver")
    module.ChromeOptions = FakeOptions  # type: ignore[attr-defined]
    module.Patcher = FakePatcher  # type: ignore[attr-defined]
    module.Chrome = chrome  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "undetected_chromedriver", module)
    return module


def make_client() -> CrunchyrollClient:
    return CrunchyrollClient("user@example.com", "hunter2", headless=True)


@pytest.fixture
def sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Point tempfile and the browser home at an isolated directory."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    browser_home = tmp_path / "browser-home"
    browser_home.mkdir()
    monkeypatch.setattr(
        CrunchyrollClient,
        "_ensure_browser_home",
        staticmethod(lambda: str(browser_home)),
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Profile handling
# ---------------------------------------------------------------------------


def test_profile_is_fresh_and_local_on_every_call(sandbox: Any) -> None:
    """Each launch gets its own directory, so a stale lock cannot wedge it."""
    browser_home = str(sandbox / "browser-home")

    first = CrunchyrollClient._prepare_chrome_profile(browser_home)
    second = CrunchyrollClient._prepare_chrome_profile(browser_home)

    assert first != second
    assert os.path.isdir(first) and os.path.isdir(second)
    # Local temp space, not the mounted config volume.
    assert os.path.dirname(first) == str(sandbox)
    assert not first.startswith(browser_home)
    assert os.path.basename(first).startswith(_CHROME_PROFILE_PREFIX)


def test_legacy_config_volume_profile_is_purged(sandbox: Any) -> None:
    """The old fixed profile under the browser home is removed once."""
    browser_home = sandbox / "browser-home"
    legacy = browser_home / "chrome-profile"
    (legacy / "Default").mkdir(parents=True)
    (legacy / "SingletonLock").write_text("stale")

    CrunchyrollClient._prepare_chrome_profile(str(browser_home))

    assert not legacy.exists()


def test_singleton_locks_are_cleared(tmp_path: Any) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "SingletonCookie").write_text("x")
    (profile / "SingletonSocket").write_text("x")
    # Chrome's SingletonLock is a symlink that usually dangles.
    os.symlink("/nonexistent/pid-12345", profile / "SingletonLock")
    (profile / "Preferences").write_text("keep me")

    CrunchyrollClient._clear_singleton_locks(str(profile))

    assert not os.path.lexists(profile / "SingletonLock")
    assert not (profile / "SingletonSocket").exists()
    assert not (profile / "SingletonCookie").exists()
    assert (profile / "Preferences").exists()


def test_sweep_removes_only_old_owned_profiles(tmp_path: Any) -> None:
    stale = tmp_path / f"{_CHROME_PROFILE_PREFIX}old"
    recent = tmp_path / f"{_CHROME_PROFILE_PREFIX}new"
    foreign = tmp_path / "someone-elses-dir"
    for path in (stale, recent, foreign):
        path.mkdir()
    old_time = time.time() - _STALE_PROFILE_AGE_SECONDS - 60
    os.utime(stale, (old_time, old_time))
    os.utime(foreign, (old_time, old_time))

    CrunchyrollClient._sweep_stale_profiles(str(tmp_path))

    assert not stale.exists(), "a stale profile from a failed run should be swept"
    assert recent.exists(), "a concurrent run's profile must survive"
    assert foreign.exists(), "unrelated directories must not be touched"


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


def test_options_are_never_shared_between_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    install_fake_uc(monkeypatch, chrome=lambda **_: FakeDriver())
    client = make_client()

    first = client._build_chrome_options(str(tmp_path), "/usr/bin/chromium")
    second = client._build_chrome_options(str(tmp_path), "/usr/bin/chromium")

    assert first is not second
    assert first.arguments == second.arguments


def test_options_pin_the_profile_and_leave_the_debug_port_to_uc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    install_fake_uc(monkeypatch, chrome=lambda **_: FakeDriver())
    client = make_client()

    options = client._build_chrome_options(str(tmp_path), "/usr/bin/chromium")

    assert f"--user-data-dir={tmp_path}" in options.arguments
    assert options.binary_location == "/usr/bin/chromium"
    assert "--headless=new" in options.arguments
    assert "--no-sandbox" in options.arguments
    # A hardcoded port collides with the free port uc picks and then polls.
    assert not any(a.startswith("--remote-debugging-port") for a in options.arguments)
    # Chrome keeps only the last value of a repeated switch.
    disable_features = [
        a for a in options.arguments if a.startswith("--disable-features=")
    ]
    assert len(disable_features) == 1
    assert "TranslateUI" in disable_features[0]
    assert "VizDisplayCompositor" in disable_features[0]


# ---------------------------------------------------------------------------
# Chrome version detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("Chromium 146.0.7680.177 (bookworm)", 146),
        ("Google Chrome 120.0.6099.109", 120),
        ("not a version at all", None),
    ],
)
def test_detect_chrome_major_version(
    monkeypatch: pytest.MonkeyPatch, output: str, expected: int | None
) -> None:
    def fake_run(*_args: Any, **_kwargs: Any) -> Any:
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=output, stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert (
        CrunchyrollClient._detect_chrome_major_version("/usr/bin/chromium") == expected
    )


def test_detect_chrome_major_version_survives_a_missing_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("No such file or directory")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert CrunchyrollClient._detect_chrome_major_version("/nope/chromium") is None


# ---------------------------------------------------------------------------
# Driver setup / retry
# ---------------------------------------------------------------------------


def test_retry_uses_a_fresh_options_object(
    monkeypatch: pytest.MonkeyPatch, sandbox: Any
) -> None:
    """Regression: the fallback launch used to reuse the ChromeOptions object.

    undetected_chromedriver stamps ``options._session`` and rejects a second
    use, so the retry raised ``RuntimeError`` and buried the real launch error.
    """
    seen: list[FakeOptions] = []
    kwargs_seen: list[dict[str, Any]] = []

    def fake_chrome(options: FakeOptions = None, **kwargs: Any) -> FakeDriver:  # type: ignore[assignment]
        # Mirror uc's own guard verbatim.
        if getattr(options, "_session", None) is not None:
            raise RuntimeError("you cannot reuse the ChromeOptions object")
        options._session = object()
        seen.append(options)
        kwargs_seen.append(kwargs)
        if len(seen) == 1:
            raise Exception(
                "session not created: cannot connect to chrome at 127.0.0.1:50501"
            )
        return FakeDriver()

    install_fake_uc(monkeypatch, chrome=fake_chrome)
    monkeypatch.setattr(
        CrunchyrollClient, "_detect_chrome_major_version", staticmethod(lambda _b: 146)
    )
    client = make_client()

    client._setup_driver()

    assert len(seen) == 2, "the fallback attempt must actually run"
    assert seen[0] is not seen[1], "each attempt needs its own ChromeOptions"
    assert kwargs_seen[0]["version_main"] == 146
    assert "version_main" not in kwargs_seen[1]
    assert isinstance(client._driver, FakeDriver)
    assert client._driver.page_load_timeout == 60


def test_each_attempt_gets_its_own_profile(
    monkeypatch: pytest.MonkeyPatch, sandbox: Any
) -> None:
    profiles: list[str] = []

    def fake_chrome(options: FakeOptions = None, **_kwargs: Any) -> FakeDriver:  # type: ignore[assignment]
        profiles.append(
            next(
                a.split("=", 1)[1]
                for a in options.arguments
                if a.startswith("--user-data-dir=")
            )
        )
        if len(profiles) == 1:
            raise Exception("chrome not reachable")
        return FakeDriver()

    install_fake_uc(monkeypatch, chrome=fake_chrome)
    monkeypatch.setattr(
        CrunchyrollClient, "_detect_chrome_major_version", staticmethod(lambda _b: 146)
    )
    client = make_client()

    client._setup_driver()

    assert profiles[0] != profiles[1]
    assert not os.path.exists(profiles[0]), "the failed attempt's profile is cleaned up"
    assert client._profile_dir == profiles[1]


def test_total_failure_raises_and_leaves_nothing_behind(
    monkeypatch: pytest.MonkeyPatch, sandbox: Any
) -> None:
    real_error = Exception("session not created: ... from chrome not reachable")

    def fake_chrome(options: FakeOptions = None, **_kwargs: Any) -> FakeDriver:  # type: ignore[assignment]
        if getattr(options, "_session", None) is not None:
            raise RuntimeError("you cannot reuse the ChromeOptions object")
        options._session = object()
        raise real_error

    install_fake_uc(monkeypatch, chrome=fake_chrome)
    monkeypatch.setattr(
        CrunchyrollClient, "_detect_chrome_major_version", staticmethod(lambda _b: 146)
    )
    client = make_client()

    with pytest.raises(RuntimeError) as excinfo:
        client._setup_driver()

    # The genuine launch failure must be the chained cause, not swallowed.
    assert excinfo.value.__cause__ is real_error
    assert client._driver is None
    assert client._profile_dir == ""
    leftovers = [
        n for n in os.listdir(str(sandbox)) if n.startswith(_CHROME_PROFILE_PREFIX)
    ]
    assert leftovers == []


def test_post_launch_failure_does_not_leak_the_browser(
    monkeypatch: pytest.MonkeyPatch, sandbox: Any
) -> None:
    driver = FakeDriver()

    def boom(_script: str, *_args: Any) -> None:
        raise Exception("devtools went away")

    driver.execute_script = boom  # type: ignore[assignment]
    install_fake_uc(monkeypatch, chrome=lambda **_kwargs: driver)
    monkeypatch.setattr(
        CrunchyrollClient, "_detect_chrome_major_version", staticmethod(lambda _b: None)
    )
    client = make_client()

    with pytest.raises(Exception, match="devtools went away"):
        client._setup_driver()

    assert driver.quit_called, "a half-built driver must be quit, not orphaned"
    assert client._driver is None


@pytest.mark.asyncio
async def test_cleanup_removes_the_throwaway_profile(tmp_path: Any) -> None:
    client = make_client()
    driver = FakeDriver()
    client._driver = driver
    profile = tmp_path / "profile"
    profile.mkdir()
    client._profile_dir = str(profile)

    await client.cleanup()

    assert driver.quit_called
    assert client._driver is None
    assert not profile.exists()
    assert client._profile_dir == ""
