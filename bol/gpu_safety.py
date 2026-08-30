"""Fail-closed graphics checks which never open a GPU device.

The launcher must not call Vulkan/OpenGL merely to decide whether Vulkan is
safe: on a broken proprietary driver, that probe itself can panic the kernel.
Everything here is obtained from the existing X server, sysfs, or text logs.
"""
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from .config import GAMES, WINEGDK_BUILD_REV
from .log import BolError, die, warn
from .platform import IS_LINUX, IS_MAC
from .util import env_flag, launcher_command


try:
    # ``games`` is shared by isolated profiles. Following that link back to
    # its parent keeps the interlock global to every profile while preserving
    # the normal and relocated main-data roots.
    _GPU_STATE_ROOT = GAMES.resolve(strict=False).parent
except (OSError, RuntimeError) as exc:
    raise BolError(
        f"Cannot locate the shared GPU-safety state directory: {exc}"
    ) from exc

GPU_LAUNCH_MARKER = _GPU_STATE_ROOT / ".gpu-launch-in-progress.json"
GPU_SAFETY_ACK = _GPU_STATE_ROOT / ".gpu-safety-ack.json"
_STATE_VERSION = 2
_LEGACY_MARKER_VERSION = 1


@dataclass(frozen=True)
class GpuSafetyAcknowledgementStatus:
    code: str
    can_acknowledge: bool
    message: str
    marker_present: bool = False
    previous_boot_fault: bool = False


def acknowledge_gpu_crash_command() -> str:
    """The acknowledgement command line for the running installation."""
    return launcher_command("doctor", "--acknowledge-gpu-crash")


def _x11_session(env: Mapping[str, str]) -> bool:
    # Only Linux runs the game on an X server. A Mac with XQuartz installed
    # exports DISPLAY too, and answering "yes" there sends the RandR provider
    # check below looking for hardware providers on a display the game never
    # uses -- which finds none and blocks the launch outright.
    if not IS_LINUX:
        return False
    session = (env.get("XDG_SESSION_TYPE") or "").strip().lower()
    if session:
        return session == "x11"
    return bool(env.get("DISPLAY")) and not bool(env.get("WAYLAND_DISPLAY"))


def _gamescope_root_atoms(env: Mapping[str, str]) -> bool:
    """True when the X display's root window carries Gamescope's own atoms.

    Gamescope names every property it owns ``GAMESCOPE_*``, so the compositor
    identifies itself on the root window of the Xwayland server it runs. This
    is protocol only: it opens no GPU, exactly like the RandR probe beside it.

    Reading them matters because the environment is not a reliable witness. A
    Flatpak sandbox does not forward ``GAMESCOPE_WAYLAND_DISPLAY``, so in Steam
    Deck Game Mode the packaged launcher saw an ordinary X11 session reporting
    zero RandR providers and refused to start, while the AppImage on the same
    Deck was fine. Connecting an external monitor made a provider appear and
    was the only known way out (issue #127).
    """

    if not (env.get("DISPLAY") or "").strip():
        return False
    try:
        import ctypes

        xlib = ctypes.cdll.LoadLibrary("libX11.so.6")
        xlib.XOpenDisplay.restype = ctypes.c_void_p
        xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        xlib.XDefaultRootWindow.restype = ctypes.c_ulong
        xlib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        xlib.XListProperties.restype = ctypes.POINTER(ctypes.c_ulong)
        xlib.XListProperties.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_int)]
        xlib.XGetAtomName.restype = ctypes.c_void_p
        xlib.XGetAtomName.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        xlib.XFree.argtypes = [ctypes.c_void_p]
        xlib.XCloseDisplay.argtypes = [ctypes.c_void_p]
    except (OSError, AttributeError):
        return False

    display = xlib.XOpenDisplay(str(env["DISPLAY"]).encode())
    if not display:
        return False
    display = ctypes.c_void_p(display)
    try:
        count = ctypes.c_int(0)
        atoms = xlib.XListProperties(
            display, xlib.XDefaultRootWindow(display), ctypes.byref(count))
        if not atoms:
            return False
        try:
            for index in range(count.value):
                name = xlib.XGetAtomName(display, atoms[index])
                if not name:
                    continue
                try:
                    if ctypes.string_at(name).startswith(b"GAMESCOPE"):
                        return True
                finally:
                    xlib.XFree(ctypes.c_void_p(name))
        finally:
            xlib.XFree(ctypes.cast(atoms, ctypes.c_void_p))
    except Exception:
        return False
    finally:
        xlib.XCloseDisplay(display)
    return False


