"""bol.ntsync — report whether Wine's in-process synchronization is usable.

Upstream Wine 11 has no esync/fsync any more: its only fast synchronization
path is the kernel ntsync driver behind ``/dev/ntsync`` (mainline Linux 6.14).
When no fast path is available every Win32 wait, ``SetEvent``, mutex and
semaphore becomes a wineserver round-trip, and because the wineserver
serialises those requests Minecraft's worker threads queue behind one another.
The game then behaves as if it were single-threaded: low server TPS, a starved
GPU and chunk-generation stalls (issues #63, #139, #143, #148, #150).

Engines carrying vendored patch 0014 (Valve's futex-based fsync backend) fall
back to fsync when ``/dev/ntsync`` cannot be opened, which only needs the
``futex_waitv`` system call (Linux 5.16).  The wineserver always tries ntsync
first, so the fallback changes nothing where the driver is present.

A fast path is there when either holds:

* the engine was *built* with the ntsync backend and the host kernel exposes
  ``/dev/ntsync``, or
* the engine carries the fsync fallback, the kernel answers ``futex_waitv``
  and ``PROTON_NO_FSYNC`` is not set.

The remedies differ (update the engine, load or get a kernel driver, clear a
variable), so the message names the one that applies.  Detection is cheap and
side-effect free: byte searches in ``wineserver``, a stat of the device node
and an empty ``futex_waitv`` call, which the kernel rejects without waiting on
anything.  No Wine process is started and no ioctl is attempted, so this can
run before every launch.
"""
# SPDX-License-Identifier: MIT

import ctypes
import errno
import os
import platform
from pathlib import Path

# server/inproc_sync.c only compiles this literal in when the build found
# <linux/ntsync.h>; its absence is exactly the "compiled out to stubs" case.
_NTSYNC_DEVICE_MARKER = b"/dev/ntsync"

# Both are produced by one build, so either answers the question; the managed
# engine runs the WoW64 server, a custom Proton may ship only the classic one.
_WINESERVER_PATHS = ("files/bin-wow64/wineserver", "files/bin/wineserver")

# Patch 0014's server reads this variable before it allows the futex_waitv
# fallback; the literal is only there when the backend is compiled in.
_FSYNC_FALLBACK_MARKER = b"WINEFSYNC"

NTSYNC_DEVICE = "/dev/ntsync"

# Mainline kernel that first shipped the driver. Distributions do backport it,
# so this is quoted as guidance only and never used to decide the verdict.
MAINLINE_KERNEL = "6.14"

# futex_waitv(2), the only thing the fsync fallback needs from the kernel.
# Every Linux architecture except alpha numbers it 449.
_NR_FUTEX_WAITV = 449
FUTEX_WAITV_KERNEL = "5.16"

_FALSY = ("", "0", "no", "off", "false")


def _file_contains(path, needle, chunk_size=1 << 20):
    """Whether a binary contains a literal, read in overlapping chunks."""
    overlap = len(needle) - 1
    try:
        with Path(path).open("rb") as handle:
            tail = b""
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    return False
                if needle in tail + chunk:
                    return True
                tail = chunk[-overlap:] if overlap else b""
    except OSError:
        return None


def _engine_server_contains(engine_root, marker):
    """Whether any wineserver of the engine carries a literal.

    Returns True/False, or None when no wineserver could be read at all — an
    unreadable engine is reported as unknown rather than as a defect.
    """
    if not engine_root:
        return None
    root = Path(engine_root)
    verdict = None
    for relative in _WINESERVER_PATHS:
        found = _file_contains(root / relative, marker)
        if found is None:
            continue
        if found:
            return True
        verdict = False
    return verdict


def engine_supports_inproc_sync(engine_root):
    """Whether the engine was built with the ntsync backend compiled in."""
    return _engine_server_contains(engine_root, _NTSYNC_DEVICE_MARKER)


def engine_has_fsync_fallback(engine_root):
    """Whether the engine falls back to fsync when ntsync is unavailable."""
    return _engine_server_contains(engine_root, _FSYNC_FALLBACK_MARKER)


def kernel_supports_futex_waitv():
    """Whether this process may call futex_waitv(2).

    Asked the way Wine asks: an empty call, which a kernel that knows the
    system call rejects with EINVAL and an older one with ENOSYS. A seccomp
    filter answering EPERM counts as absent, as it does for Wine. Returns
    None off Linux or when the call cannot be made from Python at all.
    """
    if platform.system() != "Linux" or platform.machine() == "alpha":
        return None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        ctypes.set_errno(0)
        result = libc.syscall(ctypes.c_long(_NR_FUTEX_WAITV), None,
                              ctypes.c_uint(0), ctypes.c_uint(0), None,
                              ctypes.c_int(0))
    except (AttributeError, OSError, TypeError):
        return None
    if result == 0:
        return True
    return ctypes.get_errno() not in (errno.ENOSYS, errno.EPERM)


def kernel_exposes_ntsync(device=None):
    """Whether the running kernel provides the ntsync character device."""
    path = Path(device) if device is not None else Path(NTSYNC_DEVICE)
    try:
        return path.exists()
    except OSError:
        return False


def ntsync_device_readable(device=None):
    """Whether this user may open the device, as the wineserver will.

    The wineserver opens it read-only and falls back when that fails, so a
    node created without read access for the player is no better than none.
    """
    path = Path(device) if device is not None else Path(NTSYNC_DEVICE)
    return os.access(path, os.R_OK)


def _env_flag(environ, name):
    source = os.environ if environ is None else environ
    return str(source.get(name, "")).strip().lower() not in _FALSY


