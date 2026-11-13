"""The first-run network notice and the Network tab of Settings."""
# SPDX-License-Identifier: MIT

import unittest
from unittest import mock

from PySide6.QtWidgets import QLabel, QPushButton

from bol import gui, netpolicy
from tests.guiharness import headless_window, qt_app

_REAL_CHECK = gui.MainWindow.check_for_update_async


def _texts(widget, kind):
    return [w.text() for w in widget.findChildren(kind)]


class _Base(unittest.TestCase):
    def setUp(self):
        self.app = qt_app()
        patch = mock.patch.dict("os.environ", {netpolicy.ENV: ""})
        patch.start()
        self.addCleanup(patch.stop)

    def window(self, **settings):
        context = headless_window(**settings)
        window = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.app.processEvents()
        return window


class FirstRunTests(_Base):
    def test_nothing_is_fetched_before_the_notice_is_answered(self):
        with mock.patch.object(gui.MainWindow, "_start_network") as start:
            window = self.window(network_updates=None)
            start.assert_not_called()
        bar = window._network_notice
        self.assertTrue(gui._alive(bar))
        said = " ".join(_texts(bar, QLabel))
        self.assertIn("Nothing has been contacted yet", said)
        self.assertIn("No telemetry", said)
        self.assertEqual(
            _texts(bar, QPushButton),
            ["What is contacted…", "Off", "Only when I ask", "Automatic"])

    def test_the_notice_is_a_strip_and_play_stays_usable(self):
        window = self.window(network_updates=None)
        self.assertTrue(window.play_btn.isEnabled())
        self.assertIsNone(self.app.activeModalWidget())

    def test_an_answer_is_saved_and_only_then_does_the_window_go_online(self):
        window = self.window(network_updates=None)
        self.assertFalse(window._network_started)
        with mock.patch.object(window, "_save_setting",
                               wraps=window._save_setting) as save:
            window._choose_network_mode("auto")
        save.assert_called_once_with("network_updates", "auto")
        self.assertTrue(window._network_started)
        self.assertIsNone(window._network_notice)

    def test_refusing_still_loads_what_the_player_asked_to_see(self):
        """Off stops update checks, not the version list of the picker."""
        window = self.window(network_updates=None)
        window._choose_network_mode("off")
        self.assertTrue(window._network_started)
        self.assertEqual(window.settings["network_updates"], "off")

    def test_a_decided_player_never_sees_the_notice_again(self):
        for mode in netpolicy.MODES:
            window = self.window(network_updates=mode)
            self.assertFalse(gui._alive(
                getattr(window, "_network_notice", None)))
            self.assertTrue(window._network_started)

    def test_a_bootstrap_download_is_owned_up_to(self):
        with mock.patch.dict("os.environ", {"BOL_BOOTSTRAP_FETCHED": "1"}):
            window = self.window(network_updates=None)
        said = " ".join(_texts(window._network_notice, QLabel))
        self.assertIn("files.pythonhosted.org", said)
        self.assertNotIn("Nothing has been contacted", said)


class UpdateCheckTests(_Base):
    def _check(self, mode, manual=False):
        window = self.window(network_updates=mode)
        with mock.patch.object(gui.feed, "refresh",
                               return_value=("current", "")) as refresh, \
                mock.patch.object(gui.feed, "refresh_if_due",
                                  return_value=("skipped", "")) as due, \
                mock.patch.object(gui, "check_for_update",
                                  return_value=None) as check:
            _REAL_CHECK(window, manual=manual)
            worker = window._workers.get("update-check")
            if worker is not None:
                worker.wait(5000)
            self.app.processEvents()
        return window, refresh, due, check

    def test_off_checks_nothing(self):
        window, refresh, due, check = self._check("off")
        for call in (refresh, due, check):
            call.assert_not_called()
        self.assertIsNone(window._workers.get("update-check"))

    def test_ask_offers_a_button_instead_of_checking(self):
        window, refresh, due, check = self._check("ask")
        for call in (refresh, due, check):
            call.assert_not_called()
        self.assertEqual(_texts(window._ask_bar, QPushButton),
                         ["Not now", "Check now"])

    def test_auto_checks_in_the_background(self):
        _window, refresh, due, check = self._check("auto")
        due.assert_called_once()
        check.assert_called_once()
        refresh.assert_not_called()

    def test_check_now_works_whatever_the_setting(self):
        for mode in ("off", "ask"):
            window, refresh, _due, check = self._check(mode, manual=True)
            refresh.assert_called_once()
            check.assert_called_once()
            self.assertIn("up to date", window.status_label.text())


class NetworkTabTests(_Base):
    def test_the_tab_lists_every_host_and_the_choice(self):
        window = self.window(network_updates="ask")
        tab = window.settings_tabs.widget(window._network_tab)
        listing = " ".join(_texts(tab, QLabel))
        for endpoint in netpolicy.ENDPOINTS:
            for host in endpoint.hosts:
                self.assertIn(host, listing)
        self.assertEqual(window.network_mode_combo.currentData(), "ask")
        self.assertEqual(window.update_channel_combo.currentData(), "stable")

    def test_an_undecided_player_sees_no_choice_preselected(self):
        window = self.window(network_updates=None)
        self.assertEqual(window.network_mode_combo.currentIndex(), -1)
        window._choose_network_mode("ask")
        self.assertEqual(window.network_mode_combo.currentData(), "ask")

    def test_incident_notices_show_once_and_can_be_dismissed(self):
        window = self.window(network_updates="auto")
        notice = {"id": "down", "level": "critical",
                  "title": "Xbox sign-in is down"}
        with mock.patch.object(gui.feed, "notices", return_value=[notice]):
            window._show_notices()
            window._show_notices()
        bars = [label for label in window.findChildren(QLabel)
                if label.text() == "Xbox sign-in is down"]
        self.assertEqual(len(bars), 1)
        with mock.patch.object(gui, "save_settings") as save:
            window._dismiss_notice(bars[0].parentWidget(), "down")
        self.assertEqual(save.call_args.args[0]["dismissed_notices"],
                         ["down"])


if __name__ == "__main__":
    unittest.main()
