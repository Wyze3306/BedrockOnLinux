"""What the controller can actually reach in the launcher window.

The ring is discovered rather than declared: it walks the widget tree of
whatever is on top. That makes these tests worth having against a real
window rather than a mock one — what breaks it is a real widget that is not
shaped the way the walk assumes, which no fake would reproduce.
"""
# SPDX-License-Identifier: MIT

import unittest
from unittest import mock

from PySide6.QtWidgets import (QApplication, QMessageBox, QPushButton,
                               QScrollBar)
from PySide6.QtCore import QPoint, QTimer
from PySide6.QtGui import QColor

from bol import gui
from bol.navigation import choose_neighbour, reading_order, within
from tests.guiharness import headless_window, qt_app


class FakeReader:
    """A controller that reports whatever the test tells it to."""

    def __init__(self, on_action, on_devices=None):
        self.on_action = on_action
        self.on_devices = on_devices
        self.device_names = ("Test Pad",)
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True
        if self.on_devices:
            self.on_devices(self.device_names)
        return True

    def stop(self):
        self.stopped = True


class DeadReader(FakeReader):
    """A machine with no input layer to read."""

    def start(self):
        return False


# ====================================================================
# Geometry — the part that was never about a toolkit
# ====================================================================

class GeometryTests(unittest.TestCase):
    def test_within_needs_an_overlap_on_both_axes(self):
        view = (0, 0, 100, 100)
        self.assertTrue(within((10, 10, 20, 20), view))
        self.assertFalse(within((10, 200, 20, 20), view))
        self.assertTrue(within((10, 130, 20, 20), view, margin=48))
        self.assertFalse(within((10, 200, 20, 20), view, margin=48))

    def test_reading_order_is_down_then_across(self):
        items = ["a", "b", "c"]
        rects = [(500, 10, 10, 10), (10, 10, 10, 10), (10, 5, 10, 10)]
        self.assertEqual(reading_order(items, rects), ["c", "b", "a"])


class NeighbourTests(unittest.TestCase):
    # PLAY, the gear and Details, the way they sit along the dock.
    ROW = [(843, 579, 120, 52), (785, 579, 52, 52), (703, 579, 76, 52)]

    def test_the_nearest_control_in_that_direction_wins(self):
        self.assertEqual(choose_neighbour(self.ROW, 0, "left"), 1)
        self.assertEqual(choose_neighbour(self.ROW, 1, "left"), 2)
        self.assertEqual(choose_neighbour(self.ROW, 2, "right"), 1)

    def test_an_aligned_control_beats_a_closer_but_offset_one(self):
        rects = [
            (100, 100, 80, 30),    # current
            (400, 108, 80, 30),    # same row, further away
            (260, 260, 80, 30),    # nearer as the crow flies, off the row
        ]
        self.assertEqual(choose_neighbour(rects, 0, "right"), 1)

    def test_nothing_ahead_wraps_to_the_far_side(self):
        self.assertEqual(choose_neighbour(self.ROW, 2, "left"), 0)
        self.assertEqual(choose_neighbour(self.ROW, 0, "right"), 2)

    def test_wrapping_can_be_turned_off(self):
        self.assertIsNone(choose_neighbour(self.ROW, 2, "left", wrap=False))
        self.assertEqual(choose_neighbour(self.ROW, 0, "left", wrap=False), 1)

    def test_vertical_movement_prefers_the_column(self):
        rects = [
            (900, 150, 80, 30),    # current, top right
            (890, 220, 80, 30),    # straight below
            (120, 200, 80, 30),    # closer vertically, far to the left
        ]
        self.assertEqual(choose_neighbour(rects, 0, "down"), 1)

    def test_an_empty_screen_has_no_neighbour(self):
        self.assertIsNone(choose_neighbour([], None, "down"))
        self.assertEqual(choose_neighbour(self.ROW, None, "down"), 0)


# ====================================================================
# The ring over the real window
# ====================================================================

class RingTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt_app()

    def window(self, **settings):
        """A shown window with a controller already reporting."""
        settings.setdefault("controller_nav", True)
        patch = mock.patch("bol.navigation.GamepadReader", FakeReader)
        patch.start()
        self.addCleanup(patch.stop)
        context = headless_window(**settings)
        window = context.__enter__()
        self.addCleanup(lambda: context.__exit__(None, None, None))
        window.resize(1000, 660)
        window.show()
        self.app.processEvents()
        return window

    def press(self, window, *actions):
        for action in actions:
            window.nav.dispatch(action)
            self.app.processEvents()
        return window.nav._current


