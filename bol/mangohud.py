"""bol.mangohud — MangoHud on NVIDIA cards whose driver cannot read power.

Issue #395. On some cards the NVIDIA driver has no power reading at all:
``nvidia-smi`` prints ``[N/A]``, and NVML, the driver's monitoring library,
answers ``nvmlDeviceGetPowerUsage`` with NVML_ERROR_NOT_SUPPORTED. MangoHud
0.8.4 and older never look at that answer. They divide whatever was left in
the variable the driver did not write, so the overlay shows leftover memory
as watts: 2278630 W with the Flatpak's MangoHud (the
org.freedesktop.Platform.VulkanLayer.MangoHud extension), 0.0 W with another
build of the same version that happened to leave a zero there. MangoHud fixed
it upstream in 698bea3 (0.8.5-rc1), which shows 0 W instead.

Neither number is a reading. So when MangoHud will draw the game's overlay on
such a card, the launcher turns MangoHud's GPU power field off for the game,
the way the player would by unticking it: ``gpu_power=0`` is appended to
``MANGOHUD_CONFIG``, after the player's own options, so everything else they
chose stays as they chose it. With no ``MANGOHUD_CONFIG`` of theirs it starts
with ``read_cfg``, which keeps MangoHud reading their config file, as it does
when the variable is unset. A ``MANGOHUD_CONFIG`` that names ``gpu_power``
itself is the player's explicit choice and is left alone.

Whether the driver has a reading is asked of NVML in a child process, only
when MangoHud is on: a driver that hangs or crashes when asked takes a probe
down, never the launcher.
"""
# SPDX-License-Identifier: MIT

import re
import shlex
import subprocess
import sys

from . import gpus
from .log import info

CONFIG = "MANGOHUD_CONFIG"
POWER_OFF = "gpu_power=0"
# MangoHud separates its options with any of these (overlay_params.cpp,
# is_delimiter); "=" separates a name from its value.
_SEPARATORS = re.compile(r"[,:;]")

_NVML_SUCCESS = 0
_NVML_ERROR_NOT_SUPPORTED = 3
_PROBE_TIMEOUT = 10

# Prints NVML's answer to nvmlDeviceGetPowerUsage for one card (argv[1], a
# PCI address) or for every NVIDIA card, space separated. Exits 2 when NVML
# cannot be loaded or started.
_PROBE = r"""
import ctypes, sys
try:
    nvml = ctypes.CDLL("libnvidia-ml.so.1")
    if nvml.nvmlInit_v2() != 0:
        sys.exit(2)
except (OSError, AttributeError):
    sys.exit(2)
try:
    handles = []
    if len(sys.argv) > 1:
        handle = ctypes.c_void_p()
        if nvml.nvmlDeviceGetHandleByPciBusId_v2(
                sys.argv[1].encode(), ctypes.byref(handle)) == 0:
            handles.append(handle)
    else:
        count = ctypes.c_uint()
        if nvml.nvmlDeviceGetCount_v2(ctypes.byref(count)) == 0:
            for index in range(count.value):
                handle = ctypes.c_void_p()
                if nvml.nvmlDeviceGetHandleByIndex_v2(
                        index, ctypes.byref(handle)) == 0:
                    handles.append(handle)
    answers = []
    for handle in handles:
        milliwatts = ctypes.c_uint()
        answers.append(nvml.nvmlDeviceGetPowerUsage(
            handle, ctypes.byref(milliwatts)))
    print(" ".join(str(answer) for answer in answers))
finally:
    nvml.nvmlShutdown()
"""


def requested(env, gamescope_argv=()):
    """Whether MangoHud will draw an overlay for a game started with env.

    Its Vulkan layer turns on for MANGOHUD=1 exactly (the loader compares the
    value) unless DISABLE_MANGOHUD=1; gamescope's ``--mangoapp`` starts
    MangoHud's own process, which reads MANGOHUD_CONFIG from the same
    environment.
    """
    if str(env.get("DISABLE_MANGOHUD", "")).strip() == "1":
        return False
    if str(env.get("MANGOHUD", "")).strip() == "1":
        return True
    return "--mangoapp" in list(gamescope_argv or ())


def power_readable(slot=None, runner=subprocess.run):
    """Whether the NVIDIA driver can read the power draw of the game's card.

    ``slot`` is the card's PCI address; None asks about every NVIDIA card.
    False only on the driver's own word, NVML_ERROR_NOT_SUPPORTED for every
    card asked about; True when one has a reading; None when the driver
    could not be asked or gave another answer.
    """
    if not sys.executable:
        return None
    argv = [sys.executable, "-I", "-c", _PROBE] + ([slot] if slot else [])
    try:
        proc = runner(argv, capture_output=True, text=True,
                      timeout=_PROBE_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        answers = [int(word) for word in str(proc.stdout).split()]
    except ValueError:
        return None
    if not answers:
        return None
    if _NVML_SUCCESS in answers:
        return True
    if all(answer == _NVML_ERROR_NOT_SUPPORTED for answer in answers):
        return False
    return None


def names_power(config):
    """Whether a MANGOHUD_CONFIG value sets the gpu_power option itself."""
    for option in _SEPARATORS.split(str(config or "")):
        if option.partition("=")[0].strip() == "gpu_power":
            return True
    return False


def without_power(config):
    """MANGOHUD_CONFIG as it was, with MangoHud's GPU power field off.

    Unset, MangoHud reads its config file; set, only with ``read_cfg``. An
    empty value is a set one, so it gets no ``read_cfg`` either.
    """
    if config is None:
        return "read_cfg," + POWER_OFF
    kept = str(config).rstrip(" ,:;")
    return f"{kept},{POWER_OFF}" if kept else POWER_OFF


def _gamescope_argv(gamescope):
    if isinstance(gamescope, str):
        try:
            return shlex.split(gamescope)
        except ValueError:
            return []
    return list(gamescope or ())


def hide_unreadable_power(env, settings=None, gamescope=(), probe=None,
                          cards=None):
    """Turn off MangoHud's GPU power field where there is nothing to show.

    ``gamescope`` is the gamescope command line the game starts under, if
    any. Returns True when ``env`` was changed.
    """
    if not requested(env, _gamescope_argv(gamescope)):
        return False
    config = env.get(CONFIG)
    if config is not None and names_power(config):
        return False
    cards = gpus.list_gpus() if cards is None else list(cards)
    chosen = gpus.chosen_gpu(settings, cards)
    if chosen is not None and not chosen.nvidia:
        return False
    if chosen is None and cards and not any(card.nvidia for card in cards):
        return False
    readable = (probe or power_readable)(chosen.slot if chosen else None)
    if readable is not False:
        return False
    env[CONFIG] = without_power(config)
    info("MangoHud: the NVIDIA driver has no power reading for this card "
         "(nvidia-smi shows N/A), so MangoHud's GPU power field is turned "
         "off instead of showing a meaningless number. Setting gpu_power in "
         "MANGOHUD_CONFIG keeps it.")
    return True
