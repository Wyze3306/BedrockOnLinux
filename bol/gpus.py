"""bol.gpus — which graphics card Minecraft renders on.

A laptop with an integrated GPU and a discrete one starts every program on
the integrated one unless it is told otherwise: that is what keeps the
battery alive, and it is also why Minecraft could run on an Intel iGPU beside
an idle RTX 3050 (#275). Windows asks the player once per game; Linux leaves
it to whoever starts the program, through environment variables that differ
by driver.

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
choice does.
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


def hybrid_gpu_problem(settings, environ=None, gpus=None):
    """Advice for a laptop about to start the game on its integrated GPU.

    Only for the unambiguous cases, with nothing chosen in Settings and
    nothing asked for by the desktop either: the display runs on a card that
    is not NVIDIA's and an NVIDIA card with its own driver sits beside it
    (Intel or AMD laptops with a GeForce), or the display runs on an Intel
    iGPU beside an AMD or Intel Arc card.
    """
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
    return (f"This computer has two graphics cards, and Minecraft starts on "
            f"the one your desktop runs on: the {boot.name}. Settings ▸ "
            f"Advanced ▸ Graphics card puts it on the {discrete.name} "
            f"instead.")
