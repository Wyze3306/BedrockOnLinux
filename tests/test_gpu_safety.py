"""Tests for the no-GPU-open launch safety gate."""
# SPDX-License-Identifier: MIT

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bol import gpu_safety


def result(stdout="", stderr="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


class GraphicsSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.marker = root / "gpu-launch.json"
        self.ack = root / "gpu-ack.json"
        self.patchers = [
            mock.patch.object(gpu_safety, "GPU_LAUNCH_MARKER", self.marker),
            mock.patch.object(gpu_safety, "GPU_SAFETY_ACK", self.ack),
            mock.patch.object(gpu_safety, "_boot_id", return_value="boot-now"),
            # The library fallback would otherwise ask the test host's own X
            # server; tests that need it pass provider_probe.
            mock.patch.object(gpu_safety, "_randr_provider_count",
                              return_value=None),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tempdir.cleanup()

    @staticmethod
    def clean_journal(*_args, **_kwargs):
        return result()

    def test_x11_zero_provider_is_blocked(self):
        def xrandr(*_args, **_kwargs):
            return result("Providers: number : 0\n")

        with mock.patch.object(gpu_safety, "_nvidia_device_with_mesa_glx",
                               return_value=False):
            problem = gpu_safety.graphics_safety_problem(
                {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"},
                xrandr_runner=xrandr,
                journal_runner=self.clean_journal,
            )
        self.assertIn("zero RandR GPU providers", problem)

    def test_x11_hardware_provider_is_allowed(self):
        def xrandr(*_args, **_kwargs):
            return result("Providers: number : 1\n")

        with mock.patch.object(
                gpu_safety, "_nvidia_device_with_mesa_glx",
                side_effect=AssertionError(
                    "a healthy provider must not inspect global PRIME state")):
            self.assertIsNone(gpu_safety.graphics_safety_problem(
                {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"},
                xrandr_runner=xrandr,
                journal_runner=self.clean_journal,
            ))

    def test_healthy_prime_hybrid_is_not_blocked_by_global_mesa_glx(self):
        def xrandr(*_args, **_kwargs):
            return result("Providers: number : 2\n")

        # An Intel/AMD provider can legitimately drive X while an NVIDIA dGPU
        # is loaded for PRIME offload. The global alternatives link alone does
        # not identify which GPU backs this X screen.
        with mock.patch.object(
                gpu_safety, "_nvidia_device_with_mesa_glx",
                side_effect=AssertionError("must not inspect split GLX")):
            self.assertIsNone(gpu_safety.graphics_safety_problem(
                {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"},
                xrandr_runner=xrandr,
                journal_runner=self.clean_journal,
            ))

    def test_zero_provider_can_include_nvidia_split_state_as_a_hint(self):
        def xrandr(*_args, **_kwargs):
            return result("Providers: number : 0\n")

        with mock.patch.object(gpu_safety, "_nvidia_device_with_mesa_glx",
                               return_value=True):
            problem = gpu_safety.graphics_safety_problem(
                {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"},
                xrandr_runner=xrandr,
                journal_runner=self.clean_journal,
            )
        self.assertIn("zero RandR GPU providers", problem)
        self.assertIn("mesa-diverted", problem)

    def test_x11_missing_xrandr_is_unknown_and_blocked(self):
        with mock.patch.object(gpu_safety.shutil, "which", return_value=None), \
                mock.patch.object(gpu_safety, "_kernel_driver_fault_scope",
                                  return_value=None), \
                mock.patch.object(gpu_safety, "_xorg_software_fallback",
                                  return_value=False):
            problem = gpu_safety.graphics_safety_problem(
                {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"})
        self.assertIn("could not verify any X11 hardware provider", problem)

    def test_missing_xrandr_counts_providers_through_libxrandr(self):
        # The Flatpak's GNOME runtime ships no xrandr, so every X11 session
        # was refused before the library was asked (#259).
        with mock.patch.object(gpu_safety.shutil, "which", return_value=None), \
                mock.patch.object(
                    gpu_safety, "_nvidia_device_with_mesa_glx",
                    side_effect=AssertionError(
                        "a healthy provider must not inspect PRIME state")):
            self.assertIsNone(gpu_safety.graphics_safety_problem(
                {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"},
                journal_runner=self.clean_journal,
                provider_probe=lambda _env: 1,
            ))

    def test_sandboxed_game_mode_without_xrandr_is_allowed(self):
        with mock.patch.object(gpu_safety.shutil, "which", return_value=None):
            self.assertIsNone(gpu_safety.graphics_safety_problem(
                {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"},
                journal_runner=self.clean_journal,
                atom_probe=lambda _env: True,
                provider_probe=lambda _env: 0,
            ))

    def test_library_zero_provider_count_is_still_blocked(self):
        with mock.patch.object(gpu_safety.shutil, "which", return_value=None), \
                mock.patch.object(gpu_safety, "_nvidia_device_with_mesa_glx",
                                  return_value=False):
            problem = gpu_safety.graphics_safety_problem(
                {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"},
                journal_runner=self.clean_journal,
                atom_probe=lambda _env: False,
                provider_probe=lambda _env: 0,
            )
        self.assertIn("zero RandR GPU providers", problem)

    def test_a_parsed_xrandr_answer_is_not_asked_again(self):
        def xrandr(*_args, **_kwargs):
            return result("Providers: number : 1\n")

        def must_not_run(_env):
            raise AssertionError("xrandr already counted the providers")

        self.assertIsNone(gpu_safety.graphics_safety_problem(
            {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"},
            xrandr_runner=xrandr,
            journal_runner=self.clean_journal,
            provider_probe=must_not_run,
        ))

    def test_x11_xrandr_timeout_is_unknown_and_blocked(self):
        def timeout(*_args, **_kwargs):
            raise subprocess.TimeoutExpired("xrandr", 4)

        with mock.patch.object(gpu_safety, "_xorg_software_fallback",
                               return_value=False):
            problem = gpu_safety.graphics_safety_problem(
                {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"},
                xrandr_runner=timeout,
                journal_runner=self.clean_journal,
            )
        self.assertIn("could not verify any X11 hardware provider", problem)

    def test_x11_unparseable_xrandr_is_unknown_and_blocked(self):
        def malformed(*_args, **_kwargs):
            return result("provider output changed\n")

        with mock.patch.object(gpu_safety, "_xorg_software_fallback",
                               return_value=False):
            problem = gpu_safety.graphics_safety_problem(
                {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"},
                xrandr_runner=malformed,
                journal_runner=self.clean_journal,
            )
        self.assertIn("could not verify any X11 hardware provider", problem)

    def test_fbdev_fallback_uses_nvidia_split_state_only_as_a_hint(self):
        def failed(*_args, **_kwargs):
            return result(returncode=1)

        with mock.patch.object(gpu_safety, "_xorg_software_fallback",
                               return_value=True), \
                mock.patch.object(gpu_safety, "_nvidia_device_with_mesa_glx",
                                  return_value=True):
            problem = gpu_safety.graphics_safety_problem(
                {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"},
                xrandr_runner=failed,
                journal_runner=self.clean_journal,
            )
        self.assertIn("FBDEV/software rendering", problem)
        self.assertIn("mesa-diverted", problem)

    def test_xwayland_provider_count_is_not_interpreted_as_xorg_health(self):
        def must_not_run(*_args, **_kwargs):
            raise AssertionError("xrandr provider probe must be skipped")

        self.assertIsNone(gpu_safety.graphics_safety_problem(
            {"DISPLAY": ":1", "WAYLAND_DISPLAY": "wayland-0",
             "XDG_SESSION_TYPE": "wayland"},
            xrandr_runner=must_not_run,
            journal_runner=self.clean_journal,
        ))

    def test_nested_gamescope_zero_provider_is_allowed(self):
        def xrandr(*_args, **_kwargs):
            return result("Providers: number : 0\n")

        environments = (
            {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11",
             "GAMESCOPE_WAYLAND_DISPLAY": "gamescope-0"},
            {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11",
             "XDG_CURRENT_DESKTOP": "gamescope"},
            {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11",
             "DESKTOP_SESSION": "gamescope-session"},
        )
        for env in environments:
            with self.subTest(env=env), mock.patch.object(
                    gpu_safety, "_nvidia_device_with_mesa_glx",
                    side_effect=AssertionError(
                        "nested Gamescope must not inspect direct-Xorg GLX")):
                self.assertIsNone(gpu_safety.graphics_safety_problem(
                    env,
                    xrandr_runner=xrandr,
                    journal_runner=self.clean_journal,
                ))

    def test_generic_steam_flags_do_not_bypass_zero_provider_block(self):
        def xrandr(*_args, **_kwargs):
            return result("Providers: number : 0\n")

        env = {
            "DISPLAY": ":0",
            "XDG_SESSION_TYPE": "x11",
            "SteamDeck": "1",
            "SteamGamepadUI": "1",
        }
        with mock.patch.object(gpu_safety, "_nvidia_device_with_mesa_glx",
                               return_value=False):
            problem = gpu_safety.graphics_safety_problem(
                env,
                xrandr_runner=xrandr,
                journal_runner=self.clean_journal,
                atom_probe=lambda _env: False,
            )
        self.assertIn("zero RandR GPU providers", problem)

    def test_sandboxed_gamescope_is_recognised_without_its_variables(self):
        # A Flatpak sandbox does not forward GAMESCOPE_WAYLAND_DISPLAY, so in
        # Steam Deck Game Mode the packaged launcher saw a plain X11 session
        # with zero RandR providers and refused to start (#127).
        def xrandr(*_args, **_kwargs):
            return result("Providers: number : 0\n")

        with mock.patch.object(
                gpu_safety, "_nvidia_device_with_mesa_glx",
                side_effect=AssertionError(
                    "nested Gamescope must not inspect direct-Xorg GLX")):
            self.assertIsNone(gpu_safety.graphics_safety_problem(
                {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"},
                xrandr_runner=xrandr,
                journal_runner=self.clean_journal,
                atom_probe=lambda _env: True,
            ))

    def test_root_atoms_are_not_probed_when_the_environment_already_says_so(self):
        def xrandr(*_args, **_kwargs):
            return result("Providers: number : 0\n")

        def must_not_run(_env):
            raise AssertionError("the X server must not be opened needlessly")

        self.assertIsNone(gpu_safety.graphics_safety_problem(
            {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11",
             "GAMESCOPE_WAYLAND_DISPLAY": "gamescope-0"},
            xrandr_runner=xrandr,
            journal_runner=self.clean_journal,
            atom_probe=must_not_run,
        ))

    def test_root_atom_probe_needs_a_display(self):
        self.assertFalse(gpu_safety._gamescope_root_atoms({}))
        self.assertFalse(gpu_safety._gamescope_root_atoms({"DISPLAY": "  "}))

    def test_gamescope_only_exempts_a_parsed_zero_provider_result(self):
        def failed(*_args, **_kwargs):
            return result(returncode=1)

        with mock.patch.object(gpu_safety, "_xorg_software_fallback",
                               return_value=False):
            problem = gpu_safety.graphics_safety_problem(
                {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11",
                 "GAMESCOPE_WAYLAND_DISPLAY": "gamescope-0"},
                xrandr_runner=failed,
                journal_runner=self.clean_journal,
            )
        self.assertIn("could not verify any X11 hardware provider", problem)

    def test_gamescope_does_not_hide_current_kernel_fault(self):
        def journal(*_args, **_kwargs):
            return result("amdgpu 0000:03:00.0: GPU reset begin!\n")

        problem = gpu_safety.graphics_safety_problem(
            {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11",
             "GAMESCOPE_WAYLAND_DISPLAY": "gamescope-0"},
            journal_runner=journal,
        )
        self.assertIn("during this boot", problem)

    def test_gamescope_does_not_hide_interrupted_launch_marker(self):
        self.marker.write_text(json.dumps({
            "version": gpu_safety._STATE_VERSION,
            "engine_rev": "wow64-archs-r12",
            "phase": "running",
            "token": "1" * 32,
            "boot_id": "boot-before-power-loss",
            "launcher_pid": 424242,
            "created": 1,
        }))

        problem = gpu_safety.graphics_safety_problem(
            {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11",
             "GAMESCOPE_WAYLAND_DISPLAY": "gamescope-0"},
            journal_runner=self.clean_journal,
        )
        self.assertIn("did not return cleanly", problem)

    def test_current_kernel_gpu_oops_is_blocked(self):
        def journal(*_args, **_kwargs):
            return result(
                "BUG: kernel NULL pointer dereference\n"
                "RIP: _nv023868rm+0x3c/0xa3 [nvidia]\n")

        problem = gpu_safety.graphics_safety_problem(
            {"XDG_SESSION_TYPE": "wayland"},
            journal_runner=journal,
        )
        self.assertIn("fatal kernel fault", problem)

    def test_previous_boot_gpu_oops_is_blocked_after_hard_reboot(self):
        calls = []

        def journal(args, **_kwargs):
            calls.append(args)
            boot = args[args.index("-b") + 1]
            if boot == "0":
                return result()
            return result(
                "BUG: kernel NULL pointer dereference\n"
                "RIP: _nv023868rm+0x3c/0xa3 [nvidia]\n")

        problem = gpu_safety.graphics_safety_problem(
            {"XDG_SESSION_TYPE": "wayland"}, journal_runner=journal)
        self.assertIn("before the last reboot", problem)
        self.assertEqual([call[call.index("-b") + 1] for call in calls],
                         ["0", "-1"])
        for call in calls:
            self.assertNotIn("--since", call)
            self.assertIn("-n", call)

    def test_gpu_fault_never_ages_out_during_current_boot(self):
        calls = []

        def journal(args, **_kwargs):
            calls.append(args)
            return result("amdgpu 0000:03:00.0: GPU reset begin!\n")

        problem = gpu_safety.graphics_safety_problem(
            {"XDG_SESSION_TYPE": "wayland"}, journal_runner=journal)
        self.assertIn("during this boot", problem)
        self.assertNotIn("--since", calls[0])
        self.assertEqual(calls[0][calls[0].index("-n") + 1], "5000")

    def test_unrelated_kernel_oops_is_not_misattributed_to_gpu(self):
        text = "amdgpu: initialized normally\n" + ("quiet line\n" * 100)
        text += ("BUG: kernel NULL pointer dereference\n"
                 "RIP: e1000e_network_path\n")
        self.assertFalse(gpu_safety._gpu_fault_in_text(text))

    def test_acknowledgement_hides_only_previous_not_current_fault(self):
        self.ack.write_text(json.dumps({
            "version": gpu_safety._STATE_VERSION,
            "boot_id": "boot-now", "acknowledged": 1,
            "previous_boot_fault": True,
        }))

        def current_fault(args, **_kwargs):
            return result("amdgpu: GPU reset begin!\n")

        problem = gpu_safety.graphics_safety_problem(
            {"XDG_SESSION_TYPE": "wayland"}, journal_runner=current_fault)
        self.assertIn("during this boot", problem)

    def test_acknowledged_previous_boot_fault_is_not_rechecked(self):
        self.ack.write_text(json.dumps({
            "version": gpu_safety._STATE_VERSION,
            "boot_id": "boot-now", "acknowledged": 1,
            "previous_boot_fault": True,
        }))
        calls = []

        def journal(args, **_kwargs):
            calls.append(args)
            return result()

        self.assertIsNone(gpu_safety.graphics_safety_problem(
            {"XDG_SESSION_TYPE": "wayland"}, journal_runner=journal))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][calls[0].index("-b") + 1], "0")

    def test_marker_only_ack_does_not_hide_previous_boot_driver_fault(self):
        self.ack.write_text(json.dumps({
            "version": gpu_safety._STATE_VERSION,
            "boot_id": "boot-now",
            "acknowledged": 1,
            "marker": True,
            "previous_boot_fault": False,
        }))

        def journal(args, **_kwargs):
            boot = args[args.index("-b") + 1]
            if boot == "0":
                return result()
            return result("amdgpu: GPU reset begin!\n")

        problem = gpu_safety.graphics_safety_problem(
            {"XDG_SESSION_TYPE": "wayland"}, journal_runner=journal)
        self.assertIn("before the last reboot", problem)

    def test_hard_reboot_marker_blocks_next_launch(self):
        self.marker.write_text(json.dumps({
            "version": gpu_safety._STATE_VERSION,
            "engine_rev": "wow64-archs-r12",
            "token": "old-token",
            "boot_id": "boot-before-power-loss",
            "launcher_pid": 424242,
            "created": 1,
        }))
        problem = gpu_safety.graphics_safety_problem(
            {"XDG_SESSION_TYPE": "wayland"},
            journal_runner=self.clean_journal,
        )
        self.assertIn("did not return cleanly", problem)
        self.assertIn("--acknowledge-gpu-crash", problem)

    def test_same_boot_marker_does_not_advertise_an_impossible_action(self):
        """A current-boot marker is refused, so the block must say 'reboot'."""
        self.marker.write_text(json.dumps({
            "version": gpu_safety._STATE_VERSION,
            "engine_rev": "wow64-archs-native12",
            "phase": "running",
            "token": "1" * 32,
            "boot_id": "boot-now",
            "launcher_pid": 424242,
            "created": 1,
        }))

        problem = gpu_safety.graphics_safety_problem(
            {"XDG_SESSION_TYPE": "wayland"},
            journal_runner=self.clean_journal,
        )
        status = gpu_safety.gpu_safety_acknowledgement_status(
            journal_runner=self.clean_journal)

        self.assertFalse(status.can_acknowledge)
        self.assertIn("during this boot", problem)
        self.assertIn("cannot be acknowledged", problem)
        self.assertIn("only clears it after that reboot", problem)

    def test_torn_hard_reboot_marker_is_still_blocking(self):
        self.marker.write_text("{torn")
        problem = gpu_safety.graphics_safety_problem(
            {"XDG_SESSION_TYPE": "wayland"},
            journal_runner=self.clean_journal,
        )
        self.assertIn("marker exists but is unreadable", problem)

    def test_marker_is_private_exclusive_and_token_owned(self):
        token = gpu_safety.arm_gpu_launch()
        self.assertTrue(self.marker.is_file())
        self.assertEqual(json.loads(self.marker.read_text())["phase"],
                         "running")
        self.assertEqual(stat.S_IMODE(self.marker.stat().st_mode), 0o600)
        with self.assertRaisesRegex(gpu_safety.BolError,
                                    "previous Minecraft GPU launch"):
            gpu_safety.arm_gpu_launch()
        self.assertFalse(gpu_safety.disarm_gpu_launch("wrong-token"))
        self.assertTrue(self.marker.exists())
        self.assertTrue(gpu_safety.disarm_gpu_launch(token))
        self.assertFalse(self.marker.exists())

    def test_returned_wrapper_marker_is_retired_when_prefix_is_idle(self):
        self.marker.write_text(json.dumps({
            "version": gpu_safety._STATE_VERSION,
            "engine_rev": "wow64-archs-r12",
            "phase": "wrapper_returned",
            "token": "1" * 32,
            "boot_id": "boot-now",
            "launcher_pid": 424242,
            "created": 1,
            "wrapper_returned": 2,
        }))
        with mock.patch.object(gpu_safety, "warn") as warning:
            self.assertTrue(gpu_safety.retire_idle_current_boot_marker())
        self.assertFalse(self.marker.exists())
        warning.assert_called_once()

    def test_idle_recovery_keeps_running_and_old_boot_markers(self):
        state = {
            "version": gpu_safety._STATE_VERSION,
            "engine_rev": "wow64-archs-r12",
            "phase": "running",
            "token": "1" * 32,
            "boot_id": "boot-now",
            "launcher_pid": 424242,
            "created": 1,
        }
        self.marker.write_text(json.dumps(state))
        self.assertFalse(gpu_safety.retire_idle_current_boot_marker())
        self.assertTrue(self.marker.exists())
        state["phase"] = "wrapper_returned"
        state["wrapper_returned"] = 2
        state["boot_id"] = "boot-before-power-loss"
        self.marker.write_text(json.dumps(state))
        self.assertFalse(gpu_safety.retire_idle_current_boot_marker())
        self.assertTrue(self.marker.exists())

    def test_idle_recovery_keeps_malformed_current_boot_marker(self):
        self.marker.write_text(json.dumps({
            "version": gpu_safety._STATE_VERSION,
            "engine_rev": "wow64-archs-r12",
            "phase": "wrapper_returned",
            "token": "not-an-owned-token",
            "boot_id": "boot-now",
            "launcher_pid": 424242,
            "created": 1,
            "wrapper_returned": 2,
        }))
        self.assertFalse(gpu_safety.retire_idle_current_boot_marker())
        self.assertTrue(self.marker.exists())

    def test_wrapper_return_phase_is_durable_and_token_owned(self):
        with mock.patch.object(gpu_safety.time, "time", return_value=20):
            token = gpu_safety.arm_gpu_launch()
        with mock.patch.object(gpu_safety.time, "time", return_value=30):
            self.assertTrue(gpu_safety.mark_gpu_wrapper_returned(token))
        state = json.loads(self.marker.read_text())
        self.assertEqual(state["phase"], "wrapper_returned")
        self.assertEqual(state["wrapper_returned"], 30)
        self.assertFalse(gpu_safety.mark_gpu_wrapper_returned("0" * 32))

    def test_explicit_acknowledgement_clears_old_marker_privately(self):
        self.marker.write_text(json.dumps({
            "version": gpu_safety._STATE_VERSION,
            "engine_rev": "wow64-archs-r12",
            "token": "old-token",
            "boot_id": "boot-before-power-loss",
            "launcher_pid": 424242,
            "created": 1,
        }))
        status = gpu_safety.acknowledge_gpu_safety_incident(
            journal_runner=self.clean_journal)
        self.assertTrue(status.can_acknowledge)
        self.assertTrue(status.marker_present)
        self.assertFalse(status.previous_boot_fault)
        self.assertFalse(self.marker.exists())
        self.assertEqual(stat.S_IMODE(self.ack.stat().st_mode), 0o600)
        acknowledgement = json.loads(self.ack.read_text())
        self.assertEqual(acknowledgement["boot_id"], "boot-now")
        self.assertTrue(acknowledgement["marker"])
        self.assertFalse(acknowledgement["previous_boot_fault"])

    def test_acknowledgement_never_bypasses_current_x11_failure(self):
        with self.assertRaisesRegex(
                gpu_safety.BolError, "No previous-boot GPU safety incident"):
            gpu_safety.acknowledge_gpu_safety_incident(
                journal_runner=self.clean_journal)
        self.assertFalse(self.ack.exists())

        def xrandr(*_args, **_kwargs):
            return result("Providers: number : 0\n")

        with mock.patch.object(gpu_safety, "_nvidia_device_with_mesa_glx",
                               return_value=False):
            problem = gpu_safety.graphics_safety_problem(
                {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"},
                xrandr_runner=xrandr,
                journal_runner=self.clean_journal,
            )
        self.assertIn("zero RandR GPU providers", problem)

    def test_active_marker_cannot_be_acknowledged(self):
        self.marker.write_text(json.dumps({
            "version": gpu_safety._STATE_VERSION,
            "engine_rev": "wow64-archs-r12",
            "token": "active-token",
            "boot_id": "boot-now",
            "launcher_pid": os.getpid(),
            "created": 1,
        }))
        with self.assertRaisesRegex(gpu_safety.BolError, "still active"):
            gpu_safety.acknowledge_gpu_safety_incident()
        self.assertTrue(self.marker.exists())
        self.assertFalse(self.ack.exists())

    def test_dead_current_boot_marker_requires_reboot_before_acknowledgement(self):
        self.marker.write_text(json.dumps({
            "version": gpu_safety._STATE_VERSION,
            "engine_rev": "wow64-archs-r12",
            "phase": "running",
            "token": "1" * 32,
            "boot_id": "boot-now",
            "launcher_pid": 424242,
            "created": 1,
        }))
        status = gpu_safety.gpu_safety_acknowledgement_status(
            journal_runner=self.clean_journal)
        self.assertEqual(status.code, "current-boot-launch")
        self.assertFalse(status.can_acknowledge)
        with self.assertRaisesRegex(gpu_safety.BolError,
                                    "during this boot"):
            gpu_safety.acknowledge_gpu_safety_incident(
                journal_runner=self.clean_journal)
        self.assertTrue(self.marker.exists())
        self.assertFalse(self.ack.exists())

    def test_unreadable_marker_cannot_be_acknowledged(self):
        self.marker.write_text("{torn")
        status = gpu_safety.gpu_safety_acknowledgement_status(
            journal_runner=self.clean_journal)
        self.assertEqual(status.code, "unreadable-marker")
        self.assertFalse(status.can_acknowledge)
        with self.assertRaisesRegex(gpu_safety.BolError,
                                    "cannot prove"):
            gpu_safety.acknowledge_gpu_safety_incident(
                journal_runner=self.clean_journal)
        self.assertTrue(self.marker.exists())
        self.assertFalse(self.ack.exists())

    def test_current_kernel_fault_prevents_old_marker_acknowledgement(self):
        self.marker.write_text(json.dumps({
            "version": gpu_safety._STATE_VERSION,
            "engine_rev": "wow64-archs-r12",
            "phase": "running",
            "token": "1" * 32,
            "boot_id": "boot-before-power-loss",
            "launcher_pid": 424242,
            "created": 1,
        }))

        def current_fault(*_args, **_kwargs):
            return result("amdgpu: GPU reset begin!\n")

        status = gpu_safety.gpu_safety_acknowledgement_status(
            journal_runner=current_fault)
        self.assertEqual(status.code, "current-boot-fault")
        self.assertFalse(status.can_acknowledge)
        with self.assertRaisesRegex(gpu_safety.BolError,
                                    "fatal fault during this boot"):
            gpu_safety.acknowledge_gpu_safety_incident(
                journal_runner=current_fault)
        self.assertTrue(self.marker.exists())
        self.assertFalse(self.ack.exists())

    def test_previous_boot_kernel_fault_is_acknowledgeable_without_marker(self):
        def previous_fault(args, **_kwargs):
            boot = args[args.index("-b") + 1]
            return (result() if boot == "0"
                    else result("NVRM: GPU has fallen off the bus\n"))

        status = gpu_safety.gpu_safety_acknowledgement_status(
            journal_runner=previous_fault)
        self.assertTrue(status.can_acknowledge)
        self.assertFalse(status.marker_present)
        self.assertTrue(status.previous_boot_fault)
        acknowledged = gpu_safety.acknowledge_gpu_safety_incident(
            journal_runner=previous_fault)
        self.assertEqual(acknowledged.code, "previous-boot-incident")
        payload = json.loads(self.ack.read_text())
        self.assertFalse(payload["marker"])
        self.assertTrue(payload["previous_boot_fault"])

    def test_already_acknowledged_fault_cannot_rewrite_ack_state(self):
        def previous_fault(args, **_kwargs):
            boot = args[args.index("-b") + 1]
            return (result() if boot == "0"
                    else result("NVRM: GPU has fallen off the bus\n"))

        gpu_safety.acknowledge_gpu_safety_incident(
            journal_runner=previous_fault)
        with mock.patch.object(gpu_safety, "_write_state_atomic") as write:
            with self.assertRaisesRegex(
                    gpu_safety.BolError,
                    "No previous-boot GPU safety incident"):
                gpu_safety.acknowledge_gpu_safety_incident(
                    journal_runner=previous_fault)
        write.assert_not_called()

    def test_no_incident_never_creates_acknowledgement(self):
        status = gpu_safety.gpu_safety_acknowledgement_status(
            journal_runner=self.clean_journal)
        self.assertEqual(status.code, "none")
        self.assertFalse(status.can_acknowledge)
        with self.assertRaisesRegex(
                gpu_safety.BolError, "No previous-boot GPU safety incident"):
            gpu_safety.acknowledge_gpu_safety_incident(
                journal_runner=self.clean_journal)
        self.assertFalse(self.ack.exists())

    def test_old_boot_legacy_marker_requires_explicit_acknowledgement(self):
        self.marker.write_text(json.dumps({
            "version": gpu_safety._LEGACY_MARKER_VERSION,
            "token": "1" * 32,
            "boot_id": "boot-with-r11",
            "launcher_pid": 424242,
            "created": 1,
        }))
        problem = gpu_safety.graphics_safety_problem(
            {"XDG_SESSION_TYPE": "wayland"},
            journal_runner=self.clean_journal,
        )
        self.assertIn("old marker cannot distinguish a Wine crash", problem)
        self.assertIn("doctor --acknowledge-gpu-crash", problem)
        self.assertTrue(self.marker.exists())

    def test_current_boot_active_legacy_marker_is_not_silently_retired(self):
        self.marker.write_text(json.dumps({
            "version": gpu_safety._LEGACY_MARKER_VERSION,
            "token": "1" * 32,
            "boot_id": "boot-now",
            "launcher_pid": os.getpid(),
            "created": 1,
        }))
        problem = gpu_safety.graphics_safety_problem(
            {"XDG_SESSION_TYPE": "wayland"},
            journal_runner=self.clean_journal,
        )
        self.assertIn("legacy Minecraft GPU session is still marked active",
                      problem)
        self.assertTrue(self.marker.exists())

    def test_override_is_explicit_and_non_persistent(self):
        env = {"BOL_ALLOW_UNSAFE_GPU": "1"}
        with mock.patch.object(gpu_safety, "graphics_safety_problem",
                               return_value="injected unsafe state"), \
                mock.patch.object(gpu_safety, "warn") as warning:
            gpu_safety.require_safe_graphics_session(env)
        warning.assert_called_once()

    def test_unsafe_state_raises_before_launch(self):
        with mock.patch.object(gpu_safety, "graphics_safety_problem",
                               return_value="injected unsafe state"):
            with self.assertRaisesRegex(gpu_safety.BolError,
                                        "did not start Wine"):
                gpu_safety.require_safe_graphics_session({})


class RandrLibraryProbeTests(unittest.TestCase):
    def test_the_library_probe_needs_a_display(self):
        self.assertIsNone(gpu_safety._randr_provider_count({}))
        self.assertIsNone(gpu_safety._randr_provider_count({"DISPLAY": " "}))

    @unittest.skipUnless(
        os.environ.get("DISPLAY") and gpu_safety.shutil.which("xrandr"),
        "needs an X server and the xrandr program")
    def test_the_library_counts_what_xrandr_counts(self):
        cli = gpu_safety._xrandr_provider_count(os.environ)
        if cli is None:
            self.skipTest("xrandr could not list this server's providers")
        self.assertEqual(gpu_safety._randr_provider_count(os.environ), cli)


class AcknowledgementGuidanceTests(unittest.TestCase):
    """The blocking message must name a command this installation can run."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.marker = self.root / "gpu-launch.json"
        self.marker.write_text(json.dumps({
            "version": gpu_safety._STATE_VERSION,
            "engine_rev": "wow64-archs-native12",
            "token": "1" * 32,
            "boot_id": "boot-before-power-loss",
            "launcher_pid": 424242,
            "created": 1,
        }))

    def problem(self, packaged_command):
        calls = []

        def fake_launcher_command(*arguments):
            calls.append(arguments)
            return packaged_command + " " + " ".join(arguments)

        with mock.patch.object(gpu_safety, "GPU_LAUNCH_MARKER", self.marker), \
                mock.patch.object(gpu_safety, "_boot_id",
                                  return_value="boot-now"), \
                mock.patch.object(gpu_safety, "launcher_command",
                                  fake_launcher_command):
            return gpu_safety.interrupted_launch_problem(), calls

    def test_guidance_uses_the_packaging_aware_invocation(self):
        problem, calls = self.problem(
            "/home/p/BedrockOnLinux-2.1.1-x86_64.AppImage")
        self.assertIn(
            "/home/p/BedrockOnLinux-2.1.1-x86_64.AppImage doctor "
            "--acknowledge-gpu-crash",
            problem,
        )
        self.assertNotIn("'bedrock-on-linux doctor", problem)
        self.assertEqual(
            calls, [("doctor", "--acknowledge-gpu-crash")])

    def test_flatpak_guidance_is_runnable(self):
        problem, _calls = self.problem(
            "flatpak run io.github.wyze3306.BedrockOnLinux")
        self.assertIn(
            "flatpak run io.github.wyze3306.BedrockOnLinux doctor "
            "--acknowledge-gpu-crash",
            problem,
        )


if __name__ == "__main__":
    unittest.main()
