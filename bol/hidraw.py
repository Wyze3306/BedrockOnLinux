"""bol.hidraw - keep non-controller hidraw devices out of Wine's HID stack.

winebus opens every ``/dev/hidraw`` node the user can open except keyboards,
touchscreens and the controllers it would rather take through SDL: a mouse or
a vendor-defined collection is enough to be taken over. Most hidraw nodes are
root-only, but the ones that ship ``uaccess`` udev rules for their Linux
tools are not -- AIO coolers, fan and RGB controllers, a receiver's HID++
channel, headsets.

Some of those send input reports longer than their own report descriptor
declares (an NZXT 1e71:1714 fan controller declares 21 bytes and sends 64),
and Wine trusts the descriptor twice: winebus's ``deliver_next_report``
copies the whole report into a read buffer hidclass sized from the
descriptor's largest input report, and hidclass's ``hid_device_queue_input``
copies it again into a per-collection buffer of the declared size. The
corrupted heap crashes ``winedevice.exe`` on every start, and that process
hosts the entire HID stack -- hidclass, winebus and Wine's virtual mouse and
keyboard with them. GameInput then has no mouse at all: no hover, no clicks,
while the keyboard, which does not depend on that stack, keeps working.

Minecraft needs none of these devices. The mouse and keyboard reach it
through Wine's own virtual HID devices, and controllers are left exactly as
Wine would have them. Every other device Wine could open gets winebus's
per-device option ``Devices\\<vid>/<pid>`` → ``Hidraw`` = 0, so it never does.
``BOL_HIDRAW=all`` restores Wine's default for those devices.

PlayStation pads get the same option for the opposite reason (#57). winebus
prefers hidraw for every DualShock 4 and DualSense, and over hidraw the game
is handed Sony's own HID report, which GameInput does not take for a gamepad:
the touchpad moves the mouse and nothing else does anything. With hidraw off
for them, winebus takes them through SDL, which maps them to an XInput
gamepad -- and when Steam Input runs, its virtual pad is the only one the game
sees. Proton's wine is told this with ``PROTON_DISABLE_HIDRAW``; the engine's
winebus is WineGDK's and reads only this registry option.
"""
# SPDX-License-Identifier: MIT

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from .log import info
from .wine_registry import (
    RegistryChange, reg_delete, reg_dword, update_prefix_registry)

SYSFS_HIDRAW = Path("/sys/class/hidraw")
DEV = Path("/dev")
# winebus.sys reads per-device options below its own service key: a subkey
# named "<vid>[/<pid>]" in hex holding a REG_DWORD "Hidraw" (0 = never open).
WINEBUS_DEVICES_KEY = r"System\CurrentControlSet\Services\winebus\Devices"

Usage = Tuple[int, int]      # (usage page, usage id)
DeviceId = Tuple[int, int]   # (vendor id, product id)

# Top-level application collections that mark a game controller: the
# Generic Desktop joystick, gamepad and multi-axis controller winebus itself
# treats as controllers, and the Simulation and Game Controls pages worn only
# by wheels, pedals, flight sticks and the like.
_CONTROLLER_USAGES = frozenset({(0x01, 0x04), (0x01, 0x05), (0x01, 0x08)})
_CONTROLLER_PAGES = frozenset({0x02, 0x05})
# Valve's Steam Controller and Steam Deck present vendor-defined collections
# but are controllers that Steam Input drives through hidraw.
_CONTROLLER_VENDORS = frozenset({0x28DE})
# The pads winebus's is_dualshock4_gamepad() and is_dualsense_gamepad() send
# to hidraw.
PLAYSTATION_PADS: Tuple[DeviceId, ...] = (
    (0x054C, 0x05C4),  # DualShock 4
    (0x054C, 0x09CC),  # DualShock 4 v2
    (0x054C, 0x0BA0),  # DualShock 4 wireless adapter
    (0x054C, 0x0CE6),  # DualSense
    (0x054C, 0x0DF2),  # DualSense Edge
)


def application_usages(descriptor: bytes) -> List[Usage]:
    """Top-level application collections declared by a HID report descriptor.

    Only what classifies a device is decoded: Usage Page (with Push and Pop),
    Usage -- including 32-bit extended usages that carry their own page --
    and the collection nesting. Long items are skipped, and a truncated
    descriptor yields whatever was complete before the cut.
    """
    usages: List[Usage] = []
    page, pages, local, depth = 0, [], [], 0
    i, end = 0, len(descriptor)
    while i < end:
        prefix = descriptor[i]
        if prefix == 0xFE:  # long item: data size, tag, data
            if i + 1 >= end:
                break
            i += 3 + descriptor[i + 1]
            continue
        size = (0, 1, 2, 4)[prefix & 0x03]
        if i + 1 + size > end:
            break
        data = int.from_bytes(descriptor[i + 1:i + 1 + size], "little")
        i += 1 + size
        tag = prefix & 0xFC
        if tag == 0x04:            # Usage Page
            page = data
        elif tag == 0xA4:          # Push
            pages.append(page)
        elif tag == 0xB4:          # Pop
            page = pages.pop() if pages else page
        elif tag == 0x08:          # Usage
            local.append((data >> 16, data & 0xFFFF) if size == 4
                         else (page, data))
        elif tag == 0xA0:          # Collection
            if depth == 0 and data == 0x01 and local:
                usages.append(local[0])
            depth += 1
            local = []
        elif tag == 0xC0:          # End Collection
            depth = max(depth - 1, 0)
            local = []
        elif tag in (0x80, 0x90, 0xB0):  # Input / Output / Feature
            local = []
    return usages


