"""bol.gamesetup — the do_setup() orchestration and post-mortem diagnose()."""
# SPDX-License-Identifier: MIT

import re
from pathlib import Path

from . import hostarch
from .auth import msa_signed_in
from .config import LOGS
from .deps import ensure_login_deps
from .fixups import (
    fix_curl_ssl,
    hide_signin_button,
    install_ca_bundle,
    install_gdk_xbox_dlls,
)
from .gameinput import install_gameinput
from .config import GAMES
from .games import (
    _auto_selection,
    _game_root,
    edition_exe,
    edition_for_dir,
    install_game,
    is_bedrock,
    list_versions,
    selected_edition,
    prune_legacy_game_archives,
    use_game_dir,
)
from .log import BolError, desktop_notify, info, ok, warn
from .prefix import (
    active_prefix,
    boot_prefix,
    ensure_umu,
    prefix_operation_lock,
    shared_assets_lock,
)
from .proton import ensure_proton
from .util import launcher_owned_overrides, load_settings, mkdirs
from .winegdk import ensure_winegdk

def do_setup(game_dir=None, mc_edition=None, mc_version=None, proton_tag=None,
             force=False, progress=None):
    """Install/update shared game, engine and prefix state exclusively."""
    with shared_assets_lock(
            "install or update BedrockOnLinux", exclusive=True), \
            prefix_operation_lock("install or update BedrockOnLinux"):
        return _do_setup(game_dir, mc_edition, mc_version, proton_tag,
                         force, progress)


# Off unless asked for: a server that has not updated yet refuses a newer
# client, so moving to a new build stays the player's decision by default.
AUTO_UPDATE_SETTING = "auto_update_game"


def update_game_before_play(always=False, setup=None, versions=None):
    """Move to the newest build of the edition being played (#328).

    For a start with no launcher window -- a desktop or Steam shortcut --
    where nobody is shown that a new build is out. Does nothing unless the
    setting is on or ``always`` is. Returns the build switched to, or None
    when the installed one is kept: it is already the newest, it is a copy
    from outside the managed folder, or the update could not be made. Never
    raises: whatever stops the update, the build already installed still
    starts.
    """
    settings = load_settings()
    if not (always or settings.get(AUTO_UPDATE_SETTING, False)):
        return None
    game_dir = settings.get("game_dir")
    try:
        managed = bool(game_dir) and Path(game_dir).resolve().is_relative_to(
            GAMES.resolve())
    except OSError:
        managed = False
    if not managed:
        return None
    try:
        edition, current = _auto_selection(settings)
        listed = (versions or list_versions)(edition["id"], offline_ok=False)
        newest = listed[0]["version"] if listed else None
        if not newest or newest == current:
            return None
        message = (f"Updating {edition['name']} from {current or '?'} to "
                   f"{newest} before it starts …")
        info(message)
        desktop_notify(message)
        (setup or do_setup)(mc_edition=edition, mc_version=newest)
    except Exception as exc:
        warn(f"Could not update Minecraft ({exc}); starting the build "
             "already installed.")
        return None
    return newest


def _build_root(folder):
    """The complete build in ``folder``, for whichever title it holds."""
    folder = Path(folder).expanduser()
    return _game_root(folder, edition_exe(edition_for_dir(folder)))


