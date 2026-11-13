"""bol.instances — the games that are running, one record each.

Every profile has its own Wine prefix, account and worlds, so nothing keeps
two of them from playing at once, each in its own launcher process. What was
missing is a place that says which games are up: a launcher window only knew
about the one it started, and a game started from a shortcut, from a terminal
or by a window that has since been closed was known to nobody.

A session writes one small JSON file here when its game process exists and
removes it when the game has returned. The directory sits beside the shared
game builds, so every profile reads the same list. A record is only believed
while the launcher or the game wrapper it names is still the same process:
a crash, a kill or a reboot leaves a file that the next reader drops.

A record also carries whether the game is meant to be on screen. A hidden
game keeps running and drawing with its window withdrawn from the desktop,
which is what a tool driving it through the game's own back buffer needs
(docs/INSTANCES.md). The session that owns the game enforces that for as
long as it lasts, because Wine builds a new X window whenever the game
changes what kind of window it wants.
"""
# SPDX-License-Identifier: MIT

import json
import os
import secrets
import stat
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Optional

from . import x11
from .log import BolError

SCHEMA = 1
DIRECTORY = "instances"

# How a game reaches the screen, which decides whether it can be hidden.
BACKEND_X11 = "x11"
BACKEND_WAYLAND = "wayland"
BACKEND_GAMESCOPE = "gamescope"

# How long a polite close waits for the game before its processes are
# stopped. Minecraft saves the open world on WM_CLOSE and is usually gone in
# two or three seconds.
CLOSE_GRACE_S = 15.0

# How often the owning session re-applies "hidden" to windows the game has
# built since: quickly while windows are still appearing, so one is on screen
# for as short a time as possible, then at a pace that costs nothing.
KEEPER_INTERVAL_S = 0.4
KEEPER_IDLE_INTERVAL_S = 2.0
KEEPER_QUIET_TICKS = 75


def instances_root(base=None) -> Path:
    """The directory every profile's sessions record themselves in."""
    if base is not None:
        return Path(base)
    from .config import GAMES
    return GAMES.resolve(strict=False).parent / DIRECTORY


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def _process_start(pid) -> Optional[int]:
    """When ``pid`` started, in clock ticks after boot, or None if unknown."""
    try:
        line = Path(f"/proc/{pid}/stat").read_text(errors="replace")
    except OSError:
        return None
    fields_ = line.rpartition(")")[2].split()
    try:
        return int(fields_[19])
    except (IndexError, ValueError):
        return None


def _same_process(pid, started) -> bool:
    """Whether ``pid`` is still the process that started at ``started``."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return False
    current = _process_start(pid)
    if current is None:
        return False
    return started is None or current == started


@dataclass
class Instance:
    """One running game, as its session recorded it."""

    id: str
    profile: str = "Default"
    slug: str = "default"
    home: str = ""
    prefix: str = ""
    title: str = "Minecraft"
    edition: str = ""
    version: str = ""
    game_dir: str = ""
    launcher_pid: int = 0
    launcher_start: Optional[int] = None
    game_pid: int = 0
    game_start: Optional[int] = None
    boot_id: str = ""
    started: float = 0.0
    hidden: bool = False
    backend: str = BACKEND_X11
    display: str = ""
    wm_class: str = ""
    schema: int = SCHEMA

    @property
    def can_hide(self) -> bool:
        """Whether this game's window can be taken off the screen."""
        return self.backend == BACKEND_X11 and bool(self.display) \
            and bool(self.wm_class)

    @property
    def launcher_alive(self) -> bool:
        return _same_process(self.launcher_pid, self.launcher_start)

    @property
    def game_alive(self) -> bool:
        return _same_process(self.game_pid, self.game_start)

    @property
    def alive(self) -> bool:
        boot = _boot_id()
        if boot and self.boot_id and self.boot_id != boot:
            return False
        return self.launcher_alive or self.game_alive

    def uptime(self, now=None) -> float:
        return max(0.0, (time.time() if now is None else now) - self.started)

    @property
    def pid(self) -> int:
        """The process to show for this game: its wrapper, else its
        launcher."""
        return self.game_pid if self.game_alive else self.launcher_pid


