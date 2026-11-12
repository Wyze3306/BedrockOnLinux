"""The Add-ons & Maps page, the navigation rail and the home shelf."""
# SPDX-License-Identifier: MIT

import time
import unittest
from unittest import mock

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QMessageBox

from bol import catalog, gui_content
from tests.guiharness import headless_window, qt_app


def _item(slug, title=None, kind="addons"):
    return {"slug": slug, "title": title or slug.title(), "summary": "s",
            "image": "", "author": "Someone", "downloads": 1234,
            "rating": 4.5, "kind": kind, "tags": [], "updated": "",
            "url": f"https://mcpedl.com/{slug}/"}


def _page(items, page=1, pages=2):
    return {"items": items, "page": page, "pages": pages,
            "total": len(items) * pages}


DETAIL = dict(_item("cool"), versions=["1.21.130"], description="About",
              installation="", images=[], curseforge="https://www.curseforge"
              ".com/minecraft-bedrock/addons/cool", source="curseforge",
              files=[{"name": "Cool.mcaddon", "label": "Cool v2",
                      "url": "https://edge.mcpedl.com/c", "size": "1 KB",
                      "date": "", "installable": True},
                     {"name": "readme.txt", "label": "readme", "url":
                      "https://edge.mcpedl.com/r", "size": "", "date": "",
                      "installable": False}],
              links=[{"name": "Mirror", "url": "https://mcpedl.com/leaving/"
                      "?url=x", "host": "example.com"}])


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


class RailTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        qt_app()

    def test_every_rail_item_opens_its_page_and_lights_up(self):
        with headless_window() as window, \
                mock.patch.object(catalog, "search",
                                  return_value=_page([])), \
                mock.patch.object(catalog, "library", return_value=[]):
            for key, opener, page in (
                    ("settings", window.open_settings, "settings_page"),
                    ("changelog", window.open_changelog, "changelog_page"),
                    ("profiles", window.open_profiles, "profiles_page"),
                    ("content", window.open_content, "content_page"),
                    ("mods", window.open_mods, "mods_page"),
                    ("home", window.go_home, "hero_page")):
                with mock.patch.object(window, "load_changelogs"), \
                        mock.patch.object(window, "refresh_profiles"):
                    opener()
                    _wait(lambda: True, 0.05)
                self.assertIs(window.stack.currentWidget(),
                              getattr(window, page), key)
                self.assertTrue(window.rail_buttons[key].isChecked(), key)
                self.assertEqual(window.page_title.text(),
                                 window._PAGE_TITLES[key])

    def test_opening_a_page_twice_from_the_rail_keeps_it_open(self):
        with headless_window() as window:
            window.open_settings()
            window.open_settings()
            self.assertIs(window.stack.currentWidget(), window.settings_page)

    def test_the_old_toggles_still_come_back_home(self):
        with headless_window() as window, \
                mock.patch.object(catalog, "search",
                                  return_value=_page([])):
            window.toggle_content()
            self.assertIs(window.stack.currentWidget(), window.content_page)
            window.toggle_content()
            self.assertIs(window.stack.currentWidget(), window.hero_page)
            self.assertTrue(window.rail_buttons["home"].isChecked())


class RailFoldTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        qt_app()

    def test_the_rail_folds_to_icons_and_remembers_it(self):
        saved = {}
        with headless_window() as window, \
                mock.patch("bol.gui.save_settings", side_effect=saved.update):
            window.resize(1300, 760)
            self.assertEqual(window.rail.width(), window.RAIL_WIDE)
            self.assertIn("Settings", window.rail_buttons["settings"].text())
            window.rail_fold.click()
            self.assertEqual(window.rail.width(), window.RAIL_NARROW)
            self.assertEqual(window.rail_buttons["settings"].text(), "⚙")
            self.assertFalse(window.prof_label.isVisibleTo(window.rail))
            self.assertTrue(saved.get("rail_collapsed"))
            window.rail_unfold.click()
            self.assertEqual(window.rail.width(), window.RAIL_WIDE)
            self.assertFalse(saved.get("rail_collapsed"))

    def test_a_narrow_window_folds_the_rail_without_saving(self):
        with headless_window() as window, \
                mock.patch("bol.gui.save_settings") as save:
            window.show()
            window.resize(1300, 760)
            _wait(lambda: window.rail.width() == window.RAIL_WIDE, 1)
            window.resize(1000, 700)
            self.assertTrue(_wait(
                lambda: window.rail.width() == window.RAIL_NARROW, 2))
            self.assertFalse(window.rail_unfold.isEnabled())
            window.resize(1300, 760)
            self.assertTrue(_wait(
                lambda: window.rail.width() == window.RAIL_WIDE, 2))
            save.assert_not_called()

    def test_profile_and_accounts_live_in_the_rail(self):
        with headless_window() as window:
            self.assertTrue(window.rail.isAncestorOf(window.prof_card))
            self.assertTrue(window.rail.isAncestorOf(window.acct_card))
            self.assertFalse(hasattr(window, "settings_btn"))


class ContentPageTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        qt_app()

    def _open(self, window, search):
        with mock.patch.object(catalog, "search", search):
            window.open_content()
            page = window.content_page
            self.assertTrue(_wait(lambda: page.items or
                                  page.count_label.text() not in
                                  ("", "Loading…")))
        return page

    def test_discover_lists_a_kind_and_loads_more(self):
        calls = []

        def search(query, kind, sort, page_no=1, refresh=False):
            calls.append((query, kind, sort, page_no))
            return _page([_item(f"{kind}-{page_no}-{i}") for i in range(3)],
                         page_no, 2)

        with headless_window() as window:
            page = self._open(window, search)
            self.assertEqual(calls[0], ("", "addons", "popular", 1))
            self.assertEqual(len(page._cards), 3)
            self.assertTrue(page.more_btn.isVisibleTo(page))
            with mock.patch.object(catalog, "search", search):
                page.load_more()
                self.assertTrue(_wait(lambda: len(page._cards) == 6))
                self.assertFalse(page.more_btn.isVisibleTo(page))
                page.kind_buttons["maps"].click()
                self.assertTrue(_wait(lambda: page.items and page.items[0][
                    "slug"].startswith("maps-")))
            self.assertEqual(calls[-1][1], "maps")
            self.assertIn("maps", page.count_label.text())

    def test_a_search_disables_the_sort(self):
        seen = []

        def search(query, kind, sort, page_no=1, refresh=False):
            seen.append(query)
            return _page([_item("x")])

        with headless_window() as window:
            page = self._open(window, search)
            with mock.patch.object(catalog, "search", search):
                page.search.setText("castle")
                page._search_now()
                self.assertTrue(_wait(lambda: seen[-1] == "castle"))
            self.assertFalse(page.sort.isEnabled())

    def test_a_pasted_link_opens_the_project(self):
        with headless_window() as window:
            page = self._open(window, lambda *a, **k: _page([]))
            with mock.patch.object(page, "open_details") as details:
                page.search.setText("https://www.curseforge.com/"
                                    "minecraft-bedrock/maps/castle")
                page._search_now()
            self.assertEqual(details.call_args.args[0]["slug"], "castle")

    def test_details_install_the_file_and_mark_it_installed(self):
        with headless_window() as window:
            page = self._open(window, lambda *a, **k: _page([_item("cool")]))
            with mock.patch.object(catalog, "details",
                                   return_value=DETAIL), \
                    mock.patch.object(catalog, "install",
                                      return_value=["behavior pack: Cool"]
                                      ) as install, \
                    mock.patch.object(catalog, "installed_slugs",
                                      return_value={"cool"}), \
                    mock.patch("bol.prefix._mc_running", return_value=False):
                page.open_details(page.items[0])
                dialog = page._details
                self.assertTrue(_wait(lambda: dialog.detail is not None))
                buttons = [b for b in dialog.files_host.findChildren(
                    gui_content.QPushButton) if b.text() == "Install"]
                self.assertEqual(len(buttons), 1)  # readme is not content
                buttons[0].click()
                self.assertTrue(_wait(lambda: install.called and
                                      not page.busy))
            self.assertEqual(install.call_args.args[1]["name"],
                             "Cool.mcaddon")
            self.assertIn("Installed Cool", page.op_label.text())
            self.assertTrue(page._cards[0].badge.isVisibleTo(page._cards[0]))
            self.assertTrue(dialog.cf_button.isVisibleTo(dialog))
            dialog.close()

    def test_a_failed_install_is_reported(self):
        with headless_window() as window:
            page = self._open(window, lambda *a, **k: _page([_item("cool")]))
            with mock.patch.object(catalog, "install",
                                   side_effect=catalog.CatalogError("gone")), \
                    mock.patch.object(QMessageBox, "warning") as warning:
                page.install(_item("cool"), DETAIL["files"][0])
                self.assertTrue(_wait(lambda: warning.called))
            self.assertFalse(page.busy)
            self.assertIn("gone", page.op_label.text())

    def test_library_lists_and_removes_after_asking(self):
        entries = [{"name": "My World", "kind": "World",
                    "sub": "minecraftWorlds", "path": "/x/com.mojang/"
                    "minecraftWorlds/w", "icon": "", "description": "",
                    "slug": None, "modified": 1},
                   {"name": "Cool", "kind": "Behaviour pack",
                    "sub": "behavior_packs", "path": "/x/com.mojang/"
                    "behavior_packs/c", "icon": "", "description": "",
                    "slug": "cool", "modified": 0}]
        with headless_window() as window:
            page = self._open(window, lambda *a, **k: _page([]))
            with mock.patch.object(catalog, "library",
                                   return_value=entries):
                page.tabs.setCurrentIndex(1)
                self.assertTrue(_wait(lambda: page.library_entries))
                self.assertEqual(page.tabs.tabText(1), "Library (2)")
                rows = page.findChildren(gui_content.LibraryRow)
                self.assertEqual(len(rows), 2)
                page.library_kind.setCurrentIndex(
                    page.library_kind.findData("minecraftWorlds"))
                rows = [r for r in page.findChildren(
                    gui_content.LibraryRow) if not r.isHidden()
                    and r.parent() is not None]
                with mock.patch.object(QMessageBox, "question",
                                       return_value=QMessageBox.Cancel), \
                        mock.patch.object(catalog, "remove") as remove:
                    page.remove(entries[0])
                remove.assert_not_called()
                with mock.patch.object(QMessageBox, "question",
                                       return_value=QMessageBox.Yes), \
                        mock.patch.object(catalog, "remove") as remove:
                    page.remove(entries[0])
                    self.assertTrue(_wait(lambda: remove.called))
                remove.assert_called_with(entries[0]["path"])


class FeaturedShelfTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        qt_app()

    def test_the_shelf_shows_a_kind_and_opens_a_project(self):
        shelves = {"addons": [_item(f"a{i}") for i in range(6)],
                   "maps": [_item("m1", kind="maps")], "textures": []}
        with headless_window() as window:
            shelf = window.featured
            shelf.resize(1100, 260)
            shelf._loaded(shelves)
            self.assertTrue(shelf._cards)
            self.assertTrue(all(c.item["slug"].startswith("a")
                                for c in shelf._cards))
            shelf.show_kind("maps")
            self.assertEqual([c.item["slug"] for c in shelf._cards], ["m1"])
            with mock.patch.object(window, "open_content") as open_content:
                shelf.open_details(shelf._cards[0].item)
            open_content.assert_called_with(slug="m1")

    def test_turning_the_shelf_off_hides_it(self):
        with headless_window(home_featured=False) as window:
            self.assertTrue(window.featured.isHidden())


class HelperTests(unittest.TestCase):

    def test_long_titles_are_cut_to_two_lines(self):
        long = "Part II - OP Tool Smith + Mod " * 4
        cut = gui_content._two_lines(long, 220)
        self.assertTrue(cut.endswith("…"))
        self.assertLess(len(cut), len(long))
        self.assertEqual(gui_content._two_lines("Short", 220), "Short")

    def test_counts_read_short(self):
        self.assertEqual(gui_content._count(17_303_422), "17.3M")
        self.assertEqual(gui_content._count(4_000), "4k")
        self.assertEqual(gui_content._count(999), "999")
        self.assertEqual(gui_content._count(None), "")
