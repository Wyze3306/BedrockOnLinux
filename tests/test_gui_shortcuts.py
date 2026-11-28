"""The Shortcuts page and its editor (#392): what the cards say, what the
buttons do, and how a shortcut that opens the launcher starts its game. No
game, no menu file outside a temporary folder, no second launcher."""
# SPDX-License-Identifier: MIT

import os
import time
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QDialog

from bol import gui_shortcuts, shortcuts
from tests.guiharness import headless_window, qt_app
from tests.test_shortcuts import _Isolated


def _pump(times=20):
    for _ in range(times):
        QCoreApplication.processEvents()
        time.sleep(0.002)


class _Page(_Isolated):

    def setUp(self):
        super().setUp()
        qt_app()
        patcher = mock.patch.object(gui_shortcuts, "list_profiles",
                                    return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)
        root = mock.patch.object(shortcuts, "root",
                                 return_value=self.base / "shortcuts")
        root.start()
        self.addCleanup(root.stop)
        self.window_cm = headless_window()
        self.window = self.window_cm.__enter__()
        self.addCleanup(self.window_cm.__exit__, None, None, None)
        self.window.resize(1120, 760)

    def page(self):
        self.window.open_shortcuts()
        _pump()
        return self.window.shortcuts_page


class PageTests(_Page):

    def test_rail_opens_the_page(self):
        self.assertIn("shortcuts", self.window.rail_buttons)
        self.window.rail_buttons["shortcuts"].click()
        _pump()
        self.assertIs(self.window.stack.currentWidget(),
                      self.window.shortcuts_page)
        self.assertEqual(self.window.page_title.text(), "Shortcuts")
        self.assertEqual(self.window._page_key(), "shortcuts")

    def test_empty_page_offers_one_and_the_installed_builds(self):
        page = self.page()
        self.assertEqual(page.cards, [])
        self.assertTrue(page.empty.isVisibleTo(page))
        offered = [(row.edition_id, row.version) for row in page.suggestions]
        self.assertEqual(offered, [
            ("release", "latest"), ("release", "1.26.65.1"),
            ("release", "1.21.120.4"), ("preview", "latest"),
            ("preview", "1.26.70.26")])

    def test_cards_say_what_they_start(self):
        self.make(version="1.21.120.4", desktop=True)
        self.make(edition="preview", start="direct", editor=True)
        self.make(edition="education")
        page = self.page()
        first, second, third = page.cards
        self.assertEqual(first.name.text(), "Minecraft Bedrock (1.21.120.4)")
        self.assertIn("Minecraft Bedrock 1.21.120.4", first.summary.text())
        self.assertIn("App menu · Desktop", first.where.text())
        self.assertIn("(1.26.70.26)", second.summary.text())
        self.assertEqual([b.text() for b in second.badges],
                         ["Game only", "Bedrock Editor"])
        self.assertEqual(third.badges[0].text(), "Not installed")
        self.assertTrue(third.play.isEnabled())
        # Covered builds are no longer offered.
        offered = [(row.edition_id, row.version) for row in page.suggestions]
        self.assertNotIn(("release", "1.21.120.4"), offered)
        self.assertNotIn(("preview", "latest"), offered)

    def test_add_makes_a_shortcut_in_one_click(self):
        page = self.page()
        row = next(r for r in page.suggestions if r.version == "1.21.120.4")
        row.add.click()
        _pump()
        item, = shortcuts.load()
        self.assertEqual((item["edition"], item["version"]),
                         ("release", "1.21.120.4"))
        self.assertTrue(shortcuts.menu_file(item).exists())
        self.assertEqual(len(page.cards), 1)

    def test_remove_asks_first(self):
        item = self.make()
        page = self.page()
        with mock.patch.object(self.window, "question_box",
                               return_value=False):
            page.remove(item)
        self.assertEqual(len(shortcuts.load()), 1)
        with mock.patch.object(self.window, "question_box",
                               return_value=True):
            page.remove(item)
        self.assertEqual(shortcuts.load(), [])
        self.assertFalse(shortcuts.menu_file(item).exists())

    def test_play_here_or_through_the_menu_command(self):
        here = self.make()
        direct = self.make(start="direct")
        page = self.page()
        with mock.patch.object(gui_shortcuts, "start_in_window") as started, \
                mock.patch.object(gui_shortcuts.subprocess, "Popen") as spawn, \
                mock.patch.object(gui_shortcuts, "current_profile_info",
                                  return_value={"slug": None}):
            page.play(here)
            started.assert_called_once_with(self.window, here["id"])
            page.play(direct)
        self.assertEqual(spawn.call_args.args[0],
                         self.argv + ["shortcuts", "run", direct["id"]])

    def test_copy_command(self):
        item = self.make()
        page = self.page()
        with mock.patch.object(self.window, "info_box"):
            page.copy_command(item)
        from PySide6.QtGui import QGuiApplication
        self.assertEqual(QGuiApplication.clipboard().text(),
                         "'/opt/My Games/BedrockOnLinux.AppImage' shortcuts "
                         f"run {item['id']}")


