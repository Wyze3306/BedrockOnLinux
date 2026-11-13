"""bol.rollback — put the previous launcher back when the new one cannot start.

An AppImage or a .pyz updates by replacing its own file. bol.update checks
the new file's SHA-256 and runs it once before the swap, but a launcher can
pass both and still not come up on this machine: a Qt it cannot load, a crash
before the window. Nothing could be done about that from inside a launcher
that does not start, so the swap leaves two things behind:

* the previous file, beside the new one (``.<name>.previous``);
* a marker in the data directory naming both versions.

guard() runs first thing on every start. The new version counts its starts in
the marker; confirm() removes the marker once the launcher is really up (the
window has been on screen for a moment, or a command was understood). A start
that finds MAX_UNCONFIRMED starts already counted, none of them confirmed,
puts the previous file back and continues as that one.

Standard library only, and no import from the rest of the package beyond
bol.config: this has to work when everything else is what broke.
"""
# SPDX-License-Identifier: MIT

import json
import os
import sys
import time
from pathlib import Path

from .config import DATA, VERSION

MAX_UNCONFIRMED = 2
PROBE_ENV = "BOL_UPDATE_PROBE"


def marker_path():
    return DATA / "update-pending.json"


def note_path():
    return DATA / "update-rolled-back.json"


def backup_path(target):
    target = Path(target)
    return target.with_name(f".{target.name}.previous")


def running_file():
    """The file an update replaces: the AppImage, else the script run."""
    path = os.environ.get("APPIMAGE") or (sys.argv[0] if sys.argv else "")
    try:
        return os.path.realpath(path) if path else ""
    except OSError:
        return ""


def _read(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write(path, value):
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        staged = path.with_name(path.name + ".tmp")
        staged.write_text(json.dumps(value), encoding="utf-8")
        os.replace(staged, path)
    except OSError:
        pass


def _remove(path):
    try:
        Path(path).unlink()
    except OSError:
        pass


def arm(target, old_version, new_version):
    """Record that `target` was just replaced by `new_version`."""
    _remove(note_path())
    _write(marker_path(), {
        "target": os.path.realpath(str(target)),
        "backup": str(backup_path(os.path.realpath(str(target)))),
        "from": old_version, "to": new_version, "starts": 0,
        "time": int(time.time())})


def _mine(marker):
    return bool(marker) and marker.get("target") == running_file()


def confirm():
    """The running launcher is up: the update it came from is kept."""
    try:
        marker = _read(marker_path())
        if _mine(marker) and marker.get("to") == VERSION:
            _remove(marker_path())
    except Exception:
        pass


def last_rollback():
    """{'from': ..., 'to': ...} once after an update was undone, else None."""
    note = _read(note_path())
    if note is not None:
        _remove(note_path())
    return note


def _restore(marker):
    target, backup = marker["target"], marker.get("backup") or ""
    if not os.path.isfile(backup):
        return False
    os.replace(backup, target)
    return True


def guard(execv=os.execv):
    """Undo an update whose launcher never managed to start. Never raises."""
    # Neither the check an update runs before a swap nor a plain --version
    # says anything about whether the launcher comes up.
    if os.environ.get(PROBE_ENV) or "--version" in sys.argv[1:]:
        return
    try:
        marker = _read(marker_path())
        if not _mine(marker):
            return
        if marker.get("to") != VERSION:
            # Something else sits at the target now (the user put a file
            # there, or a roll-back already happened): nothing to watch.
            _remove(marker_path())
            return
        starts = marker.get("starts")
        starts = starts if isinstance(starts, int) else 0
        if starts < MAX_UNCONFIRMED:
            marker["starts"] = starts + 1
            _write(marker_path(), marker)
            return
        _remove(marker_path())
        if not _restore(marker):
            return
        _write(note_path(), {"from": marker.get("to"),
                             "to": marker.get("from"),
                             "time": int(time.time())})
        target = marker["target"]
        argv = [target] + sys.argv[1:]
        if os.environ.get("APPIMAGE"):
            execv(target, argv)
        else:
            execv(sys.executable, [sys.executable] + argv)
    except Exception:
        return
