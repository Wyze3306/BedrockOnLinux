"""bol.doctor — environment health checks."""
# SPDX-License-Identifier: MIT

import shutil
import sys

from . import deps
from .config import PRETTY, VERSION
from .gpu_safety import (
    GpuSafetyAcknowledgementStatus,
    acknowledge_gpu_safety_incident,
    graphics_safety_problem,
    gpu_safety_acknowledgement_status,
)
from .log import BolError, info, ok, warn
from .ntsync import inproc_sync_problem, inproc_sync_summary
from .util import custom_env_map, load_settings


def gpu_crash_acknowledgement_status():
    """Return structured acknowledgement state for CLI or GUI callers."""

    # Import lazily to keep the ordinary doctor lightweight and avoid making
    # GPU safety state depend on the Wine/UMU modules at import time.
    from .prefix import active_prefix, prefix_processes

    running = prefix_processes(active_prefix())
    if running:
        return GpuSafetyAcknowledgementStatus(
            "active-prefix", False,
            "BedrockOnLinux still has "
            f"{len(running)} Wine/UMU process(es). Force-stop them before "
            "acknowledging GPU safety.",
        )
    return gpu_safety_acknowledgement_status()


def _acknowledge_gpu_crash():
    """Clear an interrupted-launch block only while PLAY is fully idle."""

    from .prefix import active_prefix, launch_lock, prefix_processes

    try:
        with launch_lock():
            running = prefix_processes(active_prefix())
            if running:
                warn(
                    "Cannot acknowledge GPU safety while BedrockOnLinux still "
                    f"has {len(running)} Wine/UMU process(es). Force-stop them "
                    "first."
                )
                return False
            status = acknowledge_gpu_safety_incident()
    except BolError as exc:
        warn(str(exc))
        return False
    details = []
    if status.marker_present:
        details.append("interrupted-launch marker cleared")
    if status.previous_boot_fault:
        details.append("previous-boot driver fault acknowledged")
    warn("GPU safety incident explicitly acknowledged for the current boot"
         + ("; " + ", ".join(details) + "." if details else "."))
    return True


def acknowledge_gpu_crash():
    """Public UI/CLI entry point for the guarded acknowledgement action."""
    return _acknowledge_gpu_crash()


def doctor(acknowledge_gpu_crash=False):
    if acknowledge_gpu_crash and not _acknowledge_gpu_crash():
        return False
    info(f"{PRETTY} {VERSION} — system check")
    hint = next((h for pm, h in (
        ("apt-get", "sudo apt install {}"), ("dnf", "sudo dnf install {}"),
        ("pacman", "sudo pacman -S {}"), ("zypper", "sudo zypper in {}"))
        if shutil.which(pm)), "installe : {}")
    miss = []
    print(f"  {'python3':12} : {sys.version.split()[0]}")
    for tool, pkg in (("tar", "tar"), ("curl", "curl"), ("unzstd", "zstd")):
        have = shutil.which(tool)
        print(f"  {tool:12} : {'OK' if have else 'MANQUANT'}")
        if not have and not (tool == "curl" and shutil.which("wget")):
            miss.append(pkg)
    tk_ok = deps.have("tkinter")
    print(f"  {'tkinter':12} : {'OK (GUI)' if tk_ok else 'MANQUANT (GUI)'}")
    if not tk_ok:
        miss.append("python3-tk")
    ctk_ok = deps.have("customtkinter")
    print(f"  {'customtkinter':12} : "
          f"{'OK (GUI)' if ctk_ok else 'auto-installed on launch'}")
    cr_ok = deps.have("cryptography")
    print(f"  {'cryptography':12} : "
          f"{'OK (login)' if cr_ok else 'MANQUANT (login)'}")
    if not cr_ok:
        miss.append("python3-cryptography")
    gpu_problem = graphics_safety_problem()
    print(f"  {'graphics':12} : "
          f"{'BLOQUÉ' if gpu_problem else 'OK (no unsafe state found)'}")
    if gpu_problem:
        warn("Unsafe graphics session: " + gpu_problem + ". Repair the host "
             "GPU driver and reboot; no Vulkan probe was attempted.")
    # Wine 11 has no esync/fsync; without ntsync every wait is a wineserver
    # round-trip and the game behaves as if it were single-threaded. Import
    # lazily so the ordinary doctor keeps not depending on the Wine modules.
    from .proton import proton_path

    engine = proton_path()
    # Launch drops an inherited PROTON_NO_NTSYNC, so only the Advanced
    # custom-environment field can really disable the fast path; report on
    # the same basis rather than on this shell's environment.
    custom = custom_env_map(load_settings().get("custom_env") or "")
    print(f"  {'fast sync':12} : {inproc_sync_summary(engine, environ=custom)}")
    sync_problem = inproc_sync_problem(engine, environ=custom)
    if sync_problem:
        warn(sync_problem)
    if miss:
        warn("To install: " + hint.format(" ".join(sorted(set(miss)))))
        return False
    if gpu_problem:
        return False
    ok("System ready.")
    return True
