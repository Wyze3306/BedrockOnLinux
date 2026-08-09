"""bol.ntsync — report whether Wine's in-process synchronization is usable.

Wine 11 has no esync/fsync any more: its only fast synchronization path is the
kernel ntsync driver behind ``/dev/ntsync``.  When that path is unavailable
every Win32 wait, ``SetEvent``, mutex and semaphore becomes a wineserver
round-trip, and because the wineserver serialises those requests Minecraft's
worker threads queue behind one another.  The game then behaves as if it were
single-threaded: low server TPS, a starved GPU and chunk-generation stalls
(issues #63, #139, #143, #148, #150).

Two independent things must hold, and the difference matters to the user
because the remedies are completely different:

* the engine must have been *built* with the ntsync backend compiled in, and
* the host kernel must actually expose ``/dev/ntsync``.

Detection here is deliberately cheap and side-effect free — a byte search in
``wineserver`` plus a stat of the device node.  No Wine process is started and
no ioctl is attempted, so this can run before every launch.
"""
# SPDX-License-Identifier: MIT

import os
import platform
from pathlib import Path

# server/inproc_sync.c only compiles this literal in when the build found
# <linux/ntsync.h>; its absence is exactly the "compiled out to stubs" case.
_NTSYNC_DEVICE_MARKER = b"/dev/ntsync"

# Both are produced by one build, so either answers the question; the managed
# engine runs the WoW64 server, a custom Proton may ship only the classic one.
_WINESERVER_PATHS = ("files/bin-wow64/wineserver", "files/bin/wineserver")

NTSYNC_DEVICE = "/dev/ntsync"

# Mainline kernel that first shipped the driver. Distributions do backport it,
# so this is quoted as guidance only and never used to decide the verdict.
MAINLINE_KERNEL = "6.14"


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


def engine_supports_inproc_sync(engine_root):
    """Whether the engine was built with the ntsync backend compiled in.

    Returns True/False, or None when no wineserver could be read at all — an
    unreadable engine is reported as unknown rather than as a defect.
    """
    if not engine_root:
        return None
    root = Path(engine_root)
    verdict = None
    for relative in _WINESERVER_PATHS:
        found = _file_contains(root / relative, _NTSYNC_DEVICE_MARKER)
        if found is None:
            continue
        if found:
            return True
        verdict = False
    return verdict


def kernel_exposes_ntsync(device=None):
    """Whether the running kernel provides the ntsync character device."""
    path = Path(device) if device is not None else Path(NTSYNC_DEVICE)
    try:
        return path.exists()
    except OSError:
        return False


def inproc_sync_disabled_by_env(environ=None):
    """Whether the user explicitly turned the fast path off."""
    source = os.environ if environ is None else environ
    value = str(source.get("PROTON_NO_NTSYNC", "")).strip().lower()
    return value not in ("", "0", "no", "off", "false")


def inproc_sync_problem(engine_root, device=None, environ=None,
                        kernel_release=None):
    """Actionable message when the fast synchronization path is unusable.

    Returns None when nothing is wrong or nothing can be determined, so a
    caller can treat any string as "worth telling the user about".
    """
    if inproc_sync_disabled_by_env(environ):
        return (
            "PROTON_NO_NTSYNC is set, which turns off Wine's in-process "
            "synchronization. This engine has no esync/fsync fallback, so "
            "every wait becomes a wineserver round-trip and Minecraft's "
            "worker threads end up serialised — expect low frame rates, "
            "stuttering chunk generation and low server TPS. Clear it from "
            "the Advanced custom-environment field to get performance back."
        )

    engine_ok = engine_supports_inproc_sync(engine_root)
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

    if engine_ok and not kernel_exposes_ntsync(device):
        release = (platform.release() if kernel_release is None
                   else kernel_release)
        return (
            "This kernel does not provide %s, so Wine cannot use its "
            "in-process synchronization and falls back to wineserver "
            "round-trips — expect low frame rates, stuttering chunk "
            "generation and low server TPS. The driver is in mainline Linux "
            "%s and later (some distributions backport it); running kernel "
            "is %s. If your kernel has it as a module, load it with "
            "'sudo modprobe ntsync' and make that persistent. "
            "Silence this notice with BOL_SKIP_NTSYNC_CHECK=1."
            % (NTSYNC_DEVICE, MAINLINE_KERNEL, release or "unknown")
        )

    return None


def inproc_sync_summary(engine_root, device=None, environ=None):
    """One short status word for Doctor's aligned report."""
    if inproc_sync_disabled_by_env(environ):
        return "OFF (PROTON_NO_NTSYNC)"
    engine_ok = engine_supports_inproc_sync(engine_root)
    if engine_ok is None:
        return "unknown (engine not installed)"
    if not engine_ok:
        return "MANQUANT (engine built without ntsync)"
    if not kernel_exposes_ntsync(device):
        return "MANQUANT (no %s)" % NTSYNC_DEVICE
    return "OK (ntsync)"
