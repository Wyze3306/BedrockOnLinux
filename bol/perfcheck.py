"""bol.perfcheck — name the conditions behind "the game lags" before launch.

Every check here answers the same report, and none of them leaves a trace in
a Wine, Proton or vkd3d log, which is why they get blamed on the GPU:

* the host has no memory left, so the kernel pages the game out and every
  chunk the player walks into comes back through a major fault;
* the data directory is nearly full, so the shader cache cannot grow and
  vkd3d recompiles pipelines that should have been reused;
* Minecraft presents with vsync into a window, so a compositing desktop adds
  a frame of latency on top of the game's own and the pacing wobbles;
* the render distance is set past what the main thread can feed — by a wide
  margin the largest frame-time cost in Bedrock, since the chunk ring the
  main thread walks grows with the square of the distance;
* Minecraft's own *Max Framerate* limiter waits by polling for window
  messages, which under Wine holds the main thread at a full CPU core.

Detection is cheap and side-effect free, in the same spirit as bol.ntsync and
bol.dgc: two ``/proc/meminfo`` fields, one ``statvfs``, and Minecraft's own
options.txt parsed out of the prefix. No Wine process is started, no Vulkan
or GPU is touched and nothing is written, so this can run before every
launch.

Every threshold below is deliberately set past the values people actually
play at: an advisory that fires on a healthy machine is one users learn to
ignore, and then it is worth nothing on the machine that really is starved.
"""
# SPDX-License-Identifier: MIT

import os
import re
import shutil
import subprocess
from pathlib import Path

from .platform import IS_MAC

MEMINFO = "/proc/meminfo"

# Minecraft settles near 2.5 GiB resident at an ordinary render distance, and
# the prefix and umu container want a few hundred MiB more. Below this the
# game still starts — it starts and then spends the session fighting the page
# cache, which is what the player feels.
LOW_MEMORY_MIB = 3072

# One game update is ~2.5 GiB, and the prefix, the world saves and the shader
# caches all grow on top of it. Under this, writes begin to fail at the least
# convenient moment.
LOW_DISK_MIB = 5120

# Bedrock stores the render distance in blocks; its slider moves in chunks.
CHUNK_BLOCKS = 16

# Past this the main thread is the frame limiter on any CPU. Set well above
# the 16-24 chunks most people play at so this only ever names extreme values.
EXTREME_RENDER_CHUNKS = 32

# Desktops that composite unconditionally in their shipped configuration.
# XFCE, MATE and LXQt are deliberately absent: there the compositor is a
# setting, so the honest answer for them is "cannot tell" rather than a guess.
_ALWAYS_COMPOSITED_DESKTOPS = (
    "cinnamon", "gnome", "kde", "plasma", "deepin", "pantheon", "cosmic",
    "unity",
)

# Minecraft has kept its settings in the Roaming path since the GDK build; the
# Local/Packages path is where a UWP-layout install of the same game puts it.
# The Users/* wildcard matters: the game keeps one settings file per signed-in
# account, plus a shared one for playing signed out.
_OPTIONS_GLOBS = (
    "drive_c/users/*/AppData/Roaming/Minecraft Bedrock/Users/*/games/"
    "com.mojang/minecraftpe/options.txt",
    "drive_c/users/*/AppData/Local/Packages/"
    "Microsoft.MinecraftUWP_8wekyb3d8bbwe/LocalState/games/com.mojang/"
    "minecraftpe/options.txt",
)

_SILENCE = "Silence this notice with BOL_SKIP_PERF_CHECK=1."


def _meminfo_mib(field, meminfo_path=None):
    """One /proc/meminfo field in MiB, or None when it cannot be read."""
    path = Path(meminfo_path) if meminfo_path is not None else Path(MEMINFO)
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    match = re.search(r"^%s:\s+(\d+)\s+kB\s*$" % re.escape(field),
                      text, re.MULTILINE)
    return int(match.group(1)) // 1024 if match else None


