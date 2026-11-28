"""bol.gpus — which graphics card Minecraft renders on.

A laptop with an integrated GPU and a discrete one runs its desktop on the
integrated one: that is what keeps the battery alive. Windows asks the player
once per game which card to use; Linux leaves it to whoever starts the
program, through environment variables that differ by driver.

What Minecraft gets on *Automatic* depends on the renderer. Its default path,
D3D12 through vkd3d-proton behind DXVK's DXGI, renders on DXGI's first
adapter, and DXVK ranks a discrete card ahead of an integrated one: on a
laptop the game is on the GeForce or the Radeon as long as that card's Vulkan
driver is there (measured with tools/dxr-probe.c, and in DXVK's
``DxvkInstance::enumAdapters``). The legacy WineD3D renderer draws through
OpenGL instead, which starts on the card the display runs on, so there the
integrated GPU is what *Automatic* means. Seeing both cards busy while the
game runs is normal either way on a laptop whose screen is wired to the
integrated chip: it copies every frame the discrete card draws to the
screen (#333).

The inventory comes from sysfs, like bol.dgc: no Vulkan or OpenGL call is
made to find out which cards exist, so reading it can never be what hangs a
broken driver. Picking one is three independent mechanisms, because which of
them a host has is a property of the host:

* Mesa's device-select layer (most distributions install it with their
  Vulkan drivers) honours ``MESA_VK_DEVICE_SELECT`` — the trailing ``!`` makes
  it expose that card alone, so neither DXVK nor vkd3d-proton can sort another
  one ahead of it — and ``DRI_PRIME`` for OpenGL;
* NVIDIA's own VK_LAYER_NV_optimus ships with the proprietary driver and is
  enabled by ``__NV_PRIME_RENDER_OFFLOAD=1``; ``__VK_LAYER_NV_optimus`` then
  keeps only the NVIDIA card. Its ``non_NVIDIA_only`` is not used: on a
  desktop whose display is on the NVIDIA card it left that card listed,
  where Mesa's filter does hide it;
* GLVND picks NVIDIA's GLX for ``__GLX_VENDOR_LIBRARY_NAME=nvidia``.

These are the variables GNOME and KDE set for "Launch using Discrete
Graphics Card", so a card chosen here behaves the way the desktop's own
choice does. All of them reach the game: pressure-vessel keeps them, and
imports the host's Mesa and NVIDIA layers with its drivers.

What it does not keep is a driver list naming files it cannot see.
``VK_DRIVER_FILES`` (or ``VK_ICD_FILENAMES``) restricts the Vulkan loader to
the files listed, and pressure-vessel imports exactly those; one that is not
there — ``/usr/share/vulkan/icd.d/nvidia_icd.json`` inside the Flatpak,
whose drivers live in its GL extensions — leaves the game with no Vulkan
driver at all, and it closes at start (#333). ``drop_missing_driver_files``
takes such entries out.

Which card the game really rendered on is written down by the engine
itself: its winevulkan names the device of every ``vkCreateDevice`` on a
debug channel of its own, ``vkdevice``, which the launcher turns on at every
launch, and ``rendered_on`` reads that line back for Doctor.
"""
# SPDX-License-Identifier: MIT

import os
import re
from dataclasses import dataclass
from pathlib import Path

DRM = Path("/sys/class/drm")
PCI_IDS = (Path("/usr/share/hwdata/pci.ids"), Path("/usr/share/misc/pci.ids"),
           Path("/usr/share/pci.ids"))

# The setting holding the chosen card's PCI address; absent or "auto" leaves
# the choice to the system.
SETTING = "gpu"
AUTO = "auto"

_VENDORS = {"10de": "NVIDIA", "8086": "Intel", "1002": "AMD", "1af4": "Virtio"}

# Integrated by construction: Intel's iGPUs, except the Arc discrete cards
# (DG2 and Battlemage, device IDs 0x56xx and 0xe2xx).
_INTEL_DISCRETE = re.compile(r"^(56|e2)", re.I)


