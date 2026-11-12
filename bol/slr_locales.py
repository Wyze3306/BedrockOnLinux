"""bol.slr_locales — build the Steam Linux Runtime's missing locales once.

pressure-vessel wants en_US.UTF-8 in the container, besides the player's own
locale. When the host lacks one of them, its pv-locale-gen compiles every
locale the game asks for into a temporary directory and points LOCPATH at it,
on every single launch: 3 to 6 seconds before the game window on a French
desktop with only fr_FR.UTF-8 installed. Switching that off
(PRESSURE_VESSEL_GENERATE_LOCALES=0) would leave the game without en_US and,
on some hosts, without the player's own locale: accented input and the
game's default language follow it.

So the launcher runs the same pv-locale-gen, in the same container, once,
into a directory it keeps; later launches hand that directory over as
LOCPATH and tell pressure-vessel there is nothing left to generate. The game
sees exactly the locales it would have had, without paying for them again.
The copy is keyed on the runtime and on the host's glibc -- the container
uses whichever glibc is newer, and compiled locales belong to one -- and on
the locale names, so a change to any of them builds a fresh copy.
"""
# SPDX-License-Identifier: MIT

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .config import CACHE, LOGS, UMU_DIR
from .log import info

# The variables pv-locale-gen reads, in its own order.
LOCALE_VARIABLES = (
    "LC_ADDRESS", "LC_CTYPE", "LC_COLLATE", "LC_IDENTIFICATION",
    "LC_MEASUREMENT", "LC_MESSAGES", "LC_MONETARY", "LC_NUMERIC", "LC_NAME",
    "LC_PAPER", "LC_TELEPHONE", "LC_TIME", "HOST_LC_ALL", "LANG", "LC_ALL",
)
# What pv-locale-gen always wants, whatever the environment says.
ALWAYS_WANTED = ("en_US.UTF-8",)
# pressure-vessel's own tools, where it mounts them inside the container.
PV_LOCALE_GEN = ("/usr/lib/pressure-vessel/from-host/libexec/"
                 "steam-runtime-tools-0/pv-locale-gen")
CACHE_DIR = CACHE / "slr-locales"
STAMP = "bol-locales.json"
GENERATE_TIMEOUT = 180
# A failure is not retried at every launch -- that would cost more than the
# generation it tries to save -- but neither forever: a loaded machine can
# time it out once.
RETRY_AFTER = 7 * 24 * 3600
# glibc's LC_ALL_MASK: every category bit except LC_ALL's own (6).
_LC_ALL_MASK = sum(1 << category for category in range(13) if category != 6)


def wanted_locales(environ):
    """The locales pv-locale-gen would want for ``environ``, in its order."""
    wanted = []
    for variable in LOCALE_VARIABLES:
        wanted.append((environ.get(variable) or "").strip())
    wanted.extend(ALWAYS_WANTED)
    names = []
    for name in wanted:
        if name in ("", "C", "C.UTF-8", "C.utf8", "POSIX"):
            continue
        # pv-locale-gen refuses these too, rather than write outside its
        # output directory.
        if ".." in name or "/" in name:
            continue
        if name not in names:
            names.append(name)
    return names


def missing_on_host(names, available=None):
    """The ``names`` this host's glibc cannot load; None when unknown."""
    if available is None:
        from .perfcheck import locale_available as available
    missing = []
    for name in names:
        found = available(name)
        if found is None:
            return None
        if not found:
            missing.append(name)
    return missing


def runtime_dir():
    return UMU_DIR / "steamrt3"


def runtime_version(root=None):
    """The Steam Linux Runtime's depot version, or None when not installed."""
    root = Path(root or runtime_dir())
    try:
        for line in (root / "VERSIONS.txt").read_text(
                encoding="utf-8", errors="replace").splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0] == "depot":
                return fields[1]
    except OSError:
        pass
    try:
        platforms = sorted(p.name for p in root.glob("sniper_platform_*"))
    except OSError:
        return None
    return platforms[-1] if platforms else None


