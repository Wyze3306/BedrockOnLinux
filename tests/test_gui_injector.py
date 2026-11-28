"""Settings ▸ Tools ▸ Injector Settings: where an injected DLL's settings are."""
# SPDX-License-Identifier: MIT

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtWidgets import QPushButton

from bol import inject
from tests.guiharness import headless_window, qt_app


class DllSettingsFolderTests(unittest.TestCase):
    def setUp(self):
        qt_app()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.prefix = Path(tmp.name)
        (self.prefix / "drive_c/users/steamuser").mkdir(parents=True)
        self.mods = self.prefix / inject.DLL_SETTINGS_REL
        patch = mock.patch.object(inject, "active_prefix",
                                  return_value=self.prefix)
        patch.start()
        self.addCleanup(patch.stop)

    def _button(self, window):
        return next(b for b in window.findChildren(QPushButton)
                    if b.text() == "Open the DLL's settings folder")

    def test_the_injector_card_opens_the_folder_of_the_chosen_dll(self):
        (self.mods / "ForceCloseOreUI").mkdir(parents=True)
        with headless_window(injector_dll_path="/x/ForceCloseOreUI.dll") \
                as window, \
                mock.patch.object(window, "_open_folder") as opened:
            button = self._button(window)
            self.assertIn(r"%APPDATA%\Minecraft Bedrock\mods",
                          button.toolTip())
            button.click()
        opened.assert_called_once_with(self.mods / "ForceCloseOreUI")

    def test_a_downloaded_dll_is_found_by_the_name_in_its_url(self):
        (self.mods / "ForceCloseOreUI").mkdir(parents=True)
        with headless_window(
                injector_dll_type="url",
                injector_dll_url="https://example.com/ForceCloseOreUI.dll",
                injector_dll_path="/x/Other.dll") as window, \
                mock.patch.object(window, "_open_folder") as opened:
            self._button(window).click()
        opened.assert_called_once_with(self.mods / "ForceCloseOreUI")

    def test_without_a_dll_it_makes_and_opens_the_shared_folder(self):
        with headless_window() as window, \
                mock.patch.object(window, "_open_folder") as opened:
            self._button(window).click()
        opened.assert_called_once_with(self.mods)
        self.assertTrue(self.mods.is_dir())


if __name__ == "__main__":
    unittest.main()
