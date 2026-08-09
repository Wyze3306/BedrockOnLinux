"""bol.gamesetup — the do_setup() orchestration and post-mortem diagnose()."""
# SPDX-License-Identifier: MIT

import re
from pathlib import Path

from .auth import msa_signed_in
from .config import LOGS
from .deps import ensure_login_deps
from .fixups import fix_curl_ssl, hide_signin_button, install_gdk_xbox_dlls
from .gameinput import install_gameinput
from .games import _auto_mc_version, _game_root, download_game, use_game_dir
from .log import BolError, info, ok, warn
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

def do_setup(game_dir=None, mc_ver=None, proton_tag=None, force=False,
             progress=None):
    """Install/update shared game, engine and prefix state exclusively."""
    with shared_assets_lock(
            "install or update BedrockOnLinux", exclusive=True), \
            prefix_operation_lock("install or update BedrockOnLinux"):
        return _do_setup(game_dir, mc_ver, proton_tag, force, progress)


def _do_setup(game_dir=None, mc_ver=None, proton_tag=None, force=False,
              progress=None):
    mkdirs()
    s = load_settings()
    ensure_login_deps()
    if mc_ver:
        use_game_dir(download_game(mc_ver, progress, force=force))
    elif game_dir and _game_root(Path(game_dir).expanduser()):
        use_game_dir(game_dir)
    cur = load_settings().get("game_dir")
    if not cur or not _game_root(Path(cur)):
        use_game_dir(download_game(_auto_mc_version(s), progress, force=force))
    gd = Path(load_settings()["game_dir"])
    if load_settings().get("proton_source") == "winegdk":
        ensure_winegdk(force, progress)
        install_gdk_xbox_dlls(gd)
    else:
        ensure_proton(proton_tag, force, progress)
    ensure_umu(force)
    fix_curl_ssl(gd)
    if not boot_prefix():
        raise BolError(
            "Could not initialise the Wine prefix, so setup stopped before "
            "installing GameInput. Check "
            f"{LOGS / 'native-login.log'} and re-run 'Install / Update'."
        )
    install_gameinput(active_prefix(), gd)
    hide_signin_button(gd)
    ok("Setup complete — click PLAY, then sign in to Microsoft in-game.")


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
        hits.append("No Microsoft account linked — click 'Sign in' "
                    "before PLAY.")
    if hits:
        warn("Likely cause:")
        for h in dict.fromkeys(hits):
            warn("  • " + h)
    else:
        info(f"No known cause. Logs: {LOGS}")
    return hits
