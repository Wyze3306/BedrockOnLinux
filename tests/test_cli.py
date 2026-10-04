"""Offline command-line dispatch regressions."""
# SPDX-License-Identifier: MIT

import builtins
import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from bol import cli, deps
from bol.log import BolError
from bol.network import NetworkCheck


class CliTests(unittest.TestCase):
    def test_direct_bol_error_is_printed_before_cli_exit(self):
        output = io.StringIO()
        with mock.patch.object(
                sys, "argv",
                ["bedrock-on-linux", "profiles", "create", "../bad"]), \
                mock.patch.object(
                    cli, "require_profile_shortcuts_supported"), \
                mock.patch.object(
                    cli, "create_profile",
                    side_effect=BolError("invalid profile name")), \
                contextlib.redirect_stdout(output), \
                self.assertRaises(SystemExit) as exited:
            cli.main()

        self.assertEqual(exited.exception.code, 1)
        self.assertIn("invalid profile name", output.getvalue())

    def test_regular_setup_preserves_existing_dispatch(self):
        with mock.patch.object(
                sys, "argv", ["bedrock-on-linux", "setup", "--force"]), \
                mock.patch.object(cli, "do_setup") as setup, \
                mock.patch.object(cli, "ok"):
            cli.main()

        setup.assert_called_once_with(
            mc_edition=None, mc_version=None, force=True)

    def test_play_says_when_nothing_is_signed_in_for_online_play(self):
        # The launcher window warns before starting the game (#240); a
        # terminal that just launches leaves the same absence to be found
        # in-game, as Realms and servers quietly missing.
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["bedrock-on-linux", "play"]), \
                mock.patch.object(cli, "msa_signed_in", return_value=False), \
                mock.patch.object(cli, "launch") as launch, \
                contextlib.redirect_stdout(output):
            cli.main()

        # A warning, not a refusal: offline is a real way to play.
        self.assertTrue(launch.called)
        printed = output.getvalue()
        self.assertIn("Not signed in for online play", printed)
        self.assertIn("login", printed)

    def test_play_says_nothing_when_the_account_is_there(self):
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["bedrock-on-linux", "play"]), \
                mock.patch.object(cli, "msa_signed_in", return_value=True), \
                mock.patch.object(cli, "launch") as launch, \
                contextlib.redirect_stdout(output):
            cli.main()

        self.assertTrue(launch.called)
        self.assertNotIn("Not signed in", output.getvalue())

    def test_profile_create_prints_profile_shortcut_and_command(self):
        profile = Path("/tmp/bol-profiles/family")
        shortcut = Path("/tmp/applications/bol-family.desktop")
        output = io.StringIO()
        with mock.patch.object(
                sys, "argv",
                ["bedrock-on-linux", "profiles", "create", "Family"]), \
                mock.patch.object(
                    cli, "create_profile", return_value=profile) as create, \
                mock.patch.object(
                    cli, "write_profile_shortcut",
                    return_value=shortcut) as write_shortcut, \
                mock.patch.object(
                    cli, "profile_launch_command",
                    return_value="BOL_HOME=/tmp/family bedrock-on-linux gui"
                ) as launch_command, \
                mock.patch.object(cli, "ok") as success, \
                contextlib.redirect_stdout(output):
            cli.main()

        create.assert_called_once_with("Family")
        write_shortcut.assert_called_once_with(
            "Family",
            profile_dir=profile,
        )
        launch_command.assert_called_once_with(profile)
        self.assertEqual(
            [call.args[0] for call in success.call_args_list],
            [f"Profile: {profile}", f"Desktop shortcut: {shortcut}"],
        )
        self.assertIn(
            "Steam command: BOL_HOME=/tmp/family bedrock-on-linux gui",
            output.getvalue(),
        )

    def test_shortcut_creates_a_direct_launch_entry_for_the_default_install(
            self):
        shortcut = Path("/tmp/applications/bedrock-on-linux-play.desktop")
        output = io.StringIO()
        with mock.patch.object(
                sys, "argv", ["bedrock-on-linux", "shortcut"]), \
                mock.patch.object(cli, "require_shortcuts_supported"), \
                mock.patch.object(cli, "create_profile") as create, \
                mock.patch.object(
                    cli, "write_play_shortcut",
                    return_value=shortcut) as write_shortcut, \
                mock.patch.object(
                    cli, "play_launch_command",
                    return_value="bedrock-on-linux play") as launch_command, \
                mock.patch.object(
                    cli, "direct_launch_readiness",
                    return_value=["Sign in from the launcher once."]), \
                mock.patch.object(cli, "warn") as warned, \
                mock.patch.object(cli, "info"), \
                mock.patch.object(cli, "ok") as success, \
                contextlib.redirect_stdout(output):
            cli.main()

        create.assert_not_called()
        write_shortcut.assert_called_once_with(
            profile_name=None, profile_dir=None)
        launch_command.assert_called_once_with(None)
        success.assert_called_once_with(f"Desktop shortcut: {shortcut}")
        self.assertIn("Steam command: bedrock-on-linux play",
                      output.getvalue())
        warned.assert_called_once_with("Sign in from the launcher once.")

    def test_profile_shortcut_reports_no_readiness_for_another_home(self):
        profile = Path("/tmp/bol-profiles/family")
        with mock.patch.object(
                sys, "argv",
                ["bedrock-on-linux", "shortcut", "--profile", "Family"]), \
                mock.patch.object(cli, "require_shortcuts_supported"), \
                mock.patch.object(
                    cli, "create_profile", return_value=profile), \
                mock.patch.object(
                    cli, "write_play_shortcut", return_value=Path("/tmp/e")), \
                mock.patch.object(
                    cli, "play_launch_command", return_value="cmd"), \
                mock.patch.object(
                    cli, "direct_launch_readiness") as readiness, \
                mock.patch.object(cli, "info"), \
                mock.patch.object(cli, "ok"), \
                contextlib.redirect_stdout(io.StringIO()):
            cli.main()

        # Settings under the current BOL_HOME say nothing about the profile.
        readiness.assert_not_called()

    def test_windowless_play_failure_reaches_the_desktop(self):
        with mock.patch.object(
                sys, "argv", ["bedrock-on-linux", "play"]), \
                mock.patch.object(cli, "IS_TTY", False), \
                mock.patch.object(
                    cli, "launch",
                    side_effect=BolError("No game — choose a version")), \
                mock.patch.object(cli, "desktop_notify") as notified, \
                mock.patch.object(cli, "err"), \
                self.assertRaises(SystemExit) as exited:
            cli.main()

        self.assertEqual(exited.exception.code, 1)
        self.assertIn("No game — choose a version", notified.call_args.args[0])

    def test_terminal_play_failure_is_not_duplicated_as_a_notification(self):
        with mock.patch.object(
                sys, "argv", ["bedrock-on-linux", "play"]), \
                mock.patch.object(cli, "IS_TTY", True), \
                mock.patch.object(
                    cli, "launch", side_effect=BolError("prefix busy")), \
                mock.patch.object(cli, "desktop_notify") as notified, \
                mock.patch.object(cli, "err"), \
                self.assertRaises(SystemExit):
            cli.main()

        notified.assert_not_called()

    def test_profile_list_prints_all_known_profiles(self):
        output = io.StringIO()
        profiles = [
            {
                "name": "Alice",
                "slug": "alice",
                "path": "/tmp/profiles/alice",
            },
            {
                "name": "Bob",
                "slug": "bob",
                "path": "/tmp/profiles/bob",
            },
        ]
        with mock.patch.object(
                sys, "argv",
                ["bedrock-on-linux", "profiles", "list"]), \
                mock.patch.object(
                    cli, "list_profiles", return_value=profiles) as listing, \
                contextlib.redirect_stdout(output):
            cli.main()

        listing.assert_called_once_with()
        self.assertIn("Alice (alice): /tmp/profiles/alice", output.getvalue())
        self.assertIn("Bob (bob): /tmp/profiles/bob", output.getvalue())

    def test_empty_profile_list_is_reported(self):
        with mock.patch.object(
                sys, "argv",
                ["bedrock-on-linux", "profiles", "list"]), \
                mock.patch.object(cli, "list_profiles", return_value=[]), \
                mock.patch.object(cli, "info") as informational:
            cli.main()

        informational.assert_called_once_with("No isolated profiles.")

    def test_doctor_network_combines_system_and_network_results(self):
        checks = [
            NetworkCheck("dns", "xbox.example", True, "resolved"),
            NetworkCheck("tls", "xbox.example", False, "timed out"),
            NetworkCheck("clock", "host", None, "status unavailable"),
        ]
        output = io.StringIO()
        with mock.patch.object(
                sys, "argv",
                ["bedrock-on-linux", "doctor", "--network"]), \
                mock.patch.object(
                    cli, "doctor", return_value=True) as system_doctor, \
                mock.patch.object(
                    cli, "diagnose_network",
                    return_value=(False, checks)) as network_doctor, \
                contextlib.redirect_stdout(output), \
                self.assertRaises(SystemExit) as exited:
            cli.main()

        self.assertEqual(exited.exception.code, 1)
        system_doctor.assert_called_once_with(False)
        network_doctor.assert_called_once_with(None)
        report = output.getvalue()
        self.assertIn("dns", report)
        self.assertIn("OK", report)
        self.assertIn("ÉCHEC", report)
        self.assertIn("INFO", report)

    def test_doctor_host_implies_network_checks(self):
        with mock.patch.object(
                sys, "argv",
                ["bedrock-on-linux", "doctor", "--host", "192.0.2.7"]), \
                mock.patch.object(cli, "doctor", return_value=True), \
                mock.patch.object(
                    cli, "diagnose_network",
                    return_value=(True, [])) as network_doctor, \
                self.assertRaises(SystemExit) as exited:
            cli.main()

        self.assertEqual(exited.exception.code, 0)
        network_doctor.assert_called_once_with("192.0.2.7")

    def test_plain_doctor_does_not_run_network_checks(self):
        with mock.patch.object(
                sys, "argv", ["bedrock-on-linux", "doctor"]), \
                mock.patch.object(
                    cli, "doctor", return_value=True) as system_doctor, \
                mock.patch.object(cli, "diagnose_network") as network_doctor, \
                self.assertRaises(SystemExit) as exited:
            cli.main()

        self.assertEqual(exited.exception.code, 0)
        system_doctor.assert_called_once_with(False)
        network_doctor.assert_not_called()

    def test_failed_system_doctor_is_not_masked_by_healthy_network(self):
        with mock.patch.object(
                sys, "argv",
                ["bedrock-on-linux", "doctor", "--network"]), \
                mock.patch.object(cli, "doctor", return_value=False), \
                mock.patch.object(
                    cli, "diagnose_network", return_value=(True, [])), \
                self.assertRaises(SystemExit) as exited:
            cli.main()

        self.assertEqual(exited.exception.code, 1)


