"""bol.hostarch — the processor the launcher runs on (#250).

The launcher itself is Python and runs anywhere, but everything it starts is
x86-64: Minecraft for Windows, the Wine engine, xodus-cli and the WebKitGTK
runtime. On an ARM computer those run only through an x86-64 emulator that
the kernel hands them to -- FEX-Emu, Box64 or qemu-user, registered with
binfmt_misc. Without one, each fails with "Exec format error", which reached
the player as a missing WebKitGTK or an engine that would not start.
"""
# SPDX-License-Identifier: MIT

import platform
from pathlib import Path

from .platform import IS_MAC

BINFMT = Path("/proc/sys/fs/binfmt_misc")
_X86_64 = {"x86_64", "amd64"}


def machine():
    return (platform.machine() or "").lower()


def native():
    """Whether x86-64 programs run here without an emulator."""
    return machine() in _X86_64


def x86_64_emulator(binfmt=BINFMT):
    """The interpreter the kernel runs x86-64 programs with, or None."""
    try:
        entries = sorted(binfmt.iterdir())
    except OSError:
        return None
    for entry in entries:
        if entry.name in ("register", "status"):
            continue
        try:
            lines = entry.read_text(errors="replace").splitlines()
        except OSError:
            continue
        if not lines or lines[0].strip() != "enabled":
            continue
        fields = {}
        for line in lines[1:]:
            key, _, value = line.partition(" ")
            fields[key.rstrip(":")] = value.strip()
        magic = fields.get("magic", "").replace(" ", "").lower()
        # An x86-64 executable: ELF, 64-bit, and e_machine 0x3E at byte 18.
        if magic.startswith("7f454c4602") and magic[36:40] == "3e00":
            return fields.get("interpreter") or entry.name
    return None


def problem(binfmt=BINFMT):
    """Why x86-64 programs cannot run here, or None when they can."""
    # A Mac runs them through Rosetta 2, not binfmt_misc; whether Rosetta is
    # there is bol.winemac.rosetta_problem()'s to say.
    if IS_MAC or native() or x86_64_emulator(binfmt):
        return None
    return (
        f"This computer's processor is {platform.machine() or 'not x86-64'}, "
        "and Minecraft for Windows, Wine and xodus-cli are x86-64 programs. "
        "On an ARM computer they run through an x86-64 emulator registered "
        "with the kernel, and none is. Install FEX-Emu with its x86-64 "
        "RootFS (https://fex-emu.com), or Box64, then try again.")


def summary(binfmt=BINFMT):
    """For `doctor`."""
    name = platform.machine() or "unknown"
    if native():
        return f"OK ({name})"
    if IS_MAC:
        return f"{name}, x86-64 programs through Rosetta 2"
    emulator = x86_64_emulator(binfmt)
    if emulator:
        return f"{name}, x86-64 programs through {emulator} (experimental)"
    return f"MISSING x86-64 emulation ({name})"
