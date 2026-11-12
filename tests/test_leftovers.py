"""Closing what the game's WebView2 leaves running once Minecraft has exited.

MicrosoftEdgeUpdate.exe never exits under Wine and Proton's wrapper waits
for it, so closing the game did not end the session and PLAY kept answering
that Minecraft was still running.
"""
# SPDX-License-Identifier: MIT

import signal
import unittest

from bol import leftovers


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Reaper(unittest.TestCase):

    def setUp(self):
        self.clock = Clock()
        self.procs = {}
        self.kills = []
        self.reaper = leftovers.LeftoverReaper(
            "/pfx", scan=lambda _p: list(self.procs),
            image=lambda pid: self.procs.get(pid, ""),
            kill=lambda pid, sig: self.kills.append((pid, sig)),
            clock=self.clock)

    def at(self, t):
        self.clock.now = t
        return self.reaper.tick()

    def test_nothing_is_closed_while_the_game_runs(self):
        self.procs = {10: "minecraft.windows.exe",
                      11: "microsoftedgeupdate.exe", 12: "services.exe"}
        for t in range(0, 60, 2):
            self.at(t)
        self.assertEqual(self.kills, [])

    def test_the_updater_is_closed_after_the_game_has_gone(self):
        self.procs = {10: "minecraft.windows.exe",
                      11: "microsoftedgeupdate.exe", 12: "rpcss.exe"}
        self.at(0)
        del self.procs[10]
        self.at(2)  # gone, grace starts
        self.assertEqual(self.kills, [])
        self.at(4)
        self.assertEqual(self.kills, [])
        self.at(8)
        self.assertEqual(self.kills, [(11, signal.SIGTERM)])
        self.at(10)  # still there, not yet time for SIGKILL
        self.assertEqual(self.kills, [(11, signal.SIGTERM)])
        self.at(14)
        self.assertEqual(self.kills[-1], (11, signal.SIGKILL))
        # Wine's own services are left to leave by themselves.
        self.assertNotIn(12, [pid for pid, _sig in self.kills])

    def test_a_game_that_comes_back_within_the_grace_is_left_alone(self):
        self.procs = {10: "minecraft.windows.exe",
                      11: "microsoftedgeupdate.exe"}
        self.at(0)
        del self.procs[10]
        self.at(2)
        self.procs[20] = "minecraft.windows.exe"
        self.at(4)
        self.at(30)
        self.assertEqual(self.kills, [])

    def test_nothing_happens_before_the_game_was_ever_seen(self):
        self.procs = {11: "microsoftedgeupdate.exe"}
        for t in range(0, 60, 2):
            self.at(t)
        self.assertEqual(self.kills, [])

    def test_scans_are_spaced_out(self):
        calls = []
        reaper = leftovers.LeftoverReaper(
            "/pfx", scan=lambda _p: calls.append(1) or [],
            image=lambda _pid: "", kill=lambda *_: None, clock=self.clock)
        for t in (0, 0.5, 1, 1.5, 2, 2.5):
            self.clock.now = t
            reaper.tick()
        self.assertEqual(len(calls), 2)


class Image(unittest.TestCase):

    def test_windows_argv0_is_read_and_linux_wrappers_are_not_the_game(self):
        import os
        import tempfile
        from unittest import mock

        cases = {
            b"C:\\Program Files (x86)\\Microsoft\\EdgeUpdate\\"
            b"MicrosoftEdgeUpdate.exe\0/c\0": "microsoftedgeupdate.exe",
            b"S:\\.local\\share\\bol\\content\\Minecraft.Windows.exe\0\0":
                "minecraft.windows.exe",
            b"/usr/bin/python3\0/x/umu-run\0/x/content/Minecraft.Windows.exe"
            b"\0": "python3",
        }
        for raw, expected in cases.items():
            with tempfile.NamedTemporaryFile(delete=False) as f:
                f.write(raw)
            try:
                real_open = open
                with mock.patch("builtins.open",
                                lambda p, m="r", *a, **k: real_open(
                                    f.name, m, *a, **k)):
                    self.assertEqual(leftovers._image(1), expected)
            finally:
                os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()


def test_wine_processes_are_found_under_protons_own_prefix_path():
    """Proton points Wine at <prefix>/pfx/ while the launcher's wrappers keep
    <prefix>: both belong to the prefix, or the reaper never sees the game
    (and the session never ends once it closes)."""
    from bol.prefix import _environ_uses_prefix
    prefix = "/data/compatdata/pfx"
    for value in (prefix, prefix + "/", prefix + "/pfx", prefix + "/pfx/"):
        environ = b"A=1\0WINEPREFIX=" + value.encode() + b"\0B=2"
        assert _environ_uses_prefix(environ, prefix), value
    for value in ("/data/compatdata/pfx2", "/data/compatdata/pfx/pfx/x",
                  "/data/compatdata", "/other/compatdata/pfx/pfx"):
        environ = b"WINEPREFIX=" + value.encode()
        assert not _environ_uses_prefix(environ, prefix), value
    assert not _environ_uses_prefix(
        b"STEAM_COMPAT_DATA_PATH=/data/compatdata/pfx", prefix)
