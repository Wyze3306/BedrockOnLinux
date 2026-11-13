"""The Profiles page: what it lists, that nothing on it is cut off, and what
its buttons do. No game and no second launcher is started."""
# SPDX-License-Identifier: MIT

import os
import time
import unittest
from unittest import mock

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import QLabel, QPushButton

from bol import gui_profiles, instances
from tests.guiharness import headless_window, qt_app

HOME = "/data/bedrock-on-linux"
BOT = HOME + "/profiles/bot-1"
LONG = HOME + "/profiles/a-second-account-with-a-very-long-name"
PROFILES = [
    {"name": "Bot 1", "slug": "bot-1", "path": BOT},
    {"name": "A second account with a very long name",
     "slug": "a-second-account-with-a-very-long-name", "path": LONG},
]
ACCOUNTS = {None: {"signed_in": True, "gamertag": "MainTag"},
            BOT: {"signed_in": True, "gamertag": None},
            LONG: {"signed_in": False, "gamertag": None}}
BUILDS = {None: "Release 1.26.60.5", BOT: "Release 1.26.60.5", LONG: None}


def _pump(times=20):
    for _ in range(times):
        QCoreApplication.processEvents()
        time.sleep(0.005)


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _instance(profile="Bot 1", slug="bot-1", home=BOT, hidden=False,
              backend=instances.BACKEND_X11, ident=None):
    return instances.Instance(
        id=ident or f"{slug}-abc123", profile=profile, slug=slug, home=home,
        prefix=home + "/compatdata/pfx", version="1.26.60.5",
        launcher_pid=os.getpid(), game_pid=os.getpid(),
        started=time.time() - 125, hidden=hidden, backend=backend,
        display=":0", wm_class="minecraft.windows.exe")


class ProfilesPageTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        qt_app()

    def setUp(self):
        self.running = []
        patches = [
            mock.patch.object(gui_profiles, "list_profiles",
                              side_effect=lambda: list(self.profiles)),
            mock.patch.object(gui_profiles, "current_profile_info",
                              return_value={"path": None}),
            mock.patch.object(gui_profiles, "profile_account",
                              side_effect=lambda path: ACCOUNTS[path]),
            mock.patch.object(gui_profiles, "profile_build",
                              side_effect=lambda path: BUILDS[path]),
            mock.patch.object(gui_profiles, "profile_home",
                              side_effect=lambda path: path or HOME),
            mock.patch.object(gui_profiles.instances, "list_instances",
                              side_effect=lambda: list(self.running)),
        ]
        self.profiles = list(PROFILES)
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _open(self, window, size=(1120, 700)):
        window.resize(*size)
        window.show()
        window.open_profiles()
        _pump()
        return window.profiles_page

    def _card(self, page, name):
        return next(card for card in page.profile_cards
                    if card.profile["name"] == name)

    # -- what it shows ----------------------------------------------------
    def test_one_card_per_profile_with_account_build_and_folder(self):
        with headless_window() as window:
            page = self._open(window)
            self.assertEqual(
                [card.profile["name"] for card in page.profile_cards],
                ["Default", "Bot 1", "A second account with a very long name"])
            default = self._card(page, "Default")
            self.assertIn("MainTag", default.summary.text())
            self.assertIn("Release 1.26.60.5", default.summary.text())
            self.assertEqual(default.path.text(), HOME)
            self.assertEqual(default.property("active"), "true")
            self.assertIn("This window",
                          [b.text() for b in default.badges])
            self.assertIn("signed in", self._card(page, "Bot 1")
                          .summary.text())
            self.assertIn("not signed in", self._card(
                page, "A second account with a very long name")
                .summary.text())

    def test_no_running_game_and_a_single_profile_get_their_empty_states(self):
        self.profiles = []
        with headless_window() as window:
            page = self._open(window)
            texts = [label.text() for label in page.findChildren(QLabel)]
            self.assertIn("No game is running", texts)
            self.assertIn("One profile so far", texts)
            self.assertEqual(page.instance_rows, [])

    def test_a_profile_without_a_build_offers_to_set_it_up_not_to_play(self):
        with headless_window() as window:
            page = self._open(window)
            card = self._card(page, "A second account with a very long name")
            self.assertIsNone(card.play)
            self.assertEqual(card.setup.text(), "Set up")
            with mock.patch.object(gui_profiles,
                                   "open_profile_window") as opened:
                card.setup.click()
            opened.assert_called_once_with(LONG)

    def test_running_games_are_listed_with_state_duration_and_pid(self):
        self.running = [_instance(),
                        _instance("Default", "default", HOME, hidden=True)]
        with headless_window() as window:
            page = self._open(window)
            self.assertEqual(len(page.instance_rows), 2)
            row = page.instance_rows[0]
            self.assertEqual(row.name.text(), "Bot 1")
            self.assertEqual(row.state.text(), "Visible")
            self.assertEqual(row.toggle.text(), "Hide")
            detail = row.detail.text()
            self.assertIn("Minecraft 1.26.60.5", detail)
            self.assertIn("up 2 min", detail)
            self.assertIn(f"PID {os.getpid()}", detail)
            hidden = page.instance_rows[1]
            self.assertEqual((hidden.state.text(), hidden.toggle.text()),
                             ("Hidden", "Show"))
            # A running profile is handled in "Running now", not played twice.
            card = self._card(page, "Bot 1")
            self.assertIsNone(card.play)
            self.assertIn("Running", [b.text() for b in card.badges])
            self.assertIn("Running hidden", [
                b.text() for b in self._card(page, "Default").badges])

    def test_a_game_that_cannot_be_hidden_says_why(self):
        self.running = [_instance(backend=instances.BACKEND_WAYLAND)]
        with headless_window() as window:
            page = self._open(window)
            toggle = page.instance_rows[0].toggle
            self.assertFalse(toggle.isEnabled())
            self.assertIn("Wayland", toggle.toolTip())

    # -- nothing is cut off -------------------------------------------------
    def _assert_nothing_clipped(self, page):
        for button in page.findChildren(QPushButton):
            if button.isVisible():
                self.assertGreaterEqual(
                    button.width(), button.sizeHint().width(),
                    f"button '{button.text()}' is cut off")
                self.assertGreaterEqual(
                    button.height(), button.sizeHint().height(),
                    f"button '{button.text()}' is cut off")
        for label in page.findChildren(QLabel):
            if not label.isVisible() or label.wordWrap() \
                    or isinstance(label, gui_profiles.ElidedLabel):
                continue
            self.assertGreaterEqual(
                label.width(), label.sizeHint().width() - 1,
                f"label '{label.text()}' is cut off")

    def test_no_button_or_label_is_cut_off_at_any_size_or_font(self):
        self.running = [_instance(),
                        _instance("Default", "default", HOME, hidden=True)]
        app = qt_app()
        with headless_window() as window:
            page = self._open(window)
            sheet = window.theme.qss()
            self.addCleanup(app.setStyleSheet, sheet)
            for scale in (1.0, 1.5, 2.0):
                app.setStyleSheet(
                    sheet.replace("font-size: 13px",
                                  f"font-size: {round(13 * scale)}px")
                    .replace("font-size: 11px",
                             f"font-size: {round(11 * scale)}px")
                    .replace("font-size: 15px",
                             f"font-size: {round(15 * scale)}px"))
                for size in ((1920, 1080), (1120, 700), (1000, 640)):
                    window.resize(*size)
                    page.refresh()
                    _pump()
                    with self.subTest(scale=scale, size=size):
                        self._assert_nothing_clipped(page)

    def test_a_narrow_page_puts_the_actions_under_the_text(self):
        with headless_window() as window:
            self._open(window)
            page = gui_profiles.ProfilesPage(window)
            self.addCleanup(page.deleteLater)
            page.resize(480, 800)
            page.show()
            page.refresh()
            _pump()
            card = self._card(page, "Bot 1")
            self.assertGreater(card.actions.geometry().top(),
                               card.summary.geometry().bottom())
            self._assert_nothing_clipped(page)
            page.resize(1400, 800)
            _pump()
            card = self._card(page, "Bot 1")
            self.assertLess(card.actions.geometry().top(),
                            card.path.geometry().bottom())
            page.hide()

    def test_a_long_path_is_elided_and_kept_whole_in_its_tooltip(self):
        with headless_window() as window:
            self._open(window)
            label = gui_profiles.ElidedLabel("/a/very/long/path/" + "x" * 200)
            self.addCleanup(label.deleteLater)
            label.resize(160, 20)
            label.show()
            _pump()
            label.grab()
            self.assertIn("…", label.shown_text())
            self.assertEqual(label.toolTip(), label.text())
            label.resize(4000, 20)
            label.grab()
            self.assertEqual(label.toolTip(), "")
            label.hide()

    # -- what its buttons do ------------------------------------------------
    def test_play_on_another_profile_starts_its_own_launcher_process(self):
        with headless_window() as window:
            page = self._open(window)
            process = mock.Mock()
            process.poll.return_value = None
            with mock.patch.object(gui_profiles, "start_profile_game",
                                   return_value=process) as start:
                self._card(page, "Bot 1").play_hidden.click()
                start.assert_called_once_with(BOT, hidden=True)
                _pump()
                card = self._card(page, "Bot 1")
                self.assertIn("Starting…", [b.text() for b in card.badges])
                self.assertIsNone(card.play)
                # It shows up in the list: the card follows by itself.
                self.running = [_instance(hidden=True)]
                page.poll()
                card = self._card(page, "Bot 1")
                self.assertIn("Running hidden",
                              [b.text() for b in card.badges])
                self.assertEqual(len(page.instance_rows), 1)

    def test_a_launch_that_dies_is_reported_on_the_card(self):
        with headless_window() as window:
            page = self._open(window)
            process = mock.Mock()
            process.poll.return_value = None
            with mock.patch.object(gui_profiles, "start_profile_game",
                                   return_value=process):
                self._card(page, "Bot 1").play.click()
            process.poll.return_value = 1
            process.returncode = 1
            page.poll()
            notes = [label.text() for label in page.findChildren(QLabel)
                     if label.objectName() == "Warn"]
            self.assertEqual(len(notes), 1)
            self.assertIn("did not start", notes[0])
            self.assertIsNotNone(self._card(page, "Bot 1").play)

    def test_play_on_this_windows_profile_uses_the_windows_own_launch(self):
        with headless_window() as window:
            page = self._open(window)
            with mock.patch.object(window, "do_play") as play, \
                    mock.patch.object(gui_profiles,
                                      "start_profile_game") as start:
                self._card(page, "Default").play_hidden.click()
            play.assert_called_once_with(hidden=True)
            self.assertFalse(start.called)
            self.assertIs(window.stack.currentWidget(), window.hero_page)

    def test_the_list_follows_games_started_and_stopped_elsewhere(self):
        with headless_window() as window:
            page = self._open(window)
            self.assertTrue(page.timer.isActive())
            self.assertEqual(page.instance_rows, [])
            self.running = [_instance()]
            page.poll()
            self.assertEqual(len(page.instance_rows), 1)
            row = page.instance_rows[0]
            page.poll()
            self.assertIs(page.instance_rows[0], row,
                          "nothing changed: the rows must not be rebuilt")
            self.running = []
            page.poll()
            self.assertEqual(page.instance_rows, [])
            window.go_home()
            _pump()
            self.assertFalse(page.timer.isActive())

    def test_hide_and_show_reach_the_instance(self):
        item = _instance()
        self.running = [item]
        with headless_window() as window:
            page = self._open(window)
            with mock.patch.object(gui_profiles.instances,
                                   "set_hidden") as hide:
                page.instance_rows[0].toggle.click()
            hide.assert_called_once_with(item, True)

    def test_a_refused_hide_is_shown_not_raised(self):
        self.running = [_instance()]
        with headless_window() as window:
            page = self._open(window)
            with mock.patch.object(
                    gui_profiles.instances, "set_hidden",
                    side_effect=instances.BolError("no longer running")), \
                    mock.patch.object(window, "error_box") as box:
                page.instance_rows[0].toggle.click()
            self.assertIn("no longer running", box.call_args.args[1])

    def test_close_runs_off_the_ui_thread_and_the_row_says_closing(self):
        item = _instance()
        self.running = [item]
        closed = []

        def close(instance):
            time.sleep(0.05)
            closed.append(instance.id)
            self.running = []
            return "closed"

        with headless_window() as window:
            page = self._open(window)
            with mock.patch.object(gui_profiles.instances, "close_instance",
                                   close):
                page.instance_rows[0].close_button.click()
                row = page.instance_rows[0]
                self.assertEqual(row.close_button.text(), "Closing…")
                self.assertFalse(row.close_button.isEnabled())
                self.assertFalse(row.toggle.isEnabled())
                # A second click while it closes starts nothing.
                page.close_instance(item)
                self.assertTrue(_wait(lambda: closed == [item.id]))
                self.assertTrue(_wait(lambda: not page.closing))
                worker = window._workers[f"close-{item.id}"]
                self.assertTrue(worker.wait(5000))
            self.assertEqual(page.instance_rows, [])

    def test_secondary_actions_live_in_the_menu(self):
        self.running = [_instance()]
        with headless_window() as window:
            page = self._open(window)
            idle = self._card(
                page, "A second account with a very long name")
            labels = [entry[0] for entry in idle.menu_actions()]
            self.assertEqual(labels, ["Open in a new window", "Open folder",
                                      "Rename…", "Delete…"])
            self.assertTrue(all(entry[2] for entry in idle.menu_actions()))
            self.assertTrue(idle.more.toolTip())
            # Default is the installation itself: no rename, no delete.
            default = self._card(page, "Default")
            self.assertEqual([entry[0] for entry in default.menu_actions()],
                             ["Open in a new window", "Open folder"])
            # A profile whose game runs cannot be renamed or deleted.
            busy = {entry[0]: entry for entry in
                    self._card(page, "Bot 1").menu_actions()}
            self.assertFalse(busy["Rename…"][2])
            self.assertFalse(busy["Delete…"][2])
            self.assertIn("Close this profile's game", busy["Delete…"][3])
            with mock.patch.object(window, "_rename_profile_row") as rename, \
                    mock.patch.object(window, "_delete_profile_row") as delete, \
                    mock.patch.object(gui_profiles.subprocess,
                                      "Popen") as popen:
                entries = {entry[0]: entry[1]
                           for entry in idle.menu_actions()}
                entries["Rename…"]()
                entries["Delete…"]()
                entries["Open folder"]()
            rename.assert_called_once_with(
                "A second account with a very long name", False)
            delete.assert_called_once_with(
                "A second account with a very long name", False)
            self.assertEqual(popen.call_args.args[0], ["xdg-open", LONG])

    def test_switch_and_new_profile_keep_their_handlers(self):
        with headless_window() as window:
            page = self._open(window)
            with mock.patch.object(window, "_switch_profile_row") as switch:
                self._card(page, "Bot 1").switch.click()
            switch.assert_called_once_with(BOT)
            self.assertFalse(hasattr(self._card(page, "Default"), "switch"))
            self.assertEqual(page.new_button.text(), "New profile")

    def test_every_action_is_a_stop_for_the_controller(self):
        self.running = [_instance()]
        with headless_window() as window:
            page = self._open(window)
            for button in page.findChildren(QPushButton):
                if button.isVisible() and button.isEnabled():
                    self.assertNotEqual(button.focusPolicy(), Qt.NoFocus,
                                        button.text())


if __name__ == "__main__":
    unittest.main()
