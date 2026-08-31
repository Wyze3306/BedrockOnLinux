"""Closing the launcher when the game starts, without abandoning the session.

The window is the only thing the setting takes away. The process behind it
still has to see the launch out: it armed a GPU safety marker before starting
the game, and a marker nobody watches return blocks the next launch until a
reboot. These tests pin the decision and the wiring that keeps that true.

The Qt rewrite moved this from one long Tk closure (do_play.work) into a
QThread subclass (LaunchWorker) whose UI-facing steps go through Qt signals
instead of a scheduling call like Tk's root.after -- Qt queues cross-thread
signal delivery onto the GUI thread itself, so LaunchWorker.run() must never
touch window widgets directly, only emit.
"""
# SPDX-License-Identifier: MIT

import ast
import inspect
import threading
import unittest
from pathlib import Path

from bol import gui
from bol.gui import window_action_for_launch
from tests.guiharness import headless_window, qt_app


class WindowActionTests(unittest.TestCase):
    def test_an_ordinary_desktop_keeps_both_windows(self):
        self.assertEqual(window_action_for_launch({}, False), "stay")

    def test_the_setting_is_off_until_it_is_turned_on(self):
        # Settings written before the switch existed carry no key at all.
        self.assertEqual(window_action_for_launch({"light_theme": True}, False),
                         "stay")
        self.assertEqual(window_action_for_launch(None, False), "stay")

    def test_a_single_window_session_steps_aside_on_its_own(self):
        self.assertEqual(window_action_for_launch({}, True), "step-aside")

    def test_the_setting_closes_the_window(self):
        self.assertEqual(
            window_action_for_launch({"close_on_launch": True}, False), "close")

    def test_closing_wins_over_stepping_aside(self):
        # Both take the window off the screen; only one was asked for, and
        # coming back afterwards would contradict it.
        self.assertEqual(
            window_action_for_launch({"close_on_launch": True}, True), "close")

    def test_the_setting_being_off_still_steps_aside_in_game_mode(self):
        self.assertEqual(
            window_action_for_launch({"close_on_launch": False}, True),
            "step-aside")


def _find_scope(tree, name):
    """The class or function node named `name`, searched anywhere in the
    tree (not just at module level)."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name == name:
            return node
    return None


def _nested(tree, dotted_name):
    """Resolve a dotted path like 'LaunchWorker.run' to its FunctionDef,
    searching each component within the previous one's body."""
    scope = tree
    for part in dotted_name.split("."):
        found = None
        for node in ast.walk(scope):
            if node is scope:
                continue
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name == part:
                found = node
                break
        if found is None:
            return None
        scope = found
    return scope


