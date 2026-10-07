"""A game started in fullscreen gets a frame its size (#283).

Started fullscreen, Minecraft keeps the swap chain of its maximized window
and stretches it over the screen, so clicks land above what the pointer
shows. It resizes for any later change of size; these tests hold when the
launcher sends it one, and that it never does for a game started windowed.
"""
# SPDX-License-Identifier: MIT

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import fullscreen_fix, winehelper

_OPTIONS = ("drive_c/users/steamuser/AppData/Roaming/Minecraft Bedrock/Users/"
            "1/games/com.mojang/minecraftpe/options.txt")


def _prefix(root, fullscreen):
    options = Path(root) / _OPTIONS
    options.parent.mkdir(parents=True)
    options.write_bytes(b"gfx_vsync:1\r\ngfx_fullscreen:%d\r\n" % fullscreen)
    return Path(root)


class StartTests(unittest.TestCase):
    def test_a_fullscreen_start_is_watched(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(fullscreen_fix, "_watch"):
            watcher = fullscreen_fix.start(environ={},
                                           prefix=_prefix(td, 1))
            watcher.join(1)
        self.assertIsNotNone(watcher)

    def test_a_windowed_start_is_left_alone(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(fullscreen_fix.start(environ={},
                                                   prefix=_prefix(td, 0)))

    def test_a_game_that_never_wrote_its_settings_is_left_alone(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(fullscreen_fix.start(environ={},
                                                   prefix=Path(td)))

    def test_it_can_be_turned_off(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(fullscreen_fix.start(
                environ={"BOL_FULLSCREEN_NUDGE": "0"}, prefix=_prefix(td, 1)))


class WatchTests(unittest.TestCase):
    def _watch(self, running, menu):
        with mock.patch.object(fullscreen_fix, "_mc_running",
                               side_effect=running), \
                mock.patch.object(fullscreen_fix, "_menu_reached",
                                  side_effect=menu), \
                mock.patch.object(fullscreen_fix, "_POLL", 0), \
                mock.patch.object(fullscreen_fix, "nudge") as nudged:
            fullscreen_fix._watch(since=0)
        return nudged

    def test_it_waits_for_the_game_then_its_menu(self):
        # xodus-cli and the Steam runtime come first; the game follows.
        nudged = self._watch(running=[False, False] + [True] * 10,
                             menu=[False, False, True])
        nudged.assert_called_once_with()

    def test_a_game_that_closes_first_is_not_touched(self):
        nudged = self._watch(running=[True, True, False], menu=[False])
        nudged.assert_not_called()

    def test_a_game_that_never_starts_is_not_waited_for_forever(self):
        with mock.patch.object(fullscreen_fix, "_PROCESS_CEILING", 0):
            nudged = self._watch(running=lambda: False, menu=[False])
        nudged.assert_not_called()

    def test_a_menu_that_never_says_so_is_resized_anyway(self):
        with mock.patch.object(fullscreen_fix, "_MENU_CEILING", 0):
            nudged = self._watch(running=lambda: True, menu=lambda _s: False)
        nudged.assert_called_once_with()


class NudgeTests(unittest.TestCase):
    def test_the_helper_runs_in_the_games_prefix_with_the_engine_wine(self):
        with tempfile.TemporaryDirectory() as td:
            engine = Path(td) / "engine"
            (engine / "files" / "bin").mkdir(parents=True)
            (engine / "files" / "bin" / "wine").write_text("")
            with mock.patch.object(winehelper, "proton_path",
                                   return_value=engine), \
                    mock.patch.object(winehelper, "CACHE", Path(td)), \
                    mock.patch.object(fullscreen_fix, "LOGS", Path(td)), \
                    mock.patch.object(winehelper, "active_prefix",
                                      return_value=Path("/pfx")), \
                    mock.patch.object(
                        fullscreen_fix.subprocess, "run",
                        return_value=subprocess.CompletedProcess(
                            [], 0, "resized 1600x900\n", "")) as run:
                self.assertEqual(fullscreen_fix.nudge(), 0)
                logged = (Path(td) / "fullscreen-nudge.log").read_text()
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], str(engine / "files" / "bin" / "wine"))
        self.assertTrue(argv[1].endswith(fullscreen_fix.EXE))
        self.assertEqual(run.call_args.kwargs["env"]["WINEPREFIX"], "/pfx")
        self.assertIn("resized 1600x900", logged)

    def test_the_helper_is_shipped_with_the_package(self):
        helper = Path(fullscreen_fix.__file__).with_name(fullscreen_fix.EXE)
        self.assertEqual(helper.read_bytes()[:2], b"MZ")

    def test_no_engine_means_nothing_to_run(self):
        with mock.patch.object(winehelper, "proton_path",
                               return_value=None):
            self.assertIsNone(fullscreen_fix.nudge())


if __name__ == "__main__":
    unittest.main()