def _nested_gamescope_session(env: Mapping[str, str], atom_probe=None) -> bool:
    """True only for an explicitly identified nested Gamescope session.

    Gamescope's Xwayland server legitimately reports zero RandR providers even
    while the compositor is presenting through a healthy DRM/Vulkan device.
    Generic Steam or Steam Deck flags are deliberately insufficient: they can
    also be present on an ordinary direct-Xorg desktop, where zero providers
    remains a useful failure signal.

    The environment is checked first because it costs nothing; the root-window
    atoms are what still identify the session once a sandbox has dropped those
    variables.
    """

    if (env.get("GAMESCOPE_WAYLAND_DISPLAY") or "").strip():
        return True
    for key in ("XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP",
                "DESKTOP_SESSION"):
        value = (env.get(key) or "").strip().lower()
        parts = re.split(r"[:;,\s]+", value)
        if any(part == "gamescope" or part.startswith("gamescope-")
               for part in parts):
            return True
    probe = _gamescope_root_atoms if atom_probe is None else atom_probe
    return bool(probe(env))


def in_gamescope_session(environ: Optional[Mapping[str, str]] = None,
                         atom_probe=None) -> bool:
    """Whether this process is running inside a nested Gamescope session.

    Steam Game Mode is the case that matters to callers: it shows one
    application window at a time, so a launcher window there stands between
    Steam and the game.
    """
    env = os.environ if environ is None else environ
    return _nested_gamescope_session(env, atom_probe)


def _xrandr_provider_count(env: Mapping[str, str], runner=None) -> Optional[int]:
    binary = shutil.which("xrandr")
    # Supplying a runner is the unit-test seam; it must not depend on whether
    # the test host happens to install xrandr.
    if not binary and runner is not None:
        binary = "xrandr"
    if not binary:
        return None
    runner = runner or subprocess.run
    try:
        result = runner(
            [binary, "--listproviders"],
            env=dict(env),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=4,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    match = re.search(r"Providers:\s*number\s*:\s*(\d+)",
                      getattr(result, "stdout", ""), re.IGNORECASE)
    return int(match.group(1)) if match else None


def _boot_id() -> str:
    """A value that changes at every boot and at no other time.

    The interrupted-launch interlock is only as good as this: two boots that
    share an id make a marker from the previous one look like this one's, and
    an empty id makes every boot look identical. Linux publishes one directly;
    macOS has no ``/proc``, so the equivalent is the kernel's own boot
    timestamp, which is fixed for the life of the boot.
    """
    if IS_MAC:
        try:
            found = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.boottime"],
                                   capture_output=True, text=True, timeout=5,
                                   check=False)
        except (OSError, subprocess.SubprocessError):
            return ""
        return found.stdout.strip() if found.returncode == 0 else ""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def _sync_parent(path: Path) -> None:
    try:
        fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_state(path: Path) -> Optional[dict]:
    """Read a small regular JSON state file, rejecting links/oversized data."""

    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return None
        with path.open("r", encoding="utf-8") as stream:
            raw = stream.read(65_537)
    except (OSError, UnicodeError):
        return None
    if len(raw) > 65_536:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_state_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    staged = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(staged, 0o600)
        os.replace(staged, path)
        _sync_parent(path)
    finally:
        staged.unlink(missing_ok=True)


