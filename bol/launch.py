"""bol.launch — launching Minecraft through Proton/umu."""
# SPDX-License-Identifier: MIT

import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from .auth import (
    account_epoch_is_current,
    msa_refresh,
    msa_save_for_account_epoch,
    msa_session_snapshot,
    wine_apply_winegdk_prereqs,
    wine_reg_set_refresh_token,
    xbl_preauth,
    xbl_preauth_diagnostic,
    xbl_preauth_error_message,
)
from .config import CONTENT, DATA, HOME, LOGS, WINEGDK_BUILD_REV
from .deps import ensure_login_deps
from .dgc import dgc_warning_message, intel_dgpus_on_legacy_driver
from .fixups import _install_cryptbase_in_prefix, bump_stack_reserve
from .gameinput import install_gameinput
from .gamesetup import diagnose
from .gpu_safety import (
    acknowledge_gpu_crash_command,
    arm_gpu_launch,
    disarm_gpu_launch,
    mark_gpu_wrapper_returned,
    require_safe_graphics_session,
    retire_idle_current_boot_marker,
)
from .log import BolError, die, info, ok, warn
from .ntsync import inproc_sync_problem
from .prefix import (
    active_prefix,
    boot_prefix,
    launch_lock,
    patch_options,
    prefix_processes,
    proton_umu_cmd,
)
from .proton import custom_proton, patch_proton, proton_path
from .util import (
    _screen_wh,
    apply_custom_env,
    custom_env_map,
    LAUNCHER_OWNED_ENV,
    LAUNCHER_OWNED_ENV_ALTERNATIVE,
    launcher_owned_overrides,
    load_settings,
)
from .vkd3d import prepare_universal_vkd3d
from .winegdk import ensure_winegdk


_SONY_STEAM_INPUT_HIDRAW_IDS = ",".join((
    "0x054C/0x05C4",  # DualShock 4
    "0x054C/0x09CC",  # DualShock 4 v2
    "0x054C/0x0BA0",  # DualShock 4 wireless adapter
    "0x054C/0x0CE6",  # DualSense
    "0x054C/0x0DF2",  # DualSense Edge
))


def _prepare_graphics_engine():
    """Activate the universal DGC pair without opening Vulkan in the launcher."""
    if custom_proton():
        return None
    try:
        variant, changed = prepare_universal_vkd3d(
            proton_path(), WINEGDK_BUILD_REV)
    except BolError as exc:
        die(str(exc))
    info(f"Graphics command path: {variant}"
         + (" (activated)." if changed else " (already active)."))
    return variant


def _require_vkd3d_config(env, option):
    """Add one vkd3d option without discarding user-provided options."""
    options = [item.strip() for item in
               env.get("VKD3D_CONFIG", "").replace(";", ",").split(",")
               if item.strip()]
    if option not in options:
        options.append(option)
    env["VKD3D_CONFIG"] = ",".join(options)


def _steam_input_available(environ=None):
    """Whether Steam handed an actual virtual controller to this launch."""
    source = os.environ if environ is None else environ
    # Steam app IDs do not prove that Steam Input is enabled for the shortcut.
    for name in ("SteamVirtualGamepadInfo_Proton",
                 "SteamVirtualGamepadInfo"):
        if str(source.get(name, "")).strip():
            return True
    return False


def _is_steam_deck(environ=None, product_name_path=None):
    """Detect Steam Deck without running a graphics or hardware probe."""
    source = os.environ if environ is None else environ
    if str(source.get("SteamDeck", "")).strip() == "1":
        return True
    product = (Path(product_name_path) if product_name_path is not None
               else Path("/sys/devices/virtual/dmi/id/product_name"))
    try:
        return product.read_text(errors="ignore").strip().lower() in {
            "jupiter", "galileo",
        }
    except OSError:
        return False


