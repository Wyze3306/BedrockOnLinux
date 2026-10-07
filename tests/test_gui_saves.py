"""What the window says and asks about the player's worlds across versions."""
# SPDX-License-Identifier: MIT

import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtWidgets import QLabel, QMessageBox

from bol import gui
from tests.guiharness import headless_window, qt_app


def _answer(text):
    """Stand in for QMessageBox.exec: click the button reading ``text``."""
    def exec_(box):
        for button in box.buttons():
            if button.text() == text:
                button.click()
                return 0
        raise AssertionError(f"no {text!r} button in {box.text()!r}")
    return exec_


class PreviewQuestionTests(unittest.TestCase):
    """The first Preview launch asks whether it starts from Minecraft's
    worlds, rather than opening an empty game that reads as data lost."""

    PREVIEW = {"edition": {"id": "preview"}, "tag": "1.26.60.20"}

    @classmethod
    def setUpClass(cls):
        qt_app()

    def _settle(self, answer, pending=True, ver=None):
        with headless_window() as window, \
                mock.patch.object(gui.saves, "preview_copy_pending",
                                  return_value=pending), \
                mock.patch.object(gui.saves,
                                  "keep_preview_separate") as separate, \
                mock.patch.object(QMessageBox, "exec", _answer(answer)):
            settled = window._preview_data_settled(ver or self.PREVIEW)
        return settled, separate.called

    def test_copying_goes_on_with_the_launch_and_records_nothing(self):
        # The copy itself is the launch's, where the prefix is known idle.
        self.assertEqual(self._settle("Copy my worlds"), (True, False))

    def test_starting_empty_is_remembered_and_goes_on(self):
        self.assertEqual(self._settle("Start Preview empty"), (True, True))

    def test_closing_the_question_starts_nothing(self):
        self.assertEqual(self._settle("Cancel"), (False, False))

    def test_nothing_is_asked_once_it_is_settled(self):
        with headless_window() as window, \
                mock.patch.object(gui.saves, "preview_copy_pending",
                                  return_value=False), \
                mock.patch.object(QMessageBox, "exec") as shown:
            self.assertTrue(window._preview_data_settled(self.PREVIEW))
        shown.assert_not_called()

    def test_minecraft_itself_is_never_asked_about(self):
        with headless_window() as window, \
                mock.patch.object(gui.saves, "preview_copy_pending",
                                  return_value=True) as pending, \
                mock.patch.object(QMessageBox, "exec") as shown:
            self.assertTrue(window._preview_data_settled(
                {"edition": {"id": "release"}, "tag": "1.26.52.3"}))
        shown.assert_not_called()
        pending.assert_not_called()


class SessionNoticeTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        qt_app()

    def test_the_launch_thread_hands_notices_over_before_it_is_done(self):
        worker = gui.LaunchWorker({"edition": {"id": "release"}, "tag": "1.0"})
        seen = []
        worker.session_notice.connect(lambda text: seen.append(("notice", text)))
        worker.done.connect(lambda result: seen.append(("done", result)))

        def play(on_started=None, notices=None, editor=False):
            notices.append("signed out")
            return 0

        with mock.patch.object(gui, "do_setup"), \
                mock.patch.object(gui, "launch", side_effect=play):
            worker.run()
        self.assertEqual(seen, [("notice", "signed out"), ("done", "closed")])

    def test_a_notice_is_shown_in_a_dialog(self):
        with headless_window() as window, \
                mock.patch.object(window, "info_box") as shown:
            window._show_session_notice("signed out")
        shown.assert_called_once_with("Your worlds are safe", "signed out")

    def test_with_the_window_gone_it_goes_to_the_desktop(self):
        with headless_window() as window, \
                mock.patch.object(gui, "desktop_notify") as notify, \
                mock.patch.object(window, "info_box") as shown:
            window.ui_state["window_gone"] = True
            window._show_session_notice("signed out")
        notify.assert_called_once()
        shown.assert_not_called()


class BackupsSectionTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        qt_app()

    BACKUPS = [
        {"edition": "release", "reason": "version-change",
         "version": "1.26.60.1", "previous": "1.26.52.3",
         "created": 1791100000.5, "size": 50 * 1024 ** 2, "worlds": 3,
         "path": Path("/nonexistent/backups/b2")},
        {"edition": "preview", "reason": "before-preview-copy",
         "created": 1791000000.0, "size": 1024, "worlds": 1,
         "path": Path("/nonexistent/backups/b1")},
    ]

    def _texts(self, window):
        return [label.text() for label in
                window.backups_list.parentWidget().findChildren(QLabel)]

    def test_each_backup_says_which_version_it_was_made_before(self):
        with headless_window() as window, \
                mock.patch.object(gui.saves, "list_backups",
                                  return_value=self.BACKUPS):
            window.refresh_backups()
            texts = self._texts(window)
            summary = window.backups_summary.text()
        self.assertIn("Minecraft, before 1.26.60.1", texts)
        self.assertIn("Minecraft Preview, before the copy from Minecraft",
                      texts)
        self.assertTrue(any("saved by 1.26.52.3" in text
                            and "3 worlds" in text for text in texts))
        self.assertTrue(summary.startswith("2 backups"))

    def test_no_backup_yet_says_when_one_is_made(self):
        with headless_window() as window:
            window.refresh_backups()
            self.assertIn("different version", window.backups_summary.text())

    def test_restoring_asks_first_and_never_while_the_game_runs(self):
        with headless_window() as window, \
                mock.patch.object(gui, "_mc_running", return_value=True), \
                mock.patch.object(window, "warn_box") as warned, \
                mock.patch.object(window, "_run_saved_data_job") as job:
            window._restore_backup(self.BACKUPS[0])
        warned.assert_called_once()
        job.assert_not_called()

    def test_a_confirmed_restore_runs_off_the_window(self):
        with headless_window() as window, \
                mock.patch.object(gui, "_mc_running", return_value=False), \
                mock.patch.object(QMessageBox, "exec", _answer("Restore")), \
                mock.patch.object(window, "_run_saved_data_job") as job:
            window._restore_backup(self.BACKUPS[0])
        self.assertIs(job.call_args.args[1], gui.saves.restore_backup)
        self.assertEqual(job.call_args.args[2], self.BACKUPS[0]["path"])


if __name__ == "__main__":
    unittest.main()
