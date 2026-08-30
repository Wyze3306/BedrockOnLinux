"""bol.launch — launching Minecraft through Proton/umu."""
# SPDX-License-Identifier: MIT

import math
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from .auth import (
    account_epoch_is_current,
    msa_refresh,
    msa_signed_in,
    msa_save_for_account_epoch,
    msa_session_snapshot,
    wine_apply_winegdk_prereqs,
    wine_reg_set_refresh_token,
    xbl_preauth,
    xbl_preauth_diagnostic,
    xbl_preauth_error_message,
)
from . import discord, presence as xbl_presence, xodus
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
    in_gamescope_session,
    mark_gpu_wrapper_returned,
    require_safe_graphics_session,
    retire_idle_current_boot_marker,
)
from .inject import start_auto_inject
from .log import BolError, die, info, ok, warn
from .ntsync import inproc_sync_problem
from .perfcheck import (
    find_options_file,
    frame_rate_is_unlimited,
    performance_problems,
    read_game_options,
)
from .platform import IS_MAC
from .prefix import (
    active_prefix,
    boot_prefix,
    engine_cmd,
    launch_lock,
    patch_options,
    prefix_processes,
    restore_truncated_game_options,
    seed_default_servers,
    snapshot_game_options,
)
from .proton import custom_proton, patch_proton, proton_path
from .util import (
    _screen_refresh_hz,
    _screen_wh,
    apply_custom_env,
    custom_env_map,
    env_flag,
    LAUNCHER_OWNED_ENV,
    LAUNCHER_OWNED_ENV_ALTERNATIVE,
    launcher_owned_overrides,
    load_settings,
    steam_app_id,
)
from .vkd3d import prepare_universal_vkd3d
from .waylanddrv import engine_wayland_driver_problem
from .winegdk import ensure_winegdk
from .x11 import tag_steam_game_windows


# Completes both "Minecraft starts in …" sentences below. Keep the wording in
# one place: it is the answer to "why can I not play without signing in?".
_OFFLINE_MODE_NOTICE = (
    "offline mode — single-player worlds and LAN play work, while Realms, "
    "servers, the Marketplace and Xbox friends stay unavailable until "
    "Xbox Live sign-in succeeds."
)

_SONY_STEAM_INPUT_HIDRAW_IDS = ",".join((
    "0x054C/0x05C4",  # DualShock 4
    "0x054C/0x09CC",  # DualShock 4 v2
    "0x054C/0x0BA0",  # DualShock 4 wireless adapter
    "0x054C/0x0CE6",  # DualSense
    "0x054C/0x0DF2",  # DualSense Edge
))


def _prepare_graphics_engine():
    """Activate the universal DGC pair without opening Vulkan in the launcher."""
    if IS_MAC:
        # vkd3d-proton is a Vulkan payload built for Linux. On macOS the
        # backend brings its own Direct3D 12 translation -- D3DMetal under
        # Game Porting Toolkit and CrossOver -- which the launcher neither
        # ships nor configures, so there is no graphics payload to activate.
        return None
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


def _vkd3d_config_options(env):
    """The vkd3d options currently declared, in their declared order."""
    return [item.strip() for item in
            env.get("VKD3D_CONFIG", "").replace(";", ",").split(",")
            if item.strip()]


def _require_vkd3d_config(env, option):
    """Add one vkd3d option without discarding user-provided options."""
    options = _vkd3d_config_options(env)
    if option not in options:
        options.append(option)
    env["VKD3D_CONFIG"] = ",".join(options)


def _forbid_vkd3d_config(env, option):
    """Drop one vkd3d option without discarding user-provided options."""
    options = [item for item in _vkd3d_config_options(env) if item != option]
    if options:
        env["VKD3D_CONFIG"] = ",".join(options)
    else:
        env.pop("VKD3D_CONFIG", None)