@dataclass(frozen=True)
class Gpu:
    slot: str          # PCI address, "0000:01:00.0"
    vendor: str        # "10de"
    device: str        # "25a2"
    driver: str        # kernel driver: "nvidia", "i915", "xe", "amdgpu", …
    boot_vga: bool     # the card the firmware brought the display up on
    name: str          # what to call it in a window

    @property
    def nvidia(self):
        return self.driver == "nvidia"

    @property
    def integrated(self):
        """Intel's iGPUs; an AMD APU cannot be told apart from sysfs alone."""
        return (self.vendor == "8086"
                and not _INTEL_DISCRETE.match(self.device))


def _read(path):
    try:
        return Path(path).read_text(errors="replace").strip()
    except OSError:
        return ""


def _pci_names(vendor, device, pci_ids=None):
    """Vendor and device names from pci.ids, or ("", "")."""
    for candidate in ([Path(pci_ids)] if pci_ids else PCI_IDS):
        try:
            handle = open(candidate, encoding="utf-8", errors="replace")
        except OSError:
            continue
        vendor_name = ""
        with handle:
            for line in handle:
                if not line.strip() or line.startswith("#"):
                    continue
                if not line.startswith("\t"):
                    if vendor_name:
                        # Past our vendor's block: its device is not listed.
                        return vendor_name, ""
                    if line[:4].lower() == vendor:
                        vendor_name = line[4:].strip()
                    continue
                if vendor_name and not line.startswith("\t\t") \
                        and line[1:5].lower() == device:
                    return vendor_name, line[5:].strip()
        if vendor_name:
            return vendor_name, ""
    return "", ""


def _display_name(vendor, device, pci_ids=None):
    short = _VENDORS.get(vendor, "")
    vendor_name, device_name = _pci_names(vendor, device, pci_ids)
    # "GA107M [GeForce RTX 3050 Mobile]": the bracket is the marketing name.
    bracket = re.search(r"\[([^\]]+)\]", device_name)
    model = bracket.group(1) if bracket else device_name
    if model:
        return f"{short or vendor_name.split()[0]} {model}".strip()
    return f"{short or 'GPU'} graphics ({vendor}:{device})"


def list_gpus(drm_root=None, pci_ids=None):
    """Every PCI graphics card the kernel drives, boot display first."""
    root = Path(drm_root) if drm_root is not None else DRM
    found = {}
    try:
        cards = sorted(root.glob("card[0-9]*"))
    except OSError:
        return []
    for card in cards:
        if "-" in card.name:
            continue                     # a connector, card0-DP-1
        device = card / "device"
        vendor = _read(device / "vendor").lower().removeprefix("0x")
        model = _read(device / "device").lower().removeprefix("0x")
        if not vendor or not model:
            continue                     # simpledrm and other non-PCI nodes
        try:
            slot = os.path.basename(os.path.realpath(device))
            driver = os.path.basename(os.path.realpath(device / "driver"))
        except OSError:
            continue
        if slot in found or not re.match(r"^[0-9a-f]{4}:", slot, re.I):
            continue
        found[slot] = Gpu(
            slot=slot, vendor=vendor, device=model,
            driver=driver if (device / "driver").exists() else "",
            boot_vga=_read(device / "boot_vga") == "1",
            name=_display_name(vendor, model, pci_ids))
    return sorted(found.values(), key=lambda g: (not g.boot_vga, g.slot))


def chosen_gpu(settings, gpus):
    """The card the settings pick, or None for the system's own choice."""
    slot = str((settings or {}).get(SETTING) or AUTO).strip()
    if slot == AUTO:
        return None
    return next((g for g in gpus if g.slot == slot), None)


