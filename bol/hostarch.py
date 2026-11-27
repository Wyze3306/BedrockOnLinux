"""bol.hostarch — the processor the launcher runs on (#250).

The launcher itself is Python and runs anywhere, but everything it starts is
x86-64: Minecraft for Windows, the Wine engine, xodus-cli and the WebKitGTK
runtime. On an ARM computer those run only through an x86-64 emulator that
the kernel hands them to -- FEX-Emu, Box64 or qemu-user, registered with
binfmt_misc. Without one, each fails with "Exec format error", which reached
the player as a missing WebKitGTK or an engine that would not start.

Apple silicon under Asahi Linux runs a kernel with 16 KiB memory pages, and
neither Wine nor FEX-Emu works on one. There x86-64 programs run inside muvm,
a microVM with a 4 KiB-page kernel that shares the host's files, GPU, input
devices and display, and in which muvm itself registers FEX-Emu with
binfmt_misc. The host has no such entry, so the binfmt check alone told those
players to install the FEX-Emu they already had. Instead the launcher starts
itself again inside muvm, the way Steam runs there, and everything it starts
from then on finds 4 KiB pages and FEX.

The experimental ARM64EC engine (bol.engine_arch) changes what needs that
emulator: Wine is native there and FEX-Emu runs inside the Wine process, so
the engine and the game need no binfmt entry at all. What the launcher
starts beside them still may -- xodus-cli and the WebKitGTK sign-in runtime
are x86-64 builds until aarch64 ones are pinned -- so x86_64_programs()
names whatever still does, and problem() asks for an emulator only for
those. The muvm relaunch stays either way: Wine's ARM64 support needs 4 KiB
pages too.
"""
# SPDX-License-Identifier: MIT

import os
import platform
import shutil
import sys
from pathlib import Path

BINFMT = Path("/proc/sys/fs/binfmt_misc")
_X86_64 = {"x86_64", "amd64"}
_PAGE = 4096
# Set inside the microVM, so a muvm configured with large pages cannot make
# the launcher start itself again forever.
IN_MUVM = "BOL_IN_MUVM"
# A player who sets up 4 KiB pages some other way can keep the launcher out.
NO_MUVM = "BOL_NO_MUVM"
# muvm passes only LANG/LC_*, PATH, LD_LIBRARY_PATH and a few Mesa variables
# into the microVM; these change where the launcher keeps its files or how it
# behaves, so they follow it in.
_FORWARD = ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
            "XDG_STATE_HOME", "PYTHONPATH", "WINEDEBUG", "DXVK_HUD",
            "MANGOHUD", "VKD3D_DEBUG")


def machine():
    return (platform.machine() or "").lower()


def native():
    """Whether x86-64 programs run here without an emulator."""
    return machine() in _X86_64


def page_size():
    try:
        return os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, AttributeError):
        return _PAGE


def large_pages():
    """Whether this kernel's pages are too large for Wine and FEX-Emu."""
    return not native() and page_size() != _PAGE


def muvm():
    """The muvm program, or None."""
    return shutil.which("muvm")


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


def engine_needs_emulator():
    """Whether the engine and the game run through the binfmt emulator:
    everywhere off x86-64 except with the ARM64EC engine."""
    if native():
        return False
    from . import engine_arch
    try:
        return engine_arch.active() != engine_arch.ARM64EC
    except Exception:  # an unreadable settings file means the default engine
        return True


def native_helpers():
    """Whether xodus-cli and the WebKitGTK runtime run as aarch64 builds here.

    One answer for both: xodus-cli loads WebKitGTK into its own process, so
    the runtime it gets has to be built for the same processor. They are
    native on an aarch64 computer once this launcher pins the aarch64
    xodus-cli; without that pin both stay the x86-64 builds, run through the
    emulator as before. Either engine works with either: xodus-cli only
    hands the game to Wine.
    """
    from . import config
    return (machine() in ("aarch64", "arm64")
            and bool((config.XODUS_ARM64_ARCHIVE_SHA256 or "").strip()))


def helpers_need_emulator():
    """Whether xodus-cli and the WebKitGTK runtime are x86-64 programs here."""
    return not native() and not native_helpers()


def x86_64_programs():
    """What this launcher starts that needs x86-64 emulation here."""
    programs = []
    if engine_needs_emulator():
        programs += ["Minecraft for Windows", "Wine"]
    if helpers_need_emulator():
        programs += ["xodus-cli", "the WebKitGTK sign-in window"]
    return programs