class DiscoveryTests(RingTestCase):
    def test_the_dock_controls_are_all_reachable(self):
        window = self.window()
        items, _rects = window.nav._items()
        self.assertIn(window.play_btn, items)
        self.assertIn(window.rail_buttons["settings"], items)
        self.assertIn(window.details_btn, items)
        self.assertIn(window.stable_btn, items)
        self.assertIn(window.preview_btn, items)

    def test_a_clickable_frame_is_a_control_too(self):
        # The version pill, the profile chip and the account chip are frames
        # with a click handler; a pointing-hand cursor is what tells a user
        # they are controls, and it is what tells the ring.
        window = self.window()
        items, _rects = window.nav._items()
        self.assertIn(window.ver_field, items)
        self.assertIn(window.prof_card, items)
        self.assertIn(window.acct_card, items)

    def test_scrollbars_are_not_stops_on_the_ring(self):
        window = self.window()
        window.toggle_settings()
        self.app.processEvents()
        items, _rects = window.nav._items()
        self.assertFalse([w for w in items if isinstance(w, QScrollBar)])

    def test_a_switch_row_counts_once_not_twice(self):
        window = self.window()
        window.toggle_settings()
        self.app.processEvents()
        items, _rects = window.nav._items()
        rows = [w for w in items if isinstance(w, gui.SwitchRow)]
        self.assertTrue(rows)
        for row in rows:
            self.assertNotIn(row.switch, items)


class MovementTests(RingTestCase):
    def test_the_ring_starts_on_play_and_walks_the_dock(self):
        window = self.window()
        self.assertIs(self.press(window, "down"), window.play_btn)
        self.assertTrue(window.nav.is_showing())
        self.assertIs(self.press(window, "left"), window.details_btn)

    def test_the_outline_follows_the_ring(self):
        window = self.window()
        self.press(window, "down")
        ring = window.nav._ring
        self.assertTrue(ring.isVisible())
        play = window.play_btn.geometry()
        self.assertGreater(ring.geometry().width(), play.width())
        self.assertTrue(ring.geometry().contains(
            play.translated(window.play_btn.parentWidget().mapTo(
                window, play.topLeft()) - play.topLeft())))

    def test_the_outline_is_actually_painted(self):
        """The whole feature is invisible if paintEvent is wrong.

        The gear button carries no accent colour of its own, so every accent
        pixel in the area the ring claims has to have come from the ring.
        """
        window = self.window()
        target = window.details_btn
        accent = QColor(window.theme.accent)

        def accent_pixels(pixmap):
            image = pixmap.toImage()
            hits = 0
            for y in range(0, image.height(), 2):
                for x in range(0, image.width(), 2):
                    pixel = image.pixelColor(x, y)
                    if (abs(pixel.red() - accent.red()) < 40
                            and abs(pixel.green() - accent.green()) < 40
                            and abs(pixel.blue() - accent.blue()) < 40):
                        hits += 1
            return hits

        window.nav.hide_ring()
        self.app.processEvents()
        before = accent_pixels(
            window.grab(target.geometry().adjusted(-6, -6, 6, 6)))
        window.nav.show_ring(target)
        self.app.processEvents()
        after = accent_pixels(window.grab(window.nav._ring.geometry()))
        self.assertEqual(before, 0)
        self.assertGreater(after, 20)

    def _pointer_at(self, point):
        return mock.patch("bol.navigation.QCursor.pos", return_value=point)

    def test_the_mouse_taking_over_hides_the_ring(self):
        window = self.window()
        inside = window.frameGeometry().center()
        with self._pointer_at(inside):
            self.press(window, "down")
            self.assertTrue(window.nav.is_showing())
        with self._pointer_at(inside + QPoint(120, 60)):
            window.nav._check_mouse()
        self.assertFalse(window.nav.is_showing())

    def test_a_resting_mouse_does_not_keep_taking_the_ring_away(self):
        """A mouse sitting still reports a pixel or two of drift.

        Acting on that made every controller press look like it did nothing:
        the ring was hidden between presses, so each press only revealed it
        again instead of moving. Found on a real desktop, invisible offscreen.
        """
        window = self.window()
        inside = window.frameGeometry().center()
        with self._pointer_at(inside):
            self.press(window, "down")
        for drift in (QPoint(1, 0), QPoint(2, 1), QPoint(3, 2)):
            with self._pointer_at(inside + drift):
                window.nav._check_mouse()
            self.assertTrue(window.nav.is_showing())

    def test_a_pointer_on_another_screen_is_not_the_mouse_taking_over(self):
        window = self.window()
        inside = window.frameGeometry().center()
        with self._pointer_at(inside):
            self.press(window, "down")
        far_away = window.frameGeometry().topRight() + QPoint(4000, 0)
        with self._pointer_at(far_away):
            window.nav._check_mouse()
        self.assertTrue(window.nav.is_showing())

    def test_the_press_after_the_mouse_only_brings_the_ring_back(self):
        window = self.window()
        self.press(window, "down", "left")
        where = window.nav._current
        window.nav.hide_ring()
        self.assertIs(self.press(window, "down"), where)
        self.assertTrue(window.nav.is_showing())

    def test_settings_is_walked_in_reading_order(self):
        window = self.window()
        window.toggle_settings()
        self.app.processEvents()
        window.nav.enter(window.settings_page)
        seen = []
        for _ in range(6):
            seen.append(self.press(window, "down"))
        # Every step lands somewhere new and inside the page.
        self.assertEqual(len(set(seen)), len(seen))
        for widget in seen:
            self.assertTrue(window.settings_page.isAncestorOf(widget))

    def test_walking_past_the_fold_scrolls_the_panel(self):
        window = self.window()
        window.toggle_settings()
        self.app.processEvents()
        # The visible tab's scroll area: each tab has its own, and the
        # hidden ones never move.
        area = window.nav._largest_scroll_area()
        bar = area.verticalScrollBar()
        bar.setValue(bar.minimum())
        window.nav.enter(window.settings_page)
        for _ in range(14):
            self.press(window, "down")
        self.assertGreater(bar.value(), bar.minimum())


