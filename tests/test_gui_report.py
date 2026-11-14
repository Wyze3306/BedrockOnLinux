"""The "Report a problem" window: what it shows, what it sends, and that it
sends nothing by itself. Collection and both ways out are replaced by fakes;
nothing here opens a connection or a browser."""
# SPDX-License-Identifier: MIT

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from bol import gui, gui_report, report  # noqa: E402
from tests.guiharness import headless_window, qt_app  # noqa: E402
from tests.test_scrub import KNOWN, TRAPPED  # noqa: E402

pytestmark = pytest.mark.gui

ISSUE = "https://github.com/Wyze3306/BedrockOnLinux/issues/401"


def pump(until, timeout=5.0):
    """Run the event loop until `until()` is true; whether it became so."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if until():
            return True
        time.sleep(0.005)
    return False


class DialogCase(unittest.TestCase):
    relay = "off"

    def setUp(self):
        qt_app()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        logs = Path(self.directory.name)
        (logs / "minecraft.log").write_text(TRAPPED, encoding="utf-8")
        (logs / "proton.log").write_text(TRAPPED, encoding="utf-8")
        self.sent = []
        self.browsed = []
        self.collected_with = []
        environ = mock.patch.dict("os.environ",
                                  {report.RELAY_ENV: self.relay})
        environ.start()
        self.addCleanup(environ.stop)

        def collect(activity_log=None):
            self.collected_with.append(activity_log)
            return report.collect(activity_log=activity_log, settings={},
                                  logs_dir=logs, identity=list(KNOWN))

        def send(sending, url):
            self.sent.append((sending.payload(), url))
            return ISSUE

        def browse(sending):
            self.browsed.append(sending.full_text())
            return (logs / "bedrock-on-linux-report-x.txt",
                    "https://github.com/Wyze3306/BedrockOnLinux/issues/new",
                    True)

        self.fakes = {"collect": collect, "send": send,
                      "open_in_browser": browse}

    def dialog(self, **overrides):
        options = dict(self.fakes, activity_log="xx Game closed (exit 139)")
        options.update(overrides)
        dialog = gui_report.ReportDialog(**options)
        self.addCleanup(self._dispose, dialog)
        return dialog

    @staticmethod
    def _dispose(dialog):
        dialog.done(0)
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    def ready(self, **overrides):
        dialog = self.dialog(**overrides)
        self.assertTrue(pump(lambda: dialog.report is not None))
        return dialog


class PreviewTests(DialogCase):
    def test_every_piece_is_listed_with_its_exact_text(self):
        dialog = self.ready()
        self.assertEqual(self.collected_with, ["xx Game closed (exit 139)"])
        for key in ("message", "launcher", "system", "graphics", "settings",
                    "activity", "log:minecraft.log", "log:proton.log"):
            self.assertIn(key, dialog.rows)
        row = dialog.rows["log:minecraft.log"]
        self.assertTrue(row.view.isHidden())
        row.toggle.setChecked(True)
        self.assertFalse(row.view.isHidden())
        self.assertEqual(row.view.toPlainText(),
                         dialog.report.piece("log:minecraft.log").text)
        self.assertNotIn("marguerite", row.view.toPlainText().lower())
        self.assertIn("Already removed", dialog.removed.text())

    def test_the_players_own_words_are_shown_as_they_will_be_sent(self):
        dialog = self.ready()
        dialog.rows["message"].toggle.setChecked(True)
        dialog.title_edit.setText("Crash for marguerite.duval@courriel.example")
        dialog.comment_edit.setPlainText("It happens in La Ferme de Mamie.")
        shown = dialog.rows["message"].view.toPlainText()
        self.assertIn("Title: Crash for <email>", shown)
        self.assertIn("It happens in <world>.", shown)
        self.assertFalse(dialog.rows["message"].check.isEnabled())

    def test_the_destination_is_named_before_anything_is_sent(self):
        dialog = self.ready()
        self.assertIn("Nothing is sent by the launcher", dialog.where.text())
        self.assertIn("github.com/Wyze3306/BedrockOnLinux", dialog.where.text())
        self.assertEqual(dialog.send_btn.text(), "Send with my browser")
        self.assertTrue(dialog.browser_btn.isHidden())

    def test_send_waits_for_a_title_and_for_the_collection(self):
        release = threading.Event()

        def slow(activity_log=None):
            release.wait(5)
            return self.fakes["collect"](activity_log)

        dialog = self.dialog(collect=slow)
        dialog.title_edit.setText("A real title")
        self.assertFalse(dialog.send_btn.isEnabled())
        release.set()
        self.assertTrue(pump(lambda: dialog.send_btn.isEnabled()))
        dialog.title_edit.setText("ab")
        self.assertFalse(dialog.send_btn.isEnabled())

    def test_opening_the_window_sends_nothing(self):
        dialog = self.ready()
        dialog.title_edit.setText("A real title")
        dialog.rows["system"].toggle.setChecked(True)
        pump(lambda: False, timeout=0.2)
        self.assertEqual((self.sent, self.browsed), ([], []))

    def test_a_failed_collection_still_lets_the_player_write(self):
        def broken(activity_log=None):
            raise RuntimeError("no sysfs")

        dialog = self.ready(collect=broken)
        self.assertIn("could not be collected", dialog.status.text())
        dialog.title_edit.setText("Still a report")
        self.assertTrue(dialog.send_btn.isEnabled())

    def test_copy_puts_the_whole_report_on_the_clipboard(self):
        dialog = self.ready()
        dialog.title_edit.setText("A real title")
        dialog.copy_btn.click()
        copied = QApplication.clipboard().text()
        self.assertIn("Title: A real title", copied)
        self.assertIn("Game log (minecraft.log)", copied)
        self.assertEqual((self.sent, self.browsed), ([], []))


class BrowserRouteTests(DialogCase):
    def test_send_saves_copies_and_explains(self):
        dialog = self.ready()
        dialog.title_edit.setText("FPS drops when breaking blocks")
        dialog.rows["log:proton.log"].check.setChecked(False)
        dialog.send_btn.click()
        self.assertEqual(len(self.browsed), 1)
        self.assertEqual(self.sent, [])
        self.assertIn("Game log (minecraft.log)", self.browsed[0])
        self.assertNotIn("Engine log (proton.log)", self.browsed[0])
        self.assertEqual(dialog.pages.currentIndex(), 1)
        self.assertEqual(dialog.done_title.text(), "Finish in your browser")
        self.assertIn("bedrock-on-linux-report-x.txt", dialog.done_text.text())
        self.assertIn("Nothing has been sent by the launcher",
                      dialog.done_text.text())
        self.assertEqual(QApplication.clipboard().text(), self.browsed[0])
        self.assertFalse(dialog.done_folder.isHidden())


class RelayRouteTests(DialogCase):
    relay = "https://reports.example/v1/report"

    def test_the_relay_host_is_named_and_the_browser_stays_on_offer(self):
        dialog = self.ready()
        self.assertIn("Sent to reports.example", dialog.where.text())
        self.assertEqual(dialog.send_btn.text(), "Send")
        self.assertFalse(dialog.browser_btn.isHidden())

    def test_send_files_the_report_and_shows_the_link(self):
        dialog = self.ready()
        dialog.title_edit.setText("FPS drops when breaking blocks")
        dialog.comment_edit.setPlainText("Every time, since yesterday.")
        dialog.rows["settings"].check.setChecked(False)
        dialog.send_btn.click()
        self.assertTrue(pump(lambda: dialog.pages.currentIndex() == 1))
        (payload, url), = self.sent
        self.assertEqual(url, self.relay)
        self.assertEqual(payload["title"], "FPS drops when breaking blocks")
        self.assertEqual(payload["comment"], "Every time, since yesterday.")
        self.assertNotIn("Launcher settings", payload["summary"])
        self.assertIn("== System ==", payload["summary"])
        self.assertEqual(dialog.done_title.text(), "Report filed")
        self.assertIn(ISSUE, dialog.done_text.text())
        dialog.done_copy.click()
        self.assertEqual(QApplication.clipboard().text(), ISSUE)
        self.assertTrue(dialog.done_folder.isHidden())

    def test_a_refusal_is_shown_and_the_browser_offered(self):
        def refused(_report, _url):
            raise report.ReportError("Too many reports were sent.")

        dialog = self.ready(send=refused)
        dialog.title_edit.setText("FPS drops when breaking blocks")
        dialog.send_btn.click()
        self.assertTrue(pump(lambda: "Too many" in dialog.status.text()))
        self.assertEqual(dialog.pages.currentIndex(), 0)
        self.assertIn("Use my browser", dialog.status.text())
        self.assertTrue(dialog.send_btn.isEnabled())
        dialog.browser_btn.click()
        self.assertEqual(len(self.browsed), 1)

    def test_closing_waits_for_the_thread_it_started(self):
        """A QThread that outlives its window aborts the process."""
        started, release = threading.Event(), threading.Event()

        def slow(_report, _url):
            started.set()
            release.wait(5)
            return ISSUE

        dialog = self.ready(send=slow)
        dialog.title_edit.setText("FPS drops when breaking blocks")
        dialog.send_btn.click()
        self.assertTrue(started.wait(5))
        workers = list(dialog._workers)
        threading.Timer(0.2, release.set).start()
        dialog.done(0)
        self.assertTrue(all(worker.isFinished() for worker in workers))
        self.assertEqual(dialog._workers, [])
        self.assertEqual(dialog.pages.currentIndex(), 0)


class LauncherWindowTests(unittest.TestCase):
    def setUp(self):
        qt_app()
        real = report.collect
        fake = mock.patch.object(
            report, "collect", lambda activity_log=None: real(
                activity_log=activity_log, settings={},
                logs_dir="/nonexistent", identity=list(KNOWN)))
        fake.start()
        self.addCleanup(fake.stop)

    def test_the_window_opens_once_with_the_activity_log(self):
        with headless_window() as window:
            window.log_view.setPlainText("xx Game closed (exit 139)")
            dialog = window.open_report()
            self.assertIs(window.open_report(), dialog)
            self.assertTrue(pump(lambda: dialog.report is not None))
            self.assertEqual(dialog.report.piece("activity").text,
                             "xx Game closed (exit 139)")

    def test_closing_the_launcher_closes_it_first(self):
        with headless_window() as window:
            dialog = window.open_report()
            workers = list(dialog._workers)
        self.assertTrue(all(worker.isFinished() for worker in workers))
        self.assertEqual(dialog._workers, [])

    def test_tools_and_the_menu_both_lead_to_it(self):
        with headless_window() as window, \
                mock.patch.object(window, "open_report") as opened:
            from PySide6.QtWidgets import QPushButton
            labels = [button for button in window.findChildren(QPushButton)
                      if button.text() in ("Report", "Report a problem…")]
            self.assertEqual(sorted(b.text() for b in labels),
                             ["Report", "Report a problem…"])
            for button in labels:
                button.click()
            self.assertEqual(opened.call_count, 2)

    def test_a_failed_launch_offers_the_report(self):
        with headless_window() as window, \
                mock.patch.object(gui, "gpu_crash_acknowledgement_status",
                                  return_value=None), \
                mock.patch.object(window, "error_box") as box:
            window._play_failed("Game closed (exit 139)")
        box.assert_called_once_with("Minecraft could not start",
                                    "Game closed (exit 139)", report=True)

    def test_the_error_box_button_opens_it_with_the_error(self):
        def click_report(box):
            report_button = next(b for b in box.buttons()
                                 if b.text() == "Report a problem…")
            box.clickedButton = lambda: report_button
            return 0

        with headless_window() as window, \
                mock.patch.object(QMessageBox, "exec", click_report), \
                mock.patch.object(window, "open_report") as opened:
            window.error_box("Minecraft could not start", "exit 139",
                             report=True)
            opened.assert_called_once()
            self.assertEqual(opened.call_args.kwargs["title"],
                             "Minecraft could not start")
            self.assertIn("exit 139", opened.call_args.kwargs["comment"])
            # An ordinary error has no such button.
            opened.reset_mock()
            with mock.patch.object(QMessageBox, "exec",
                                   lambda box: len(box.buttons())) as _:
                window.error_box("Other", "message")
            opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
