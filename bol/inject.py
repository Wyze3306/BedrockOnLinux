"""Inject client DLLs through the game's Wine prefix.

Flatpak is unsupported because its sandbox isolates the game's wineserver.
"""
# SPDX-License-Identifier: MIT

import hashlib
import os
import pkgutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .config import CACHE, LOGS
from .log import BolError, desktop_notify
from .prefix import _mc_running, active_prefix, prefix_processes
from .proton import proton_path


def _cmdline_has_winedbg(cmdline):
    """Match Wine's debugger as an argv entry, not a parent-folder substring."""
    for argument in cmdline.lower().split(b"\0"):
        normalized = argument.replace(b"\\", b"/").rstrip(b"/")
        basename = normalized.rsplit(b"/", 1)[-1]
        if basename in (b"winedbg", b"winedbg.exe"):
            return True
    return False


def _wine_debugger_running():
    """Return whether this prefix has entered Wine's crash debugger."""
    for pid in prefix_processes(active_prefix()):
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            continue
        if _cmdline_has_winedbg(cmdline):
            return True
    return False


def _post_injection_failure(timeout=3.0, interval=0.1):
    """Observe asynchronous DLL startup long enough to catch an immediate crash.

    LoadLibrary returning successfully only proves that Windows mapped the DLL.
    Client initialization commonly continues on another thread, so a bad
    game/client version can still enter winedbg just after the injector exits.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if _wine_debugger_running():
            return (
                "Minecraft entered Wine's crash debugger immediately after "
                "the DLL was loaded (winedbg)"
            )
        if not _mc_running():
            return "Minecraft exited immediately after the DLL was loaded"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(max(0.01, interval), remaining))


def _extract_injector():
    """Materialize the bundled injector.exe for Wine."""
    blob = pkgutil.get_data("bol", "injector.exe")
    if not blob:
        raise BolError("Bundled injector.exe is missing from this install.")
    CACHE.mkdir(parents=True, exist_ok=True)
    inj = CACHE / "injector.exe"
    try:
        current = inj.read_bytes() if inj.is_file() else None
    except OSError:
        current = None
    if current != blob:
        fd, name = tempfile.mkstemp(
            prefix=".injector-", suffix=".tmp", dir=CACHE
        )
        staged = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(blob)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staged, inj)
        finally:
            staged.unlink(missing_ok=True)
    return inj


def run_injector(dll_path):
    """Inject the given client .dll into the running Minecraft and wait for the
    outcome. The game must already be running (main menu reached). Returns the
    .dll's file name on success; raises BolError with a user-facing reason."""
    dll = Path(dll_path).expanduser()
    if not dll.is_file():
        raise BolError(f"DLL not found: {dll}")
    if dll.suffix.lower() != ".dll":
        raise BolError(f"Not a .dll: {dll.name}")
    if not _mc_running():
        raise BolError("Start Minecraft first and wait for the main menu, then "
                       "inject.")
    if _wine_debugger_running():
        raise BolError(
            "Minecraft is already stopped in Wine's crash debugger. Close the "
            "debugger, restart Minecraft, and do not inject the same DLL again."
        )
    pp = proton_path()
    if not pp:
        raise BolError("GDK-Proton engine missing — run Install / Update first.")
    wine = pp / "files/bin/wine"
    if not wine.exists():
        raise BolError(f"Engine wine binary not found ({wine}).")
    inj = _extract_injector()
    # The DLL path as Wine sees it: a host /a/b.dll maps to Z:\a\b.dll.
    wpath = "Z:" + str(dll.resolve()).replace("/", "\\")
    env = dict(os.environ)
    env.update({"WINEESYNC": "1", "WINEFSYNC": "1",
                "WINEPREFIX": str(active_prefix()),
                "WINEDEBUG": os.environ.get("WINEDEBUG", "-all")})
    LOGS.mkdir(parents=True, exist_ok=True)
    with open(LOGS / "injector.log", "a") as log:
        log.write(f"\n--- inject {dll} ---\n")
        log.flush()
        try:
            r = subprocess.run(
                [str(wine), str(inj), wpath, "Minecraft.Windows.exe"],
                env=env, stdout=log, stderr=subprocess.STDOUT, timeout=60)
        except subprocess.TimeoutExpired:
            raise BolError("Injection timed out — is the game still running?")
    if r.returncode == 8:
        raise BolError(
            "Injection timed out while the DLL was being loaded. Minecraft "
            "was left running and the remote path buffer was retained safely; "
            f"details are in {LOGS / 'injector.log'}."
        )
    if r.returncode == 7:
        raise BolError(
            "Injection failed because LoadLibrary could not load the DLL "
            "(code 7). Check that it is 64-bit and its dependencies are "
            f"present — details in {LOGS / 'injector.log'}."
        )
    if r.returncode != 0:
        raise BolError(
            f"Injection infrastructure failed (code {r.returncode}) while "
            "accessing Minecraft or starting the remote loader. The game may "
            f"have exited; details are in {LOGS / 'injector.log'}."
        )
    post_failure = _post_injection_failure()
    if post_failure:
        with open(LOGS / "injector.log", "a") as log:
            log.write(f"ERR post-injection: {post_failure}\n")
        raise BolError(
            f"{dll.name} was loaded, but {post_failure}. Use a client release "
            "made for the exact Minecraft version shown in the launcher; a "
            "client's GitHub 'latest' release may target an older game line. "
            f"Details: {LOGS / 'injector.log'}."
        )
    return dll.name


