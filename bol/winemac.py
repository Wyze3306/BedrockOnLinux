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
    layer under a commercial licence. Its ``bin/wine`` is not Wine itself but
    a Perl front end that runs *bottles* and ignores ``WINEPREFIX``, so the
    launcher's prefix is handed to it as a bottle -- see
    :func:`ensure_crossover_bottle`.
``whisky``
    **Whisky**'s bundled Wine (also CrossOver-derived). Whisky is no longer
    developed, but a lot of Macs still have it and its runtime works.
``wine``
    A plain Homebrew or WineHQ build. It has no Metal translation layer, so
    Direct3D goes through the software-ish path and Minecraft will be slow.

An explicit choice always wins: ``$BOL_WINE``, or ``wine_override`` in the
settings file.

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
import getpass
import os
import re
import shutil
import subprocess
from pathlib import Path

from .log import BolError, die, info, ok, warn
from .platform import IS_MAC, mac_arm
from .util import load_settings, save_settings

# CrossOver's bottled Wine on a stock install, and dragged into the user's
# own Applications folder instead.
_CROSSOVER_WINE = ("/Applications/CrossOver.app/Contents/SharedSupport/"
                   "CrossOver/bin/wine")
_CROSSOVER_USER_WINE = (Path.home() / "Applications/CrossOver.app/Contents/"
                        "SharedSupport/CrossOver/bin/wine")
# Whisky keeps its runtime in its own application-support directory. Its Wine
# is called wine64 in the builds most Macs have, and wine in newer ones.
_WHISKY_WINE = (Path.home() / "Library/Application Support/"
                "com.isaacmarovitz.Whisky/Libraries/Wine/bin/wine")
# Where the Game Porting Toolkit formula lands: it only installs into the
# Intel Homebrew under Rosetta, which is not the brew an Apple Silicon shell
# finds first on PATH.
_GPTK_PREFIXES = ("/usr/local/opt/game-porting-toolkit",
                  "/opt/homebrew/opt/game-porting-toolkit")
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
    "Install one of these Windows runtimes, then click PLAY again:\n"
    "  • CrossOver:  https://www.codeweavers.com/crossover\n"
    "  • Game Porting Toolkit (Apple Silicon):\n"
    "      brew install apple/apple/game-porting-toolkit\n"
    "  • Wine:       brew install --cask wine-stable\n"
    "To use a specific build instead, start the launcher with "
    "BOL_WINE=/path/to/wine set."
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
    for base in _GPTK_PREFIXES:
        for name in ("bin/wine64", "bin/wine"):
            if (Path(base) / name).exists():
                return Path(base) / name
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
    The Wine a previous detection recorded is not an override: reading it
    back as one is what kept a Mac on a plain Wine after CrossOver was
    installed beside it.
    """
    override = _explicit_wine()
    if override:
        candidate = Path(override).expanduser()
        if candidate.exists():
            return ("crossover" if is_crossover_wrapper(candidate)
                    else "custom", candidate)
        warn(f"The configured Wine '{candidate}' is not there any more — "
             "detecting one instead.")
    gptk = _gptk_wine()
    if gptk:
        return ("gptk", gptk)
    for crossover in (Path(_CROSSOVER_WINE), _CROSSOVER_USER_WINE):
        if crossover.exists():
            return ("crossover", crossover)
    for whisky in (_WHISKY_WINE.with_name("wine64"), _WHISKY_WINE):
        if whisky.exists():
            return ("whisky", whisky)
    for cask in _WINE_CASKS:
        if Path(cask).exists():
            return ("wine", Path(cask))
    for name in ("wine64", "wine"):
        found = shutil.which(name)
        if found:
            return ("wine", Path(found))
    return (None, None)


def _explicit_wine():
    """The Wine the player named themselves, or ``None``."""
    return (os.environ.get("BOL_WINE", "").strip()
            or (load_settings().get("wine_override") or "").strip() or None)


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
    if kind == "crossover" and mac_arm():
        # Minecraft draws through Direct3D 12, which CrossOver hands to
        # D3DMetal -- Apple's translation, Apple Silicon only -- when a
        # bottle asks for it. A bottle's own settings say so in its
        # [EnvironmentVariables]; the launcher's bottle is made here, so it
        # asks here. "auto" or "dxmt" in the custom environment still wins.
        env["CX_GRAPHICS_BACKEND"] = "d3dmetal"
    return env


# ---------------------------------------------------------------- CrossOver
#
# CrossOver's bin/wine is not a Wine binary. It is a Perl front end that runs
# a *bottle*: it looks the bottle up by name in ~/Library/Application Support/
# CrossOver/Bottles, sets WINEPREFIX to it itself, and execs the real loader
# (bin/wineloader) with the environment CrossOver needs -- the library paths,
# D3DMetal, the bottle's own settings. A WINEPREFIX given to it is simply
# overwritten. Without a bottle named "default" it stopped with "bottle
# 'default' not found", and with one it initialised *that* bottle while the
# launcher waited for its own prefix to appear.
#
# Two properties of that front end (lib/perl/CXBottle.pm, find_bottle) make it
# usable as it is: a bottle given as an absolute path is used where it is,
# with no lookup at all, and the one file that path needs to be a bottle is a
# cxbottle.conf. So the launcher's prefix becomes a private bottle that lives
# where the prefix always lived, and CrossOver itself sets up everything
# around it.

# A bottle CrossOver itself would create from its win10_64 template, minus
# the menus and file associations it would otherwise publish to the Mac.
_CXBOTTLE_CONF = """\
;; Written by BedrockOnLinux: this Wine prefix runs as a CrossOver bottle.
[Bottle]
"Timestamp" = "{timestamp}"
"Encoding" = "UTF-8"
"Template" = "win10_64"
"Description" = "BedrockOnLinux"
"MenuMode" = "ignore"
"AssocMode" = "ignore"
"WineArch" = "win64"

