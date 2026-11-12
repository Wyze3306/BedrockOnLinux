"""The other Minecraft titles of the Microsoft Store: Dungeons, Legends.

Bedrock is what the launcher was written for, and everything it does to the
game -- its options.txt, HBUI, LeviLamina, the Editor, world backups, the
PlayFab pre-auth -- is Bedrock's. These tests pin down that another title of
the family installs and starts with none of that, and that Bedrock itself is
unchanged.
"""
# SPDX-License-Identifier: MIT

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import cli, gamesetup, games, launch, prefix, xodus
from bol.log import BolError
from tests.test_launch import ReadyLaunchHarness


DUNGEONS = xodus.edition("dungeons")
LEGENDS = xodus.edition("legends")

_GAME_CONFIG = """<?xml version="1.0" encoding="utf-8"?>
<Game configVersion="1">
  <Identity Name="Microsoft.Lovika" Publisher="CN=Microsoft"
            Version="1.17.0.0"/>
  <ExecutableList>
    <Executable TargetDeviceFamily="PC" Name="{exe}" Id="Game"/>
  </ExecutableList>
</Game>
"""
_MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<Package><Identity Name="Microsoft.Lovika" Version="1.17.0.0"/>
<Applications><Application Id="Game" Executable="{exe}"/></Applications>
</Package>
"""


def _write_dungeons(root, exe="Dungeons.exe", encrypted=True, package=True,
                    config=True):
    """A Minecraft Dungeons build the way `xodus-cli streaming` leaves one."""
    root.mkdir(parents=True, exist_ok=True)
    target = root / exe.replace("\\", "/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x9c\x1f ciphertext" if encrypted else b"MZ")
    (root / "appxmanifest.xml").write_text(_MANIFEST.format(exe=exe),
                                           encoding="utf-8")
    if config:
        (root / "MicrosoftGame.config").write_text(
            _GAME_CONFIG.format(exe=exe), encoding="utf-8")
    if package:
        (root / xodus.PACKAGE_CACHE).write_bytes(b"msixvc")
    return root


class EditionModelTests(unittest.TestCase):
    def test_the_family_is_listed_with_its_store_identity(self):
        self.assertEqual(DUNGEONS["product"], "9P8MK4NC0LJB")
        self.assertEqual(DUNGEONS["package_family"],
                         "Microsoft.Lovika_8wekyb3d8bbwe")
        self.assertEqual(LEGENDS["product"], "9N98Z825TNFW")
        ids = [entry["id"] for entry in games.list_editions(True)]
        self.assertEqual(ids[:2], ["release", "preview"])
        self.assertIn("dungeons", ids)
        self.assertIn("legends", ids)

    def test_only_bedrock_editions_are_bedrock(self):
        self.assertTrue(games.is_bedrock(xodus.edition("release")))
        self.assertTrue(games.is_bedrock(xodus.edition("preview")))
        self.assertFalse(games.is_bedrock(DUNGEONS))
        self.assertFalse(games.is_bedrock(LEGENDS))

    def test_an_entry_without_the_new_keys_is_still_bedrock(self):
        # An imported build, a pre-Store one, or an edition added without them.
        old = {"id": "release", "product": "9NBLGGH2JHXJ", "beta": False}
        self.assertTrue(games.is_bedrock(None))
        self.assertTrue(games.is_bedrock(old))
        self.assertEqual(games.edition_exe(None), "Minecraft.Windows.exe")
        self.assertEqual(games.edition_exe(old), "Minecraft.Windows.exe")
        self.assertIsNone(games.edition_exe(DUNGEONS))

    def test_a_store_title_has_one_build_and_no_index(self):
        with mock.patch.object(xodus, "_fetch_with_fallback") as index:
            catalogue = xodus.version_catalogue("dungeons")
        index.assert_not_called()
        self.assertEqual(catalogue, [{"version": xodus.STORE_BUILD,
                                      "urls": ["9P8MK4NC0LJB"]}])


class DeclaredExecutableTests(unittest.TestCase):
    def test_microsoftgame_config_names_the_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_dungeons(Path(tmp))
            self.assertEqual(xodus.declared_executable(root), "Dungeons.exe")

    def test_a_nested_executable_keeps_its_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = "Dungeons\\Binaries\\Win64\\Dungeons-Win64-Shipping.exe"
            root = _write_dungeons(Path(tmp), exe=exe)
            self.assertEqual(xodus.declared_executable(root),
                             "Dungeons/Binaries/Win64/"
                             "Dungeons-Win64-Shipping.exe")
            self.assertEqual(xodus.game_root(root, None), root)
            self.assertEqual(
                games.game_executable(root, DUNGEONS),
                root / "Dungeons/Binaries/Win64/Dungeons-Win64-Shipping.exe")

    def test_the_appx_manifest_is_the_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_dungeons(Path(tmp), config=False)
            self.assertEqual(xodus.declared_executable(root), "Dungeons.exe")

    def test_a_path_out_of_the_package_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "MicrosoftGame.config").write_text(
                _GAME_CONFIG.format(exe="..\\..\\evil.exe"), encoding="utf-8")
            self.assertIsNone(xodus.declared_executable(root))

    def test_a_build_without_its_declared_executable_is_not_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_dungeons(Path(tmp))
            (root / "Dungeons.exe").unlink()
            self.assertIsNone(xodus.game_root(root, None))

    def test_a_dungeons_build_is_not_a_bedrock_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_dungeons(Path(tmp))
            self.assertIsNone(xodus.game_root(root))
            self.assertEqual(xodus.game_root(root, None), root)

    def test_a_lost_package_is_noticed_for_any_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_dungeons(Path(tmp), package=False)
            self.assertTrue(xodus.lost_package_cache(root, None))
            # And Bedrock's question about the same folder is unchanged:
            # there is no Minecraft.Windows.exe in it to have lost anything.
            self.assertFalse(xodus.lost_package_cache(root))

    def test_the_package_version_is_read_as_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_dungeons(Path(tmp))
            self.assertEqual(games.build_version(root), "1.17.0.0")


class StoreTitleInstallTests(unittest.TestCase):
    def setUp(self):
        self.settings = {}
        for target, kwargs in (
                ("load_settings",
                 {"side_effect": lambda: dict(self.settings)}),
                ("save_settings", {"side_effect": self.settings.update})):
            patcher = mock.patch.object(games, target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_installs_the_product_into_its_one_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            calls = []

            def fake_install(sources, dest, progress=None, **kwargs):
                calls.append((list(sources), Path(dest), kwargs))
                _write_dungeons(Path(dest))

            with mock.patch.object(games, "GAMES", base), \
                    mock.patch.object(games.xodus, "install",
                                      side_effect=fake_install):
                root = games.install_game(DUNGEONS)

        self.assertEqual(root, base / "dungeons" / "current")
        sources, dest, kwargs = calls[0]
        self.assertEqual(sources, ["9P8MK4NC0LJB"])
        self.assertEqual(dest, base / "dungeons" / "current")
        self.assertEqual(kwargs, {"exe": None, "title": "Minecraft Dungeons"})

    def test_an_installed_build_is_not_downloaded_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_dungeons(base / "dungeons" / "current")
            with mock.patch.object(games, "GAMES", base), \
                    mock.patch.object(games.xodus, "install") as install:
                root = games.install_game(DUNGEONS, xodus.STORE_BUILD)
        install.assert_not_called()
        self.assertEqual(root, base / "dungeons" / "current")

    def test_a_download_with_no_executable_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)

            def truncated(sources, dest, progress=None, **kwargs):
                Path(dest).mkdir(parents=True, exist_ok=True)
                (Path(dest) / "appxmanifest.xml").write_text(
                    _MANIFEST.format(exe="Dungeons.exe"), encoding="utf-8")

            with mock.patch.object(games, "GAMES", base), \
                    mock.patch.object(games.xodus, "install",
                                      side_effect=truncated), \
                    self.assertRaises(BolError):
                games.install_game(DUNGEONS)

    def test_no_bedrock_copy_stands_in_for_a_failed_dungeons_download(self):
        # The pre-Store fallback keeps a Bedrock player playing; it must not
        # answer "Dungeons could not be downloaded" by starting Minecraft.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            legacy = base / "1.26.42.1"
            legacy.mkdir(parents=True)
            (legacy / "Minecraft.Windows.exe").write_bytes(b"MZ")
            (legacy / "AppxManifest.xml").write_text(
                '<Package><Identity Version="1.26.4201.0"/></Package>',
                encoding="utf-8")
            self.settings["game_dir"] = str(legacy)
            with mock.patch.object(games, "GAMES", base), \
                    mock.patch.object(games.xodus, "install",
                                      side_effect=BolError("no network")), \
                    self.assertRaises(BolError):
                games.install_game(DUNGEONS)

    def test_the_picker_lists_the_store_build_with_its_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with mock.patch.object(games, "GAMES", base):
                before = games.list_versions("dungeons")
                _write_dungeons(base / "dungeons" / "current")
                after = games.list_versions("dungeons")
        self.assertEqual([entry["version"] for entry in before], ["current"])
        self.assertFalse(before[0]["installed"])
        self.assertNotIn("build", before[0])
        self.assertTrue(after[0]["installed"])
        self.assertEqual(after[0]["build"], "1.17.0.0")

    def test_selecting_the_build_records_its_edition(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = _write_dungeons(base / "games" / "dungeons" / "current")
            with mock.patch.object(games, "GAMES", base / "games"), \
                    mock.patch.object(games, "CONTENT", base / "content"):
                games.use_game_dir(root)
                self.assertEqual(games.selected_edition(self.settings),
                                 DUNGEONS)
            self.assertEqual((base / "content").resolve(), root.resolve())
        self.assertEqual(self.settings["mc_edition"], "dungeons")
        self.assertEqual(self.settings["mc_version"], "current")

    def test_installed_builds_show_the_real_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_dungeons(base / "dungeons" / "current")
            with mock.patch.object(games, "GAMES", base):
                builds = games.installed_builds(with_size=False)
        self.assertEqual(len(builds), 1)
        build = builds[0]
        self.assertEqual(build["edition"], "dungeons")
        self.assertEqual(build["title"], "dungeons")
        self.assertEqual(build["version"], "1.17.0.0")
        self.assertTrue(build["playable"])
        self.assertTrue(build["managed"])

    def test_a_dungeons_build_can_be_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = _write_dungeons(base / "dungeons" / "current")
            with mock.patch.object(games, "GAMES", base), \
                    mock.patch("bol.prefix._mc_running", return_value=False):
                games.remove_build(root)
            self.assertFalse(root.exists())
            self.assertTrue((base / "dungeons").is_dir())


class NotOwnedMessageTests(unittest.TestCase):
    def test_the_title_is_named(self):
        with self.assertRaisesRegex(xodus.NotOwned, "own Minecraft Dungeons"):
            xodus._raise_unretryable(
                "not entitled to this content: Users do not possess any "
                "satisfying entitlements", title="Minecraft Dungeons")

    def test_bedrock_keeps_its_sentence(self):
        with self.assertRaisesRegex(xodus.NotOwned,
                                    "own this edition of Minecraft"):
            xodus._raise_unretryable("not entitled to this content")

    def test_install_checks_the_title_s_own_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "current"

            def streamed(cmd, progress=None, record=None):
                _write_dungeons(dest)
                return 0, []

            with mock.patch.object(xodus, "ensure_cli",
                                   return_value=Path("/bin/true")), \
                    mock.patch.object(xodus, "signed_in", return_value=True), \
                    mock.patch.object(xodus, "_free_space", return_value=None), \
                    mock.patch.object(xodus, "_open_download_log",
                                      return_value=None), \
                    mock.patch.object(xodus, "_run_streaming",
                                      side_effect=streamed) as run:
                result = xodus.install("9P8MK4NC0LJB", dest, exe=None,
                                       title="Minecraft Dungeons")
        self.assertEqual(result, dest)
        self.assertEqual(run.call_args[0][0][1:],
                         ["streaming", "9P8MK4NC0LJB", str(dest)])

    def test_a_refused_download_names_the_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "current"
            with mock.patch.object(xodus, "ensure_cli",
                                   return_value=Path("/bin/true")), \
                    mock.patch.object(xodus, "signed_in", return_value=True), \
                    mock.patch.object(xodus, "_free_space", return_value=None), \
                    mock.patch.object(xodus, "_open_download_log",
                                      return_value=None), \
                    mock.patch.object(
                        xodus, "_run_streaming",
                        return_value=(1, ["something went wrong"])), \
                    self.assertRaisesRegex(
                        xodus.XodusError,
                        "^The Minecraft Dungeons download failed"):
                xodus.install("9P8MK4NC0LJB", dest, exe=None,
                              title="Minecraft Dungeons")


class SetupTests(unittest.TestCase):
    def _setup(self, game_dir):
        settings = {"game_dir": str(game_dir), "proton_source": "winegdk"}
        calls = {}
        names = ("install_gdk_xbox_dlls", "fix_curl_ssl", "hide_signin_button",
                 "install_ca_bundle", "install_gameinput")
        with mock.patch.object(gamesetup, "mkdirs"), \
                mock.patch.object(gamesetup, "prune_legacy_game_archives"), \
                mock.patch.object(gamesetup, "load_settings",
                                  return_value=settings), \
                mock.patch.object(gamesetup, "ensure_login_deps"), \
                mock.patch.object(gamesetup, "use_game_dir"), \
                mock.patch.object(gamesetup, "ensure_winegdk"), \
                mock.patch.object(gamesetup, "ensure_umu"), \
                mock.patch.object(gamesetup, "boot_prefix",
                                  return_value=True), \
                mock.patch.object(gamesetup, "active_prefix"), \
                mock.patch.object(gamesetup.hostarch, "problem",
                                  return_value=None), \
                mock.patch.object(gamesetup, "ok"):
            mocks = [mock.patch.object(gamesetup, name) for name in names]
            for name, patcher in zip(names, mocks):
                calls[name] = patcher.start()
            try:
                gamesetup._do_setup()
            finally:
                for patcher in mocks:
                    patcher.stop()
        return calls

    def test_dungeons_keeps_the_dlls_it_shipped_with(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = _write_dungeons(base / "dungeons" / "current")
            with mock.patch.object(games, "GAMES", base):
                calls = self._setup(root)
        calls["install_gdk_xbox_dlls"].assert_not_called()
        calls["fix_curl_ssl"].assert_not_called()
        calls["hide_signin_button"].assert_not_called()
        calls["install_ca_bundle"].assert_called_once()
        calls["install_gameinput"].assert_called_once()

    def test_bedrock_still_gets_its_fixups(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "release" / "1.26.63.1"
            root.mkdir(parents=True)
            (root / "Minecraft.Windows.exe").write_bytes(b"MZ")
            (root / "AppxManifest.xml").write_text(
                '<Package><Identity Version="1.26.6301.0"/></Package>',
                encoding="utf-8")
            with mock.patch.object(games, "GAMES", base):
                calls = self._setup(root)
        calls["install_gdk_xbox_dlls"].assert_called_once()
        calls["fix_curl_ssl"].assert_called_once()
        calls["hide_signin_button"].assert_called_once()
        calls["install_ca_bundle"].assert_not_called()


class SignedOutWordingTests(unittest.TestCase):
    """Realms and servers are Bedrock's; Dungeons only misses Xbox Live."""

    def _diagnose(self, settings):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(gamesetup, "LOGS", Path(td)), \
                mock.patch.object(gamesetup, "msa_signed_in",
                                  return_value=False), \
                mock.patch.object(gamesetup, "load_settings",
                                  return_value=settings), \
                mock.patch.object(gamesetup, "warn"), \
                mock.patch.object(gamesetup, "info"):
            return gamesetup.diagnose()

    def test_dungeons_is_told_about_xbox_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = _write_dungeons(base / "dungeons" / "current")
            with mock.patch.object(games, "GAMES", base):
                hits = self._diagnose({"game_dir": str(root)})
        hit = next(h for h in hits if h.startswith("No Microsoft account"))
        self.assertIn("Minecraft Dungeons cannot sign in to Xbox Live", hit)
        self.assertNotIn("Realms", hit)

    def test_bedrock_keeps_its_offline_mode_sentence(self):
        hits = self._diagnose({})
        hit = next(h for h in hits if h.startswith("No Microsoft account"))
        self.assertIn("Realms", hit)


