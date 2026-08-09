"""Keep Wine's fast synchronization path built in, and report it when absent.

The engine is built in Debian 11, whose linux-libc-dev predates ntsync, so
Wine's configure silently compiled the in-process synchronization backend out
and every Win32 wait became a wineserver round-trip. That serialised
Minecraft's worker threads and produced the "the game runs on one thread"
performance reports (issues #63, #139, #143, #148, #150).
"""
# SPDX-License-Identifier: MIT

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import launch, ntsync
from bol.util import LAUNCHER_OWNED_ENV, custom_env_map


ROOT = Path(__file__).resolve().parents[1]
HEADER = ROOT / "third_party/linux-uapi/ntsync.h"
BULLSEYE_SCRIPT = ROOT / "scripts/build-winegdk-bullseye.sh"
CONTAINER_SCRIPT = ROOT / "scripts/build-winegdk-container.sh"

# include/uapi/linux/ntsync.h, byte-identical from Linux v6.14 to mainline.
HEADER_SHA256 = \
    "006437ee52a3e04f921df77081eb5c21c44c71f598b10ac534c6ef9e78296262"

# Everything server/inproc_sync.c and dlls/ntdll/unix/sync.c reference.
REQUIRED_HEADER_SYMBOLS = (
    "struct ntsync_sem_args",
    "struct ntsync_mutex_args",
    "struct ntsync_event_args",
    "struct ntsync_wait_args",
    "NTSYNC_WAIT_REALTIME",
    "NTSYNC_IOC_CREATE_SEM",
    "NTSYNC_IOC_CREATE_MUTEX",
    "NTSYNC_IOC_CREATE_EVENT",
    "NTSYNC_IOC_WAIT_ANY",
    "NTSYNC_IOC_WAIT_ALL",
    "NTSYNC_IOC_SEM_RELEASE",
    "NTSYNC_IOC_SEM_READ",
    "NTSYNC_IOC_MUTEX_UNLOCK",
    "NTSYNC_IOC_MUTEX_KILL",
    "NTSYNC_IOC_MUTEX_READ",
    "NTSYNC_IOC_EVENT_SET",
    "NTSYNC_IOC_EVENT_RESET",
    "NTSYNC_IOC_EVENT_PULSE",
    # inproc_sync.c gates the entire backend on this one being defined.
    "NTSYNC_IOC_EVENT_READ",
)


def _engine(root, *, with_ntsync, paths=("files/bin-wow64/wineserver",
                                         "files/bin/wineserver")):
    """A fake engine tree whose wineserver does or does not carry the marker."""
    for relative in paths:
        server = Path(root) / relative
        server.parent.mkdir(parents=True, exist_ok=True)
        body = b"\x7fELF" + b"padding" * 64
        if with_ntsync:
            body += b"\x00/dev/ntsync\x00"
        server.write_bytes(body + b"trailer")
    return Path(root)


class VendoredHeaderTests(unittest.TestCase):
    def test_header_matches_the_pinned_upstream_hash(self):
        digest = hashlib.sha256(HEADER.read_bytes()).hexdigest()
        self.assertEqual(digest, HEADER_SHA256)

    def test_header_defines_every_symbol_wine_uses(self):
        text = HEADER.read_text(encoding="utf-8")
        for symbol in REQUIRED_HEADER_SYMBOLS:
            self.assertIn(symbol, text)

    def test_both_build_scripts_install_and_verify_the_header(self):
        for script in (BULLSEYE_SCRIPT, CONTAINER_SCRIPT):
            text = script.read_text(encoding="utf-8")
            with self.subTest(script=script.name):
                self.assertIn("third_party/linux-uapi/ntsync.h", text)
                self.assertIn(HEADER_SHA256, text)
                self.assertIn("/usr/include/linux/ntsync.h", text)

    def test_both_build_scripts_gate_on_the_macro_wine_actually_uses(self):
        # Kernels 6.10-6.13 shipped a preview UAPI with two ioctls. It defines
        # HAVE_LINUX_NTSYNC_H, so configure looks satisfied, yet Wine gates the
        # backend on NTSYNC_IOC_EVENT_READ and still compiles it out. Debian 13
        # (linux-libc-dev 6.12) is exactly that case, so the scripts must
        # recognise an incomplete header instead of trusting its presence.
        for script in (BULLSEYE_SCRIPT, CONTAINER_SCRIPT):
            text = script.read_text(encoding="utf-8")
            with self.subTest(script=script.name):
                self.assertIn("NTSYNC_IOC_EVENT_READ", text)

    def test_both_build_scripts_fail_closed_when_ntsync_is_compiled_out(self):
        # The regression was silent; the build must now refuse an artifact
        # whose wineserver lacks the ntsync code path.
        for script in (BULLSEYE_SCRIPT, CONTAINER_SCRIPT):
            text = script.read_text(encoding="utf-8")
            with self.subTest(script=script.name):
                self.assertIn("/dev/ntsync", text)
                self.assertIn("in-process sync compiled out", text)