class DialogTests(_Page):

    def tearDown(self):
        from PySide6.QtCore import QEvent
        from PySide6.QtWidgets import QApplication
        for widget in QApplication.topLevelWidgets():
            if isinstance(widget, gui_shortcuts.ShortcutDialog):
                widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    def test_new_shortcut_follows_the_choices(self):
        dialog = gui_shortcuts.ShortcutDialog(self.window)
        self.assertEqual(dialog.name.text(), "Minecraft Bedrock (Latest)")
        dialog.version.setCurrentIndex(dialog.version.findData("1.21.120.4"))
        self.assertEqual(dialog.name.text(), "Minecraft Bedrock (1.21.120.4)")
        self.assertFalse(dialog.update_first.isEnabled())
        dialog.game_only.picked.emit()
        dialog.env.setText("MANGOHUD=1")
        dialog.desktop.switch.setChecked(True)
        dialog.accept_changes()
        self.assertEqual(dialog.result(), QDialog.Accepted)
        item, = shortcuts.load()
        self.assertEqual(
            {k: item[k] for k in ("edition", "version", "start", "env",
                                  "menu", "desktop")},
            {"edition": "release", "version": "1.21.120.4",
             "start": "direct", "env": "MANGOHUD=1", "menu": True,
             "desktop": True})
        self.assertTrue(Path(item["desktop_file"]).exists())

    def test_a_name_typed_by_hand_stays(self):
        dialog = gui_shortcuts.ShortcutDialog(self.window)
        dialog.name.setText("Server build")
        dialog.name.textEdited.emit("Server build")
        dialog.game.setCurrentIndex(dialog.game.findData("preview"))
        self.assertEqual(dialog.name.text(), "Server build")

    def test_other_titles_lose_what_is_bedrock_only(self):
        dialog = gui_shortcuts.ShortcutDialog(self.window)
        dialog.editor.switch.setChecked(True)
        dialog.game.setCurrentIndex(dialog.game.findData("dungeons"))
        self.assertFalse(dialog.editor.isEnabled())
        self.assertFalse(dialog.editor.isChecked())
        self.assertFalse(dialog.version.isEnabled())
        self.assertEqual(dialog.version.currentText(),
                         "From the Microsoft Store")
        self.assertIn("(not installed)", dialog.game.currentText())

    def test_errors_stay_in_the_dialog(self):
        dialog = gui_shortcuts.ShortcutDialog(self.window)
        dialog.menu.switch.setChecked(False)
        dialog.accept_changes()
        self.assertNotEqual(dialog.result(), QDialog.Accepted)
        self.assertIn("app menu", dialog.error.text())
        self.assertEqual(shortcuts.load(), [])

    def test_edit_keeps_a_build_that_is_gone(self):
        item = self.make(version="1.20.0.1")
        dialog = gui_shortcuts.ShortcutDialog(self.window, item=item)
        self.assertEqual(dialog.version.currentData(), "1.20.0.1")
        self.assertIn("not installed", dialog.version.currentText())
        dialog.name.setText("Old build")
        dialog.accept_changes()
        self.assertEqual(shortcuts.get(item["id"])["name"], "Old build")

    def test_flatpak_has_no_desktop(self):
        with mock.patch.object(shortcuts, "in_flatpak", return_value=True):
            dialog = gui_shortcuts.ShortcutDialog(self.window)
        self.assertFalse(dialog.desktop.isEnabled())
        self.assertFalse(dialog.desktop.isChecked())


