"""Regression tests for Minecraft edition installation through Xodus."""
# SPDX-License-Identifier: MIT

import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import games
from bol.log import BolError


_CATALOGUE = (
    {"version": "1.26.44.3",
     "urls": ["http://assets1.xboxlive.com/Z/a/"
              "7792d9ce-355a-493c-afbd-768f4a77c3b0/1.26.4403.0.b/x.msixvc"]},
    {"version": "1.26.42.1",
     "urls": ["http://assets1.xboxlive.com/Z/a/"
              "7792d9ce-355a-493c-afbd-768f4a77c3b0/1.26.4201.0.b/x.msixvc"]},
)


def _catalogue(installed=()):
    return [dict(entry, installed=entry["version"] in installed)
            for entry in _CATALOGUE]


def _write_game(root, marker=b"MZ game"):
    root.mkdir(parents=True, exist_ok=True)
    (root / "Minecraft.Windows.exe").write_bytes(marker)
    (root / "AppxManifest.xml").write_text(
        '<Package><Identity Version="1.26.3301.0" /></Package>',
        encoding="utf-8")
    return root


def _write_store_game(root, package=True):
    """A build downloaded from the Store: ciphertext exe, package beside it.

    The executable of a GDK title is kept encrypted at rest and decrypted out
    of the package at every launch, so a directory without that package holds
    a game that cannot start — which is what `package=False` writes.
    """
    _write_game(root, marker=b"\x9c\x1f encrypted")
    if package:
        (root / games.xodus.PACKAGE_CACHE).write_bytes(b"msixvc")
    return root


class EditionListingTests(unittest.TestCase):
    def test_beta_edition_is_hidden_unless_requested(self):
        stable = games.list_editions(include_beta=False)
        everything = games.list_editions(include_beta=True)

        self.assertTrue(stable)
        self.assertTrue(all(not e["beta"] for e in stable))
        self.assertTrue(any(e["beta"] for e in everything))
        self.assertGreater(len(everything), len(stable))


