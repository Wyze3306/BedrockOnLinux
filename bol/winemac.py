"""bol.winemac — the macOS Windows runtime.

On Linux the game runs on GDK-Proton inside umu, the Steam Linux Runtime.
Neither of those is an executable that exists on macOS, so there the launcher
drives a **native macOS Wine** instead, and this module is everything that
knows about it: which one is installed, what environment it wants, how to build
a command with it, and how to tell whether a prefix is still busy without the
``/proc`` filesystem macOS does not have.

Detection order, best first:

``gptk``
    Apple's **Game Porting Toolkit** — a Wine with D3DMetal, Apple's own
    Direct3D-to-Metal translation. Apple Silicon only; the Wine itself is
    x86-64 and runs under Rosetta 2.
``crossover``
    CodeWeavers **CrossOver**, whose bundled Wine carries the same D3DMetal
    layer under a commercial licence.
``whisky``
    **Whisky**'s bundled Wine (also CrossOver-derived). Whisky is no longer
    developed, but a lot of Macs still have it and its runtime works.
``wine``
    A plain Homebrew or WineHQ build. It has no Metal translation layer, so
    Direct3D goes through the software-ish path and Minecraft will be slow.

An explicit choice always wins: ``$BOL_WINE`` or the Wine path in Settings.

**What rides on top unchanged.** The prefix layout, the DLL shims, the binary
patches and the host-side Microsoft pre-auth all operate on files in the prefix
and on Windows PE images, and a PE is a PE under any Wine — so those work here
exactly as they do on Linux.

**What does not exist here.** The WineGDK XUser fork that makes the in-game
Microsoft sign-in work is compiled into GDK-Proton, and there is no macOS build
of it; neither is there a macOS build of ``xodus-cli``, which is what downloads
Minecraft from the Microsoft Store and decrypts its executable at every launch.
So on macOS the launcher plays a **decrypted game folder you already have**,
offline and on LAN, and says so rather than pretending otherwise. See the macOS
section of the README.
"""
# SPDX-License-Identifier: MIT

import fcntl
import os
import re
import shutil
import subprocess
from pathlib import Path

from .log import BolError, die, info, ok, warn
from .platform import IS_MAC, mac_arm
from .util import load_settings, save_settings

# CrossOver's bottled Wine on a stock install.
_CROSSOVER_WINE = ("/Applications/CrossOver.app/Contents/SharedSupport/"
                   "CrossOver/bin/wine")
# Whisky keeps its runtime in its own application-support directory.
_WHISKY_WINE = (Path.home() / "Library/Application Support/"
                "com.isaacmarovitz.Whisky/Libraries/Wine/bin/wine")
# Homebrew's wine-stable cask, which puts nothing on PATH by default.
_WINE_CASKS = (
    "/Applications/Wine Stable.app/Contents/Resources/wine/bin/wine64",
    "/Applications/Wine Staging.app/Contents/Resources/wine/bin/wine64",
    "/Applications/Wine Devel.app/Contents/Resources/wine/bin/wine64",
)

BACKEND_NAMES = {
    "gptk": "Game Porting Toolkit",
    "crossover": "CrossOver",
    "whisky": "Whisky",
    "wine": "Wine",
    "custom": "a Wine you configured",
}

INSTALL_HINT = (
    "Install one of these Windows runtimes, then run Install / Update again:\n"
    "  • Game Porting Toolkit (recommended, Apple Silicon):\n"
    "      brew install apple/apple/game-porting-toolkit\n"
    "  • CrossOver:  https://www.codeweavers.com/crossover\n"
    "  • Wine:       brew install --cask wine-stable\n"
    "To point at a specific build instead, set BOL_WINE=/path/to/wine or fill "
    "in the Wine path in Settings."
)


def _require_mac(what):
    if not IS_MAC:
        raise BolError(f"winemac.{what} was called off macOS")