class ClosedWindowWiringTests(unittest.TestCase):
    """gui.py's window/thread wiring is read rather than run, since it needs
    a live QThread + event loop to exercise directly."""

    def setUp(self):
        source = Path(inspect.getsourcefile(gui)).read_text(encoding="utf-8")
        self.tree = ast.parse(source)

    def _calls_in(self, dotted_name):
        node = _nested(self.tree, dotted_name)
        self.assertIsNotNone(node, f"{dotted_name} not found in bol.gui")
        return {
            call.func.id for call in ast.walk(node)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
        }

    def _attribute_calls_in(self, dotted_name):
        node = _nested(self.tree, dotted_name)
        self.assertIsNotNone(node, f"{dotted_name} not found in bol.gui")
        return {
            call.func.attr for call in ast.walk(node)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
        }

    def test_the_launch_thread_decides_with_the_shared_helper(self):
        self.assertIn(
            "window_action_for_launch", self._calls_in("LaunchWorker.run"))

    def test_the_launch_thread_signals_close_or_step_aside_not_both(self):
        signals = self._attribute_calls_in("LaunchWorker.run")
        self.assertIn("emit", signals)
        # The action is read once and only one outcome path taken; both
        # signal-emitting calls exist in source (one per branch), which is
        # the closest static proxy for "either, never both" without running
        # a full QThread + event loop here.
        node = _nested(self.tree, "LaunchWorker.run")
        emitted = {
            call.func.value.attr
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "emit"
            and isinstance(call.func.value, ast.Attribute)
        }
        self.assertIn("close_window", emitted)
        self.assertIn("step_aside", emitted)

    def test_the_game_start_hook_can_close_or_step_aside(self):
        connections = self._attribute_calls_in("do_play")
        self.assertIn("connect", connections)
        text = ast.unparse(_nested(self.tree, "do_play"))
        self.assertIn("close_window.connect(self._close_for_game)", text)
        self.assertIn("step_aside.connect(self._step_aside_for_game)", text)

    def test_closing_destroys_the_window_and_stops_the_auth_poller(self):
        shut = self._attribute_calls_in("MainWindow._close_for_game")
        self.assertIn("stop", shut)   # self.na.stop()
        self.assertIn("close", shut)  # self.close() -- Qt's window teardown

    def test_stepping_aside_hides_rather_than_closes(self):
        step = self._attribute_calls_in("MainWindow._step_aside_for_game")
        self.assertIn("hide", step)
        self.assertNotIn("close", step)

    def test_coming_back_restores_the_window(self):
        come_back = self._attribute_calls_in("MainWindow._come_back_from_game")
        self.assertIn("showNormal", come_back)

    def test_the_launch_thread_only_ever_emits_never_touches_widgets(self):
        # The safety property Tk's root.after existed to enforce: nothing
        # running on the worker thread may call into the window directly.
        # Under Qt the equivalent is that LaunchWorker.run() (and everything
        # it calls in-thread: do_setup, launch) only ever reaches the UI
        # through self.<signal>.emit(...), never a direct window method.
        node = _nested(self.tree, "LaunchWorker.run")
        attr_calls = [
            call for call in ast.walk(node)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Attribute)
            and isinstance(call.func.value.value, ast.Name)
            and call.func.value.value.id == "self"
        ]
        for call in attr_calls:
            self.assertEqual(call.func.attr, "emit",
                              f"self.{call.func.value.attr}.{call.func.attr}(...) "
                              "reaches off-thread by something other than emit()")

    def test_a_failure_with_no_window_left_is_still_reported(self):
        self.assertIn("desktop_notify", self._calls_in("MainWindow._play_failed"))


class ClosingWithWorkRunningTests(unittest.TestCase):
    """A window may not take a running thread with it.

    `_workers` holds the only reference to each background job, so it dies
    with the window -- and Qt destroys a QThread that is still running by
    aborting the process. Closing the launcher during the Settings > Versions
    disk scan did that, and so did the test suite, which opened a Settings tab
    in one test and was still scanning several tests later.
    """

    def setUp(self):
        self.app = qt_app()
        self.addCleanup(gui._ORPHAN_WORKERS.clear)

    def test_a_worker_still_running_outlives_the_window_that_closed(self):
        release = threading.Event()
        self.addCleanup(release.set)
        with headless_window() as window:
            worker = gui.Worker(lambda: release.wait(10))
            self.assertTrue(window._start_worker("slow", worker))
            while not worker.isRunning():  # started, not merely constructed
                pass
            window._force_close = True
            window.close()
            self.assertIn(worker, gui._ORPHAN_WORKERS)
            self.assertTrue(worker.isRunning())
        release.set()
        self.assertTrue(worker.wait(10000))

    def test_a_worker_that_finishes_stops_being_held(self):
        # Parking is not a leak: the list is emptied by finished(), or the
        # launcher would accumulate every worker it ever closed over.
        with headless_window() as window:
            worker = gui.Worker(lambda: None)
            window._start_worker("quick", worker)
            self.assertTrue(worker.wait(10000))
            window._force_close = True
            window.close()
        self.app.processEvents()
        self.assertNotIn(worker, gui._ORPHAN_WORKERS)

    def test_the_settings_scan_never_reaches_the_real_games_folder(self):
        # What made the suite abort: opening Settings > Versions walked the
        # developer's own install tree on a thread nothing waited for. The
        # harness has to keep that off the disk, and the tab has to be the
        # thing that asks for it.
        with headless_window() as window:
            window.toggle_settings()
            window.settings_tabs.setCurrentIndex(window._versions_tab)
            worker = window._workers.get("builds")
            self.assertIsNotNone(worker, "the Versions tab started no scan")
            self.assertTrue(worker.wait(10000))
            gui.installed_builds.assert_called()


if __name__ == "__main__":
    unittest.main()