def _do_setup(game_dir=None, mc_edition=None, mc_version=None, proton_tag=None,
              force=False, progress=None):
    # Everything setup downloads is x86-64; say so before the first of them
    # fails to start with "Exec format error" (#250).
    arch_problem = hostarch.problem()
    if arch_problem:
        raise BolError(arch_problem)
    mkdirs()
    prune_legacy_game_archives()
    s = load_settings()
    ensure_login_deps()
    if mc_edition:
        use_game_dir(install_game(mc_edition, mc_version, progress,
                                  force=force))
    elif game_dir and _build_root(game_dir):
        use_game_dir(game_dir)
    cur = load_settings().get("game_dir")
    if not cur or not _build_root(cur):
        edition, version = _auto_selection(s)
        use_game_dir(install_game(edition, version, progress, force=force))
    gd = Path(load_settings()["game_dir"])
    # The DLLs the game folder gets are Bedrock's own fixes -- its XCurl and
    # libHttpClient, its sign-in button. Another title keeps what it shipped
    # with: those replacements were never tried on it.
    bedrock = is_bedrock(edition_for_dir(gd))
    if load_settings().get("proton_source") == "winegdk":
        ensure_winegdk(force, progress)
        if bedrock:
            install_gdk_xbox_dlls(gd)
    else:
        ensure_proton(proton_tag, force, progress)
    ensure_umu(force)
    if bedrock:
        fix_curl_ssl(gd)
    else:
        install_ca_bundle(gd)
    if not boot_prefix():
        raise BolError(
            "Could not initialise the Wine prefix, so setup stopped before "
            "installing GameInput. Check "
            f"{LOGS / 'native-login.log'} and re-run 'Install / Update'."
        )
    install_gameinput(active_prefix(), gd)
    if bedrock:
        hide_signin_button(gd)
    # Not "sign in in-game": that button reaches a sign-in the engine does
    # not implement, and telling people to use it is how #227/#228 were
    # filed. The account is linked here, and PLAY carries it into the
    # prefix for the game to pick up.
    ok("Setup complete — sign in from the launcher, then click PLAY.")


# Unlike every other cause below, this one means the game never started, so
# the launch reports it as its failure rather than only listing it (#280).
LICENCE_REFUSED = (
    "Microsoft refused the game licence: this account has reached its limit "
    "of ten Microsoft Store download devices. Remove the ones you no longer "
    "use at https://account.microsoft.com/devices/content, then click PLAY "
    "again.")


# The same kind of failure, reached without a network. A Store build stays
# encrypted on disk and xodus-cli fetches the key that decrypts it from
# Microsoft at every launch, so with no connection the game never starts --
# and all xodus-cli says is a Rust panic on login.live.com (#308):
#   thread 'main' panicked at crates/xodus-cli/src/license.rs:51:6:
#   called `Result::unwrap()` on an `Err` value: Request(reqwest::Error {
#   kind: Request, url: "https://login.live.com/RST2.srf", source: … dns error
LICENCE_UNREACHABLE = (
    "Minecraft could not start because its licence could not be fetched: no "
    "connection to Microsoft. A game downloaded from the Microsoft Store "
    "stays encrypted on disk, and Microsoft hands out the key that decrypts "
    "it each time it starts, so it needs an internet connection to start, "
    "single-player included. Connect to the internet and click PLAY again.")
# From 64d39eb87a56-p9 on, the exchange that needs the network first reports
# it instead of panicking:
#   could not exchange this device's Microsoft session: Error making a request
#   reqwest::Error { kind: Request, url: "https://login.live.com/RST2.srf",
#   source: … ConnectError("dns error", …
_LICENCE_UNREACHABLE = (
    r"(?:panicked at [^\n]*license\.rs[^\n]*\n|"
    r"could not exchange this device's Microsoft session: |"
    r"Microsoft session was refused again after signing it in afresh: )"
    r"[^\n]*"
    r"(?:dns error|failed to lookup address|ConnectError|Connect,|"
    r"timed out|Connection refused|Network is unreachable|"
    r"error sending request)")


