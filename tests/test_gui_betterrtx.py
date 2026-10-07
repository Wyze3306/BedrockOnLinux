"""Settings ▸ Tools ▸ BetterRTX (#166)."""
# SPDX-License-Identifier: MIT

import unittest
from unittest import mock

from PySide6.QtWidgets import QPushButton

from bol import gui
from tests.guiharness import headless_window, qt_app


def _preset(uuid, version):
    return {"uuid": uuid, "name": f"Preset {uuid}", "game_version": "26.40",
            "material_version": version, "urls": {}}


class BetterRtxCardTests(unittest.TestCase):
    def setUp(self):
        qt_app()

    def test_the_tools_offer_install_pack_and_restore(self):
        with headless_window() as window:
            texts = {b.text() for b in window.findChildren(QPushButton)}
        self.assertIn("Install a BetterRTX preset…", texts)
        self.assertIn("Install a .rtpack file…", texts)
        self.assertIn("Restore Minecraft's own ray tracing shaders", texts)

    def test_no_preset_for_this_version_is_said_plainly(self):
        with headless_window() as window, \
                mock.patch.object(window, "info_box") as said, \
                mock.patch.object(gui.QInputDialog, "getItem") as asked:
            window._pick_betterrtx(([_preset("default", 25)], 26,
                                    {"installed": None}))
        asked.assert_not_called()
        self.assertIn("No BetterRTX preset is made for this Minecraft",
                      said.call_args.args[1])

    def test_only_presets_for_this_format_are_offered(self):
        presets = [_preset("new", 26), _preset("old", 25),
                   _preset("unknown", None)]
        with headless_window() as window, \
                mock.patch.object(gui.QInputDialog, "getItem",
                                  return_value=("Preset new", True)) as asked, \
                mock.patch.object(window, "_betterrtx_job") as job:
            window._pick_betterrtx((presets, 26, {"installed": None}))
        offered = asked.call_args.args[3]
        self.assertEqual(offered, ["Preset new", "Preset unknown  (older preset)"])
        self.assertEqual(job.call_args.args[0], "betterrtx-install")
        with mock.patch.object(gui.betterrtx, "install_preset") as install:
            job.call_args.args[1]()
        self.assertEqual(install.call_args.args[0]["uuid"], "new")

    def test_cancelling_the_choice_installs_nothing(self):
        with headless_window() as window, \
                mock.patch.object(gui.QInputDialog, "getItem",
                                  return_value=("", False)), \
                mock.patch.object(window, "_betterrtx_job") as job:
            window._pick_betterrtx(([_preset("new", 26)], 26, {}))
        job.assert_not_called()


if __name__ == "__main__":
    unittest.main()
