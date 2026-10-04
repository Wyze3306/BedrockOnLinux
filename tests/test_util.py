"""Tests for bol.util's screen-geometry helper."""
# SPDX-License-Identifier: MIT

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import util
from bol.log import BolError


class HttpJsonNoCredentialTests(unittest.TestCase):
    """http_json fetches public endpoints (GitHub releases, Minecraft feedback)
    from more than one host, so it must never attach a credential, even when
    GITHUB_TOKEN is present in the environment."""

    def _captured_headers(self, url):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured.update({k.lower(): v for k, v in req.header_items()})
            return io.BytesIO(json.dumps({"ok": True}).encode())

        with mock.patch.dict("os.environ", {"GITHUB_TOKEN": "SENTINEL_SECRET"}), \
                mock.patch("urllib.request.urlopen", fake_urlopen):
            util.http_json(url)
        return captured

    def test_no_authorization_for_github(self):
        headers = self._captured_headers("https://api.github.com/repos/x/y/releases")
        self.assertNotIn("authorization", headers)

    def test_no_authorization_for_non_github(self):
        headers = self._captured_headers(
            "https://feedback.minecraft.net/api/v2/help_center/en-us/sections/1/articles.json")
        self.assertNotIn("authorization", headers)


class ScreenWHTests(unittest.TestCase):
    def test_delegates_to_x11_primary_output_size(self):
        with mock.patch("bol.x11.primary_output_size",
                         return_value=("1920", "1080")) as mocked:
            self.assertEqual(util._screen_wh(), ("1920", "1080"))
        mocked.assert_called_once_with(None)

    def test_passes_runner_through(self):
        runner = object()
        with mock.patch("bol.x11.primary_output_size",
                         return_value=None) as mocked:
            self.assertIsNone(util._screen_wh(runner=runner))
        mocked.assert_called_once_with(runner)


class SteamAppIdTests(unittest.TestCase):
    """The identity Steam hands a launch, as gamescope and the overlay
    key it."""

    def test_a_numeric_application_id_is_returned(self):
        self.assertEqual(
            util.steam_app_id({"SteamAppId": "2716672805"}), "2716672805")

    def test_surrounding_whitespace_is_ignored(self):
        self.assertEqual(util.steam_app_id({"SteamAppId": " 480 "}), "480")

    def test_no_steam_launch_has_no_application_id(self):
        for environ in ({}, {"SteamAppId": ""}, {"SteamAppId": "   "}):
            with self.subTest(environ=environ):
                self.assertIsNone(util.steam_app_id(environ))

    def test_a_zero_or_non_numeric_value_is_not_an_application_id(self):
        # "default" is what UMU writes when it is given no GAMEID (#199), and
        # a zero is the placeholder Steam and UMU both use for "none".
        for value in ("default", "0", "00", "umu-480", "12.5", "-3", "٣"):
            with self.subTest(value=value):
                self.assertIsNone(util.steam_app_id({"SteamAppId": value}))

    def test_the_64_bit_game_id_is_never_used_as_the_application_id(self):
        # A non-Steam shortcut carries a different, 64-bit identifier there.
        self.assertIsNone(
            util.steam_app_id({"SteamGameId": "11668020851441139712"}))