class SudoRefusalTests(unittest.TestCase):
    def test_nothing_runs_as_root_for_someone_else(self):
        # `sudo ... doctor --acknowledge-gpu-crash` looked at root's data
        # and found no incident to acknowledge (#299).
        output = io.StringIO()
        with mock.patch.object(
                sys, "argv",
                ["bedrock-on-linux", "doctor", "--acknowledge-gpu-crash"]), \
                mock.patch.dict(os.environ, {"SUDO_USER": "bilal"}), \
                mock.patch.object(os, "geteuid", return_value=0), \
                mock.patch("bol.util._ensure_xdg_storage") as storage, \
                mock.patch.object(cli, "doctor") as doctor, \
                contextlib.redirect_stdout(output), \
                self.assertRaises(SystemExit) as exited:
            cli.main()

        self.assertEqual(exited.exception.code, 1)
        self.assertIn("without sudo", output.getvalue())
        storage.assert_not_called()
        doctor.assert_not_called()


class LauncherStartTests(unittest.TestCase):
    """Starting the launcher opens the launcher — in Game Mode too."""

    def _run(self, argv, gamescope):
        # Qt is imported where it is used, so the window is patched on
        # bol.gui itself rather than on a name bol.cli holds.
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.dict(os.environ, {"DISPLAY": ":0"}), \
                mock.patch.object(cli, "launch") as launched, \
                mock.patch("bol.gui.gui") as window, \
                mock.patch.object(cli, "info"), \
                mock.patch("bol.gpu_safety.in_gamescope_session",
                           return_value=gamescope):
            cli.main()
        return launched, window

    def test_the_window_opens_in_every_session(self):
        # The 2.1.4 behaviour — Game Mode starts the game and never shows the
        # launcher — also fired on the reporter's desktop session (#127, #130).
        for gamescope in (True, False):
            for argv in (["bedrock-on-linux"], ["bedrock-on-linux", "gui"]):
                with self.subTest(argv=argv, gamescope=gamescope):
                    launched, window = self._run(argv, gamescope)
                    window.assert_called_once_with()
                    launched.assert_not_called()

    def test_play_stays_the_launcher_free_path(self):
        launched, window = self._run(["bedrock-on-linux", "play"], True)
        launched.assert_called_once_with()
        window.assert_not_called()

    def test_shortcut_launch_failure_reaches_the_desktop(self):
        # A shortcut leaves no terminal, so the notification is the only place
        # this can surface.
        with mock.patch.object(sys, "argv", ["bedrock-on-linux", "play"]), \
                mock.patch.object(cli, "IS_TTY", False), \
                mock.patch.object(
                    cli, "launch",
                    side_effect=BolError("No game — choose a version")), \
                mock.patch.object(cli, "desktop_notify") as notified, \
                mock.patch.object(cli, "err"), \
                self.assertRaises(SystemExit) as exited:
            cli.main()

        self.assertEqual(exited.exception.code, 1)
        self.assertIn("No game — choose a version", notified.call_args.args[0])

    def test_window_failure_is_not_reported_as_a_launch_failure(self):
        with mock.patch.object(sys, "argv", ["bedrock-on-linux", "gui"]), \
                mock.patch.dict(os.environ, {"DISPLAY": ":0"}), \
                mock.patch.object(cli, "IS_TTY", False), \
                mock.patch("bol.gui.gui",
                           side_effect=BolError("Qt is missing")), \
                mock.patch.object(cli, "desktop_notify") as notified, \
                mock.patch.object(cli, "err"), \
                self.assertRaises(SystemExit):
            cli.main()

        notified.assert_not_called()

    def test_a_toolkit_that_is_not_installed_yet_is_installed_first(self):
        # bol.cli imported bol.gui -- and with it the whole Qt stack -- while
        # it was still being imported itself, so the pip bootstrap that is
        # supposed to install the toolkit on a portable .pyz or a bare
        # checkout could never run: the import had already failed.
        with mock.patch.object(sys, "argv", ["bedrock-on-linux", "gui"]), \
                mock.patch.dict(os.environ, {"DISPLAY": ":0"}), \
                mock.patch.object(deps, "have", return_value=False), \
                mock.patch.object(deps, "ensure_gui_deps",
                                  return_value=[]) as bootstrap, \
                mock.patch("bol.gui.gui") as window:
            cli.main()

        bootstrap.assert_called_once_with()
        window.assert_called_once_with()

    def test_an_installed_toolkit_is_not_reinstalled_on_every_launch(self):
        # pip must not be reached for a toolkit that is already there.
        with mock.patch.object(sys, "argv", ["bedrock-on-linux", "gui"]), \
                mock.patch.dict(os.environ, {"DISPLAY": ":0"}), \
                mock.patch.object(deps, "have", return_value=True), \
                mock.patch.object(deps, "ensure_gui_deps") as bootstrap, \
                mock.patch("bol.gui.gui") as window:
            cli.main()

        bootstrap.assert_not_called()
        window.assert_called_once_with()

    def test_a_toolkit_that_cannot_be_installed_is_reported_plainly(self):
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["bedrock-on-linux", "gui"]), \
                mock.patch.dict(os.environ, {"DISPLAY": ":0"}), \
                mock.patch.object(deps, "have", return_value=False), \
                mock.patch.object(deps, "ensure_gui_deps",
                                  return_value=["PySide6"]), \
                contextlib.redirect_stdout(output), \
                self.assertRaises(SystemExit) as exited:
            cli.main()

        self.assertEqual(exited.exception.code, 1)
        self.assertIn("PySide6", output.getvalue())

    def test_a_qt_library_the_host_lacks_is_named_rather_than_raised(self):
        # Issue #205: the AppImage on NixOS reported the launcher as a
        # traceback out of `from PySide6.QtCore import ...`. The loader says
        # exactly which library it could not open; say it back.
        refused = ImportError("libzstd.so.1: cannot open shared object file: "
                              "No such file or directory")
        real_import = builtins.__import__

        def without_qt(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "gui" or name.endswith("bol.gui"):
                raise refused
            return real_import(name, globals, locals, fromlist, level)

        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["bedrock-on-linux", "gui"]), \
                mock.patch.dict(os.environ, {"DISPLAY": ":0"}), \
                mock.patch.object(deps, "ensure_gui_deps", return_value=[]), \
                mock.patch.object(builtins, "__import__", without_qt), \
                contextlib.redirect_stdout(output), \
                self.assertRaises(SystemExit) as exited:
            cli.main()

        self.assertEqual(exited.exception.code, 1)
        self.assertIn("libzstd.so.1", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())

    def test_a_gui_import_failure_of_our_own_making_still_raises(self):
        # Only the loader's "cannot open shared object file" is answered with
        # a system library to install; a broken import inside bol.gui is a bug
        # here and must keep its traceback.
        real_import = builtins.__import__

        def broken_gui(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "gui" or name.endswith("bol.gui"):
                raise ImportError("cannot import name 'QToolButton'")
            return real_import(name, globals, locals, fromlist, level)

        with mock.patch.object(sys, "argv", ["bedrock-on-linux", "gui"]), \
                mock.patch.dict(os.environ, {"DISPLAY": ":0"}), \
                mock.patch.object(deps, "ensure_gui_deps", return_value=[]), \
                mock.patch.object(builtins, "__import__", broken_gui), \
                self.assertRaises(ImportError):
            cli.main()


if __name__ == "__main__":
    unittest.main()