def _gptk_wine():
    """Game Porting Toolkit's wine, if Homebrew installed it.

    The formula puts a ``gameportingtoolkit`` wrapper on PATH and the real
    ``wine64`` beside it under the formula prefix. The wrapper is deliberately
    not used to run anything: its argument signature has changed between
    releases, while the wine binary's has not.
    """
    wrapper = shutil.which("gameportingtoolkit")
    if wrapper:
        base = Path(wrapper).resolve().parent
        for name in ("wine64", "wine"):
            if (base / name).exists():
                return base / name
    brew = shutil.which("brew")
    if brew:
        try:
            found = subprocess.run(
                [brew, "--prefix", "game-porting-toolkit"],
                capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        if found.returncode == 0:
            base = Path(found.stdout.strip())
            for name in ("bin/wine64", "bin/wine"):
                if (base / name).exists():
                    return base / name
    return None


def detect_wine():
    """Return ``(backend, wine_path)`` for the best Wine on this Mac, or
    ``(None, None)``.

    An explicit override wins; otherwise GPTK ▸ CrossOver ▸ Whisky ▸ Wine.
    """
    settings = load_settings()
    override = (os.environ.get("BOL_WINE")
                or settings.get("wine_override")
                or settings.get("wine"))
    if override:
        candidate = Path(override).expanduser()
        if candidate.exists():
            return (settings.get("wine_backend") or "custom", candidate)
        warn(f"The configured Wine '{candidate}' is not there any more — "
             "detecting one instead.")
    gptk = _gptk_wine()
    if gptk:
        return ("gptk", gptk)
    if Path(_CROSSOVER_WINE).exists():
        return ("crossover", Path(_CROSSOVER_WINE))
    if _WHISKY_WINE.exists():
        return ("whisky", _WHISKY_WINE)
    for cask in _WINE_CASKS:
        if Path(cask).exists():
            return ("wine", Path(cask))
    for name in ("wine64", "wine"):
        found = shutil.which(name)
        if found:
            return ("wine", Path(found))
    return (None, None)


def wine_bin():
    """The wine binary :func:`ensure_wine` recorded, or ``None``."""
    configured = load_settings().get("wine")
    if not configured:
        return None
    path = Path(configured)
    return path if path.exists() else None


def backend():
    """The recorded backend id, or ``None`` when no Wine is configured yet."""
    return load_settings().get("wine_backend") if wine_bin() else None


def wineserver_bin(wine=None):
    """The ``wineserver`` beside ``wine`` — the clean way to stop a prefix."""
    wine = wine or wine_bin()
    if not wine:
        return None
    candidate = Path(wine).parent / "wineserver"
    return candidate if candidate.exists() else None


def rosetta_problem():
    """Why an Apple Silicon Mac cannot run this Wine, or ``None``.

    Every macOS Wine that can render Direct3D is an x86-64 build, so on Apple
    Silicon it runs under Rosetta 2 — and a Mac without Rosetta installed
    fails with "Bad CPU type in executable", which names nothing a player can
    act on.
    """
    if not mac_arm():
        return None
    if Path("/Library/Apple/usr/libexec/oah").is_dir() or \
            Path("/usr/libexec/rosetta").is_dir():
        return None
    return ("Rosetta 2 is not installed, and the macOS Wine builds are all "
            "x86-64. Install it with:  softwareupdate --install-rosetta "
            "--agree-to-license")


def _backend_env(kind):
    """The environment a backend wants, as ``setdefault`` pairs so the host
    environment and the Advanced custom-environment field both still win."""
    env = {}
    if kind in ("gptk", "crossover", "whisky"):
        # CrossOver-derived Wines synchronise through msync on macOS; without
        # it every wait is a wineserver round-trip, which is the macOS shape
        # of the same "runs on one thread" stutter ntsync fixes on Linux.
        env["WINEMSYNC"] = "1"
        env["WINEESYNC"] = "1"
    if kind == "gptk":
        # D3DMetal's shader translation uses AVX, which Rosetta only exposes
        # when it is asked to; without this, Direct3D 12 device creation
        # fails outright.
        env["ROSETTA_ADVERTISE_AVX"] = "1"
        env["MTL_HUD_ENABLED"] = "0"
    return env


def wine_cmd(exe, prefix=None):
    """Build ``(argv, env)`` to run a Windows program or a Wine verb.

    Same contract as :func:`bol.prefix.proton_umu_cmd`, so the shared prefix,
    setup and launch code drives either runtime the same way: ``exe`` is
    either an absolute path to a ``.exe`` or a Wine verb such as ``wineboot``.
    """
    from .config import PFX
    _require_mac("wine_cmd")
    wine = wine_bin()
    if not wine:
        die("No macOS Windows runtime is configured yet — run Install / "
            "Update first.\n" + INSTALL_HINT)
    prefix = Path(prefix or PFX)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["WINEPREFIX"] = str(prefix)
    env.setdefault("WINEDEBUG", "fixme-all")
    # A 64-bit prefix. Minecraft is x86-64 and the GDK DLLs beside it are too,
    # so a prefix Wine decided to make 32-bit could never load them.
    env.setdefault("WINEARCH", "win64")
    for name, value in _backend_env(load_settings().get("wine_backend")).items():
        env.setdefault(name, value)
    return [str(wine), str(exe)], env


# ------------------------------------------------------------------ prefix


def server_dir(prefix):
    """The wineserver runtime directory for ``prefix``.

    Wine keys it on the prefix directory's device and inode rather than on its
    path, and so does this, because that is the only way to find the server of
    a prefix reached through a different symlink. Returns ``None`` when the
    prefix does not exist yet — in which case no server can be running for it.
    """
    try:
        stat = Path(prefix).stat()
    except OSError:
        return None
    base = os.environ.get("WINESERVER_DIR") or f"/tmp/.wine-{os.getuid()}"
    return Path(base) / f"server-{stat.st_dev:x}-{stat.st_ino:x}"


def prefix_busy(prefix):
    """Whether a wineserver still owns ``prefix``.

    macOS has no ``/proc`` to read another process's ``WINEPREFIX`` out of, so
    the launcher asks the same question Wine itself asks: the server holds an
    exclusive ``fcntl`` lock on the ``lock`` file in its runtime directory for
    as long as it lives, and a lock this process cannot take is a server that
    is still there. Unlike a process scan this cannot miss a service spawned
    while its parent exits — the lock outlives every one of them.
    """
    directory = server_dir(prefix)
    if directory is None:
        return False
    lock = directory / "lock"
    try:
        handle = os.open(lock, os.O_RDWR)
    except OSError:
        # No lock file at all: the server never started, or cleaned up.
        return False
    try:
        fcntl.lockf(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.lockf(handle, fcntl.LOCK_UN)
        return False
    finally:
        os.close(handle)


# Wine's own processes, plus the game running under them. `ps` shows the unix
# argv, so the prefix path appears for anything the launcher started with an
# absolute path, and the wine binary's own path appears for the rest.
def prefix_processes(prefix):
    """Best-effort PIDs belonging to ``prefix``, newest last.

    This is the macOS stand-in for the ``/proc`` environment scan: it matches
    the prefix path or the configured Wine's own directory in the command line
    ``ps`` reports. It can only see what names one of those, which is why
    :func:`prefix_busy` — not this — is what decides whether a prefix is idle.
    """
    try:
        listing = subprocess.run(["/bin/ps", "-A", "-o", "pid=,command="],
                                 capture_output=True, text=True, timeout=15,
                                 check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    wine = wine_bin()
    needles = [str(Path(prefix))]
    if wine:
        needles.append(str(Path(wine).parent))
    mine = os.getpid()
    found = []
    for line in listing.stdout.splitlines():
        text = line.strip()
        pid_text, _, command = text.partition(" ")
        if not pid_text.isdigit():
            continue
        pid = int(pid_text)
        if pid == mine:
            continue
        if any(needle in command for needle in needles):
            found.append(pid)
    return sorted(set(found))


def kill_prefix(prefix, timeout=30):
    """Stop everything in ``prefix`` the way Wine intends: ``wineserver -k``.

    Returns whether the prefix is idle afterwards.
    """
    server = wineserver_bin()
    if server:
        env = dict(os.environ)
        env["WINEPREFIX"] = str(prefix)
        try:
            subprocess.run([str(server), "-k"], env=env,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=timeout,
                           check=False)
        except (OSError, subprocess.SubprocessError):
            pass
    return not prefix_busy(prefix)


# ------------------------------------------------------------------ setup


def _version_text(wine):
    try:
        result = subprocess.run([str(wine), "--version"], capture_output=True,
                                text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (result.stdout or result.stderr or "").strip().splitlines()
    return text[0] if text else None


def wine_version(wine=None):
    """The ``wine --version`` line of the configured Wine, or ``None``."""
    wine = wine or wine_bin()
    return _version_text(wine) if wine else None


def summary():
    """One line for doctor: which runtime is configured, and which Wine."""
    if not IS_MAC:
        return "not applicable on Linux"
    problem = rosetta_problem()
    if problem:
        return "Rosetta 2 missing"
    wine = wine_bin()
    if not wine:
        found_backend, found = detect_wine()
        if found:
            return (f"{BACKEND_NAMES.get(found_backend, found_backend)} found "
                    f"({found}) — run Install / Update to use it")
        return "none installed"
    kind = backend() or "wine"
    version = wine_version(wine)
    line = f"{BACKEND_NAMES.get(kind, kind)} ({wine})"
    return line + (f" — {version}" if version else "")


def problem():
    """Why this Mac cannot run the game yet, or ``None``."""
    if not IS_MAC:
        return None
    rosetta = rosetta_problem()
    if rosetta:
        return rosetta
    if not wine_bin() and not detect_wine()[1]:
        return "No Windows runtime is installed. " + INSTALL_HINT
    if backend() == "wine":
        return ("A plain Wine has no Direct3D-to-Metal translation, so "
                "Minecraft renders through the fallback path and will be very "
                "slow. Game Porting Toolkit or CrossOver render it properly.")
    return None


def ensure_wine(force=False):
    """Detect a macOS Wine, record it in settings, and return its path.

    Dies with the install hints when there is none. ``force`` re-detects even
    when one is already recorded, which is what Install / Update passes after
    a player installs a better backend.
    """
    _require_mac("ensure_wine")
    rosetta = rosetta_problem()
    if rosetta:
        die(rosetta)
    settings = load_settings()
    current = settings.get("wine")
    if not force and current and Path(current).exists():
        kind = settings.get("wine_backend") or "wine"
        info(f"Windows runtime ready: {BACKEND_NAMES.get(kind, kind)} "
             f"({current}).")
        return Path(current)
    kind, wine = detect_wine()
    if not wine:
        die("No Windows runtime was found on this Mac.\n" + INSTALL_HINT)
    settings = load_settings()
    settings["wine"] = str(wine)
    settings["wine_backend"] = kind
    # Never "winegdk": that name means the managed GDK-Proton engine, whose
    # combase/ntdll byte offsets belong to one specific Linux build. This Wine
    # is a shared system installation the launcher must not patch in place.
    settings["proton_source"] = "winemac"
    # proton_path() answers "is there an engine at all", and several checks
    # refuse to go on without one. Point it at the Wine's root so it answers
    # truthfully, not at a Proton tree that does not exist here.
    settings["proton"] = str(Path(wine).parent.parent)
    save_settings(settings)
    ok(f"Windows runtime: {BACKEND_NAMES.get(kind, kind)} ({wine})")
    if kind == "wine":
        warn("This is a plain Wine, with no Direct3D-to-Metal translation. "
             "Minecraft will start, but expect it to be very slow — Game "
             "Porting Toolkit (Apple Silicon) or CrossOver render it "
             "properly.")
    return wine


_VERSION_LINE = re.compile(r"wine-(\d+)\.(\d+)")


def wine_version_tuple(text=None):
    """``(major, minor)`` parsed out of a ``wine --version`` line, or ``None``.

    Used only for reporting: nothing here refuses to run on a version, because
    what matters for this game is the backend's Direct3D layer, not Wine's
    own version number.
    """
    found = _VERSION_LINE.search(text or wine_version() or "")
    return (int(found.group(1)), int(found.group(2))) if found else None