# The licence request is signed with two Microsoft sessions the Store sign-in
# left in xodus-cli's keyring: this computer's, which Microsoft issues for 60
# days, and the account's, for 90. xodus-cli up to 64d39eb87a56-p8 renewed
# neither, and a refused device session ended in a panic before the game
# started (#367), which read "No known cause":
#   thread 'main' panicked at crates/xodus/src/api/live/mod.rs:79:14:
#   not implemented: Exchange token supports only singular token right now
#   Fault(Fault)
# Patches 0009 and 0010 renew both. What is left is a device session Microsoft
# refuses even freshly signed in, or an account session that ran out before
# the game was played in its last week, and the Store sign-in replaces both.
# Signing out keeps the device, so it does not cost one of the account's ten.
STORE_SESSION_REFUSED = (
    "Minecraft could not start because Microsoft no longer accepts the "
    "Microsoft Store sign-in the game's licence is fetched with at every "
    "start. Open the account menu, sign out of the Microsoft Store account "
    "(Download Minecraft), sign in again, then click PLAY.")
_STORE_SESSION_REFUSED = (
    r"Exchange token supports only singular token[^\n]*Fault|"
    r"Microsoft session was refused again after signing it in afresh|"
    r"not logged in: [^\n]*(?:in the keyring has expired|"
    r"is missing from the keyring)")


# The licensing service answered, and the answer was not a licence (#345).
# xodus-cli up to -p5 printed only that it could not decode the body; -p7
# names the HTTP status and Microsoft's reason. Either way the game never
# started, and the launcher came back as if PLAY had done nothing.
LICENCE_SERVICE_REFUSED = (
    "Microsoft's licensing service refused the licence Minecraft needs to "
    "start, so the game never opened")
_LICENCE_SERVICE_REFUSAL = re.compile(
    r"the licensing service refused the request \(([^)\n]{1,40})\):?[ \t]*"
    r"([^\n]{0,160})|"
    r"error decoding response body for url \(https://licensing\.mp\."
    r"microsoft\.com", re.I)


def _licence_service_refusal(text):
    """The hit for a licence the service refused, or None."""
    match = _LICENCE_SERVICE_REFUSAL.search(text)
    if not match:
        return None
    status, reason = match.group(1), (match.group(2) or "").strip()
    detail = f" ({status}{': ' + reason if reason else ''})" if status else ""
    return (f"{LICENCE_SERVICE_REFUSED}{detail}. In Settings ▸ Accounts, "
            "unlink the Store account and link it again with the account "
            "that owns Minecraft, then click PLAY. If the licence is refused "
            "again, update the launcher and attach minecraft.log to a bug "
            "report.")


# DRM devices, for telling a virtual GPU from a real one (#302).
DRM_SYSFS = Path("/sys/class/drm")


def _virtio_gpu(text=""):
    """Whether the game drew through a virtio-gpu device.

    That is what ChromeOS's Linux container and most virtual machines hand
    a guest: Mesa's Venus driver forwards Vulkan to the host through it.
    The log names it when vkd3d-proton or Mesa did; sysfs says so for every
    virtio-gpu device (PCI vendor 0x1af4) either way.
    """
    if re.search(r"\bVenus\b|virtio[-_ ]?gpu|\b1af4:10[0-9a-f]{2}\b",
                 text, re.I):
        return True
    for vendor in DRM_SYSFS.glob("card*/device/vendor"):
        try:
            if vendor.read_text().strip().lower() == "0x1af4":
                return True
        except OSError:
            continue
    return False


