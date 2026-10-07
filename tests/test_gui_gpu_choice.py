"""Settings ▸ Advanced ▸ Graphics card (#275)."""
# SPDX-License-Identifier: MIT

import unittest
from unittest import mock

from bol import gpus
from tests.guiharness import headless_window, qt_app

_CARDS = [
    gpus.Gpu("0000:00:02.0", "8086", "3e9b", "i915", True,
             "Intel UHD Graphics 630"),
    gpus.Gpu("0000:01:00.0", "10de", "25a2", "nvidia", False,
             "NVIDIA GeForce RTX 3050 Mobile"),
]


class GraphicsCardChoiceTests(unittest.TestCase):
    def setUp(self):
        qt_app()

    def _items(self, combo):
        return [(combo.itemText(i), combo.itemData(i))
                for i in range(combo.count())]

    def test_every_card_is_offered_after_automatic(self):
        with mock.patch.object(gpus, "list_gpus", return_value=_CARDS), \
                headless_window() as window:
            items = self._items(window.gpu_combo)
            self.assertEqual([data for _text, data in items],
                             ["auto", "0000:00:02.0", "0000:01:00.0"])
            self.assertIn("runs the display", items[1][0])
            self.assertEqual(window.gpu_combo.currentData(), "auto")

    def test_picking_a_card_saves_its_address(self):
        with mock.patch.object(gpus, "list_gpus", return_value=_CARDS), \
                headless_window() as window:
            window.gpu_combo.setCurrentIndex(2)
            self.assertEqual(window.settings["gpu"], "0000:01:00.0")

    def test_a_card_that_is_gone_is_shown_as_gone(self):
        with mock.patch.object(gpus, "list_gpus", return_value=_CARDS[:1]), \
                headless_window(gpu="0000:01:00.0") as window:
            self.assertEqual(window.gpu_combo.currentData(), "0000:01:00.0")
            self.assertIn("not found", window.gpu_combo.currentText())


if __name__ == "__main__":
    unittest.main()
