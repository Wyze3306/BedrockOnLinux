"""Bedrock Editor from the launcher window (#286).

The Editor is the game itself, started with the protocol URI a Windows
shortcut to "minecraft:?Editor=true" hands it, so the window offers it as
PLAY with one flag set rather than as a separate install.
"""
# SPDX-License-Identifier: MIT

import unittest
from unittest import mock

from PySide6.QtWidgets import QPushButton

from bol import gui
from tests.guiharness import headless_window, qt_app


def _editor_button(window):
    for button in window.findChildren(QPushButton):
        if button.text() == "Open Bedrock Editor":
            return button
    return None


class EditorButtonTests(unittest.TestCase):
    def setUp(self):
        qt_app()

    def test_tools_offer_the_editor(self):
        with headless_window() as window:
            self.assertIsNotNone(_editor_button(window))

    def test_the_button_plays_with_the_editor_flag(self):
        with headless_window() as window:
            window.toggle_settings()
            with mock.patch.object(window, "do_play") as play:
                _editor_button(window).click()
            play.assert_called_once_with(editor=True)
            # Back on the hero page, where the launch shows its progress.
            self.assertIs(window.stack.currentWidget(), window.hero_page)

    def test_the_launch_thread_carries_the_flag(self):
        worker = gui.LaunchWorker({"edition": "release", "tag": "1"},
                                  editor=True)
        with mock.patch.object(gui, "do_setup"), \
                mock.patch.object(gui, "single_window_session",
                                  return_value=False), \
                mock.patch.object(gui, "load_settings", return_value={}), \
                mock.patch.object(gui, "launch") as launch:
            worker.run()
        self.assertTrue(launch.call_args.kwargs["editor"])

    def test_play_stays_a_plain_launch(self):
        # PLAY is connected to clicked(bool); the flag must never be the
        # button's checked state.
        with headless_window() as window:
            with mock.patch.object(gui, "LaunchWorker") as worker, \
                    mock.patch.object(window, "selected_version",
                                      return_value={"edition": {"id": "release"},
                                                    "tag": "1"}), \
                    mock.patch.object(window, "_online_sign_in_settled",
                                      return_value=True), \
                    mock.patch.object(window, "_preview_data_settled",
                                      return_value=True), \
                    mock.patch.object(window, "_start_worker",
                                      return_value=True):
                window.play_btn.click()
            self.assertFalse(worker.call_args.kwargs["editor"])


if __name__ == "__main__":
    unittest.main()
