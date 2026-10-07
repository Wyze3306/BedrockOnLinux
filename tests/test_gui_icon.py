"""Where the launcher looks for its own icon.

Four packaging layouts put data/icon.png in four different places, and one
of them -- the Flatpak -- has no copy next to bol/ at all: the manifest
installs only the themed icon under /app/share/icons, under the app-id name.
A candidate list that misses that leaves the Flathub build with no window
icon, no title-bar icon and a blank hero screen, on the one artifact whose
whole point is looking like a normal desktop app.
"""
# SPDX-License-Identifier: MIT

import unittest
from pathlib import Path

from bol.gui import icon_candidates


class IconCandidateTests(unittest.TestCase):
    def test_the_flatpak_themed_icon_is_a_candidate(self):
        self.assertIn(
            Path("/app/share/icons/hicolor/256x256/apps/"
                 "io.github.wyze3306.BedrockOnLinux.png"),
            icon_candidates())

    def test_the_system_icon_theme_is_a_candidate(self):
        self.assertIn(
            Path("/usr/share/icons/hicolor/256x256/apps/"
                 "bedrock-on-linux.png"),
            icon_candidates())

    def test_the_deb_and_appimage_layout_is_found_beside_the_package(self):
        # .deb/.rpm: /usr/lib/bedrock-on-linux/{bol,data}
        # AppImage:  usr/bin/{bol,data}
        candidates = icon_candidates("/usr/lib/bedrock-on-linux/bol/gui.py")
        self.assertIn(Path("/usr/lib/bedrock-on-linux/data/icon.png"),
                      candidates)

    def test_a_wheel_install_finds_the_icon_under_its_own_prefix(self):
        # pip into a venv or ~/.local, a /gnu/store item, a distribution's
        # build of the wheel: data-files land in <prefix>/share (#306).
        for prefix, site in (("/no-such-venv", "python3.14/site-packages"),
                             ("/no-such-usr", "python3/dist-packages")):
            with self.subTest(site=site):
                candidates = icon_candidates(
                    f"{prefix}/lib/{site}/bol/gui.py")
                self.assertIn(
                    Path(prefix) / "share/icons/hicolor/256x256/apps/"
                    "bedrock-on-linux.png", candidates)

    def test_a_checkout_looks_under_no_prefix_of_its_own(self):
        root = Path(__file__).resolve().parents[1]
        themed = [path for path in icon_candidates(root / "bol/gui.py")
                  if "icons/hicolor" in str(path)]
        self.assertEqual(
            themed,
            [Path("/app/share/icons/hicolor/256x256/apps/"
                  "io.github.wyze3306.BedrockOnLinux.png"),
             Path("/usr/share/icons/hicolor/256x256/apps/"
                  "bedrock-on-linux.png")])

    def test_the_repository_checkout_is_found(self):
        root = Path(__file__).resolve().parents[1]
        self.assertIn(root / "data/icon.png",
                      icon_candidates(root / "bol/gui.py"))

    def test_the_shipped_repository_icon_actually_exists(self):
        # Guards the checkout path itself: every other candidate is absolute
        # and only real on an installed system, so this is the one entry the
        # suite can prove still resolves.
        root = Path(__file__).resolve().parents[1]
        self.assertTrue((root / "data/icon.png").is_file())

    def test_the_order_prefers_the_packaged_copy_over_the_icon_theme(self):
        # The themed copies are scaled for the desktop; the packaged one is
        # the original the hero screen scales itself.
        candidates = list(icon_candidates())
        packaged = candidates.index(
            Path(__file__).resolve().parents[1] / "data/icon.png")
        themed = min(
            index for index, path in enumerate(candidates)
            if "icons/hicolor" in str(path))
        self.assertLess(packaged, themed)


if __name__ == "__main__":
    unittest.main()
