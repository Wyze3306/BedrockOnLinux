"""bol.fullscreen_fix — a game started in fullscreen gets a frame its size (#283).

Started with Fullscreen on, Minecraft shows its window maximized, creates its
swap chain at that window's client size -- 848 rows on a 900-row screen once
the window manager has drawn its title bar -- and then turns the window into
a borderless fullscreen one. It records the monitor's size from the WM_SIZE
that switch sends but never resizes its buffers: vkd3d-proton logs every
ResizeBuffers ("Reallocating swapchain") and none follows. The 848-row picture
is stretched over 900 rows and every click lands where the unstretched one
put the button: the mouse sits above what it selects.

The game resizes for any later change of size, so once its menu is up
src/fullscreen-nudge.c sends its window one, a row shorter and then the real
size, through Wine itself rather than X: aimed at the game's window, never at
whatever has the keyboard. Measured: "Reallocating swapchain (1600 x 899)"
then "(1600 x 900)", and the picture is no longer stretched. A window that
does not cover its monitor is left alone, so a session started windowed, or
already resized, is not touched. BOL_FULLSCREEN_NUDGE=0 turns it off.

Sometimes the game reaches its menu still in its windowed mode, with its
framed window as large as the screen: the window manager's title bar and
border show along the top and left and the rest runs off the screen (#316).
The helper then asks the game to switch with F11, as a player would, and the
game's own switch resizes its buffers. Measured: a windowed game sized to
the screen went fullscreen, reallocating its swap chain at 1600 x 900.
"""
# SPDX-License-Identifier: MIT

import os
import subprocess
import threading
import time

from . import winehelper
from .config import LOGS
from .inject import _menu_reached
from .perfcheck import find_options_file, read_game_options
from .prefix import _mc_running, active_prefix
from .util import env_flag

EXE = "fullscreen-nudge.exe"

# The menu marker can be late on a cold start; past this the game is resized
# anyway, as the automatic DLL injection is.
_MENU_CEILING = 150.0
# What the launch starts first is xodus-cli and the Steam runtime; the game's
# own process follows them by several seconds.
_PROCESS_CEILING = 120.0
_POLL = 1.0


def starts_fullscreen(prefix=None):
    """Whether Minecraft's settings ask for fullscreen at start."""
    options = find_options_file(active_prefix() if prefix is None else prefix)
    return read_game_options(options).get("gfx_fullscreen") == "1"


def _log(message):
    LOGS.mkdir(parents=True, exist_ok=True)
    with open(LOGS / "fullscreen-nudge.log", "a") as log:
        log.write(f"{time.strftime('%F %T')} {message}\n")


def nudge():
    """Run the helper in the game's prefix; its exit code, or None."""
    prepared = winehelper.command(EXE)
    if prepared is None:
        return None
    argv, env = prepared
    try:
        result = subprocess.run(argv, env=env, capture_output=True, text=True,
                                errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        _log(f"could not run the helper: {exc}")
        return None
    _log(f"exit {result.returncode}: "
         f"{(result.stdout + result.stderr).strip()}")
    return result.returncode


def _watch(since):
    """Wait for the menu, then resize the game once; never raises."""
    try:
        started = time.monotonic()
        while not _mc_running():
            if time.monotonic() - started >= _PROCESS_CEILING:
                return
            time.sleep(_POLL)
        started = time.monotonic()
        while time.monotonic() - started < _MENU_CEILING:
            if not _mc_running():
                return
            if _menu_reached(since):
                break
            time.sleep(_POLL)
        if _mc_running():
            nudge()
    except Exception as exc:                              # noqa: BLE001
        _log(f"failed: {type(exc).__name__}: {exc}")


def start(environ=None, prefix=None):
    """Watch this launch for a fullscreen start; the thread, or None."""
    source = os.environ if environ is None else environ
    value = str(source.get("BOL_FULLSCREEN_NUDGE", "")).strip()
    if value and not env_flag(value):
        return None
    if not starts_fullscreen(prefix):
        return None
    watcher = threading.Thread(target=_watch, args=(time.time(),),
                               name="fullscreen-nudge", daemon=True)
    watcher.start()
    return watcher