# The game's X window exists long before its main menu does, so a window
# sighting is a floor for the wait and never a shortcut through it: what the
# player configured is what decides when the DLL goes in.
_AUTO_INJECT_WINDOW_SETTLE = 1.5
# A Wayland-backend session gives the game no X window at all, so the wait
# for one is capped rather than open-ended.
_AUTO_INJECT_WINDOW_CEILING = 30.0
_AUTO_INJECT_PROCESS_CEILING = 30.0
_AUTO_INJECT_POLL = 0.25

# The window is up long before the game is: it opens on the loading screen,
# and the main menu follows 13 s later on a warm start and over a minute on a
# cold one. Injected at the window, a client lands mid-startup, which a manual
# injection -- done from the menu -- never does, and that is where Flarial's
# automatic injection lost the mouse while its manual one did not (#281). The
# game says when its menu is up by writing the feature flags it fetches right
# after drawing it: measured on 1.26.52 at 0 to 8 s after the menu appeared,
# never before. A session that never writes them is injected anyway, this
# long after its window.
_MENU_MARKERS = ("Flighting/currentTreatments",
                 "bootstrapStorage/bootstrap_settings.json")
_GAME_DATA_DIRS = ("Minecraft Bedrock", "Minecraft Bedrock Preview")
_AUTO_INJECT_MENU_CEILING = 150.0


def _auto_inject_log(message):
    LOGS.mkdir(parents=True, exist_ok=True)
    with open(LOGS / "injector.log", "a") as log:
        log.write(f"{message}\n")


def _auto_inject_failed(message, detail=None):
    _auto_inject_log(f"Auto-inject failed: {detail or message}")
    desktop_notify(message, "DLL Injector")


def auto_inject_download_path(url):
    """Where a downloaded client DLL is cached, named after its own URL.

    ``download`` resumes an interrupted transfer from the partial file beside
    its destination, so one name shared by every URL splices the tail of one
    DLL onto the head of another — a chimera that is the right length, passes
    the transfer's own integrity check, and is then handed to LoadLibrary.
    Hashing the URL keeps each one to itself and leaves resumption working
    for the URL that started it.
    """
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return CACHE / f"auto-inject-{digest}.dll"


def _auto_inject_dll(settings):
    """Resolve the DLL to inject, or None once the failure has been reported."""
    if settings.get("injector_dll_type", "file") == "url":
        url = (settings.get("injector_dll_url") or "").strip()
        if not url:
            _auto_inject_failed("Auto-inject failed: DLL URL is empty.")
            return None
        try:
            from .util import download
            dest = auto_inject_download_path(url)
            download(url, dest, label="Auto-inject DLL")
            return dest
        except Exception as error:
            _auto_inject_failed(f"Auto-inject download failed:\n{error}",
                                detail=f"download: {error}")
            return None
    path = ((settings.get("injector_dll_path") or "").strip()
            or (settings.get("injector_dll") or "").strip())
    if not path:
        _auto_inject_failed("Auto-inject failed: Local DLL path is empty.")
        return None
    return Path(path)


