"""Restarting the launcher after it updated itself.

A .pyz's __main__ has a module spec too, named "__main__". Taken for
`python3 -m bol`, it sent the restart to `python3 -m bol gui`, which cannot
find bol outside the archive: the window closed and nothing came back.

The restart is an execv, and it used to be offered while Minecraft ran. The
launch thread never reached its teardown, the GPU safety marker stayed
"running" under a PID and start time that execv keeps, and the new launcher
refused every PLAY as a launch of its own still under way.
"""
# SPDX-License-Identifier: MIT

import importlib.machinery
import os
import sys
import types
import unittest
from unittest import mock

from bol import gui
from bol.gui import MainWindow
from tests.guiharness import headless_window, qt_app


def _first_exec(main_spec_name, argv0):
    main = types.ModuleType("__main__")
    main.__spec__ = (importlib.machinery.ModuleSpec(main_spec_name, None)
                     if main_spec_name else None)
    window = types.SimpleNamespace(na=mock.Mock())
    environ = {k: v for k, v in os.environ.items() if k != "APPIMAGE"}
    with mock.patch.dict(os.environ, environ, clear=True), \
            mock.patch.dict(sys.modules, {"__main__": main}), \
            mock.patch.object(sys, "argv", [argv0]), \
            mock.patch("os.execv") as execv:
        MainWindow.relaunch_app(window)
    return execv.call_args_list[0].args


class RelaunchTests(unittest.TestCase):
    def test_a_pyz_restarts_itself(self):
        self.assertEqual(
            _first_exec("__main__", "/opt/bol/bedrock-on-linux.pyz"),
            (sys.executable,
             [sys.executable, os.path.realpath("/opt/bol/bedrock-on-linux.pyz"),
              "gui"]))

    def test_python_m_bol_restarts_with_m(self):
        self.assertEqual(
            _first_exec("bol.__main__", "/src/bol/__main__.py"),
            (sys.executable, [sys.executable, "-m", "bol", "gui"]))

    def test_a_script_restarts_itself(self):
        self.assertEqual(
            _first_exec(None, "/usr/bin/bedrock-on-linux"),
            (sys.executable,
             [sys.executable, os.path.realpath("/usr/bin/bedrock-on-linux"),
              "gui"]))



class RestartingDuringALaunchTests(unittest.TestCase):
    """The update's restart waits for the launch, as relocation does."""

    def test_an_update_installed_during_a_game_does_not_restart(self):
        qt_app()
        with headless_window() as window, \
                mock.patch.object(window, "question_box") as asked, \
                mock.patch("os.execv") as execv:
            window.ui_state["launch_active"] = True
            window._restart_prompt()
            self.assertFalse(asked.called,
                             "a Yes here replaces the process under the game")
            self.assertFalse(execv.called)
            self.assertTrue(window.ui_state.get("restart_pending"))

    def test_the_restart_is_offered_once_the_game_has_closed(self):
        qt_app()
        with headless_window() as window, \
                mock.patch.object(window, "question_box",
                                  return_value=True) as asked, \
                mock.patch("os.execv") as execv:
            window.ui_state["launch_active"] = True
            window._restart_prompt()
            self.assertFalse(asked.called)
            window._play_finished("closed")
            asked.assert_called_once()
            self.assertTrue(execv.called)
            self.assertNotIn("restart_pending", window.ui_state)

    def test_a_launch_that_could_not_start_offers_it_too(self):
        qt_app()
        with headless_window() as window, \
                mock.patch.object(gui, "gpu_crash_acknowledgement_status",
                                  return_value=None), \
                mock.patch.object(window, "error_box"), \
                mock.patch.object(window, "question_box",
                                  return_value=True) as asked, \
                mock.patch("os.execv") as execv:
            window.ui_state["launch_active"] = True
            window._restart_prompt()
            self.assertFalse(asked.called)
            window._play_failed("the prefix is broken")
            asked.assert_called_once()
            self.assertTrue(execv.called)

    def test_play_resumed_by_a_store_sign_in_keeps_it_waiting(self):
        qt_app()
        with headless_window() as window, \
                mock.patch.object(window, "_offer_store_account_link",
                                  return_value=True), \
                mock.patch.object(
                    window, "_link_store_account",
                    side_effect=lambda then: window.ui_state.update(
                        store_login_active=True)), \
                mock.patch.object(window, "question_box") as asked, \
                mock.patch("os.execv") as execv:
            window.ui_state["launch_active"] = True
            window._restart_prompt()
            window._store_signin_needed("no account for the download")
            self.assertFalse(asked.called,
                             "the restart would cut the sign-in PLAY waits on")
            self.assertFalse(execv.called)
            self.assertTrue(window.ui_state.get("restart_pending"))

    def test_a_launch_started_under_the_question_defers_the_restart(self):
        # A Store sign-in that completes while the question is open resumes
        # PLAY by itself, so the Yes can arrive with a launch under way.
        qt_app()
        with headless_window() as window:
            def play_starts_then_yes(*_args):
                window.ui_state["launch_active"] = True
                return True

            with mock.patch.object(window, "question_box",
                                   side_effect=play_starts_then_yes), \
                    mock.patch("os.execv") as execv:
                window._restart_prompt()
            self.assertFalse(execv.called)
            self.assertTrue(window.ui_state.get("restart_pending"))

    def test_without_a_launch_the_restart_is_offered_at_once(self):
        qt_app()
        with headless_window() as window, \
                mock.patch.object(window, "question_box",
                                  return_value=True) as asked, \
                mock.patch("os.execv") as execv:
            window._restart_prompt()
            asked.assert_called_once()
            self.assertTrue(execv.called)
            self.assertNotIn("restart_pending", window.ui_state)


if __name__ == "__main__":
    unittest.main()
