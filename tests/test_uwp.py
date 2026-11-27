"""The UWP builds of Minecraft for Windows on wine-uwp (#387)."""
# SPDX-License-Identifier: MIT

import base64
import hashlib
import io
import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from bol import games, launch, uwp, xodus
from bol.log import BolError

UWP = xodus.edition("uwp")
UWP_MANIFEST = ('<Package><Identity Name="Microsoft.MinecraftUWP" '
                'Publisher="CN=Microsoft Corporation" '
                'Version="1.21.5101.0" ProcessorArchitecture="x64" />'
                '</Package>')


def _write_build(folder, manifest=UWP_MANIFEST):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "Minecraft.Windows.exe").write_bytes(b"MZ plaintext")
    (folder / "AppxManifest.xml").write_text(manifest, encoding="utf-8")
    (folder / "identity.env").write_text(
        f"export WINE_UWP_PACKAGE_FAMILY_NAME={uwp.FAMILY}\n")
    return folder


class EditionTests(unittest.TestCase):
    def test_the_uwp_builds_are_an_edition_of_their_own(self):
        self.assertTrue(uwp.is_uwp(UWP))
        self.assertFalse(uwp.is_uwp(xodus.edition("release")))
        self.assertFalse(uwp.is_uwp(None))
        self.assertIn("uwp", [e["id"] for e in games.list_editions(False)])
        # Nothing Bedrock's GDK session does applies to them...
        self.assertFalse(games.is_bedrock(UWP))
        # ...but their package version is Bedrock's packed numbering.
        self.assertTrue(games.bedrock_numbering(UWP))
        # Stable stays what a fresh install picks.
        edition, _version = games._auto_selection({})
        self.assertEqual(edition["id"], "release")

    def test_a_folder_is_uwp_by_its_place_not_by_its_package_name(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "games"
            managed = _write_build(base / "uwp" / "1.21.51.1")
            # A GDK build carries the very same package name.
            gdk = _write_build(base / "release" / "1.26.44.3")
            with mock.patch.object(games, "GAMES", base):
                self.assertEqual(games.edition_for_dir(managed)["id"], "uwp")
                self.assertEqual(games.edition_for_dir(gdk)["id"], "release")
                self.assertFalse(games.uses_xbox_live(managed))

    def test_wine_uwps_own_folder_is_not_a_build(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _write_build(base / "uwp" / "1.21.51.1")
            _write_build(base / "uwp" / ".wine-uwp" / "stage" / "1.21.50.7")
            with mock.patch.object(games, "GAMES", base):
                self.assertEqual(list(games._installed_versions("uwp")),
                                 ["1.21.51.1"])


class HostTests(unittest.TestCase):
    def test_an_old_glibc_is_said_before_anything_is_downloaded(self):
        with mock.patch.object(uwp.platform, "machine",
                               return_value="x86_64"), \
                mock.patch.object(uwp.os, "confstr",
                                  return_value="glibc 2.35"):
            problem = uwp.host_problem()
            self.assertIn("2.38", problem)
            self.assertIn("2.35", problem)
            with mock.patch.object(uwp, "require_ownership") as owned, \
                    self.assertRaisesRegex(uwp.UwpError, "glibc"):
                uwp.install(UWP, {"version": "1.21.51.1",
                                  "update_id": "x"}, "/nonexistent/x")
            owned.assert_not_called()
            with mock.patch.object(uwp, "ensure_runtime") as runtime, \
                    self.assertRaises(uwp.UwpError):
                uwp.prepare()
            runtime.assert_not_called()

    def test_a_recent_x86_64_system_has_no_problem(self):
        with mock.patch.object(uwp.platform, "machine",
                               return_value="x86_64"), \
                mock.patch.object(uwp.os, "confstr",
                                  return_value="glibc 2.41"):
            self.assertIsNone(uwp.host_problem())
        with mock.patch.object(uwp.platform, "machine",
                               return_value="aarch64"):
            self.assertIn("x86-64", uwp.host_problem())


class CatalogueTests(unittest.TestCase):
    ROWS = [
        ["1.21.50.7", "11111111-2222-3333-4444-555555555555", 0],
        ["1.21.51.1", "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE", 0],
        ["1.21.60.21", "99999999-2222-3333-4444-555555555555", 2],
        ["1.9.0.3", "12121212-2222-3333-4444-555555555555", 0],
        ["1.21.50.7", "22222222-2222-3333-4444-555555555555", 0],
        ["not a version", "33333333-2222-3333-4444-555555555555", 0],
        ["1.20.0.1", "not-a-guid", 0],
        ["1.20.1.2", "44444444-2222-3333-4444-555555555555", 1],
        "garbage",
    ]

    def test_releases_only_newest_first_the_later_row_winning(self):
        with mock.patch.object(uwp, "_fetch_with_fallback",
                               return_value=self.ROWS) as fetched:
            builds = uwp.version_catalogue(UWP)
        self.assertEqual([b["version"] for b in builds],
                         ["1.21.51.1", "1.21.50.7", "1.9.0.3"])
        self.assertEqual(builds[0]["update_id"],
                         "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        self.assertEqual(builds[1]["update_id"],
                         "22222222-2222-3333-4444-555555555555")
        self.assertEqual(builds[0]["urls"], [])
        self.assertGreater(fetched.call_args.kwargs["ttl"], 0)
        with mock.patch.object(uwp, "_fetch_with_fallback",
                               return_value=self.ROWS) as fetched:
            uwp.version_catalogue(UWP, ignore_cache=True)
        self.assertEqual(fetched.call_args.kwargs["ttl"], 0)

    def test_an_unreadable_list_is_said_so(self):
        with mock.patch.object(uwp, "_fetch_with_fallback",
                               side_effect=OSError("offline")), \
                self.assertRaisesRegex(uwp.UwpError, "offline"):
            uwp.version_catalogue(UWP)
        with mock.patch.object(uwp, "_fetch_with_fallback",
                               return_value={"a": 1}), \
                self.assertRaises(uwp.UwpError):
            uwp.version_catalogue(UWP)

    def test_games_lists_them_through_their_own_catalogue(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(games, "GAMES", Path(td)), \
                mock.patch.object(uwp, "_fetch_with_fallback",
                                  return_value=self.ROWS):
            _write_build(Path(td) / "uwp" / "1.21.50.7")
            listed = games.list_versions("uwp")
        versions = [entry["version"] for entry in listed]
        self.assertEqual(versions[:3], ["1.21.51.1", "1.21.50.7", "1.9.0.3"])
        installed = {entry["version"]: entry.get("installed")
                     for entry in listed}
        self.assertTrue(installed["1.21.50.7"])
        self.assertFalse(installed["1.21.51.1"])


class LicenceTests(unittest.TestCase):
    """xodus.require_licence, against a stand-in for xodus-cli."""

    def _cli(self, td, script):
        binary = Path(td) / "xodus-cli"
        binary.write_text("#!/bin/sh\n" + script)
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        record = Path(td) / "called"
        env = dict(os.environ, RECORD=str(record))
        patches = [
            mock.patch.object(xodus, "ensure_cli", return_value=binary),
            mock.patch.object(xodus, "signed_in", return_value=True),
            mock.patch.object(xodus, "_env", return_value=env),
            mock.patch.object(xodus, "_announce_device_registration"),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        return record

    def test_an_owned_copy_passes_and_its_keys_are_not_kept(self):
        with tempfile.TemporaryDirectory() as td:
            record = self._cli(td, 'echo "$1 $2 $3" > "$RECORD"\n'
                                   'printf secret > "$3"\n')
            self.assertTrue(xodus.require_licence(UWP["content_id"]))
            verb, content_id, keys = record.read_text().split()
            self.assertEqual((verb, content_id),
                             ("license", UWP["content_id"]))
            self.assertTrue(Path(keys).name == "keys")
            self.assertFalse(Path(keys).parent.exists())

    def test_a_copy_the_account_lacks_is_not_owned(self):
        with tempfile.TemporaryDirectory() as td:
            self._cli(td, 'echo "Error: not entitled to this content" >&2\n'
                          'exit 1\n')
            with self.assertRaises(xodus.NotOwned):
                xodus.require_licence(UWP["content_id"],
                                      title="Minecraft for Windows")

    def test_keys_written_with_a_refusal_are_still_a_refusal(self):
        with tempfile.TemporaryDirectory() as td:
            self._cli(td, 'printf x > "$3"\n'
                          'echo "package was not found, is it owned by the '
                          'user?"\n')
            with self.assertRaises(xodus.NotOwned):
                xodus.require_licence(UWP["content_id"])

    def test_no_network_is_not_mistaken_for_no_copy(self):
        with tempfile.TemporaryDirectory() as td:
            self._cli(td, 'echo "error sending request for url" >&2\n'
                          'exit 1\n')
            with self.assertRaises(xodus.XodusError) as caught:
                xodus.require_licence(UWP["content_id"])
            self.assertNotIsInstance(caught.exception, xodus.NotOwned)
            self.assertIn("network", str(caught.exception))

    def test_without_an_account_nothing_is_asked(self):
        with tempfile.TemporaryDirectory() as td:
            record = self._cli(td, 'echo called > "$RECORD"\n')
            with mock.patch.object(xodus, "signed_in", return_value=False), \
                    self.assertRaises(xodus.NotSignedIn):
                xodus.require_licence(UWP["content_id"])
            self.assertFalse(record.exists())


class _Paths:
    """Every folder uwp writes to, inside one temporary directory."""

    def __init__(self, testcase):
        self.td = tempfile.TemporaryDirectory()
        testcase.addCleanup(self.td.cleanup)
        root = Path(self.td.name)
        self.root = root
        self.games = root / "games"
        self.share = root / "wine-uwp" / "runtime" / "v1" / "share" \
            / "wine-uwp"
        for target, value in (("GAMES", self.games),
                              ("WINE_UWP", root / "wine-uwp"),
                              ("WINE_UWP_PREFIX", root / "prefix"),
                              ("CACHE", root / "cache"),
                              ("LOGS", root / "logs"),
                              ("WINE_UWP_TAG", "v1")):
            patcher = mock.patch.object(uwp, target, value)
            patcher.start()
            testcase.addCleanup(patcher.stop)
        (root / "logs").mkdir()


class EnvironmentTests(unittest.TestCase):
    def test_wine_uwps_variables_and_nothing_of_another_wine(self):
        paths = _Paths(self)
        with mock.patch.dict(os.environ, {"WINEPREFIX": "/elsewhere",
                                          "WINEDLLOVERRIDES": "x=n",
                                          "WAYLAND_DISPLAY": "wayland-0",
                                          "PATH": "/usr/bin"}):
            env = uwp.environment()
        runtime = paths.root / "wine-uwp" / "runtime" / "v1"
        self.assertEqual(env["WINEPREFIX"], str(paths.root / "prefix"))
        self.assertEqual(env["WINE_UWP_ROOT"], str(paths.share))
        self.assertEqual(env["WIN11"], str(paths.root / "wine-uwp" /
                                           "windows"))
        self.assertEqual(env["WINE_BUILD"], str(runtime / "bin"))
        self.assertEqual(env["SHIM_DIR"], str(paths.share / "shims"))
        self.assertTrue(env["PATH"].startswith(str(runtime / "bin")))
        self.assertTrue(env["PATH"].endswith("/usr/bin"))
        self.assertTrue(env["WINE_UWP_D3D_RUNTIME"].endswith(
            "ext/d3d-runtime/x86_64-windows"))
        self.assertNotIn("WINEDLLOVERRIDES", env)
        self.assertNotIn("WAYLAND_DISPLAY", env)
        self.assertEqual(uwp.environment(debug="+loaddll")["WINEDEBUG"],
                         "+loaddll")

    def test_the_game_starts_as_run_sh_starts_it(self):
        paths = _Paths(self)
        build = paths.games / "uwp" / "1.21.51.1"
        argv, env, cwd = uwp.launch_command(build)
        self.assertEqual(argv[:2], ["wine", "wineuwp-launch"])
        self.assertEqual(argv[argv.index("--stage") + 1], str(build))
        self.assertEqual(argv[argv.index("--defaults") + 1],
                         str(paths.share / "launch-defaults.env"))
        self.assertEqual(argv[-2:], ["--graphics", "x11"])
        self.assertEqual(cwd, str(build))

    def test_a_step_that_fails_says_how(self):
        _Paths(self)
        with self.assertRaisesRegex(uwp.UwpError, "exit 3"):
            uwp._run(["sh", "-c", "echo 'STAGE one'; echo boom; exit 3"],
                     dict(os.environ), "A step")

    def test_a_step_stops_when_the_window_asks(self):
        _Paths(self)

        class Cancelled(Exception):
            pass

        calls = []

        def progress(done, total):
            calls.append(done)
            raise Cancelled()

        with mock.patch.object(uwp, "stop_prefix") as stopped, \
                self.assertRaises(Cancelled):
            uwp._run(["sleep", "30"], dict(os.environ), "A step", progress)
        stopped.assert_called_once()
        self.assertTrue(calls)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.paths = _Paths(self)
        self.dest = self.paths.games / "uwp" / "1.21.51.1"
        self.entry = {"version": "1.21.51.1", "urls": [],
                      "update_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}
        self.payload = b"appx bytes"
        digest = base64.b64encode(hashlib.sha1(self.payload).digest())
        self.digest = digest.decode()
        for target, kwargs in (
                ("prepare", {}),
                ("ensure_store_engagement", {}),
                ("prefix_ready", {"return_value": True}),
                ("windows_files_ready", {"return_value": True}),
                ("_free_space", {"return_value": 100 << 30}),
                ("host_problem", {"return_value": None})):
            patcher = mock.patch.object(uwp, target, **kwargs)
            setattr(self, target.strip("_"), patcher.start())
            self.addCleanup(patcher.stop)

    def _download(self, payload=None):
        def fake(url, path, label, progress=None):
            # bol.util.download makes the folder it writes into.
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(self.payload if payload is None
                                   else payload)
        return fake

    def _install_step(self, cmd, env, what, progress=None, **kwargs):
        package, data = Path(cmd[2]), Path(cmd[3])
        self.assertTrue(package.is_file())
        stage = data / "stage" / cmd[cmd.index("--stage") + 1]
        _write_build(stage)
        return []

    def test_the_package_is_checked_installed_and_moved_into_place(self):
        with mock.patch.object(uwp, "require_ownership") as owned, \
                mock.patch.object(uwp, "_package_url",
                                  return_value=(self.digest, "http://x/p")), \
                mock.patch.object(uwp, "download",
                                  side_effect=self._download()), \
                mock.patch.object(uwp, "_run",
                                  side_effect=self._install_step) as ran:
            uwp.install(UWP, self.entry, self.dest)
        owned.assert_called_once_with(UWP)
        cmd = ran.call_args[0][0]
        self.assertEqual(cmd[:2], ["wine", "wineuwp-install"])
        self.assertEqual(cmd[cmd.index("--category") + 1],
                         UWP["wu_category"])
        self.assertTrue((self.dest / "Minecraft.Windows.exe").is_file())
        data = uwp.install_data()
        self.assertFalse((data / "stage" / "1.21.51.1").exists())
        self.assertFalse(list(data.glob("*.appx*")))

    def test_ownership_comes_before_any_download(self):
        with mock.patch.object(uwp, "require_ownership",
                               side_effect=xodus.NotOwned("no copy")), \
                mock.patch.object(uwp, "_package_url") as asked, \
                mock.patch.object(uwp, "download") as downloaded, \
                self.assertRaises(xodus.NotOwned):
            uwp.install(UWP, self.entry, self.dest)
        asked.assert_not_called()
        downloaded.assert_not_called()
        self.prepare.assert_not_called()

    def test_a_package_that_is_not_the_one_published_is_not_installed(self):
        with mock.patch.object(uwp, "require_ownership"), \
                mock.patch.object(uwp, "_package_url",
                                  return_value=(self.digest, "http://x/p")), \
                mock.patch.object(uwp, "download",
                                  side_effect=self._download(b"tampered")), \
                mock.patch.object(uwp, "_run") as ran, \
                self.assertRaisesRegex(uwp.UwpError, "does not match"):
            uwp.install(UWP, self.entry, self.dest)
        ran.assert_not_called()
        self.assertFalse(list(uwp.install_data().glob("*.appx*")))
        self.assertFalse(self.dest.exists())

    def test_another_package_than_minecraft_is_refused(self):
        def other(cmd, env, what, progress=None, **kwargs):
            stage = Path(cmd[3]) / "stage" / "1.21.51.1"
            _write_build(stage, manifest='<Identity Name="Other.App" />')
            return []

        with mock.patch.object(uwp, "require_ownership"), \
                mock.patch.object(uwp, "_package_url",
                                  return_value=(self.digest, "http://x/p")), \
                mock.patch.object(uwp, "download",
                                  side_effect=self._download()), \
                mock.patch.object(uwp, "_run", side_effect=other), \
                self.assertRaisesRegex(uwp.UwpError, "did not install"):
            uwp.install(UWP, self.entry, self.dest)
        self.assertFalse(self.dest.exists())

    def test_too_little_room_is_said_before_the_download(self):
        self.free_space.return_value = 4 << 30
        self.prefix_ready.return_value = False
        with mock.patch.object(uwp, "require_ownership"), \
                mock.patch.object(uwp, "download") as downloaded, \
                self.assertRaisesRegex(uwp.UwpError, "8 GiB"):
            uwp.install(UWP, self.entry, self.dest)
        downloaded.assert_not_called()

    def test_the_package_url_is_the_signed_one_not_the_block_map(self):
        with mock.patch("bol.wustore.file_urls", return_value={
                "bWFw": "http://tlu.example/blockmap",
                "cGtn": "http://tlu.example/file?P1=1&P2=2"}):
            self.assertEqual(uwp._package_url("id"),
                             ("cGtn", "http://tlu.example/file?P1=1&P2=2"))
        with mock.patch("bol.wustore.file_urls",
                        return_value={"bWFw": "http://tlu.example/map"}), \
                self.assertRaisesRegex(uwp.UwpError, "no longer serves"):
            uwp._package_url("id")

    def test_games_installs_them_through_uwp(self):
        catalogue = [self.entry]
        with mock.patch.object(games, "GAMES", self.paths.games), \
                mock.patch.object(games, "load_settings", return_value={}), \
                mock.patch.object(games, "list_versions",
                                  return_value=catalogue), \
                mock.patch.object(
                    games.uwp, "install",
                    side_effect=lambda edition, entry, dest, progress=None:
                        _write_build(Path(dest))) as install:
            root = games.install_game(UWP)
        self.assertEqual(root, self.dest)
        self.assertEqual(install.call_args[0][1], self.entry)


class StoreEngagementTests(unittest.TestCase):
    def test_the_framework_is_unpacked_from_the_sdks_appx(self):
        _Paths(self)
        appx = io.BytesIO()
        with zipfile.ZipFile(appx, "w") as archive:
            archive.writestr("AppxManifest.xml", (
                '<Package><Identity Name="Microsoft.Services.Store.Engagement"'
                ' Version="10.0.19011.0" ProcessorArchitecture="x64" />'
                '</Package>'))
            archive.writestr("Microsoft.Services.Store.Engagement.dll", b"MZ")
        package = appx.getvalue()
        def fetched(url, path, *args, **kwargs):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"msi")

        with mock.patch.object(uwp, "_fetch_pinned", side_effect=fetched), \
                mock.patch("bol.gameinput._msi_embedded_cab",
                           return_value=b"cab"), \
                mock.patch("bol.gameinput._cab_payload",
                           return_value=[(3, b"x86"), (len(package),
                                                       package)]), \
                mock.patch.object(uwp, "STORE_ENGAGEMENT_APPX_SHA256",
                                  hashlib.sha256(package).hexdigest()):
            fw = uwp.ensure_store_engagement()
            target = fw / ("Microsoft.Services.Store.Engagement_10.0.19011.0_"
                           "x64__8wekyb3d8bbwe")
            self.assertTrue((target / "AppxManifest.xml").is_file())
            self.assertTrue(
                (target / "Microsoft.Services.Store.Engagement.dll").is_file())
            # Once there, nothing is downloaded again.
            with mock.patch.object(uwp, "_fetch_pinned") as fetched:
                uwp.ensure_store_engagement()
            fetched.assert_not_called()


class LaunchTests(unittest.TestCase):
    def test_a_copy_the_account_does_not_own_never_starts(self):
        with tempfile.TemporaryDirectory() as td:
            build = _write_build(Path(td) / "uwp" / "1.21.51.1")
            with mock.patch.object(launch.uwp, "require_ownership",
                                   side_effect=xodus.NotOwned("no copy")), \
                    mock.patch.object(launch.subprocess, "Popen") as popen, \
                    mock.patch.object(launch, "arm_gpu_launch") as armed, \
                    self.assertRaises(xodus.NotOwned):
                launch._launch_uwp((), UWP, str(build),
                                   build / "Minecraft.Windows.exe")
            popen.assert_not_called()
            armed.assert_not_called()

    def test_the_editor_is_not_offered(self):
        with mock.patch.object(launch.uwp, "require_ownership") as owned, \
                self.assertRaises(BolError):
            launch._launch_uwp((), UWP, "/x", Path("/x/Minecraft.Windows.exe"),
                               editor=True)
        owned.assert_not_called()

    def test_a_uwp_build_is_started_by_its_own_path(self):
        with mock.patch.object(launch, "load_settings", return_value={}), \
                mock.patch.object(launch, "_selected_game",
                                  return_value=(UWP, "/g", Path("/g/x.exe"))), \
                mock.patch.object(launch, "_launch_uwp",
                                  return_value=0) as started, \
                mock.patch.object(launch, "proton_path") as proton:
            self.assertEqual(launch._launch_once(), 0)
        started.assert_called_once()
        proton.assert_not_called()


if __name__ == "__main__":
    unittest.main()