def _await_game_process():
    """Wait for the game to appear; False once its absence has been reported."""
    deadline = time.monotonic() + _AUTO_INJECT_PROCESS_CEILING
    while time.monotonic() < deadline:
        if _mc_running():
            return True
        time.sleep(0.2)
    _auto_inject_failed(
        "Auto-inject failed: Minecraft process did not start.",
        detail=(f"Minecraft process did not start within "
                f"{_AUTO_INJECT_PROCESS_CEILING:.0f}s."))
    return False


def _game_window_present():
    try:
        from .x11 import find_presentable_window
        return bool(find_presentable_window("minecraft.windows.exe"))
    except Exception:
        return False


def _menu_reached(since, prefix=None):
    """Whether the game wrote what it writes once its main menu is up."""
    try:
        users = Path(prefix or active_prefix()) / "drive_c" / "users"
        homes = [home for home in users.iterdir() if home.is_dir()]
    except OSError:
        return False
    for home in homes:
        for data in _GAME_DATA_DIRS:
            for marker in _MENU_MARKERS:
                try:
                    path = home / "AppData" / "Roaming" / data / marker
                    if path.stat().st_mtime >= since:
                        return True
                except OSError:
                    continue
    return False


def _await_injection_moment(delay, since=None):
    """Hold until the game is ready to be injected into; False if it exited.

    Three conditions have to hold. The configured delay has to have run out —
    it is the only control the player has over a client that loads too early,
    so nothing detected ever cuts it short. The game has to have opened a
    window, because a DLL loaded into a game that has not drawn anything yet
    lands mid-startup; that wait is capped, since a Wayland session has no X
    window to find. And the game has to have reached its main menu, where a
    player injects by hand; that wait is capped too, after the window, for a
    session that never says so.
    """
    since = time.time() if since is None else since
    started = time.monotonic()
    window_seen_at = None
    while True:
        now = time.monotonic()
        if not _mc_running():
            return False
        if window_seen_at is None and _game_window_present():
            window_seen_at = now
        waited = now - started >= delay
        menu = _menu_reached(since)
        if window_seen_at is not None:
            settled = now - window_seen_at >= _AUTO_INJECT_WINDOW_SETTLE
            if waited and settled and menu:
                return True
            if waited and now - window_seen_at >= _AUTO_INJECT_MENU_CEILING:
                _auto_inject_log("The game never said its menu was up; "
                                 "injecting anyway.")
                return True
        elif waited and now - started >= _AUTO_INJECT_WINDOW_CEILING and (
                menu or now - started >= _AUTO_INJECT_MENU_CEILING):
            return True
        time.sleep(_AUTO_INJECT_POLL)


def perform_auto_inject(settings):
    """Inject the configured DLL once the game it was launched for is ready."""
    settings = settings or {}
    dll_path = _auto_inject_dll(settings)
    if dll_path is None:
        return
    # Everything the launcher writes is written before the game starts, so
    # a marker newer than this is the game's.
    since = time.time()
    if not _await_game_process():
        return
    if not _await_injection_moment(float(settings.get("injector_delay", 5)),
                                   since):
        return
    try:
        name = run_injector(dll_path)
    except Exception as error:
        _auto_inject_failed(f"Could not inject:\n{error}", detail=str(error))
        return
    desktop_notify(f"Injected {name} into Minecraft. ✓", "DLL Injector")


def start_auto_inject(settings):
    """Start this launch's auto-injection watcher, if the player asked for one.

    Returns the thread it started, or None when the setting is off, so every
    launch path can call this the same way.
    """
    if not (settings or {}).get("injector_auto_enable", False):
        return None
    watcher = threading.Thread(target=perform_auto_inject, args=(settings,),
                               name="auto-inject", daemon=True)
    watcher.start()
    return watcher
