"""bol.platform — the one place that knows which operating system this is.

BedrockOnLinux was born Linux-only: the game runs on GDK-Proton inside umu (the
Steam Linux Runtime), and half the launcher reads ``/proc``, talks to X11 or
Wayland, writes ``.desktop`` files and drives Vulkan-side payloads. None of
that exists on macOS, where the Windows runtime is a native Wine (Apple's Game
Porting Toolkit, CrossOver, Whisky or a plain build) driven from
:mod:`bol.winemac`.

Rather than sprinkle ``sys.platform`` checks through twenty modules, everything
that differs per OS is answered here: where user data lives, how a file or URL
is opened, how a process is killed without ``/proc``, how a message reaches the
desktop, and — the one every caller wants — whether a Linux-only subsystem
applies at all.

It imports only the standard library, so the lowest layer (:mod:`bol.config`)
can import it without creating a cycle.
"""
# SPDX-License-Identifier: MIT

import os
import shutil
import subprocess
import sys
from pathlib import Path

SYSTEM = sys.platform
IS_MAC = SYSTEM == "darwin"
IS_LINUX = SYSTEM.startswith("linux")

# Shown wherever a Linux-only check is asked for on macOS. A check that cannot
# run is not a check that passed, and it is not a failure either -- doctor and
# the GUI both print this rather than inventing an answer.
NOT_APPLICABLE = "not applicable on macOS"


def os_name():
    """Human name of the host OS, for messages and the doctor header."""
    if IS_MAC:
        return "macOS"
    if IS_LINUX:
        return "Linux"
    return SYSTEM


def data_home(app: str) -> Path:
    """Per-OS data directory for ``app``.

    Linux keeps the XDG layout (``$XDG_DATA_HOME`` or ``~/.local/share``);
    macOS uses ``~/Library/Application Support``, which is where a Mac user
    expects to find — and to be able to delete — an application's state.
    """
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / app
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base).expanduser() if base
            else Path.home() / ".local" / "share") / app


def config_home(app: str) -> Path:
    """Per-OS configuration directory for ``app``. Same split as
    :func:`data_home`; macOS has no separate config location by convention, so
    it uses ``~/Library/Application Support`` too."""
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / app
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base).expanduser() if base
            else Path.home() / ".config") / app


_PM_LINUX = (
    ("apt-get", "sudo apt install {}"),
    ("dnf", "sudo dnf install {}"),
    ("pacman", "sudo pacman -S {}"),
    ("zypper", "sudo zypper in {}"),
)


def pm_hint() -> str:
    """A ``"install {}"``-shaped template for the host's package manager, so
    every "you are missing X" message can name a command that actually works
    here — ``brew install`` on macOS."""
    if IS_MAC:
        return "brew install {}"
    for pm, hint in _PM_LINUX:
        if shutil.which(pm):
            return hint
    return "install: {}"


def in_flatpak(info_path=Path("/.flatpak-info")) -> bool:
    """Whether this process runs inside the Flatpak sandbox. Never true on
    macOS, where there is no Flatpak to be inside of."""
    if not IS_LINUX:
        return False
    return bool(os.environ.get("FLATPAK_ID")) or Path(info_path).is_file()


def has_display() -> bool:
    """Whether a graphical session is reachable, so `bol` with no argument can
    decide between opening a window and printing its help.

    On macOS the answer is always yes: a process launched from Finder, from a
    ``.app`` or from Terminal can open a window with no display variable to
    look at, and requiring ``$DISPLAY`` there would make the launcher print
    help at every double-click.
    """
    if IS_MAC:
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def kill_pattern(pattern: str):
    """Best-effort SIGKILL of processes whose command line matches ``pattern``.

    ``pkill`` when it exists (it is part of macOS), and a ``/proc`` sweep as
    the fallback for the Linux hosts that lack it. macOS has no ``/proc``, so
    the fallback is guarded rather than merely unused.
    """
    if shutil.which("pkill"):
        subprocess.run(["pkill", "-9", "-f", pattern],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=False)
        return
    if not IS_LINUX:
        return
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            if pattern.encode() in cmdline.replace(b"\0", b" "):
                os.kill(int(pid), 9)
        except (OSError, ValueError, ProcessLookupError):
            pass


def process_environ(pid):
    """The environment of another process as a dict, or ``None`` when it
    cannot be read.

    This is how the launcher tells its own Wine processes from everybody
    else's: it looks for ``WINEPREFIX`` pointing at its prefix. Linux answers
    it from ``/proc/<pid>/environ``; macOS refuses to show one process's
    environment to another (``ps -E`` needs root and is redacted besides), so
    there it answers ``None`` and callers fall back to
    :func:`bol.winemac.prefix_busy`, which asks wineserver instead.
    """
    if not IS_LINUX:
        return None
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    environ = {}
    for chunk in raw.split(b"\0"):
        name, sep, value = chunk.partition(b"=")
        if sep:
            environ[name.decode("utf-8", "replace")] = \
                value.decode("utf-8", "replace")
    return environ


def open_path(target):
    """Open a file, a folder or a URL with the desktop's default handler.
    ``open`` on macOS, ``xdg-open`` on Linux. Best-effort; never raises."""
    opener = "open" if IS_MAC else "xdg-open"
    try:
        subprocess.Popen([opener, str(target)], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def notify(message, summary=None):
    """Put a message on screen for a run with no visible terminal.

    A ``.app`` double-click and a desktop shortcut both discard stdout, and an
    unreported failure there is indistinguishable from the launcher doing
    nothing at all. macOS has no ``notify-send``; the scriptable equivalent is
    a Notification Center banner posted through ``osascript``.
    """
    title = summary or "BedrockOnLinux"
    if IS_MAC:
        script = shutil.which("osascript")
        if not script:
            return False
        body = str(message).replace("\\", "\\\\").replace('"', '\\"')
        head = str(title).replace("\\", "\\\\").replace('"', '\\"')
        command = [script, "-e",
                   f'display notification "{body}" with title "{head}"']
    else:
        notifier = shutil.which("notify-send")
        if not notifier:
            return False
        command = [notifier, "--app-name", "BedrockOnLinux", title,
                   str(message)]
    try:
        subprocess.run(command, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def mac_arm() -> bool:
    """Whether this is Apple Silicon. Game Porting Toolkit exists only there,
    and an Intel Mac has to be told so rather than sent chasing a formula that
    will not install."""
    if not IS_MAC:
        return False
    return (os.uname().machine or "").lower() in ("arm64", "aarch64")
