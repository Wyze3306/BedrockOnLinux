"""The launcher window in Steam Game Mode, where Gamescope shows one window.

Gamescope scales that window to the whole screen and no window manager lets
the player resize it, so the launcher opens at the screen's own size there:
a Steam Deck's 1280x800 drawn one to one, not 1120x700 stretched to it.
"""
# SPDX-License-Identifier: MIT

import types
import unittest
from unittest import mock

from tests.guiharness import qt_app  # noqa: F401  (sets the offscreen platform)
from bol import gui


class _Size:
    def __init__(self, width, height):
        self._width, self._height = width, height

    def width(self):
        return self._width

    def height(self):
        return self._height


def _window(screen_size, minimum=(1000, 640)):
    resized = []
    if screen_size is None:
        screen = None
    else:
        geometry = types.SimpleNamespace(size=lambda: _Size(*screen_size))
        screen = types.SimpleNamespace(availableGeometry=lambda: geometry)
    window = types.SimpleNamespace(
        screen=lambda: screen,
        minimumSize=lambda: _Size(*minimum),
        resize=lambda size: resized.append((size.width(), size.height())))
    return window, resized


class FillSingleWindowScreenTests(unittest.TestCase):
    def _fill(self, single_window, screen_size):
        window, resized = _window(screen_size)
        with mock.patch.object(gui, "single_window_session",
                               return_value=single_window):
            gui.MainWindow._fill_single_window_screen(window)
        return resized

    def test_game_mode_opens_at_the_steam_deck_screen_size(self):
        self.assertEqual(self._fill(True, (1280, 800)), [(1280, 800)])

    def test_a_desktop_keeps_the_usual_size(self):
        self.assertEqual(self._fill(False, (1920, 1080)), [])

    def test_a_screen_below_the_minimum_size_is_left_alone(self):
        self.assertEqual(self._fill(True, (800, 600)), [])

    def test_no_screen_is_left_alone(self):
        self.assertEqual(self._fill(True, None), [])


if __name__ == "__main__":
    unittest.main()
