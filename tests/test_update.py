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


class _Run:
    """subprocess.run for the package database: owner by command."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, argv, **_kwargs):
        self.calls.append(argv)
        code, out = self.answers.get(argv[0], (1, ""))
        return mock.Mock(returncode=code, stdout=out)


class PackageFormatTests(unittest.TestCase):
    """Which installs the launcher may update through a package manager."""

    def _format(self, answers):
        with mock.patch.object(update, "is_flatpak", return_value=False):
            return update.package_format(runner=_Run(answers))

    def test_the_release_deb_is_recognised_by_dpkg(self):
        self.assertEqual(self._format({"dpkg-query": (
            0, "bedrock-on-linux: /usr/lib/bedrock-on-linux/bol/__init__.py\n")
        }), "deb")

    def test_the_release_rpm_is_recognised_by_rpm(self):
        self.assertEqual(self._format({"rpm": (0, "bedrock-on-linux\n")}),
                         "rpm")

    def test_a_package_built_by_someone_else_is_left_to_them(self):
        self.assertIsNone(self._format({"dpkg-query": (
            0, "python3-bedrock: /usr/lib/python3/dist-packages/bol/x.py\n")}))
        self.assertIsNone(self._format({}))

    def test_never_inside_a_flatpak(self):
        with mock.patch.object(update, "is_flatpak", return_value=True):
            self.assertIsNone(update.package_format(runner=_Run(
                {"dpkg-query": (0, "bedrock-on-linux: /x\n")})))


_DEB = b"!<arch>\n a release package"
_SUMS = ("%s  bedrock-on-linux_2.2.9_amd64.deb\n"
         "0000000000000000000000000000000000000000000000000000000000000000"
         "  BedrockOnLinux-2.2.9-x86_64.flatpak\n")


class PackageUpdateTests(unittest.TestCase):
    """A .deb or .rpm install updates itself through its package manager
    (#294), with the file the release's checksum list names."""

    def setUp(self):
        import hashlib
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.cache = Path(holder.name)
        self.release = dict(_RELEASE, assets=_RELEASE["assets"] + [
            {"name": "BedrockOnLinux-2.2.9-SHA256SUMS",
             "browser_download_url": "https://example.invalid/sums"}])
        self.sums = _SUMS % hashlib.sha256(_DEB).hexdigest()

    def _update(self, kind="deb", served=_DEB, installed=0, tools=None):
        tools = {"pkexec", "apt-get"} if tools is None else tools

        def fetch(url, dest, label=None, progress=None):
            dest.parent.mkdir(parents=True, exist_ok=True)
            if url.endswith("sums"):
                dest.write_text(self.sums)
            else:
                dest.write_bytes(served)

        with mock.patch.object(update, "CACHE", self.cache), \
                mock.patch.object(update, "update_kind", return_value=kind), \
                mock.patch.object(update, "download", side_effect=fetch), \
                mock.patch.object(update.shutil, "which",
                                  side_effect=lambda name: (
                                      f"/usr/bin/{name}" if name in tools
                                      else None)), \
                mock.patch.object(update.subprocess, "run",
                                  return_value=mock.Mock(
                                      returncode=installed, stdout="",
                                      stderr="E: broken")) as run:
            result = update.self_update(self.release)
        return result, run

    def test_the_checked_package_is_installed_with_apt(self):
        (state, msg), run = self._update()
        self.assertEqual(state, "ok")
        self.assertIn("restart", msg)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], ["pkexec", "apt-get", "install", "-y"])
        self.assertEqual(Path(argv[4]).name, "bedrock-on-linux_2.2.9_amd64.deb")
        # Installed, so not kept; and the checksum list is not kept either.
        self.assertEqual(list((self.cache / "updates").iterdir()), [])

    def test_a_package_that_does_not_match_its_checksum_is_not_installed(self):
        (state, msg), run = self._update(served=b"something else")
        self.assertEqual(state, "error")
        self.assertIn("checksum", msg)
        run.assert_not_called()
        self.assertEqual(list((self.cache / "updates").iterdir()), [])

    def test_a_cancelled_password_prompt_says_how_to_install_it_by_hand(self):
        (state, msg), _ = self._update(installed=126)
        self.assertEqual(state, "error")
        self.assertIn("cancelled", msg)
        self.assertIn("sudo apt install", msg)
        self.assertTrue((self.cache / "updates" /
                         "bedrock-on-linux_2.2.9_amd64.deb").is_file())

    def test_a_package_manager_failure_is_reported_with_its_words(self):
        (state, msg), _ = self._update(installed=100)
        self.assertEqual(state, "error")
        self.assertIn("E: broken", msg)

    def test_without_pkexec_the_downloaded_package_is_pointed_at(self):
        (state, msg), run = self._update(tools={"apt-get"})
        self.assertEqual(state, "system")
        self.assertIn("sudo apt install", msg)
        run.assert_not_called()

    def test_a_release_without_the_package_sends_to_the_page(self):
        self.release["assets"] = [a for a in self.release["assets"]
                                  if not a["name"].endswith(".deb")]
        (state, msg), _ = self._update()
        self.assertEqual(state, "error")
        self.assertIn(self.release["url"], msg)

    def test_an_rpm_goes_to_dnf(self):
        self.release["assets"].append(
            {"name": "bedrock-on-linux-2.2.9-1.x86_64.rpm",
             "browser_download_url": "https://example.invalid/rpm"})
        self.sums += "%s  bedrock-on-linux-2.2.9-1.x86_64.rpm\n" % (
            __import__("hashlib").sha256(_DEB).hexdigest())
        (state, _msg), run = self._update(kind="rpm",
                                          tools={"pkexec", "dnf"})
        self.assertEqual(state, "ok")
        self.assertEqual(run.call_args.args[0][:4],
                         ["pkexec", "dnf", "install", "-y"])


if __name__ == "__main__":
    unittest.main()