def _pid_alive(pid) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def interrupted_launch_problem(path: Optional[Path] = None) -> Optional[str]:
    """Describe a launch which never returned to the launcher.

    Presence is authoritative even if the JSON was torn by a power loss.  This
    is deliberately conservative: a stale marker is removed only by the
    explicit doctor acknowledgement after a reboot and inspection.
    """

    marker = Path(path) if path is not None else GPU_LAUNCH_MARKER
    command = acknowledge_gpu_crash_command()
    try:
        marker.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return (
            "the launcher cannot inspect its persistent GPU safety marker; "
            f"repair the data-directory permissions, then run '{command}'"
        )
    state = _read_state(marker)
    if not state:
        return (
            "an interrupted Minecraft launch marker exists but is unreadable; "
            f"after repairing the graphics driver and rebooting, run '{command}'"
        )
    if state.get("version") == _LEGACY_MARKER_VERSION:
        # Schema 1 predates the durable wrapper-return phase. It cannot prove
        # whether Wine merely crashed in userspace (as in issue #31) or the GPU
        # session contributed to a hard lock, so never delete it implicitly.
        same_boot = bool(_boot_id() and state.get("boot_id") == _boot_id())
        if same_boot and _pid_alive(state.get("launcher_pid")):
            return (
                "a legacy Minecraft GPU session is still marked active in "
                "this launcher; close or force-stop it instead of starting a "
                "second session"
            )
        when = ("during this boot" if same_boot
                else "before the last reboot/power loss")
        return (
            f"a legacy Minecraft GPU session did not return cleanly {when}; "
            "the old marker cannot distinguish a Wine crash from a graphics-"
            "driver failure, so after checking the driver and rebooting run "
            f"'{command}' once to acknowledge it"
        )
    if state.get("version") != _STATE_VERSION:
        return (
            "an interrupted Minecraft launch marker has an unsupported format; "
            f"after repairing the graphics driver and rebooting, run '{command}'"
        )
    same_boot = bool(_boot_id() and state.get("boot_id") == _boot_id())
    if same_boot and _pid_alive(state.get("launcher_pid")):
        return (
            "a Minecraft GPU session is still marked active in this launcher; "
            "close or force-stop it instead of starting a second session"
        )
    if same_boot:
        # The acknowledgement is deliberately refused for a marker created
        # during this boot, so do not advertise it as the immediate next step.
        return (
            "the previous Minecraft GPU session did not return cleanly during "
            "this boot; inspect why the session or machine stopped, then "
            "reboot — an interrupted launch from the running boot cannot be "
            f"acknowledged, and '{command}' only clears it after that reboot"
        )
    return (
        "the previous Minecraft GPU session did not return cleanly before the "
        "last reboot/power loss; inspect why the session or machine stopped "
        f"and reboot before retrying; if no current graphics fault remains, run "
        f"'{command}' to acknowledge the interrupted launch"
    )


