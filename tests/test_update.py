"""What the launcher tells a Flatpak user to run for an update.

A bundle has to be installed with the same flag as the copy it replaces.
Without --user, flatpak installs a second, system-wide copy beside the
per-user one the README sets up, and every launch keeps starting the
per-user one, still at the old version (#315).
"""
# SPDX-License-Identifier: MIT

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import update

_APP = "io.github.wyze3306.BedrockOnLinux"

_RELEASE = {
    "version": "2.2.9",
    "url": "https://github.com/Wyze3306/BedrockOnLinux/releases/tag/v2.2.9",
    "assets": [
        {"name": "bedrock-on-linux_2.2.9_amd64.deb",
         "browser_download_url": "https://example.invalid/deb"},
        {"name": "BedrockOnLinux-2.2.9-x86_64.flatpak",
         "browser_download_url": "https://example.invalid/flatpak"},
    ],
}


def _flatpak_info(root, app_path):
    """A sandbox's /.flatpak-info, as flatpak run writes it."""
    path = Path(root) / "flatpak-info"
    path.write_text(
        "[Application]\n"
        "name=%s\n"
        "runtime=runtime/org.gnome.Platform/x86_64/49\n"
        "\n"
        "[Instance]\n"
        "instance-id=655197581\n"
        "instance-path=/home/player/.var/app/%s\n"
        "app-path=%s\n"
        "branch=master\n"
        "arch=x86_64\n" % (_APP, _APP, app_path))
    return path


class FlatpakInstallationTests(unittest.TestCase):
    def _installation(self, app_path):
        with tempfile.TemporaryDirectory() as td:
            return update.flatpak_installation(_flatpak_info(td, app_path))

    def test_a_per_user_install_is_user(self):
        self.assertEqual(self._installation(
            "/home/player/.local/share/flatpak/app/%s/x86_64/master/"
            "2586/files" % _APP), "user")

    def test_a_system_wide_install_is_system(self):
        self.assertEqual(self._installation(
            "/var/lib/flatpak/app/%s/x86_64/master/2586/files" % _APP),
            "system")

    def test_a_custom_installation_is_not_guessed(self):
        self.assertIsNone(self._installation(
            "/run/media/player/games/flatpak/app/%s/x86_64/master/2586/files"
            % _APP))

    def test_outside_flatpak_there_is_no_installation(self):
        self.assertIsNone(update.flatpak_installation("/nonexistent/info"))


class FlatpakUpdateMessageTests(unittest.TestCase):
    def _message(self, installation):
        with mock.patch.object(update, "update_kind", return_value="system"), \
                mock.patch.object(update, "is_flatpak", return_value=True), \
                mock.patch.object(update, "flatpak_installation",
                                  return_value=installation):
            return update.self_update(_RELEASE)

    def test_a_per_user_install_is_told_to_keep_user(self):
        state, msg = self._message("user")
        self.assertEqual(state, "system")
        self.assertIn(
            "flatpak install --user ./BedrockOnLinux-2.2.9-x86_64.flatpak",
            msg)
        self.assertIn(_RELEASE["url"], msg)

    def test_a_system_wide_install_is_told_to_stay_system_wide(self):
        _state, msg = self._message("system")
        self.assertIn(
            "flatpak install --system ./BedrockOnLinux-2.2.9-x86_64.flatpak",
            msg)

    def test_an_unknown_installation_gets_no_flag_it_might_not_have(self):
        _state, msg = self._message(None)
        self.assertIn(
            "flatpak install ./BedrockOnLinux-2.2.9-x86_64.flatpak", msg)

    def test_a_distribution_package_is_still_sent_to_its_package_manager(self):
        with mock.patch.object(update, "update_kind", return_value="system"), \
                mock.patch.object(update, "is_flatpak", return_value=False):
            state, msg = update.self_update(_RELEASE)
        self.assertEqual(state, "system")
        self.assertIn("package manager", msg)
        self.assertNotIn("flatpak", msg)


if __name__ == "__main__":
    unittest.main()