class ActionTests(RingTestCase):
    def test_a_opens_settings_and_b_leaves_it(self):
        window = self.window()
        self.press(window, "down")
        settings = window.rail_buttons["settings"]
        # Left out of the dock into the rail, then along it to Settings.
        for _ in range(30):
            current = window.nav._current
            if current is settings:
                break
            if window.rail.isAncestorOf(current):
                above = (current.mapToGlobal(current.rect().center()).y()
                         > settings.mapToGlobal(settings.rect().center()).y())
                self.press(window, "up" if above else "down")
            else:
                self.press(window, "left")
        self.assertIs(window.nav._current, settings)
        self.press(window, "accept")
        self.assertIs(window.stack.currentWidget(), window.settings_page)
        self.press(window, "back")
        self.assertIs(window.stack.currentWidget(), window.hero_page)

    def test_opening_a_page_takes_the_ring_with_it(self):
        window = self.window()
        self.press(window, "down")
        window.toggle_settings()
        self.app.processEvents()
        self.assertTrue(
            window.settings_page.isAncestorOf(window.nav._current))

    def test_a_page_opened_with_the_mouse_does_not_summon_a_ring(self):
        window = self.window()
        window.toggle_settings()
        self.app.processEvents()
        self.assertFalse(window.nav.is_showing())

    def test_a_toggles_a_settings_switch(self):
        window = self.window()
        window.toggle_settings()
        self.app.processEvents()
        items, _rects = window.nav._items()
        row = next(w for w in items if isinstance(w, gui.SwitchRow))
        before = row.isChecked()
        window.nav.show_ring(row)
        self.press(window, "accept")
        self.assertNotEqual(row.isChecked(), before)

    def test_start_presses_play(self):
        window = self.window()
        with mock.patch.object(gui.MainWindow, "do_play") as play:
            self.press(window, "start")
        self.assertEqual(play.call_count, 1)

    def test_the_shoulder_buttons_change_tab(self):
        window = self.window()
        window.toggle_settings()
        self.app.processEvents()
        tabs = window.nav._tab_bar()
        self.assertIsNotNone(tabs)
        self.assertEqual(tabs.currentIndex(), 0)
        self.press(window, "next_tab")
        self.assertEqual(tabs.currentIndex(), 1)
        self.press(window, "prev_tab")
        self.assertEqual(tabs.currentIndex(), 0)
        self.press(window, "prev_tab")
        self.assertEqual(tabs.currentIndex(), tabs.count() - 1)  # wraps

    def test_the_scroll_stick_moves_the_page(self):
        window = self.window()
        window.toggle_settings()
        self.app.processEvents()
        area = window.nav._largest_scroll_area()
        bar = area.verticalScrollBar()
        bar.setValue(bar.minimum())
        self.press(window, "scroll_down", "scroll_down")
        self.assertGreater(bar.value(), bar.minimum())


