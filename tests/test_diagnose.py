"""Regression tests for post-mortem freeze diagnostics."""
# SPDX-License-Identifier: MIT

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import gamesetup


class FreezeDiagnosisTests(unittest.TestCase):
    def test_unsupported_dgc_signature_reports_driver_guidance(self):
        with tempfile.TemporaryDirectory() as td:
            logs = Path(td)
            (logs / "proton.log").write_text(
                "d3d12_command_signature_create: Device generated commands "
                "is not supported by implementation.\n",
                encoding="utf-8",
            )
            with mock.patch.object(gamesetup, "LOGS", logs), \
                    mock.patch.object(gamesetup, "msa_signed_in",
                                      return_value=True), \
                    mock.patch.object(gamesetup, "load_settings",
                                      return_value={}), \
                    mock.patch.object(gamesetup, "warn"), \
                    mock.patch.object(gamesetup, "info"):
                hits = gamesetup.diagnose()
        self.assertEqual(len(hits), 1)
        self.assertIn("Device Generated Commands", hits[0])
        self.assertIn("xe", hits[0])
        self.assertIn("setup --force", hits[0])


class GameCrashDiagnosisTests(unittest.TestCase):
    def _diagnose(self, log):
        with tempfile.TemporaryDirectory() as td:
            logs = Path(td)
            (logs / "proton.log").write_text(log, encoding="utf-8")
            with mock.patch.object(gamesetup, "LOGS", logs), \
                    mock.patch.object(gamesetup, "msa_signed_in",
                                      return_value=True), \
                    mock.patch.object(gamesetup, "load_settings",
                                      return_value={}), \
                    mock.patch.object(gamesetup, "warn"), \
                    mock.patch.object(gamesetup, "info"):
                return gamesetup.diagnose()

    def test_unhandled_page_fault_is_reported_instead_of_no_known_cause(self):
        hits = self._diagnose(
            "info:  Game: Minecraft.Windows.exe\n"
            "wine: Unhandled page fault on read access to 0000000000000008 "
            "at address 0000000140427AB5 (thread 0118), starting debugger...\n"
        )
        self.assertTrue(
            any("memory access violation" in hit for hit in hits), hits)
        self.assertFalse(
            any("prefix broken" in hit.lower() for hit in hits), hits)

    def test_access_violation_exception_is_recognised(self):
        hits = self._diagnose(
            "wine: Unhandled exception 0xc0000005 in thread 118\n")
        self.assertTrue(
            any("memory access violation" in hit for hit in hits), hits)

    _PAGE_FAULT = ("wine: Unhandled page fault on read access to "
                   "0000000000000008 at address 0000000140427AB5 (thread "
                   "0118), starting debugger...\n")

    def _diagnose_on(self, log, drm):
        with mock.patch.object(gamesetup, "DRM_SYSFS", drm):
            return self._diagnose(log)

    def test_a_crash_on_a_virtual_gpu_suggests_the_legacy_renderer(self):
        # A Chromebook's Venus driver: exit 84 until the legacy renderer (#302).
        with tempfile.TemporaryDirectory() as td:
            hits = self._diagnose_on(
                "info:vkd3d-proton: Virtio-GPU Venus (Intel(R) UHD Graphics "
                "(JSL))\n" + self._PAGE_FAULT, Path(td))
        self.assertTrue(any("Legacy compatibility renderer" in hit
                            and "virtual GPU" in hit for hit in hits), hits)

    def test_sysfs_names_a_virtual_gpu_the_log_does_not(self):
        with tempfile.TemporaryDirectory() as td:
            card = Path(td) / "card0" / "device"
            card.mkdir(parents=True)
            (card / "vendor").write_text("0x1af4\n")
            hits = self._diagnose_on(self._PAGE_FAULT, Path(td))
        self.assertTrue(any("virtual GPU" in hit for hit in hits), hits)

    def test_a_crash_on_a_real_gpu_does_not_blame_virtualisation(self):
        with tempfile.TemporaryDirectory() as td:
            card = Path(td) / "card0" / "device"
            card.mkdir(parents=True)
            (card / "vendor").write_text("0x10de\n")
            hits = self._diagnose_on(self._PAGE_FAULT, Path(td))
        self.assertFalse(any("virtual GPU" in hit for hit in hits), hits)

    def test_a_virtual_gpu_that_did_not_crash_is_not_mentioned(self):
        with tempfile.TemporaryDirectory() as td:
            hits = self._diagnose_on("Virtio-GPU Venus\n", Path(td))
        self.assertFalse(any("virtual GPU" in hit for hit in hits), hits)

    def test_ordinary_log_traffic_is_not_a_crash(self):
        hits = self._diagnose(
            "fixme:seh:page fault handling discussed in a fixme\n"
            "info:  DXVK: v3.0.1\n"
        )
        self.assertFalse(
            any("memory access violation" in hit for hit in hits), hits)

    def test_a_refused_licence_is_the_exact_cause_the_launch_reports(self):
        # launch._launch_once raises on this very string (#280), so the rule
        # has to hand it back unchanged.
        hits = self._diagnose(
            "not entitled to this content: Device group is full, please "
            "remove a device and try again\n")
        self.assertIn(gamesetup.LICENCE_REFUSED, hits)

    def test_a_licence_fetched_without_a_network_is_named(self):
        # What xodus-cli printed with the network down (#308): a Store build
        # cannot be decrypted without the key Microsoft hands out per launch.
        hits = self._diagnose(
            "appxmanifest.xml: 73994240 4913\n\n"
            "thread 'main' (277526) panicked at "
            "crates/xodus-cli/src/license.rs:51:6:\n"
            "called `Result::unwrap()` on an `Err` value: Request(reqwest::"
            "Error { kind: Request, url: \"https://login.live.com/RST2.srf\", "
            "source: hyper_util::client::legacy::Error(Connect, ConnectError("
            "\"dns error\", Custom { kind: Uncategorized, error: \"failed to "
            "lookup address information: Temporary failure in name "
            "resolution\" })) })\n"
            "note: run with `RUST_BACKTRACE=1` environment variable to "
            "display a backtrace\n")
        self.assertIn(gamesetup.LICENCE_UNREACHABLE, hits)
        self.assertIn("internet connection", gamesetup.LICENCE_UNREACHABLE)
        # login.live.com and "failed" in the same panic are not a sign-in
        # the player can do anything about.
        self.assertFalse(any(hit.startswith("Microsoft sign-in failed")
                             for hit in hits), hits)

    def test_other_xodus_panics_are_not_called_offline(self):
        hits = self._diagnose(
            "thread 'main' panicked at crates/xodus-cli/src/run.rs:133:58:\n"
            "called `Result::unwrap()` on an `Err` value: Os { code: 2, "
            "kind: NotFound, message: \"No such file or directory\" }\n")
        self.assertNotIn(gamesetup.LICENCE_UNREACHABLE, hits)


