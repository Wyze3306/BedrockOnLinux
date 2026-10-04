"""bol.swapguard — keep Minecraft's own memory out of swap while it runs.

On a desktop that has run short of memory the kernel pages out whatever was
touched least recently, and a game is full of exactly that: the world behind
the player, the chunks a moment ago, everything while its window is in the
background. Each of those pages comes back through a major fault on the
thread that touches it — Bedrock's main thread or one of its chunk streaming
workers — and the frame waits for the disk. Nothing of it reaches a Wine,
Proton or vkd3d log, so it is reported as lag, stutter or a slow GPU.

Measured on a 16 GiB desktop with 13 GiB already in swap, in the first
seconds of play after coming back to the game: 27,203 major faults in three
seconds, frames of 133 ms at the 95th percentile and 324 ms at worst, against
a median frame of 16.8 ms. The GPU sat at 11%.

cgroup v2 can forbid one group of processes from using swap at all
(``memory.swap.max`` = 0), and the user's own systemd instance may set that
on a scope it creates, with no privilege, wherever it manages a delegated
memory controller — the default on systemd distributions. The game then keeps
its pages in RAM and reclaim falls on the page cache and on every other
program instead, which while the player is playing are the ones nobody is
looking at. It is a limit on the game alone: the rest of the desktop swaps
exactly as before.

Whether that works is probed, not assumed: a throwaway scope has to read its
own limit back as 0, through the same environment the game will be started
with. A Flatpak sandbox, a host without systemd, a session with no user bus
and a controller that is not delegated all fail that probe, and the game then
starts exactly as it always has. ``BOL_ALLOW_GAME_SWAP=1`` turns it off.
"""
# SPDX-License-Identifier: MIT

import shutil
import subprocess

from .util import env_flag
from .xdg_migration import is_flatpak

SYSTEMD_RUN = "systemd-run"

# The one property this is about. Nothing else is changed for the game.
SCOPE_PROPERTY = "MemorySwapMax=0"

SCOPE_DESCRIPTION = "Minecraft (BedrockOnLinux)"

# Reads the limit of the cgroup the probe itself was started in; systemd only
# reports that the scope exists, not that the controller took the property.
_PROBE_SCRIPT = (
    'cg=$(sed -n "s/^0:://p" /proc/self/cgroup) && '
    'cat "/sys/fs/cgroup${cg}/memory.swap.max"'
)

# The user bus normally answers in milliseconds; this bounds a hung one.
_PROBE_TIMEOUT_S = 10


def _scope_argv(command):
    return [SYSTEMD_RUN, "--user", "--scope", "--quiet", "--collect",
            "--description=" + SCOPE_DESCRIPTION,
            "-p", SCOPE_PROPERTY, "--"] + list(command)


def swap_guard_available(env=None, runner=None, which=None):
    """Whether a scope created for the game can really be kept out of swap.

    ``env`` is the environment the game will be started with: the user bus
    has to be reachable from it, not from the launcher's own.
    """
    if is_flatpak(env):
        # The sandbox has no route to the host's systemd, and the probe
        # would only find that out the slow way.
        return False
    which = shutil.which if which is None else which
    if not which(SYSTEMD_RUN):
        return False
    runner = subprocess.run if runner is None else runner
    try:
        probe = runner(_scope_argv(["sh", "-c", _PROBE_SCRIPT]), env=env,
                       stdin=subprocess.DEVNULL, capture_output=True,
                       text=True, timeout=_PROBE_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return probe.returncode == 0 and str(probe.stdout).strip() == "0"


def guard_game_command(command, env, runner=None, which=None):
    """*command* started in a scope that may not swap, when that is possible.

    Returns the command to run and whether it is guarded. Unguarded, it is
    the command exactly as given.
    """
    if env_flag((env or {}).get("BOL_ALLOW_GAME_SWAP")):
        return list(command), False
    if not swap_guard_available(env, runner=runner, which=which):
        return list(command), False
    return _scope_argv(command), True
