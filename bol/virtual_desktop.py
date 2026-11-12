"""bol.virtual_desktop — Minecraft inside Wine's virtual desktop.

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

The game draws through Vulkan into an X window of its own, inside the one
Wine made for the game's window. Wine places it at the client area when it
creates it, below the title bar Wine draws, and after that only resizes it:
the engine's winex11 keeps that window's position in client coordinates
(third_party/winegdk-native5/0005, for #78), which is (0, 0) for the game's
own window, so the move a frame change calls for is never sent. Switching to
fullscreen takes the title bar away, and the picture stayed where it was:
a band of the window's white background along the top, and the bottom of
the picture, the hotbar with it, below the screen (#202). Resizing the
window does not help, since Wine then sends the new size alone. The bridge
also reports where the client area starts inside the game's X window, and
the launcher moves the X windows in it there. Measured in a headless
gamescope at 1600x900: after F11 the game's window is 1606x906 at -3,-3,
with a 3-pixel frame, and its picture's X window moves from 4,30 to 3,3.
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
# How long a reported client area waits for the X window the game draws in:
# the game makes its swap chain after its window, behind its loading screen.
_PLACE_GRACE = 60.0


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


def _client_area(line):
    """(X window, x, y) from a bridge's "client" line, or None."""
    fields = line.split()
    if len(fields) != 4 or fields[0] != "client":
        return None
    try:
        window, x, y = int(fields[1], 0), int(fields[2]), int(fields[3])
    except ValueError:
        return None
    if window <= 0 or not (0 <= x < 4096 and 0 <= y < 4096):
        return None
    return window, x, y


def _place(placement, display):
    """Put the game's picture at its client area; True once that is done."""
    window, x, y = placement
    moved = x11.place_child_windows(window, x, y, display)
    if moved is None:
        return False
    if moved:
        _log(f"client area at {x},{y} in the game's window: moved the "
             f"picture there")
    return True


def _follow(proc, display):
    """Mirror the game's window onto the desktop's X windows."""
    lines = queue.Queue()
    threading.Thread(target=_pump, args=(proc.stdout, lines),
                     name="desktop-bridge-output", daemon=True).start()
    # None: nothing to undo. "asked": the window manager was asked to
    # minimize the desktop. "hidden": it did, and the desktop coming back
    # on screen means the player wants the game back.
    state, asked_at = None, 0.0
    # Where the game's picture belongs, until it is there.
    placement, reported_at = None, 0.0
    while _mc_running() and proc.poll() is None:
        try:
            line = lines.get(timeout=_POLL)
        except queue.Empty:
            line = ""
        if line is None:
            return
        reported = _client_area(line)
        if reported is not None:
            placement, reported_at = reported, time.monotonic()
        if placement is not None:
            if _place(placement, display):
                placement = None
            elif time.monotonic() - reported_at > _PLACE_GRACE:
                _log("no X window to move inside the game's window")
                placement = None
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
