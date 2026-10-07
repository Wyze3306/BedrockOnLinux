"""bol.virtual_desktop — minimizing Minecraft inside Wine's virtual desktop.

"Keep the mouse inside the window" (``confine_cursor``, or
BOL_CONFINE_CURSOR=1) runs the game in a Wine virtual desktop, where cursor
clipping is reliable because Wine owns the one X window (#26). That desktop
is as large as the screen and fullscreen, so it has no title bar of the
window manager's; the game's title bar is one Wine draws inside it. The
minimize button there minimizes the game *inside* the desktop: the game
shrinks to a small bar in the desktop's corner, and the desktop stays where
it was, over everything, still showing the game's last frame (#189).

src/desktop-bridge.c reports from inside Wine when the game is minimized.
The launcher then minimizes the desktop's X window, as the window manager's
own button would, and once that window is back on screen -- from the taskbar,
like any other -- restores the game in it. Measured on Xvfb with openbox:
the screen is the desktop's again while the game is minimized, and the game
comes back maximized and drawing.
"""
# SPDX-License-Identifier: MIT

import os
import queue
import subprocess
import threading
import time

from . import winehelper, x11
from .config import LOGS
from .prefix import _mc_running
from .util import env_flag, load_settings

EXE = "desktop-bridge.exe"
# The class Wine gives the virtual desktop's X window ("Wine Desktop").
DESKTOP_CLASS = "explorer.exe"

# The launch starts xodus-cli and the Steam runtime first; the game's own
# process follows them by several seconds.
_PROCESS_CEILING = 120.0
_POLL = 0.5
# How long a window manager may take to act on a minimize request before the
# launcher stops waiting for the desktop to leave the screen.
_ICONIFY_GRACE = 5.0


def requested(settings=None, environ=None):
    """Whether this launch runs the game in a Wine virtual desktop."""
    source = os.environ if environ is None else environ
    if env_flag(source.get("BOL_CONFINE_CURSOR")):
        return True
    settings = load_settings() if settings is None else settings
    return bool(settings.get("confine_cursor", False))


def _log(message):
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        with open(LOGS / "virtual-desktop.log", "a") as log:
            log.write(f"{time.strftime('%F %T')} {message}\n")
    except OSError:
        pass


def _pump(stream, lines):
    for line in stream:
        lines.put(line.strip())
    lines.put(None)


def _follow(proc, display):
    """Mirror the game's minimized state onto the desktop's X window."""
    lines = queue.Queue()
    threading.Thread(target=_pump, args=(proc.stdout, lines),
                     name="desktop-bridge-output", daemon=True).start()
    # None: nothing to undo. "asked": the window manager was asked to
    # minimize the desktop. "hidden": it did, and the desktop coming back
    # on screen means the player wants the game back.
    state, asked_at = None, 0.0
    while _mc_running() and proc.poll() is None:
        try:
            line = lines.get(timeout=_POLL)
        except queue.Empty:
            line = ""
        if line is None:
            return
        if line == "iconic":
            if x11.iconify_windows(DESKTOP_CLASS, display):
                state, asked_at = "asked", time.monotonic()
                _log("game minimized: minimizing the virtual desktop")
            else:
                _log("game minimized, but no virtual desktop window to "
                     "minimize")
            continue
        if line == "normal":
            state = None
            continue
        if state is None:
            continue
        shown = x11.find_presentable_window(DESKTOP_CLASS, display)
        if state == "asked":
            if not shown:
                state = "hidden"
            elif time.monotonic() - asked_at > _ICONIFY_GRACE:
                _log("the window manager did not minimize the desktop")
                state = None
        elif shown:
            _log("virtual desktop back on screen: restoring the game")
            proc.stdin.write("restore\n")
            proc.stdin.flush()
            state = None


def _watch(display):
    """Run the bridge for this session; never raises."""
    proc = None
    try:
        started = time.monotonic()
        while not _mc_running():
            if time.monotonic() - started >= _PROCESS_CEILING:
                return
            time.sleep(_POLL)
        prepared = winehelper.command(EXE)
        if prepared is None:
            return
        argv, env = prepared
        proc = subprocess.Popen(argv, env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True,
                                errors="replace")
        _follow(proc, display)
    except Exception as exc:                              # noqa: BLE001
        _log(f"failed: {type(exc).__name__}: {exc}")
    finally:
        if proc is not None:
            # Closing its input is what ends the bridge.
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()


def start(settings, environ=None, display=None):
    """Bridge this launch's virtual desktop, if it has one; the thread."""
    if not display or not requested(settings, environ):
        return None
    watcher = threading.Thread(target=_watch, args=(display,),
                               name="desktop-bridge", daemon=True)
    watcher.start()
    return watcher