def _configure_ray_tracing(env, settings):
    """Hand DXR to Minecraft, or hide it, per the Settings switch.

    vkd3d-proton reports the ray tracing tier by itself once the driver
    exposes the Vulkan ray tracing extensions, so "on" is not something to
    declare: it is making sure nothing declares the opposite, an inherited
    ``VKD3D_CONFIG=nodxr`` in particular. Only "off" needs a positive
    statement, which is why the vkd3d option exists in that direction alone.

    Turning it off hides Minecraft's *Ray Traced* graphics mode. It leaves
    *Vibrant Visuals* alone: that pipeline is deferred rendering, not ray
    tracing, and runs on GPUs that have no DXR at all.
    """
    if settings.get("ray_tracing", True):
        _forbid_vkd3d_config(env, "nodxr")
        return
    _require_vkd3d_config(env, "nodxr")
    info("Ray tracing is off in Settings — Minecraft's Ray Traced graphics "
         "mode stays unavailable this launch.")


def _requested_frame_rate(environ=None):
    """What ``BOL_FRAME_RATE`` asks for: a rate, 0 for uncapped, or None.

    None means "not set", which leaves the automatic behaviour below in
    charge. Anything unparsable is treated the same way rather than failing a
    launch over a typo in an environment variable.
    """
    raw = str((os.environ if environ is None else environ)
              .get("BOL_FRAME_RATE", "")).strip().lower()
    if not raw:
        return None
    if raw in ("0", "off", "no", "false", "none", "unlimited"):
        return 0.0
    try:
        value = float(raw)
    except ValueError:
        warn("BOL_FRAME_RATE=%s is not a number of frames per second — "
             "ignored." % raw)
        return None
    return value if value > 0 else 0.0


def _configure_frame_rate_limit(env, settings=None, prefix=None, environ=None,
                                refresh_probe=None):
    """Stop Minecraft drawing frames no display will ever show.

    With vsync off and *Max Framerate* on Unlimited nothing paces the render
    loop, and the main menu — the cheapest frame in the game — then runs into
    four figures of FPS and takes most of the GPU to display a still image
    (issue #150). vkd3d-proton's own limiter is the right place to stop that:
    it sleeps until the frame deadline instead of spinning, and it applies to
    every frame the game presents, menu included.

    Only the genuinely unpaced case is capped, and only at the refresh rate of
    the fastest display attached, so a player who set either of Minecraft's
    own limits keeps exactly what they chose and nobody loses a frame they
    could have seen.

    Two things override that, in order. ``BOL_FRAME_RATE`` wins outright,
    since it is the only one that can name a rate: 0 never caps, a number
    always caps at it, whatever the game and the switch say. Callers pass
    ``environ`` with the Advanced custom-environment field overlaid, since
    that field is where it is documented and it is applied too late in the
    launch to be visible here. Failing that, the *Limit the frame rate to the
    display* switch in Settings decides whether the automatic cap applies at
    all; it is on by default, because the uncapped menu is a bug report and
    not a preference.

    The limit is always whole frames per second, rounded up. That is not
    cosmetic: a value carrying a decimal point is parsed as no limit at all
    and silently does nothing, so a 143.85 Hz display has to be asked for as
    144 — and rounding up rather than down is what keeps the cap from landing
    just under the rate the display is actually driving.
    """
    source = os.environ if environ is None else environ
    requested = _requested_frame_rate(source)
    if requested == 0.0:
        return None
    if requested is None and not (settings or {}).get("limit_frame_rate", True):
        return None
    if env.get("VKD3D_FRAME_RATE", "").strip():
        # An inherited limit is already an explicit answer to this question.
        return None

    if requested is None:
        options = read_game_options(find_options_file(prefix))
        if not frame_rate_is_unlimited(options):
            return None
        probe = _screen_refresh_hz if refresh_probe is None else refresh_probe
        try:
            refresh = probe()
        except Exception:
            # A display that cannot be measured must cost the cap, never the
            # launch.
            refresh = None
        if not refresh or refresh <= 0:
            # Without a refresh rate there is no defensible number to pick,
            # and inventing one would cap a display we never measured.
            warn("Minecraft has vsync off and Max Framerate on Unlimited, so "
                 "nothing limits how fast it draws — the main menu alone can "
                 "take most of the GPU. The launcher could not read any "
                 "display's refresh rate to cap it; set Max Framerate in "
                 "Video settings, or BOL_FRAME_RATE=<fps>.")
            return None
        limit = math.ceil(refresh)
        info("Nothing in Minecraft's settings limits the frame rate (vsync "
             "off, Max Framerate on Unlimited), so the launcher caps it at "
             "%d FPS for this display's %.2f Hz — frames past that are never "
             "shown, and the menu alone would otherwise take most of the "
             "GPU. Turn off 'Limit the frame rate to the display' in "
             "Settings ▸ Advanced to render uncapped."
             % (limit, refresh))
    else:
        limit = math.ceil(requested)
        info("BOL_FRAME_RATE limits Minecraft to %d FPS." % limit)

    env["VKD3D_FRAME_RATE"] = str(limit)
    return limit


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