def inproc_sync_disabled_by_env(environ=None):
    """Whether the user explicitly turned ntsync off."""
    return _env_flag(environ, "PROTON_NO_NTSYNC")


def fsync_disabled_by_env(environ=None):
    """Whether the user explicitly turned the fsync fallback off."""
    return _env_flag(environ, "PROTON_NO_FSYNC")


def _fsync_fallback_usable(engine_root, environ, futex_waitv):
    """Whether the engine's fsync fallback would carry the game."""
    if not engine_has_fsync_fallback(engine_root):
        return False
    if fsync_disabled_by_env(environ):
        return False
    supported = (kernel_supports_futex_waitv() if futex_waitv is None
                 else futex_waitv)
    # A kernel that cannot be asked is not reported as one lacking it.
    return supported is not False


def inproc_sync_problem(engine_root, device=None, environ=None,
                        kernel_release=None, futex_waitv=None):
    """Actionable message when no fast synchronization path is usable.

    Returns None when nothing is wrong or nothing can be determined, so a
    caller can treat any string as "worth telling the user about".
    ``futex_waitv`` overrides the kernel probe (True, False or None).
    """
    ntsync_off = inproc_sync_disabled_by_env(environ)
    engine_ok = engine_supports_inproc_sync(engine_root)
    present = kernel_exposes_ntsync(device)
    if engine_ok and not ntsync_off and present \
            and ntsync_device_readable(device):
        return None
    if _fsync_fallback_usable(engine_root, environ, futex_waitv):
        # Wine opens /dev/ntsync first and only then falls back, so this is
        # what runs whenever ntsync cannot.
        return None

    fallback = engine_has_fsync_fallback(engine_root)
    if fallback and fsync_disabled_by_env(environ):
        why_no_fallback = (
            " PROTON_NO_FSYNC is set as well, which turns off the engine's "
            "fsync fallback; clear it from the Advanced custom-environment "
            "field.")
    elif fallback:
        why_no_fallback = (
            " The engine's fsync fallback cannot help either: it needs the "
            "futex_waitv system call, which this kernel refuses (mainline "
            "Linux %s and later have it)." % FUTEX_WAITV_KERNEL)
    else:
        why_no_fallback = ""

    if ntsync_off:
        return (
            "PROTON_NO_NTSYNC is set, which turns off Wine's in-process "
            "synchronization (ntsync)."
            + (why_no_fallback or " This engine has no esync/fsync "
               "fallback.")
            + " Every wait becomes a wineserver round-trip and Minecraft's "
            "worker threads end up serialised — expect low frame rates, "
            "stuttering chunk generation and low server TPS. Clear it from "
            "the Advanced custom-environment field to get performance back."
        )

    if engine_ok is False:
        return (
            "The installed game engine was built without Wine's in-process "
            "synchronization (ntsync) backend, and this Wine has no "
            "esync/fsync fallback. Every wait becomes a wineserver "
            "round-trip, which serialises Minecraft's worker threads: low "
            "frame rates, stuttering chunk generation and low server TPS. "
            "Run Install / Update to fetch an engine built with it. "
            "Silence this notice with BOL_SKIP_NTSYNC_CHECK=1."
        )

    if engine_ok and present:
        return (
            "%s exists but this user cannot open it, so Wine cannot use "
            "its in-process synchronization." % NTSYNC_DEVICE
            + why_no_fallback
            + " Every wait falls back to a wineserver round-trip — expect "
            "low frame rates, stuttering chunk generation and low server "
            "TPS. Give it read access for everyone, as the kernel driver "
            "does, for example with the udev rule "
            "'KERNEL==\"ntsync\", MODE=\"0666\"'. "
            "Silence this notice with BOL_SKIP_NTSYNC_CHECK=1."
        )

    if engine_ok:
        release = (platform.release() if kernel_release is None
                   else kernel_release)
        return (
            "This kernel does not provide %s, so Wine cannot use its "
            "in-process synchronization." % NTSYNC_DEVICE
            + why_no_fallback
            + " Every wait falls back to a wineserver round-trip — expect "
            "low frame rates, stuttering chunk generation and low server "
            "TPS. The driver is in mainline Linux %s and later (some "
            "distributions backport it); running kernel is %s. If your "
            "kernel has it as a module, load it with 'sudo modprobe ntsync' "
            "and make that persistent. "
            "Silence this notice with BOL_SKIP_NTSYNC_CHECK=1."
            % (MAINLINE_KERNEL, release or "unknown")
        )

    return None


def inproc_sync_summary(engine_root, device=None, environ=None,
                        futex_waitv=None):
    """One short status word for Doctor's aligned report."""
    ntsync_off = inproc_sync_disabled_by_env(environ)
    engine_ok = engine_supports_inproc_sync(engine_root)
    present = kernel_exposes_ntsync(device)
    readable = present and ntsync_device_readable(device)
    if engine_ok and not ntsync_off and readable:
        return "OK (ntsync)"
    if _fsync_fallback_usable(engine_root, environ, futex_waitv):
        return "OK (fsync fallback: %s)" % (
            "PROTON_NO_NTSYNC" if ntsync_off
            else "%s not readable" % NTSYNC_DEVICE if present
            else "no %s" % NTSYNC_DEVICE)
    if ntsync_off:
        return "OFF (PROTON_NO_NTSYNC)"
    if engine_ok is None:
        return "unknown (engine not installed)"
    if not engine_ok:
        return "MISSING (engine built without ntsync)"
    if present:
        return "MISSING (%s not readable)" % NTSYNC_DEVICE
    return "MISSING (no %s)" % NTSYNC_DEVICE
