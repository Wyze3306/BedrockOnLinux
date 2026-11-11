"""Regression tests for the macOS port.

These run on Linux: every one of them simulates macOS by patching the
``IS_MAC`` binding of the module under test, which is exactly the switch the
port is built on. What they check is that the switch is actually consulted --
that a Mac gets its own data directory, its own Windows runtime, its own
shortcut format, and a clear refusal (never a Linux path silently taken) for
the two things macOS genuinely cannot do.
"""
# SPDX-License-Identifier: MIT

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import gamesetup, perfcheck, platform as bolplatform, profiles
from bol import prefix as bolprefix
from bol import update, winemac, xodus


class PlatformSeamTests(unittest.TestCase):
    def test_mac_data_home_is_application_support(self):
        with mock.patch.object(bolplatform, "IS_MAC", True), \
                mock.patch.object(Path, "home",
                                  return_value=Path("/Users/someone")):
            self.assertEqual(
                bolplatform.data_home("bedrock-on-linux"),
                Path("/Users/someone/Library/Application Support/"
                     "bedrock-on-linux"))
            self.assertEqual(
                bolplatform.config_home("bedrock-on-linux"),
                Path("/Users/someone/Library/Application Support/"
                     "bedrock-on-linux"))

    def test_linux_data_home_still_follows_xdg(self):
        with mock.patch.object(bolplatform, "IS_MAC", False), \
                mock.patch.dict(os.environ,
                                {"XDG_DATA_HOME": "/xdg/data"}, clear=False):
            self.assertEqual(bolplatform.data_home("app"),
                             Path("/xdg/data/app"))

    def test_package_manager_hint_is_brew_on_mac(self):
        with mock.patch.object(bolplatform, "IS_MAC", True):
            self.assertEqual(bolplatform.pm_hint(), "brew install {}")

    def test_a_mac_always_has_a_display(self):
        """A .app double-click carries no DISPLAY, and must still open."""
        with mock.patch.object(bolplatform, "IS_MAC", True), \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(bolplatform.has_display())
        with mock.patch.object(bolplatform, "IS_MAC", False), \
                mock.patch.object(bolplatform, "IS_LINUX", True), \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(bolplatform.has_display())

    def test_no_proc_scan_off_linux(self):
        """process_environ has no answer on macOS, and must say so rather
        than pretend a process is not ours."""
        with mock.patch.object(bolplatform, "IS_LINUX", False):
            self.assertIsNone(bolplatform.process_environ(os.getpid()))

    def test_flatpak_is_never_true_off_linux(self):
        with mock.patch.object(bolplatform, "IS_LINUX", False), \
                mock.patch.dict(os.environ, {"FLATPAK_ID": "x"}, clear=False):
            self.assertFalse(bolplatform.in_flatpak())


class WineBackendDetectionTests(unittest.TestCase):
    """The order matters: only GPTK and CrossOver translate Direct3D to
    Metal, so a plain Wine found first would silently cost all the frames."""

    def _fake_wine(self, root, name):
        binary = Path(root) / name
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        return binary

    def test_game_porting_toolkit_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            gptk = self._fake_wine(Path(directory) / "gptk/bin", "wine64")
            crossover = self._fake_wine(Path(directory) / "cx", "wine")
            with mock.patch.object(winemac, "_gptk_wine", return_value=gptk), \
                    mock.patch.object(winemac, "_CROSSOVER_WINE",
                                      str(crossover)), \
                    mock.patch.object(winemac, "load_settings",
                                      return_value={}), \
                    mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(winemac.detect_wine(), ("gptk", gptk))

    def test_crossover_beats_whisky_and_plain_wine(self):
        with tempfile.TemporaryDirectory() as directory:
            crossover = self._fake_wine(Path(directory) / "cx", "wine")
            whisky = self._fake_wine(Path(directory) / "whisky", "wine")
            with mock.patch.object(winemac, "_gptk_wine", return_value=None), \
                    mock.patch.object(winemac, "_CROSSOVER_WINE",
                                      str(crossover)), \
                    mock.patch.object(winemac, "_WHISKY_WINE", whisky), \
                    mock.patch.object(winemac, "load_settings",
                                      return_value={}), \
                    mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(winemac.detect_wine(),
                                 ("crossover", Path(crossover)))

    def test_an_explicit_wine_overrides_every_detected_one(self):
        with tempfile.TemporaryDirectory() as directory:
            chosen = self._fake_wine(Path(directory) / "mine", "wine")
            gptk = self._fake_wine(Path(directory) / "gptk/bin", "wine64")
            with mock.patch.object(winemac, "_gptk_wine", return_value=gptk), \
                    mock.patch.object(winemac, "load_settings",
                                      return_value={}), \
                    mock.patch.dict(os.environ, {"BOL_WINE": str(chosen)},
                                    clear=True):
                self.assertEqual(winemac.detect_wine(), ("custom", chosen))

    def test_nothing_installed_reports_nothing(self):
        with mock.patch.object(winemac, "_gptk_wine", return_value=None), \
                mock.patch.object(winemac, "_CROSSOVER_WINE", "/nope/wine"), \
                mock.patch.object(winemac, "_WHISKY_WINE", Path("/nope/w")), \
                mock.patch.object(winemac, "_WINE_CASKS", ()), \
                mock.patch.object(winemac.shutil, "which", return_value=None), \
                mock.patch.object(winemac, "load_settings", return_value={}), \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(winemac.detect_wine(), (None, None))


class WineCommandTests(unittest.TestCase):
    def test_the_command_is_the_wine_and_the_program(self):
        with tempfile.TemporaryDirectory() as directory:
            wine = Path(directory) / "wine"
            wine.write_text("")
            prefix = Path(directory) / "pfx"
            with mock.patch.object(winemac, "IS_MAC", True), \
                    mock.patch.object(winemac, "wine_bin", return_value=wine), \
                    mock.patch.object(winemac, "load_settings",
                                      return_value={"wine_backend": "gptk"}), \
                    mock.patch.dict(os.environ, {}, clear=True):
                argv, env = winemac.wine_cmd("wineboot", prefix=prefix)
        self.assertEqual(argv, [str(wine), "wineboot"])
        self.assertEqual(env["WINEPREFIX"], str(prefix))
        self.assertEqual(env["WINEARCH"], "win64")
        # D3DMetal's shaders need AVX, which Rosetta hides unless asked.
        self.assertEqual(env["ROSETTA_ADVERTISE_AVX"], "1")
        self.assertEqual(env["WINEMSYNC"], "1")

    def test_the_host_environment_still_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            wine = Path(directory) / "wine"
            wine.write_text("")
            with mock.patch.object(winemac, "IS_MAC", True), \
                    mock.patch.object(winemac, "wine_bin", return_value=wine), \
                    mock.patch.object(winemac, "load_settings",
                                      return_value={"wine_backend": "gptk"}), \
                    mock.patch.dict(os.environ, {"WINEMSYNC": "0"},
                                    clear=True):
                _argv, env = winemac.wine_cmd(
                    "wineboot", prefix=Path(directory) / "pfx")
        self.assertEqual(env["WINEMSYNC"], "0")

    def test_a_plain_wine_asks_for_no_metal_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            wine = Path(directory) / "wine"
            wine.write_text("")
            with mock.patch.object(winemac, "IS_MAC", True), \
                    mock.patch.object(winemac, "wine_bin", return_value=wine), \
                    mock.patch.object(winemac, "load_settings",
                                      return_value={"wine_backend": "wine"}), \
                    mock.patch.dict(os.environ, {}, clear=True):
                _argv, env = winemac.wine_cmd(
                    "wineboot", prefix=Path(directory) / "pfx")
        self.assertNotIn("ROSETTA_ADVERTISE_AVX", env)
        self.assertNotIn("WINEMSYNC", env)