# Long enough for a cold Steam Deck to reach the menu, short enough that a
# launch which never opens a window still reports why before the player quits.
_STEAM_WINDOW_TAG_DEADLINE = 180.0


def _steam_game_window_tagger(env, exe, environ=None, single_window=None,
                              deadline=_STEAM_WINDOW_TAG_DEADLINE,
                              clock=time.time, tag=None):
    """A callable that gives Minecraft's window this session's Steam identity.

    Steam Game Mode presents whichever window gamescope can attribute to the
    application Steam launched, and gamescope attributes a window either from
    its ``STEAM_GAME`` property or by walking the owning process up to Steam's
    reaper. Out of the Flatpak neither route reaches Minecraft: the game is
    started through the Flatpak portal, so its process tree no longer leads
    back to that reaper, and this engine is built from Wine rather than
    Proton's fork, which is what sets ``STEAM_GAME``. The window is mapped and
    rendering while gamescope considers only Steam focusable, and that reaches
    the player as a game that is audible but never appears (#199).

    Returns None when there is nothing to do, and otherwise a callable that
    makes one attempt per call and reports True once there is no point in
    calling it again. It keeps watching after the first success, because Wine
    builds a new X window whenever the game changes what kind of window it
    wants, and a replacement starts out with no identity of its own again.
    """
    app_id = steam_app_id(environ)
    display = str(env.get("DISPLAY") or "").strip()
    if not app_id or not display or env.get("PROTON_ENABLE_WAYLAND") == "1":
        return None
    if single_window is None:
        single_window = single_window_session()
    if not single_window:
        return None
    # Wine names a window's class after the executable it runs, lowercased.
    wm_class = Path(exe).name.lower()
    stamp = tag_steam_game_windows if tag is None else tag
    started = clock()
    tagged = set()
    reported = set()

    def attempt():
        try:
            fresh = stamp(app_id, wm_class, display=display, skip=tagged)
        except Exception as error:
            warn("Could not give the game window this session's Steam "
                 "application ID (%s). If Minecraft stays invisible in Game "
                 "Mode, this is why." % type(error).__name__)
            return True
        if fresh:
            tagged.update(fresh)
            if "tagged" not in reported:
                reported.add("tagged")
                info("Minecraft's window now carries Steam application ID %s, "
                     "so Game Mode can bring it to the front." % app_id)
        elif (not tagged and "missing" not in reported
                and clock() - started >= deadline):
            reported.add("missing")
            warn("No Minecraft window appeared on this session's display, so "
                 "none could be given Steam application ID %s. In Game Mode "
                 "the game keeps running without ever being shown." % app_id)
        return False

    return attempt


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


