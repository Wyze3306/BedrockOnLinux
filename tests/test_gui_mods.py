"""The Mods page: what it shows for a build, and what its buttons do."""
# SPDX-License-Identifier: MIT

import time
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QMessageBox

from bol import gui, gui_mods, mods
from tests.guiharness import headless_window, qt_app

BUILD = Path("/games/release/1.26.60.5")
CATALOGUE = [
    {"id": "github.com/LiteLDev/LeviLamina", "name": "LeviLamina",
     "owner": "LiteLDev", "description": "The loader", "tags": [],
     "gameVersions": ["1.26.60"], "stars": 1700},
    {"id": "github.com/A/Fast", "name": "Fast", "owner": "A",
     "description": "Makes it fast", "tags": ["type:mod", "optimization"],
     "gameVersions": ["1.26.60", "1.26.51"], "stars": 50,
     "updated": "2026-11-01T00:00:00Z"},
    {"id": "github.com/B/Old", "name": "Old", "owner": "B",
     "description": "For an older game", "tags": ["utility"],
     "gameVersions": ["1.26.51"], "stars": 900},
]


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _state(installed=None, enabled=True, supported=True):
    return {"game_dir": BUILD, "game_version": "1.26.60.5",
            "supported": supported,
            "levilamina_versions": ["26.60.6"] if supported else [],
            "levilamina": installed, "levilamina_latest": "26.60.6"
            if supported else None, "enabled": enabled, "error": None}


class ModsPageTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        qt_app()

    def _page(self, window, state, items=(), packages=None):
        patches = [
            mock.patch.object(gui, "installed_builds", return_value=[
                {"edition": "release", "version": "1.26.60.5",
                 "path": BUILD, "playable": True, "in_use": True}]),
            mock.patch.object(mods, "catalogue",
                              lambda refresh=False: CATALOGUE),
            mock.patch.object(mods, "describe", lambda d: dict(state)),
            mock.patch.object(mods, "installed", lambda d: list(items)),
            mock.patch.object(mods, "packages",
                              lambda d: dict(packages or {})),
            mock.patch.object(mods, "supported_builds",
                              lambda refresh=False: ["1.26.60.05"]),
            mock.patch.object(mods, "logo", lambda url: None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        window.toggle_mods()
        page = window.mods_page
        self.assertTrue(_wait(lambda: page.catalogue and page.state))
        return page

    def test_the_top_bar_opens_the_page_and_back_closes_it(self):
        with headless_window() as window:
            page = self._page(window, _state())
            self.assertIs(window.stack.currentWidget(), page)
            window._controller_back()
            self.assertIs(window.stack.currentWidget(), window.hero_page)

    def test_browse_lists_the_mods_for_this_build_and_not_the_loader(self):
        with headless_window() as window:
            page = self._page(window, _state())
            names = [card.mod["name"] for card in page._cards]
            self.assertEqual(names, ["Fast"])
            page.compat_switch.switch.setChecked(False)
            names = [card.mod["name"] for card in page._cards]
            self.assertEqual(names, ["Old", "Fast"])
            page.search.setText("older")
            page._render()
            self.assertEqual([c.mod["name"] for c in page._cards], ["Old"])

    def test_install_on_a_card_installs_that_mod_into_the_build(self):
        with headless_window() as window, \
                mock.patch.object(mods, "install",
                                  return_value=["github.com/A/Fast"]) as inst:
            page = self._page(window, _state())
            self.assertIn("ready for Minecraft 1.26.60.5",
                          page.status_title.text())
            page._cards[0].action.click()
            self.assertTrue(_wait(lambda: inst.called and not page.busy))
            args, kwargs = inst.call_args
            self.assertEqual(args[:2], (BUILD, ["github.com/A/Fast"]))
            self.assertIn("Installed Fast", page.op_label.text())

    def test_an_unsupported_build_says_why_and_offers_no_install(self):
        with headless_window() as window:
            page = self._page(window, _state(supported=False))
            self.assertIn("does not support", page.status_title.text())
            self.assertIn("1.26.60.5", page.status_detail.text())
            self.assertFalse(page.ll_action.isVisible())
            page.compat_switch.switch.setChecked(False)
            self.assertFalse(page._cards[0].action.isEnabled())

    def test_installed_mods_can_be_turned_off_and_mods_switched_off(self):
        items = [
            {"folder": "LeviLamina", "path": BUILD / "mods/LeviLamina",
             "name": "LeviLamina", "version": "26.60.6", "enabled": True,
             "package": "github.com/LiteLDev/LeviLamina#client",
             "id": "github.com/LiteLDev/LeviLamina", "explicit": True,
             "source": "catalogue", "core": True, "type": ""},
            {"folder": "Mine", "path": BUILD / "mods/Mine", "name": "Mine",
             "version": "1.0", "enabled": True, "package": None, "id": None,
             "explicit": False, "source": "imported", "core": False,
             "type": "native"},
        ]
        with headless_window() as window, \
                mock.patch.object(mods, "set_enabled") as set_enabled, \
                mock.patch.object(mods, "set_mods_enabled") as set_all:
            page = self._page(window, _state(installed="26.60.6"), items,
                              {"github.com/LiteLDev/LeviLamina#client":
                               {"version": "26.60.6"}})
            self.assertIn("LeviLamina 26.60.6", page.status_title.text())
            self.assertEqual(page.tabs.tabText(1), "Installed (1)")
            rows = [page.installed_layout.itemAt(i).widget()
                    for i in range(page.installed_layout.count())]
            mine = [r for r in rows if r.item["folder"] == "Mine"][0]
            switch = [w for w in mine.findChildren(gui.SwitchRow)][0]
            switch.switch.setChecked(False)
            set_enabled.assert_called_once_with(BUILD, "Mine", False)
            page.mods_switch.switch.setChecked(False)
            set_all.assert_called_once_with(BUILD, False)

    def test_details_show_the_readme_without_its_images(self):
        document = {"versions": [{"version": "1.0.0"}],
                    "latest": {"version": "1.0.0"},
                    "readmes": [{"lang": "en", "text":
                                 "# Fast\n\n![badge](https://x/b.svg)\n\n"
                                 "Makes **everything** fast."}]}
        with headless_window() as window, \
                mock.patch.object(mods, "detail",
                                  lambda pid, refresh=False: document), \
                mock.patch.object(QMessageBox, "question",
                                  return_value=QMessageBox.Yes):
            page = self._page(window, _state())
            page.open_details(CATALOGUE[1])
            details = page._details
            self.assertTrue(_wait(lambda: "Latest version 1.0.0"
                                  in details.info.text()))
            text = details.readme.toPlainText()
            self.assertIn("Makes everything fast.", text)
            self.assertNotIn("badge", text)
            details.close()


def test_readmes_lose_remote_images_and_layout_html_but_not_code():
    text = gui_mods._readable_markdown(
        '<p align="center"><img src="logo.png"></p>\n'
        "[![CI](https://x/ci.svg)](https://x)\n"
        "Text ![img](a.png) after\n"
        "```\n<img keep>\n```\n")
    assert "img src" not in text and "ci.svg" not in text
    assert "Text  after" in text
    assert "<img keep>" in text