[EnvironmentVariables]
"""
_BUILD_TIMESTAMP = re.compile(r'^"BuildTimestamp"\s*=\s*"([^"]*)"', re.M)


def is_crossover_wrapper(wine):
    """Whether ``wine`` is CrossOver's bottle front end rather than a Wine.

    Read from the file rather than from the backend name, so that BOL_WINE
    pointed at CrossOver's bin/wine is handled the same way.
    """
    try:
        with open(wine, "rb") as stream:
            head = stream.read(8192)
    except (OSError, TypeError):
        return False
    return head.startswith(b"#!") and b"CX_ROOT" in head


def _crossover_root(wine):
    """CrossOver's own root (``.../SharedSupport/CrossOver``) for its bin/wine."""
    return Path(wine).parent.parent


def _crossover_timestamp(wine):
    """The build timestamp a bottle up to date with this CrossOver records.

    A bottle whose timestamp differs is upgraded by CrossOver's template
    script before anything runs in it, which is right after a CrossOver
    update and pointless on a bottle the launcher has just described.
    """
    try:
        text = (_crossover_root(wine) / "etc" / "CrossOver.conf").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return ""
    found = _BUILD_TIMESTAMP.search(text)
    return found.group(1) if found else ""


def ensure_crossover_bottle(prefix, wine):
    """Make ``prefix`` a CrossOver bottle, keeping whatever it already holds.

    Only the description CrossOver reads is added; the prefix itself, and the
    worlds inside it, are left exactly as they are. Returns the bottle path.
    """
    prefix = Path(prefix)
    prefix.mkdir(parents=True, exist_ok=True)
    conf = prefix / "cxbottle.conf"
    if not conf.is_file():
        conf.write_text(_CXBOTTLE_CONF.format(
            timestamp=_crossover_timestamp(wine)), encoding="utf-8")
    return prefix


def finalize_cmd(cmd, env):
    """Last changes before a command from :func:`wine_cmd` runs.

    CrossOver's front end deletes WINEDLLOVERRIDES from the environment it is
    given and takes the overrides as ``--dll`` instead, so the overrides the
    launch settled on are moved there once they are final. Anything else is
    returned untouched.
    """
    if not cmd or not is_crossover_wrapper(cmd[0]) or "--" not in cmd:
        return cmd, env
    env = dict(env)
    overrides = env.pop("WINEDLLOVERRIDES", "")
    if overrides:
        split = cmd.index("--")
        cmd = cmd[:split] + ["--dll", overrides] + cmd[split:]
    return cmd, env


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
        die("No macOS Windows runtime is configured yet.\n" + INSTALL_HINT)
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
    if is_crossover_wrapper(wine):
        bottle = ensure_crossover_bottle(prefix, wine)
        env["CX_BOTTLE"] = str(bottle)
        # Set by the front end once a bottle's environment is in place; one
        # inherited from a CrossOver session would make it skip that setup.
        env.pop("CX_INITIALIZED", None)
        # --wait: without it the front end returns as soon as the program has
        # started, and the launcher takes the game for closed the moment it
        # opens. Everything after "--" is the program and its arguments.
        return [str(wine), "--bottle", str(bottle), "--wait", "--",
                str(exe)], env
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


