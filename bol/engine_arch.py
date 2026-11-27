"""bol.engine_arch — which game engine this computer runs (#250).

There are two managed engines. The x86-64 one is what every computer has
always run: on an ARM computer it runs whole through an x86-64 emulator
registered with the kernel (bol.hostarch), Wine, vkd3d-proton and the
graphics driver included. The experimental ARM64EC one is an aarch64 build
of the same engine, made the way Valve's Proton ARM64 is: Wine, vkd3d-proton,
DXVK and the GPU driver are native code, and FEX-Emu, loaded into the Wine
process as ``libarm64ecfex.dll``, translates only the x86-64 game and the
x86-64 DLLs it loads (docs/ARM64EC.md).

The ARM64EC engine is opt-in, and only offered where it can run: an aarch64
kernel with 4 KiB memory pages -- Wine's ARM64 support needs them, so on
Apple silicon it is offered inside muvm, where the launcher already starts
itself again (bol.hostarch) -- and a launcher that pins a published build of
it. An empty ``WINEGDK_ARM64_BUILD_REV`` means there is none yet: the choice
is hidden, and nothing reports the absence. Whenever the choice cannot be
honoured the x86-64 engine runs, so a player is never left without one.

The setting is ``engine_arch`` ("x86_64", the default, or "arm64ec");
``BOL_ENGINE_ARCH`` overrides it for one run, either way.
"""
# SPDX-License-Identifier: MIT

import os
from dataclasses import dataclass
from pathlib import Path

from . import config, hostarch

X86_64 = "x86_64"
ARM64EC = "arm64ec"
CHOICES = (X86_64, ARM64EC)
SETTING = "engine_arch"
ENV = "BOL_ENGINE_ARCH"

# How the ARM64EC engine is started (bol.prefix.proton_umu_cmd): "direct"
# runs its proton script on this system, "umu" through umu-launcher and the
# Steam Linux Runtime 4 arm64 container its toolmanifest asks for.
RUNNER_ENV = "BOL_ARM64_RUNNER"
DIRECT = "direct"
UMU = "umu"
# Steam Linux Runtime 4 (arm64), the runtime GE-Proton11-aarch64's
# toolmanifest.vdf names.
SLR4_ARM64_APPID = "4185400"
# The ARM64EC engine's Unix side is built in Steam Linux Runtime 4 (Debian 13),
# so outside that container it needs this system's C library to be at least
# as new: Debian 13, Ubuntu 25.04, Fedora 42 (and its Asahi Remix) have it.
GLIBC_MIN = (2, 41)

_ALIASES = {
    "x86_64": X86_64, "x86-64": X86_64, "amd64": X86_64, "x64": X86_64,
    "arm64ec": ARM64EC, "arm64": ARM64EC, "aarch64": ARM64EC,
}
_ARM_MACHINES = {"aarch64", "arm64"}


def normalise(value):
    """A choice from a setting or the environment, or None."""
    if not isinstance(value, str):
        return None
    return _ALIASES.get(value.strip().lower())


def requested(settings=None, environ=None):
    """The engine the player asked for, whether or not it can run here."""
    environ = os.environ if environ is None else environ
    override = normalise(environ.get(ENV, ""))
    if override:
        return override
    if settings is None:
        from .util import load_settings
        settings = load_settings()
    return normalise(settings.get(SETTING)) or X86_64


def host_supports_arm64ec():
    """An aarch64 kernel with the 4 KiB pages Wine's ARM64 support needs.

    Inside muvm the launcher sees the microVM's kernel, whose pages are
    4 KiB, so Apple silicon counts there and only there.
    """
    return (hostarch.machine() in _ARM_MACHINES
            and hostarch.page_size() == 4096)


def build_available():
    """Whether this launcher pins a published ARM64EC engine."""
    return bool((config.WINEGDK_ARM64_BUILD_REV or "").strip())


def offered():
    """Whether the ARM64EC choice is shown at all."""
    return host_supports_arm64ec() and build_available()


def host_glibc():
    """This system's glibc version as a tuple, or None when it has none
    (musl) or does not say."""
    try:
        text = os.confstr("CS_GNU_LIBC_VERSION") or ""
    except (AttributeError, OSError, ValueError):
        return None
    name, _, version = text.partition(" ")
    if name != "glibc":
        return None
    parts = []
    for piece in version.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) or None


def runner(environ=None):
    """How the ARM64EC engine starts: ``DIRECT`` unless asked for ``UMU``.

    umu-launcher's Steam Linux Runtime 4 arm64 container is not dependable
    yet: on aarch64 its pressure-vessel exits with status 1 before Proton
    starts, and a second run stops on KeyError 'steamrt4-arm64' while
    updating it (umu#716, reported against 1.4.4; the 1.4.3 we pin has the
    same code). The same GE-Proton aarch64 build that fails there runs when
    its proton script is started directly, so that is the default, and
    ``BOL_ARM64_RUNNER=umu`` keeps the container one switch away for when
    umu can do it.
    """
    environ = os.environ if environ is None else environ
    value = (environ.get(RUNNER_ENV) or "").strip().lower()
    return UMU if value == UMU else DIRECT