def gpu_env(gpu):
    """The environment that puts Vulkan and OpenGL on ``gpu``."""
    env = {
        "MESA_VK_DEVICE_SELECT": f"{gpu.vendor}:{gpu.device}!",
        "DRI_PRIME": "pci-" + re.sub(r"[:.]", "_", gpu.slot),
    }
    if gpu.nvidia:
        env.update({
            "__NV_PRIME_RENDER_OFFLOAD": "1",
            "__VK_LAYER_NV_optimus": "NVIDIA_only",
            "__GLX_VENDOR_LIBRARY_NAME": "nvidia",
        })
    return env


# Set by a desktop's "Launch using Discrete Graphics Card" or by hand; a card
# chosen here replaces whatever they said.
GPU_ENV_KEYS = ("MESA_VK_DEVICE_SELECT", "DRI_PRIME", "__NV_PRIME_RENDER_OFFLOAD",
                "__VK_LAYER_NV_optimus", "__GLX_VENDOR_LIBRARY_NAME")


def apply_gpu_choice(env, settings, gpus=None):
    """Put the game on the card chosen in Settings; return that card.

    None, and ``env`` untouched, when the choice is the system's. A card that
    is no longer there (an eGPU unplugged) is the same as no choice, said
    once so a slow game is not a mystery.
    """
    gpus = list_gpus() if gpus is None else gpus
    slot = str((settings or {}).get(SETTING) or AUTO).strip()
    gpu = chosen_gpu(settings, gpus)
    if gpu is None:
        if slot != AUTO:
            from .log import warn
            warn(f"The graphics card chosen in Settings ({slot}) is not in "
                 "this computer any more; Minecraft starts on the system's "
                 "default one.")
        return None
    for key in GPU_ENV_KEYS:
        env.pop(key, None)
    env.update(gpu_env(gpu))
    return gpu


def hybrid_gpu_problem(settings, environ=None, gpus=None, renderer=None):
    """Advice for a laptop about to start the game on its integrated GPU.

    Only where that is what happens: on the legacy WineD3D renderer
    (``renderer`` "wined3d"), which draws through OpenGL on the card the
    display runs on. The default D3D12 path already takes a discrete card
    over an integrated one, so there is nothing to say there. Then only for
    the unambiguous cases, with nothing chosen in Settings and nothing asked
    for by the desktop either: the display runs on a card that is not
    NVIDIA's and an NVIDIA card with its own driver sits beside it (Intel or
    AMD laptops with a GeForce), or the display runs on an Intel iGPU beside
    an AMD or Intel Arc card.
    """
    if renderer != "wined3d":
        return None
    source = os.environ if environ is None else environ
    gpus = list_gpus() if gpus is None else gpus
    if chosen_gpu(settings, gpus) is not None or len(gpus) < 2:
        return None
    if any(str(source.get(key, "")).strip() for key in GPU_ENV_KEYS):
        return None
    boot = next((g for g in gpus if g.boot_vga), None)
    if boot is None:
        return None
    others = [g for g in gpus if not g.boot_vga]
    discrete = None
    if not boot.nvidia:
        discrete = next((g for g in others if g.nvidia), None)
    if discrete is None and boot.integrated:
        discrete = next((g for g in others if not g.integrated
                         and g.driver in ("amdgpu", "xe", "i915")), None)
    if discrete is None:
        return None
    return (f"This computer has two graphics cards, and the Legacy renderer "
            f"starts Minecraft on the one your desktop runs on: the "
            f"{boot.name}. Settings ▸ Advanced ▸ Graphics card puts it on the "
            f"{discrete.name} instead.")


# The Vulkan loader's driver lists: VK_DRIVER_FILES and its older name
# replace the drivers it would find, VK_ADD_DRIVER_FILES adds to them. Each
# is a list of JSON manifests or folders of them, ":"-separated.
DRIVER_FILE_KEYS = ("VK_DRIVER_FILES", "VK_ICD_FILENAMES",
                    "VK_ADD_DRIVER_FILES")


