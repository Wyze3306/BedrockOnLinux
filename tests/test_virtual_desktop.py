"""Minecraft inside Wine's virtual desktop (#189, #202).

The desktop "Keep the mouse inside the window" runs the game in is as large
as the screen. Minimized from the title bar Wine draws for it, the game
shrank inside the desktop and the desktop stayed on screen; these tests hold
the launcher to minimizing the desktop then, and to giving the game back
when the desktop is back. Switched to fullscreen, the game's picture stayed
below the title bar Wine no longer drew; they also hold the launcher to
moving the picture to the client area the bridge reports.
"""
# SPDX-License-Identifier: MIT

import io
import threading
import unittest
from pathlib import Path
from unittest import mock

from bol import virtual_desktop


class _Output:
    """A bridge's output: these lines, then nothing until the test ends."""

    def __init__(self, lines, done):
        self._lines = list(lines)
        self._done = done

    def __iter__(self):
        yield from self._lines
        self._done.wait(10)


class _Bridge:
    def __init__(self, lines, done):
        self.stdout = _Output(lines, done)
        self.stdin = io.StringIO()

    def poll(self):
        return None


class RequestedTests(unittest.TestCase):
    def test_the_setting_or_the_variable_asks_for_it(self):
        self.assertTrue(virtual_desktop.requested({"confine_cursor": True}, {}))
        self.assertTrue(virtual_desktop.requested(
            {}, {"BOL_CONFINE_CURSOR": "1"}))
        self.assertFalse(virtual_desktop.requested({}, {}))
        self.assertFalse(virtual_desktop.requested(
            {"confine_cursor": False}, {"BOL_CONFINE_CURSOR": "0"}))

    def test_a_session_without_one_starts_no_bridge(self):
        with mock.patch.object(virtual_desktop, "_watch") as watched:
            self.assertIsNone(virtual_desktop.start({}, environ={},
                                                    display=":0"))
            self.assertIsNone(virtual_desktop.start(
                {"confine_cursor": True}, environ={}, display=None))
        watched.assert_not_called()

    def test_a_session_with_one_is_bridged(self):
        with mock.patch.object(virtual_desktop, "_watch") as watched:
            watcher = virtual_desktop.start({"confine_cursor": True},
                                            environ={}, display=":0")
            watcher.join(5)
        watched.assert_called_once_with(":0")


class FollowTests(unittest.TestCase):
    def _follow(self, lines, shown, polls=8, iconified=1):
        done = threading.Event()
        self.addCleanup(done.set)
        bridge = _Bridge(lines, done)
        running = iter([True] * polls + [False])
        with mock.patch.object(virtual_desktop, "_mc_running",
                               side_effect=lambda: next(running)), \
                mock.patch.object(virtual_desktop, "_POLL", 0.01), \
                mock.patch.object(virtual_desktop, "LOGS", Path(self.tmp)), \
                mock.patch.object(virtual_desktop.x11, "iconify_windows",
                                  return_value=iconified) as iconify, \
                mock.patch.object(virtual_desktop.x11,
                                  "find_presentable_window",
                                  side_effect=shown) as looked:
            virtual_desktop._follow(bridge, ":0")
        return bridge.stdin.getvalue(), iconify, looked

    def setUp(self):
        import tempfile
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = holder.name

    def test_a_minimized_game_takes_the_desktop_with_it_and_comes_back(self):
        # Minimized; the window manager hides the desktop; the player brings
        # the desktop back from the taskbar.
        sent, iconify, _ = self._follow(
            ["iconic\n"], shown=[False, False, True] + [True] * 10)
        iconify.assert_called_once_with("explorer.exe", ":0")
        self.assertEqual(sent, "restore\n")

    def test_the_game_is_not_restored_before_the_desktop_ever_left(self):
        # A window manager slow to act still shows the desktop at first.
        sent, _, _ = self._follow(["iconic\n"], shown=[True, True, False,
                                                        True] + [True] * 10,
                                  polls=10)
        self.assertEqual(sent, "restore\n")
        with mock.patch.object(virtual_desktop, "_ICONIFY_GRACE", 0):
            sent, _, _ = self._follow(["iconic\n"],
                                      shown=lambda *a: True)
        self.assertEqual(sent, "")

    def test_a_game_restored_inside_the_desktop_needs_nothing(self):
        sent, _, looked = self._follow(["iconic\n", "normal\n"],
                                       shown=lambda *a: True)
        self.assertEqual(sent, "")
        looked.assert_not_called()

    def test_no_desktop_window_means_nothing_to_wait_for(self):
        sent, _, looked = self._follow(["iconic\n"], shown=lambda *a: True,
                                       iconified=0)
        self.assertEqual(sent, "")
        looked.assert_not_called()