def _warn_custom_env_overrides(custom_env):
    """Name the launcher settings the Advanced field is overriding.

    That field is applied last and keeps the final word by design, so this
    never blocks or rewrites it. It only makes the override visible: an
    unsupported value there crashes the game at every launch with nothing
    pointing at the field, and the reporter of issue #134 wiped their whole
    installation three times before connecting the two.
    """
    for key in launcher_owned_overrides(custom_env):
        alternative = LAUNCHER_OWNED_ENV_ALTERNATIVE.get(key)
        warn("Custom environment variable %s overrides what Settings "
             "configures%s. If the game crashes or misbehaves, clear it from "
             "the Advanced custom-environment field before reinstalling "
             "anything."
             % (key,
                "; the supported control is " + alternative
                if alternative else ""))


def _warn_if_dgc_unavailable(environ=None):
    """Pre-launch heads-up for Intel dGPUs that cannot expose DGC under i915.

    GPU-free (sysfs only), and managed-engine only: a custom Proton may not
    use the DGC-only vkd3d this advisory is about. Advisory, not a block;
    BOL_SKIP_DGC_CHECK=1 silences it.
    """
    source = os.environ if environ is None else environ
    if custom_proton() or source.get("BOL_SKIP_DGC_CHECK") == "1":
        return
    cards = intel_dgpus_on_legacy_driver()
    if cards:
        warn(dgc_warning_message(cards))


def _warn_if_inproc_sync_unavailable(settings, environ=None):
    """Pre-launch heads-up when Wine has no fast synchronization path.

    Wine 11 dropped esync/fsync, so without the kernel ntsync backend every
    Win32 wait is a wineserver round-trip and Minecraft's worker threads end
    up serialised behind it — the "the game runs on one thread" performance
    reports. File/stat inspection only, no Wine process and no ioctl.
    Advisory, not a block; BOL_SKIP_NTSYNC_CHECK=1 silences it.

    PROTON_NO_NTSYNC is read from the Advanced custom-environment field rather
    than the host environment: an inherited copy is dropped as a
    launcher-owned variable, so only the field can actually disable the
    fast path.
    """
    source = os.environ if environ is None else environ
    if source.get("BOL_SKIP_NTSYNC_CHECK") == "1":
        return
    problem = inproc_sync_problem(
        proton_path(),
        environ=custom_env_map(settings.get("custom_env") or ""))
    if problem:
        warn(problem)


def _configure_runtime_compat(env, settings, backend, host_wayland,
                              diagnostics=False, host_env=None,
                              steam_deck=None):
    """Apply launcher-owned Proton compatibility defaults.

    Explicit values from the Advanced custom-environment field are applied
    later and therefore remain the final authority.
    """
    source = os.environ if host_env is None else host_env
    # Drop inherited compatibility flags; Advanced custom values are applied
    # last and remain the supported override.
    for name in LAUNCHER_OWNED_ENV:
        env.pop(name, None)

    if backend == "x11":
        # Keep the stable X11/Xwayland path independent of global Wine settings.
        env["PROTON_ENABLE_WAYLAND"] = "0"
        if host_wayland:
            # Avoid stale Xwayland frames after hiding and restoring the window.
            env["WINE_DISABLE_VULKAN_OPWR"] = "1"

    # GDK-Proton disables Steam Input under Wine-Wayland; HID filtering there
    # would leave no usable controller.
    if backend != "wayland" and _steam_input_available(source):
        # Hide only Sony raw interfaces when Steam supplies its virtual pad.
        env["PROTON_DISABLE_HIDRAW"] = _SONY_STEAM_INPUT_HIDRAW_IDS
    else:
        # SDL exposes Sony devices as gamepads when Steam Input is unavailable.
        env["PROTON_PREFER_SDL"] = "1"

    on_deck = _is_steam_deck(source) if steam_deck is None else steam_deck
    if on_deck:
        # Prevent Wine's decorated frame around fullscreen on Steam Deck.
        env["PROTON_NO_WM_DECORATION"] = "1"

    renderer = str(settings.get("renderer", "auto")).strip().lower()
    if renderer in {"opengl", "wined3d", "legacy"}:
        # Fallback for GPUs below modern DXVK's Vulkan requirement.
        env["PROTON_USE_WINED3D"] = "1"

    if diagnostics:
        env["PROTON_LOG"] = "1"
        env["PROTON_LOG_DIR"] = str(LOGS)
        # These hot polling channels can starve the game with synchronous trace
        # output; keep their warnings and errors without enabling trace.
        env["WINEDEBUG"] = (
            "+gdkc,trace-gdkc,+xgameruntime,"
            "trace-xgameruntime,fixme-all"
        )
    else:
        # Avoid Proton's heavyweight debug log during normal play.
        env["WINEDEBUG"] = "-all"