def _hid_id(uevent: str) -> Optional[DeviceId]:
    """``(vid, pid)`` from a HID device's ``HID_ID=bus:vid:pid`` uevent line."""
    for line in uevent.splitlines():
        if line.startswith("HID_ID="):
            try:
                _bus, vid, pid = line[len("HID_ID="):].split(":")
                return int(vid, 16) & 0xFFFF, int(pid, 16) & 0xFFFF
            except ValueError:
                return None
    return None


def hidraw_devices(root: Path = SYSFS_HIDRAW,
                   dev: Path = DEV) -> Dict[DeviceId, List[Usage]]:
    """Top-level usages of every hidraw node Wine could open, per VID/PID.

    winebus opens a node read-write as the user, so a node the user cannot
    open is not Wine's to crash on and is skipped, as is one whose sysfs
    entries cannot be read. A composite device has one node per interface
    while winebus keys its per-device option by VID/PID, so the interfaces
    are merged.
    """
    devices: Dict[DeviceId, List[Usage]] = {}
    try:
        nodes = sorted(root.iterdir())
    except OSError:
        return devices
    for node in nodes:
        if not os.access(dev / node.name, os.R_OK | os.W_OK):
            continue
        device = node / "device"
        try:
            ident = _hid_id((device / "uevent").read_text(errors="replace"))
            descriptor = (device / "report_descriptor").read_bytes()
        except OSError:
            continue
        if ident is not None:
            devices.setdefault(ident, []).extend(application_usages(descriptor))
    return devices


def is_controller(vid: int, usages: Iterable[Usage]) -> bool:
    if vid in _CONTROLLER_VENDORS:
        return True
    return any(usage in _CONTROLLER_USAGES or usage[0] in _CONTROLLER_PAGES
               for usage in usages)


def non_controllers(devices: Mapping[DeviceId, List[Usage]]) -> List[DeviceId]:
    """Devices to keep off hidraw: classified, and not a game controller.

    A device whose descriptor declared no application collection is left to
    Wine, since nothing says what it is.
    """
    return sorted(ident for ident, usages in devices.items()
                  if usages and not is_controller(ident[0], usages))


def _device_key(ident: DeviceId) -> str:
    vid, pid = ident
    return WINEBUS_DEVICES_KEY + "\\" + f"{vid:04x}/{pid:04x}"


def hidraw_option_changes(idents: Iterable[DeviceId],
                          restore: bool = False) -> List[RegistryChange]:
    """winebus ``Hidraw`` = 0 per device, or its removal when ``restore``."""
    return [reg_delete(_device_key(ident), "Hidraw") if restore
            else reg_dword(_device_key(ident), "Hidraw", 0)
            for ident in idents]


def _names(idents: Iterable[DeviceId]) -> str:
    return ", ".join(f"{vid:04x}:{pid:04x}" for vid, pid in idents)


def keep_non_controllers_off_hidraw(prefix: Path, root: Path = SYSFS_HIDRAW,
                                    dev: Path = DEV,
                                    environ: Optional[Mapping[str, str]] = None
                                    ) -> List[DeviceId]:
    """Stop Wine opening the non-controller hidraw devices it could open now.

    Writes the stopped prefix directly, like the GameInput registry, so it
    must run before Wine starts. Returns the devices covered; with
    ``BOL_HIDRAW=all`` it instead removes the option for them and returns [].
    """
    env = os.environ if environ is None else environ
    idents = non_controllers(hidraw_devices(root, dev))
    if not idents:
        return []
    if env.get("BOL_HIDRAW", "").strip().lower() == "all":
        update_prefix_registry(
            prefix, machine=hidraw_option_changes(idents, restore=True))
        return []
    if update_prefix_registry(prefix, machine=hidraw_option_changes(idents)):
        info(f"Keeping non-controller HID devices away from Wine: "
             f"{_names(idents)} (they can crash Wine's HID stack and take "
             f"the mouse with it; BOL_HIDRAW=all to allow them).")
    return idents


def playstation_pads_through_sdl(prefix: Path,
                                 environ: Optional[Mapping[str, str]] = None
                                 ) -> bool:
    """Have winebus take PlayStation pads through SDL rather than hidraw.

    Written for every pad of PLAYSTATION_PADS, connected or not, so one
    plugged in while the game runs is covered as well. Like the other
    options it must be written before Wine starts; ``BOL_HIDRAW=all`` removes
    it. Returns whether the prefix changed.
    """
    env = os.environ if environ is None else environ
    restore = env.get("BOL_HIDRAW", "").strip().lower() == "all"
    return update_prefix_registry(
        prefix, machine=hidraw_option_changes(PLAYSTATION_PADS, restore))


def summary(root: Path = SYSFS_HIDRAW, dev: Path = DEV) -> str:
    """One line for the doctor: what PLAY keeps off Wine's hidraw bus."""
    idents = non_controllers(hidraw_devices(root, dev))
    if not idents:
        return "OK (no non-controller hidraw devices)"
    return f"OK (kept off Wine at PLAY: {_names(idents)})"