def _names(programs):
    if len(programs) <= 2:
        return " and ".join(programs)
    return ", ".join(programs[:-1]) + " and " + programs[-1]


def problem(binfmt=BINFMT):
    """Why x86-64 programs cannot run here, or None when they can."""
    if native():
        return None
    if large_pages() and os.environ.get(NO_MUVM) != "1":
        if muvm() and not os.environ.get(IN_MUVM):
            return None
        return (
            f"This computer's kernel uses {page_size() // 1024} KiB memory "
            "pages (Apple silicon with Asahi Linux), and Wine and the x86-64 "
            "emulator need 4 KiB ones. Install muvm and FEX-Emu, which run "
            "x86-64 programs in a small virtual machine with 4 KiB pages "
            "(on Fedora Asahi Remix: sudo dnf install muvm fex-emu), then "
            "start BedrockOnLinux again: it uses them by itself.")
    programs = x86_64_programs()
    if not programs or x86_64_emulator(binfmt):
        return None
    if engine_needs_emulator():
        return (
            f"This computer's processor is "
            f"{platform.machine() or 'not x86-64'}, and Minecraft for "
            "Windows, Wine and xodus-cli are x86-64 programs. On an ARM "
            "computer they run through an x86-64 emulator registered with "
            "the kernel, and none is. Install FEX-Emu with its x86-64 RootFS "
            "(https://fex-emu.com), or Box64, then try again.")
    return (
        "The ARM game engine runs Minecraft through the FEX-Emu it carries, "
        f"but {_names(programs)} are still x86-64 programs, which need an "
        "x86-64 emulator registered with the kernel, and none is. Install "
        "FEX-Emu with its x86-64 RootFS (https://fex-emu.com), or Box64, "
        "then try again.")


def summary(binfmt=BINFMT):
    """For `doctor`."""
    name = platform.machine() or "unknown"
    if native():
        return f"OK ({name})"
    if large_pages():
        pages = f"{page_size() // 1024} KiB pages"
        if muvm() and not os.environ.get(IN_MUVM):
            return f"{name}, {pages}, x86-64 programs through muvm"
        return f"MISSING muvm ({name}, {pages})"
    emulator = x86_64_emulator(binfmt)
    inside = ", inside muvm" if os.environ.get(IN_MUVM) else ""
    engine = ("" if engine_needs_emulator()
              else "; the ARM engine runs the game through its own FEX-Emu")
    if not x86_64_programs():
        return f"{name}{inside}{engine}"
    if emulator:
        return (f"{name}, x86-64 programs through {emulator}{inside} "
                f"(experimental){engine}")
    return f"MISSING x86-64 emulation ({name}{inside}){engine}"


def muvm_command(argv=None, environ=None, executable=None):
    """The command that runs this launcher again inside muvm, or None when
    it should run here."""
    environ = os.environ if environ is None else environ
    if (not large_pages() or environ.get(IN_MUVM)
            or environ.get(NO_MUVM) == "1"):
        return None
    program = muvm()
    if not program:
        return None
    if argv is None:
        # orig_argv keeps "-m bol", which a path to bol/__main__.py loses.
        # muvm runs the command in this working directory, and the microVM
        # sees the host's files, so the same interpreter finds the same bol.
        orig = getattr(sys, "orig_argv", None)
        argv = (orig[1:] if orig
                else [os.path.abspath(sys.argv[0])] + sys.argv[1:])
    cmd = [program, "-e", f"{IN_MUVM}=1"]
    for key in sorted(environ):
        if key in _FORWARD or (key.startswith("BOL_") and key != IN_MUVM):
            cmd += ["-e", f"{key}={environ[key]}"]
    return cmd + ["--", executable or sys.executable] + list(argv)


def relaunch_in_muvm():
    """On a large-page ARM kernel with muvm, replace this process with the
    same launcher inside muvm. Returns only when it should run here."""
    cmd = muvm_command()
    if not cmd:
        return
    print("BedrockOnLinux: this kernel uses "
          f"{page_size() // 1024} KiB pages; starting again inside muvm.",
          file=sys.stderr, flush=True)
    try:
        os.execv(cmd[0], cmd)
    except OSError as exc:
        print(f"BedrockOnLinux: could not start muvm ({exc}).",
              file=sys.stderr, flush=True)