def _game_paths():
    """The folders the game is started from, as ``ps`` would show them."""
    from .config import CONTENT, GAMES
    paths = {str(CONTENT), str(GAMES)}
    configured = (load_settings().get("game_dir") or "").strip()
    if configured:
        paths.add(configured)
    for path in list(paths):
        try:
            paths.add(str(Path(path).resolve()))
        except (OSError, RuntimeError):
            pass
    return paths


# The launcher's own Wine processes: what it started names the prefix (a
# CrossOver bottle path) or the game folder in its command line.
def prefix_processes(prefix):
    """Best-effort PIDs belonging to ``prefix``, newest last.

    This is the macOS stand-in for the ``/proc`` environment scan: it matches
    the prefix path or the game's folder in the command line ``ps`` reports.
    Not the Wine's own directory: CrossOver, Whisky and the Game Porting
    Toolkit are shared by every bottle on the Mac, and a match on their path
    is how "Force stop" and the clean-up after every wineboot killed the
    player's other Windows programs along with Minecraft. Wine's services
    name neither, which is why :func:`prefix_busy` -- not this -- decides
    whether a prefix is idle, and ``wineserver -k`` is what stops them.
    """
    try:
        listing = subprocess.run(["/bin/ps", "-A", "-o", "pid=,command="],
                                 capture_output=True, text=True, timeout=15,
                                 check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    needles = {str(Path(prefix))} | _game_paths()
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


def wine_user_name(prefix):
    """The Windows user whose profile Wine made in ``prefix``.

    Proton always calls it ``steamuser``, which is the name the rest of the
    launcher was written against. A macOS Wine does not: CrossOver and the
    Wines derived from it call it ``crossover``, and a plain Wine uses the
    Mac's own login. Everything the game keeps -- worlds, options.txt, the
    Servers tab -- is under that profile, so asking for steamuser's on a Mac
    named a folder that never exists ("Open Minecraft folder" did nothing).
    A profile that already holds Minecraft's data wins; before the first
    boot, the name this Wine is going to use.
    """
    users = Path(prefix) / "drive_c" / "users"
    try:
        names = sorted(entry.name for entry in users.iterdir()
                       if entry.is_dir() and not entry.is_symlink()
                       and entry.name.lower() != "public")
    except OSError:
        names = []
    for name in names:
        if any((users / name / "AppData" / "Roaming").glob(
                "Minecraft Bedrock*")):
            return name
    wine = wine_bin()
    if load_settings().get("wine_backend") in ("crossover", "whisky", "gptk") \
            or (wine and is_crossover_wrapper(wine)):
        expected = "crossover"
    else:
        expected = os.environ.get("USER") or getpass.getuser()
    if expected in names or not names:
        return expected
    return names[0]


# ------------------------------------------------------------------ setup


def _version_text(wine):
    try:
        result = subprocess.run([str(wine), "--version"], capture_output=True,
                                text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (result.stdout or result.stderr or "").strip().splitlines()
    # CrossOver's front end answers with a short report rather than Wine's
    # "wine-x.y" line; its public version is the line that means something.
    for line in text:
        if line.startswith("Public Version:"):
            return "CrossOver " + line.partition(":")[2].strip()
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
                    f"({found}) — used from the next PLAY")
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
    when one is already recorded.
    """
    _require_mac("ensure_wine")
    rosetta = rosetta_problem()
    if rosetta:
        die(rosetta)
    settings = load_settings()
    current = settings.get("wine")
    recorded = settings.get("wine_backend") or "wine"
    explicit = _explicit_wine()
    # Kept as recorded unless the player named another one, or it is a plain
    # Wine -- the one backend worth looking past at every PLAY, because the
    # player who installs CrossOver or the Game Porting Toolkit to replace it
    # should get it without knowing to ask.
    settled = (current and Path(current).exists() and recorded != "wine"
               and (not explicit
                    or Path(explicit).expanduser() == Path(current)))
    if not force and settled:
        info(f"Windows runtime ready: {BACKEND_NAMES.get(recorded, recorded)} "
             f"({current}).")
        return Path(current)
    kind, wine = detect_wine()
    if not wine:
        if current and Path(current).exists():
            return Path(current)
        die("No Windows runtime was found on this Mac.\n" + INSTALL_HINT)
    if current and Path(wine) == Path(current) and kind == recorded:
        info(f"Windows runtime ready: {BACKEND_NAMES.get(kind, kind)} "
             f"({current}).")
        return Path(current)
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