def _run_text(argv):
    """stdout of a short read-only command, or None. Never raises."""
    try:
        found = subprocess.run(argv, capture_output=True, text=True,
                               timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return found.stdout if found.returncode == 0 else None


_VM_STAT_PAGE = re.compile(r"page size of (\d+) bytes")
_VM_STAT_LINE = re.compile(r"^(.+?):\s+(\d+)\.?$", re.MULTILINE)

# The macOS counterpart of MemAvailable. Free pages alone would fire on every
# healthy Mac -- the kernel keeps very few of them -- so this adds the classes
# it hands back on demand, which is the same reasoning MemAvailable encodes.
_VM_STAT_AVAILABLE = ("Pages free", "Pages inactive", "Pages speculative",
                      "Pages purgeable")


def _mac_available_memory_mib():
    text = _run_text(["/usr/bin/vm_stat"])
    if not text:
        return None
    page = _VM_STAT_PAGE.search(text)
    page_size = int(page.group(1)) if page else 4096
    counts = {name.strip(): int(value)
              for name, value in _VM_STAT_LINE.findall(text)}
    pages = sum(counts.get(name, 0) for name in _VM_STAT_AVAILABLE)
    return (pages * page_size) // (1024 * 1024) if pages else None


_SWAPUSAGE_USED = re.compile(r"used\s*=\s*([\d.]+)([KMG])", re.IGNORECASE)
_SWAP_UNIT_MIB = {"K": 1.0 / 1024, "M": 1.0, "G": 1024.0}


def _mac_swap_used_mib():
    text = _run_text(["/usr/sbin/sysctl", "-n", "vm.swapusage"])
    if not text:
        return None
    found = _SWAPUSAGE_USED.search(text)
    if not found:
        return None
    scale = _SWAP_UNIT_MIB.get(found.group(2).upper())
    return int(float(found.group(1)) * scale) if scale else None


def available_memory_mib(meminfo_path=None):
    """What the kernel says a new workload can take without swapping.

    MemAvailable, not MemFree: reclaimable page cache is available to the
    game, and reading MemFree instead would fire on every healthy machine.
    macOS publishes no single such field, so there it is summed out of
    ``vm_stat`` on the same principle. An explicit ``meminfo_path`` always
    takes the Linux parser, which is what the tests hand it.
    """
    if IS_MAC and meminfo_path is None:
        return _mac_available_memory_mib()
    return _meminfo_mib("MemAvailable", meminfo_path)


def swap_used_mib(meminfo_path=None):
    """How much has already been pushed to swap, or None if unreadable."""
    if IS_MAC and meminfo_path is None:
        return _mac_swap_used_mib()
    total = _meminfo_mib("SwapTotal", meminfo_path)
    free = _meminfo_mib("SwapFree", meminfo_path)
    if total is None or free is None:
        return None
    return max(0, total - free)


def low_memory_problem(meminfo_path=None, threshold_mib=LOW_MEMORY_MIB):
    """Actionable message when the host cannot hold the game in memory."""
    available = available_memory_mib(meminfo_path)
    if available is None or available >= threshold_mib:
        return None
    swapped = swap_used_mib(meminfo_path)
    detail = ""
    if swapped:
        detail = (" %.1f GiB is already in swap, which is where the game's "
                  "own pages go next." % (swapped / 1024.0))
    return (
        "Only %d MiB of memory is available, and Minecraft settles around "
        "%d MiB once a world is loaded.%s The game will start and then stall "
        "every time the kernel has to fetch a paged-out chunk back from disk "
        "— the freezes and lag spikes that get blamed on the GPU. Close what "
        "you are not using before playing (browsers and Electron apps are "
        "usually the largest holders), or lower the render distance to "
        "shrink the game's own footprint. %s"
        % (available, threshold_mib, detail, _SILENCE))


def free_disk_problem(path, threshold_mib=LOW_DISK_MIB):
    """Actionable message when the data directory has no room left."""
    if not path:
        return None
    try:
        usage = shutil.disk_usage(str(path))
    except OSError:
        return None
    free_mib = usage.free // (1 << 20)
    if free_mib >= threshold_mib:
        return None
    used_percent = (100.0 * (usage.total - usage.free) / usage.total
                    if usage.total else 0.0)
    return (
        "Only %d MiB is free where the game lives (%s, %.0f%% full). The "
        "shader cache cannot grow there, so vkd3d recompiles pipelines every "
        "session instead of reusing them — that is stutter on first sight of "
        "each new effect, every time you play — and a world save or a game "
        "update can fail outright. Free a few GiB before playing. %s"
        % (free_mib, path, used_percent, _SILENCE))


def find_options_files(prefix):
    """Every Minecraft options.txt inside a prefix, in no particular order.

    A prefix holds one per account the player has signed in with, and code
    that guards the files rather than reading a setting has to see all of
    them: the game picks which one it uses at sign-in time, not at launch.
    """
    if not prefix:
        return []
    root = Path(prefix)
    found = []
    for pattern in _OPTIONS_GLOBS:
        try:
            found.extend(item for item in root.glob(pattern) if item.is_file())
        except OSError:
            continue
    return found


def find_game_data_dirs(prefix):
    """Every com.mojang/minecraftpe folder inside a prefix.

    The same one-per-account rule as the settings files: everything the game
    keeps per profile — its settings, its server list — sits together in
    these folders, so anything writing one has to find them all.
    """
    if not prefix:
        return []
    root = Path(prefix)
    found = []
    for pattern in _OPTIONS_GLOBS:
        folder = pattern.rsplit("/", 1)[0]
        try:
            found.extend(item for item in root.glob(folder) if item.is_dir())
        except OSError:
            continue
    return found


def find_options_file(prefix):
    """The most recently written Minecraft options.txt inside a prefix.

    Returns None when the game has never written one, which is the normal
    state before the first launch and must stay silent.
    """
    found = find_options_files(prefix)
    if not found:
        return None
    try:
        return max(found, key=lambda item: item.stat().st_mtime)
    except OSError:
        return found[0]


def read_game_options(path):
    """Minecraft's options.txt as a mapping; empty when it cannot be read.

    The format is one ``key:value`` per line. Values holding a colon of their
    own keep it, since only the first separator delimits the key.
    """
    options = {}
    if not path:
        return options
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return options
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip():
            options[key.strip()] = value.strip()
    return options


def _option_int(options, key):
    """One options.txt value as an int, or None when absent or malformed."""
    try:
        return int(str(options.get(key, "")).strip())
    except (TypeError, ValueError):
        return None


def render_distance_chunks(options):
    """The configured render distance in chunks, or None when unset."""
    blocks = _option_int(options, "gfx_viewdistance")
    if blocks is None or blocks <= 0:
        return None
    return blocks // CHUNK_BLOCKS


def render_distance_problem(options, threshold_chunks=EXTREME_RENDER_CHUNKS):
    """Actionable message when the render distance outruns the main thread."""
    chunks = render_distance_chunks(options)
    if chunks is None or chunks <= threshold_chunks:
        return None
    return (
        "Minecraft's render distance is set to %d chunks. Bedrock builds and "
        "streams that ring on its main thread, and the work grows with the "
        "square of the distance, so past roughly %d chunks the main thread "
        "becomes the frame limiter and the GPU idles no matter how fast it "
        "is. If the game feels slow, this is the first setting to lower — "
        "16 to 24 chunks costs little to look at and gives most of the frame "
        "rate back. %s" % (chunks, threshold_chunks, _SILENCE))


def frame_rate_is_unlimited(options):
    """Whether Minecraft's own settings leave the render loop unpaced.

    Two settings can bound it, and the game needs neither: ``gfx_vsync`` makes
    it wait for the display, and ``gfx_max_framerate`` is its own limit in
    frames per second, where 0 is the *Unlimited* end of the slider. With
    vsync off and the slider on Unlimited nothing stops Minecraft from drawing
    as fast as the machine physically can, and the main menu — a still image
    over a panorama, the cheapest frame the game ever draws — is where that
    goes furthest: measured at ~1800 FPS on an RTX 4060, for 60-70% of the
    card and the fan noise and heat that come with it (issue #150).

    Answers False unless ``gfx_max_framerate`` was positively read as 0. A
    settings file that predates the slider, or one Minecraft has never
    written, is "cannot tell" and must not be treated as unlimited.
    """
    limit = _option_int(options, "gfx_max_framerate")
    if limit is None or limit > 0:
        return False
    return _option_int(options, "gfx_vsync") != 1


def game_frame_limiter_problem(options):
    """Actionable message when Minecraft's own frame limiter spins.

    Minecraft waits for its frame deadline by pumping messages rather than by
    sleeping. On Windows that is close to free; under Wine every
    ``PeekMessage`` on an empty queue costs two user-mode callbacks and a
    ``NtYieldExecution`` — three syscalls — and the game issues on the order
    of a hundred and fifty of them per frame. The main thread then sits at a
    full core while doing nothing, and since that thread is what builds every
    frame in Bedrock, the wait comes straight out of the frame rate.

    Measured in the main menu on an RTX 4060, same scene and same 60 FPS: with
    *Max Framerate* at 60 the main thread costs 99% of a core, 56 points of it
    in the kernel; with *Unlimited* and the launcher capping at 60 instead it
    costs 10%, and the whole process drops from 127% of a core to 50%.

    Only reported when vsync is off, which is the configuration that was
    measured: with vsync on the game waits inside its present call instead,
    and the main thread stays near 20%.
    """
    limit = _option_int(options, "gfx_max_framerate")
    if not limit or limit <= 0:
        return None
    if _option_int(options, "gfx_vsync") == 1:
        return None
    return (
        "Minecraft's own Max Framerate setting (%d FPS) is expensive here: "
        "the game waits for each frame's deadline by polling for window "
        "messages, and under Wine that poll costs three system calls a time, "
        "about a hundred and fifty times per frame. Measured on this exact "
        "path, it holds the main thread at 99%% of a CPU core to draw a still "
        "menu — and the main thread is what builds every frame, so the wait "
        "is taken out of your frame rate. Set Max Framerate to *Unlimited* in "
        "Video settings and let the launcher hold the same rate instead "
        "(BOL_FRAME_RATE=%d in the Advanced custom-environment field, or "
        "nothing at all to follow the display): the same %d FPS then costs "
        "the main thread 10%% of a core. %s"
        % (limit, limit, limit, _SILENCE))


def session_is_composited(environ=None):
    """Whether the desktop certainly composites; None when it cannot be told.

    Environment only, no X11 round-trip: a compositing manager owns the
    ``_NET_WM_CM_S0`` selection, which cannot be queried without an X
    connection, and guessing from a process list would be worse than saying
    nothing. Wayland always composites, and the desktops listed above ship a
    compositor that cannot be turned off; everything else answers None and
    stays silent.
    """
    source = os.environ if environ is None else environ
    if IS_MAC and environ is None:
        # WindowServer composites every window on macOS and cannot be turned
        # off, so the answer here is never in doubt.
        return True
    session = (source.get("XDG_SESSION_TYPE") or "").strip().lower()
    if session == "wayland" or source.get("WAYLAND_DISPLAY"):
        return True
    desktop = (source.get("XDG_CURRENT_DESKTOP") or "").strip().lower()
    if any(name in desktop for name in _ALWAYS_COMPOSITED_DESKTOPS):
        return True
    return None


def windowed_vsync_problem(options, environ=None):
    """Actionable message when vsync and a window stack two frame queues.

    A fullscreen window is normally handed straight to the display, bypassing
    the compositor; a windowed one is not, so the game waits on its own vsync
    and then on the desktop's. Silent unless the desktop is known to
    composite, since on a bare window manager the pairing is harmless.
    """
    vsync = _option_int(options, "gfx_vsync")
    fullscreen = _option_int(options, "gfx_fullscreen")
    if vsync != 1 or fullscreen != 0:
        return None
    if not session_is_composited(environ):
        return None
    return (
        "Minecraft has vsync on while running in a window, and this desktop "
        "composites every window. The game waits for its own vsync and the "
        "desktop then holds the frame for its next refresh, which adds "
        "latency and makes the frame times uneven — it reads as lag even "
        "when the frame rate is fine. Play fullscreen, so the window goes "
        "straight to the display, or turn vsync off. %s" % _SILENCE)


def performance_problems(prefix=None, data_dir=None, environ=None,
                         meminfo_path=None):
    """Every frame-rate condition found, worst cost first.

    Returns a list of ready-to-print messages, empty when nothing is wrong or
    nothing could be determined, so a caller can treat any entry as worth
    telling the user about.
    """
    options = read_game_options(find_options_file(prefix))
    candidates = (
        low_memory_problem(meminfo_path),
        render_distance_problem(options),
        game_frame_limiter_problem(options),
        free_disk_problem(data_dir),
        windowed_vsync_problem(options, environ),
    )
    return [problem for problem in candidates if problem]


def performance_summary(prefix=None, data_dir=None, environ=None,
                        meminfo_path=None):
    """One short status word for Doctor's aligned report."""
    problems = performance_problems(prefix, data_dir, environ, meminfo_path)
    if not problems:
        return "OK (nothing found)"
    return "%d notice%s" % (len(problems), "" if len(problems) == 1 else "s")