def host_glibc():
    try:
        return os.confstr("CS_GNU_LIBC_VERSION") or ""
    except (ValueError, OSError):
        return ""


def cache_key(names, version, glibc):
    blob = json.dumps({"locales": sorted(names), "runtime": version,
                       "glibc": glibc}, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _read_stamp(directory):
    try:
        return json.loads((Path(directory) / STAMP).read_text(
            encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _complete(directory, names):
    """Whether ``directory`` holds a compiled copy of every one of ``names``."""
    stamp = _read_stamp(directory)
    if not stamp or stamp.get("kind") != "generated":
        return False
    return all((Path(directory) / name / "LC_CTYPE").is_file()
               for name in names)


def _failed_marker(key):
    return CACHE_DIR / (key + ".failed")


def _recent_failure(key, now=None):
    marker = _failed_marker(key)
    try:
        age = (now or time.time()) - marker.stat().st_mtime
    except OSError:
        return None
    if age > RETRY_AFTER:
        return None
    try:
        return marker.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def _record_failure(key, reason):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _failed_marker(key).write_text(reason + "\n", encoding="utf-8")
    except OSError:
        pass


def _plan(environ, available=None, root=None):
    """(names, key, directory) when this launch would build locales, else None."""
    if (environ.get("PRESSURE_VESSEL_GENERATE_LOCALES") or "").strip():
        return None
    if (environ.get("LOCPATH") or "").strip():
        return None
    names = wanted_locales(environ)
    missing = missing_on_host(names, available)
    if not missing:
        return None
    version = runtime_version(root)
    if not version:
        return None
    key = cache_key(names, version, host_glibc())
    return names, key, CACHE_DIR / key


def state(environ=None, available=None, root=None):
    """Where the container's locales stand for ``environ``.

    None when pressure-vessel has nothing to generate (or was told what to
    do); otherwise "cached", "none-needed", "pending" (built at the next
    launch) or "failed".
    """
    plan = _plan(os.environ if environ is None else environ, available, root)
    if plan is None:
        return None
    names, key, directory = plan
    stamp = _read_stamp(directory)
    if stamp and stamp.get("kind") == "none-needed":
        return "none-needed"
    if _complete(directory, names):
        return "cached"
    if _recent_failure(key):
        return "failed"
    return "pending"


def _host_loads(directory, names):
    """Whether this host's glibc reads the copy too: LOCPATH reaches the
    launcher-side processes (umu, pressure-vessel's wrapper, xodus-cli) as
    well as the container."""
    check = (
        "import ctypes, sys\n"
        "libc = ctypes.CDLL(None)\n"
        "libc.newlocale.restype = ctypes.c_void_p\n"
        "libc.newlocale.argtypes = [ctypes.c_int, ctypes.c_char_p, "
        "ctypes.c_void_p]\n"
        "bad = [n for n in sys.argv[1:] "
        f"if not libc.newlocale({_LC_ALL_MASK}, n.encode(), None)]\n"
        "sys.exit(1 if bad else 0)\n"
    )
    env = {key: value for key, value in os.environ.items()
           if key not in LOCALE_VARIABLES}
    env["LOCPATH"] = str(directory)
    try:
        return subprocess.run(
            [sys.executable, "-I", "-S", "-c", check, *names], env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _prune(keep):
    try:
        entries = list(CACHE_DIR.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name in (keep, keep + ".failed") or \
                entry.name.startswith("."):
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            try:
                entry.unlink()
            except OSError:
                pass


def _generate(names, key, directory, environ, root):
    """Run pv-locale-gen in the runtime once; return a failure reason or None."""
    entry_point = Path(root) / "_v2-entry-point"
    if not entry_point.is_file():
        return "the Steam Linux Runtime has no _v2-entry-point"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="." + key + "-", dir=CACHE_DIR))
    env = {key_: value for key_, value in os.environ.items()
           if key_ not in LOCALE_VARIABLES}
    for variable in LOCALE_VARIABLES:
        if environ.get(variable):
            env[variable] = environ[variable]
    env.pop("LOCPATH", None)
    # Or the container's own startup builds a throwaway set first.
    env["PRESSURE_VESSEL_GENERATE_LOCALES"] = "0"
    command = [str(entry_point), "--verb=waitforexitandrun", "--",
               PV_LOCALE_GEN, "--output-dir", str(staging), *names]
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        with open(LOGS / "slr-locales.log", "w", encoding="utf-8") as log:
            log.write("$ " + " ".join(command) + "\n")
            log.flush()
            status = subprocess.run(
                command, env=env, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, timeout=GENERATE_TIMEOUT,
                start_new_session=True).returncode
    except subprocess.TimeoutExpired:
        shutil.rmtree(staging, ignore_errors=True)
        return f"pv-locale-gen took longer than {GENERATE_TIMEOUT} s"
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        return f"could not start the Steam Linux Runtime ({exc})"
    if status == 0:
        # The container has them all after all: pressure-vessel generates
        # nothing at launch either, and there is nothing to keep.
        kind = "none-needed"
    elif status == 72:
        # EX_OSFILE: "some were missing and have been generated".
        kind = "generated"
        absent = [name for name in names
                  if not (staging / name / "LC_CTYPE").is_file()]
        if absent:
            shutil.rmtree(staging, ignore_errors=True)
            return "pv-locale-gen did not produce " + ", ".join(absent)
        if not _host_loads(staging, names):
            shutil.rmtree(staging, ignore_errors=True)
            return "this system's glibc cannot read the generated locales"
    else:
        shutil.rmtree(staging, ignore_errors=True)
        return f"pv-locale-gen exited with status {status}"
    (staging / STAMP).write_text(json.dumps(
        {"kind": kind, "locales": names, "runtime": runtime_version(root),
         "glibc": host_glibc()}, indent=1) + "\n", encoding="utf-8")
    if directory.exists():
        shutil.rmtree(directory, ignore_errors=True)
    os.replace(staging, directory)
    _prune(key)
    return None


def configure(env, available=None, root=None):
    """Give ``env`` a kept copy of the locales pressure-vessel would build.

    ``env`` is the environment the game is started with, after the Advanced
    custom-environment field: that is where a different LANG, or the
    player's own PRESSURE_VESSEL_GENERATE_LOCALES, comes from. Returns the
    directory handed over as LOCPATH, or None when the launch is left to
    pressure-vessel as before. Never raises: the worst case is the old
    behaviour.
    """
    root = Path(root or runtime_dir())
    try:
        plan = _plan(env, available, root)
        if plan is None:
            return None
        names, key, directory = plan
        stamp = _read_stamp(directory)
        if stamp and stamp.get("kind") == "none-needed":
            return None
        if not _complete(directory, names):
            if _recent_failure(key):
                return None
            info("Preparing the game's locales (" + ", ".join(names)
                 + ") once, so later launches start without building them "
                 "again …")
            started = time.monotonic()
            reason = _generate(names, key, directory, env, root)
            if reason:
                _record_failure(key, reason)
                info(f"The game's locales could not be kept ({reason}); "
                     "the Steam Linux Runtime builds them at each launch, as "
                     "before.")
                return None
            info("Locales ready in %.1f s." % (time.monotonic() - started))
            stamp = _read_stamp(directory)
            if stamp and stamp.get("kind") == "none-needed":
                return None
    except Exception as exc:  # never worth failing a launch over
        info(f"The game's locales could not be prepared "
             f"({type(exc).__name__}: {exc}).")
        return None
    env["LOCPATH"] = str(directory)
    env["PRESSURE_VESSEL_GENERATE_LOCALES"] = "0"
    return str(directory)
