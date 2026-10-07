"""bol.raytracing — report what the graphics stack granted Minecraft.

Issue #153 asked why Minecraft "refuses to acknowledge the ray tracing
capabilities" of an RDNA4 card, and there was no way to answer it: nothing on
either side of the launch writes down whether the game was handed DXR at all.
The launcher's *Ray tracing* switch only removes an inherited
``VKD3D_CONFIG=nodxr`` — the tier itself is decided inside the game process by
vkd3d-proton, from the Vulkan features the driver exposes, and that decision
was thrown away.

It is not thrown away any more: every launch now runs the graphics payload at
its ``info`` level, which states the outcome in a handful of lines at device
creation, and this module reads them back out of the log afterwards. Nothing
here opens a GPU device or starts a Wine process — it reads state a previous
launch already produced, the same rule the rest of the GPU reporting follows.

What the log says, and what it means for Minecraft:

- ``DXR 1.1 support enabled.`` — the tier Minecraft's Ray Traced graphics mode
  needs. Tier 1.0 alone is *not* enough for the mode to become selectable, and
  no tier at all means the driver exposed no Vulkan ray tracing.
- ``DX Ultimate supported!`` — mesh shaders, sampler feedback and VRS on top,
  which is what a Windows "DirectX 12 Ultimate" device reports.
- ``Enabling fast paths for advanced ExecuteIndirect() … (NV_dgc|EXT_dgc)`` —
  which half of the universal device-generated-commands payload the driver
  took, the thing the main menu depends on.

A tier reported here is what the *device* offered. Minecraft additionally
requires a ray-tracing-capable world before it will let the mode be selected,
and that condition is the game's, not the driver's.
"""
# SPDX-License-Identifier: MIT

from pathlib import Path

from .config import LOGS

# Device creation happens near the start of a launch, so the answer is always
# in the head of the log. Bounding the read keeps a long session's log from
# being pulled into memory to re-read something written in its first second.
_MAX_READ = 1 << 20

# vkd3d-proton tags every line it writes; without one of these the log is from
# something else and has nothing to say about ray tracing.
_PAYLOAD_MARKER = b"vkd3d-proton"

# The payload announces itself when the D3D12 *instance* is created, which a
# launch that died before it ever reached a device also does. The ray tracing
# tier is decided while the device's caps are filled in, right after the
# shader model, so require evidence of that step: "no ray tracing was
# reported" is only a fact about a device that was actually created.
_DEVICE_MARKERS = (b"d3d12_device_caps_init", b"Enabling support for SM ")

# Highest tier first: "DXR 1.1 support enabled." is logged in addition to
# "DXR support enabled.", never instead of it.
_TIERS = (
    (b"DXR 1.2 support enabled.", "1.2"),
    (b"DXR 1.1 support enabled.", "1.1"),
    (b"DXR support enabled.", "1.0"),
)

_ULTIMATE = b"DX Ultimate supported!"
# vkd3d-proton's own per-application overrides. Neither applies to Minecraft
# today, but a future quirk entry would silently remove the tier, so read them
# rather than assume.
_LIMITED_TO_1_0 = b"Limiting reported DXR tier to 1.0."
_DISABLED_ON_DECK = b"Disabling automatic enablement of DXR on Deck."

_INDIRECT = ((b"(NV_dgc)", "NV_dgc"), (b"(EXT_dgc)", "EXT_dgc"))

_CONFIG_PREFIX = b"VKD3D_CONFIG='"

# Logs a launch may have left, newest first once sorted by modification time.
_LOG_NAMES = ("minecraft.log", "proton.log")


def _log_head(path):
    """The head of a log file, or None when there is nothing to read."""
    try:
        with Path(path).open("rb") as handle:
            head = handle.read(_MAX_READ)
    except OSError:
        return None
    return head or None


def _graphics_log_head(logs_dir=None):
    """The head of the most recent launch log that a graphics payload wrote.

    Newest first, because a diagnostics launch writes both files and only the
    launch being asked about should answer.
    """
    directory = Path(LOGS if logs_dir is None else logs_dir)
    candidates = []
    for name in _LOG_NAMES:
        path = directory / name
        try:
            candidates.append((path.stat().st_mtime, path))
        except OSError:
            continue
    for _, path in sorted(candidates, reverse=True):
        head = _log_head(path)
        if head and _PAYLOAD_MARKER in head:
            return head
    return None