def _warn_if_performance_degraded(environ=None):
    """Pre-launch heads-up for the ordinary causes of "the game lags".

    Exhausted memory, a full data directory, windowed vsync on a compositing
    desktop and an extreme render distance all cost frame rate without
    leaving anything in a Wine or vkd3d log, so they get reported as engine
    or GPU faults. Naming them here costs two /proc reads, one statvfs and a
    parse of Minecraft's own options.txt — no Wine process, no GPU.
    Advisory, not a block; BOL_SKIP_PERF_CHECK=1 silences it.
    """
    source = os.environ if environ is None else environ
    if source.get("BOL_SKIP_PERF_CHECK") == "1":
        return
    for problem in performance_problems(active_prefix(), DATA, environ=source):
        warn(problem)


def _resolve_input_backend(backend, host_wayland, engine_root):
    """The graphics backend to really launch with.

    ``BOL_INPUT=wayland`` asks Wine for a native Wayland window. An engine
    whose winewayland cannot load fails its PROCESS_ATTACH and opens no window
    at all, which reaches the user as "the game does not start" and reads like
    a display or GPU fault (issue #180). The driver is opt-in and XWayland is
    the supported path anyway, so name the real cause and take XWayland rather
    than starting a launch that cannot succeed. A missing WAYLAND_DISPLAY is
    left to the caller, which reports it where it configures the X11 session.
    """
    if backend != "wayland" or not host_wayland:
        return backend
    problem = engine_wayland_driver_problem(engine_root)
    if not problem:
        return backend
    warn(problem + " Using X11/XWayland for this launch.")
    return "x11"


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

    # vkd3d-proton is silent by default, so "the game does not detect my ray
    # tracing hardware" (#153) arrives with no way to tell whether the game
    # was ever offered DXR, at which tier, and which ExecuteIndirect path it
    # got. Its info level answers all three in about two dozen lines at device
    # creation and logs nothing per frame, so ask for it on every launch --
    # this is the graphics equivalent of what the launcher already reads back
    # for synchronisation and the Wayland driver. `bol.raytracing` parses it.
    env["VKD3D_DEBUG"] = "info"


def _configure_graphics_cache(env, managed_engine):
    """Keep managed-engine shader caches across Minecraft version changes."""
    if not managed_engine:
        return
    cache = DATA / "graphics-cache"
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    env["VKD3D_SHADER_CACHE_PATH"] = str(cache)
    env["DXVK_SHADER_CACHE_PATH"] = str(cache)


def _configure_display(env, backend, wl):
    """Point Wine at this session's display server.

    Linux only, and called only from there: it chooses between winewayland and
    X11/XWayland, carries the X authority file across, and says what to do
    when neither is reachable. macOS Wine draws through Quartz with no
    equivalent to configure.
    """
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
        return
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


def _configure_mac_runtime(env, settings, diagnostics=False):
    """The macOS half of _configure_runtime_compat.

    Almost nothing carries over. Every ``PROTON_*`` variable there is read by
    Proton's own launch script, which does not exist here; the Steam Input
    HIDRAW filtering is Linux hidraw; ``VKD3D_DEBUG`` belongs to a payload
    macOS does not run. What is left is Wine's own logging, the backend
    environment bol.winemac chose, and the Metal HUD -- and that is the whole
    of it, deliberately: the Direct3D translation here is Apple's or
    CodeWeavers', and second-guessing it from the launcher is how a working
    renderer gets broken.
    """
    for name in LAUNCHER_OWNED_ENV:
        env.pop(name, None)
    if diagnostics:
        env["WINEDEBUG"] = "+xgameruntime,trace-xgameruntime,fixme-all"
        # Wine writes to stderr, which the caller already redirects into
        # minecraft.log; there is no separate Proton log on macOS.
    else:
        env["WINEDEBUG"] = "-all"
    # Apple's frame-rate/GPU overlay, off unless Diagnostics asked for it.
    env["MTL_HUD_ENABLED"] = "1" if diagnostics else "0"
    renderer = str(settings.get("renderer", "auto")).strip().lower()
    if renderer in {"opengl", "wined3d", "legacy"}:
        warn("The Legacy compatibility renderer is a Proton setting and does "
             "nothing on macOS: Direct3D here is translated by the Wine "
             "backend's own layer. Leaving the renderer as it is.")
    return env


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
    if IS_MAC:
        # The macOS runtime is a Wine installed system-wide, shared with
        # every other application that uses it. Confirm it is still there and
        # leave it exactly as it is: the combase/ntdll byte patches belong to
        # one specific GDK-Proton build, and this is not it.
        from .winemac import ensure_wine
        ensure_wine()
        return None
    managed_engine = not custom_proton()
    if managed_engine:
        ensure_winegdk()
    else:
        patch_proton(proton_path(), strict=False)
    return _prepare_graphics_engine()