class PrefixBusyTests(unittest.TestCase):
    """macOS cannot read another process's WINEPREFIX, so idleness is decided
    by wineserver's own lock file. fcntl behaves the same on both platforms,
    so this exercises the real mechanism rather than a stand-in."""

    def test_no_lock_file_means_idle(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "pfx"
            prefix.mkdir()
            self.assertFalse(winemac.prefix_busy(prefix))

    # Wine takes a POSIX record lock (fcntl F_SETLK), not flock(2); on Linux
    # the two do not conflict with each other, so the stand-in server here has
    # to hold the same kind the probe tests for.
    _HOLDER = (
        "import fcntl, sys, time\n"
        "handle = open(sys.argv[1], 'r+b')\n"
        "fcntl.lockf(handle, fcntl.LOCK_EX)\n"
        "sys.stdout.write('locked\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(60)\n"
    )

    def test_a_held_lock_means_busy(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "pfx"
            prefix.mkdir()
            server = Path(directory) / "server"
            server.mkdir()
            (server / "lock").write_bytes(b"")
            holder = subprocess.Popen(
                [sys.executable, "-c", self._HOLDER, str(server / "lock")],
                stdout=subprocess.PIPE, text=True)
            self.addCleanup(holder.kill)
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            with mock.patch.object(winemac, "server_dir",
                                   return_value=server):
                self.assertTrue(winemac.prefix_busy(prefix))
                holder.kill()
                holder.wait()
                self.assertFalse(winemac.prefix_busy(prefix))

    def test_a_prefix_that_does_not_exist_has_no_server(self):
        self.assertIsNone(winemac.server_dir("/nonexistent/prefix"))


class EngineDispatchTests(unittest.TestCase):
    def test_a_mac_launch_goes_through_the_native_wine(self):
        with mock.patch.object(bolprefix, "IS_MAC", True), \
                mock.patch.object(winemac, "wine_cmd",
                                  return_value=(["wine", "x.exe"], {})) as call:
            argv, _env = bolprefix.engine_cmd("x.exe", prefix="/tmp/pfx")
        self.assertEqual(argv, ["wine", "x.exe"])
        call.assert_called_once_with("x.exe", prefix="/tmp/pfx")

    def test_a_linux_launch_still_goes_through_umu(self):
        with mock.patch.object(bolprefix, "IS_MAC", False), \
                mock.patch.object(bolprefix, "proton_umu_cmd",
                                  return_value=(["umu"], {})) as call:
            argv, _env = bolprefix.engine_cmd("x.exe")
        self.assertEqual(argv, ["umu"])
        call.assert_called_once_with("x.exe", prefix=None)

    def test_umu_is_never_downloaded_on_a_mac(self):
        with mock.patch.object(bolprefix, "IS_MAC", True):
            with self.assertRaises(bolprefix.BolError):
                bolprefix.ensure_umu()

    def test_the_steam_runtime_is_never_pending_on_a_mac(self):
        with mock.patch.object(bolprefix, "IS_MAC", True):
            self.assertFalse(bolprefix.runtime_setup_pending())


class StoreDownloaderTests(unittest.TestCase):
    """xodus-cli is a Linux binary that links WebKitGTK. macOS has to be told
    that, in one message, at every door that leads to it."""

    def test_the_downloader_is_reported_absent(self):
        with mock.patch.object(xodus, "IS_MAC", True):
            self.assertFalse(xodus.cli_available())

    def test_installing_it_refuses_with_the_reason(self):
        with mock.patch.object(xodus, "IS_MAC", True):
            with self.assertRaises(xodus.XodusError) as caught:
                xodus.ensure_cli()
        self.assertIn("has no macOS build", str(caught.exception))
        self.assertIn("already have", str(caught.exception))


class MacSetupTests(unittest.TestCase):
    def test_setup_without_a_game_folder_names_the_way_to_add_one(self):
        with mock.patch.object(gamesetup, "IS_MAC", True), \
                mock.patch.object(gamesetup, "mkdirs"), \
                mock.patch.object(gamesetup, "prune_legacy_game_archives"), \
                mock.patch.object(gamesetup, "ensure_login_deps"), \
                mock.patch.object(gamesetup, "load_settings",
                                  return_value={}), \
                mock.patch.object(gamesetup, "installed_builds",
                                  return_value=[]), \
                mock.patch.object(gamesetup, "_game_root", return_value=None):
            with self.assertRaises(gamesetup.NoGameFolder) as caught:
                gamesetup._do_setup()
        self.assertIn("Use a Minecraft folder", str(caught.exception))
        self.assertIn("setup --game-dir", str(caught.exception))

    def test_a_build_the_mac_cannot_download_falls_back_to_its_own(self):
        """The picker lists every build Microsoft has; PLAY on a Mac starts
        the one that is there instead of trying to download the one picked."""
        with tempfile.TemporaryDirectory() as directory:
            own = Path(directory) / "own"
            own.mkdir()
            settings = {"game_dir": str(own)}
            with mock.patch.object(gamesetup, "load_settings",
                                   return_value=settings), \
                    mock.patch.object(gamesetup, "version_dir",
                                      return_value=Path(directory) / "none"), \
                    mock.patch.object(gamesetup, "_game_root",
                                      side_effect=lambda p: p if Path(p) == own
                                      else None), \
                    mock.patch.object(gamesetup, "mc_version_str",
                                      return_value="1.26.45.1"), \
                    mock.patch.object(gamesetup, "info") as said, \
                    mock.patch.object(gamesetup, "install_game") as download:
                root = gamesetup._mac_build({"id": "release"}, "1.26.52.3")
        self.assertEqual(root, own)
        download.assert_not_called()
        self.assertIn("1.26.52.3", said.call_args[0][0])
        self.assertIn("1.26.45.1", said.call_args[0][0])

    def test_setup_prepares_the_native_wine_and_no_proton(self):
        with tempfile.TemporaryDirectory() as directory:
            game = Path(directory)
            settings = {"game_dir": str(game), "proton_source": "winegdk"}
            with mock.patch.object(gamesetup, "IS_MAC", True), \
                    mock.patch.object(gamesetup, "mkdirs"), \
                    mock.patch.object(gamesetup, "ensure_login_deps"), \
                    mock.patch.object(gamesetup, "load_settings",
                                      return_value=settings), \
                    mock.patch.object(gamesetup, "_game_root",
                                      return_value=game), \
                    mock.patch.object(gamesetup, "fix_curl_ssl"), \
                    mock.patch.object(gamesetup, "boot_prefix",
                                      return_value=True), \
                    mock.patch.object(gamesetup, "active_prefix",
                                      return_value=Path(directory)), \
                    mock.patch.object(gamesetup, "install_gameinput"), \
                    mock.patch.object(gamesetup, "hide_signin_button"), \
                    mock.patch.object(gamesetup, "ok"), \
                    mock.patch.object(gamesetup, "ensure_winegdk") as winegdk, \
                    mock.patch.object(gamesetup, "ensure_proton") as proton, \
                    mock.patch.object(gamesetup, "ensure_umu") as umu, \
                    mock.patch.object(winemac, "install_gdk_runtime") as gdk, \
                    mock.patch.object(winemac, "ensure_wine") as wine:
                gamesetup._do_setup()
        wine.assert_called_once()
        gdk.assert_called_once_with(Path(directory))
        winegdk.assert_not_called()
        proton.assert_not_called()
        umu.assert_not_called()


class MacShortcutTests(unittest.TestCase):
    def test_the_shortcut_is_an_executable_command_script(self):
        with tempfile.TemporaryDirectory() as directory:
            apps = Path(directory) / "Applications"
            with mock.patch.object(profiles, "IS_MAC", True), \
                    mock.patch.object(profiles, "launcher_executable",
                                      return_value="/Applications/BOL.app/"
                                                   "Contents/MacOS/bol"):
                entry = profiles.write_play_shortcut(applications_dir=apps)
            self.assertEqual(entry.suffix, ".command")
            self.assertTrue(os.access(entry, os.X_OK))
            body = entry.read_text()
            self.assertTrue(body.startswith("#!/bin/sh\n"))
            self.assertIn("Contents/MacOS/bol", body)
            self.assertIn(" play", body)

    def test_a_renamed_profile_leaves_no_orphan_shortcut(self):
        apps = Path("/Applications")
        with mock.patch.object(profiles, "IS_MAC", True):
            names = profiles._profile_shortcut_paths(apps, "alex", "Alex")
        self.assertEqual([p.suffix for p in names], [".command", ".command"])
        self.assertTrue(all("Alex" in p.name for p in names))

    def test_linux_shortcuts_are_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            apps = Path(directory) / "applications"
            with mock.patch.object(profiles, "IS_MAC", False), \
                    mock.patch.object(profiles, "launcher_executable",
                                      return_value="/usr/bin/bedrock-on-linux"):
                entry = profiles.write_play_shortcut(applications_dir=apps)
            self.assertEqual(entry.suffix, ".desktop")
            self.assertIn("[Desktop Entry]", entry.read_text())


class MacUpdateTests(unittest.TestCase):
    def test_an_app_bundle_is_replaced_whole_not_patched(self):
        bundle = Path("/Applications/BedrockOnLinux.app/Contents/MacOS/bol")
        with mock.patch.object(update, "IS_MAC", True), \
                mock.patch.object(update, "_self_path", return_value=bundle), \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(update.update_kind(), "system")
            state, message = update.self_update(
                {"version": "9.9.9", "url": "https://example.invalid/r"})
        self.assertEqual(state, "system")
        self.assertIn("Applications", message)


class MacPerformanceReadingTests(unittest.TestCase):
    _VM_STAT = (
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        "Pages free:                             1000.\n"
        "Pages active:                          50000.\n"
        "Pages inactive:                         2000.\n"
        "Pages speculative:                       500.\n"
        "Pages purgeable:                         100.\n"
    )

    def test_available_memory_is_summed_from_vm_stat(self):
        with mock.patch.object(perfcheck, "IS_MAC", True), \
                mock.patch.object(perfcheck, "_run_text",
                                  return_value=self._VM_STAT):
            # (1000 + 2000 + 500 + 100) pages × 16 KiB = 56 MiB
            self.assertEqual(perfcheck.available_memory_mib(), 56)

    def test_swap_in_use_is_read_from_sysctl(self):
        usage = "total = 2048.00M  used = 512.00M  free = 1536.00M\n"
        with mock.patch.object(perfcheck, "IS_MAC", True), \
                mock.patch.object(perfcheck, "_run_text", return_value=usage):
            self.assertEqual(perfcheck.swap_used_mib(), 512)

    def test_an_unreadable_host_answers_nothing_rather_than_zero(self):
        with mock.patch.object(perfcheck, "IS_MAC", True), \
                mock.patch.object(perfcheck, "_run_text", return_value=None):
            self.assertIsNone(perfcheck.available_memory_mib())
            self.assertIsNone(perfcheck.swap_used_mib())

    def test_an_explicit_meminfo_path_still_uses_the_linux_parser(self):
        with tempfile.TemporaryDirectory() as directory:
            meminfo = Path(directory) / "meminfo"
            meminfo.write_text("MemAvailable:    2097152 kB\n")
            with mock.patch.object(perfcheck, "IS_MAC", True):
                self.assertEqual(
                    perfcheck.available_memory_mib(str(meminfo)), 2048)

    def test_a_mac_desktop_always_composites(self):
        with mock.patch.object(perfcheck, "IS_MAC", True):
            self.assertTrue(perfcheck.session_is_composited())


class MacRouteCheckTests(unittest.TestCase):
    """`doctor --host <ip>` asks which interface reaches a LAN host. iproute2
    answers on Linux; macOS has BSD route(8), which prints something else
    entirely."""

    _ROUTE_OUTPUT = (
        "   route to: 192.168.1.20\n"
        "destination: 192.168.1.0\n"
        "       mask: 255.255.255.0\n"
        "    gateway: 192.168.1.1\n"
        "  interface: en0\n"
        "      flags: <UP,DONE,CLONING>\n"
    )

    def _runner(self, argv, **_kwargs):
        self.argv = argv
        return subprocess.CompletedProcess(
            argv, 0, stdout=self._ROUTE_OUTPUT, stderr="")

    def test_the_bsd_route_tool_is_used_and_parsed(self):
        from bol import network

        with mock.patch.object(network, "IS_MAC", True):
            check = network._route_check("192.168.1.20", self._runner, 5)
        self.assertEqual(self.argv[:3], ["/sbin/route", "-n", "get"])
        self.assertTrue(check.ok)
        self.assertIn("interface=en0", check.detail)
        self.assertIn("gateway=192.168.1.1", check.detail)

    def test_linux_still_asks_iproute2(self):
        from bol import network

        def runner(argv, **_kwargs):
            self.argv = argv
            return subprocess.CompletedProcess(
                argv, 0, stdout="192.168.1.20 dev eth0 src 192.168.1.9\n",
                stderr="")

        with mock.patch.object(network, "IS_MAC", False):
            check = network._route_check("192.168.1.20", runner, 5)
        self.assertEqual(self.argv[:3], ["ip", "route", "get"])
        self.assertIn("interface=eth0", check.detail)
        self.assertIn("source=192.168.1.9", check.detail)


class MacLaunchRefusalTests(unittest.TestCase):
    """An encrypted Store package cannot start here, and the launcher has to
    say which folder to change instead of handing Wine ciphertext."""

    def test_an_encrypted_build_is_refused_by_name(self):
        from bol import launch

        with tempfile.TemporaryDirectory() as directory:
            content = Path(directory) / "content"
            content.mkdir()
            (content / "Minecraft.Windows.exe").write_bytes(b"MZ")
            with mock.patch.object(launch, "IS_MAC", True), \
                    mock.patch.object(launch, "CONTENT", content), \
                    mock.patch.object(
                        launch, "load_settings",
                        return_value={"game_dir": str(content)}), \
                    mock.patch.object(launch, "proton_path",
                                      return_value=Path("/tmp/wine")), \
                    mock.patch.object(launch, "_prepare_launch_engine"), \
                    mock.patch.object(launch,
                                      "retire_idle_current_boot_marker"), \
                    mock.patch.object(launch,
                                      "require_safe_graphics_session"), \
                    mock.patch.object(launch, "_warn_if_performance_degraded"), \
                    mock.patch.object(launch, "msa_session_snapshot",
                                      return_value=({}, "a" * 32)), \
                    mock.patch.object(launch, "boot_prefix",
                                      return_value=True), \
                    mock.patch.object(launch, "wine_apply_winegdk_prereqs"), \
                    mock.patch.object(launch, "_install_cryptbase_in_prefix"), \
                    mock.patch.object(launch, "install_gameinput"), \
                    mock.patch.object(launch, "active_prefix",
                                      return_value=Path(directory)), \
                    mock.patch.object(launch.xodus, "exe_is_encrypted",
                                      return_value=True), \
                    mock.patch.object(launch, "warn"), \
                    mock.patch.object(launch, "info"):
                with self.assertRaises(launch.BolError) as caught:
                    launch._launch_once()
        self.assertIn("Microsoft Store package", str(caught.exception))
        self.assertIn("no macOS build", str(caught.exception))


def _fake_crossover(root):
    """A stand-in for CrossOver's bin/wine: a script that locates CX_ROOT."""
    cx = Path(root) / "CrossOver.app/Contents/SharedSupport/CrossOver"
    (cx / "bin").mkdir(parents=True)
    (cx / "etc").mkdir()
    wine = cx / "bin/wine"
    wine.write_text("#!/usr/bin/perl\n# locate_cx_root: sets $ENV{CX_ROOT}\n")
    wine.chmod(0o755)
    (cx / "etc/CrossOver.conf").write_text(
        '[CrossOver]\n"BuildTimestamp" = "20260715T103801Z"\n')
    return wine


class CrossOverBottleTests(unittest.TestCase):
    """CrossOver's bin/wine runs bottles and overwrites WINEPREFIX. Without a
    bottle named "default" it refused to start; with one, it initialised
    that bottle while the launcher waited for its own prefix (wineboot
    "finished without valid system.reg, user.reg, and system32 state")."""

    def _command(self, directory, exe="wineboot", env=None):
        wine = _fake_crossover(directory)
        prefix = Path(directory) / "compatdata/pfx"
        with mock.patch.object(winemac, "IS_MAC", True), \
                mock.patch.object(winemac, "wine_bin", return_value=wine), \
                mock.patch.object(winemac, "load_settings",
                                  return_value={"wine_backend": "crossover"}), \
                mock.patch.dict(os.environ, env or {}, clear=True):
            argv, environ = winemac.wine_cmd(exe, prefix=prefix)
        return wine, prefix, argv, environ

    def test_the_prefix_is_handed_over_as_a_bottle_by_path(self):
        with tempfile.TemporaryDirectory() as directory:
            wine, prefix, argv, env = self._command(directory)
        # An absolute path is a bottle to CrossOver, used where it is.
        self.assertEqual(argv, [str(wine), "--bottle", str(prefix), "--wait",
                                "--", "wineboot"])
        self.assertEqual(env["CX_BOTTLE"], str(prefix))

    def test_the_prefix_is_described_as_a_win64_bottle(self):
        with tempfile.TemporaryDirectory() as directory:
            _wine, prefix, _argv, _env = self._command(directory)
            conf = (prefix / "cxbottle.conf").read_text()
        self.assertIn('"WineArch" = "win64"', conf)
        # CrossOver's own timestamp: no upgrade pass on a bottle just made.
        self.assertIn('"Timestamp" = "20260715T103801Z"', conf)
        # Nothing published to the Mac's menus or file associations.
        self.assertIn('"MenuMode" = "ignore"', conf)
        self.assertIn('"AssocMode" = "ignore"', conf)

    def test_an_existing_bottle_description_is_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "compatdata/pfx"
            prefix.mkdir(parents=True)
            (prefix / "cxbottle.conf").write_text("[Bottle]\nmine\n")
            self._command(directory)
            self.assertEqual((prefix / "cxbottle.conf").read_text(),
                             "[Bottle]\nmine\n")

    def test_an_inherited_crossover_session_does_not_skip_the_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            _wine, _prefix, _argv, env = self._command(
                directory, env={"CX_INITIALIZED": "501:default"})
        self.assertNotIn("CX_INITIALIZED", env)

    def test_the_dll_overrides_travel_on_the_command_line(self):
        # The front end deletes WINEDLLOVERRIDES and takes --dll instead.
        with tempfile.TemporaryDirectory() as directory:
            wine, prefix, argv, env = self._command(directory, exe="x.exe")
            env["WINEDLLOVERRIDES"] = "vrclient=;amd_ags_x64="
            final, final_env = winemac.finalize_cmd(argv, env)
        self.assertEqual(final, [str(wine), "--bottle", str(prefix), "--wait",
                                 "--dll", "vrclient=;amd_ags_x64=", "--",
                                 "x.exe"])
        self.assertNotIn("WINEDLLOVERRIDES", final_env)

    def test_a_plain_wine_keeps_its_overrides_in_the_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            wine = Path(directory) / "wine64"
            wine.write_bytes(b"\xcf\xfa\xed\xfe")
            cmd, env = winemac.finalize_cmd(
                [str(wine), "x.exe"], {"WINEDLLOVERRIDES": "a="})
        self.assertEqual(cmd, [str(wine), "x.exe"])
        self.assertEqual(env["WINEDLLOVERRIDES"], "a=")

    def test_the_front_end_is_recognised_from_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            wine = _fake_crossover(directory)
            self.assertTrue(winemac.is_crossover_wrapper(wine))
            other = Path(directory) / "wine64"
            other.write_text("#!/bin/sh\nexec wine64-preloader \"$@\"\n")
            self.assertFalse(winemac.is_crossover_wrapper(other))
            self.assertFalse(winemac.is_crossover_wrapper(
                Path(directory) / "missing"))

    def test_apple_silicon_asks_crossover_for_d3dmetal(self):
        # Minecraft is a Direct3D 12 game; "auto" or "dxmt" set by the player
        # in the custom environment still win, since this is a default.
        with mock.patch.object(winemac, "mac_arm", return_value=True):
            self.assertEqual(winemac._backend_env("crossover")
                             ["CX_GRAPHICS_BACKEND"], "d3dmetal")
        with mock.patch.object(winemac, "mac_arm", return_value=False):
            self.assertNotIn("CX_GRAPHICS_BACKEND",
                             winemac._backend_env("crossover"))

    def test_the_wrapper_reports_crossovers_own_version(self):
        report = subprocess.CompletedProcess(
            ["wine", "--version"], 0,
            stdout="Product Name: CrossOver\nPublic Version: 26.3.0\n",
            stderr="")
        with mock.patch.object(winemac.subprocess, "run",
                               return_value=report):
            self.assertEqual(winemac._version_text("wine"), "CrossOver 26.3.0")


class MacWineChoiceTests(unittest.TestCase):
    def test_a_recorded_wine_is_not_read_back_as_an_override(self):
        """Installing CrossOver after a plain Wine has to change something."""
        with tempfile.TemporaryDirectory() as directory:
            plain = Path(directory) / "wine"
            plain.write_text("")
            crossover = _fake_crossover(directory)
            settings = {"wine": str(plain), "wine_backend": "wine"}
            with mock.patch.object(winemac, "load_settings",
                                   return_value=settings), \
                    mock.patch.object(winemac, "_gptk_wine",
                                      return_value=None), \
                    mock.patch.object(winemac, "_CROSSOVER_WINE",
                                      str(crossover)), \
                    mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(winemac.detect_wine(),
                                 ("crossover", crossover))

    def test_a_plain_wine_gives_way_to_a_better_one_at_play(self):
        with tempfile.TemporaryDirectory() as directory:
            plain = Path(directory) / "wine"
            plain.write_text("")
            crossover = _fake_crossover(directory)
            settings = {"wine": str(plain), "wine_backend": "wine"}
            saved = {}
            with mock.patch.object(winemac, "IS_MAC", True), \
                    mock.patch.object(winemac, "rosetta_problem",
                                      return_value=None), \
                    mock.patch.object(winemac, "load_settings",
                                      side_effect=lambda: dict(settings)), \
                    mock.patch.object(winemac, "save_settings",
                                      side_effect=saved.update), \
                    mock.patch.object(winemac, "detect_wine",
                                      return_value=("crossover", crossover)), \
                    mock.patch.object(winemac, "ok"), \
                    mock.patch.object(winemac, "info"):
                self.assertEqual(winemac.ensure_wine(), crossover)
        self.assertEqual(saved["wine_backend"], "crossover")

    def test_a_named_wine_is_classified_by_what_it_is(self):
        with tempfile.TemporaryDirectory() as directory:
            crossover = _fake_crossover(directory)
            with mock.patch.object(winemac, "load_settings",
                                   return_value={}), \
                    mock.patch.dict(os.environ, {"BOL_WINE": str(crossover)},
                                    clear=True):
                self.assertEqual(winemac.detect_wine(),
                                 ("crossover", crossover))


class MacProcessScopeTests(unittest.TestCase):
    """CrossOver, Whisky and GPTK are shared by every bottle on the Mac.
    Matching their directory made "Force stop" and the clean-up after every
    wineboot SIGKILL the player's other Windows programs."""

    def test_only_the_launchers_own_processes_match(self):
        listing = subprocess.CompletedProcess(["ps"], 0, stdout=(
            "  10 /Apps/CrossOver/bin/wineloader C:\\\\windows\\\\explorer.exe\n"
            "  11 /usr/bin/perl /Apps/CrossOver/bin/wine --bottle /pfx --wait "
            "-- wineboot -u\n"
            "  12 /Apps/CrossOver/bin/wineloader winewrapper.exe --run -- "
            "/data/content/Minecraft.Windows.exe\n"
            "  13 /Apps/CrossOver/bin/wineloader C:\\\\Program Files\\\\Steam"
            "\\\\steam.exe\n"), stderr="")
        with mock.patch.object(winemac.subprocess, "run",
                               return_value=listing), \
                mock.patch.object(winemac, "_game_paths",
                                  return_value={"/data/content"}), \
                mock.patch.object(winemac, "wine_bin",
                                  return_value=Path("/Apps/CrossOver/bin/wine")):
            self.assertEqual(winemac.prefix_processes(Path("/pfx")), [11, 12])


class MacWineUserTests(unittest.TestCase):
    """Proton's Windows user is steamuser; a macOS Wine's is not, and every
    path to the game's data has to follow it."""

    def _settings(self, backend):
        return mock.patch.object(winemac, "load_settings",
                                 return_value={"wine_backend": backend})

    def test_crossover_names_its_user_crossover(self):
        with tempfile.TemporaryDirectory() as directory, \
                self._settings("crossover"), \
                mock.patch.object(winemac, "wine_bin", return_value=None):
            self.assertEqual(winemac.wine_user_name(directory), "crossover")

    def test_a_plain_wine_uses_the_macs_login(self):
        with tempfile.TemporaryDirectory() as directory, \
                self._settings("wine"), \
                mock.patch.object(winemac, "wine_bin", return_value=None), \
                mock.patch.dict(os.environ, {"USER": "thedoc"}):
            self.assertEqual(winemac.wine_user_name(directory), "thedoc")

    def test_the_profile_holding_minecraft_wins(self):
        with tempfile.TemporaryDirectory() as directory, \
                self._settings("crossover"), \
                mock.patch.object(winemac, "wine_bin", return_value=None):
            users = Path(directory) / "drive_c/users"
            (users / "crossover").mkdir(parents=True)
            (users / "thedoc/AppData/Roaming/Minecraft Bedrock").mkdir(
                parents=True)
            (users / "Public").mkdir()
            self.assertEqual(winemac.wine_user_name(directory), "thedoc")

    def test_paths_follow_the_user_on_a_mac_only(self):
        rel = "drive_c/users/steamuser/AppData/Roaming/Minecraft Bedrock"
        with mock.patch.object(bolprefix, "IS_MAC", False):
            self.assertEqual(bolprefix.in_wine_user(rel, "/pfx"), rel)
        with mock.patch.object(bolprefix, "IS_MAC", True), \
                mock.patch.object(winemac, "wine_user_name",
                                  return_value="crossover"):
            self.assertEqual(
                bolprefix.in_wine_user(rel, "/pfx"),
                "drive_c/users/crossover/AppData/Roaming/Minecraft Bedrock")


class OpenPathTests(unittest.TestCase):
    def test_a_folder_that_does_not_exist_is_reported_not_opened(self):
        with mock.patch.object(bolplatform.subprocess, "Popen") as popen:
            self.assertFalse(bolplatform.open_path("/no/such/folder"))
        popen.assert_not_called()

    def test_a_url_is_opened_without_looking_for_it_on_disk(self):
        with mock.patch.object(bolplatform.subprocess, "Popen") as popen:
            self.assertTrue(bolplatform.open_path("https://example.org"))
        popen.assert_called_once()

    def test_a_mac_opens_with_the_system_opener_by_path(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(bolplatform, "IS_MAC", True), \
                mock.patch.object(bolplatform.subprocess, "Popen") as popen:
            self.assertTrue(bolplatform.open_path(directory))
        self.assertEqual(popen.call_args[0][0], ["/usr/bin/open", directory])


class MacCurlTests(unittest.TestCase):
    def test_no_zstd_archive_is_unpacked_on_a_mac(self):
        """MSYS2's libcurl is a .pkg.tar.zst, and one DLL of a dozen."""
        from bol import fixups

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(fixups, "IS_MAC", True), \
                mock.patch.object(fixups, "_install_openssl_xcurl") as openssl, \
                mock.patch.object(fixups, "run") as run, \
                mock.patch.object(fixups, "download") as download, \
                mock.patch.object(fixups, "info"):
            game = Path(directory) / "game"
            game.mkdir()
            fixups.fix_curl_ssl(game)
            self.assertFalse((Path(directory) / "etc").exists())
        openssl.assert_called_once_with(game)
        run.assert_not_called()
        download.assert_not_called()


class MacImportTests(unittest.TestCase):
    """macOS cannot download Minecraft, so the player's own folder is copied
    in -- and the original is never touched."""

    def _game(self, root, encrypted=False):
        game = Path(root) / "Minecraft for Windows" / "Content"
        game.mkdir(parents=True)
        (game / "Minecraft.Windows.exe").write_bytes(
            b"\x00\x01ciphertext" if encrypted else b"MZ" + b"\0" * 64)
        (game / "appxmanifest.xml").write_text(
            '<Package><Identity Name="Microsoft.MinecraftUWP" '
            'Version="1.26.4501.0" /></Package>')
        return game

    def _import(self, source, games):
        from bol import games as bolgames

        with mock.patch.object(bolgames, "GAMES", games), \
                mock.patch.object(bolgames, "IS_MAC", True), \
                mock.patch.object(bolgames, "info"), \
                mock.patch.object(bolgames, "ok"):
            return bolgames.import_game_folder(source)

    def test_the_folder_is_copied_under_its_edition_and_version(self):
        with tempfile.TemporaryDirectory() as directory:
            game = self._game(directory)
            games = Path(directory) / "data/games"
            before = sorted(p.name for p in game.iterdir())
            root = self._import(game.parent, games)
            self.assertEqual(root, games / "release" / "1.26.45.1")
            self.assertTrue((root / "Minecraft.Windows.exe").is_file())
            self.assertEqual(sorted(p.name for p in game.iterdir()), before)
            self.assertEqual(list(games.glob("release/.import-*")), [])

    def test_an_encrypted_executable_is_refused_by_name(self):
        with tempfile.TemporaryDirectory() as directory:
            game = self._game(directory, encrypted=True)
            with self.assertRaises(gamesetup.BolError) as caught:
                self._import(game, Path(directory) / "games")
        self.assertIn("encrypted", str(caught.exception))
        self.assertFalse((Path(directory) / "games").exists())

    def test_an_executable_without_its_manifest_is_explained(self):
        with tempfile.TemporaryDirectory() as directory:
            game = self._game(directory)
            (game / "appxmanifest.xml").unlink()
            with self.assertRaises(Exception) as caught:
                self._import(game, Path(directory) / "games")
        self.assertIn("appxmanifest.xml", str(caught.exception))

    def test_a_folder_without_minecraft_says_so(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(Exception) as caught:
                self._import(directory, Path(directory) / "games")
        self.assertIn("no Minecraft.Windows.exe", str(caught.exception))

    def test_the_command_line_can_import_too(self):
        from bol import cli

        with mock.patch.object(cli, "do_setup") as setup, \
                mock.patch.object(cli, "ok"), \
                mock.patch.object(sys, "argv",
                                  ["bol", "setup", "--game-dir", "/x/y"]):
            cli.main()
        setup.assert_called_once_with(mc_edition=None, mc_version=None,
                                      force=False, import_dir="/x/y")


class MacSwapGuardTests(unittest.TestCase):
    def test_no_systemd_scope_is_attempted_on_a_mac(self):
        from bol import swapguard

        which = mock.Mock(return_value="/usr/bin/systemd-run")
        with mock.patch.object(swapguard, "IS_LINUX", False):
            self.assertFalse(swapguard.swap_guard_available({}, which=which))
        which.assert_not_called()


class MacWindowTests(unittest.TestCase):
    """The folder a Mac plays from has to be reachable from the window: there
    was no way to choose one at all."""

    @classmethod
    def setUpClass(cls):
        from tests.guiharness import qt_app
        qt_app()

    def test_the_versions_tab_offers_a_folder_on_a_mac(self):
        from bol import gui
        from tests.guiharness import headless_window

        with mock.patch.object(gui, "IS_MAC", True), \
                headless_window() as window:
            tab = window._build_versions_tab()
            buttons = {b.text(): b for b in tab.findChildren(gui.QPushButton)}
            self.assertIn("Use a Minecraft folder…", buttons)
            # Clicked, not called: a click hands the slot `checked`, which
            # must not arrive as the callback to run afterwards.
            with mock.patch.object(gui.QFileDialog, "getExistingDirectory",
                                   return_value="/a/folder"), \
                    mock.patch.object(gui, "_mc_running", return_value=False), \
                    mock.patch.object(gui, "Worker") as worker, \
                    mock.patch.object(window, "_start_worker",
                                      return_value=True):
                buttons["Use a Minecraft folder…"].click()
            worker.assert_called_once_with(gui.import_game, "/a/folder")
            finished = worker.return_value.done.connect.call_args[0][0]
            with mock.patch.object(window, "refresh_builds"), \
                    mock.patch.object(window, "refresh_versions"):
                finished("/a/folder")

    def test_play_without_a_game_offers_the_folder_picker(self):
        from bol import gui
        from tests.guiharness import headless_window

        with headless_window() as window, \
                mock.patch.object(gui.QMessageBox, "exec"), \
                mock.patch.object(gui.QMessageBox, "clickedButton",
                                  return_value=None), \
                mock.patch.object(window, "_import_game_folder") as choose:
            window.ui_state["launch_active"] = True
            window._game_folder_needed("no game")
            self.assertFalse(window.ui_state["launch_active"])
            self.assertFalse(window.ui_state["busy"])
        choose.assert_called_once_with(then=window.do_play)

    def test_the_worker_reports_a_missing_game_apart_from_a_failure(self):
        from bol import gamesetup, gui

        worker = gui.LaunchWorker({"edition": {"id": "release"}, "tag": "1.0"})
        seen = {"failed": [], "folder": []}
        worker.failed.connect(seen["failed"].append)
        worker.needs_game_folder.connect(seen["folder"].append)
        with mock.patch.object(gui, "do_setup",
                               side_effect=gamesetup.NoGameFolder("none")):
            worker.run()
        self.assertEqual(seen, {"failed": [], "folder": ["none"]})

    def test_opening_a_folder_minecraft_has_not_made_yet_says_so(self):
        from bol import gui
        from tests.guiharness import headless_window

        with headless_window() as window, \
                mock.patch.object(gui, "game_content_dir",
                                  return_value=Path("/no/such/com.mojang")), \
                mock.patch.object(window, "info_box") as told:
            window._open_minecraft_folder()
        self.assertIn("first time it starts", told.call_args[0][1])


class MacGdkRuntimeTests(unittest.TestCase):
    """Minecraft quits with exit code 0 when no xgameruntime.dll is there,
    and dies on a C++ exception (exit 99) when WineGDK's has no threading
    sidecar to hand XThreading to. No macOS Wine ships either: the Mac takes
    both files of the Linux engine's runtime."""

    _DLL = (b"MZ" + b"\0" * 0x3e + b"Wine builtin DLL" + b"\0" * 0x30
            + b"PE\0\0rest of the image")
    _THREADING = b"MZ\x90\0Microsoft's own Gaming Runtime" + b"\0" * 64
    _NAMES = ("xgameruntime.dll", "xgameruntime.dll.threading")

    @staticmethod
    def _pin(data):
        import hashlib
        return hashlib.sha256(data).hexdigest()

    def _config(self, directory, dll=None, threading=None):
        from bol import config
        return mock.patch.multiple(
            config, CACHE=Path(directory) / "cache",
            WINEGDK_OUT=Path(directory) / "engine",
            MAC_GDK_RUNTIME_SHA256={
                "xgameruntime.dll": self._pin(dll or self._DLL),
                "xgameruntime.dll.threading": self._pin(
                    threading or self._THREADING)})

    def _engine(self, directory, dll=None, threading=None):
        folder = Path(directory) / "engine" / winemac._ENGINE_DLL_DIR
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "xgameruntime.dll").write_bytes(dll or self._DLL)
        (folder / "xgameruntime.dll.threading").write_bytes(
            threading or self._THREADING)
        return folder

    def test_both_files_are_installed_and_only_wines_is_unmarked(self):
        with tempfile.TemporaryDirectory() as directory, \
                self._config(directory):
            self._engine(directory)
            prefix = Path(directory) / "pfx"
            with mock.patch.object(winemac, "ok") as said, \
                    mock.patch.object(winemac, "update_prefix_registry",
                                      return_value=False):
                system32 = winemac.install_gdk_runtime(prefix)
                runtime = (system32 / "xgameruntime.dll").read_bytes()
                threading = (system32 / "xgameruntime.dll.threading").read_bytes()
                # Unchanged the second time, and not rewritten.
                before = [(system32 / name).stat().st_mtime_ns
                          for name in self._NAMES]
                winemac.install_gdk_runtime(prefix)
                after = [(system32 / name).stat().st_mtime_ns
                         for name in self._NAMES]
        self.assertEqual(system32, prefix / "drive_c/windows/system32")
        self.assertNotIn(b"Wine builtin DLL", runtime)
        self.assertEqual(len(runtime), len(self._DLL))
        self.assertIn(b"PE\0\0rest of the image", runtime)
        # Microsoft's file is not a Wine builtin, and goes in byte for byte.
        self.assertEqual(threading, self._THREADING)
        self.assertEqual(before, after)
        self.assertEqual(said.call_count, 1)

    def test_the_engine_on_this_machine_is_used_when_it_matches(self):
        with tempfile.TemporaryDirectory() as directory, \
                self._config(directory):
            folder = self._engine(directory)
            with mock.patch.object(winemac, "_stream_archive_members") as fetch:
                found = winemac.gdk_runtime_files()
            fetch.assert_not_called()
        self.assertEqual(found, {name: folder / name for name in self._NAMES})

    def test_only_the_file_that_is_missing_is_read_from_the_archive(self):
        with tempfile.TemporaryDirectory() as directory, \
                self._config(directory):
            folder = self._engine(directory)
            (folder / "xgameruntime.dll.threading").unlink()
            member = f"{winemac._ENGINE_DLL_DIR}/xgameruntime.dll.threading"
            with mock.patch.object(
                    winemac, "_stream_archive_members",
                    return_value={member: self._THREADING}) as fetch, \
                    mock.patch.object(winemac, "info"):
                found = winemac.gdk_runtime_files()
            self.assertEqual(fetch.call_args[0][1], [member])
            self.assertEqual(found["xgameruntime.dll"],
                             folder / "xgameruntime.dll")
            self.assertEqual(found["xgameruntime.dll.threading"].read_bytes(),
                             self._THREADING)
            self.assertEqual(found["xgameruntime.dll.threading"].parent,
                             Path(directory) / "cache")

    def test_a_copy_that_does_not_match_the_pin_is_not_used(self):
        with tempfile.TemporaryDirectory() as directory, \
                self._config(directory):
            self._engine(directory, dll=b"MZ another build")
            member = f"{winemac._ENGINE_DLL_DIR}/xgameruntime.dll"
            with mock.patch.object(winemac, "_stream_archive_members",
                                   return_value={member: self._DLL}), \
                    mock.patch.object(winemac, "info"):
                found = winemac.gdk_runtime_files()
            self.assertEqual(found["xgameruntime.dll"].read_bytes(), self._DLL)

    def test_bytes_from_the_archive_that_do_not_match_are_refused(self):
        with tempfile.TemporaryDirectory() as directory, \
                self._config(directory):
            members = {f"{winemac._ENGINE_DLL_DIR}/{name}": b"MZ tampered"
                       for name in self._NAMES}
            with mock.patch.object(winemac, "_stream_archive_members",
                                   return_value=members), \
                    mock.patch.object(winemac, "info"):
                with self.assertRaises(winemac.BolError):
                    winemac.gdk_runtime_files()
            cache = Path(directory) / "cache"
            self.assertEqual(list(cache.glob("xgameruntime*"))
                             if cache.exists() else [], [])

    def test_both_files_are_read_out_of_the_archive_in_one_pass(self):
        import io
        import tarfile

        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "engine.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                for name, data in (
                        ("GDK-Proton-xuser/proton", b"#!/bin/sh\n"),
                        ("GDK-Proton-xuser/" + winemac._ENGINE_DLL_DIR
                         + "/xgameruntime.dll", self._DLL),
                        ("GDK-Proton-xuser/" + winemac._ENGINE_DLL_DIR
                         + "/xgameruntime.dll.threading", self._THREADING)):
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
            members = [f"{winemac._ENGINE_DLL_DIR}/{name}"
                       for name in self._NAMES]
            found = winemac._stream_archive_members(
                archive.as_uri(), members, 1 << 20)
            self.assertEqual(found, {members[0]: self._DLL,
                                     members[1]: self._THREADING})
            # A member past the size bound is not read.
            self.assertEqual(winemac._stream_archive_members(
                archive.as_uri(), members, 10), {})

    def test_the_pins_are_the_engines_own(self):
        """An engine of the pinned revision on this machine says what the
        pins should be: its manifest for the runtime, its files for all."""
        import json
        from bol import config

        manifest = config.WINEGDK_OUT / "engine-manifest.json"
        try:
            payload = json.loads(manifest.read_text())
        except (OSError, ValueError):
            self.skipTest("no engine installed here")
        if payload.get("build_rev") != config.MAC_GDK_RUNTIME_REV:
            self.skipTest("the installed engine is another revision")
        folder = config.WINEGDK_OUT / winemac._ENGINE_DLL_DIR
        for name, pin in winemac._gdk_runtime_pins().items():
            critical = payload["critical_files"].get(
                f"{winemac._ENGINE_DLL_DIR}/{name}")
            if critical is not None:
                self.assertEqual(critical, pin, name)
            if (folder / name).is_file():
                self.assertEqual(winemac._sha256_file(folder / name), pin,
                                 name)

    def test_the_pinned_revision_is_a_published_engine_name(self):
        from bol import config

        self.assertRegex(config.MAC_GDK_RUNTIME_REV,
                         r"^wow64-archs-native\d+$")
        for name, pin in config.MAC_GDK_RUNTIME_SHA256.items():
            self.assertRegex(pin, r"^[0-9a-f]{64}$", name)


class MacWinRtLibrariesTests(unittest.TestCase):
    """Minecraft dies on a C++ exception (exit 99) when a WinRT class it
    activates at startup is refused. A CrossOver bottle neither registers
    nor implements four of them: the Mac takes the engine's libraries that
    do, as native DLLs, and registers each class with the one that has it."""

    _CLASSES = {
        "Windows.ApplicationModel.DataTransfer.DataTransferManager":
            "twinapi.appcore.dll",
        "Windows.UI.Text.Core.CoreTextServicesManager":
            "windows.ui.core.textinput.dll",
        "Windows.UI.ViewManagement.Core.CoreInputView":
            "windows.ui.core.textinput.dll",
        "Windows.Foundation.Metadata.ApiInformation": "wintypes.dll",
    }
    _LIBRARIES = ("twinapi.appcore.dll", "wintypes.dll",
                  "windows.ui.core.textinput.dll", "windows.ui.dll")
    _SYSTEM_REG = ("WINE REGISTRY Version 2\n;; All keys relative to "
                   "\\\\Machine\n\n#arch=win64\n\n")
    _USER_REG = ("WINE REGISTRY Version 2\n;; All keys relative to "
                 "\\\\User\\\\S-1-5-21-0-0-0-1000\n\n#arch=win64\n\n")

    @staticmethod
    def _builtin(body):
        return (b"MZ" + b"\0" * 0x3e + b"Wine builtin DLL" + b"\0" * 0x30
                + b"PE\0\0" + body)

    # -- the marker

    def test_the_marker_is_cleared_and_nothing_else_changes(self):
        image = self._builtin(b"twinapi")
        native = winemac.as_native_dll(image)
        self.assertEqual(len(native), len(image))
        self.assertEqual(native[:0x40], image[:0x40])
        self.assertEqual(native[0x40:0x50], bytes(16))
        self.assertEqual(native[0x50:], image[0x50:])
        self.assertNotIn(b"Wine builtin DLL", native)

    def test_clearing_the_marker_twice_changes_nothing_more(self):
        native = winemac.as_native_dll(self._builtin(b"wintypes"))
        self.assertEqual(winemac.as_native_dll(native), native)

    def test_a_file_without_the_marker_is_left_alone(self):
        for image in (b"MZ\x90\0" + b"This program cannot be run" * 8,
                      b"MZ", b"",
                      # The words elsewhere in the file are not the marker.
                      b"MZ" + b"\0" * 0x60 + b"Wine builtin DLL"):
            self.assertEqual(winemac.as_native_dll(image), image)

    def test_the_marker_is_the_one_the_engines_libraries_carry(self):
        from bol import config

        folder = config.WINEGDK_OUT / winemac._ENGINE_DLL_DIR
        seen = 0
        for name in self._LIBRARIES + ("xgameruntime.dll",):
            try:
                with (folder / name).open("rb") as image:
                    head = image.read(0x50)
            except OSError:
                continue
            seen += 1
            self.assertEqual(head[0x40:], b"Wine builtin DLL", name)
        if not seen:
            self.skipTest("no engine installed here")

    # -- what is installed

    def test_every_library_is_pinned(self):
        from bol import config

        for name in self._LIBRARIES + ("xgameruntime.dll",
                                       "xgameruntime.dll.threading"):
            self.assertIn(name, config.MAC_GDK_RUNTIME_SHA256)
        for library in self._CLASSES.values():
            self.assertIn(library, config.MAC_GDK_RUNTIME_SHA256)

    def test_each_class_is_in_the_engine_library_it_is_registered_with(self):
        from bol import config

        folder = config.WINEGDK_OUT / winemac._ENGINE_DLL_DIR
        seen = 0
        for name, library in winemac._GDK_WINRT_CLASSES.items():
            try:
                image = (folder / library).read_bytes()
            except OSError:
                continue
            seen += 1
            self.assertIn(name.encode("utf-16-le"), image, name)
        if not seen:
            self.skipTest("no engine installed here")

    def _prefix(self, directory):
        prefix = Path(directory) / "pfx"
        prefix.mkdir()
        (prefix / "system.reg").write_text(self._SYSTEM_REG)
        (prefix / "user.reg").write_text(self._USER_REG)
        return prefix

    def _install(self, directory, prefix, images):
        from bol import config, prefix as prefix_module

        engine = Path(directory) / "engine" / winemac._ENGINE_DLL_DIR
        engine.mkdir(parents=True, exist_ok=True)
        for name, data in images.items():
            (engine / name).write_bytes(data)
        pins = {name: MacGdkRuntimeTests._pin(data)
                for name, data in images.items()}
        with mock.patch.multiple(config, CACHE=Path(directory) / "cache",
                                 WINEGDK_OUT=Path(directory) / "engine",
                                 MAC_GDK_RUNTIME_SHA256=pins), \
                mock.patch.object(prefix_module, "require_prefix_idle"), \
                mock.patch.object(winemac, "_stream_archive_members") as fetch, \
                mock.patch.object(winemac, "ok") as said:
            system32 = winemac.install_gdk_runtime(prefix)
        fetch.assert_not_called()
        return engine, system32, said

    def test_the_copies_are_native_and_the_engines_files_are_untouched(self):
        images = {name: self._builtin(name.encode())
                  for name in self._LIBRARIES}
        with tempfile.TemporaryDirectory() as directory:
            prefix = self._prefix(directory)
            engine, system32, said = self._install(directory, prefix, images)
            for name, image in images.items():
                installed = (system32 / name).read_bytes()
                self.assertEqual(installed, winemac.as_native_dll(image), name)
                self.assertNotIn(b"Wine builtin DLL", installed, name)
                # Hash-pinned, and read only.
                self.assertEqual((engine / name).read_bytes(), image, name)
            self.assertEqual(said.call_count, 1)
            # The second PLAY rewrites neither the files nor the registry.
            before = [(path, path.stat().st_mtime_ns) for path in
                      [system32 / name for name in images]
                      + [prefix / "system.reg", prefix / "user.reg"]]
            _engine, _system32, said = self._install(directory, prefix, images)
            self.assertEqual(before,
                             [(path, path.stat().st_mtime_ns)
                              for path, _stamp in before])
            said.assert_not_called()

    # -- the registry

    def test_each_class_is_registered_with_the_library_that_has_it(self):
        machine, _user = winemac.gdk_registry_changes()
        root = "Software\\Microsoft\\WindowsRuntime\\ActivatableClassId\\"
        paths = {change.key: change.value for change in machine
                 if change.name == "DllPath"}
        self.assertEqual(paths, {
            root + name: "C:\\windows\\system32\\" + library
            for name, library in self._CLASSES.items()})
        types = {change.key: (change.kind, change.value) for change in machine
                 if change.name == "ActivationType"}
        self.assertEqual(types, {root + name: ("dword", 0)
                                 for name in self._CLASSES})
        self.assertEqual(len(machine), 2 * len(self._CLASSES))

    def test_the_installed_libraries_are_asked_for_native_first(self):
        _machine, user = winemac.gdk_registry_changes()
        self.assertEqual(
            {(change.key, change.name, change.kind, change.value)
             for change in user},
            {("Software\\Wine\\DllOverrides", library, "string",
              "native,builtin")
             for library in ("twinapi.appcore", "wintypes",
                             "windows.ui.core.textinput", "windows.ui")})

    def test_the_values_land_in_the_bottles_registry_files(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = self._prefix(directory)
            self._install(directory, prefix,
                          {"wintypes.dll": self._builtin(b"wintypes")})
            system = (prefix / "system.reg").read_text()
            user = (prefix / "user.reg").read_text()
        for name, library in self._CLASSES.items():
            section = system.split(
                "[Software\\\\Microsoft\\\\WindowsRuntime\\\\"
                "ActivatableClassId\\\\" + name + "]", 1)
            self.assertEqual(len(section), 2, name)
            body = section[1].split("\n[", 1)[0]
            self.assertIn('"DllPath"="C:\\\\windows\\\\system32\\\\'
                          + library + '"', body)
            self.assertIn('"ActivationType"=dword:00000000', body)
        overrides = user.split("[Software\\\\Wine\\\\DllOverrides]", 1)[1]
        for library in ("twinapi.appcore", "wintypes",
                        "windows.ui.core.textinput", "windows.ui"):
            self.assertIn(f'"{library}"="native,builtin"', overrides)

    def test_a_registry_that_cannot_be_written_does_not_stop_the_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "pfx"   # no registry files at all
            from bol import config
            with mock.patch.multiple(config, CACHE=Path(directory) / "cache",
                                     WINEGDK_OUT=Path(directory) / "engine",
                                     MAC_GDK_RUNTIME_SHA256={}), \
                    mock.patch.object(winemac, "update_prefix_registry",
                                      side_effect=OSError("busy")), \
                    mock.patch.object(winemac, "warn") as warned:
                winemac.install_gdk_runtime(prefix)
        self.assertIn("register", warned.call_args[0][0])

    def test_linux_installs_none_of_it(self):
        """Only bol.winemac knows these libraries, and Linux never calls it:
        GDK-Proton serves its own builtins."""
        from bol import auth, fixups, launch, prefix as prefix_module

        for module in (auth, fixups, prefix_module):
            source = Path(module.__file__).read_text()
            self.assertNotIn("twinapi.appcore", source, module.__name__)
            self.assertNotIn("install_gdk_runtime", source, module.__name__)
        launch_source = Path(launch.__file__).read_text()
        guard = launch_source.index("if IS_MAC:\n            # The GDK runtime")
        call = launch_source.index("install_gdk_runtime(active_prefix())")
        self.assertLess(guard, call)
        self.assertLess(call - guard, 400)


class MacArchitectureTests(unittest.TestCase):
    def test_apple_silicon_is_not_told_to_install_an_emulator(self):
        """Rosetta runs x86-64 there, not binfmt_misc (#250 is Linux's)."""
        from bol import hostarch

        with mock.patch.object(hostarch, "IS_MAC", True), \
                mock.patch.object(hostarch, "machine", return_value="arm64"), \
                mock.patch.object(hostarch.platform, "machine",
                                  return_value="arm64"):
            self.assertIsNone(hostarch.problem())
            self.assertIn("Rosetta", hostarch.summary())


if __name__ == "__main__":
    unittest.main()
