"""Minecraft Education as an edition (#351): listing, install, setup, CLI."""
# SPDX-License-Identifier: MIT

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import cli, games, gamesetup, xodus

EDUCATION = xodus.edition("education")
EDU_MANIFEST = ('<Package><Identity Name="Microsoft.MinecraftEducationEdition"'
                ' Version="1.26.3300.0" /></Package>')
BEDROCK_MANIFEST = ('<Package><Identity Name="Microsoft.MinecraftUWP" '
                    'Version="1.26.5203.0" /></Package>')
CATALOGUE = [{"version": "1.26.33.0", "urls": [], "package": {
    "update_id": "uid", "revision": "1", "sha1": "QUJD", "sha256": "x",
    "size": 10, "file_name": "b.appxbundle",
    "moniker": "Microsoft.MinecraftEducationEdition_2026.1021.2142.0_"
               "neutral_~_8wekyb3d8bbwe"}}]


def _write_game(root, manifest=EDU_MANIFEST):
    root.mkdir(parents=True, exist_ok=True)
    (root / "Minecraft.Windows.exe").write_bytes(b"MZ plaintext")
    (root / "AppxManifest.xml").write_text(manifest, encoding="utf-8")
    return root


class EditionTests(unittest.TestCase):
    def test_education_is_listed_beside_the_bedrock_editions(self):
        ids = [entry["id"] for entry in games.list_editions(True)]
        self.assertEqual(ids[:2], ["release", "preview"])
        self.assertIn("education", ids)
        # Not a beta: the stable listing has it, and Stable stays first --
        # which is what a fresh install picks when nothing was chosen.
        self.assertIn("education",
                      [e["id"] for e in games.list_editions(False)])
        edition, _version = games._auto_selection({})
        self.assertEqual(edition["id"], "release")

    def test_the_edition_of_a_folder_comes_from_its_path_or_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "games"
            managed = _write_game(base / "education" / "1.26.33.0",
                                  manifest=BEDROCK_MANIFEST)
            dropped = _write_game(Path(td) / "elsewhere" / "edu")
            bedrock = _write_game(Path(td) / "elsewhere" / "mc",
                                  manifest=BEDROCK_MANIFEST)
            bare = Path(td) / "bare"
            bare.mkdir()
            with mock.patch.object(games, "GAMES", base):
                # The managed folder names its edition outright.
                self.assertEqual(games.edition_for_dir(managed)["id"],
                                 "education")
                # A copy dropped in by hand is told by its package identity.
                self.assertEqual(games.edition_for_dir(dropped)["id"],
                                 "education")
                self.assertIsNone(games.edition_for_dir(bedrock))
                self.assertIsNone(games.edition_for_dir(bare))
                self.assertIsNone(games.edition_for_dir(None))

                self.assertFalse(games.uses_xbox_live(managed))
                self.assertFalse(games.uses_xbox_live(dropped))
                self.assertTrue(games.uses_xbox_live(bedrock))
                self.assertTrue(games.uses_xbox_live(bare))