_DIAG_RULES = [
    (r"d3d12_command_signature_init_state_template_dgc_(?:ext|nv):.*"
     r"Cannot implement command signature|"
     r"d3d12_command_signature_create: Device generated commands is not "
     r"supported by implementation",
     "Minecraft's menu needs Vulkan Device Generated Commands (DGC), which "
     "this GPU/driver did not provide. Reinstall the engine first — it is "
     "the common cause and the safe fix ('bedrock-on-linux setup --force'). "
     "If that changes nothing, the GPU/driver itself cannot provide DGC: on "
     "an Intel discrete GPU it is exposed only under the 'xe' kernel driver, "
     "not 'i915'; otherwise update your Vulkan driver or use a DGC-capable "
     "GPU."),
    (r"Unimplemented function\s+combase\.dll\.RoOriginateErrorW?\b",
     "combase patch missing — re-run 'Install / Update'."),
    (r"Unimplemented function\s+ntdll\.dll\.NtQueryWnfStateData\b",
     "ntdll patch missing — re-run 'Install / Update'."),
    (r"Loading library user32\.dll[^\r\n]*failed \(error c0000020\)",
     "Wine user32.dll in the prefix could not be loaded (invalid image, "
     "status c0000020) — Install / Update repairs managed engine and prefix "
     "files automatically; custom prefixes need a matching Wine runtime."),
    (r"vkGetPhysicalDeviceSurfaceFormatsKHR|Can't open display|x11drv: Can't",
     "Display unavailable (no X/Wayland server)."),
    (r"VK_ERROR_DEVICE_LOST|device removed|DXGI_ERROR|vkd3d.*fatal|"
     r"VK_ERROR_OUT_OF_DEVICE_MEMORY",
     "GPU/Vulkan crash — update the driver or lower graphics."),
    (r"Cannot allocate memory|OutOfMemory|std::bad_alloc",
     "Out of memory (RAM/VRAM)."),
    (r"wineserver.*version mismatch|wineserver binary was not upgraded",
     "Broken WineGDK packaging (wine vs wineserver mismatch) — rebuild the "
     "engine: 'bedrock-on-linux setup --force'."),
    # Wine only prints these when it is about to hand the process to the crash
    # debugger, so they are the game's own fatal fault rather than a first-
    # chance exception. Without this rule the launcher reported "No known
    # cause" for a reproducible crash (issues #115, #116, #129, #132).
    (r"wine: Unhandled page fault on \w+ access to [0-9A-Fa-f]+ at address|"
     r"wine: Unhandled exception 0xc0000005",
     "Minecraft itself crashed with a memory access violation (unhandled page "
     "fault) — the launcher, the Wine prefix and the GPU driver are not the "
     "failing component, so repairing the prefix does not change it. Note the "
     "faulting address and the engine revision from proton.log, try another "
     "Minecraft version, and attach both to a bug report."),
    (r"Authentication failed|invalid_grant|login.*failed",
     "Microsoft sign-in failed in-game — sign in again "
     "(open the link, enter the code)."),
    # An encrypted build is licensed to a device at every launch, so this is
    # a launch failure and not only a download one. The account is out of
    # Store devices, and where they are given back is not in the message.
    (r"Device group is full", LICENCE_REFUSED),
    (_LICENCE_UNREACHABLE, LICENCE_UNREACHABLE),
    (_STORE_SESSION_REFUSED, STORE_SESSION_REFUSED),
    (r"\bInitialConnection[-_: ]*13(?!\d)",
     "LAN InitialConnection-13 — check that the host firewall allows "
     "Minecraft's inbound RakNet UDP 19132 and, on Windows, that the host "
     "network profile is Private."),
    (r"\bInitialConnection[-_: ]*25(?!\d)",
     "LAN InitialConnection-25 ('world full') — the host may have a stale "
     "NetherNet/RakNet session rather than a real capacity limit. On a "
     "Windows host, change its network profile from Public to Private, then "
     "toggle Multiplayer Game off/on and fully restart both games. This "
     "requires the host owner; otherwise use a correctly configured Bedrock "
     "Dedicated Server."),
    (r"\b(?:IncompatibleVersion|VersionMismatch|ProtocolVersionMismatch)\b|"
     r"\b(?:outdated client|outdated server)\b|"
     r"\bversion\s+mismatch\b|"
     r"\b(?:client|server|host)\s+(?:build|version)\b[^\r\n]{0,100}"
     r"\b(?:does not match|mismatch|incompatible)\b",
     "Minecraft client/host version mismatch — select exactly the host's "
     "Bedrock version/build before joining."),
    # Must come BEFORE the nodrv_CreateWindow rule: when SystemFunction036 is
    # unresolved, every Wine service and explorer.exe abort on RtlGenRandom, and
    # the *symptom* is a nodrv_CreateWindow / "explorer failed to start". That is
    # NOT a broken prefix or a GPU fault, so resetting the prefix can't fix it.
    (r"unimplemented function advapi32\.dll\.SystemFunction036|"
     r"forward 'cryptbase\.SystemFunction036'|"
     r"module not found for forward 'cryptbase",
     "Wine RNG unresolved (cryptbase.SystemFunction036) — re-run "
     "'Install / Update'; setup seeds the verified native RNG before wineboot "
     "and reinstalls it once if the prefix still aborts. Connect to the "
     "network for that download if the message persists."),
    (r"nodrv_CreateWindow|no driver could be loaded|"
     r"explorer process failed to start",
     "Wine prefix broken (Wine couldn't open a window)."),
    # Old GDK-Proton without the WineGDK XUser fork → xgameruntime stubs the
    # XUser calls. Require the xgameruntime/XUser context: a bare 0x80004001 /
    # E_NOTIMPL also appears in benign WindowsAppRuntime bootstrapper messages
    # ("Bootstrapper initialization failed looking for version 1.8"), and the
    # diagnose() guard below also drops this hit when the engine's own XUser
    # patches are in the log.
    (r"(?:XUserAddAsync|xgameruntime:.*XUser\w*).*"
     r"(?:unimpl|stub|not implemented|E_NOTIMPL|0x80004001)|"
     r"QueryApiImpl.*(?:unimpl|stub|not implemented|E_NOTIMPL|0x80004001)",
     "The GDK-Proton in use has no WineGDK XUser — reinstall the engine: "
     "'bedrock-on-linux setup --force'."),
]