_FIELDS = {f.name: f for f in fields(Instance)}
_TYPES = {
    "id": str, "profile": str, "slug": str, "home": str, "prefix": str,
    "title": str, "edition": str, "version": str, "game_dir": str,
    "launcher_pid": int, "game_pid": int, "boot_id": str, "hidden": bool,
    "backend": str, "display": str, "wm_class": str, "schema": int,
}


def _record_path(root, instance_id) -> Path:
    name = str(instance_id)
    if not name or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-"
                       for c in name):
        raise BolError(f"'{instance_id}' is not an instance identifier.")
    return Path(root) / f"{name}.json"


def _read(path: Path) -> Optional[Instance]:
    """One record, or None for anything that is not a well-formed one."""
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return None
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    if len(raw) > 65_536:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return None
    known = {}
    for name, value in data.items():
        if name not in _FIELDS:
            continue
        wanted = _TYPES.get(name)
        if wanted is not None and (not isinstance(value, wanted)
                                   or (wanted is int
                                       and isinstance(value, bool))):
            return None
        known[name] = value
    if known.get("id") != path.name[:-len(".json")]:
        return None
    for name in ("launcher_start", "game_start"):
        value = known.get(name)
        if value is not None and (isinstance(value, bool)
                                  or not isinstance(value, int)):
            return None
    started = known.get("started", 0.0)
    if isinstance(started, bool) or not isinstance(started, (int, float)):
        return None
    known["started"] = float(started)
    try:
        return Instance(**known)
    except TypeError:
        return None


def _write(root: Path, instance: Instance) -> Path:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = _record_path(root, instance.id)
    fd, name = tempfile.mkstemp(prefix=".record-", suffix=".tmp", dir=root)
    staged = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(asdict(instance), stream, sort_keys=True, indent=1)
            stream.write("\n")
        os.chmod(staged, 0o600)
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)
    return path


def register(*, profile, slug, home, prefix, game_pid, title="Minecraft",
             edition="", version="", game_dir="", hidden=False,
             backend=BACKEND_X11, display="", wm_class="", root=None,
             launcher_pid=None, clock=time.time) -> Instance:
    """Record the game this process has just started."""
    launcher = os.getpid() if launcher_pid is None else int(launcher_pid)
    instance = Instance(
        id=f"{slug or 'default'}-{secrets.token_hex(3)}",
        profile=str(profile or "Default"), slug=str(slug or "default"),
        home=str(home), prefix=str(prefix), title=str(title or "Minecraft"),
        edition=str(edition or ""), version=str(version or ""),
        game_dir=str(game_dir or ""),
        launcher_pid=launcher, launcher_start=_process_start(launcher),
        game_pid=int(game_pid), game_start=_process_start(int(game_pid)),
        boot_id=_boot_id(), started=float(clock()), hidden=bool(hidden),
        backend=str(backend), display=str(display or ""),
        wm_class=str(wm_class or "").lower())
    _write(instances_root(root), instance)
    return instance


def get(instance_id, root=None) -> Optional[Instance]:
    """The live record ``instance_id`` names, or None."""
    try:
        instance = _read(_record_path(instances_root(root), instance_id))
    except BolError:
        return None
    return instance if instance is not None and instance.alive else None


def update(instance_id, root=None, **changes) -> Optional[Instance]:
    """Change fields of a record that is still there; the new record."""
    base = instances_root(root)
    try:
        instance = _read(_record_path(base, instance_id))
    except BolError:
        return None
    if instance is None:
        return None
    for name, value in changes.items():
        if name not in _FIELDS or name in ("id", "schema"):
            raise BolError(f"An instance has no '{name}' to change.")
        setattr(instance, name, value)
    try:
        _write(base, instance)
    except OSError:
        return None
    return instance


def unregister(instance_id, root=None) -> bool:
    try:
        _record_path(instances_root(root), instance_id).unlink()
    except (OSError, BolError):
        return False
    return True


