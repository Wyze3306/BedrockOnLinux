"""Read the ray tracing verdict back out of a launch, and never invent one.

Issue #153 reported that Minecraft "does not detect" the ray tracing hardware
of an RDNA4 card. Nothing in the launcher could confirm or deny it: the tier
is decided inside the game process by vkd3d-proton, and the decision was never
written down. Every launch now runs the payload at its info level, so the
answer exists -- these tests pin what may be concluded from it, and, just as
importantly, what may not: a log from a launch that never created a device is
not evidence that the driver has no ray tracing.
"""
# SPDX-License-Identifier: MIT

import os
import tempfile
import unittest
from pathlib import Path

from bol import raytracing


# One device creation as the shipped payload logs it, trimmed to the lines
# this module reads. Wine's debug formatting supplies the timestamp/channel
# prefix, so keep it: the parser has to survive it.
_PREFIX = "39982.514:0794:03f4:info:vkd3d-proton:"

INSTANCE = (
    _PREFIX + "vkd3d_config_flags_init_once: "
    "VKD3D_CONFIG='force_raw_va_cbv'.\n"
    + _PREFIX + "vkd3d_instance_init: vkd3d-proton - build: "
    "3b10bd7a7ec6a73+.\n"
)

DEVICE = (
    _PREFIX + "d3d12_device_caps_init_shader_model: Enabling support for SM "
    "6.7.\n"
)

DGC_NV = (
    _PREFIX + "vkd3d_bindless_state_get_bindless_flags: Enabling fast paths "
    "for advanced ExecuteIndirect() graphics (NV_dgc).\n"
)

DXR_1_0 = _PREFIX + "d3d12_device_determine_ray_tracing_tier: DXR support " \
                    "enabled.\n"
DXR_1_1 = DXR_1_0 + _PREFIX + "d3d12_device_determine_ray_tracing_tier: DXR " \
                              "1.1 support enabled.\n"
ULTIMATE = _PREFIX + "d3d12_device_caps_init_feature_level: DX Ultimate " \
                     "supported!\n"


class RayTracingReportTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.logs = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _write(self, body, name="minecraft.log"):
        (self.logs / name).write_text(body, encoding="utf-8")

    def test_no_log_at_all_is_not_a_verdict(self):
        caps = raytracing.graphics_capabilities(self.logs)
        self.assertFalse(caps["observed"])
        self.assertIn("unknown", raytracing.ray_tracing_summary(self.logs))
        self.assertIsNone(raytracing.ray_tracing_problem(self.logs))

    def test_a_log_from_another_program_is_not_a_verdict(self):
        self._write("wine: could not load kernel32.dll\n")
        self.assertFalse(
            raytracing.graphics_capabilities(self.logs)["observed"])

    def test_a_launch_that_never_created_a_device_is_not_a_verdict(self):
        """The payload announces itself before any device exists."""
        self._write(INSTANCE)
        self.assertFalse(
            raytracing.graphics_capabilities(self.logs)["observed"])
        self.assertIsNone(raytracing.ray_tracing_problem(self.logs))

    def test_tier_1_1_is_read_with_its_ultimate_and_indirect_path(self):
        self._write(INSTANCE + DEVICE + DGC_NV + DXR_1_1 + ULTIMATE)
        caps = raytracing.graphics_capabilities(self.logs)
        self.assertTrue(caps["observed"])
        self.assertEqual(caps["tier"], "1.1")
        self.assertTrue(caps["ultimate"])
        self.assertEqual(caps["indirect"], "NV_dgc")
        self.assertEqual(caps["config"], "force_raw_va_cbv")
        summary = raytracing.ray_tracing_summary(self.logs)
        self.assertTrue(summary.startswith("OK"), summary)
        self.assertIn("DXR 1.1", summary)
        self.assertIsNone(raytracing.ray_tracing_problem(self.logs))

    def test_tier_1_0_is_not_enough_for_the_ray_traced_mode(self):
        """"DXR support enabled." alone leaves Minecraft's mode unavailable."""
        self._write(INSTANCE + DEVICE + DXR_1_0)
        caps = raytracing.graphics_capabilities(self.logs)
        self.assertEqual(caps["tier"], "1.0")
        self.assertIn("PARTIEL", raytracing.ray_tracing_summary(self.logs))
        self.assertIn("tier 1.1", raytracing.ray_tracing_problem(self.logs))

    def test_a_device_without_ray_tracing_names_both_vendors(self):
        self._write(INSTANCE + DEVICE)
        caps = raytracing.graphics_capabilities(self.logs)
        self.assertTrue(caps["observed"])
        self.assertIsNone(caps["tier"])
        self.assertIn("MISSING", raytracing.ray_tracing_summary(self.logs))
        problem = raytracing.ray_tracing_problem(self.logs)
        self.assertIn("RX 6000", problem)
        self.assertIn("RTX 20", problem)

    def test_the_settings_switch_is_reported_as_a_choice_not_a_fault(self):
        self._write(INSTANCE.replace("force_raw_va_cbv",
                                     "force_raw_va_cbv,nodxr") + DEVICE)
        summary = raytracing.ray_tracing_summary(self.logs)
        self.assertIn("nodxr", summary)
        self.assertNotIn("MISSING", summary)
        self.assertIn("Ray tracing switch",
                      raytracing.ray_tracing_problem(self.logs))

    def test_a_payload_override_is_reported_rather_than_blamed_on_the_driver(
            self):
        """vkd3d-proton drops DXR on a Deck *after* logging the tier."""
        self._write(INSTANCE + DEVICE + DXR_1_1 + _PREFIX
                    + "d3d12_device_caps_override_application: Disabling "
                      "automatic enablement of DXR on Deck.\n")
        caps = raytracing.graphics_capabilities(self.logs)
        self.assertTrue(caps["deck"])
        # The tier was logged and then taken away; the game never saw it.
        self.assertIsNone(caps["tier"])
        summary = raytracing.ray_tracing_summary(self.logs)
        self.assertNotIn("DXR 1.1", summary)
        self.assertIn("Steam Deck", raytracing.ray_tracing_problem(self.logs))

    def test_a_tier_capped_by_the_payload_is_reported_as_the_cap(self):
        """Same shape: the log states 1.1, the game is handed 1.0."""
        self._write(INSTANCE + DEVICE + DXR_1_1 + _PREFIX
                    + "d3d12_device_caps_override_application: Limiting "
                      "reported DXR tier to 1.0.\n")
        caps = raytracing.graphics_capabilities(self.logs)
        self.assertTrue(caps["limited"])
        self.assertEqual(caps["tier"], "1.0")
        self.assertIn("tier 1.1", raytracing.ray_tracing_problem(self.logs))

    def test_the_newest_launch_log_answers(self):
        """Diagnostics writes both files; the stale one must not win."""
        self._write(INSTANCE + DEVICE + DXR_1_1, name="proton.log")
        self._write(INSTANCE + DEVICE, name="minecraft.log")
        stale = self.logs / "proton.log"
        os.utime(stale, (1, 1))
        self.assertIsNone(
            raytracing.graphics_capabilities(self.logs)["tier"])
        os.utime(stale, None)
        os.utime(self.logs / "minecraft.log", (1, 1))
        self.assertEqual(
            raytracing.graphics_capabilities(self.logs)["tier"], "1.1")


class LaunchAsksForTheVerdictTest(unittest.TestCase):
    """Without info-level graphics logging there is nothing to read back."""

    def test_every_launch_records_the_graphics_capabilities(self):
        from bol import launch

        for diagnostics in (False, True):
            env = {}
            launch._configure_runtime_compat(
                env, {}, "x11", host_wayland=False, diagnostics=diagnostics,
                host_env={}, steam_deck=False)
            self.assertEqual(env["VKD3D_DEBUG"], "info")


if __name__ == "__main__":
    unittest.main()
