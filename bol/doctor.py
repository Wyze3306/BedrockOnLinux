"""bol.doctor — environment health checks."""
# SPDX-License-Identifier: MIT

import shutil
import sys
from pathlib import Path

from . import deps, discord, fixups, gamepad, presence, webview
from .config import DATA, PRETTY, VERSION
from .gpu_safety import (
    GpuSafetyAcknowledgementStatus,
    acknowledge_gpu_safety_incident,
    graphics_safety_problem,
    gpu_safety_acknowledgement_status,
)
from .log import BolError, info, ok, warn
from .ntsync import inproc_sync_problem, inproc_sync_summary
from .perfcheck import performance_problems, performance_summary
from .platform import IS_MAC, NOT_APPLICABLE, os_name, pm_hint
from .raytracing import ray_tracing_problem, ray_tracing_summary
from .util import custom_env_map, load_settings
from .waylanddrv import wayland_driver_summary


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


def gui_toolkit_summary():
    """One line about the Qt toolkit: present, absent, or present and unable
    to load.

    Installed is not the same as usable. Qt loads its own shared libraries
    when PySide6 is imported, so a host missing one of them — libzstd.so.1 on
    NixOS, issue #205 — has the toolkit right there on the path and still
    cannot open a window. Import it here, and name what the loader could not
    find rather than reporting it as ready.
    """
    if not deps.have("PySide6"):
        return "auto-installed on launch"
    error = deps.gui_import_error()
    if not error:
        return "OK (GUI)"
    library = deps.missing_shared_library(error)
    if library:
        return (f"installed, but Qt cannot load: this system has no "
                f"{library}")
    return f"installed, but Qt cannot load: {error}"