def list_instances(root=None, prune=True) -> list:
    """Every game that is running, oldest first.

    A record whose launcher and game wrapper are both gone is dropped, and
    with ``prune`` its file goes too. One that cannot be read is left alone:
    it may be a newer launcher's.
    """
    base = instances_root(root)
    try:
        paths = sorted(base.glob("*.json"))
    except OSError:
        return []
    found = []
    for path in paths:
        instance = _read(path)
        if instance is None:
            continue
        if not instance.alive:
            if prune:
                try:
                    path.unlink()
                except OSError:
                    pass
            continue
        found.append(instance)
    found.sort(key=lambda item: (item.started, item.id))
    return found


def find(reference, root=None) -> Instance:
    """The one running game ``reference`` names.

    An identifier, the start of one, a profile name or a profile's folder
    name. More than one match is an error rather than a guess.
    """
    wanted = str(reference or "").strip()
    running = list_instances(root)
    if not wanted:
        raise BolError("Name the instance: its ID or its profile.")
    exact = [item for item in running if item.id == wanted]
    if exact:
        return exact[0]
    lowered = wanted.lower()
    matches = [item for item in running
               if item.id.startswith(lowered)
               or item.profile.lower() == lowered
               or item.slug == lowered]
    if not matches:
        raise BolError(f"No running instance matches '{wanted}'.")
    if len(matches) > 1:
        raise BolError(
            f"'{wanted}' matches {len(matches)} running instances: "
            + ", ".join(item.id for item in matches) + ".")
    return matches[0]


def _same_path(one, other) -> bool:
    try:
        return Path(one).resolve() == Path(other).resolve()
    except (OSError, RuntimeError):
        return False


def for_home(home, root=None) -> list:
    """The running games of the profile whose data lives at ``home``."""
    return [item for item in list_instances(root)
            if item.home and _same_path(item.home, home)]


def game_dir_in_use(game_dir, root=None, exclude_home=None) -> bool:
    """Whether another profile's game is running from this build's folder."""
    if not game_dir:
        return False
    for item in list_instances(root):
        if exclude_home is not None and item.home \
                and _same_path(item.home, exclude_home):
            continue
        if item.game_dir and _same_path(item.game_dir, game_dir):
            return True
    return False


# --- windows ---------------------------------------------------------------

def _prefix_pids(instance):
    from .prefix import prefix_processes
    if not instance.prefix:
        return []
    return prefix_processes(Path(instance.prefix))


def _window_pids(instance, root=None, scan=None):
    """The PIDs whose windows are this game's, or None for "all of them".

    Wine stamps each window with the PID of the process that owns it, and
    the processes of one game are the ones running in its prefix. When no
    such process can be seen from here (a sandbox with a PID namespace of
    its own) and this is the only game of its kind on the display, the
    window class alone names it.
    """
    pids = (scan or _prefix_pids)(instance)
    if pids:
        return set(pids)
    others = [item for item in list_instances(root, prune=False)
              if item.id != instance.id
              and item.wm_class == instance.wm_class
              and item.display == instance.display]
    return None if not others else set()


def _require_hideable(instance):
    if instance.can_hide:
        return
    if instance.backend == BACKEND_GAMESCOPE:
        raise BolError(
            "This instance runs inside its own gamescope, whose window "
            "cannot be hidden from here.")
    if instance.backend == BACKEND_WAYLAND:
        raise BolError(
            "This instance draws through Wine's Wayland driver, and a "
            "Wayland desktop lets no other program hide a window. Start it "
            "with the X11 input backend (the default) to hide it.")
    raise BolError("This instance has no window on an X display to hide.")


def apply_hidden(instance, root=None, windows=None, scan=None) -> int:
    """Withdraw this game's on-screen windows; how many were."""
    if not instance.can_hide:
        return 0
    return x11.hide_instance_windows(
        instance.wm_class, _window_pids(instance, root, scan),
        display=instance.display, windows=windows)