class VersionListingTests(unittest.TestCase):
    def test_builds_already_on_disk_are_marked(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_game(base / "release" / "1.26.42.1")
            with mock.patch.object(games, "GAMES", base), \
                    mock.patch.object(games.xodus, "version_catalogue",
                                      return_value=_CATALOGUE):
                builds = games.list_versions("release")

        by_version = {b["version"]: b for b in builds}
        # Switching back to a build you already have must cost nothing, so the
        # picker has to be able to say which those are.
        self.assertTrue(by_version["1.26.42.1"]["installed"])
        self.assertFalse(by_version["1.26.44.3"]["installed"])

    def test_a_build_that_lost_its_package_is_not_marked_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_store_game(base / "release" / "1.26.42.1", package=False)
            with mock.patch.object(games, "GAMES", base), \
                    mock.patch.object(games.xodus, "version_catalogue",
                                      return_value=_CATALOGUE):
                builds = games.list_versions("release")

        # Nothing in that folder can be launched, so offering it as a build
        # already on disk is offering a build that does not start.
        self.assertFalse(
            {b["version"]: b for b in builds}["1.26.42.1"]["installed"])


class InstallTests(unittest.TestCase):
    def setUp(self):
        # install_game() consults the configured game_dir to find a copy
        # inherited from before the Store switch. Without this the tests would
        # read whatever is installed on the machine running them.
        self.settings = {}
        for target, kwargs in (
                ("load_settings", {"side_effect": lambda: dict(self.settings)}),
                ("list_versions", {"return_value": _catalogue()})):
            patcher = mock.patch.object(games, target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _edition(self):
        return {"id": "release", "product": "9NBLGGH2JHXJ",
                "name": "Minecraft for Windows", "beta": False}

    def test_no_version_named_installs_the_newest(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)

            def fake_install(url, dest, progress=None):
                _write_game(Path(dest))

            with mock.patch.object(games, "GAMES", base), \
                    mock.patch.object(games.xodus, "install",
                                      side_effect=fake_install) as install:
                root = games.install_game(self._edition())

            self.assertEqual(root, base / "release" / "1.26.44.3")
            # Every mirror of that build, so a truncated body is retryable.
            self.assertTrue(all("1.26.4403" in url
                                for url in install.call_args[0][0]))
            record = json.loads(
                (base / "release" / "1.26.44.3"
                 / games._INSTALL_METADATA).read_text())
            self.assertEqual(record["version"], "1.26.44.3")

    def test_a_named_version_is_the_one_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)

            def fake_install(url, dest, progress=None):
                _write_game(Path(dest))

            with mock.patch.object(games, "GAMES", base), \
                    mock.patch.object(games.xodus, "install",
                                      side_effect=fake_install) as install:
                root = games.install_game(self._edition(), "1.26.42.1")

            self.assertEqual(root, base / "release" / "1.26.42.1")
            self.assertTrue(all("1.26.4201" in url
                                for url in install.call_args[0][0]))

    def test_a_build_already_on_disk_is_not_downloaded_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_game(base / "release" / "1.26.42.1")

            with mock.patch.object(games, "GAMES", base), \
                    mock.patch.object(games.xodus, "install") as install:
                games.install_game(self._edition(), "1.26.42.1")

            install.assert_not_called()

    def test_a_store_build_with_its_package_is_not_downloaded_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_store_game(base / "release" / "1.26.42.1")

            with mock.patch.object(games, "GAMES", base), \
                    mock.patch.object(games.xodus, "install") as install:
                games.install_game(self._edition(), "1.26.42.1")

            install.assert_not_called()

    def test_a_store_build_that_lost_its_package_is_downloaded_again(self):
        # Issue #216. The folder still holds an executable and a manifest, so
        # it used to count as installed: the download was skipped and every
        # launch died on the package that decrypts the executable, with the
        # only way out being to delete the build by hand — the launcher offers
        # no reinstall of its own.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_store_game(base / "release" / "1.26.42.1", package=False)

            with mock.patch.object(games, "GAMES", base), \
                    mock.patch.object(
                        games.xodus, "install",
                        side_effect=lambda url, dest, progress=None:
                            _write_store_game(Path(dest))) as install:
                root = games.install_game(self._edition(), "1.26.42.1")

            install.assert_called_once()
            self.assertEqual(root, base / "release" / "1.26.42.1")

    def test_a_download_that_leaves_no_package_is_rejected(self):
        # The other half: repairing it must actually produce a build that can
        # be decrypted, or install_game would hand the launcher the same
        # unplayable folder and report it as a fresh install.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)

            with mock.patch.object(games, "GAMES", base), \
                    mock.patch.object(
                        games.xodus, "install",
                        side_effect=lambda url, dest, progress=None:
                            _write_store_game(Path(dest), package=False)), \
                    self.assertRaises(BolError):
                games.install_game(self._edition(), "1.26.42.1")

    def test_a_delisted_version_falls_back_to_the_newest(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)

            def fake_install(url, dest, progress=None):
                _write_game(Path(dest))

            with mock.patch.object(games, "GAMES", base), \
                    mock.patch.object(games.xodus, "install",
                                      side_effect=fake_install):
                root = games.install_game(self._edition(), "1.0.0.0")

            # A build Microsoft stopped serving must not leave PLAY dead.
            self.assertEqual(root, base / "release" / "1.26.44.3")

    def test_an_empty_catalogue_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(games, "GAMES", Path(tmp)), \
                mock.patch.object(games, "list_versions", return_value=[]), \
                self.assertRaises(BolError):
            games.install_game(self._edition())

    def test_an_install_from_before_the_store_switch_still_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            # The pre-Store layout is GAMES/<version-tag>/, not
            # GAMES/<edition>/<version>/.
            legacy = _write_game(base / "1.26.42.1" / "Microsoft.MinecraftUWP")
            self.settings["game_dir"] = str(legacy)

            with mock.patch.object(games, "GAMES", base), \
                    mock.patch.object(
                        games.xodus, "install",
                        side_effect=BolError("downloader not published")):
                root = games.install_game(self._edition())

            # An upgrade must not strand a player on a game they already have.
            self.assertEqual(root, legacy)

    def test_being_signed_out_is_surfaced_not_swallowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            legacy = _write_game(base / "1.26.42.1" / "Microsoft.MinecraftUWP")
            self.settings["game_dir"] = str(legacy)

            # NotSignedIn is a BolError, so the inherited-copy fallback would
            # otherwise eat it and the launcher would keep starting the old
            # build instead of offering the sign-in that would update it.
            with mock.patch.object(games, "GAMES", base), \
                    mock.patch.object(
                        games.xodus, "install",
                        side_effect=games.xodus.NotSignedIn("sign in")), \
                    self.assertRaises(games.xodus.NotSignedIn):
                games.install_game(self._edition())

    def test_a_failed_download_with_nothing_installed_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(games, "GAMES", Path(tmp)), \
                mock.patch.object(games.xodus, "install",
                                  side_effect=BolError("no network")), \
                self.assertRaises(BolError):
            games.install_game(self._edition())

    def test_incomplete_tree_after_streaming_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)

            def truncated(url, dest, progress=None):
                Path(dest).mkdir(parents=True, exist_ok=True)
                (Path(dest) / "Minecraft.Windows.exe").write_bytes(b"MZ")

            with mock.patch.object(games, "GAMES", base), \
                    mock.patch.object(games.xodus, "install",
                                      side_effect=truncated), \
                    self.assertRaises(BolError):
                games.install_game(self._edition())