def doctor(acknowledge_gpu_crash=False):
    if acknowledge_gpu_crash and not _acknowledge_gpu_crash():
        return False
    info(f"{PRETTY} {VERSION} — system check ({os_name()})")
    hint = pm_hint()
    miss = []
    print(f"  {'python3':12} : {sys.version.split()[0]}")
    # zstd is only needed to unpack the Linux engine and payloads; a Mac that
    # never downloads them should not be told to install it.
    tools = (("tar", "tar"), ("curl", "curl"))
    if not IS_MAC:
        tools += (("unzstd", "zstd"),)
    for tool, pkg in tools:
        have = shutil.which(tool)
        print(f"  {tool:12} : {'OK' if have else 'MANQUANT'}")
        if not have and not (tool == "curl" and shutil.which("wget")):
            miss.append(pkg)
    # The GUI toolkit is PySide6 (Qt), not Tk/customtkinter anymore. It is not
    # named in `miss`: bol.deps.ensure_gui_deps() pip-installs the pinned
    # PySide6-Essentials wheel on the first GUI launch, exactly as it did for
    # customtkinter, so a portable .pyz or a bare checkout without it is ready
    # even though the import is not there yet. Reporting it as missing would
    # also have to name a package -- and there is no `python3-pyside6` to
    # install on Debian or Ubuntu, where it is split per Qt module
    # (python3-pyside6.qtcore, .qtgui, .qtwidgets).
    if IS_MAC:
        # The single most important line on a Mac: which Windows runtime is
        # going to run the game, since the launcher installs none of them.
        from . import winemac

        print(f"  {'wine':12} : {winemac.summary()}")
        wine_problem = winemac.problem()
        if wine_problem:
            warn(wine_problem)
    print(f"  {'PySide6':12} : {gui_toolkit_summary()}")
    cr_ok = deps.have("cryptography")
    print(f"  {'cryptography':12} : "
          f"{'OK (login)' if cr_ok else 'MANQUANT (login)'}")
    if not cr_ok:
        miss.append("python3-cryptography")
    # Minecraft is downloaded from the Microsoft Store, and xodus-cli opens
    # that sign-in in an embedded WebKitGTK webview. Without the library it
    # cannot even start -- not the download and not an installed game, whose
    # executable stays encrypted -- so name it here rather than at the first
    # download. Hosts that cannot install it use the bundled runtime instead.
    if IS_MAC:
        print(f"  {'webkit2gtk':12} : {NOT_APPLICABLE}")
    else:
        webkit_summary, webkit_package = webview.status()
        print(f"  {'webkit2gtk':12} : {webkit_summary}")
        if webkit_package:
            miss.append(webkit_package)
    # Whether that sign-in is on file, and where. Never a missing dependency:
    # it is linked from the launcher, not installed. It is printed with its
    # path because losing it is expensive -- each fresh sign-in claims one of
    # the account's ten Microsoft Store devices (issue #198) -- so a report
    # that "it asks me to sign in every time" can be answered from here
    # instead of from inside the sandbox.
    from . import xodus

    if IS_MAC:
        print(f"  {'store acct':12} : {NOT_APPLICABLE} — the Microsoft Store "
              "downloader is Linux-only")
    else:
        print(f"  {'store acct':12} : "
              + ("linked" if xodus.signed_in() else "not linked")
              + f" ({xodus.XODUS_KEYRING})")
    # And whether the installed build can still be decrypted. A Store package
    # keeps the game executable as ciphertext and the only copy of the plain
    # one is made, at every launch, out of the package file beside it -- so a
    # game directory that lost it is unplayable while looking complete.
    game_dir = (load_settings().get("game_dir") or "").strip()
    if game_dir and xodus.exe_is_encrypted(
            Path(game_dir) / "Minecraft.Windows.exe"):
        if IS_MAC:
            # Not a repairable state on a Mac: decrypting it needs the
            # Linux-only downloader, so the answer is a different folder.
            print(f"  {'mc package':12} : ENCRYPTED — this build is a "
                  "Microsoft Store package and macOS cannot decrypt it")
            warn(xodus.MAC_UNSUPPORTED)
        else:
            print(f"  {'mc package':12} : "
                  + ("OK" if not xodus.lost_package_cache(game_dir) else
                     f"MISSING ({xodus.PACKAGE_CACHE} in {game_dir}) — "
                     "press PLAY to download this build again"))
    # Whether the installed build still carries the two HBUI patches that
    # keep the Servers tab reachable and hide the in-game Sign-in link. They
    # fail silently by nature -- a Minecraft build rebundles its minified UI
    # and the anchors stop matching -- and nothing else notices: the game
    # starts, setup reports success, and only the Play screen behaves as
    # though no patch had run (#227/#228/#229).
    signin_summary, signin_problem = fixups.hbui_signin_gate_status(game_dir)
    print(f"  {'sign-in ui':12} : {signin_summary}")
    if signin_problem:
        warn(signin_problem)
    # Which controllers the launcher window itself can be driven with. The
    # game reads its own pad through GameInput inside the prefix, so a blank
    # here says nothing about playing -- only about navigating the launcher
    # with no mouse, which is how Steam Game Mode reaches PLAY.
    print(f"  {'controller':12} : "
          + (NOT_APPLICABLE if IS_MAC else gamepad.summary()))
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
    if IS_MAC:
        # ntsync is a Linux kernel driver. The macOS backends have their own
        # fast path -- msync -- and bol.winemac asks for it at every launch.
        print(f"  {'fast sync':12} : "
              "msync/esync requested for the macOS backend")
    else:
        print(f"  {'fast sync':12} : "
              f"{inproc_sync_summary(engine, environ=custom)}")
        sync_problem = inproc_sync_problem(engine, environ=custom)
        if sync_problem:
            warn(sync_problem)
    # Whether BOL_INPUT=wayland has a driver it can actually use. Reported
    # without a warning: the launcher runs on XWayland by default, so an
    # unusable native driver is not a problem for anyone who never asks for
    # it — but it is the first thing to look at for anyone who did (#180).
    print(f"  {'wayland drv':12} : "
          + (NOT_APPLICABLE if IS_MAC else wayland_driver_summary(engine)))
    # What the graphics payload granted the game last time it ran: the tier
    # Minecraft's Ray Traced mode is gated on, read back from the launch log
    # rather than measured here, since answering it live would mean opening
    # the GPU device this check exists to avoid (#153).
    if IS_MAC:
        # The tier is read back out of vkd3d-proton's own log, and macOS runs
        # no vkd3d: Direct3D 12 goes through the backend's Metal layer.
        print(f"  {'ray tracing':12} : {NOT_APPLICABLE}")
    else:
        print(f"  {'ray tracing':12} : {ray_tracing_summary()}")
        rt_problem = ray_tracing_problem()
        if rt_problem:
            warn(rt_problem)
    # The same "it lags" report, from causes outside the engine entirely:
    # no memory, no disk, windowed vsync, a render distance past the main
    # thread. Prefix-scoped, so it is imported next to the other Wine module.
    from .prefix import active_prefix

    prefix = active_prefix()
    print(f"  {'performance':12} : {performance_summary(prefix, DATA)}")
    for perf_problem in performance_problems(prefix, DATA):
        warn(perf_problem)
    # What Discord shows while you play. Nothing here can keep the game from
    # starting, but "my friends do not see it" has three separate answers --
    # switched off, Discord not running, or a build with no application id --
    # and only this line tells them apart.
    print(f"  {'discord':12} : {discord.presence_summary(load_settings())}")
    # And the half of "my friends do not see me" that is not Discord at all:
    # under Wine nothing in the game publishes Xbox Live presence, so the
    # launcher does it, and this says whether it will (#238, #243).
    if IS_MAC:
        # The launcher publishes presence for the game because nothing in the
        # game does it under Wine -- but on macOS the account never reaches
        # the game at all, so a play session published here would say someone
        # is online in a session no one can join.
        print(f"  {'xbox friends':12} : {NOT_APPLICABLE}")
    else:
        print(f"  {'xbox friends':12} : "
              f"{presence.presence_summary(load_settings())}")
        presence_problem = presence.presence_problem(load_settings())
        if presence_problem:
            warn(presence_problem)
    if IS_MAC:
        # Said last, because it is the thing to know before filing a bug: the
        # online half of this launcher is the WineGDK engine, and there is no
        # macOS build of it.
        info("On macOS the game runs offline and on the LAN. Realms, servers, "
             "the Marketplace and Xbox friends need the WineGDK engine, which "
             "is built for Linux only.")
    if miss:
        warn("To install: " + hint.format(" ".join(sorted(set(miss)))))
        return False
    if gpu_problem:
        return False
    ok("System ready.")
    return True