class EngineCapabilityTests(unittest.TestCase):
    def test_engine_with_the_backend_is_detected(self):
        with tempfile.TemporaryDirectory() as td:
            root = _engine(td, with_ntsync=True)
            self.assertIs(ntsync.engine_supports_inproc_sync(root), True)

    def test_engine_without_the_backend_is_detected(self):
        with tempfile.TemporaryDirectory() as td:
            root = _engine(td, with_ntsync=False)
            self.assertIs(ntsync.engine_supports_inproc_sync(root), False)

    def test_marker_split_across_a_read_boundary_is_still_found(self):
        with tempfile.TemporaryDirectory() as td:
            server = Path(td) / "files/bin/wineserver"
            server.parent.mkdir(parents=True)
            server.write_bytes(b"a" * 15 + b"/dev/ntsync" + b"b" * 15)
            self.assertIs(
                ntsync._file_contains(server, b"/dev/ntsync", chunk_size=16),
                True)

    def test_missing_engine_is_unknown_not_broken(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(ntsync.engine_supports_inproc_sync(Path(td)))
        self.assertIsNone(ntsync.engine_supports_inproc_sync(None))

    def test_a_custom_engine_shipping_only_one_server_is_read(self):
        with tempfile.TemporaryDirectory() as td:
            root = _engine(td, with_ntsync=True,
                           paths=("files/bin/wineserver",))
            self.assertIs(ntsync.engine_supports_inproc_sync(root), True)


class KernelCapabilityTests(unittest.TestCase):
    def test_present_device_is_detected(self):
        with tempfile.TemporaryDirectory() as td:
            device = Path(td) / "ntsync"
            device.write_bytes(b"")
            self.assertTrue(ntsync.kernel_exposes_ntsync(device))

    def test_absent_device_is_detected(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(
                ntsync.kernel_exposes_ntsync(Path(td) / "ntsync"))


class ProblemReportTests(unittest.TestCase):
    def _absent_device(self, td):
        return Path(td) / "no-ntsync"

    def test_engine_without_backend_is_reported_as_the_engine(self):
        with tempfile.TemporaryDirectory() as td:
            root = _engine(td, with_ntsync=False)
            problem = ntsync.inproc_sync_problem(
                root, device=self._absent_device(td), environ={})
            self.assertIn("built without", problem)
            self.assertIn("Install / Update", problem)

    def test_engine_with_backend_but_no_device_blames_the_kernel(self):
        with tempfile.TemporaryDirectory() as td:
            root = _engine(td, with_ntsync=True)
            problem = ntsync.inproc_sync_problem(
                root, device=self._absent_device(td), environ={},
                kernel_release="6.12.0-test")
            self.assertIn("/dev/ntsync", problem)
            self.assertIn("modprobe ntsync", problem)
            self.assertIn("6.12.0-test", problem)

    def test_working_setup_reports_no_problem(self):
        with tempfile.TemporaryDirectory() as td:
            root = _engine(td, with_ntsync=True)
            device = Path(td) / "ntsync"
            device.write_bytes(b"")
            self.assertIsNone(ntsync.inproc_sync_problem(
                root, device=device, environ={}))

    def test_unknown_engine_reports_no_problem(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(ntsync.inproc_sync_problem(
                Path(td), device=self._absent_device(td), environ={}))

    def test_explicit_opt_out_is_named_as_such(self):
        with tempfile.TemporaryDirectory() as td:
            root = _engine(td, with_ntsync=True)
            device = Path(td) / "ntsync"
            device.write_bytes(b"")
            problem = ntsync.inproc_sync_problem(
                root, device=device, environ={"PROTON_NO_NTSYNC": "1"})
            self.assertIn("PROTON_NO_NTSYNC", problem)

    def test_falsy_opt_out_values_do_not_count_as_disabled(self):
        for value in ("", "0", "off", "no", "false"):
            with self.subTest(value=value):
                self.assertFalse(ntsync.inproc_sync_disabled_by_env(
                    {"PROTON_NO_NTSYNC": value}))
        self.assertTrue(
            ntsync.inproc_sync_disabled_by_env({"PROTON_NO_NTSYNC": "1"}))

    def test_summary_words_cover_each_state(self):
        with tempfile.TemporaryDirectory() as td:
            absent = self._absent_device(td)
            broken = _engine(Path(td) / "broken", with_ntsync=False)
            good = _engine(Path(td) / "good", with_ntsync=True)
            device = Path(td) / "ntsync"
            device.write_bytes(b"")
            self.assertIn("engine built without", ntsync.inproc_sync_summary(
                broken, device=absent, environ={}))
            self.assertIn("no /dev/ntsync", ntsync.inproc_sync_summary(
                good, device=absent, environ={}))
            self.assertIn("OK", ntsync.inproc_sync_summary(
                good, device=device, environ={}))
            self.assertIn("unknown", ntsync.inproc_sync_summary(
                Path(td) / "nothing", device=device, environ={}))


class LaunchWiringTests(unittest.TestCase):
    def test_inherited_opt_out_is_dropped_so_it_cannot_kneecap_the_game(self):
        # A stale global export must not silently serialise every worker
        # thread behind the wineserver.
        self.assertIn("PROTON_NO_NTSYNC", LAUNCHER_OWNED_ENV)
        env = {"PROTON_NO_NTSYNC": "1"}
        launch._configure_runtime_compat(
            env, {}, "x11", False, host_env={}, steam_deck=False)
        self.assertNotIn("PROTON_NO_NTSYNC", env)

    def test_warning_reads_the_custom_env_field_not_the_shell(self):
        # Only the Advanced field survives to the game, so only it may
        # trigger the "you turned it off" wording.
        settings = {"custom_env": "PROTON_NO_NTSYNC=1"}
        with tempfile.TemporaryDirectory() as td:
            root = _engine(td, with_ntsync=True)
            with mock.patch.object(launch, "proton_path", return_value=root), \
                    mock.patch.object(launch, "warn") as warned, \
                    mock.patch.dict(os.environ, {}, clear=True):
                launch._warn_if_inproc_sync_unavailable(settings)
        self.assertTrue(warned.called)
        self.assertIn("PROTON_NO_NTSYNC", warned.call_args[0][0])

    def test_shell_opt_out_alone_does_not_warn(self):
        with tempfile.TemporaryDirectory() as td:
            root = _engine(td, with_ntsync=True)
            device_present = mock.patch.object(
                ntsync, "kernel_exposes_ntsync", return_value=True)
            with mock.patch.object(launch, "proton_path", return_value=root), \
                    mock.patch.object(launch, "warn") as warned, \
                    device_present, \
                    mock.patch.dict(os.environ, {"PROTON_NO_NTSYNC": "1"},
                                    clear=True):
                launch._warn_if_inproc_sync_unavailable({})
        self.assertFalse(warned.called)

    def test_engine_without_backend_warns_at_launch(self):
        with tempfile.TemporaryDirectory() as td:
            root = _engine(td, with_ntsync=False)
            with mock.patch.object(launch, "proton_path", return_value=root), \
                    mock.patch.object(launch, "warn") as warned, \
                    mock.patch.dict(os.environ, {}, clear=True):
                launch._warn_if_inproc_sync_unavailable({})
        self.assertTrue(warned.called)

    def test_the_check_can_be_silenced(self):
        with tempfile.TemporaryDirectory() as td:
            root = _engine(td, with_ntsync=False)
            with mock.patch.object(launch, "proton_path", return_value=root), \
                    mock.patch.object(launch, "warn") as warned, \
                    mock.patch.dict(os.environ,
                                    {"BOL_SKIP_NTSYNC_CHECK": "1"},
                                    clear=True):
                launch._warn_if_inproc_sync_unavailable({})
        self.assertFalse(warned.called)

    def test_custom_env_map_reads_values_quietly(self):
        self.assertEqual(custom_env_map("A=1 PROTON_NO_NTSYNC=1"),
                         {"A": "1", "PROTON_NO_NTSYNC": "1"})
        self.assertEqual(custom_env_map(""), {})
        # An unterminated quote must not raise or warn here.
        self.assertEqual(custom_env_map('A="1'), {})


if __name__ == "__main__":
    unittest.main()