def drop_missing_driver_files(env, exists=os.path.exists):
    """Take the Vulkan driver files that are not there out of ``env``.

    pressure-vessel imports the drivers these variables name, from where
    the launcher runs, and nothing else: an entry it cannot open is a
    driver the game does not get, and a list with none left gives it no
    Vulkan at all. Returns the entries taken out, by variable; a variable
    left with nothing is removed.
    """
    dropped = {}
    for key in DRIVER_FILE_KEYS:
        value = env.get(key)
        if value is None:
            continue
        entries = [entry for entry in str(value).split(":") if entry.strip()]
        missing = [entry for entry in entries if not exists(entry)]
        if not missing:
            continue
        dropped[key] = missing
        kept = [entry for entry in entries if entry not in missing]
        if kept:
            env[key] = ":".join(kept)
        else:
            env.pop(key, None)
    return dropped


def missing_driver_files_problem(dropped, flatpak=False):
    """What to tell the player about ``drop_missing_driver_files``."""
    if not dropped:
        return None
    names = "; ".join(f"{key}: {', '.join(entries)}"
                      for key, entries in dropped.items())
    where = (" Inside the Flatpak the system's graphics drivers are not at "
             "their usual place: they come from its GL extensions."
             if flatpak else "")
    return (f"Left out Vulkan driver files that do not exist here ({names})."
            f"{where} With them, Minecraft would start with no graphics "
            "driver and close. To choose the graphics card, use Settings ▸ "
            "Advanced ▸ Graphics card.")


# ---------------------------------------------------------------- after a launch

# The engine's winevulkan, at every vkCreateDevice, on its own debug channel:
#   0118:011c:trace:vkdevice:vkCreateDevice "NVIDIA GeForce RTX 4060",
#   vendor 0x10de, device 0x2882, driver 0x89b28100.
_RENDERED_ON = re.compile(
    rb'trace:vkdevice:[^ ]* "((?:[^"\\]|\\.)*)", vendor (0x[0-9a-f]+|0), '
    rb'device (0x[0-9a-f]+|0)', re.I)
_LOG_NAMES = ("minecraft.log", "proton.log")
# Device creation happens in a launch's first seconds.
_MAX_READ = 1 << 20


def rendered_on(logs_dir=None):
    """The graphics cards the last logged launch created a device on.

    A list of ``(name, "vendor:device")`` in order of first use, empty when
    no launch log says: none yet, or an engine that does not log it.
    """
    if logs_dir is None:
        from .config import LOGS
        logs_dir = LOGS
    directory = Path(logs_dir)
    candidates = []
    for name in _LOG_NAMES:
        path = directory / name
        try:
            candidates.append((path.stat().st_mtime, path))
        except OSError:
            continue
    for _, path in sorted(candidates, reverse=True):
        try:
            with path.open("rb") as handle:
                head = handle.read(_MAX_READ)
        except OSError:
            continue
        found = []
        for match in _RENDERED_ON.finditer(head):
            name = match.group(1).decode("utf-8", "replace")
            name = name.replace('\\"', '"').replace("\\\\", "\\")
            ids = "%04x:%04x" % (int(match.group(2), 16),
                                 int(match.group(3), 16))
            if (name, ids) not in found:
                found.append((name, ids))
        if found:
            return found
    return []


def rendered_on_summary(logs_dir=None, gpus=None):
    """Doctor's "rendered on" line: the card the game last drew with."""
    devices = rendered_on(logs_dir)
    if not devices:
        return ("unknown (play once with an engine that reports it, then "
                "run this again)")
    gpus = list_gpus() if gpus is None else gpus
    names = []
    for name, ids in devices:
        card = next((g for g in gpus if f"{g.vendor}:{g.device}" == ids),
                    None)
        role = ""
        if card is not None and len(gpus) > 1:
            role = (" — the integrated GPU" if card.integrated
                    else " — the card the display runs on" if card.boot_vga
                    else " — the discrete GPU")
        names.append(f"{name} [{ids}]{role}")
    return "; ".join(names)