def _glibc_text(version):
    return ".".join(str(part) for part in version)


def problem(environ=None):
    """Why the ARM64EC engine cannot run here, in a sentence, or None."""
    if hostarch.machine() not in _ARM_MACHINES:
        return ("The ARM engine is for ARM computers; this one is "
                f"{hostarch.machine() or 'not ARM'}.")
    if hostarch.page_size() != 4096:
        return (f"This kernel uses {hostarch.page_size() // 1024} KiB memory "
                "pages, and the ARM engine needs 4 KiB ones: install muvm, "
                "which BedrockOnLinux starts itself in by itself.")
    if not build_available():
        return "This version of BedrockOnLinux has no ARM engine yet."
    if runner(environ) == DIRECT:
        have = host_glibc()
        if have is None or have < GLIBC_MIN:
            found = f"glibc {_glibc_text(have)}" if have else "no glibc"
            return (f"The ARM engine needs glibc {_glibc_text(GLIBC_MIN)} or "
                    f"newer, and this system has {found}. It runs on Debian "
                    "13, Ubuntu 25.04, Fedora 42 or newer.")
    return None


def active(settings=None, environ=None):
    """The engine that really runs: ARM64EC only when asked for and able."""
    if requested(settings, environ) != ARM64EC:
        return X86_64
    return X86_64 if problem(environ) else ARM64EC


def fallback_notice(settings=None, environ=None):
    """What to tell a player who asked for the ARM engine and gets the
    x86-64 one, or None. Silent on x86-64 computers unless it was asked for
    there explicitly, and silent while no ARM engine is published."""
    if requested(settings, environ) != ARM64EC:
        return None
    if not build_available() and hostarch.machine() in _ARM_MACHINES:
        return None
    reason = problem(environ)
    if not reason:
        return None
    return reason + " Using the x86-64 engine instead."


@dataclass(frozen=True)
class Engine:
    """One managed engine: what it is called, its pin and where it lives."""

    arch: str
    build_rev: str
    archive_sha256: str
    out: Path

    @property
    def manifest_arch(self):
        """The ``arch`` its engine-manifest.json declares."""
        return "aarch64" if self.arch == ARM64EC else "x86_64"

    @property
    def asset(self):
        return f"GDK-Proton-xuser-{self.build_rev}.tar.gz"

    @property
    def host_pe_dir(self):
        """The engine directory whose DLLs fill the prefix's system32."""
        return ("files/lib/wine/aarch64-windows" if self.arch == ARM64EC
                else "files/lib/wine/x86_64-windows")

    @property
    def label(self):
        return "ARM64EC" if self.arch == ARM64EC else "x86-64"


def engine(arch=X86_64):
    """The managed engine of ``arch``, from the pins this launcher carries."""
    if arch == ARM64EC:
        return Engine(ARM64EC, config.WINEGDK_ARM64_BUILD_REV,
                      config.WINEGDK_ARM64_ARCHIVE_SHA256,
                      config.WINEGDK_ARM64_OUT)
    return Engine(X86_64, config.WINEGDK_BUILD_REV,
                  config.WINEGDK_ARCHIVE_SHA256, config.WINEGDK_OUT)


def _same_path(a, b):
    try:
        return Path(a).expanduser().resolve(strict=False) == \
            Path(b).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


def engine_for_path(path, x86_out=None, arm_out=None):
    """The managed engine installed at ``path``, or None for any other.

    Which engine a tree is follows from where it is, not from the setting:
    a profile already playing keeps the engine it started on.
    """
    if not path:
        return None
    if _same_path(path, x86_out or config.WINEGDK_OUT):
        return engine(X86_64)
    if build_available() and _same_path(path, arm_out or config.WINEGDK_ARM64_OUT):
        return engine(ARM64EC)
    return None


def summary(settings=None, environ=None):
    """For `doctor`: which engine runs, and how the game is translated."""
    arch = active(settings, environ)
    if arch == ARM64EC:
        how = ("its proton script on this system" if runner(environ) == DIRECT
               else "umu and Steam Linux Runtime 4 (arm64)")
        return (f"ARM64EC {config.WINEGDK_ARM64_BUILD_REV} (experimental): "
                f"native Wine, FEX-Emu inside the prefix; started through {how}")
    text = f"x86-64 {config.WINEGDK_BUILD_REV}"
    if hostarch.machine() in _ARM_MACHINES:
        text += ", all of it through the x86-64 emulator"
        if offered():
            reason = problem(environ)
            text += (f" (ARM engine unavailable: {reason})" if reason
                     else " (ARM engine available in Settings)")
    return text
