"""Regression tests for prefix shutdown and operation serialization."""
# SPDX-License-Identifier: MIT

import ast
import os
import tempfile
import subprocess
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bol import auth, fixups, gameinput, gamesetup, prefix
from bol.log import BolError


def _make_ready_prefix(path):
    (path / "drive_c/windows/system32").mkdir(parents=True, exist_ok=True)
    for hive in ("system.reg", "user.reg"):
        (path / hive).write_bytes(b"WINE REGISTRY Version 2\n")


class PrefixShutdownTests(unittest.TestCase):
    def test_prefix_process_match_requires_exact_environment_name(self):
        pfx = Path("/data/prefix")
        self.assertTrue(prefix._environ_uses_prefix(
            b"USER=test\0WINEPREFIX=/data/prefix\0", pfx
        ))
        self.assertFalse(prefix._environ_uses_prefix(
            b"NOT_WINEPREFIX=/data/prefix\0", pfx
        ))
        self.assertFalse(prefix._environ_uses_prefix(
            b"WINEPREFIX=/data/prefix-extra\0", pfx
        ))

    def test_prefix_ready_requires_system32_and_valid_registry_hives(self):
        with tempfile.TemporaryDirectory() as td:
            pfx = Path(td) / "pfx"
            (pfx / "drive_c/windows/system32").mkdir(parents=True)
            self.assertFalse(prefix.prefix_ready(pfx))

            (pfx / "system.reg").write_bytes(b"not a Wine registry\n")
            (pfx / "user.reg").write_bytes(b"WINE REGISTRY Version 2\n")
            self.assertFalse(prefix.prefix_ready(pfx))

            (pfx / "system.reg").write_bytes(b"WINE REGISTRY Version 2\n")
            self.assertTrue(prefix.prefix_ready(pfx))

    def test_unsafe_fresh_prefix_is_rejected_before_umu_or_wine(self):
        with tempfile.TemporaryDirectory() as td:
            pfx = Path(td) / "fresh-prefix"
            with mock.patch(
                    "bol.gpu_safety.require_safe_graphics_session",
                    side_effect=BolError("unsafe graphics")) as safety, \
                    mock.patch.object(prefix, "proton_umu_cmd") as umu, \
                    mock.patch.object(prefix.subprocess, "run") as run:
                with self.assertRaisesRegex(BolError, "unsafe graphics"):
                    prefix.boot_prefix(pfx)
            safety.assert_called_once_with()
            umu.assert_not_called()
            run.assert_not_called()

    def test_partial_prefix_retries_wineboot(self):
        with tempfile.TemporaryDirectory() as td:
            pfx = Path(td) / "partial-prefix"
            (pfx / "drive_c/windows/system32").mkdir(parents=True)

            def finish_prefix(*_args, **_kwargs):
                _make_ready_prefix(pfx)
                return SimpleNamespace(returncode=0)

            with mock.patch.object(prefix, "LOGS", Path(td) / "logs"), \
                    mock.patch(
                        "bol.gpu_safety.require_safe_graphics_session"), \
                    mock.patch.object(
                        prefix, "proton_umu_cmd",
                        return_value=(["umu", "wineboot"], {})) as umu, \
                    mock.patch.object(
                        prefix, "seed_managed_bootstrap_cryptbase",
                        return_value=False), \
                    mock.patch.object(
                        prefix.subprocess, "run",
                        side_effect=finish_prefix) as run, \
                    mock.patch.object(prefix, "stop_prefix_procs"):
                self.assertTrue(prefix.boot_prefix(pfx))

            umu.assert_called_once_with("wineboot", prefix=pfx)
            run.assert_called_once()

    def test_complete_prefix_skips_wineboot(self):
        with tempfile.TemporaryDirectory() as td:
            pfx = Path(td) / "ready-prefix"
            _make_ready_prefix(pfx)
            with mock.patch(
                    "bol.gpu_safety.require_safe_graphics_session") as safety, \
                    mock.patch.object(
                        prefix, "repair_managed_prefix_user32") as repair, \
                    mock.patch.object(prefix, "proton_umu_cmd") as umu, \
                    mock.patch.object(prefix.subprocess, "run") as run:
                self.assertTrue(prefix.boot_prefix(pfx))
            safety.assert_not_called()
            repair.assert_called_once_with(pfx)
            umu.assert_not_called()
            run.assert_not_called()

    def test_nonzero_wineboot_is_logged_and_fails(self):
        with tempfile.TemporaryDirectory() as td:
            logs = Path(td) / "logs"
            with mock.patch.object(prefix, "LOGS", logs), \
                    mock.patch(
                        "bol.gpu_safety.require_safe_graphics_session"), \
                    mock.patch.object(
                        prefix, "proton_umu_cmd",
                        return_value=(["umu", "wineboot"], {})), \
                    mock.patch.object(
                        prefix, "seed_managed_bootstrap_cryptbase",
                        return_value=False), \
                    mock.patch.object(
                        prefix.subprocess, "run",
                        return_value=SimpleNamespace(returncode=42)), \
                    mock.patch.object(prefix, "stop_prefix_procs"), \
                    mock.patch.object(prefix, "warn") as warn:
                self.assertFalse(prefix.boot_prefix(Path(td) / "pfx"))

            self.assertIn(
                "wineboot exited with status 42",
                (logs / "native-login.log").read_text(),
            )
            self.assertIn(
                str(logs / "native-login.log"), warn.call_args.args[0])

    def _timed_out_boot(self, td, runtime_pending):
        logs = Path(td) / "logs"
        with mock.patch.object(prefix, "LOGS", logs), \
                mock.patch(
                    "bol.gpu_safety.require_safe_graphics_session"), \
                mock.patch.object(
                    prefix, "proton_umu_cmd",
                    return_value=(["umu", "wineboot"], {})), \
                mock.patch.object(
                    prefix, "seed_managed_bootstrap_cryptbase",
                    return_value=False), \
                mock.patch.object(
                    prefix, "runtime_setup_pending",
                    return_value=runtime_pending), \
                mock.patch.object(
                    prefix.subprocess, "run",
                    side_effect=prefix.subprocess.TimeoutExpired(
                        ["umu", "wineboot"], 300)) as run, \
                mock.patch.object(prefix, "stop_prefix_procs"), \
                mock.patch.object(prefix, "info"), \
                mock.patch.object(prefix, "warn") as warn:
            self.assertFalse(prefix.boot_prefix(Path(td) / "pfx"))
        return logs, run, warn

    def test_wineboot_timeout_is_logged_and_fails(self):
        with tempfile.TemporaryDirectory() as td:
            logs, run, warn = self._timed_out_boot(td, runtime_pending=False)

            self.assertEqual(run.call_args.kwargs["timeout"], 300)
            self.assertIn(
                "wineboot timed out after 300 seconds",
                (logs / "native-login.log").read_text(),
            )
            self.assertIn("timed out", warn.call_args.args[0])

    def test_first_run_does_not_charge_the_runtime_download_to_wine(self):
        """umu-launcher downloads close to 900 MB of Steam Linux Runtime
        inside the process we are timing, so Wine's own budget must not be
        what a slow connection runs out of (#144)."""
        with tempfile.TemporaryDirectory() as td:
            _logs, run, _warn = self._timed_out_boot(td, runtime_pending=True)

            self.assertEqual(
                run.call_args.kwargs["timeout"],
                prefix.WINEBOOT_TIMEOUT + prefix.RUNTIME_SETUP_TIMEOUT,
            )

    def test_runtime_setup_is_pending_until_the_platform_is_unpacked(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td) / "steamrt3"
            with mock.patch.object(prefix, "UMU_DIR", Path(td)):
                self.assertTrue(prefix.runtime_setup_pending())
                runtime.mkdir()
                self.assertTrue(prefix.runtime_setup_pending())
                (runtime / "sniper_platform_3.0.1").mkdir()
                self.assertFalse(prefix.runtime_setup_pending())

    def test_wineboot_exception_is_logged_and_fails(self):
        with tempfile.TemporaryDirectory() as td:
            logs = Path(td) / "logs"
            with mock.patch.object(prefix, "LOGS", logs), \
                    mock.patch(
                        "bol.gpu_safety.require_safe_graphics_session"), \
                    mock.patch.object(
                        prefix, "proton_umu_cmd",
                        return_value=(["umu", "wineboot"], {})), \
                    mock.patch.object(
                        prefix, "seed_managed_bootstrap_cryptbase",
                        return_value=False), \
                    mock.patch.object(
                        prefix.subprocess, "run",
                        side_effect=OSError("cannot execute")), \
                    mock.patch.object(prefix, "stop_prefix_procs"), \
                    mock.patch.object(prefix, "warn") as warn:
                self.assertFalse(prefix.boot_prefix(Path(td) / "pfx"))

            self.assertIn(
                "wineboot raised OSError: cannot execute",
                (logs / "native-login.log").read_text(),
            )
            self.assertIn("OSError", warn.call_args.args[0])

    def test_shutdown_rescans_and_terminates_new_children(self):
        scans = [[10], [10, 11], []]
        with mock.patch.object(
                prefix, "prefix_processes", side_effect=scans), \
                mock.patch.object(prefix.os, "kill") as kill, \
                mock.patch.object(prefix.time, "sleep"):
            stopped, forced = prefix.stop_prefix_procs(
                Path("/tmp/bol-prefix"), grace=5)

        self.assertEqual((stopped, forced), (2, 0))
        self.assertEqual(
            kill.call_args_list,
            [mock.call(10, 15), mock.call(11, 15)],
        )

    def test_shutdown_fails_if_process_survives_sigkill(self):
        with mock.patch.object(prefix, "prefix_processes",
                               return_value=[10]), \
                mock.patch.object(prefix.os, "kill") as kill:
            with self.assertRaisesRegex(
                    BolError, "refusing unsafe offline changes"):
                prefix.stop_prefix_procs(
                    Path("/tmp/bol-prefix"), grace=0, kill_grace=0)

        self.assertIn(mock.call(10, 15), kill.call_args_list)
        self.assertIn(mock.call(10, 9), kill.call_args_list)

    def _rng_abort_writer(self, attempts, finish_on=None, pfx=None):
        """Fake wineboot which reproduces the unresolved-RtlGenRandom abort."""

        def fake_run(_cmd, **kwargs):
            attempts.append(dict(kwargs.get("env") or {}))
            log = kwargs["stdout"]
            index = len(attempts)
            if finish_on is not None and index >= finish_on:
                log.write("wineboot: prefix updated\n")
                _make_ready_prefix(pfx)
                return SimpleNamespace(returncode=0)
            log.write(
                "wine: Call from 00006FFFFFC59E08 to unimplemented function "
                "advapi32.dll.SystemFunction036, aborting\n"
                "err:module:find_forwarded_export module not found for "
                "forward 'cryptbase.SystemFunction036' used by "
                'L"C:\\\\windows\\\\system32\\\\advapi32.dll"\n'
                "err:winediag:nodrv_CreateWindow Application tried to create "
                "a window, but no driver could be loaded.\n"
            )
            raise prefix.subprocess.TimeoutExpired(["umu", "wineboot"], 300)

        return fake_run

    def test_rng_abort_reseeds_cryptbase_and_completes_the_prefix(self):
        with tempfile.TemporaryDirectory() as td:
            pfx = Path(td) / "pfx"
            attempts = []
            with mock.patch.object(prefix, "LOGS", Path(td) / "logs"), \
                    mock.patch(
                        "bol.gpu_safety.require_safe_graphics_session"), \
                    mock.patch.object(
                        prefix, "proton_umu_cmd",
                        return_value=(["umu", "wineboot"], {})), \
                    mock.patch.object(
                        prefix, "seed_managed_bootstrap_cryptbase",
                        return_value=False), \
                    mock.patch.object(
                        prefix, "repair_bootstrap_cryptbase",
                        return_value=True) as repair, \
                    mock.patch.object(
                        prefix, "repair_managed_prefix_user32"), \
                    mock.patch.object(
                        prefix.subprocess, "run",
                        side_effect=self._rng_abort_writer(
                            attempts, finish_on=2, pfx=pfx)), \
                    mock.patch.object(prefix, "stop_prefix_procs"):
                self.assertTrue(prefix.boot_prefix(pfx))

            repair.assert_called_once_with(pfx)
            self.assertEqual(len(attempts), 2)
            self.assertIn("cryptbase=b", attempts[0]["WINEDLLOVERRIDES"])
            self.assertIn("cryptbase=n,b", attempts[1]["WINEDLLOVERRIDES"])

    def test_rng_abort_is_retried_only_once(self):
        with tempfile.TemporaryDirectory() as td:
            pfx = Path(td) / "pfx"
            logs = Path(td) / "logs"
            attempts = []
            with mock.patch.object(prefix, "LOGS", logs), \
                    mock.patch(
                        "bol.gpu_safety.require_safe_graphics_session"), \
                    mock.patch.object(
                        prefix, "proton_umu_cmd",
                        return_value=(["umu", "wineboot"], {})), \
                    mock.patch.object(
                        prefix, "seed_managed_bootstrap_cryptbase",
                        return_value=True), \
                    mock.patch.object(
                        prefix, "repair_bootstrap_cryptbase",
                        return_value=True), \
                    mock.patch.object(
                        prefix.subprocess, "run",
                        side_effect=self._rng_abort_writer(attempts)), \
                    mock.patch.object(prefix, "stop_prefix_procs"), \
                    mock.patch.object(prefix, "warn") as warn:
                self.assertFalse(prefix.boot_prefix(pfx))

            self.assertEqual(len(attempts), 2)
            self.assertIn("timed out", warn.call_args.args[0])
            self.assertIn(
                str(logs / "native-login.log"), warn.call_args.args[0])

    def test_rng_abort_without_a_verified_component_is_reported(self):
        with tempfile.TemporaryDirectory() as td:
            pfx = Path(td) / "pfx"
            attempts = []
            with mock.patch.object(prefix, "LOGS", Path(td) / "logs"), \
                    mock.patch(
                        "bol.gpu_safety.require_safe_graphics_session"), \
                    mock.patch.object(
                        prefix, "proton_umu_cmd",
                        return_value=(["umu", "wineboot"], {})), \
                    mock.patch.object(
                        prefix, "seed_managed_bootstrap_cryptbase",
                        return_value=False), \
                    mock.patch.object(
                        prefix, "repair_bootstrap_cryptbase",
                        return_value=False), \
                    mock.patch.object(
                        prefix, "managed_bootstrap_prefix",
                        return_value=True), \
                    mock.patch.object(
                        prefix.subprocess, "run",
                        side_effect=self._rng_abort_writer(attempts)), \
                    mock.patch.object(prefix, "stop_prefix_procs"), \
                    mock.patch.object(prefix, "warn") as warn:
                self.assertFalse(prefix.boot_prefix(pfx))

            # Without a usable RNG component another attempt cannot help.
            self.assertEqual(len(attempts), 1)
            messages = " ".join(call.args[0] for call in warn.call_args_list)
            self.assertIn("No verified cryptbase.dll", messages)
            self.assertIn("Install / Update", messages)

    def test_rng_abort_on_a_user_supplied_prefix_names_the_manual_fix(self):
        """A prefix or engine we refuse to seed cannot be repaired for the
        user, so point at the file they have to place themselves rather than
        at a download that would change nothing."""
        with tempfile.TemporaryDirectory() as td:
            pfx = Path(td) / "pfx"
            attempts = []
            with mock.patch.object(prefix, "LOGS", Path(td) / "logs"), \
                    mock.patch(
                        "bol.gpu_safety.require_safe_graphics_session"), \
                    mock.patch.object(
                        prefix, "proton_umu_cmd",
                        return_value=(["umu", "wineboot"], {})), \
                    mock.patch.object(
                        prefix, "seed_managed_bootstrap_cryptbase",
                        return_value=False), \
                    mock.patch.object(
                        prefix, "repair_bootstrap_cryptbase",
                        return_value=False), \
                    mock.patch.object(
                        prefix, "managed_bootstrap_prefix",
                        return_value=False), \
                    mock.patch.object(
                        prefix.subprocess, "run",
                        side_effect=self._rng_abort_writer(attempts)), \
                    mock.patch.object(prefix, "stop_prefix_procs"), \
                    mock.patch.object(prefix, "warn") as warn:
                self.assertFalse(prefix.boot_prefix(pfx))

            messages = " ".join(call.args[0] for call in warn.call_args_list)
            self.assertIn("user-supplied", messages)
            self.assertIn("drive_c/windows/system32", messages)
            self.assertNotIn("No verified cryptbase.dll", messages)

    def test_wineboot_failure_names_the_rng_abort_that_caused_it(self):
        """A bare "timed out" sent this failure's reporters chasing Wine
        packages and file-descriptor limits; the abort has to be named."""
        with tempfile.TemporaryDirectory() as td:
            pfx = Path(td) / "pfx"
            with mock.patch.object(prefix, "LOGS", Path(td) / "logs"), \
                    mock.patch(
                        "bol.gpu_safety.require_safe_graphics_session"), \
                    mock.patch.object(
                        prefix, "proton_umu_cmd",
                        return_value=(["umu", "wineboot"], {})), \
                    mock.patch.object(
                        prefix, "seed_managed_bootstrap_cryptbase",
                        return_value=True), \
                    mock.patch.object(
                        prefix, "repair_bootstrap_cryptbase",
                        return_value=True), \
                    mock.patch.object(
                        prefix.subprocess, "run",
                        side_effect=self._rng_abort_writer([])), \
                    mock.patch.object(prefix, "stop_prefix_procs"), \
                    mock.patch.object(prefix, "warn") as warn:
                self.assertFalse(prefix.boot_prefix(pfx))

            self.assertIn(
                "advapi32.SystemFunction036", warn.call_args.args[0])

    def test_unrelated_wineboot_failure_is_not_blamed_on_the_rng(self):
        with tempfile.TemporaryDirectory() as td:
            pfx = Path(td) / "pfx"
            with mock.patch.object(prefix, "LOGS", Path(td) / "logs"), \
                    mock.patch(
                        "bol.gpu_safety.require_safe_graphics_session"), \
                    mock.patch.object(
                        prefix, "proton_umu_cmd",
                        return_value=(["umu", "wineboot"], {})), \
                    mock.patch.object(
                        prefix, "seed_managed_bootstrap_cryptbase",
                        return_value=True), \
                    mock.patch.object(
                        prefix.subprocess, "run",
                        return_value=SimpleNamespace(returncode=3)), \
                    mock.patch.object(prefix, "stop_prefix_procs"), \
                    mock.patch.object(prefix, "warn") as warn:
                self.assertFalse(prefix.boot_prefix(pfx))

            self.assertNotIn(
                "SystemFunction036", warn.call_args.args[0])

    def test_only_this_attempt_s_log_section_triggers_the_retry(self):
        with tempfile.TemporaryDirectory() as td:
            logs = Path(td) / "logs"
            logs.mkdir()
            log_path = logs / "native-login.log"
            log_path.write_text(
                "wine: Call from 0000 to unimplemented function "
                "advapi32.dll.SystemFunction036, aborting\n",
                encoding="utf-8",
            )
            stale = log_path.stat().st_size
            log_path.write_text(
                log_path.read_text(encoding="utf-8")
                + "wineboot: prefix updated\n",
                encoding="utf-8",
            )

            self.assertTrue(prefix._wineboot_hit_rng_abort(log_path))
            self.assertFalse(prefix._wineboot_hit_rng_abort(log_path, stale))

    def test_unrelated_wineboot_failure_is_not_retried(self):
        with tempfile.TemporaryDirectory() as td:
            attempts = []

            def fake_run(_cmd, **kwargs):
                attempts.append(True)
                kwargs["stdout"].write("err:winediag:something else\n")
                return SimpleNamespace(returncode=42)

            with mock.patch.object(prefix, "LOGS", Path(td) / "logs"), \
                    mock.patch(
                        "bol.gpu_safety.require_safe_graphics_session"), \
                    mock.patch.object(
                        prefix, "proton_umu_cmd",
                        return_value=(["umu", "wineboot"], {})), \
                    mock.patch.object(
                        prefix, "seed_managed_bootstrap_cryptbase",
                        return_value=True), \
                    mock.patch.object(
                        prefix, "repair_bootstrap_cryptbase") as repair, \
                    mock.patch.object(
                        prefix.subprocess, "run", side_effect=fake_run), \
                    mock.patch.object(prefix, "stop_prefix_procs"):
                self.assertFalse(prefix.boot_prefix(Path(td) / "pfx"))

            self.assertEqual(len(attempts), 1)
            repair.assert_not_called()

    def test_wineboot_propagates_shutdown_failure(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(prefix, "LOGS", Path(td) / "logs"), \
                mock.patch.object(
                    prefix, "proton_umu_cmd",
                    return_value=(["umu", "wineboot"], {})), \
                mock.patch("bol.gpu_safety.require_safe_graphics_session"), \
                mock.patch.object(prefix.subprocess, "run"), \
                mock.patch.object(
                    prefix, "stop_prefix_procs",
                    side_effect=BolError("wineserver survived")):
            with self.assertRaisesRegex(BolError, "wineserver survived"):
                prefix.boot_prefix(Path(td) / "pfx")


class PrefixEnvironmentTests(unittest.TestCase):
    def test_flatpak_uses_app_owned_steam_compat_directory(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            host_steam = root / "home/.steam/steam"
            host_steam.mkdir(parents=True)
            with mock.patch.dict(
                    prefix.os.environ,
                    {"FLATPAK_ID": "io.github.bedrock_on_linux"},
                    clear=True), \
                    mock.patch.object(prefix, "HOME", root / "home"), \
                    mock.patch.object(prefix, "DATA", root / "data"):
                selected = prefix.steam_compat_dir()

            self.assertEqual(selected, root / "data/steamcompat")
            self.assertTrue(selected.is_dir())
            self.assertNotEqual(selected, host_steam)

    def test_flatpak_info_file_also_selects_app_owned_compat_directory(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.dict(prefix.os.environ, {}, clear=True), \
                    mock.patch.object(prefix, "HOME", root / "home"), \
                    mock.patch.object(prefix, "DATA", root / "data"), \
                    mock.patch.object(
                        prefix.Path, "exists", autospec=True,
                        side_effect=lambda path: str(path) == "/.flatpak-info"):
                selected = prefix.steam_compat_dir()

            self.assertEqual(selected, root / "data/steamcompat")
            self.assertTrue(selected.is_dir())

    @contextmanager
    def _host(self, root):
        """A non-Flatpak host whose home and data live under ``root``."""
        exists = Path.exists

        def not_sandboxed(path, *args, **kwargs):
            if str(path) == "/.flatpak-info":
                return False
            return exists(path, *args, **kwargs)

        with mock.patch.dict(prefix.os.environ, {}, clear=True), \
                mock.patch.object(prefix, "HOME", root / "home"), \
                mock.patch.object(prefix, "DATA", root / "data"), \
                mock.patch.object(prefix.Path, "exists", autospec=True,
                                  side_effect=not_sandboxed):
            yield

    def test_host_without_steam_never_creates_steam_directory(self):
        # A real ~/.steam/steam directory is where Steam's bootstrapper has to
        # put a symlink; making one broke every later Steam install (#265).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "home").mkdir()
            with self._host(root):
                selected = prefix.steam_compat_dir()

            self.assertEqual(selected, root / "data/steamcompat")
            self.assertTrue(selected.is_dir())
            self.assertFalse((root / "home/.steam").exists())

    def test_empty_steam_directory_left_by_earlier_launchers_is_removed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "home/.steam/steam").mkdir(parents=True)
            with self._host(root):
                selected = prefix.steam_compat_dir()

            self.assertEqual(selected, root / "data/steamcompat")
            self.assertFalse((root / "home/.steam").exists())

    def test_real_steam_installations_are_used_and_left_alone(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = root / "home/.local/share/Steam"
            data.mkdir(parents=True)
            (data / "steam.sh").write_text("#!/bin/sh\n")
            linked = root / "home/.steam/steam"
            linked.parent.mkdir(parents=True)
            linked.symlink_to(data)
            with self._host(root):
                self.assertEqual(prefix.steam_compat_dir(), linked)
            self.assertTrue(linked.is_symlink())

            linked.unlink()
            linked.mkdir()
            (linked / "steam.sh").write_text("#!/bin/sh\n")
            with self._host(root):
                self.assertEqual(prefix.steam_compat_dir(), linked)
            self.assertTrue((linked / "steam.sh").is_file())
            self.assertFalse((root / "data/steamcompat").exists())

    def test_uninstalled_steam_link_falls_back_without_touching_it(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            linked = root / "home/.steam/steam"
            linked.parent.mkdir(parents=True)
            linked.symlink_to(root / "home/.local/share/Steam")
            with self._host(root):
                selected = prefix.steam_compat_dir()

            self.assertEqual(selected, root / "data/steamcompat")
            self.assertTrue(linked.is_symlink())

    def test_headless_setup_forces_builtin_cryptbase(self):
        env = {
            "DISPLAY": ":0",
            "WAYLAND_DISPLAY": "wayland-0",
            "PROTON_ENABLE_WAYLAND": "1",
            "WINEDLLOVERRIDES": "cryptbase=n,b;foo=n;dxgi=n",
        }
        result = prefix.headless_setup_env(env)
        overrides = result["WINEDLLOVERRIDES"].split(";")

        self.assertNotIn("DISPLAY", result)
        self.assertNotIn("WAYLAND_DISPLAY", result)
        self.assertNotIn("PROTON_ENABLE_WAYLAND", result)
        self.assertEqual(result["SDL_VIDEODRIVER"], "dummy")
        self.assertEqual(overrides.count("cryptbase=b"), 1)
        self.assertIn("foo=n", overrides)
        self.assertNotIn("cryptbase=n,b", overrides)
        self.assertNotIn("dxgi=n", overrides)

    def test_headless_setup_prefers_seeded_native_cryptbase(self):
        result = prefix.headless_setup_env(
            {"WINEDLLOVERRIDES": "cryptbase=b;foo=n"},
            native_cryptbase=True,
        )
        overrides = result["WINEDLLOVERRIDES"].split(";")

        self.assertEqual(overrides.count("cryptbase=n,b"), 1)
        self.assertNotIn("cryptbase=b", overrides)
        self.assertIn("foo=n", overrides)

    def test_bootstrap_cryptbase_is_seeded_into_fresh_managed_prefix(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pfx = root / "fresh-prefix"
            engine = root / "managed-engine"
            engine.mkdir()

            with mock.patch.dict(prefix.os.environ, {}, clear=True), \
                    mock.patch.object(prefix, "PFX", pfx), \
                    mock.patch.object(prefix, "WINEGDK_OUT", engine), \
                    mock.patch.object(
                        prefix, "proton_path", return_value=engine), \
                    mock.patch(
                        "bol.fixups._install_cryptbase_in_prefix",
                        return_value=True) as install:
                self.assertTrue(
                    prefix.seed_managed_bootstrap_cryptbase(pfx))

            self.assertTrue(
                (pfx / "drive_c/windows/system32").is_dir())
            install.assert_called_once_with(pfx)

    def test_bootstrap_cryptbase_never_mutates_explicit_prefix(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            managed = root / "managed-prefix"
            external = root / "external-prefix"
            engine = root / "managed-engine"
            with mock.patch.dict(
                    prefix.os.environ,
                    {"BOL_WINEPREFIX": str(external)}, clear=True), \
                    mock.patch.object(prefix, "PFX", managed), \
                    mock.patch.object(prefix, "WINEGDK_OUT", engine), \
                    mock.patch.object(
                        prefix, "proton_path", return_value=engine), \
                    mock.patch(
                        "bol.fixups._install_cryptbase_in_prefix") as install:
                self.assertFalse(
                    prefix.seed_managed_bootstrap_cryptbase(external))

            self.assertFalse(external.exists())
            install.assert_not_called()

    def test_bootstrap_cryptbase_never_mutates_custom_engine_prefix(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pfx = root / "managed-prefix"
            managed_engine = root / "managed-engine"
            custom_engine = root / "custom-engine"
            with mock.patch.dict(prefix.os.environ, {}, clear=True), \
                    mock.patch.object(prefix, "PFX", pfx), \
                    mock.patch.object(
                        prefix, "WINEGDK_OUT", managed_engine), \
                    mock.patch.object(
                        prefix, "proton_path", return_value=custom_engine), \
                    mock.patch(
                        "bol.fixups._install_cryptbase_in_prefix") as install:
                self.assertFalse(
                    prefix.seed_managed_bootstrap_cryptbase(pfx))

            self.assertFalse(pfx.exists())
            install.assert_not_called()

    def test_bootstrap_cryptbase_rejects_system32_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pfx = root / "managed-prefix"
            engine = root / "managed-engine"
            external = root / "external"
            (pfx / "drive_c/windows").mkdir(parents=True)
            engine.mkdir()
            external.mkdir()
            (pfx / "drive_c/windows/system32").symlink_to(
                external, target_is_directory=True)

            with mock.patch.dict(prefix.os.environ, {}, clear=True), \
                    mock.patch.object(prefix, "PFX", pfx), \
                    mock.patch.object(prefix, "WINEGDK_OUT", engine), \
                    mock.patch.object(
                        prefix, "proton_path", return_value=engine), \
                    mock.patch(
                        "bol.fixups._install_cryptbase_in_prefix") as install:
                with self.assertRaisesRegex(BolError, "unsafe system32"):
                    prefix.seed_managed_bootstrap_cryptbase(pfx)

            self.assertEqual(list(external.iterdir()), [])
            install.assert_not_called()

    def test_proton_umu_gameid_carries_the_inherited_steam_application_id(
            self):
        # UMU rebuilds SteamAppId/SteamGameId inside the container from
        # GAMEID, so its `umu-default` fallback turns a launch Steam started
        # into the literal identity "default" (#199).
        cases = [
            ({"GAMEID": "umu-existing"}, "umu-existing"),
            ({"GAMEID": "17"}, "17"),
            ({"GAMEID": "0", "SteamAppId": "123",
              "SteamGameId": "456"}, "0"),
            ({"SteamAppId": "2716672805",
              "SteamGameId": "11668020851441139712"}, "umu-2716672805"),
            ({"SteamAppId": "invalid",
              "SteamGameId": "456"}, "umu-default"),
            ({"SteamAppId": "0", "SteamGameId": ""}, "umu-default"),
            ({}, "umu-default"),
        ]
        for inherited, expected in cases:
            with self.subTest(inherited=inherited), \
                    mock.patch.dict(prefix.os.environ, inherited, clear=True), \
                    mock.patch.object(
                        prefix, "steam_compat_dir",
                        return_value=Path("/tmp/steam")), \
                    mock.patch.object(
                        prefix, "proton_path",
                        return_value=Path("/tmp/proton")), \
                    mock.patch.object(
                        prefix, "ensure_umu",
                        return_value=Path("/tmp/umu-run")), \
                    mock.patch.object(prefix, "info"):
                _cmd, env = prefix.proton_umu_cmd(
                    "Minecraft.Windows.exe", Path("/tmp/external-prefix"))

            self.assertEqual(env["GAMEID"], expected)
            self.assertEqual(env["UMU_FOLDERS_PATH"], str(prefix.DATA))
            for name in ("SteamAppId", "SteamGameId"):
                if name in inherited:
                    self.assertEqual(env[name], inherited[name])

    def test_managed_engine_forces_pure_wow64_runtime(self):
        managed = Path("/tmp/managed-winegdk")
        with mock.patch.dict(prefix.os.environ, {}, clear=True), \
                mock.patch.object(prefix, "WINEGDK_OUT", managed), \
                mock.patch.object(prefix, "steam_compat_dir",
                                  return_value=Path("/tmp/steam")), \
                mock.patch.object(prefix, "proton_path",
                                  return_value=managed), \
                mock.patch.object(prefix, "ensure_umu",
                                  return_value=Path("/tmp/umu-run")):
            _cmd, env = prefix.proton_umu_cmd(
                "Minecraft.Windows.exe", Path("/tmp/prefix"))

        self.assertEqual(env["PROTON_USE_WOW64"], "1")

    def test_custom_engine_does_not_force_pure_wow64_runtime(self):
        managed = Path("/tmp/managed-winegdk")
        custom = Path("/tmp/custom-engine")
        with mock.patch.dict(prefix.os.environ, {}, clear=True), \
                mock.patch.object(prefix, "WINEGDK_OUT", managed), \
                mock.patch.object(prefix, "steam_compat_dir",
                                  return_value=Path("/tmp/steam")), \
                mock.patch.object(prefix, "proton_path",
                                  return_value=custom), \
                mock.patch.object(prefix, "ensure_umu",
                                  return_value=Path("/tmp/umu-run")):
            _cmd, env = prefix.proton_umu_cmd(
                "Minecraft.Windows.exe", Path("/tmp/prefix"))

        self.assertNotIn("PROTON_USE_WOW64", env)


class CryptbaseRepairTests(unittest.TestCase):
    def test_repair_refetches_the_payload_before_seeding_again(self):
        order = []
        with mock.patch.object(
                fixups, "ensure_openssl_xcurl_set",
                side_effect=lambda: order.append("fetch")), \
                mock.patch.object(
                    prefix, "seed_managed_bootstrap_cryptbase",
                    side_effect=lambda _p: order.append("seed") or True):
            self.assertTrue(
                prefix.repair_bootstrap_cryptbase(Path("/tmp/bol-pfx")))
        self.assertEqual(order, ["fetch", "seed"])

    def test_repair_still_seeds_when_the_download_fails(self):
        with mock.patch.object(
                fixups, "ensure_openssl_xcurl_set",
                side_effect=OSError("offline")), \
                mock.patch.object(
                    prefix, "seed_managed_bootstrap_cryptbase",
                    return_value=False) as seed, \
                mock.patch.object(prefix, "warn") as warn:
            self.assertFalse(
                prefix.repair_bootstrap_cryptbase(Path("/tmp/bol-pfx")))
        seed.assert_called_once_with(Path("/tmp/bol-pfx"))
        self.assertIn("offline", warn.call_args.args[0])


class CryptbaseInstallTests(unittest.TestCase):
    def test_install_is_atomic_and_preserves_existing_file_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source_dir = root / "openssl-set"
            source_dir.mkdir()
            (source_dir / "cryptbase.dll").write_bytes(b"verified-rng")
            pfx = root / "prefix"
            system32 = pfx / "drive_c/windows/system32"
            system32.mkdir(parents=True)
            destination = system32 / "cryptbase.dll"
            destination.write_bytes(b"old-placeholder")

            with mock.patch.object(
                    fixups, "OPENSSL_XCURL_SET", source_dir), \
                    mock.patch.object(fixups, "ok"):
                self.assertTrue(
                    fixups._install_cryptbase_in_prefix(pfx))
                self.assertTrue(
                    fixups._install_cryptbase_in_prefix(pfx))

            self.assertEqual(destination.read_bytes(), b"verified-rng")
            self.assertEqual(
                (system32 / "cryptbase.dll.bol-orig").read_bytes(),
                b"old-placeholder",
            )
            self.assertEqual(
                list(system32.glob(".cryptbase.dll-*")), [])

    def test_install_replaces_symlink_without_touching_its_target(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source_dir = root / "openssl-set"
            source_dir.mkdir()
            (source_dir / "cryptbase.dll").write_bytes(b"verified-rng")
            pfx = root / "prefix"
            system32 = pfx / "drive_c/windows/system32"
            system32.mkdir(parents=True)
            external = root / "external-cryptbase.dll"
            external.write_bytes(b"external")
            destination = system32 / "cryptbase.dll"
            destination.symlink_to(external)

            with mock.patch.object(
                    fixups, "OPENSSL_XCURL_SET", source_dir), \
                    mock.patch.object(fixups, "ok"):
                self.assertTrue(
                    fixups._install_cryptbase_in_prefix(pfx))

            self.assertFalse(destination.is_symlink())
            self.assertEqual(destination.read_bytes(), b"verified-rng")
            self.assertEqual(external.read_bytes(), b"external")
            backup = system32 / "cryptbase.dll.bol-orig"
            self.assertTrue(backup.is_symlink())
            self.assertEqual(os.readlink(backup), str(external))


class ManagedRuntimeRepairTests(unittest.TestCase):
    def _paths(self, root):
        engine = root / "engine"
        pfx = root / "prefix"
        source = engine / "files/lib/wine/x86_64-windows/user32.dll"
        target = pfx / "drive_c/windows/system32/user32.dll"
        source.parent.mkdir(parents=True)
        _make_ready_prefix(pfx)
        return engine, pfx, source, target

    def test_corrupt_managed_user32_is_backed_up_and_replaced(self):
        with tempfile.TemporaryDirectory() as td:
            engine, pfx, source, target = self._paths(Path(td))
            source.write_bytes(b"verified-user32")
            target.write_bytes(b"corrupt-user32")
            with mock.patch.dict(prefix.os.environ, {}, clear=True), \
                    mock.patch.object(prefix, "PFX", pfx), \
                    mock.patch.object(prefix, "WINEGDK_OUT", engine), \
                    mock.patch.object(prefix, "proton_path",
                                      return_value=engine), \
                    mock.patch.object(prefix, "require_prefix_idle") as idle, \
                    mock.patch.object(prefix, "ok"):
                changed = prefix.repair_managed_prefix_user32(pfx)

            self.assertTrue(changed)
            self.assertEqual(target.read_bytes(), b"verified-user32")
            self.assertEqual(
                target.with_name(
                    "user32.dll.bol-managed-backup").read_bytes(),
                b"corrupt-user32",
            )
            idle.assert_called_once_with(
                pfx, "repair the managed Wine runtime")
            self.assertEqual(list(target.parent.glob(".user32.dll-*")), [])

    def test_matching_managed_user32_is_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            engine, pfx, source, target = self._paths(Path(td))
            source.write_bytes(b"verified-user32")
            target.write_bytes(b"verified-user32")
            with mock.patch.dict(prefix.os.environ, {}, clear=True), \
                    mock.patch.object(prefix, "PFX", pfx), \
                    mock.patch.object(prefix, "WINEGDK_OUT", engine), \
                    mock.patch.object(prefix, "proton_path",
                                      return_value=engine), \
                    mock.patch.object(prefix, "require_prefix_idle") as idle:
                changed = prefix.repair_managed_prefix_user32(pfx)

            self.assertFalse(changed)
            idle.assert_not_called()
            self.assertFalse(target.with_name(
                "user32.dll.bol-managed-backup").exists())

    def test_matching_managed_user32_symlink_is_replaced(self):
        with tempfile.TemporaryDirectory() as td:
            engine, pfx, source, target = self._paths(Path(td))
            source.write_bytes(b"verified-user32")
            target.symlink_to(source)
            with mock.patch.dict(prefix.os.environ, {}, clear=True), \
                    mock.patch.object(prefix, "PFX", pfx), \
                    mock.patch.object(prefix, "WINEGDK_OUT", engine), \
                    mock.patch.object(prefix, "proton_path",
                                      return_value=engine), \
                    mock.patch.object(prefix, "require_prefix_idle") as idle:
                changed = prefix.repair_managed_prefix_user32(pfx)

            self.assertTrue(changed)
            self.assertFalse(target.is_symlink())
            self.assertEqual(target.read_bytes(), b"verified-user32")
            backup = target.with_name("user32.dll.bol-managed-backup")
            self.assertTrue(backup.is_symlink())
            self.assertEqual(os.readlink(backup), str(source))
            idle.assert_called_once_with(
                pfx, "repair the managed Wine runtime")

    def test_external_system32_link_is_rejected_without_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            engine, pfx, source, target = self._paths(root)
            source.write_bytes(b"verified-user32")
            target.parent.rmdir()
            external = root / "external-system32"
            external.mkdir()
            external_target = external / "user32.dll"
            external_target.write_bytes(b"external-user32")
            target.parent.symlink_to(external, target_is_directory=True)

            with mock.patch.dict(prefix.os.environ, {}, clear=True), \
                    mock.patch.object(prefix, "PFX", pfx), \
                    mock.patch.object(prefix, "WINEGDK_OUT", engine), \
                    mock.patch.object(prefix, "proton_path",
                                      return_value=engine), \
                    mock.patch.object(prefix, "require_prefix_idle") as idle:
                with self.assertRaisesRegex(BolError, "unsafe system32"):
                    prefix.repair_managed_prefix_user32(pfx)

            self.assertEqual(external_target.read_bytes(), b"external-user32")
            self.assertFalse((external / (
                "user32.dll.bol-managed-backup")).exists())
            idle.assert_not_called()

    def test_custom_engine_and_external_prefix_are_never_repaired(self):
        with tempfile.TemporaryDirectory() as td:
            engine, pfx, source, target = self._paths(Path(td))
            custom = Path(td) / "custom-engine"
            external = Path(td) / "external-prefix"
            source.write_bytes(b"verified-user32")
            target.write_bytes(b"corrupt-user32")
            with mock.patch.dict(prefix.os.environ, {}, clear=True), \
                    mock.patch.object(prefix, "PFX", pfx), \
                    mock.patch.object(prefix, "WINEGDK_OUT", engine), \
                    mock.patch.object(prefix, "proton_path",
                                      return_value=custom):
                self.assertFalse(
                    prefix.repair_managed_prefix_user32(pfx))
            with mock.patch.dict(prefix.os.environ, {}, clear=True), \
                    mock.patch.object(prefix, "PFX", pfx), \
                    mock.patch.object(prefix, "WINEGDK_OUT", engine), \
                    mock.patch.object(prefix, "proton_path",
                                      return_value=engine):
                self.assertFalse(
                    prefix.repair_managed_prefix_user32(external))

            self.assertEqual(target.read_bytes(), b"corrupt-user32")


class ManagedRuntimeRefreshTests(unittest.TestCase):
    """Engine upgrades must refresh the prefix's cached Windows system DLLs.

    Swapping the WineGDK engine while reusing a prefix built by the previous
    engine leaves stale system32/syswow64 DLLs that fault Minecraft's
    account/menu path under the pure-WoW64 runtime (issue #135)."""

    def _runtime_paths(self, root, marker_rev="__absent__"):
        engine = root / "engine"
        pfx = root / "prefix"
        e64 = engine / "files/lib/wine/x86_64-windows"
        e32 = engine / "files/lib/wine/i386-windows"
        e64.mkdir(parents=True)
        e32.mkdir(parents=True)
        (e64 / "user32.dll").write_bytes(b"new-user32-64")
        (e64 / "ntdll.dll").write_bytes(b"new-ntdll-64")
        (e32 / "user32.dll").write_bytes(b"new-user32-32")
        _make_ready_prefix(pfx)
        sys32 = pfx / "drive_c/windows/system32"
        wow = pfx / "drive_c/windows/syswow64"
        wow.mkdir(parents=True, exist_ok=True)
        (sys32 / "user32.dll").write_bytes(b"old-user32-64")
        (sys32 / "ntdll.dll").write_bytes(b"old-ntdll-64")
        (wow / "user32.dll").write_bytes(b"old-user32-32")
        users = (pfx / "drive_c/users/steamuser/AppData/Roaming/"
                 "Minecraft Bedrock")
        users.mkdir(parents=True)
        (users / "worlds.mcworld").write_bytes(b"precious-world")
        if marker_rev != "__absent__":
            (pfx / prefix.ENGINE_REV_MARKER).write_text(marker_rev + "\n")
        return engine, pfx

    def _managed(self, engine, pfx):
        return mock.patch.dict(prefix.os.environ, {}, clear=True), \
            mock.patch.object(prefix, "PFX", pfx), \
            mock.patch.object(prefix, "WINEGDK_OUT", engine), \
            mock.patch.object(prefix, "proton_path", return_value=engine)

    def test_engine_upgrade_refreshes_stale_system_dlls(self):
        with tempfile.TemporaryDirectory() as td:
            engine, pfx = self._runtime_paths(Path(td))
            m = self._managed(engine, pfx)
            with m[0], m[1], m[2], m[3], \
                    mock.patch.object(prefix, "require_prefix_idle") as idle, \
                    mock.patch.object(prefix, "ok"):
                changed = prefix.refresh_managed_prefix_runtime(pfx)

            self.assertTrue(changed)
            sys32 = pfx / "drive_c/windows/system32"
            wow = pfx / "drive_c/windows/syswow64"
            self.assertEqual((sys32 / "user32.dll").read_bytes(),
                             b"new-user32-64")
            self.assertEqual((sys32 / "ntdll.dll").read_bytes(),
                             b"new-ntdll-64")
            self.assertEqual((wow / "user32.dll").read_bytes(),
                             b"new-user32-32")
            self.assertEqual(
                (sys32 / "user32.dll.bol-runtime-backup").read_bytes(),
                b"old-user32-64")
            idle.assert_called_once_with(
                pfx, "refresh the managed Wine runtime")
            self.assertEqual(list(sys32.glob(".bol-runtime-*")), [])
            # The user profile (worlds, settings, login) is preserved.
            self.assertEqual(
                (pfx / "drive_c/users/steamuser/AppData/Roaming/"
                 "Minecraft Bedrock/worlds.mcworld").read_bytes(),
                b"precious-world")

    def test_matching_runtime_is_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            engine, pfx = self._runtime_paths(Path(td))
            sys32 = pfx / "drive_c/windows/system32"
            wow = pfx / "drive_c/windows/syswow64"
            (sys32 / "user32.dll").write_bytes(b"new-user32-64")
            (sys32 / "ntdll.dll").write_bytes(b"new-ntdll-64")
            (wow / "user32.dll").write_bytes(b"new-user32-32")
            m = self._managed(engine, pfx)
            with m[0], m[1], m[2], m[3], \
                    mock.patch.object(prefix, "require_prefix_idle") as idle:
                changed = prefix.refresh_managed_prefix_runtime(pfx)

            self.assertFalse(changed)
            idle.assert_not_called()
            self.assertFalse(
                (sys32 / "user32.dll.bol-runtime-backup").exists())

    def test_custom_engine_and_external_prefix_are_never_refreshed(self):
        with tempfile.TemporaryDirectory() as td:
            engine, pfx = self._runtime_paths(Path(td))
            custom = Path(td) / "custom-engine"
            external = Path(td) / "external-prefix"
            with mock.patch.dict(prefix.os.environ, {}, clear=True), \
                    mock.patch.object(prefix, "PFX", pfx), \
                    mock.patch.object(prefix, "WINEGDK_OUT", engine), \
                    mock.patch.object(prefix, "proton_path",
                                      return_value=custom):
                self.assertFalse(prefix.refresh_managed_prefix_runtime(pfx))
            with mock.patch.dict(prefix.os.environ, {}, clear=True), \
                    mock.patch.object(prefix, "PFX", pfx), \
                    mock.patch.object(prefix, "WINEGDK_OUT", engine), \
                    mock.patch.object(prefix, "proton_path",
                                      return_value=engine):
                self.assertFalse(
                    prefix.refresh_managed_prefix_runtime(external))
            with mock.patch.dict(prefix.os.environ,
                                 {"BOL_WINEPREFIX": str(external)},
                                 clear=True), \
                    mock.patch.object(prefix, "PFX", pfx), \
                    mock.patch.object(prefix, "WINEGDK_OUT", engine), \
                    mock.patch.object(prefix, "proton_path",
                                      return_value=engine):
                self.assertFalse(prefix.refresh_managed_prefix_runtime(pfx))

            self.assertEqual(
                (pfx / "drive_c/windows/system32/user32.dll").read_bytes(),
                b"old-user32-64")

    def test_managed_prefix_engine_is_stale(self):
        with tempfile.TemporaryDirectory() as td:
            engine, pfx = self._runtime_paths(Path(td))
            m = self._managed(engine, pfx)
            with m[0], m[1], m[2], m[3]:
                # Missing marker (in-place upgrade) counts as stale.
                self.assertTrue(prefix.managed_prefix_engine_is_stale(pfx))
                (pfx / prefix.ENGINE_REV_MARKER).write_text(
                    "wow64-archs-native6\n")
                self.assertTrue(prefix.managed_prefix_engine_is_stale(pfx))
                (pfx / prefix.ENGINE_REV_MARKER).write_text(
                    prefix.WINEGDK_BUILD_REV + "\n")
                self.assertFalse(prefix.managed_prefix_engine_is_stale(pfx))

    def test_boot_prefix_refreshes_and_records_marker_on_engine_change(self):
        with tempfile.TemporaryDirectory() as td:
            engine, pfx = self._runtime_paths(Path(td))
            compat = Path(td) / "compatdata"
            m = self._managed(engine, pfx)
            with m[0], m[1], m[2], m[3], \
                    mock.patch.object(prefix, "COMPAT", compat), \
                    mock.patch.object(prefix, "require_prefix_idle"), \
                    mock.patch.object(
                        prefix, "repair_managed_prefix_user32") as repair, \
                    mock.patch.object(prefix, "ok"):
                self.assertTrue(prefix.boot_prefix(pfx))

            self.assertEqual(
                (pfx / "drive_c/windows/system32/ntdll.dll").read_bytes(),
                b"new-ntdll-64")
            self.assertEqual(prefix.read_managed_engine_rev(pfx),
                             prefix.WINEGDK_BUILD_REV)
            repair.assert_called_once_with(pfx)

    def test_boot_prefix_skips_refresh_when_engine_matches(self):
        with tempfile.TemporaryDirectory() as td:
            engine, pfx = self._runtime_paths(
                Path(td), marker_rev=prefix.WINEGDK_BUILD_REV)
            compat = Path(td) / "compatdata"
            m = self._managed(engine, pfx)
            with m[0], m[1], m[2], m[3], \
                    mock.patch.object(prefix, "COMPAT", compat), \
                    mock.patch.object(prefix, "require_prefix_idle") as idle, \
                    mock.patch.object(
                        prefix, "repair_managed_prefix_user32"), \
                    mock.patch.object(prefix, "ok"):
                self.assertTrue(prefix.boot_prefix(pfx))

            idle.assert_not_called()
            self.assertEqual(
                (pfx / "drive_c/windows/system32/user32.dll").read_bytes(),
                b"old-user32-64")

    def test_engine_only_dlls_are_not_injected_into_prefix(self):
        # A DLL the engine ships but the prefix lacks is deliberately NOT
        # copied in: a prefix's system dir is the subset wineboot installed
        # and Wine loads the rest from the engine dir. Only the prefix's
        # existing DLLs are refreshed (issue #135).
        with tempfile.TemporaryDirectory() as td:
            engine, pfx = self._runtime_paths(Path(td))
            e64 = engine / "files/lib/wine/x86_64-windows"
            (e64 / "xinput.dll").write_bytes(b"engine-only-xinput")
            m = self._managed(engine, pfx)
            with m[0], m[1], m[2], m[3], \
                    mock.patch.object(prefix, "require_prefix_idle") as idle, \
                    mock.patch.object(prefix, "ok"):
                changed = prefix.refresh_managed_prefix_runtime(pfx)

            sys32 = pfx / "drive_c/windows/system32"
            self.assertTrue(changed)
            self.assertEqual((sys32 / "user32.dll").read_bytes(),
                             b"new-user32-64")
            self.assertEqual((sys32 / "ntdll.dll").read_bytes(),
                             b"new-ntdll-64")
            self.assertFalse((sys32 / "xinput.dll").exists())
            self.assertFalse(
                (sys32 / "xinput.dll.bol-runtime-backup").exists())
            idle.assert_called_once_with(
                pfx, "refresh the managed Wine runtime")


class PrefixSetupTests(unittest.TestCase):
    def test_setup_stops_before_gameinput_when_prefix_boot_fails(self):
        with tempfile.TemporaryDirectory() as td:
            settings = {
                "game_dir": td,
                "proton_source": "proton",
            }
            with mock.patch.object(gamesetup, "mkdirs"), \
                    mock.patch.object(gamesetup, "load_settings",
                                      return_value=settings), \
                    mock.patch.object(gamesetup, "ensure_login_deps"), \
                    mock.patch.object(gamesetup, "_game_root",
                                      return_value=True), \
                    mock.patch.object(gamesetup, "ensure_proton"), \
                    mock.patch.object(gamesetup, "ensure_umu"), \
                    mock.patch.object(gamesetup, "fix_curl_ssl"), \
                    mock.patch.object(gamesetup, "boot_prefix",
                                      return_value=False), \
                    mock.patch.object(
                        gamesetup, "install_gameinput") as install, \
                    mock.patch.object(
                        gamesetup, "hide_signin_button") as hide, \
                    mock.patch.object(gamesetup, "ok") as setup_ok:
                with self.assertRaisesRegex(
                        BolError, "Could not initialise the Wine prefix"):
                    gamesetup._do_setup()

            install.assert_not_called()
            hide.assert_not_called()
            setup_ok.assert_not_called()

    def test_gameinput_refuses_to_materialise_an_incomplete_prefix(self):
        with tempfile.TemporaryDirectory() as td:
            pfx = Path(td) / "missing-prefix"
            with self.assertRaisesRegex(BolError, "prefix is incomplete"):
                gameinput.install_gameinput(pfx, Path(td) / "game")
            self.assertFalse(pfx.exists())


class PrefixOperationLockTests(unittest.TestCase):
    def test_repair_removes_managed_tree_for_clean_next_setup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            compat = root / "compat"
            pfx = compat / "pfx"
            pfx.mkdir(parents=True)
            (pfx / "broken-system.reg").write_text("partial")

            with mock.patch.object(prefix, "DATA", root / "data"), \
                    mock.patch.object(prefix, "COMPAT", compat), \
                    mock.patch.object(prefix, "PFX", pfx), \
                    mock.patch.object(
                        prefix, "stop_prefix_procs", return_value=(0, 0)
                    ) as stop, \
                    mock.patch.object(prefix, "require_prefix_idle") as idle, \
                    mock.patch.object(prefix, "ok") as repaired:
                prefix.reset_prefix()

            self.assertFalse(compat.exists())
            stop.assert_called_once_with(pfx)
            idle.assert_called_once_with(pfx, "repair the Wine prefix")
            repaired.assert_called_once()

    def test_repair_does_not_report_success_when_removal_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            compat = root / "compat"
            pfx = compat / "pfx"
            pfx.mkdir(parents=True)

            with mock.patch.object(prefix, "DATA", root / "data"), \
                    mock.patch.object(prefix, "COMPAT", compat), \
                    mock.patch.object(prefix, "PFX", pfx), \
                    mock.patch.object(
                        prefix, "stop_prefix_procs", return_value=(0, 0)
                    ), \
                    mock.patch.object(prefix, "require_prefix_idle"), \
                    mock.patch.object(
                        prefix.shutil,
                        "rmtree",
                        side_effect=PermissionError("read-only prefix"),
                    ), \
                    mock.patch.object(prefix, "ok") as repaired, \
                    self.assertRaisesRegex(
                        PermissionError, "read-only prefix"
                    ):
                prefix.reset_prefix()

            repaired.assert_not_called()

    @contextmanager
    def _repairing(self, root, compat):
        with mock.patch.object(prefix, "DATA", root / "data"), \
                mock.patch.object(prefix, "COMPAT", compat), \
                mock.patch.object(prefix, "PFX", compat / "pfx"), \
                mock.patch.object(
                    prefix, "stop_prefix_procs", return_value=(0, 0)), \
                mock.patch.object(prefix, "require_prefix_idle"), \
                mock.patch.object(prefix, "warn"), \
                mock.patch.object(prefix, "ok") as repaired:
            yield repaired

    @staticmethod
    def _played_prefix(compat):
        """A prefix that has run the game: Wine state plus the player's."""
        pfx = compat / "pfx"
        (pfx / "drive_c/windows/system32").mkdir(parents=True)
        (pfx / "drive_c/windows/system32/user32.dll").write_bytes(b"stale")
        (pfx / "system.reg").write_text("WINE REGISTRY Version 2\n")
        roaming = pfx / "drive_c/users/steamuser/AppData/Roaming"
        roaming.mkdir(parents=True)
        # Proton links the Unix login name to steamuser in every prefix, so
        # each folder below is also reachable a second time through it.
        (pfx / "drive_c/users/player").symlink_to("steamuser")
        world = (roaming / "Minecraft Bedrock/Users/1557/games/com.mojang/"
                 "minecraftWorlds/abc=/level.dat")
        world.parent.mkdir(parents=True)
        world.write_bytes(b"my world")
        options = (roaming / "Minecraft Bedrock Preview/Users/Shared/games/"
                   "com.mojang/minecraftpe/options.txt")
        options.parent.mkdir(parents=True)
        options.write_text("gfx_viewdistance:320\n")
        (roaming / "Microsoft/Crypto").mkdir(parents=True)
        return pfx, world.relative_to(pfx), options.relative_to(pfx)

    def test_repair_keeps_minecraft_worlds_and_settings(self):
        # Repair deleted the whole compatibility tree, and the worlds with it,
        # while its button promised they would be kept (#253).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            compat = root / "compat"
            pfx, world, options = self._played_prefix(compat)

            with self._repairing(root, compat) as repaired:
                prefix.reset_prefix()

            self.assertEqual((pfx / world).read_bytes(), b"my world")
            self.assertEqual((pfx / options).read_text(),
                             "gfx_viewdistance:320\n")
            self.assertFalse((pfx / "system.reg").exists())
            self.assertFalse((pfx / "drive_c/windows").exists())
            self.assertFalse(
                (pfx / "drive_c/users/steamuser/AppData/Roaming/Microsoft")
                .exists())
            self.assertFalse(prefix.prefix_ready(pfx))
            self.assertEqual(
                sorted(path.name for path in root.iterdir()),
                ["compat", "data"])
            self.assertIn("worlds and settings were kept",
                          repaired.call_args.args[0])

    def test_a_folder_that_cannot_move_leaves_the_prefix_intact(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            compat = root / "compat"
            pfx, world, options = self._played_prefix(compat)
            rename = os.rename
            calls = []

            def fail_on_the_second_folder(source, target):
                calls.append(source)
                if len(calls) == 3:
                    raise PermissionError("read-only world folder")
                return rename(source, target)

            with self._repairing(root, compat) as repaired, \
                    mock.patch.object(prefix.os, "rename",
                                      side_effect=fail_on_the_second_folder), \
                    self.assertRaisesRegex(
                        BolError, "before deleting anything"):
                prefix.reset_prefix()

            repaired.assert_not_called()
            self.assertEqual((pfx / world).read_bytes(), b"my world")
            self.assertTrue((pfx / options).is_file())
            self.assertTrue((pfx / "system.reg").is_file())
            self.assertEqual(
                sorted(path.name for path in root.iterdir()),
                ["compat", "data"])

    def test_interrupted_repair_leftovers_are_cleared_but_worlds_are_not(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            compat = root / "compat"
            compat.mkdir()
            garbage = root / ".compatdata-repair-deleting"
            (garbage / "pfx/drive_c/windows").mkdir(parents=True)
            stranded = root / ".compatdata-repair-moving"
            self._played_prefix(stranded)

            with self._repairing(root, compat):
                prefix.reset_prefix()

            self.assertFalse(garbage.exists())
            self.assertTrue(stranded.is_dir())

    def test_repair_unlinks_dangling_compat_symlink_without_following_it(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            compat = root / "compat"
            compat.symlink_to(root / "missing-target")
            pfx = compat / "pfx"

            with mock.patch.object(prefix, "DATA", root / "data"), \
                    mock.patch.object(prefix, "COMPAT", compat), \
                    mock.patch.object(prefix, "PFX", pfx), \
                    mock.patch.object(
                        prefix, "stop_prefix_procs", return_value=(0, 0)
                    ), \
                    mock.patch.object(prefix, "require_prefix_idle"), \
                    mock.patch.object(prefix.shutil, "rmtree") as rmtree, \
                    mock.patch.object(prefix, "ok") as repaired:
                prefix.reset_prefix()

            self.assertFalse(compat.is_symlink())
            rmtree.assert_not_called()
            repaired.assert_called_once()

    def test_repair_cannot_run_while_common_lock_is_held(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(prefix, "DATA", Path(td)), \
                mock.patch.object(prefix, "stop_prefix_procs") as stop, \
                mock.patch.object(prefix.shutil, "rmtree") as rmtree:
            with prefix.prefix_operation_lock("test operation"):
                with self.assertRaisesRegex(BolError, "another BedrockOnLinux"):
                    prefix.reset_prefix()

        stop.assert_not_called()
        rmtree.assert_not_called()

    def test_logout_is_refused_while_the_common_lock_is_held(self):
        """msa_logout() takes the prefix lock itself, so no caller may take it
        on its behalf. flock() is per open file description, so a second
        acquisition from the same process is refused just like one from
        another process: wrapping the call (as the GUI's sign-out button once
        did) makes sign-out fail every time, with the misleading "already in
        progress" message."""
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(prefix, "DATA", Path(td)), \
                mock.patch.object(auth, "wine_reg_remove_refresh_token") as rm:
            with prefix.prefix_operation_lock("test operation"):
                with self.assertRaisesRegex(BolError, "another BedrockOnLinux"):
                    auth.msa_logout()

        rm.assert_not_called()

    def test_gui_sign_out_does_not_nest_the_prefix_lock(self):
        """Guards the call site, which no GUI test can reach: a branch cut
        before the lock moved into msa_logout() reintroduced a
        `with prefix_operation_lock(...)` wrapper around the sign-out button's
        call. That nesting is always refused (see the test above), so sign-out
        failed for every user while the whole suite stayed green."""
        source = (Path(__file__).resolve().parent.parent
                  / "bol" / "gui.py").read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.With):
                continue
            if not any(isinstance(item.context_expr, ast.Call)
                       and getattr(item.context_expr.func, "id", None)
                       == "prefix_operation_lock"
                       for item in node.items):
                continue
            for inner in ast.walk(node):
                self.assertNotEqual(
                    getattr(getattr(inner, "func", None), "id", None),
                    "msa_logout",
                    "msa_logout() takes the prefix lock itself; nesting it "
                    "inside prefix_operation_lock() always fails")

    def test_setup_holds_the_same_operation_lock(self):
        events = []

        @contextmanager
        def locked(_operation):
            events.append("lock-enter")
            try:
                yield
            finally:
                events.append("lock-exit")

        @contextmanager
        def shared_locked(_operation, exclusive):
            events.append(f"shared-{'exclusive' if exclusive else 'shared'}-enter")
            try:
                yield
            finally:
                events.append("shared-exit")

        with mock.patch.object(gamesetup, "shared_assets_lock",
                               shared_locked), \
                mock.patch.object(gamesetup, "prefix_operation_lock", locked), \
                mock.patch.object(
                    gamesetup, "_do_setup",
                    side_effect=lambda *args: events.append("setup") or "done"):
            self.assertEqual(gamesetup.do_setup(), "done")

        self.assertEqual(
            events,
            [
                "shared-exclusive-enter",
                "lock-enter",
                "setup",
                "lock-exit",
                "shared-exit",
            ],
        )

    def test_launch_holds_shared_assets_lock_exclusively(self):
        events = []

        @contextmanager
        def shared_locked(operation, exclusive):
            events.append((operation, exclusive))
            yield

        @contextmanager
        def prefix_locked(operation):
            events.append((operation, "prefix"))
            yield

        with mock.patch.object(prefix, "shared_assets_lock", shared_locked), \
                mock.patch.object(
                    prefix, "prefix_operation_lock", prefix_locked
                ):
            with prefix.launch_lock():
                events.append(("launch", "body"))

        self.assertEqual(
            events,
            [
                ("start Minecraft", True),
                ("start Minecraft", "prefix"),
                ("launch", "body"),
            ],
        )

    def test_inherited_launch_descriptors_keep_locks_after_parent_closes(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(prefix, "DATA", Path(td) / "profile"), \
                mock.patch.object(prefix, "GAMES", Path(td) / "shared/games"):
            child = None
            with prefix.launch_lock() as lock_fds:
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import sys,time;"
                            "sys.stdout.write('ready\\n');sys.stdout.flush();"
                            "time.sleep(10)"
                        ),
                    ],
                    stdout=subprocess.PIPE,
                    text=True,
                    pass_fds=lock_fds,
                )
                self.assertEqual(child.stdout.readline().strip(), "ready")
            try:
                with self.assertRaisesRegex(
                        BolError, "shared Minecraft files are in use"):
                    with prefix.launch_lock():
                        pass
            finally:
                child.terminate()
                child.wait(timeout=5)
                child.stdout.close()

            with prefix.launch_lock():
                pass

    def test_profiles_share_asset_lock_and_parallel_play_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "base"
            (base / "games").mkdir(parents=True)
            one = Path(td) / "one"
            two = Path(td) / "two"
            one.mkdir()
            two.mkdir()
            (one / "games").symlink_to(base / "games")
            (two / "games").symlink_to(base / "games")

            with mock.patch.object(prefix, "GAMES", one / "games"):
                with prefix.shared_assets_lock("play one", exclusive=True):
                    with mock.patch.object(prefix, "GAMES", two / "games"):
                        with self.assertRaisesRegex(
                                BolError, "shared Minecraft files are in use"):
                            with prefix.shared_assets_lock(
                                    "play two", exclusive=True):
                                pass
                        with self.assertRaisesRegex(
                                BolError, "shared Minecraft files are in use"):
                            with prefix.shared_assets_lock(
                                    "run setup", exclusive=True):
                                pass


_OPTIONS_REL = ("drive_c/users/steamuser/AppData/Roaming/Minecraft Bedrock/"
                "Users/%s/games/com.mojang/minecraftpe/options.txt")

# A settings file shaped like Minecraft's own: CRLF terminators, and the keys
# players report losing (#175) sitting in the tail where a cut-off write drops
# them — graphics mode, tutorial flags, keyboard mappings.
_OPTIONS_HEAD = b"gfx_viewdistance:96\r\naudio_main:1\r\n"
_OPTIONS_TAIL = (b"graphics_mode:2\r\nhas_dismissed_new_player_flow:1\r\n"
                 b"graphics_mode_switch:1\r\nkeyboard_type_0_key.jump:32\r\n")
_OPTIONS_FULL = _OPTIONS_HEAD + _OPTIONS_TAIL


def _write_options(root, body, user="Shared"):
    """One options.txt inside a prefix tree, as the game would leave it."""
    path = Path(root) / (_OPTIONS_REL % user)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


class GameOptionsTruncationTests(unittest.TestCase):
    """The settings file must survive a save the game never finished (#175)."""

    def test_a_write_cut_off_mid_line_is_not_mistaken_for_a_finished_save(self):
        self.assertTrue(prefix._options_intact(_OPTIONS_FULL))
        # Cut where a buffered write would stop: mid-line, no terminator.
        self.assertFalse(prefix._options_intact(_OPTIONS_HEAD + b"graphics_mo"))
        self.assertFalse(prefix._options_intact(b""))
        self.assertFalse(prefix._options_intact(None))
        # Terminated but holding nothing parseable is not a copy worth keeping.
        self.assertFalse(prefix._options_intact(b"\r\n"))

    def test_a_truncated_settings_file_is_restored_after_the_game_exits(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write_options(td, _OPTIONS_FULL)
            self.assertEqual(prefix.snapshot_game_options(td), 1)
            # The game crashes part-way through rewriting it.
            path.write_bytes(_OPTIONS_HEAD + b"graphics_mo")
            restored = prefix.restore_truncated_game_options(
                td, prefix_idle=True)
            self.assertEqual(restored, [path])
            self.assertEqual(path.read_bytes(), _OPTIONS_FULL)

    def test_a_settings_file_the_game_finished_writing_is_left_alone(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write_options(td, _OPTIONS_FULL)
            prefix.snapshot_game_options(td)
            changed = _OPTIONS_HEAD + b"graphics_mode:0\r\n"
            path.write_bytes(changed)
            self.assertEqual(
                prefix.restore_truncated_game_options(td, prefix_idle=True), [])
            self.assertEqual(path.read_bytes(), changed)

    def test_a_torn_file_never_overwrites_the_copy_that_could_repair_it(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write_options(td, _OPTIONS_FULL)
            prefix.snapshot_game_options(td)
            path.write_bytes(_OPTIONS_HEAD + b"graphics_mo")
            # A second launch must not snapshot the wreckage over the copy.
            prefix.snapshot_game_options(td)
            prefix.restore_truncated_game_options(td, prefix_idle=True)
            self.assertEqual(path.read_bytes(), _OPTIONS_FULL)

    def test_nothing_is_rewritten_while_the_game_may_still_be_saving(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write_options(td, _OPTIONS_FULL)
            prefix.snapshot_game_options(td)
            torn = _OPTIONS_HEAD + b"graphics_mo"
            path.write_bytes(torn)
            self.assertEqual(
                prefix.restore_truncated_game_options(td, prefix_idle=False),
                [])
            self.assertEqual(path.read_bytes(), torn)

    def test_every_account_settings_file_is_guarded_not_only_the_newest(self):
        with tempfile.TemporaryDirectory() as td:
            shared = _write_options(td, _OPTIONS_FULL)
            signed_in = _write_options(td, _OPTIONS_FULL, user="2533274")
            self.assertEqual(prefix.snapshot_game_options(td), 2)
            for path in (shared, signed_in):
                path.write_bytes(_OPTIONS_HEAD + b"graphics_mo")
            prefix.restore_truncated_game_options(td, prefix_idle=True)
            self.assertEqual(shared.read_bytes(), _OPTIONS_FULL)
            self.assertEqual(signed_in.read_bytes(), _OPTIONS_FULL)


class MultiplayerWarningPatchTests(unittest.TestCase):
    """Patching one setting must not rewrite the rest of the file (#175)."""

    @contextmanager
    def _prefix_with(self, body):
        with tempfile.TemporaryDirectory() as td:
            path = _write_options(td, body)
            with mock.patch.object(prefix, "PFX", Path(td)), \
                    mock.patch.object(prefix, "ok"):
                yield path

    def test_only_the_patched_line_changes_and_crlf_survives(self):
        body = (_OPTIONS_HEAD + prefix._MULTIPLAYER_WARNING_KEY + b":0\r\n"
                + _OPTIONS_TAIL)
        with self._prefix_with(body) as path:
            prefix.patch_options(prefix_idle=True)
            self.assertEqual(
                path.read_bytes(),
                body.replace(b"safety_warning:0", b"safety_warning:1"))

    def test_a_line_the_launcher_cannot_parse_is_carried_over_untouched(self):
        body = _OPTIONS_HEAD + b"a comment with no separator\r\n" + _OPTIONS_TAIL
        with self._prefix_with(body) as path:
            prefix.patch_options(prefix_idle=True)
            written = path.read_bytes()
            self.assertIn(b"a comment with no separator\r\n", written)
            self.assertTrue(written.startswith(body))
            self.assertTrue(written.endswith(
                b"do_not_show_multiplayer_online_safety_warning:1\r\n"))

    def test_a_truncated_file_is_never_completed_by_the_patch(self):
        torn = _OPTIONS_HEAD + b"graphics_mo"
        with self._prefix_with(torn) as path:
            prefix.patch_options(prefix_idle=True)
            self.assertEqual(path.read_bytes(), torn)

    def test_the_patch_waits_for_the_game_to_be_gone(self):
        body = _OPTIONS_HEAD + _OPTIONS_TAIL
        with self._prefix_with(body) as path:
            prefix.patch_options(prefix_idle=False)
            self.assertEqual(path.read_bytes(), body)


_SERVERS_REL = ("drive_c/users/steamuser/AppData/Roaming/Minecraft Bedrock/"
                "Users/%s/games/com.mojang/minecraftpe/external_servers.txt")

# A server list shaped like the game's own: "<id>:<name>:<host>:<port>:<added>"
# lines, in the order the player last left them rather than by id.
_SERVERS_LIST = (b"2:zeqa:zeqa.net:19132:1771420844\n"
                 b"1:my server:192.0.2.10:19133:1771420828\n")

_DEFAULT = (("Linesia SkyFaction", "play.linesia.net", 19132),)


def _write_servers(root, body, user="Shared"):
    """One external_servers.txt inside a prefix tree, as the game leaves it."""
    path = Path(root) / (_SERVERS_REL % user)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _server_names(path):
    return [line.split(":")[1]
            for line in path.read_text().splitlines() if line]


class DefaultServersTests(unittest.TestCase):
    """A shipped server may only ever be added to a list the player owns."""

    @contextmanager
    def _quiet(self):
        with mock.patch.object(prefix, "ok"):
            yield

    def test_the_default_is_appended_with_the_next_free_id(self):
        with tempfile.TemporaryDirectory() as td, self._quiet():
            path = _write_servers(td, _SERVERS_LIST)
            added = prefix.seed_default_servers(
                td, prefix_idle=True, servers=_DEFAULT)
            self.assertEqual(added, ["Linesia SkyFaction"])
            written = path.read_text()
            self.assertTrue(written.startswith(_SERVERS_LIST.decode()))
            entry = written.splitlines()[-1].split(":")
            self.assertEqual(entry[:4],
                             ["3", "Linesia SkyFaction", "play.linesia.net",
                              "19132"])
            self.assertTrue(entry[4].isdigit())

    def test_a_default_the_player_deleted_is_never_added_again(self):
        with tempfile.TemporaryDirectory() as td, self._quiet():
            path = _write_servers(td, _SERVERS_LIST)
            prefix.seed_default_servers(td, prefix_idle=True, servers=_DEFAULT)
            # The player removes the tile in-game: the game rewrites the file.
            path.write_bytes(_SERVERS_LIST)
            self.assertEqual(
                prefix.seed_default_servers(
                    td, prefix_idle=True, servers=_DEFAULT),
                [])
            self.assertEqual(path.read_bytes(), _SERVERS_LIST)

    def test_a_server_the_player_already_added_keeps_their_own_name(self):
        with tempfile.TemporaryDirectory() as td, self._quiet():
            mine = b"1:linesia:play.linesia.net:19132:1771420828\n"
            path = _write_servers(td, mine)
            self.assertEqual(
                prefix.seed_default_servers(
                    td, prefix_idle=True, servers=_DEFAULT),
                [])
            self.assertEqual(path.read_bytes(), mine)
            # And having seen it counts as offered: it must not come back if
            # the player later deletes their own entry.
            path.write_bytes(b"")
            prefix.seed_default_servers(td, prefix_idle=True, servers=_DEFAULT)
            self.assertEqual(path.read_bytes(), b"")

    def test_every_account_gets_the_default_not_only_the_newest(self):
        with tempfile.TemporaryDirectory() as td, self._quiet():
            shared = _write_servers(td, _SERVERS_LIST)
            signed_in = _write_servers(td, b"", user="2533274")
            prefix.seed_default_servers(td, prefix_idle=True, servers=_DEFAULT)
            for path in (shared, signed_in):
                self.assertIn("Linesia SkyFaction", _server_names(path))

    def test_nothing_is_written_while_the_game_may_still_be_running(self):
        with tempfile.TemporaryDirectory() as td, self._quiet():
            path = _write_servers(td, _SERVERS_LIST)
            self.assertEqual(
                prefix.seed_default_servers(
                    td, prefix_idle=False, servers=_DEFAULT),
                [])
            self.assertEqual(path.read_bytes(), _SERVERS_LIST)

    def test_a_list_left_without_a_terminator_does_not_swallow_the_entry(self):
        with tempfile.TemporaryDirectory() as td, self._quiet():
            path = _write_servers(td, b"1:zeqa:zeqa.net:19132:1771420844")
            prefix.seed_default_servers(td, prefix_idle=True, servers=_DEFAULT)
            self.assertEqual(_server_names(path),
                             ["zeqa", "Linesia SkyFaction"])

    def test_before_the_first_launch_the_signed_out_list_is_created(self):
        with tempfile.TemporaryDirectory() as td, self._quiet():
            prefix.seed_default_servers(td, prefix_idle=True, servers=_DEFAULT)
            path = Path(td) / (_SERVERS_REL % "Shared")
            self.assertEqual(_server_names(path), ["Linesia SkyFaction"])

    def test_the_shipped_default_is_the_one_the_servers_tab_shows(self):
        self.assertIn(("Linesia SkyFaction", "play.linesia.net", 19132),
                      prefix.DEFAULT_SERVERS)


if __name__ == "__main__":
    unittest.main()