def _configure_graphics_cache(env, managed_engine):
    """Keep managed-engine shader caches across Minecraft version changes."""
    if not managed_engine:
        return
    cache = DATA / "graphics-cache"
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    env["VKD3D_SHADER_CACHE_PATH"] = str(cache)
    env["DXVK_SHADER_CACHE_PATH"] = str(cache)


def _clear_previous_proton_logs():
    """Keep post-mortem diagnosis scoped to the launch about to start."""
    for path in (LOGS / "proton.log", *LOGS.glob("steam-*.log")):
        path.unlink(missing_ok=True)


def _prefix_stably_idle_after_wrapper(timeout=10.0, interval=0.1,
                                      confirmations=3):
    """Confirm UMU did not detach a live Wine child when its wrapper returned."""

    prefix = active_prefix()
    deadline = time.monotonic() + max(0.0, timeout)
    empty_scans = 0
    while True:
        try:
            live = prefix_processes(prefix)
        except Exception:
            return False
        if live:
            empty_scans = 0
        else:
            empty_scans += 1
            if empty_scans >= max(1, confirmations):
                return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(max(0.0, interval), remaining))


def _prepare_launch_engine():
    """Make the selected engine safe before any Wine process is executed."""
    running = prefix_processes(active_prefix())
    if running:
        die("The BedrockOnLinux Wine prefix is already active "
            f"({len(running)} process(es)). Close Minecraft or use the "
            "explicit 'Force stop Minecraft' action before launching again.")
    managed_engine = not custom_proton()
    if managed_engine:
        ensure_winegdk()
    else:
        patch_proton(proton_path(), strict=False)
    return _prepare_graphics_engine()


