"""Popups follow the launcher's theme, not the desktop's (#332).

A combo box's list, a context menu and a tooltip are top-level windows. The
theme's ``QWidget { background: transparent }`` left them with no background
of their own, so on a dark desktop with the light theme on, the drop-down
lists showed dark text on the desktop's dark colour.

Each popup is rendered over magenta: a pixel of it left in the result is a
pixel the popup did not paint.
"""
# SPDX-License-Identifier: MIT

import unittest
from collections import Counter
from unittest import mock

from PySide6.QtCore import QPoint
from PySide6.QtGui import QColor, QImage, QPalette
from PySide6.QtWidgets import QApplication, QMenu, QToolTip

from bol import gpus
from tests.guiharness import headless_window, qt_app

_UNPAINTED = QColor(255, 0, 255)
_CARDS = [
    gpus.Gpu("0000:00:02.0", "8086", "3e9b", "i915", True,
             "Intel UHD Graphics 630"),
    gpus.Gpu("0000:01:00.0", "10de", "25a2", "nvidia", False,
             "NVIDIA GeForce RTX 3050 Mobile"),
]


def _colours(widget):
    image = QImage(widget.size(), QImage.Format.Format_ARGB32)
    image.fill(_UNPAINTED)
    widget.render(image)
    return Counter(image.pixelColor(x, y).name()
                   for x in range(image.width())
                   for y in range(image.height()))


def _desktop_palette(colour):
    palette = QPalette()
    for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base,
                 QPalette.ColorRole.Button, QPalette.ColorRole.ToolTipBase):
        palette.setColor(role, QColor(colour))
    return palette


class PopupThemeTests(unittest.TestCase):
    def setUp(self):
        self.app = qt_app()
        self.saved_palette = self.app.palette()
        self.saved_sheet = self.app.styleSheet()
        self.saved_style = self.app.style().name()
        self.app.setStyle("Fusion")

    def tearDown(self):
        QToolTip.hideText()
        self.app.setStyleSheet(self.saved_sheet)
        self.app.setStyle(self.saved_style)
        self.app.setPalette(self.saved_palette)

    def _themed_window(self, light, desktop):
        self.app.setPalette(_desktop_palette(desktop))
        window = self.enterContext(
            headless_window(light_theme=light))
        window.apply_theme()
        return window

    def assertPaintedWith(self, widget, colour):
        colours = _colours(widget)
        self.assertNotIn(_UNPAINTED.name(), colours)
        self.assertEqual(colours.most_common(1)[0][0], colour)

    def _check_combo_list(self, light, desktop):
        with mock.patch.object(gpus, "list_gpus", return_value=_CARDS):
            window = self._themed_window(light, desktop)
        combo = window.gpu_combo
        combo.showPopup()
        try:
            QApplication.processEvents()
            self.assertPaintedWith(combo.view().window(), window.theme.card)
        finally:
            combo.hidePopup()

    def test_light_theme_drop_down_on_a_dark_desktop(self):
        self._check_combo_list(light=True, desktop="#202020")

    def test_dark_theme_drop_down_on_a_light_desktop(self):
        self._check_combo_list(light=False, desktop="#f4f4f4")

    def test_context_menu_follows_the_theme(self):
        window = self._themed_window(light=True, desktop="#202020")
        menu = QMenu(window)
        menu.addAction("Copy")
        menu.addAction("Paste")
        menu.popup(QPoint(0, 0))
        try:
            QApplication.processEvents()
            self.assertPaintedWith(menu, window.theme.card)
        finally:
            menu.close()
            menu.deleteLater()

    def test_tooltip_follows_the_theme(self):
        window = self._themed_window(light=True, desktop="#202020")
        QToolTip.showText(QPoint(10, 10), "Which graphics card Minecraft "
                          "renders on.", window)
        QApplication.processEvents()
        tips = [widget for widget in QApplication.topLevelWidgets()
                if widget.metaObject().className() == "QTipLabel"
                and widget.isVisible()]
        self.assertTrue(tips)
        self.assertPaintedWith(tips[0], window.theme.card)


if __name__ == "__main__":
    unittest.main()