class ClientAreaTests(unittest.TestCase):
    """The bridge reports where the game's client area starts in its X
    window; the launcher puts the picture there (#202)."""

    GAME = 0x1200001

    def setUp(self):
        import tempfile
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = holder.name

    def _follow(self, lines, placed, polls=8):
        done = threading.Event()
        self.addCleanup(done.set)
        bridge = _Bridge(lines, done)
        running = iter([True] * polls + [False])
        with mock.patch.object(virtual_desktop, "_mc_running",
                               side_effect=lambda: next(running)), \
                mock.patch.object(virtual_desktop, "_POLL", 0.01), \
                mock.patch.object(virtual_desktop, "LOGS", Path(self.tmp)), \
                mock.patch.object(virtual_desktop.x11,
                                  "place_child_windows",
                                  side_effect=placed) as place:
            virtual_desktop._follow(bridge, ":0")
        log = Path(self.tmp) / "virtual-desktop.log"
        return place, log.read_text() if log.exists() else ""

    def test_a_client_line_is_read(self):
        read = virtual_desktop._client_area
        self.assertEqual(read("client 0x1200001 3 3"), (self.GAME, 3, 3))
        self.assertEqual(read("client 0x1200001 4 30"), (self.GAME, 4, 30))
        for line in ("", "iconic", "client", "client 0x1200001 3",
                     "client nope 3 3", "client 0x1200001 3 3 3",
                     "client 0 3 3", "client 0x1200001 -1 3",
                     "client 0x1200001 3 99999"):
            self.assertIsNone(read(line), line)

    def test_fullscreen_moves_the_picture_to_the_client_area(self):
        place, log = self._follow(["client 0x1200001 3 3\n"],
                                  placed=[1])
        place.assert_called_once_with(self.GAME, 3, 3, ":0")
        self.assertIn("client area at 3,3", log)

    def test_a_picture_already_in_place_is_not_logged(self):
        place, log = self._follow(["client 0x1200001 4 30\n"],
                                  placed=[0])
        place.assert_called_once_with(self.GAME, 4, 30, ":0")
        self.assertEqual(log, "")

    def test_the_picture_is_placed_once_the_game_has_made_it(self):
        # The window is reported before the swap chain exists.
        place, _ = self._follow(["client 0x1200001 3 3\n"],
                                placed=[None, None, 1], polls=10)
        self.assertEqual(place.call_count, 3)

    def test_a_picture_that_never_comes_is_given_up(self):
        with mock.patch.object(virtual_desktop, "_PLACE_GRACE", 0):
            place, log = self._follow(["client 0x1200001 3 3\n"],
                                      placed=lambda *a: None)
        place.assert_called_once()
        self.assertIn("no X window", log)

    def test_back_to_windowed_puts_it_below_the_title_bar_again(self):
        place, _ = self._follow(["client 0x1200001 3 3\n",
                                 "client 0x1200001 4 30\n"],
                                placed=[1, 1])
        self.assertEqual(place.call_args_list,
                         [mock.call(self.GAME, 3, 3, ":0"),
                          mock.call(self.GAME, 4, 30, ":0")])

    def test_minimizing_still_works_alongside(self):
        done = threading.Event()
        self.addCleanup(done.set)
        bridge = _Bridge(["client 0x1200001 4 30\n", "iconic\n"], done)
        running = iter([True] * 8 + [False])
        with mock.patch.object(virtual_desktop, "_mc_running",
                               side_effect=lambda: next(running)), \
                mock.patch.object(virtual_desktop, "_POLL", 0.01), \
                mock.patch.object(virtual_desktop, "LOGS", Path(self.tmp)), \
                mock.patch.object(virtual_desktop.x11, "place_child_windows",
                                  return_value=0), \
                mock.patch.object(virtual_desktop.x11, "iconify_windows",
                                  return_value=1) as iconify, \
                mock.patch.object(virtual_desktop.x11,
                                  "find_presentable_window",
                                  side_effect=[False, True] + [True] * 10):
            virtual_desktop._follow(bridge, ":0")
        iconify.assert_called_once_with("explorer.exe", ":0")
        self.assertEqual(bridge.stdin.getvalue(), "restore\n")


class WatchTests(unittest.TestCase):
    def test_a_game_that_never_starts_runs_no_bridge(self):
        with mock.patch.object(virtual_desktop, "_mc_running",
                               return_value=False), \
                mock.patch.object(virtual_desktop, "_PROCESS_CEILING", 0), \
                mock.patch.object(virtual_desktop.subprocess,
                                  "Popen") as spawned:
            virtual_desktop._watch(":0")
        spawned.assert_not_called()

    def test_the_bridge_is_ended_by_closing_its_input(self):
        proc = mock.Mock()
        with mock.patch.object(virtual_desktop, "_mc_running",
                               return_value=True), \
                mock.patch.object(virtual_desktop.winehelper, "command",
                                  return_value=(["wine", "bridge.exe"], {})), \
                mock.patch.object(virtual_desktop.subprocess, "Popen",
                                  return_value=proc), \
                mock.patch.object(virtual_desktop, "_follow") as followed:
            virtual_desktop._watch(":0")
        followed.assert_called_once_with(proc, ":0")
        proc.stdin.close.assert_called_once_with()
        proc.wait.assert_called_once_with(5)

    def test_the_helper_is_shipped_with_the_package(self):
        helper = Path(virtual_desktop.__file__).with_name(virtual_desktop.EXE)
        self.assertEqual(helper.read_bytes()[:2], b"MZ")


if __name__ == "__main__":
    unittest.main()