class SelectionTests(unittest.TestCase):
    def test_managed_folder_records_edition_and_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            games_dir = base / "games"
            root = _write_game(games_dir / "preview" / "1.26.50.25")
            settings = {}

            with mock.patch.object(games, "GAMES", games_dir), \
                    mock.patch.object(games, "CONTENT", base / "content"), \
                    mock.patch.object(games, "load_settings",
                                      side_effect=lambda: dict(settings)), \
                    mock.patch.object(games, "save_settings",
                                      side_effect=settings.update):
                games.use_game_dir(root)

        self.assertEqual(settings["mc_edition"], "preview")
        self.assertEqual(settings["mc_version"], "1.26.50.25")

    def test_imported_folder_leaves_the_selection_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = _write_game(base / "elsewhere")
            settings = {"mc_edition": "release"}

            with mock.patch.object(games, "GAMES", base / "games"), \
                    mock.patch.object(games, "CONTENT", base / "content"), \
                    mock.patch.object(games, "load_settings",
                                      side_effect=lambda: dict(settings)), \
                    mock.patch.object(games, "save_settings",
                                      side_effect=settings.update):
                games.use_game_dir(root)

        # A copy from outside the managed tree is not an edition; keeping the
        # previous choice would let the next setup reinstall over it.
        self.assertEqual(settings["mc_edition"], "release")
        # Its own build is still reported, from the manifest.
        self.assertEqual(settings["mc_version"], "1.26.33.1")

    def test_auto_selection_prefers_the_remembered_choice(self):
        edition, version = games._auto_selection(
            {"mc_edition": "preview", "mc_version": "1.26.50.25"})
        self.assertEqual(edition["id"], "preview")
        self.assertEqual(version, "1.26.50.25")

    def test_auto_selection_falls_back_to_the_stable_edition(self):
        edition, version = games._auto_selection({})
        self.assertFalse(edition["beta"])
        # No version means "whatever is newest", which install_game resolves.
        self.assertIsNone(version)


class ManifestVersionTests(unittest.TestCase):
    def test_appx_third_field_is_split_back_into_minor_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AppxManifest.xml").write_text(
                '<Package><Identity Version="1.26.2004.0" /></Package>',
                encoding="utf-8")
            self.assertEqual(games.mc_version_str(root), "1.26.20.4")