class StartTests(_Page):

    def versions(self, *entries):
        self.window.ui_state["versions"] = [
            {"tag": tag, "edition": {"id": edition}, "installed": installed,
             "beta": edition == "preview"}
            for edition, tag, installed in entries]

    def start(self, item, **settings):
        played = []

        def do_play(**options):
            played.append((dict(os.environ), options))
        with mock.patch.object(self.window, "select_build",
                               return_value=True) as select, \
                mock.patch.object(self.window, "do_play",
                                  side_effect=do_play), \
                mock.patch("bol.util.load_settings",
                           return_value=dict(settings)):
            starter = gui_shortcuts.start_in_window(self.window, item["id"])
            _pump(5)
        return starter, select, played

    def test_pending_shortcut_is_started_once(self):
        environ = {shortcuts.START_ENV: "abcd1234"}
        with mock.patch.object(gui_shortcuts.QTimer, "singleShot") as later:
            self.assertTrue(gui_shortcuts.start_pending(self.window, environ))
            self.assertFalse(gui_shortcuts.start_pending(self.window,
                                                         environ))
        self.assertEqual(later.call_count, 1)
        self.assertEqual(environ, {})

    def test_pinned_build_is_picked_and_played_with_its_options(self):
        item = self.make(version="1.21.120.4", editor=True, hidden=True,
                         env="MANGOHUD=1")
        self.versions(("release", "1.26.70.1", False),
                      ("release", "1.26.65.1", True),
                      ("release", "1.21.120.4", True))
        os.environ.pop("MANGOHUD", None)
        starter, select, played = self.start(item)
        select.assert_called_once_with("1.21.120.4", False, "release")
        (env, options), = played
        self.assertEqual(options, {"editor": True, "hidden": True})
        self.assertEqual(env["MANGOHUD"], "1")
        # Not started (do_play was a stand-in): the variables are gone again.
        self.assertNotIn("MANGOHUD", os.environ)

    def test_latest_is_the_newest_installed_unless_updating(self):
        item = self.make()
        self.versions(("release", "1.26.70.1", False),
                      ("release", "1.26.65.1", True))
        _s, select, _p = self.start(item)
        select.assert_called_once_with("1.26.65.1", False, "release")
        _s, select, _p = self.start(item, auto_update_game=True)
        select.assert_called_once_with("1.26.70.1", False, "release")

    def test_a_missing_build_is_only_downloaded_when_asked(self):
        item = self.make(version="1.20.0.1")
        self.versions(("release", "1.26.65.1", True),
                      ("release", "1.20.0.1", False))
        with mock.patch.object(self.window, "question_box",
                               return_value=False) as asked:
            _s, select, played = self.start(item)
        asked.assert_called_once()
        select.assert_not_called()
        self.assertEqual(played, [])
        with mock.patch.object(self.window, "question_box",
                               return_value=True):
            _s, select, played = self.start(item)
        select.assert_called_once_with("1.20.0.1", False, "release")

    def test_waits_for_the_list_of_versions(self):
        item = self.make()
        self.window.ui_state["versions"] = []
        with mock.patch.object(self.window, "select_build") as select:
            starter = gui_shortcuts.start_in_window(self.window, item["id"])
            self.assertTrue(starter.timer.isActive())
            select.assert_not_called()
            starter.timer.stop()


if __name__ == "__main__":
    unittest.main()