class OnlineDiagnosisTests(unittest.TestCase):
    def _diagnose(self, log, settings):
        with tempfile.TemporaryDirectory() as td:
            logs = Path(td)
            (logs / "proton.log").write_text(log, encoding="utf-8")
            with mock.patch.object(gamesetup, "LOGS", logs), \
                    mock.patch.object(gamesetup, "msa_signed_in",
                                      return_value=True), \
                    mock.patch.object(gamesetup, "load_settings",
                                      return_value=settings), \
                    mock.patch.object(gamesetup, "warn"), \
                    mock.patch.object(gamesetup, "info"):
                return gamesetup.diagnose()

    def test_native_identity_missing_after_preauth_is_reported(self):
        hits = self._diagnose(
            "preauth: loaded user/XSTS tokens\n",
            {"force_msa_facet": False},
        )
        self.assertTrue(any("native XGame identity" in hit for hit in hits))

    def test_native_identity_and_preauth_produce_no_auth_warning(self):
        hits = self._diagnose(
            "native XGame identity loaded: TitleId 0x35760c07\n"
            "preauth: loaded user/XSTS tokens\n",
            {"force_msa_facet": False},
        )
        self.assertFalse(any("server" in hit.lower() or "xbox" in hit.lower()
                             for hit in hits))

    def test_normal_uninitialize_stub_is_not_a_missing_xuser(self):
        hits = self._diagnose(
            "00e0:trace:xgameruntime:UninitializeApiImpl stub!\n",
            {},
        )
        self.assertFalse(any("no WineGDK XUser" in hit for hit in hits))

    def test_exact_ntquerywnf_abort_reports_missing_ntdll_patch(self):
        hits = self._diagnose(
            "wine: Call from 00000001 to unimplemented function "
            "ntdll.dll.NtQueryWnfStateData, aborting\n",
            {},
        )
        self.assertTrue(any("ntdll patch missing" in hit for hit in hits))

    def test_healthy_patch_messages_do_not_report_missing_patches(self):
        hits = self._diagnose(
            "info ntdll.NtQueryWnfStateData: already returns "
            "STATUS_NOT_IMPLEMENTED\n"
            "info combase.RoOriginateErrorW: already patched\n",
            {},
        )
        self.assertFalse(any("patch missing" in hit for hit in hits))

    def test_issue_97_user32_failure_is_not_reported_as_ntdll(self):
        hits = self._diagnose(
            "err:module:import_dll Loading library user32.dll (which is needed "
            "by L\"C:\\\\windows\\\\system32\\\\plugplay.exe\") failed "
            "(error c0000020).\n"
            "wine: Call from 00000001 to unimplemented function "
            "user32.dll.BroadcastSystemMessageW, aborting\n"
            "wine: Call from 00000002 to unimplemented function "
            "shell32.dll.SHGetFolderPathW, aborting\n",
            {},
        )
        hit = next(hit for hit in hits if "user32.dll" in hit)
        self.assertIn("Install / Update", hit)
        self.assertNotIn("use Repair", hit)
        self.assertFalse(any("ntdll patch missing" in hit for hit in hits))

    def test_exact_combase_abort_still_reports_missing_patch(self):
        hits = self._diagnose(
            "wine: Unimplemented function "
            "combase.dll.RoOriginateErrorW called at address 00000001\n",
            {},
        )
        self.assertTrue(any("combase patch missing" in hit for hit in hits))

    def test_xuser_stub_still_reports_missing_runtime(self):
        hits = self._diagnose(
            "00e0:fixme:xgameruntime:XUserAddAsync stub!\n",
            {},
        )
        self.assertTrue(any("no WineGDK XUser" in hit for hit in hits))

    def test_large_log_keeps_initialization_proof_and_tail(self):
        log = (
            "native XGame identity loaded: TitleId 0x35760c07\n"
            "preauth: loaded user/XSTS credentials\n"
            + "middle\n" * 70000
            + "00e0:trace:xgameruntime:UninitializeApiImpl stub!\n"
        )
        hits = self._diagnose(log, {})
        self.assertFalse(any("no WineGDK XUser" in hit for hit in hits))

    def test_nv_dgc_raw_va_error_reports_driver_guidance(self):
        with tempfile.TemporaryDirectory() as td:
            logs = Path(td)
            (logs / "proton.log").write_text(
                "d3d12_command_signature_init_state_template_dgc_nv: "
                "Root parameter 2 is not a raw VA. Cannot implement command "
                "signature.\n",
                encoding="utf-8",
            )
            with mock.patch.object(gamesetup, "LOGS", logs), \
                    mock.patch.object(gamesetup, "msa_signed_in",
                                      return_value=True), \
                    mock.patch.object(gamesetup, "load_settings",
                                      return_value={}), \
                    mock.patch.object(gamesetup, "warn"), \
                    mock.patch.object(gamesetup, "info"):
                hits = gamesetup.diagnose()
        self.assertEqual(len(hits), 1)
        self.assertIn("Device Generated Commands", hits[0])
        self.assertIn("xe", hits[0])
        self.assertIn("setup --force", hits[0])

    def test_initial_connection_13_reports_host_firewall_and_profile(self):
        hits = self._diagnose(
            "[NetherNet] InitialConnection-13\n",
            {},
        )
        hit = next(hit for hit in hits if "InitialConnection-13" in hit)
        self.assertIn("host firewall", hit)
        self.assertIn("UDP 19132", hit)
        self.assertIn("Private", hit)

    def test_initial_connection_25_reports_stale_host_session(self):
        hits = self._diagnose(
            "RakNet error InitialConnection-25: world is full\n",
            {},
        )
        hit = next(hit for hit in hits if "InitialConnection-25" in hit)
        self.assertIn("stale", hit)
        self.assertIn("NetherNet/RakNet", hit)
        self.assertIn("Public to Private", hit)
        self.assertIn("toggle Multiplayer Game", hit)
        self.assertIn("requires the host owner", hit)
        self.assertIn("fully restart both games", hit)
        self.assertIn("Bedrock Dedicated Server", hit)

    def test_explicit_version_mismatch_reports_exact_host_version(self):
        hits = self._diagnose(
            "Connection refused: IncompatibleVersion; "
            "client build 47475267, host build 47475290\n",
            {},
        )
        hit = next(hit for hit in hits if "version mismatch" in hit)
        self.assertIn("exactly the host's", hit)

    def test_version_number_without_mismatch_signature_is_not_flagged(self):
        hits = self._diagnose(
            "Minecraft version 1.21.90 initialized successfully\n",
            {},
        )
        self.assertFalse(any("version mismatch" in hit for hit in hits))

    def test_vulkan_13_no_adapters_offers_opengl_renderer(self):
        hits = self._diagnose(
            "Found device: Intel(R) HD Graphics 4600 (HSW GT2)\n"
            "Skipping: Device does not support Vulkan 1.3\n"
            "DXVK: No adapters found. Please check your device filter settings "
            "and Vulkan setup.\n"
            "A Vulkan 1.3 capable setup is required.\n"
            "Failed to initialize DXVK.\n",
            {},
        )
        hit = next(hit for hit in hits if "renderer=opengl" in hit)
        self.assertIn("Legacy compatibility renderer", hit)
        self.assertIn("Vulkan 1.3", hit)

    def test_llvmpipe_is_not_misdiagnosed_as_old_gpu_fallback(self):
        hits = self._diagnose(
            "Found device: llvmpipe (LLVM 19.1.7, 256 bits)\n"
            "Skipping: Device does not support Vulkan 1.3\n"
            "DXVK: No adapters found.\n"
            "Failed to initialize DXVK.\n",
            {},
        )
        self.assertTrue(any("software rendering (llvmpipe)" in hit
                            for hit in hits))
        self.assertFalse(any("renderer=opengl" in hit for hit in hits))

    def test_no_adapters_without_vulkan_13_signature_is_not_flagged(self):
        hits = self._diagnose(
            "DXVK: No adapters found because device filter excluded all GPUs\n",
            {},
        )
        self.assertFalse(any("renderer=opengl" in hit for hit in hits))

    def test_launcher_owned_custom_env_override_is_surfaced(self):
        # An override that breaks the launch can leave nothing matchable in
        # the log, so this hit comes from the field itself. Issue #134 ended
        # in three full reinstalls because nothing ever named the field.
        hits = self._diagnose(
            "info:  Game: Minecraft.Windows.exe\n",
            {"custom_env": "PROTON_USE_WINED3D=1 MANGOHUD=1"},
        )
        self.assertTrue(any("PROTON_USE_WINED3D" in hit for hit in hits), hits)
        self.assertTrue(
            any("Advanced custom-environment" in hit for hit in hits), hits)
        # Only launcher-owned variables are second-guessed.
        self.assertFalse(any("MANGOHUD" in hit for hit in hits), hits)

    def test_custom_env_without_launcher_variables_is_not_flagged(self):
        hits = self._diagnose(
            "info:  Game: Minecraft.Windows.exe\n",
            {"custom_env": "MANGOHUD=1 DXVK_HUD=fps"},
        )
        self.assertFalse(
            any("custom-environment" in hit for hit in hits), hits)


if __name__ == "__main__":
    unittest.main()
