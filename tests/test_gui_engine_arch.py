"""Settings ▸ Advanced: the ARM engine switch (#250)."""
# SPDX-License-Identifier: MIT

import unittest
from unittest import mock

from tests.guiharness import headless_window, qt_app

from bol import engine_arch, gui


class EngineSwitchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt_app()

    def test_hidden_where_it_is_not_offered(self):
        with mock.patch.object(engine_arch, "offered", return_value=False), \
                headless_window() as window:
            self.assertIsNone(getattr(window, "engine_arch_row", None))

    def test_switch_saves_the_choice(self):
        with mock.patch.object(engine_arch, "offered", return_value=True), \
                mock.patch.object(engine_arch, "problem", return_value=None), \
                headless_window() as window, \
                mock.patch.object(gui, "save_settings") as save:
            row = window.engine_arch_row
            self.assertFalse(row.isChecked())
            self.assertTrue(row.isEnabled())
            row.switch.setChecked(True)
            self.assertEqual(save.call_args.args[0][engine_arch.SETTING],
                             engine_arch.ARM64EC)
            row.switch.setChecked(False)
            self.assertEqual(save.call_args.args[0][engine_arch.SETTING],
                             engine_arch.X86_64)

    def test_cannot_be_turned_on_where_it_cannot_run(self):
        with mock.patch.object(engine_arch, "offered", return_value=True), \
                mock.patch.object(engine_arch, "problem",
                                  return_value="needs glibc 2.41"), \
                headless_window() as window:
            self.assertFalse(window.engine_arch_row.isEnabled())

    def test_one_already_on_can_still_be_turned_off(self):
        with mock.patch.object(engine_arch, "offered", return_value=True), \
                mock.patch.object(engine_arch, "problem",
                                  return_value="needs glibc 2.41"), \
                headless_window(engine_arch="arm64ec") as window:
            self.assertTrue(window.engine_arch_row.isChecked())
            self.assertTrue(window.engine_arch_row.isEnabled())


if __name__ == "__main__":
    unittest.main()
