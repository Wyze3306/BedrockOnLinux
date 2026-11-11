"""Scrolling Settings never changes a setting on the way past (#363).

The Settings tabs scroll, and a QComboBox or QSpinBox takes a wheel turn as a
new value whenever the pointer happens to be over it, with or without focus.
The graphics card choice is saved on every change, so scrolling the Advanced
tab could move Minecraft from the NVIDIA card to the processor's AMD iGPU
without anyone noticing, until the game ran at single-digit frame rates.
"""
# SPDX-License-Identifier: MIT

import unittest
from unittest import mock

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication

from bol import gpus
from tests.guiharness import headless_window, qt_app

_CARDS = [
    gpus.Gpu(slot="0000:01:00.0", vendor="10de", device="2504",
             driver="nvidia", boot_vga=True, name="NVIDIA GeForce RTX 3060"),
    gpus.Gpu(slot="0000:0e:00.0", vendor="1002", device="13c0",
             driver="amdgpu", boot_vga=False, name="AMD Radeon Graphics"),
]


def _wheel(widget, notches=-1):
    """One turn of the wheel over the middle of ``widget``, as Qt delivers it."""
    centre = QPointF(widget.rect().center())
    event = QWheelEvent(
        centre, QPointF(widget.mapToGlobal(centre.toPoint())), QPoint(),
        QPoint(0, 120 * notches), Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(widget, event)
    return event


class WheelOverSettingsTests(unittest.TestCase):
    def setUp(self):
        qt_app()

    def _window(self, **settings):
        patch = mock.patch.object(gpus, "list_gpus", return_value=list(_CARDS))
        patch.start()
        self.addCleanup(patch.stop)
        return headless_window(**settings)

    def test_scrolling_past_the_graphics_card_keeps_it(self):
        saved = []
        with self._window(gpu="0000:01:00.0") as window, \
                mock.patch.object(window, "_save_setting",
                                  lambda k, v: saved.append((k, v))):
            combo = window.gpu_combo
            before = combo.currentData()
            event = _wheel(combo)
            self.assertEqual(combo.currentData(), before)
            self.assertEqual(saved, [])
            # Left for the page underneath to scroll.
            self.assertFalse(event.isAccepted())

    def test_a_focused_combo_still_takes_the_wheel(self):
        with self._window(gpu="0000:01:00.0") as window:
            combo = window.gpu_combo
            combo.setFocus(Qt.FocusReason.TabFocusReason)
            with mock.patch.object(combo, "hasFocus", return_value=True):
                _wheel(combo)
            self.assertEqual(combo.currentData(), "0000:0e:00.0")

    def test_scrolling_past_the_other_settings_keeps_them(self):
        with self._window(injector_delay=5) as window:
            spin_before = window.delay_spin.value()
            mode_before = window.dll_mode_combo.currentIndex()
            _wheel(window.delay_spin, notches=1)
            _wheel(window.dll_mode_combo)
            self.assertEqual(window.delay_spin.value(), spin_before)
            self.assertEqual(window.dll_mode_combo.currentIndex(), mode_before)

    def test_the_wheel_never_gives_a_setting_focus(self):
        with self._window() as window:
            for widget in (window.gpu_combo, window.dll_mode_combo,
                           window.delay_spin):
                self.assertEqual(widget.focusPolicy(),
                                 Qt.FocusPolicy.StrongFocus, widget)


if __name__ == "__main__":
    unittest.main()