class CliLabelTests(unittest.TestCase):
    def test_store_build_label(self):
        self.assertEqual(cli._build_label({"version": "current"}),
                         "latest from the Microsoft Store")
        self.assertEqual(
            cli._build_label({"version": "current", "build": "1.17.0.0"}),
            "1.17.0.0")
        self.assertEqual(cli._build_label({"version": "1.26.63.1"}),
                         "1.26.63.1")


class RunningGameTests(unittest.TestCase):
    def test_the_selected_title_s_process_counts_as_the_game(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = _write_dungeons(base / "dungeons" / "current")
            with mock.patch.object(games, "GAMES", base), \
                    mock.patch("bol.util.load_settings",
                               return_value={"game_dir": str(root)}):
                names = prefix._game_process_names()
        self.assertEqual(names, {b"Minecraft.Windows.exe", b"Dungeons.exe"})

    def test_bedrock_alone_is_still_found(self):
        with mock.patch("bol.util.load_settings", return_value={}):
            self.assertEqual(prefix._game_process_names(),
                             {b"Minecraft.Windows.exe"})


class StoreTitleLaunchTests(ReadyLaunchHarness, unittest.TestCase):
    """A Dungeons launch: the engine and the prefix, none of Bedrock."""

    def _launch(self, root, **kwargs):
        content = root / "content"
        _write_dungeons(content)
        started = []

        def popen(cmd, **_options):
            started.append(cmd)
            process = mock.Mock()
            process.wait.return_value = 0
            return process

        with mock.patch.object(launch, "selected_edition",
                               return_value=DUNGEONS), \
                mock.patch.object(launch.mods, "prepare_launch") as levi, \
                mock.patch.object(launch, "start_auto_inject") as inject, \
                mock.patch.object(launch.xodus, "wrap_encrypted_launch",
                                  side_effect=lambda cmd, *a, **k:
                                  ["xodus-cli", "run"] + list(cmd)):
            self._exercise_ready_launch(root, popen, lambda: "token",
                                        lambda _token: True, **kwargs)
        return started, self.launch_mocks["proton_umu_cmd"], levi, inject

    def test_starts_the_declared_executable_without_bedrock_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            started, umu, levi, inject = self._launch(root)
            exe = umu.call_args[0][0]
            self.assertEqual(Path(exe), root / "content" / "Dungeons.exe")
            self.assertEqual(started[0][:2], ["xodus-cli", "run"])
            # None of what the launcher does to Minecraft itself.
            for name in ("hide_signin_button", "apply_branding",
                         "xbl_preauth"):
                self.launch_mocks[name].assert_not_called()
            levi.assert_not_called()
            inject.assert_not_called()
            self.assertEqual(self.options_calls, [])
            self.assertEqual(self.presence_calls, [])
            self.assertEqual(self.xbl_calls, [])
            # The account still goes into the prefix, for the engine's XUser.
            self.launch_mocks["wine_reg_set_refresh_token"].assert_called_once()

    def test_bedrock_editor_is_refused_for_dungeons(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(BolError, "Bedrock Editor"):
                self._launch(Path(tmp), editor=True)

    def test_no_executable_means_no_game(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "content"
            _write_dungeons(root)
            (root / "Dungeons.exe").unlink()
            with mock.patch.object(launch, "selected_edition",
                                   return_value=DUNGEONS):
                edition, game_dir, exe = launch._selected_game(
                    {"game_dir": str(root)})
            self.assertIs(edition, DUNGEONS)
            self.assertIsNone(exe)


if __name__ == "__main__":
    unittest.main()