class SudoTests(unittest.TestCase):
    """Elevated from an account, the launcher would use root's data (#299)."""

    def test_the_account_behind_sudo_or_doas_is_named(self):
        self.assertEqual(util.sudo_invoker({"SUDO_USER": "bilal"}, euid=0),
                         "bilal")
        self.assertEqual(util.sudo_invoker({"DOAS_USER": "maxim"}, euid=0),
                         "maxim")

    def test_root_on_its_own_is_not_elevation(self):
        # A container that runs everything as root has no one else to be.
        self.assertIsNone(util.sudo_invoker({}, euid=0))
        self.assertIsNone(util.sudo_invoker({"SUDO_USER": "root"}, euid=0))

    def test_an_ordinary_account_is_not_elevated(self):
        self.assertIsNone(util.sudo_invoker({"SUDO_USER": "bilal"}, euid=1000))

    def test_sudo_is_refused_with_the_account_to_use(self):
        with self.assertRaises(BolError) as raised, \
                contextlib.redirect_stdout(io.StringIO()):
            util.refuse_sudo({"SUDO_USER": "bilal"}, euid=0)
        message = str(raised.exception)
        self.assertIn("as bilal, without sudo", message)
        self.assertIn("BOL_ALLOW_ROOT=1", message)

    def test_the_refusal_can_be_overridden(self):
        util.refuse_sudo({"SUDO_USER": "bilal", "BOL_ALLOW_ROOT": "1"},
                         euid=0)
        util.refuse_sudo({"SUDO_USER": "bilal"}, euid=1000)


class LauncherCommandTests(unittest.TestCase):
    """Every packaging must print an invocation the user can actually run."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.missing_flatpak_info = self.root / "no-flatpak-info"

    def command(self, *arguments, **kwargs):
        kwargs.setdefault("info_path", self.missing_flatpak_info)
        kwargs.setdefault("which", lambda _name: None)
        kwargs.setdefault("argv", [""])
        return util.launcher_command(*arguments, **kwargs)

    def test_flatpak_uses_flatpak_run_with_the_sandbox_identity(self):
        self.assertEqual(
            self.command(
                "doctor", "--acknowledge-gpu-crash",
                environ={"FLATPAK_ID": "io.github.wyze3306.BedrockOnLinux"},
            ),
            "flatpak run io.github.wyze3306.BedrockOnLinux doctor "
            "--acknowledge-gpu-crash",
        )

    def test_flatpak_info_file_alone_selects_the_published_app_id(self):
        info = self.root / "flatpak-info"
        info.write_text("[Application]\n", encoding="utf-8")
        self.assertEqual(
            self.command("doctor", environ={}, info_path=info),
            "flatpak run io.github.wyze3306.BedrockOnLinux doctor",
        )

    def test_appimage_uses_its_persistent_file_not_the_temporary_mount(self):
        appimage = self.root / "BedrockOnLinux-2.1.1-x86_64.AppImage"
        appimage.write_text("ELF", encoding="utf-8")
        self.assertEqual(
            self.command(
                "doctor", "--acknowledge-gpu-crash",
                environ={"APPIMAGE": str(appimage)},
                argv=["/tmp/.mount_Bedroc123/AppRun"],
            ),
            f"{appimage} doctor --acknowledge-gpu-crash",
        )

    def test_installed_package_uses_the_program_name_on_path(self):
        self.assertEqual(
            self.command(
                "doctor", environ={},
                which=lambda name: f"/usr/bin/{name}",
            ),
            "bedrock-on-linux doctor",
        )

    def test_portable_zipapp_uses_its_own_path(self):
        zipapp = self.root / "bedrock-on-linux-2.1.1.pyz"
        zipapp.write_text("PK", encoding="utf-8")
        self.assertEqual(
            self.command("doctor", environ={}, argv=[str(zipapp)]),
            f"{zipapp} doctor",
        )

    def test_source_checkout_falls_back_to_the_program_name(self):
        module = self.root / "__main__.py"
        module.write_text("", encoding="utf-8")
        self.assertEqual(
            self.command("doctor", environ={}, argv=[str(module)]),
            "bedrock-on-linux doctor",
        )

    def test_paths_with_spaces_stay_a_single_argument(self):
        appimage = self.root / "My Games" / "BedrockOnLinux.AppImage"
        appimage.parent.mkdir()
        appimage.write_text("ELF", encoding="utf-8")
        self.assertEqual(
            self.command("doctor", environ={"APPIMAGE": str(appimage)}),
            f"'{appimage}' doctor",
        )


if __name__ == "__main__":
    unittest.main()