def diagnose():
    """Scan game logs and surface a likely cause."""
    blobs = []
    for p in (LOGS / "proton.log", LOGS / "minecraft.log",
              LOGS / "winegdk-build.log"):
        if p.exists():
            try:
                # Initialisation proof is near the beginning of Proton's log,
                # while a crash is near the end. Diagnostic tracing can make
                # the file hundreds of MiB, so retain both edges without
                # loading the entire file merely to discard its middle.
                edge = 200000
                size = p.stat().st_size
                with p.open("rb") as stream:
                    if size <= edge * 2:
                        raw = stream.read()
                    else:
                        raw = stream.read(edge)
                        stream.seek(-edge, 2)
                        raw += b"\n[... log middle omitted ...]\n"
                        raw += stream.read(edge)
                blobs.append(raw.decode("utf-8", "ignore"))
            except Exception:
                pass
    text = "\n".join(blobs)
    hits = [msg for pat, msg in _DIAG_RULES if re.search(pat, text, re.I)]
    # The game never ran: xodus-cli died fetching the licence, and its panic
    # names login.live.com right before "failed to lookup address", which
    # the in-game sign-in rule reads as a failed sign-in (#308).
    if LICENCE_UNREACHABLE in hits or STORE_SESSION_REFUSED in hits:
        hits = [h for h in hits if not h.startswith(
            "Microsoft sign-in failed in-game")]
    # No answer at all is not a refusal: signing in again needs the network
    # that is missing.
    if LICENCE_UNREACHABLE in hits:
        hits = [h for h in hits if h != STORE_SESSION_REFUSED]
    # A full device group is refused through the same service, and that
    # message already says what to do about it.
    refusal = _licence_service_refusal(text)
    if refusal and LICENCE_REFUSED not in hits:
        hits.append(refusal)
    # Positive evidence wins: if the engine logged its XUser patches or a
    # successful pre-auth, WineGDK XUser IS present — drop any "no XUser" hit so
    # a benign HRESULT elsewhere can't tell the user to reinstall a working
    # engine.
    if re.search(r"InitializeApiImplEx2 patched|preauth: loaded user/XSTS", text):
        hits = [h for h in hits if "no WineGDK XUser" not in h]
    if re.search(r"preauth: loaded user/XSTS", text, re.I) and not re.search(
            r"native XGame identity loaded", text, re.I):
        hits.append("Xbox credentials loaded, but the native XGame identity "
                    "did not initialize. Reinstall/update the managed engine "
                    "and attach the diagnostics log if online tabs stay locked.")
    # Only report software rendering when every enumerated device is llvmpipe.
    devices = re.findall(r"Found device:\s*(.+)", text)
    software_only = bool(
        devices and all("llvmpipe" in device.lower() for device in devices)
    )
    if software_only:
        hits.append("Running on software rendering (llvmpipe) — your GPU's "
                    "Vulkan driver isn't active, so the game runs on the CPU "
                    "(slow, and the Play screen may render black). Reboot, or "
                    "(re)install/enable your GPU's Vulkan drivers.")
    # WineD3D is relevant only when a real adapter lacks Vulkan 1.3.
    lacks_vulkan_13 = re.search(
        r"Skipping:\s*Device does not support Vulkan 1\.3|"
        r"A Vulkan 1\.3 capable setup is required",
        text,
        re.I,
    )
    no_dxvk_adapter = re.search(
        r"DXVK:\s*No adapters found|Failed to initialize DXVK",
        text,
        re.I,
    )
    # A Chromebook got from "Game closed (exit 84)", the access violation
    # above, to a world by turning the legacy renderer on (#302): Venus
    # forwards Vulkan to the host, and Minecraft's Direct3D 12 path through
    # vkd3d-proton did not survive it.
    crashed = any("memory access violation" in hit for hit in hits)
    if crashed and _virtio_gpu(text):
        hits.append("This system draws through a virtual GPU (virtio-gpu, as "
                    "in ChromeOS's Linux container or a virtual machine), "
                    "which Minecraft's Direct3D 12 renderer can crash on. "
                    "Turn on Settings ▸ Advanced ▸ Legacy compatibility "
                    "renderer: on a Chromebook, that is what got the game "
                    "running. It costs visual quality and ray tracing.")
    if lacks_vulkan_13 and no_dxvk_adapter and not software_only:
        hits.append("DXVK found no usable Vulkan 1.3 adapter — choose the "
                    "Legacy compatibility renderer (renderer=opengl) in "
                    "Settings for GPUs whose Vulkan driver cannot provide "
                    "Vulkan 1.3. It is a last resort: it swaps the whole "
                    "Direct3D stack, D3D12 included, to WineD3D, so both "
                    "DXVK and vkd3d-proton are dropped. Minecraft renders "
                    "exclusively through D3D12, so expect visual artifacts "
                    "and no ray tracing.")
    # Check the field itself, not the log: an override that breaks the launch
    # can leave nothing in the log to match on, which is how issue #134 ended
    # in three full reinstalls.
    overrides = launcher_owned_overrides(load_settings().get("custom_env"))
    if overrides:
        hits.append("Custom environment variables override launcher "
                    "settings: %s. Clear the Advanced custom-environment "
                    "field and launch again before reinstalling anything — "
                    "PROTON_USE_WINED3D forces the legacy renderer, which "
                    "Minecraft's menu can crash on. Use the Renderer setting "
                    "instead." % ", ".join(overrides))
    if not msa_signed_in():
        played = selected_edition(load_settings())
        if is_bedrock(played):
            hits.append("No Microsoft account linked — the game runs, but in "
                        "offline mode: single-player worlds and LAN play "
                        "only. Click 'Sign in' before PLAY for Realms, "
                        "servers, the Marketplace and Xbox friends.")
        else:
            hits.append(f"No Microsoft account linked — {played['name']} "
                        "cannot sign in to Xbox Live. Click 'Sign in' "
                        "before PLAY.")
    if hits:
        warn("Likely cause:")
        for h in dict.fromkeys(hits):
            warn("  • " + h)
    else:
        info(f"No known cause. Logs: {LOGS}")
    return hits