def _launch_once(lock_fds=()):
    s = load_settings()
    gd = s.get("game_dir")
    if not gd or not Path(gd, "Minecraft.Windows.exe").exists():
        die("No game — choose a Minecraft version first.")
    if not proton_path():
        die("GDK-Proton missing — run Install / Update.")
    # Engine preparation is GPU-free and may repair state from an older build.
    _prepare_launch_engine()
    # GPU-free advisory: an Intel dGPU on i915 cannot expose the DGC the
    # menu needs; warn before the cryptic page fault instead of after it.
    _warn_if_dgc_unavailable()
    # Same idea for the synchronization fast path: name the cause of the
    # "runs on one thread" stutter before the game starts, not after.
    _warn_if_inproc_sync_unavailable(s)
    # Only completed, idle wrappers can retire a current-boot marker.
    retire_idle_current_boot_marker()
    require_safe_graphics_session()

    account, account_epoch = msa_session_snapshot()
    tok = account.get("refresh_token")
    if not tok:
        die("No Microsoft account linked — click 'Sign in' first.")
    try:
        fresh = msa_refresh(tok)
    except Exception as e:
        fresh = None
        warn(f"Token refresh skipped ({e}) — using cached token.")
    if fresh:
        if not msa_save_for_account_epoch(
                {"refresh_token": fresh["refresh_token"],
                 "obtained": int(time.time())}, account_epoch):
            die("The Microsoft account changed during launch; no stale token "
                "was stored. Click PLAY again after signing in.")
        tok = fresh["refresh_token"]
    if not boot_prefix():
        die("Could not initialise the managed Wine prefix safely.")
    wine_apply_winegdk_prereqs()
    _install_cryptbase_in_prefix()
    try:
        install_gameinput(active_prefix(), Path(gd))
    except Exception as e:
        warn(f"GameInput check failed ({e}) — continuing.")
    if not wine_reg_set_refresh_token(tok):
        die("Could not write the Microsoft login token into the Wine prefix. "
            "The offline registry was left unchanged; use Repair and try "
            "again.")
    access = (fresh or {}).get("access_token")
    ensure_login_deps()
    if not xbl_preauth(access or "", account_epoch):
        detail = xbl_preauth_error_message()
        diagnostic = xbl_preauth_diagnostic() or {}
        stage = diagnostic.get("stage")
        suffix = f" (stage: {stage})" if stage else ""
        die(
            "Could not prepare a complete Xbox Live multiplayer session"
            + suffix + ". "
            + (detail or
               "Check the Microsoft account/network connection and try again.")
        )
    exe = str(CONTENT / "Minecraft.Windows.exe")
    bump_stack_reserve(Path(exe))
    cmd, env = proton_umu_cmd(exe)
    # Required by the menu's indirect root-CBV updates (#27/#29/#30).
    _require_vkd3d_config(env, "force_raw_va_cbv")
    diag = (s.get("diagnostics", False) or os.environ.get("BOL_DIAG") == "1")
    xlog = os.environ.get("BOL_XCURL_LOG")
    if xlog == "1" or (xlog is None and diag):
        env["XCURL_LOG"] = "1"
    # Disable incompatible VR/AGS paths; retain native cryptbase with fallback.
    overrides = ["cryptbase=n,b", "vrclient=", "vrclient_x64=", "openvr_api=",
                 "wineopenxr=", "amd_ags_x64="]
    cur = os.environ.get("WINEDLLOVERRIDES", "")
    if cur:
        overrides.append(cur)
    env["WINEDLLOVERRIDES"] = ";".join(overrides)
    # WindowsAppRuntime framework MSIX cannot install under Wine.
    env["MICROSOFT_WINDOWSAPPRUNTIME_BOOTSTRAP_INITIALIZE_SHOWUI"] = "0"
    env["MICROSOFT_WINDOWSAPPRUNTIME_BOOTSTRAP_INITIALIZE_FAILFAST"] = "0"
    env["MICROSOFT_WINDOWSAPPRUNTIME_DEPLOYMENT_INITIALIZE_ONERRORSHOWUI"] = "0"
    # Do NOT set GNUTLS_SYSTEM_PRIORITY_FILE. A previous workaround pointed it
    # at a "[priorities]\nSYSTEM = NORMAL:-VERS-TLS1.3:%COMPAT" file to force
    # TLS 1.2, but inside the Flatpak it does the opposite: the runtime's
    # GnuTLS default priority is not "@SYSTEM", so the file's SYSTEM override
    # never applies, while the mere presence of the variable makes Wine's
    # secur32 (set_priority in schannel_gnutls.c) skip its own version-capped
    # priority string and use raw GnuTLS defaults — negotiating TLS 1.3, which
    # this Wine's schannel does not support. Result: every in-game WinHTTP TLS
    # connection to Xbox/Azure edges died post-handshake (0x2746 resets /
    # 0x80090304 fatal alerts), the XSAPI RTA WebSocket could never connect,
    # MPSD session writes lacked the required "connection" member, and Friends
    # worlds failed with the misleading "world is full" error (issue #48).
    # Wine's own schannel priority already caps at TLS 1.2, achieving what the
    # workaround intended. Verified with tools/winhttp-rta-probe.c: with the
    # variable set, 58/66 probes fail (rta.xboxlive.com 100%); without it,
    # 66/66 succeed.
    env.pop("GNUTLS_SYSTEM_PRIORITY_FILE", None)
    env.pop("GNUTLS_SYSTEM_PRIORITY_FAIL_ON_INVALID", None)
    preauth = DATA / "winegdk-preauth" / "device.json"
    if preauth.exists():
        env["WINEGDK_PREAUTH_DEVICE"] = "Z:" + str(preauth).replace("/", "\\")
    rp = s.get("xsts_rp")
    if rp:
        host = s.get("xsts_rp_host") or "b980a380.minecraft.playfabapi.com"
        san = "".join(c.upper() if c.isalnum() else "_" for c in host)
        env["WINEGDK_XSTS_RP_" + san] = rp
        info(f"XSTS relying party override [{host}] = {rp}")
    if not account_epoch_is_current(account_epoch):
        die("The Microsoft account changed during launch. Minecraft was not "
            "started; click PLAY again with the current account.")
    wl = os.environ.get("WAYLAND_DISPLAY")
    backend = (os.environ.get("BOL_INPUT")
               or s.get("input_backend") or "auto").lower()
    if backend == "auto":
        backend = "x11"
    gs_opt = s.get("gamescope") or os.environ.get("BOL_GAMESCOPE")
    want_gamescope = bool(gs_opt) and \
        gs_opt.lower() not in ("0", "no", "off", "false")
    use_gamescope = want_gamescope and bool(shutil.which("gamescope"))
    if use_gamescope:
        backend = "x11"
    elif want_gamescope and not shutil.which("gamescope"):
        warn("BOL_GAMESCOPE is set but gamescope isn't installed — ignored.")
    _configure_runtime_compat(
        env, s, backend, bool(wl), diagnostics=diag,
    )
    _configure_graphics_cache(env, managed_engine=not custom_proton())
    disp = os.environ.get("DISPLAY")
    if backend == "wayland" and wl:
        env["PROTON_ENABLE_WAYLAND"] = "1"
        env["WAYLAND_DISPLAY"] = wl
        xrd = os.environ.get("XDG_RUNTIME_DIR")
        if xrd:
            env["XDG_RUNTIME_DIR"] = xrd
        env.pop("DISPLAY", None)
        mon = (os.environ.get("BOL_WAYLAND_MONITOR")
               or os.environ.get("WAYLANDDRV_PRIMARY_MONITOR"))
        if mon:
            env["WAYLANDDRV_PRIMARY_MONITOR"] = mon
        warn("BOL_INPUT=wayland → winewayland (experimental). If it can't "
             "open a window no automatic GPU relaunch is attempted; "
             "to help winewayland connect first try BOL_WAYLAND_MONITOR=<output> "
             "(e.g. eDP-1).")
    else:
        if backend == "wayland":
            warn("BOL_INPUT=wayland but no WAYLAND_DISPLAY found — using X11.")
        if disp:
            env["DISPLAY"] = disp
            for cand in (os.environ.get("XAUTHORITY"), str(HOME / ".Xauthority"),
                         f"/run/user/{os.getuid()}/.mutter-Xwaylandauth.0"):
                if cand and Path(cand).exists():
                    env["XAUTHORITY"] = cand
                    break
        elif wl:
            warn("Wayland session without X DISPLAY — install XWayland (or set "
                 "BOL_INPUT=wayland to use winewayland).")
    if use_gamescope:
        if gs_opt and gs_opt.strip().lower() not in ("1", "yes", "on", "true"):
            gs_argv = ["gamescope"] + shlex.split(gs_opt)
        else:
            gs_argv = ["gamescope", "-f"]
            wh = _screen_wh()
            if wh:
                gs_argv += ["-W", wh[0], "-H", wh[1], "-w", wh[0], "-h", wh[1]]
        cmd = gs_argv + ["--"] + cmd
        info("Using gamescope (BOL_GAMESCOPE).")
    if not account_epoch_is_current(account_epoch):
        die("The Microsoft account changed before the game process started. "
            "Minecraft was not started; click PLAY again.")
    apply_custom_env(env, s.get("custom_env") or "")
    _warn_custom_env_overrides(s.get("custom_env") or "")
    # Prevent diagnosis from attributing stale Proton logs to this launch.
    _clear_previous_proton_logs()
    info("Starting Minecraft … sign in with Microsoft in-game, then "
         "join your server from the Servers tab.")
    glog = open(LOGS / "minecraft.log", "w")
    rc = None
    hits = []
    gpu_marker_token = None
    game_returned = False
    try:
        # A hard reboot leaves this marker so the next launch fails closed.
        gpu_marker_token = arm_gpu_launch()
        try:
            popen_options = {
                "env": env,
                "cwd": str(CONTENT),
                "stdout": glog,
                "stderr": subprocess.STDOUT,
            }
            if lock_fds:
                # Keep both launch locks alive in UMU if the Python launcher
                # is killed. UMU remains the game wrapper for the session.
                popen_options["pass_fds"] = tuple(lock_fds)
            proc = subprocess.Popen(cmd, **popen_options)
        except Exception:
            try:
                if not disarm_gpu_launch(gpu_marker_token):
                    warn("The game process could not be started and its GPU "
                         "safety marker could not be cleared. Close the "
                         "launcher, then inspect the marker with Doctor.")
            except Exception as marker_error:
                warn("The game process could not be started and clearing its "
                     "GPU safety marker failed (%s)." %
                     type(marker_error).__name__)
            raise
        started = time.time()
        announced = False
        while True:
            try:
                rc = proc.wait(timeout=1)
                game_returned = True
                break
            except subprocess.TimeoutExpired:
                if not announced and time.time() - started > 8:
                    announced = True
                    ok("Minecraft is running — close the game window to come "
                       "back here.")
    finally:
        if game_returned and gpu_marker_token:
            try:
                wrapper_returned_recorded = mark_gpu_wrapper_returned(
                    gpu_marker_token)
            except Exception as marker_error:
                wrapper_returned_recorded = False
                warn("Minecraft returned, but recording its GPU-marker phase "
                     "failed (%s)." % type(marker_error).__name__)
            if not wrapper_returned_recorded:
                warn("Minecraft returned, but its GPU marker could not record "
                     "the completed wrapper phase. A failed teardown will "
                     "require explicit Doctor acknowledgement.")
            if not _prefix_stably_idle_after_wrapper():
                warn("The UMU wrapper returned while Wine/Minecraft processes "
                     "still appear live. The GPU safety marker was retained; "
                     "force-stop the remaining processes and inspect the "
                     "driver before acknowledging the incident.")
            elif not disarm_gpu_launch(gpu_marker_token):
                warn("Minecraft returned, but its GPU safety marker could not "
                     f"be cleared. Run '{acknowledge_gpu_crash_command()}' "
                     "after checking the driver.")
        glog.close()
        patch_options()
        logs = sorted(LOGS.glob("steam-*.log"),
                      key=lambda p: p.stat().st_mtime if p.exists() else 0)
        if logs:
            logs[-1].replace(LOGS / "proton.log")
            for old in logs[:-1]:
                old.unlink(missing_ok=True)
        ok(f"Game closed (exit {rc}).")
        hits = diagnose()
    # Diagnose only; never reset or relaunch a GPU process automatically.
    broken = any("prefix broken" in h.lower() for h in hits)
    no_display = any("display unavailable" in h.lower() for h in hits)
    rng_abort = any("rng unresolved" in h.lower() for h in hits)
    wayland_attempt = env.get("PROTON_ENABLE_WAYLAND") == "1"
    if use_gamescope:
        ml = LOGS / "minecraft.log"
        ran = ml.exists() and "umu-launcher" in ml.read_text(errors="ignore")[:8000]
        if broken or not ran:
            warn("gamescope could not present the game. Automatic relaunch is "
                 "disabled for GPU safety; turn off BOL_GAMESCOPE and click "
                 "PLAY once after checking the logs.")
    if rng_abort:
        warn("The window failure came from the cryptbase RNG abort, not a broken "
             "prefix or GPU — relaunch (builtin cryptbase now provides "
             "RtlGenRandom).")
    elif wayland_attempt and broken:
        warn("winewayland could not open a window. Automatic XWayland relaunch "
             "is disabled for GPU safety; set BOL_INPUT=x11, then click PLAY "
             "once after checking the display.")
    elif broken and not no_display:
        warn("The Wine prefix may be broken. Automatic reset/relaunch is "
             "disabled for GPU safety; use the explicit Repair action, then "
             "click PLAY once.")
    return rc


def launch():
    """Run exactly one guarded launch for each user action."""
    with launch_lock() as lock_fds:
        return _launch_once(lock_fds)