def set_hidden(instance, hidden, root=None, windows=None, scan=None) -> int:
    """Hide or show a running game; how many windows changed.

    The record is changed first: the session that owns the game reads it and
    keeps later windows hidden too.
    """
    _require_hideable(instance)
    hidden = bool(hidden)
    if update(instance.id, root, hidden=hidden) is None:
        raise BolError(f"Instance {instance.id} is no longer running.")
    instance.hidden = hidden
    pids = _window_pids(instance, root, scan)
    if hidden:
        return x11.hide_instance_windows(
            instance.wm_class, pids, display=instance.display,
            windows=windows)
    return x11.show_instance_windows(
        instance.wm_class, pids, display=instance.display, windows=windows)


def _game_running(instance, scan, image) -> bool:
    """Whether the game's own executable still runs in its prefix."""
    wanted = instance.wm_class or "minecraft.windows.exe"
    return any(image(pid) == wanted for pid in scan(instance))


def close_instance(instance, root=None, grace=CLOSE_GRACE_S, force=False,
                   windows=None, scan=None, image=None, stop=None,
                   sleep=time.sleep, clock=time.monotonic) -> str:
    """Close one running game.

    Its window is asked to close first, as the window's own close button
    would, so the game saves the open world. A game still there after
    ``grace`` seconds has its processes stopped, and so has one with no
    window to ask; ``force`` skips the asking. Returns "closed" when the
    game went by itself and "stopped" otherwise.
    """
    scan = scan or _prefix_pids
    if image is None:
        from .leftovers import _image as image
    if stop is None:
        from .prefix import stop_prefix_procs

        def stop(target):
            if target.prefix:
                stop_prefix_procs(Path(target.prefix))

    asked = 0
    if not force and instance.backend == BACKEND_X11 and instance.display \
            and instance.wm_class:
        asked = x11.close_instance_windows(
            instance.wm_class, _window_pids(instance, root, scan),
            display=instance.display, windows=windows)
    closed = False
    if asked:
        deadline = clock() + max(0.0, grace)
        while True:
            if not _game_running(instance, scan, image):
                closed = True
                break
            if clock() >= deadline:
                break
            sleep(0.25)
    if closed and instance.launcher_alive:
        # Its own session sees the game return and finishes the teardown:
        # the updater the game leaves behind, the record, the GPU marker.
        return "closed"
    # Nobody is left to do that for a game whose launcher is gone, and a
    # game that would not close is stopped like any other.
    stop(instance)
    if not instance.launcher_alive:
        unregister(instance.id, root)
    return "closed" if closed else "stopped"


class WindowKeeper(threading.Thread):
    """Keeps a hidden game hidden, for the session that owns it.

    Wine replaces the game's X window when it goes fullscreen or back, and a
    replacement starts out on screen. This reads the record, so hiding or
    showing the game from another launcher window or from the command line
    reaches the windows built afterwards as well.
    """

    def __init__(self, instance_id, root=None, interval=KEEPER_INTERVAL_S,
                 apply=None):
        super().__init__(name="bol-window-keeper", daemon=True)
        self._id = instance_id
        self._root = root
        self._interval = interval
        self._apply = apply or apply_hidden
        self._stop_event = threading.Event()

    def run(self):
        quiet = 0
        was_hidden = None
        while not self._stop_event.is_set():
            changed = 0
            try:
                instance = get(self._id, self._root)
                hidden = bool(instance is not None and instance.hidden)
                if hidden != was_hidden:
                    was_hidden, quiet = hidden, 0
                if hidden:
                    changed = self._apply(instance, self._root) or 0
            except Exception:
                pass
            quiet = 0 if changed else quiet + 1
            self._stop_event.wait(
                self._interval if quiet < KEEPER_QUIET_TICKS
                else max(self._interval, KEEPER_IDLE_INTERVAL_S))

    def stop(self, timeout=2.0):
        self._stop_event.set()
        if self.is_alive() and self is not threading.current_thread():
            self.join(timeout)


def format_uptime(seconds) -> str:
    """A duration the way a table shows it: 42 s, 3 min, 1 h 05."""
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d}"