def _declared_vkd3d_config(head):
    """The VKD3D_CONFIG the payload echoed back, or None."""
    start = head.find(_CONFIG_PREFIX)
    if start < 0:
        return None
    start += len(_CONFIG_PREFIX)
    end = head.find(b"'", start)
    if end < 0:
        return None
    return head[start:end].decode("utf-8", "replace")


def graphics_capabilities(logs_dir=None):
    """What the graphics payload reported on the last launch it logged.

    ``observed`` is the only field worth branching on first: without it the
    other fields say nothing, and reporting "no ray tracing" from a log that
    was never written would be an accusation rather than a measurement.

    ``tier`` is what the game was told, so the per-application overrides are
    applied to it rather than left beside it: they run after the tier has been
    determined and logged, and a log therefore states a tier the game never
    saw. ``limited`` and ``deck`` stay, to say why.
    """
    head = _graphics_log_head(logs_dir)
    if head is None or not any(marker in head for marker in _DEVICE_MARKERS):
        return {"observed": False, "tier": None, "ultimate": False,
                "limited": False, "deck": False, "indirect": None,
                "config": None}

    tier = next((name for marker, name in _TIERS if marker in head), None)
    indirect = next((name for marker, name in _INDIRECT if marker in head),
                    None)
    limited = _LIMITED_TO_1_0 in head
    deck = _DISABLED_ON_DECK in head
    if deck:
        tier = None
    elif limited:
        tier = "1.0"
    return {
        "observed": True,
        "tier": tier,
        "ultimate": _ULTIMATE in head,
        "limited": limited,
        "deck": deck,
        "indirect": indirect,
        "config": _declared_vkd3d_config(head),
    }


def _config_disables_dxr(config):
    """Whether a declared VKD3D_CONFIG string turns DXR off."""
    if not config:
        return False
    return "nodxr" in [item.strip() for item in
                       config.replace(";", ",").split(",")]


def ray_tracing_summary(logs_dir=None):
    """One short status word for Doctor's aligned report."""
    caps = graphics_capabilities(logs_dir)
    if not caps["observed"]:
        return "unknown (play once, then run this again)"
    if caps["tier"] is None:
        if caps["deck"]:
            return "off (dropped by the payload on a Steam Deck)"
        if _config_disables_dxr(caps["config"]):
            return "off (VKD3D_CONFIG=nodxr)"
        return "MISSING (the driver exposed no ray tracing)"
    detail = "DXR " + caps["tier"]
    if caps["ultimate"]:
        detail += ", DirectX 12 Ultimate"
    if caps["indirect"]:
        detail += ", ExecuteIndirect via " + caps["indirect"]
    prefix = "OK" if caps["tier"] != "1.0" else "PARTIEL"
    return f"{prefix} ({detail})"


def ray_tracing_problem(logs_dir=None):
    """Actionable message when Minecraft cannot offer Ray Traced, else None.

    Only the cases a user can act on are reported. "Ray Traced is greyed out
    in the main menu" is not one of them: the game keeps that mode uneditable
    until a ray-tracing-capable world is loaded, whatever the device reports.
    """
    caps = graphics_capabilities(logs_dir)
    if not caps["observed"]:
        return None
    if caps["deck"]:
        return (
            "The graphics payload turned ray tracing off for this device "
            "because it is a Steam Deck, where it costs more than it gives. "
            "Set VKD3D_CONFIG=dxr in Settings ▸ Advanced ▸ custom environment "
            "to take it back."
        )
    if caps["tier"] is None:
        if _config_disables_dxr(caps["config"]):
            return (
                "Ray tracing was off on the last launch (VKD3D_CONFIG=nodxr), "
                "so Minecraft's Ray Traced graphics mode was unavailable. The "
                "Ray tracing switch in Settings ▸ Advanced turns it back on."
            )
        return (
            "Your Vulkan driver exposed no ray tracing to Minecraft, so its "
            "Ray Traced graphics mode cannot appear whatever the hardware "
            "is. Ray tracing needs an NVIDIA RTX 20 series or newer, or an "
            "AMD Radeon RX 6000 series or newer, on a driver built with the "
            "Vulkan ray tracing extensions — on Mesa that is RADV, not the "
            "software renderer."
        )
    if caps["tier"] == "1.0":
        return (
            "The graphics payload reported ray tracing tier 1.0 to Minecraft. "
            "Its Ray Traced graphics mode needs tier 1.1, so the mode stays "
            "unavailable. Update the GPU driver: tier 1.1 needs ray queries "
            "and primitive culling on top of ray tracing pipelines."
        )
    return None