class LegacyArchiveTests(unittest.TestCase):
    """#289: the zips launchers before 2.1 downloaded are not kept forever."""

    def test_the_old_game_archives_go_and_nothing_else_does(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            legacy = [
                "Microsoft.MinecraftUWP_1.26.4403.0_x64__8wekyb3d8bbwe.zip",
                "Microsoft.MinecraftWindowsBeta_1.26.5020.0_x64__8wekyb3d8"
                "bbwe.zip",
                "Microsoft.MinecraftUWP_1.26.4201.0_x64__8wekyb3d8bbwe.zip"
                ".part",
            ]
            kept = [
                "GDK-Proton-xuser-wow64-archs-native18.tar.gz",
                "xodus-cli-4615749c6e02-p4.tar.gz",
                "releases_mc_360001186971_100.json",
                "some-addon.zip",
                "auto-inject-0123456789abcdef.dll",
            ]
            for name in legacy + kept:
                (cache / name).write_bytes(b"12345")
            (cache / "minecraft-folder.zip").mkdir()
            with mock.patch.object(games, "info") as said:
                freed = games.prune_legacy_game_archives(cache)
            remaining = sorted(path.name for path in cache.iterdir())
        self.assertEqual(freed, 5 * len(legacy))
        self.assertEqual(remaining, sorted(kept + ["minecraft-folder.zip"]))
        self.assertIn("3 Minecraft archive(s)", said.call_args.args[0])

    def test_a_cache_with_nothing_to_remove_says_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(games, "info") as said:
            self.assertEqual(games.prune_legacy_game_archives(Path(tmp)), 0)
            self.assertEqual(
                games.prune_legacy_game_archives(Path(tmp) / "missing"), 0)
        said.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class InstalledBuildTests(unittest.TestCase):
    """What is on disk, and removing one of it (issue #214).

    Every build is downloaded into a folder of its own, which is what makes
    switching back to one instant -- and what makes them pile up: nothing but
    `rm -rf` ever removed one, so trying three builds cost three copies of a
    2.5 GiB game and the launcher never mentioned it.
    """

    @contextlib.contextmanager
    def _tree(self, settings=None):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            games_dir = base / "games"
            games_dir.mkdir(parents=True)
            store = dict(settings or {})
            with mock.patch.object(games, "GAMES", games_dir), \
                    mock.patch.object(games, "CONTENT", base / "content"), \
                    mock.patch.object(games, "load_settings",
                                      side_effect=lambda: dict(store)), \
                    mock.patch.object(games, "save_settings",
                                      side_effect=lambda new: (store.clear(),
                                                               store.update(new))):
                yield base, games_dir, store

    def test_every_downloaded_build_is_listed_newest_first(self):
        with self._tree() as (_base, games_dir, _settings):
            _write_store_game(games_dir / "release" / "1.26.42.1")
            _write_store_game(games_dir / "release" / "1.26.44.3")
            _write_game(games_dir / "preview" / "1.26.50.25")

            builds = games.installed_builds()

        self.assertEqual([b["version"] for b in builds],
                         ["1.26.50.25", "1.26.44.3", "1.26.42.1"])
        self.assertEqual({b["edition"] for b in builds}, {"release", "preview"})
        self.assertTrue(all(b["managed"] and b["playable"] for b in builds))

    def test_a_build_from_before_the_store_switch_is_listed_too(self):
        # games/<version>/, with no edition folder above it: the layout an
        # upgrade inherits. It is a real build and the one some players are
        # still on, so it is listed -- and it is removable, because it is the
        # copy that is most often the duplicate.
        with self._tree() as (_base, games_dir, _settings):
            _write_game(games_dir / "1.26.44.3")

            builds = games.installed_builds()

        self.assertEqual(len(builds), 1)
        self.assertTrue(builds[0]["legacy"])
        self.assertTrue(builds[0]["managed"])
        self.assertIsNone(builds[0]["edition"])

    def test_a_build_that_lost_its_package_is_listed_as_incomplete(self):
        with self._tree() as (_base, games_dir, _settings):
            _write_store_game(games_dir / "release" / "1.26.44.3",
                              package=False)

            builds = games.installed_builds()

        # It has an exe and a manifest and it cannot start (#216): saying so
        # is the difference between "remove this" and "download it again".
        self.assertFalse(builds[0]["playable"])

    def test_the_build_the_launcher_would_start_is_marked(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            games_dir = base / "games"
            root = _write_store_game(games_dir / "release" / "1.26.44.3")
            _write_store_game(games_dir / "release" / "1.26.42.1")
            settings = {"game_dir": str(root)}
            with mock.patch.object(games, "GAMES", games_dir), \
                    mock.patch.object(games, "CONTENT", base / "content"), \
                    mock.patch.object(games, "load_settings",
                                      return_value=settings):
                builds = games.installed_builds()

        in_use = [b["version"] for b in builds if b["in_use"]]
        self.assertEqual(in_use, ["1.26.44.3"])

    def test_a_folder_the_player_pointed_at_is_listed_but_not_ours(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            games_dir = base / "games"
            games_dir.mkdir(parents=True)
            elsewhere = _write_game(base / "somewhere" / "Minecraft")
            settings = {"game_dir": str(elsewhere)}
            with mock.patch.object(games, "GAMES", games_dir), \
                    mock.patch.object(games, "CONTENT", base / "content"), \
                    mock.patch.object(games, "load_settings",
                                      return_value=settings):
                builds = games.installed_builds()

        self.assertEqual(len(builds), 1)
        self.assertTrue(builds[0]["in_use"])
        self.assertFalse(builds[0]["managed"])

    def test_removing_a_build_frees_its_folder_and_nothing_else(self):
        with self._tree() as (base, games_dir, _settings):
            keep = _write_store_game(games_dir / "release" / "1.26.44.3")
            drop = _write_store_game(games_dir / "release" / "1.26.42.1")
            # Worlds and settings live in the prefix, beside the account that
            # made them -- never in a build folder. This is the promise every
            # "Remove" button in the launcher makes.
            worlds = base / "prefix" / "minecraftWorlds" / "my world"
            worlds.mkdir(parents=True)

            with mock.patch("bol.prefix._mc_running", return_value=False):
                freed = games.remove_build(drop)

            self.assertGreater(freed, 0)
            self.assertFalse(drop.exists())
            self.assertTrue(keep.exists())
            self.assertTrue(worlds.exists())

    def test_removing_the_build_in_use_takes_the_selection_with_it(self):
        with self._tree() as (base, games_dir, settings):
            root = _write_store_game(games_dir / "release" / "1.26.44.3")
            settings["game_dir"] = str(root)
            content = base / "content"
            content.symlink_to(root)
            with mock.patch.object(games, "CONTENT", content), \
                    mock.patch("bol.prefix._mc_running", return_value=False):
                games.remove_build(root)

            # A setting left pointing at a folder that is gone turns the next
            # PLAY into a launch failure instead of the download it should be.
            self.assertNotIn("game_dir", settings)
            self.assertFalse(content.is_symlink())

    def test_a_folder_outside_the_games_tree_is_never_removed(self):
        with self._tree() as (base, _games_dir, _settings):
            elsewhere = _write_game(base / "somewhere" / "Minecraft")

            with mock.patch("bol.prefix._mc_running", return_value=False), \
                    self.assertRaises(BolError):
                games.remove_build(elsewhere)

            self.assertTrue(elsewhere.exists())

    def test_the_games_folder_itself_is_never_removed(self):
        with self._tree() as (_base, games_dir, _settings):
            _write_store_game(games_dir / "release" / "1.26.44.3")

            with mock.patch("bol.prefix._mc_running", return_value=False), \
                    self.assertRaises(BolError):
                games.remove_build(games_dir)

            self.assertTrue(games_dir.exists())

    def test_nothing_is_removed_while_minecraft_is_running(self):
        with self._tree() as (_base, games_dir, _settings):
            root = _write_store_game(games_dir / "release" / "1.26.44.3")

            with mock.patch("bol.prefix._mc_running", return_value=True), \
                    self.assertRaises(BolError):
                games.remove_build(root)

            self.assertTrue(root.exists())

    def test_a_whole_edition_folder_is_never_removed(self):
        # games/<edition>/ holds every build of that edition and has exactly
        # the depth of the pre-Store layout, so it is the one path a check on
        # depth alone would wave through.
        with self._tree() as (_base, games_dir, _settings):
            _write_store_game(games_dir / "release" / "1.26.44.3")

            with mock.patch("bol.prefix._mc_running", return_value=False), \
                    self.assertRaises(BolError):
                games.remove_build(games_dir / "release")

            self.assertTrue((games_dir / "release" / "1.26.44.3").exists())

    def test_a_folder_that_holds_no_build_is_left_alone(self):
        with self._tree() as (_base, games_dir, _settings):
            stray = games_dir / "release" / "notes"
            stray.mkdir(parents=True)
            (stray / "readme.txt").write_text("mine", encoding="utf-8")

            with mock.patch("bol.prefix._mc_running", return_value=False), \
                    self.assertRaises(BolError):
                games.remove_build(stray)

            self.assertTrue(stray.exists())


class BuildsPilingUpAreMentionedTests(unittest.TestCase):
    """A download never removes the build it follows, and nothing said so.

    That is the right behaviour -- it is what makes going back to a build
    instant -- but it was completely silent, so a few version changes quietly
    became 10 GiB and the launcher read as downloading Minecraft over and
    over (issue #214).
    """

    def setUp(self):
        patcher = mock.patch.object(games.xodus, "version_catalogue",
                                    side_effect=lambda _e, **k: list(_CATALOGUE))
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _edition():
        return {"id": "release", "product": "9NBLGGH2JHXJ",
                "name": "Minecraft for Windows", "beta": False}

    def _install(self, base, version):
        def fake_install(_url, dest, progress=None):
            _write_game(Path(dest))

        said = []
        with mock.patch.object(games, "GAMES", base), \
                mock.patch.object(games, "info", side_effect=said.append), \
                mock.patch.object(games, "load_settings", return_value={}), \
                mock.patch.object(games.xodus, "install",
                                  side_effect=fake_install):
            games.install_game(self._edition(), version)
        return said

    def test_the_builds_left_behind_are_named_after_a_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_game(base / "release" / "1.26.42.1")

            said = self._install(base, "1.26.44.3")

        mention = [line for line in said if "still installed" in line]
        self.assertEqual(len(mention), 1)
        self.assertIn("Settings ▸ Versions", mention[0])

    def test_the_first_build_is_not_talked_about(self):
        with tempfile.TemporaryDirectory() as tmp:
            said = self._install(Path(tmp), "1.26.44.3")

        self.assertFalse([line for line in said if "still installed" in line])