class PopupTests(RingTestCase):
    def _open_picker(self, window):
        labels = ["1.21.130.7", "1.21.120.4", "1.21.110.2"]
        window.ui_state["labels"] = labels
        window.ui_state["versions"] = [
            {"tag": tag, "beta": False, "edition": "release"}
            for tag in labels]
        window.open_picker()
        self.app.processEvents()
        return labels

    def test_a_dropdown_confines_the_ring_and_b_closes_it(self):
        window = self.window()
        self.press(window, "down")
        self._open_picker(window)
        popup = QApplication.activePopupWidget()
        self.assertIsNotNone(popup)
        self.assertIs(window.nav._scope(), popup)
        items, _rects = window.nav._items()
        self.assertTrue(items)
        for widget in items:
            self.assertTrue(popup.isAncestorOf(widget))
        self.press(window, "back")
        self.assertIsNone(QApplication.activePopupWidget())

    def test_the_version_list_moves_its_own_selection(self):
        window = self.window()
        labels = self._open_picker(window)
        picked = []
        window.set_version = picked.append
        listing = window.version_popup.list
        window.nav.show_ring(listing)
        listing.setCurrentRow(0)
        self.press(window, "down")
        self.assertEqual(listing.currentRow(), 1)
        # …and A picks the highlighted row, not the first one on the list.
        self.press(window, "accept")
        self.assertEqual(picked, [labels[1]])

    def test_a_dialog_confines_the_ring(self):
        window = self.window()
        box = QMessageBox(window)
        box.setText("Test")
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        seen = {}

        def inspect():
            self.app.processEvents()
            seen["scope"] = window.nav._scope()
            items, _rects = window.nav._items()
            seen["items"] = [w for w in items if isinstance(w, QPushButton)]
            window.nav.dispatch("back")

        QTimer.singleShot(0, inspect)
        QTimer.singleShot(2000, box.reject)
        box.exec()
        self.assertIs(seen["scope"], box)
        self.assertEqual(len(seen["items"]), 2)


    def test_the_offline_warning_is_answerable_with_a_pad(self):
        """PLAY's offline warning stands between a couch player and the game
        (#240). Game Mode has no mouse in reach, so the ring has to reach both
        of its buttons -- and B has to answer it, or the warning is a wall."""
        window = self.window()
        seen = {}

        def inspect():
            self.app.processEvents()
            items, _rects = window.nav._items()
            seen["labels"] = sorted(w.text() for w in items
                                    if isinstance(w, QPushButton))
            window.nav.dispatch("back")

        QTimer.singleShot(0, inspect)
        QTimer.singleShot(2000, lambda: QApplication.activeModalWidget()
                          and QApplication.activeModalWidget().reject())
        choice = window._offer_online_sign_in()

        self.assertEqual(seen["labels"], ["Play offline", "Sign in"])
        # B is the way out of every other dialog in the launcher, so it has to
        # mean the harmless answer here: play, rather than a sign-in nobody
        # asked for.
        self.assertEqual(choice, "play")


class InputGateTests(RingTestCase):
    def test_input_is_ignored_while_the_game_is_running(self):
        window = self.window()
        window.ui_state["launch_active"] = True
        self.assertFalse(window.nav.ready())
        self.press(window, "down")
        self.assertFalse(window.nav.is_showing())
        window.ui_state["launch_active"] = False
        self.assertTrue(window.nav.ready())

    def test_input_is_ignored_while_the_window_has_stepped_aside(self):
        window = self.window()
        window._step_aside_for_game()
        self.app.processEvents()
        self.assertFalse(window.nav.ready())
        window._come_back_from_game()
        self.app.processEvents()
        self.assertTrue(window.nav.ready())

    def test_turning_navigation_off_stops_the_reader(self):
        window = self.window()
        reader = window.nav._reader
        window.apply_controller_nav(False)
        self.assertTrue(reader.stopped)
        self.assertIsNone(window.nav)
        self.assertFalse(window.nav_legend.isVisible())
        window.apply_controller_nav(True)
        self.assertIsNotNone(window.nav)
        self.assertTrue(window.nav_legend.isVisible())

    def test_the_setting_keeps_it_off(self):
        window = self.window(controller_nav=False)
        self.assertIsNone(window.nav)
        self.assertFalse(window.nav_legend.isVisible())

    def test_a_machine_with_no_input_layer_is_not_an_error(self):
        patch = mock.patch("bol.navigation.GamepadReader", DeadReader)
        patch.start()
        self.addCleanup(patch.stop)
        with headless_window(controller_nav=True) as window:
            self.assertIsNone(window.nav)
            self.assertIn("off", window.controller_status.text())

    def test_the_legend_names_the_buttons_while_a_pad_is_connected(self):
        window = self.window()
        self.assertTrue(window.nav_legend.isVisible())
        text = window.nav_legend.text()
        for name in ("A", "Select", "B", "Back", "Start", "Play"):
            self.assertIn(name, text)
        self.assertIn("Test Pad", window.controller_status.text())

    def test_closing_the_window_lets_go_of_the_controller(self):
        window = self.window()
        reader = window.nav._reader
        window._force_close = True
        window.close()
        self.app.processEvents()
        self.assertTrue(reader.stopped)


if __name__ == "__main__":
    unittest.main()
