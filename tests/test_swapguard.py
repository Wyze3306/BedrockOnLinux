"""Keep the game's own memory out of swap, and only where that really works.

A desktop short of memory pages the game out first, and every page then comes
back through a major fault mid-frame: 27,203 of them in three seconds of play
on the machine that reported it, for frames of up to 324 ms. These tests hold
both halves: the game is started in a scope that may not swap whenever the
user's systemd can enforce it, and exactly as before whenever it cannot,
since a wrapper that fails would be a game that does not start.
"""
# SPDX-License-Identifier: MIT

import subprocess
import unittest

from bol import swapguard


def _probe(stdout="0\n", returncode=0, raises=None):
    """A stand-in for subprocess.run that records what it was asked."""
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    return run, calls


def _systemd_run(name):
    return "/usr/bin/" + name if name == "systemd-run" else None


class SwapGuardProbeTests(unittest.TestCase):
    def test_a_scope_that_reads_its_limit_back_as_zero_is_available(self):
        run, calls = _probe()
        self.assertTrue(swapguard.swap_guard_available(
            {"XDG_RUNTIME_DIR": "/run/user/1000"}, runner=run,
            which=_systemd_run))
        argv, kwargs = calls[0]
        self.assertEqual(argv[:5], ["systemd-run", "--user", "--scope",
                                    "--quiet", "--collect"])
        self.assertIn("MemorySwapMax=0", argv)
        # The probe reads its own cgroup: systemd accepting the property is
        # not proof that a delegated controller enforces it.
        self.assertIn("memory.swap.max", argv[-1])
        # The bus has to be reachable from the game's environment.
        self.assertEqual(kwargs["env"], {"XDG_RUNTIME_DIR": "/run/user/1000"})
        self.assertIn("timeout", kwargs)

    def test_a_controller_that_is_not_delegated_is_unavailable(self):
        # Without the memory controller the scope still starts, and the
        # limit it reads back is the unlimited default.
        run, _ = _probe(stdout="max\n")
        self.assertFalse(swapguard.swap_guard_available(
            {}, runner=run, which=_systemd_run))

    def test_a_scope_that_cannot_be_created_is_unavailable(self):
        run, _ = _probe(stdout="", returncode=1)
        self.assertFalse(swapguard.swap_guard_available(
            {}, runner=run, which=_systemd_run))

    def test_a_hung_user_bus_is_unavailable_not_a_failed_launch(self):
        run, _ = _probe(raises=subprocess.TimeoutExpired("systemd-run", 10))
        self.assertFalse(swapguard.swap_guard_available(
            {}, runner=run, which=_systemd_run))

    def test_a_probe_that_cannot_start_is_unavailable(self):
        run, _ = _probe(raises=OSError("exec format error"))
        self.assertFalse(swapguard.swap_guard_available(
            {}, runner=run, which=_systemd_run))

    def test_a_host_without_systemd_is_never_probed(self):
        run, calls = _probe()
        self.assertFalse(swapguard.swap_guard_available(
            {}, runner=run, which=lambda _name: None))
        self.assertEqual(calls, [])

    def test_the_flatpak_sandbox_is_never_probed(self):
        run, calls = _probe()
        self.assertFalse(swapguard.swap_guard_available(
            {"FLATPAK_ID": "io.github.wyze3306.BedrockOnLinux"}, runner=run,
            which=_systemd_run))
        self.assertEqual(calls, [])


class GuardedCommandTests(unittest.TestCase):
    def test_the_game_runs_inside_the_scope_unchanged(self):
        run, _ = _probe()
        game = ["python3", "/umu/umu-run", "Minecraft.Windows.exe"]
        command, guarded = swapguard.guard_game_command(
            game, {}, runner=run, which=_systemd_run)
        self.assertTrue(guarded)
        self.assertEqual(command[0], "systemd-run")
        self.assertIn("MemorySwapMax=0", command)
        separator = command.index("--")
        self.assertEqual(command[separator + 1:], game)
        # Nothing but the swap limit is changed for the game.
        properties = [command[index + 1]
                      for index, arg in enumerate(command[:separator])
                      if arg == "-p"]
        self.assertEqual(properties, ["MemorySwapMax=0"])

    def test_the_callers_command_is_not_modified(self):
        run, _ = _probe()
        game = ["umu-run", "Minecraft.Windows.exe"]
        swapguard.guard_game_command(game, {}, runner=run, which=_systemd_run)
        self.assertEqual(game, ["umu-run", "Minecraft.Windows.exe"])

    def test_an_unavailable_guard_leaves_the_command_as_it_was(self):
        run, _ = _probe(stdout="max\n")
        game = ["umu-run", "Minecraft.Windows.exe"]
        command, guarded = swapguard.guard_game_command(
            game, {}, runner=run, which=_systemd_run)
        self.assertFalse(guarded)
        self.assertEqual(command, game)

    def test_the_guard_can_be_turned_off_without_a_probe(self):
        run, calls = _probe()
        game = ["umu-run", "Minecraft.Windows.exe"]
        command, guarded = swapguard.guard_game_command(
            game, {"BOL_ALLOW_GAME_SWAP": "1"}, runner=run,
            which=_systemd_run)
        self.assertFalse(guarded)
        self.assertEqual(command, game)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