def _launch_once(lock_fds=(), on_started=None):
    s = load_settings()
    gd = s.get("game_dir")
    if not gd or not Path(gd, "Minecraft.Windows.exe").exists():
        die("No game — choose a Minecraft version first.")
    if not proton_path():
        die("GDK-Proton missing — run Install / Update.")
    # Engine preparation is GPU-free and may repair state from an older build.
    _prepare_launch_engine()
    if not IS_MAC:
        # GPU-free advisory: an Intel dGPU on i915 cannot expose the DGC the
        # menu needs; warn before the cryptic page fault instead of after it.
        _warn_if_dgc_unavailable()
        # Same idea for the synchronization fast path: name the cause of the
        # "runs on one thread" stutter before the game starts, not after.
        # Both read Linux kernel interfaces and describe Linux payloads; the
        # macOS backend's own synchronisation is set up in bol.winemac.
        _warn_if_inproc_sync_unavailable(s)
    # And for the causes that are not the engine at all: no memory left, no
    # disk left, windowed vsync, a render distance past the main thread.
    _warn_if_performance_degraded()
    # Only completed, idle wrappers can retire a current-boot marker.
    retire_idle_current_boot_marker()
    require_safe_graphics_session()

    account, account_epoch = msa_session_snapshot()
    tok = account.get("refresh_token")
    if IS_MAC and tok:
        # The token is carried into the prefix for WineGDK's XUser fork to
        # pick up, and that fork is compiled into GDK-Proton -- a Linux
        # build. A native macOS Wine stubs those calls, so writing the token
        # and running a full Xbox Live pre-auth would spend a round of network
        # calls to arrive at exactly the offline session below. Say so once
        # and skip it, rather than reporting a sign-in failure for a sign-in
        # this platform never had.
        warn("This Mac runs a native Wine, which has no WineGDK Xbox layer, "
             "so the linked Microsoft account cannot be handed to the game. "
             "Minecraft starts in " + _OFFLINE_MODE_NOTICE)
        tok = None
    fresh = None
    # A transport failure here is the offline case, not a rejected account.
    refresh_unreachable = False
    if tok:
        try:
            fresh = msa_refresh(tok)
        except Exception as e:
            refresh_unreachable = True
            warn(f"Token refresh skipped ({e}) — using cached token.")
        if fresh:
            if not msa_save_for_account_epoch(
                    {"refresh_token": fresh["refresh_token"],
                     "obtained": int(time.time())}, account_epoch):
                die("The Microsoft account changed during launch; no stale "
                    "token was stored. Click PLAY again after signing in.")
            tok = fresh["refresh_token"]
    if not boot_prefix():
        die("Could not initialise the managed Wine prefix safely.")
    wine_apply_winegdk_prereqs()
    _install_cryptbase_in_prefix()
    try:
        install_gameinput(active_prefix(), Path(gd))
    except Exception as e:
        warn(f"GameInput check failed ({e}) — continuing.")
    # Xbox Live is required for Realms, servers, the Marketplace and Friends —
    # never for the game itself. Neither a missing account nor an unreachable
    # Xbox Live may keep single-player and LAN worlds from starting (#160).
    online = False
    if not tok:
        warn("No Microsoft account is linked, so Minecraft starts in "
             + _OFFLINE_MODE_NOTICE + " Use 'Sign in' to add one.")
    else:
        if not wine_reg_set_refresh_token(tok):
            die("Could not write the Microsoft login token into the Wine "
                "prefix. The offline registry was left unchanged; use Repair "
                "and try again.")
        ensure_login_deps()
        online = xbl_preauth((fresh or {}).get("access_token") or "",
                             account_epoch,
                             refresh_unreachable=refresh_unreachable)
        if not online:
            detail = xbl_preauth_error_message()
            diagnostic = xbl_preauth_diagnostic() or {}
            stage = diagnostic.get("stage")
            suffix = f" (stage: {stage})" if stage else ""
            warn(
                "Could not prepare a complete Xbox Live multiplayer session"
                + suffix + ". "
                + (detail or
                   "Check the Microsoft account/network connection and try "
                   "again.")
                + " Minecraft starts in " + _OFFLINE_MODE_NOTICE
            )
    exe = str(CONTENT / "Minecraft.Windows.exe")
    # A Microsoft Store package keeps the executable encrypted at rest, so
    # there is no PE header on disk to edit and no image for Wine to open. Both
    # are handled after Xodus decrypts it into anonymous memory, below.
    encrypted_exe = xodus.exe_is_encrypted(Path(exe))
    if encrypted_exe and IS_MAC:
        # A Microsoft Store package is decrypted at every launch by xodus-cli,
        # which is Linux-only. Refuse here, where the folder that has to
        # change is still nameable, rather than handing Wine a file that is
        # ciphertext and letting it fail on "not a valid Win32 application".
        die("This Minecraft build is an encrypted Microsoft Store package, "
            "and decrypting it needs xodus-cli, which has no macOS build. "
            + xodus.MAC_UNSUPPORTED)
    if not encrypted_exe:
        bump_stack_reserve(Path(exe))
    cmd, env = engine_cmd(exe)
    if not IS_MAC:
        # Required by the menu's indirect root-CBV updates (#27/#29/#30).
        _require_vkd3d_config(env, "force_raw_va_cbv")
        _configure_ray_tracing(env, s)
        # The Advanced custom-environment field is applied at the end of this
        # function, far too late to be read here, so overlay it explicitly: it
        # is where BOL_FRAME_RATE is documented, and the supported way to set
        # it. All three drive vkd3d-proton, which macOS does not run.
        _configure_frame_rate_limit(
            env, s, active_prefix(),
            environ={**os.environ,
                     **custom_env_map(s.get("custom_env") or "")})
    diag = (s.get("diagnostics", False) or os.environ.get("BOL_DIAG") == "1")
    xlog = os.environ.get("BOL_XCURL_LOG")
    if xlog == "1" or (xlog is None and diag):
        env["XCURL_LOG"] = "1"
    # Disable incompatible VR/AGS paths; retain native cryptbase with fallback.
    # The cryptbase entry is GDK-Proton's: its advapi32 forwards
    # SystemFunction036 to a DLL the prefix has to provide (#144). A native
    # macOS Wine implements RtlGenRandom itself, so asking for a native
    # cryptbase there would only point it at a file nobody installed.
    overrides = ["vrclient=", "vrclient_x64=", "openvr_api=",
                 "wineopenxr=", "amd_ags_x64="]
    if not IS_MAC:
        overrides.insert(0, "cryptbase=n,b")
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
    # Everything from here to the account re-check is read by WineGDK's
    # xgameruntime, which exists only in the Linux engine.
    preauth = None if IS_MAC else DATA / "winegdk-preauth" / "device.json"
    # Only a payload pre-auth just vouched for is handed to the engine: an
    # expired or account-mismatched one would send it chasing a sign-in that
    # cannot complete instead of settling into offline mode.
    if online and preauth is not None and preauth.exists():
        env["WINEGDK_PREAUTH_DEVICE"] = "Z:" + str(preauth).replace("/", "\\")
    rp = None if IS_MAC else s.get("xsts_rp")
    if rp:
        host = s.get("xsts_rp_host") or "b980a380.minecraft.playfabapi.com"
        san = "".join(c.upper() if c.isalnum() else "_" for c in host)
        env["WINEGDK_XSTS_RP_" + san] = rp
        info(f"XSTS relying party override [{host}] = {rp}")
    if not account_epoch_is_current(account_epoch):
        die("The Microsoft account changed during launch. Minecraft was not "
            "started; click PLAY again with the current account.")
    # Where the two platforms stop having anything in common. Everything in
    # the else-branch below is about which Linux display server Wine talks to
    # and what wraps its window: X11 or winewayland, Proton's compatibility
    # defaults, gamescope. macOS Wine talks to Quartz, always -- there is
    # nothing to choose between and no nested compositor to wrap the game in.
    wl = None
    gs_opt = None
    use_gamescope = False
    if IS_MAC:
        _configure_mac_runtime(env, s, diagnostics=diag)
    else:
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
            warn("BOL_GAMESCOPE is set but gamescope isn't installed — "
                 "ignored.")
        backend = _resolve_input_backend(backend, bool(wl), proton_path())
        _configure_runtime_compat(
            env, s, backend, bool(wl), diagnostics=diag,
        )
        _configure_graphics_cache(env, managed_engine=not custom_proton())
        _configure_display(env, backend, wl)
    if encrypted_exe:
        # Must wrap before gamescope: gamescope has to stay outermost so it
        # owns the compositor the game renders into.
        cmd = xodus.wrap_encrypted_launch(cmd, Path(gd), DATA / "run", env=env)
    if use_gamescope:
        if gs_opt and not env_flag(gs_opt):
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
    # Repair a settings file a previous crash cut off before the game reads
    # it, then keep a copy of what it is about to start rewriting (#175).
    # Safe to declare idle: _prepare_launch_engine() refused to get this far
    # with a live prefix and the game has not been started yet, so nothing
    # that writes options.txt can be running, whatever wineboot left behind.
    restore_truncated_game_options(prefix_idle=True)
    snapshot_game_options()
    # The Servers tab is read from disk at startup, so the servers this
    # launcher ships with have to be in the list before the game opens it.
    seed_default_servers(prefix_idle=True)
    # The account travelled into the prefix a few lines above, so the
    # game signs itself in; the in-game button reaches a sign-in the
    # engine does not implement and only ever fails (#227/#228).
    info("Starting Minecraft … your account is already linked; "
         "join your server from the Servers tab.")
    glog = open(LOGS / "minecraft.log", "w")
    rc = None
    hits = []
    gpu_marker_token = None
    game_returned = False
    presence = discord.Session()
    xbl = xbl_presence.Session()
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
        if on_started is not None:
            # The caller owns a window that may have to step aside for the
            # game's own; never let that bookkeeping abort a running launch.
            try:
                on_started()
            except Exception as hook_error:
                warn("The launcher could not step aside for the game window "
                     "(%s)." % type(hook_error).__name__)
        # Auto-injection belongs to the launch and not to whoever asked for
        # it: a direct-launch shortcut and Game Mode both come through
        # `bol play`, which has no GUI worker to hang the watcher off.
        try:
            # The watcher finds the game by reading /proc, which macOS does
            # not have; nothing on a Mac can be injected into by this path.
            if not IS_MAC:
                start_auto_inject(s)
        except Exception as inject_error:
            warn("Automatic DLL injection could not be started (%s)."
                 % type(inject_error).__name__)
        started = time.time()
        # Say on Discord what is being played, for as long as it is played:
        # the launcher is found by word of mouth, and this is it saying its
        # own name. Settings turns it off, and it never affects the game.
        presence = discord.start_session(s, started_at=started)
        # And say it on Xbox Live, which is the half that other players act
        # on: nothing in the game publishes presence under Wine, so without
        # this the account reads "Offline" to its own dressing room and to
        # every friend, and no one can join or invite it (#238, #243).
        if online:
            xbl = xbl_presence.start_session(s)
            xbl_presence.warn_if_unavailable(xbl, s)
        announced = False
        # There is no window yet to give Steam's identity to, so this watches
        # for one on the same tick that waits on the game process rather than
        # adding a thread of its own.
        # X11 property writing, so Steam recognises the game's own window.
        tag_game_window = None if (use_gamescope or IS_MAC) else \
            _steam_game_window_tagger(env, exe)
        while True:
            try:
                rc = proc.wait(timeout=1)
                game_returned = True
                break
            except subprocess.TimeoutExpired:
                if tag_game_window is not None and tag_game_window():
                    tag_game_window = None
                if not announced and time.time() - started > 8:
                    announced = True
                    ok("Minecraft is running — close the game window to come "
                       "back here.")
    finally:
        # First: the teardown below can take a while, and nobody should be
        # left showing as in-game through it.
        presence.stop()
        xbl.stop()
        prefix_idle = None
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
            prefix_idle = _prefix_stably_idle_after_wrapper()
            if not prefix_idle:
                warn("The UMU wrapper returned while Wine/Minecraft processes "
                     "still appear live. The GPU safety marker was retained; "
                     "force-stop the remaining processes and inspect the "
                     "driver before acknowledging the incident.")
            elif not disarm_gpu_launch(gpu_marker_token):
                warn("Minecraft returned, but its GPU safety marker could not "
                     f"be cleared. Run '{acknowledge_gpu_crash_command()}' "
                     "after checking the driver.")
        glog.close()
        # Both of these rewrite the file Minecraft keeps its settings in, so
        # neither may run while the game could still be saving to it (#175).
        if prefix_idle is None:
            prefix_idle = _prefix_stably_idle_after_wrapper()
        restore_truncated_game_options(prefix_idle=prefix_idle)
        patch_options(prefix_idle=prefix_idle)
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