def arm_gpu_launch(path: Optional[Path] = None) -> str:
    """Durably mark a GPU launch immediately before spawning its process."""

    marker = Path(path) if path is not None else GPU_LAUNCH_MARKER
    marker.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(16)
    payload = {
        "version": _STATE_VERSION,
        "engine_rev": WINEGDK_BUILD_REV,
        "phase": "running",
        "token": token,
        "boot_id": _boot_id(),
        "launcher_pid": os.getpid(),
        "created": int(time.time()),
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(marker, flags, 0o600)
    except FileExistsError as exc:
        raise BolError(
            "A previous Minecraft GPU launch is still marked interrupted. "
            "After inspecting the interrupted session and rebooting, run "
            f"'{acknowledge_gpu_crash_command()}' if no current graphics "
            "fault remains."
        ) from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(marker, 0o600)
        _sync_parent(marker)
    except Exception:
        marker.unlink(missing_ok=True)
        raise
    return token


def disarm_gpu_launch(token: str, path: Optional[Path] = None) -> bool:
    """Clear only the marker owned by a GPU process which returned normally."""

    marker = Path(path) if path is not None else GPU_LAUNCH_MARKER
    state = _read_state(marker)
    if not state or not secrets.compare_digest(str(state.get("token", "")), token):
        return False
    try:
        marker.unlink()
    except OSError:
        return False
    _sync_parent(marker)
    return True


def mark_gpu_wrapper_returned(
        token: str, path: Optional[Path] = None) -> bool:
    """Persist that UMU returned before checking Wine's final teardown."""

    marker = Path(path) if path is not None else GPU_LAUNCH_MARKER
    state = _read_state(marker)
    if (not state or state.get("version") != _STATE_VERSION
            or state.get("phase") != "running"
            or not secrets.compare_digest(str(state.get("token", "")), token)):
        return False
    payload = dict(state)
    payload["phase"] = "wrapper_returned"
    payload["wrapper_returned"] = int(time.time())
    try:
        _write_state_atomic(marker, payload)
    except OSError:
        return False
    return True


def retire_idle_current_boot_marker(path: Optional[Path] = None) -> bool:
    """Clear an orphan marker only after the caller proved the prefix idle.

    UMU can return just before Wine's last helper or wineserver finishes its
    normal shutdown.  If that exceeds the launcher's grace period, retaining
    the marker is correct while those processes are live but must not turn
    into a permanent same-boot block after the prefix has stopped. The launch
    lock and idle-prefix check are owned by the caller. Only a marker which
    durably records that the wrapper returned is eligible; a marker left while
    Minecraft was running, an old-boot marker, malformed state, and token
    races remain untouched. Kernel, journal, and display-provider checks still
    run immediately after this recovery.
    """

    marker = Path(path) if path is not None else GPU_LAUNCH_MARKER
    state = _read_state(marker)
    expected_fields = {
        "version", "engine_rev", "phase", "token", "boot_id",
        "launcher_pid", "created", "wrapper_returned",
    }
    if (not state or state.get("version") != _STATE_VERSION
            or state.get("phase") != "wrapper_returned"
            or set(state) != expected_fields):
        return False
    boot = _boot_id()
    if not boot or state.get("boot_id") != boot:
        return False
    pid = state.get("launcher_pid")
    created = state.get("created")
    wrapper_returned = state.get("wrapper_returned")
    engine_rev = state.get("engine_rev")
    token = state.get("token")
    if (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1
            or isinstance(created, bool) or not isinstance(created, int)
            or created < 0 or not isinstance(engine_rev, str)
            or isinstance(wrapper_returned, bool)
            or not isinstance(wrapper_returned, int)
            or wrapper_returned < created
            or not engine_rev or not isinstance(token, str)
            or not re.fullmatch(r"[0-9a-f]{32}", token)):
        return False
    if not disarm_gpu_launch(token, marker):
        return False
    warn(
        "Removed an orphaned current-boot GPU marker after confirming the "
        "Wine prefix is idle; kernel and display-server safety checks still "
        "apply."
    )
    return True


def _acknowledgement_marker_scope(marker: Path) -> str:
    """Classify a marker as none/previous/current/active/unknown."""

    try:
        marker.lstat()
    except FileNotFoundError:
        return "none"
    except OSError:
        return "unknown"
    state = _read_state(marker)
    boot = _boot_id()
    marker_boot = state.get("boot_id") if state else None
    if not boot or not isinstance(marker_boot, str) or not marker_boot:
        return "unknown"
    if marker_boot != boot:
        return "previous"
    if _pid_alive(state.get("launcher_pid")):
        return "active"
    return "current"


def acknowledge_gpu_safety_incident(
        marker_path: Optional[Path] = None,
        ack_path: Optional[Path] = None,
        journal_runner=None) -> GpuSafetyAcknowledgementStatus:
    """Acknowledge one verified previous-boot incident, never a blank state.

    The caller must serialize this with PLAY (the doctor command uses the same
    launch lock). Eligibility is re-evaluated immediately before the durable
    write, so a current-boot launch marker or kernel fault cannot be cleared.
    """

    marker = Path(marker_path) if marker_path is not None else GPU_LAUNCH_MARKER
    ack = Path(ack_path) if ack_path is not None else GPU_SAFETY_ACK
    status = gpu_safety_acknowledgement_status(marker, journal_runner)
    if not status.can_acknowledge:
        raise BolError(status.message)
    _write_state_atomic(ack, {
        "version": _STATE_VERSION,
        "boot_id": _boot_id(),
        "acknowledged": int(time.time()),
        "marker": status.marker_present,
        "previous_boot_fault": status.previous_boot_fault,
    })
    if status.marker_present:
        try:
            marker.unlink()
        except OSError:
            # Leaving the marker in place remains fail-closed. The caller can
            # report the filesystem error instead of pretending it was clear.
            raise BolError(
                "The previous-boot GPU incident was acknowledged, but its "
                "interrupted-launch marker could not be removed."
            )
        _sync_parent(marker)
    return status


def _previous_boot_fault_acknowledged(path: Optional[Path] = None) -> bool:
    ack = Path(path) if path is not None else GPU_SAFETY_ACK
    state = _read_state(ack)
    boot = _boot_id()
    return bool(boot and state and state.get("version") == _STATE_VERSION
                and state.get("boot_id") == boot
                and state.get("previous_boot_fault") is True)


def _xorg_software_fallback() -> bool:
    """Best-effort confirmation when RandR is unavailable.

    Require several independent markers to avoid rejecting a healthy session
    because an old log merely mentioned fbdev during driver discovery.
    """

    candidates = (
        Path.home() / ".local/share/xorg/Xorg.0.log",
        Path("/var/log/Xorg.0.log"),
    )
    for path in candidates:
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        if ("FBDEV(0):" in text
                and "DRISWRAST GL provider" in text
                and "Failed to open DRM device" in text):
            return True
    return False


def _nvidia_device_with_mesa_glx() -> bool:
    """Recognise Debian/Mint's particularly dangerous split-driver state."""

    nvidia = False
    for vendor_path in Path("/sys/class/drm").glob("card*/device/vendor"):
        try:
            vendor = vendor_path.read_text().strip().lower()
            driver = vendor_path.parent.joinpath("driver").resolve().name
        except OSError:
            continue
        if vendor == "0x10de" and driver == "nvidia":
            nvidia = True
            break
    if not nvidia:
        return False
    try:
        target = os.readlink("/etc/alternatives/glx")
    except OSError:
        return False
    return "mesa-diverted" in target


def _gpu_fault_in_text(text: str) -> bool:
    """Recognise a kernel GPU failure without conflating unrelated messages."""

    lines = text.lower().splitlines()
    direct = (
        re.compile(r"\bnvrm:.*gpu has fallen off the bus"),
        re.compile(r"\bnvrm:.*\bxid\b.*\b(?:79|119|120)\b"),
        re.compile(r"\bamdgpu\b.*(?:gpu reset begin|ring\s+\S+\s+timeout|"
                   r"asic reset failed|gpu fault)"),
        re.compile(r"\b(?:i915|xe)\b.*(?:gpu hang|wedged|reset.*failed)"),
    )
    if any(pattern.search(line) for line in lines for pattern in direct):
        return True

    # Kernel oops/lockup headers and the responsible module are frequently on
    # different stack-trace lines. Correlate them in a small neighbourhood;
    # seeing routine "amdgpu" boot chatter and an unrelated network-driver oops
    # somewhere else in a large journal must not blame the GPU.
    generic = re.compile(
        r"bug:\s*kernel null pointer dereference|"
        r"watchdog.*(?:hard|soft)\s+lockup|"
        r"kernel panic|general protection fault"
    )
    vendor = re.compile(r"\[nvidia\]|\bnvrm:|\bamdgpu\b|\bi915\b|\bxe\b")
    for index, line in enumerate(lines):
        if not generic.search(line):
            continue
        lo, hi = max(0, index - 40), min(len(lines), index + 41)
        if any(vendor.search(candidate) for candidate in lines[lo:hi]):
            return True
    return False


def _kernel_journal_text(binary: str, runner, boot: int) -> Optional[str]:
    args = [binary, "-k", "-b", str(boot), "--no-pager", "-o", "cat"]
    # Keep the tail for the whole selected boot. A current-boot fault must not
    # become launchable merely because the user waited fifteen minutes, and a
    # relative --since value would not refer to the selected previous boot.
    args += ["-n", "5000"]
    try:
        result = runner(
            args,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    return (getattr(result, "stdout", "") + "\n"
            + getattr(result, "stderr", ""))[-2_000_000:]


def _kernel_driver_fault_scope(runner=None) -> Optional[str]:
    """Return ``current``/``previous`` for an unacknowledged kernel fault."""

    # The whole check reads the Linux kernel journal for amdgpu/i915/nouveau
    # resets. macOS has no such log and no such failure mode to guard against:
    # Metal does not leave a wedged device for the next process to walk into.
    if not IS_LINUX and runner is None:
        return None
    binary = shutil.which("journalctl")
    if not binary and runner is not None:
        binary = "journalctl"
    if not binary:
        return None
    runner = runner or subprocess.run
    current = _kernel_journal_text(binary, runner, 0)
    if current is not None and _gpu_fault_in_text(current):
        return "current"
    if _previous_boot_fault_acknowledged():
        return None
    previous = _kernel_journal_text(binary, runner, -1)
    if previous is not None and _gpu_fault_in_text(previous):
        return "previous"
    return None


def gpu_safety_acknowledgement_status(
        marker_path: Optional[Path] = None,
        journal_runner=None) -> GpuSafetyAcknowledgementStatus:
    """Return whether an explicit previous-boot acknowledgement is valid.

    This read-only helper is suitable for deciding whether a GUI should offer
    an acknowledgement action. The mutating function always rechecks it under
    the launch lock, so callers must not treat a previously returned value as
    authorization to delete state.
    """

    marker = Path(marker_path) if marker_path is not None else GPU_LAUNCH_MARKER
    marker_scope = _acknowledgement_marker_scope(marker)
    if marker_scope == "active":
        return GpuSafetyAcknowledgementStatus(
            "active-launch", False,
            "The marked Minecraft launch is still active; stop it before "
            "acknowledging a GPU safety incident.",
            marker_present=True,
        )
    if marker_scope == "current":
        return GpuSafetyAcknowledgementStatus(
            "current-boot-launch", False,
            "The interrupted Minecraft launch happened during this boot. "
            "Inspect the graphics driver and reboot before acknowledging it.",
            marker_present=True,
        )
    if marker_scope == "unknown":
        return GpuSafetyAcknowledgementStatus(
            "unreadable-marker", False,
            "The interrupted-launch marker cannot prove that it belongs to a "
            "previous boot; it cannot be acknowledged safely.",
            marker_present=True,
        )

    fault_scope = _kernel_driver_fault_scope(journal_runner)
    if fault_scope == "current":
        return GpuSafetyAcknowledgementStatus(
            "current-boot-fault", False,
            "The graphics driver reported a fatal fault during this boot. "
            "Reboot before acknowledging any previous incident.",
            marker_present=marker_scope == "previous",
        )

    previous_marker = marker_scope == "previous"
    previous_fault = fault_scope == "previous"
    if previous_marker or previous_fault:
        if not _boot_id():
            return GpuSafetyAcknowledgementStatus(
                "boot-id-unavailable", False,
                "The current boot cannot be identified, so a previous GPU "
                "incident cannot be acknowledged safely.",
                marker_present=previous_marker,
                previous_boot_fault=previous_fault,
            )
        details = []
        if previous_marker:
            details.append("an interrupted Minecraft launch")
        if previous_fault:
            details.append("a fatal graphics-driver event")
        return GpuSafetyAcknowledgementStatus(
            "previous-boot-incident", True,
            "Previous-boot GPU incident available to acknowledge: "
            + " and ".join(details) + ".",
            marker_present=previous_marker,
            previous_boot_fault=previous_fault,
        )

    return GpuSafetyAcknowledgementStatus(
        "none", False,
        "No previous-boot GPU safety incident is available to acknowledge.",
    )


def graphics_safety_problem(
        environ: Optional[Mapping[str, str]] = None,
        xrandr_runner=None,
        journal_runner=None,
        atom_probe=None) -> Optional[str]:
    """Return an actionable reason to refuse launch, or ``None``.

    No Vulkan, OpenGL, ``nvidia-smi`` or DRM ioctl is performed here.
    """

    env = os.environ if environ is None else environ
    interrupted = interrupted_launch_problem()
    if interrupted:
        return interrupted
    fault_scope = _kernel_driver_fault_scope(journal_runner)
    if fault_scope == "current":
        return (
            "the graphics driver has already reported a fatal kernel fault "
            "during this boot; reboot before starting another GPU process"
        )
    if fault_scope == "previous":
        return (
            "the graphics driver reported a fatal kernel fault before the last "
            "reboot; after repairing/updating the driver, acknowledge it with "
            f"'{acknowledge_gpu_crash_command()}'"
        )
    if _x11_session(env):
        providers = _xrandr_provider_count(env, xrandr_runner)
        if providers == 0:
            if _nested_gamescope_session(env, atom_probe):
                return None
            problem = (
                "the X11 session exposes zero RandR GPU providers and is "
                "running on a fallback framebuffer/software renderer"
            )
            if _nvidia_device_with_mesa_glx():
                problem += (
                    "; the NVIDIA kernel device is loaded while Debian's GLX "
                    "alternative points to mesa-diverted"
                )
            return problem
        if providers is None:
            if _xorg_software_fallback():
                problem = (
                    "Xorg failed to open its DRM device and fell back to "
                    "FBDEV/software rendering"
                )
                if _nvidia_device_with_mesa_glx():
                    problem += (
                        "; the NVIDIA kernel device is loaded while Debian's "
                        "GLX alternative points to mesa-diverted"
                    )
                return problem
            return (
                "the launcher could not verify any X11 hardware provider "
                "through RandR without opening the GPU"
            )
    return None


def require_safe_graphics_session(
        environ: Optional[Mapping[str, str]] = None) -> None:
    """Refuse a launch which could turn a known driver fault into a hard lock."""

    env = os.environ if environ is None else environ
    problem = graphics_safety_problem(env)
    if not problem:
        return
    if env_flag(env.get("BOL_ALLOW_UNSAFE_GPU")):
        warn("BOL_ALLOW_UNSAFE_GPU=1 bypasses the graphics safety block: "
             + problem + ".")
        return
    die("Unsafe graphics session: " + problem + ". BedrockOnLinux did not "
        "start Wine, Vulkan, or Minecraft. Repair/reinstall the host GPU "
        "driver, ensure the desktop uses the hardware DRM provider, then "
        "reboot. Advanced override (at your own risk): "
        "BOL_ALLOW_UNSAFE_GPU=1.")