class VersionListingTests(unittest.TestCase):
    def test_education_builds_come_from_windows_update(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _write_game(base / "education" / "1.26.33.0")
            with mock.patch.object(games, "GAMES", base), \
                    mock.patch.object(games.wustore, "version_catalogue",
                                      return_value=CATALOGUE) as listed, \
                    mock.patch.object(games.xodus,
                                      "version_catalogue") as indexed:
                versions = games.list_versions("education", ignore_cache=True)

        listed.assert_called_once_with(EDUCATION, ignore_cache=True)
        indexed.assert_not_called()
        self.assertEqual([(v["version"], v["installed"]) for v in versions],
                         [("1.26.33.0", True)])

    def test_bedrock_builds_still_come_from_the_index(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(games, "GAMES", Path(td)), \
                mock.patch.object(games.wustore,
                                  "version_catalogue") as listed, \
                mock.patch.object(games.xodus, "version_catalogue",
                                  return_value=[]) as indexed:
            games.list_versions("release")
        indexed.assert_called_once()
        listed.assert_not_called()


class InstallTests(unittest.TestCase):
    def test_education_installs_through_windows_update_without_xodus(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)

            def fake_install(edition, entry, dest, progress=None):
                self.assertEqual(edition["id"], "education")
                self.assertEqual(entry["version"], "1.26.33.0")
                _write_game(Path(dest))
                return dest

            with mock.patch.object(games, "GAMES", base), \
                    mock.patch.object(games, "load_settings",
                                      return_value={}), \
                    mock.patch.object(games, "list_versions",
                                      return_value=[dict(CATALOGUE[0],
                                                         installed=False)]), \
                    mock.patch.object(games.wustore, "install",
                                      side_effect=fake_install), \
                    mock.patch.object(games.xodus, "install") as xodus_install, \
                    mock.patch.object(games, "_mention_other_builds"), \
                    mock.patch.object(games, "info"), \
                    mock.patch.object(games, "ok"):
                root = games.install_game(EDUCATION)

            xodus_install.assert_not_called()
            self.assertEqual(root, base / "education" / "1.26.33.0")
            record = json.loads((root / games._INSTALL_METADATA).read_text())
            self.assertEqual(record["edition"], "education")
            self.assertEqual(record["product"], "9NBLGGH4R2R6")
            self.assertIn("MinecraftEducationEdition", record["source_url"])


class SetupTests(unittest.TestCase):
    def _setup(self, game_dir):
        settings = {"game_dir": str(game_dir), "proton_source": "winegdk"}
        mocks = {}
        names = ("mkdirs", "prune_legacy_game_archives", "ensure_login_deps",
                 "ensure_winegdk", "install_gdk_xbox_dlls", "ensure_umu",
                 "fix_curl_ssl", "install_gameinput", "hide_signin_button",
                 "active_prefix", "ok")
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                gamesetup, "load_settings", return_value=settings))
            stack.enter_context(mock.patch.object(
                gamesetup, "_game_root", return_value=True))
            stack.enter_context(mock.patch.object(
                gamesetup, "boot_prefix", return_value=True))
            stack.enter_context(mock.patch.object(
                gamesetup.hostarch, "problem", return_value=None))
            for name in names:
                mocks[name] = stack.enter_context(
                    mock.patch.object(gamesetup, name))
            gamesetup._do_setup()
        return mocks

    def test_setup_leaves_the_xbox_live_fixes_out_of_education(self):
        with tempfile.TemporaryDirectory() as td:
            mocks = self._setup(_write_game(Path(td) / "edu"))
        for name in ("install_gdk_xbox_dlls", "fix_curl_ssl",
                     "hide_signin_button"):
            with self.subTest(step=name):
                mocks[name].assert_not_called()
        mocks["install_gameinput"].assert_called_once()
        self.assertIn("school or work account",
                      mocks["ok"].call_args[0][0])

    def test_setup_still_fixes_bedrock(self):
        with tempfile.TemporaryDirectory() as td:
            mocks = self._setup(_write_game(Path(td) / "mc",
                                            manifest=BEDROCK_MANIFEST))
        for name in ("install_gdk_xbox_dlls", "fix_curl_ssl",
                     "hide_signin_button"):
            with self.subTest(step=name):
                mocks[name].assert_called_once()


class CliTests(unittest.TestCase):
    def test_setup_accepts_education(self):
        with mock.patch.object(sys, "argv", ["bedrock-on-linux", "setup",
                                             "--mc", "Education"]), \
                mock.patch.object(cli, "do_setup") as setup, \
                mock.patch.object(cli, "ok"):
            cli.main()
        self.assertEqual(setup.call_args.kwargs["mc_edition"]["id"],
                         "education")

    def test_play_does_not_ask_education_for_an_xbox_sign_in(self):
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["bedrock-on-linux", "play"]), \
                mock.patch.object(cli, "msa_signed_in", return_value=False), \
                mock.patch.object(cli, "_plays_xbox_live", return_value=False), \
                mock.patch.object(cli, "update_game_before_play"), \
                mock.patch.object(cli, "launch") as launch, \
                contextlib.redirect_stdout(output):
            cli.main()
        self.assertTrue(launch.called)
        self.assertNotIn("Not signed in", output.getvalue())

    def test_one_unreachable_source_does_not_hide_the_others(self):
        def versions(edition_id, ignore_cache=False):
            if edition_id == "education":
                raise games.wustore.WindowsUpdateError("unreachable")
            return [{"version": "1.26.52.3", "installed": False}]

        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["bedrock-on-linux", "versions"]), \
                mock.patch.object(cli, "list_versions", side_effect=versions), \
                contextlib.redirect_stdout(output):
            cli.main()
        printed = output.getvalue()
        self.assertIn("1.26.52.3", printed)
        self.assertIn("Minecraft Education", printed)
        self.assertIn("could not be listed", printed)


if __name__ == "__main__":
    unittest.main()