def launch(on_started=None):
    """Run exactly one guarded launch for each user action.

    ``on_started`` is called once the game process exists, before the wait on
    it. A launcher window uses it to get out of the game's way in a session
    that shows one window at a time.
    """
    with launch_lock() as lock_fds:
        return _launch_once(lock_fds, on_started=on_started)


def direct_launch_readiness():
    """First-run steps a launcher-free shortcut cannot perform on its own.

    A shortcut that skips the window has nowhere to show a device code or a
    version picker, so name what is still missing when one is created rather
    than letting the first click fail silently. Cheap and offline: settings
    and token files only, no network and no Wine process.
    """
    pending = []
    game_dir = load_settings().get("game_dir")
    if not game_dir or not Path(game_dir, "Minecraft.Windows.exe").exists():
        pending.append(
            "No Minecraft version is installed yet. Open the launcher once "
            "and install one; the shortcut only starts a prepared "
            "installation.")
    if not msa_signed_in():
        pending.append(
            "No Microsoft account is linked yet. Sign in from the launcher "
            "once; a shortcut has nowhere to display the Microsoft device "
            "code.")
    return pending


def single_window_session(environ=None):
    """Whether the session shows one application window at a time.

    Steam Game Mode is that session: Gamescope presents a single window, so
    the launcher's own stands between Steam and the game — the game stays
    audible but never appears (#130). The answer is for the launcher to step
    aside while the game runs, not for it to be skipped: starting the
    launcher must open the launcher, in Game Mode as everywhere else.
    """
    return in_gamescope_session(os.environ if environ is None else environ)
