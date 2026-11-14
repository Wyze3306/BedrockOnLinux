"""The edition toggle next to PLAY offers Minecraft Education (#351)."""
# SPDX-License-Identifier: MIT

import unittest
from unittest import mock

from bol import gui, xodus
from tests.guiharness import headless_window, qt_app

RELEASE = xodus.edition("release")
PREVIEW = xodus.edition("preview")
EDUCATION = xodus.edition("education")


def _versions():
    return [
        {"tag": "1.26.33.3", "beta": False, "edition": RELEASE,
         "installed": False},
        {"tag": "1.26.60.24", "beta": True, "edition": PREVIEW,
         "installed": False},
        {"tag": "1.26.33.0", "beta": False, "edition": EDUCATION,
         "installed": False},
    ]


class LabelTests(unittest.TestCase):
    def test_every_edition_gets_a_label_of_its_own(self):
        labels = [gui._version_label(v) for v in _versions()]
        self.assertEqual(len(set(labels)), len(labels))
        self.assertTrue(labels[2].endswith("·  EDU"))
        self.assertNotIn("BETA", labels[2])

    def test_an_entry_names_its_edition_with_or_without_the_dict(self):
        edition = gui._version_edition_id
        self.assertEqual(edition({"beta": True}), "preview")
        self.assertEqual(edition({"beta": False}), "release")
        self.assertEqual(edition({"beta": False, "edition": EDUCATION}),
                         "education")

    def test_a_saved_build_is_looked_up_in_the_edition_last_played(self):
        versions = [
            {"tag": "1.26.33.0", "beta": False, "edition": RELEASE},
            {"tag": "1.26.33.0", "beta": False, "edition": EDUCATION},
        ]
        with mock.patch.object(gui, "load_settings", return_value={
                "mc_version": "1.26.33.0", "mc_edition": "education"}):
            self.assertEqual(gui.MainWindow._saved_version_index(versions), 1)
        with mock.patch.object(gui, "load_settings", return_value={
                "mc_version": "1.26.33.0"}):
            self.assertEqual(gui.MainWindow._saved_version_index(versions), 0)


class ToggleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt_app()

    def test_the_education_button_filters_and_picks_education(self):
        saved = {}
        with headless_window() as window, \
                mock.patch.object(gui, "save_settings",
                                  side_effect=saved.update):
            window._on_versions_loaded(_versions())
            window.education_btn.click()

            self.assertTrue(window.education_btn.isChecked())
            self.assertEqual(saved.get("ui_edition"), "education")
            self.assertFalse(saved.get("show_betas"))
            picked = window.selected_version()
            self.assertEqual(picked["edition"]["id"], "education")
            shown = [v for v in window.ui_state["versions"]
                     if window._edition_matches(v)]
            self.assertEqual([v["tag"] for v in shown], ["1.26.33.0"])
            self.assertIn("EDU", window.selected_chip.text())

            window.stable_btn.click()
            self.assertEqual(window.selected_version()["edition"]["id"],
                             "release")
            self.assertTrue(window.stable_btn.isChecked())

    def test_the_saved_toggle_comes_back_on_education(self):
        with headless_window(ui_edition="education") as window:
            self.assertTrue(window.education_btn.isChecked())
        with headless_window(show_betas=True) as window:
            self.assertTrue(window.preview_btn.isChecked())

    def test_play_on_education_does_not_ask_for_the_xbox_sign_in(self):
        for edition, asked in ((EDUCATION, False), (RELEASE, True)):
            with self.subTest(edition=edition["id"]), \
                    headless_window() as window, \
                    mock.patch.object(gui, "LaunchWorker") as worker, \
                    mock.patch.object(window, "selected_version",
                                      return_value={"edition": edition,
                                                    "tag": "1.26.33.0"}), \
                    mock.patch.object(window, "_online_sign_in_settled",
                                      return_value=True) as settled, \
                    mock.patch.object(window, "_start_worker",
                                      return_value=True):
                window.play_btn.click()
                self.assertEqual(settled.called, asked)
                self.assertTrue(worker.called)

    def test_a_build_page_can_point_the_picker_at_an_education_build(self):
        with headless_window() as window:
            window._on_versions_loaded(_versions())
            self.assertTrue(window.select_build("1.26.33.0", False,
                                                edition="education"))
            self.assertEqual(window.selected_version()["edition"]["id"],
                             "education")
            self.assertFalse(window.select_build("1.26.33.0", True))


if __name__ == "__main__":
    unittest.main()
