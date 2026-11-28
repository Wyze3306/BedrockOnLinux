"""The changelog opens web links and nothing else (#379).

Both changelog tabs show text off the network: feedback.minecraft.net
articles inserted as HTML, and GitHub release notes run through a small
Markdown converter. A QTextBrowser told to open external links hands every
click to QDesktopServices unchecked, so a file:, smb:, ssh: or steam: link
in that text was one click away from the desktop handler for it.
"""
# SPDX-License-Identifier: MIT

import unittest
from unittest import mock

from PySide6.QtCore import QUrl

from bol import gui
from tests.guiharness import headless_window, qt_app


def setUpModule():
    qt_app()


class WebLinkTests(unittest.TestCase):
    def test_only_http_and_https_are_web_links(self):
        for url in ("https://feedback.minecraft.net/hc/articles/1",
                    "http://example.com/", "HTTPS://github.com/x"):
            with self.subTest(url=url):
                self.assertTrue(gui._web_link(url))
                self.assertTrue(gui._web_link(QUrl(url)))
        for url in ("file:///etc/passwd", "smb://host/share", "ssh://host",
                    "steam://run/1", "javascript:alert(1)", "https:///x",
                    "/relative", "", None):
            with self.subTest(url=url):
                self.assertFalse(gui._web_link(url))

    def test_an_href_is_quoted(self):
        self.assertEqual(gui._href('https://a.example/"><b>x'),
                         "https://a.example/&quot;&gt;&lt;b&gt;x")
        self.assertEqual(gui._href("file:///etc/passwd"), "")
        self.assertEqual(gui._href(None), "")

    def test_release_notes_link_web_urls_only(self):
        with headless_window() as window:
            out = window._md_to_html(
                "[notes](https://github.com/a/b) [home](file:///home)")

        self.assertIn('<a href="https://github.com/a/b">notes</a>', out)
        self.assertNotIn('href="file:', out)
        self.assertIn(" home", out)

    def test_a_release_with_a_hostile_url_keeps_a_plain_title(self):
        with headless_window() as window:
            out = window._render_launcher_changelog_html([
                {"tag_name": "v1", "html_url": "file:///etc/passwd"},
                {"tag_name": "v2", "html_url": 'https://x.example/"onclick="'},
            ])

        self.assertNotIn("file:", out)
        self.assertIn('href="https://x.example/&quot;onclick=&quot;"', out)

    def test_only_launcher_versions_are_listed(self):
        with headless_window() as window:
            out = window._render_launcher_changelog_html([
                {"tag_name": "engine-wow64-archs-native28"},
                {"tag_name": "nightly"},
                {"tag_name": "files"},
                {"tag_name": "vkd3d-3.0.1-nv-dgc-32d230b6254d"},
                {"tag_name": "v3.0.0"},
            ])
            only_files = window._render_launcher_changelog_html(
                [{"tag_name": "files"}])

        self.assertIn("v3.0.0", out)
        for tag in ("engine-", "nightly", "files", "vkd3d-"):
            self.assertNotIn(tag, out)
        self.assertIn("No releases found.", only_files)


class ChangelogClickTests(unittest.TestCase):
    def test_the_views_do_not_open_links_themselves(self):
        with headless_window() as window:
            for view in (window.game_changelog_view,
                         window.launcher_changelog_view):
                with self.subTest(view=view):
                    self.assertFalse(view.openExternalLinks())
                    # Otherwise QTextBrowser follows the link with
                    # setSource() and shows a file: URL's contents.
                    self.assertFalse(view.openLinks())

    def test_a_click_opens_web_links_and_refuses_the_rest(self):
        with headless_window() as window, \
                mock.patch.object(gui.QDesktopServices, "openUrl",
                                  return_value=True) as open_url:
            view = window.game_changelog_view
            view.anchorClicked.emit(QUrl("file:///etc/passwd"))
            view.anchorClicked.emit(QUrl("steam://run/1"))
            self.assertEqual(open_url.call_count, 0)

            view.anchorClicked.emit(QUrl("https://feedback.minecraft.net/"))
            self.assertEqual(open_url.call_count, 1)
            self.assertEqual(open_url.call_args[0][0].toString(),
                             "https://feedback.minecraft.net/")
            self.assertEqual(view.source(), QUrl())


if __name__ == "__main__":
    unittest.main()
