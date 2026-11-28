"""MangoHud's GPU power field on cards without a power reading (#395).

The NVIDIA driver of some cards answers "not supported" for the power draw;
MangoHud 0.8.4 then shows leftover memory as watts (2278630 W in the
Flatpak). These tests hold when the launcher turns that field off, what it
does to the player's own MangoHud options, and what it leaves alone. No test
here loads the machine's NVML or reads its graphics cards.
"""
# SPDX-License-Identifier: MIT

import subprocess
import sys
import unittest
from unittest import mock

from bol import mangohud
from bol.gpus import Gpu

RTX_4060 = Gpu(slot="0000:2b:00.0", vendor="10de", device="2882",
               driver="nvidia", boot_vga=True, name="RTX 4060")
RADEON_IGPU = Gpu(slot="0000:0e:00.0", vendor="1002", device="164e",
                  driver="amdgpu", boot_vga=False, name="Radeon iGPU")


class Probe:
    """Stands in for the NVML child process; remembers what it was asked."""

    def __init__(self, answer):
        self.answer = answer
        self.asked = []

    def __call__(self, slot):
        self.asked.append(slot)
        return self.answer


class RequestedTests(unittest.TestCase):
    def test_mangohud_one_turns_the_layer_on(self):
        self.assertTrue(mangohud.requested({"MANGOHUD": "1"}))

    def test_other_values_do_not(self):
        # The Vulkan loader compares the value of a layer's enable variable.
        for value in ("0", "", "true", "yes"):
            with self.subTest(value=value):
                self.assertFalse(mangohud.requested({"MANGOHUD": value}))
        self.assertFalse(mangohud.requested({}))

    def test_disable_mangohud_wins(self):
        self.assertFalse(mangohud.requested(
            {"MANGOHUD": "1", "DISABLE_MANGOHUD": "1"}))

    def test_gamescope_mangoapp_counts(self):
        self.assertTrue(mangohud.requested(
            {}, ["gamescope", "-f", "--mangoapp"]))
        self.assertFalse(mangohud.requested({}, ["gamescope", "-f"]))


class ConfigTests(unittest.TestCase):
    def test_unset_keeps_reading_the_config_file(self):
        self.assertEqual(mangohud.without_power(None), "read_cfg,gpu_power=0")

    def test_player_options_come_first_and_stay(self):
        self.assertEqual(mangohud.without_power("fps,gpu_stats"),
                         "fps,gpu_stats,gpu_power=0")
        self.assertEqual(mangohud.without_power("read_cfg,"),
                         "read_cfg,gpu_power=0")

    def test_empty_value_gets_no_read_cfg(self):
        # Set but empty, MangoHud reads no config file; that is kept.
        self.assertEqual(mangohud.without_power(""), "gpu_power=0")

    def test_gpu_power_named_with_any_separator(self):
        for config in ("gpu_power", "fps,gpu_power=1", "fps:gpu_power",
                       "fps;gpu_power=0", " gpu_power =1"):
            with self.subTest(config=config):
                self.assertTrue(mangohud.names_power(config))
        for config in ("", "gpu_power_limit", "fps,gpu_stats",
                       "gpu_load_value=60"):
            with self.subTest(config=config):
                self.assertFalse(mangohud.names_power(config))


class HideUnreadablePowerTests(unittest.TestCase):
    def hide(self, env, answer=False, settings=None, cards=(RTX_4060,),
             gamescope=()):
        probe = Probe(answer)
        changed = mangohud.hide_unreadable_power(
            env, settings or {}, gamescope=gamescope, probe=probe,
            cards=cards)
        return changed, probe

    def test_the_reported_case(self):
        # MANGOHUD=1 in the launcher's custom environment, a config file of
        # the player's, a card whose driver has no power reading.
        env = {"MANGOHUD": "1"}
        with mock.patch.object(mangohud, "info") as info:
            changed, probe = self.hide(env)
        self.assertTrue(changed)
        self.assertEqual(env["MANGOHUD_CONFIG"], "read_cfg,gpu_power=0")
        self.assertEqual(probe.asked, [None])
        info.assert_called_once()
        self.assertIn("N/A", info.call_args[0][0])

    def test_player_mangohud_config_is_extended_not_replaced(self):
        env = {"MANGOHUD": "1", "MANGOHUD_CONFIG": "fps,vram,font_size=24"}
        with mock.patch.object(mangohud, "info"):
            self.assertTrue(self.hide(env)[0])
        self.assertEqual(env["MANGOHUD_CONFIG"],
                         "fps,vram,font_size=24,gpu_power=0")

    def test_explicit_gpu_power_is_the_players_choice(self):
        env = {"MANGOHUD": "1", "MANGOHUD_CONFIG": "read_cfg,gpu_power"}
        changed, probe = self.hide(env)
        self.assertFalse(changed)
        self.assertEqual(env["MANGOHUD_CONFIG"], "read_cfg,gpu_power")
        self.assertEqual(probe.asked, [], "no need to ask the driver")

    def test_a_card_with_a_reading_is_left_alone(self):
        for answer in (True, None):
            with self.subTest(answer=answer):
                env = {"MANGOHUD": "1"}
                self.assertFalse(self.hide(env, answer=answer)[0])
                self.assertNotIn("MANGOHUD_CONFIG", env)

    def test_without_mangohud_nothing_is_asked(self):
        env = {"DXVK_HUD": "fps"}
        changed, probe = self.hide(env)
        self.assertFalse(changed)
        self.assertEqual(probe.asked, [])
        self.assertEqual(env, {"DXVK_HUD": "fps"})

    def test_mangoapp_under_gamescope(self):
        env = {}
        with mock.patch.object(mangohud, "info"):
            changed, _ = self.hide(
                env, gamescope=["gamescope", "-f", "--mangoapp"])
        self.assertTrue(changed)
        self.assertEqual(env["MANGOHUD_CONFIG"], "read_cfg,gpu_power=0")

    def test_gamescope_field_as_a_string(self):
        env = {}
        with mock.patch.object(mangohud, "info"):
            self.assertTrue(self.hide(env, gamescope="-f --mangoapp")[0])

    def test_chosen_nvidia_card_is_asked_by_its_address(self):
        env = {"MANGOHUD": "1"}
        with mock.patch.object(mangohud, "info"):
            _, probe = self.hide(env, settings={"gpu": RTX_4060.slot},
                                 cards=(RADEON_IGPU, RTX_4060))
        self.assertEqual(probe.asked, [RTX_4060.slot])

    def test_chosen_card_of_another_vendor_is_not_nvml(self):
        env = {"MANGOHUD": "1"}
        changed, probe = self.hide(env, settings={"gpu": RADEON_IGPU.slot},
                                   cards=(RADEON_IGPU, RTX_4060))
        self.assertFalse(changed)
        self.assertEqual(probe.asked, [])

    def test_no_nvidia_card_no_question(self):
        env = {"MANGOHUD": "1"}
        changed, probe = self.hide(env, cards=(RADEON_IGPU,))
        self.assertFalse(changed)
        self.assertEqual(probe.asked, [])

    def test_unknown_inventory_leaves_it_to_the_driver(self):
        # No card listed (a sysfs the sandbox hides): NVML has the last word.
        env = {"MANGOHUD": "1"}
        with mock.patch.object(mangohud, "info"):
            changed, probe = self.hide(env, cards=())
        self.assertTrue(changed)
        self.assertEqual(probe.asked, [None])


class PowerReadableTests(unittest.TestCase):
    def run_probe(self, stdout="", returncode=0, error=None):
        calls = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            if error is not None:
                raise error
            return subprocess.CompletedProcess(argv, returncode, stdout, "")

        return mangohud.power_readable("0000:2b:00.0", runner=runner), calls

    def test_not_supported_everywhere_is_false(self):
        self.assertIs(self.run_probe("3\n")[0], False)
        self.assertIs(self.run_probe("3 3\n")[0], False)

    def test_one_reading_is_true(self):
        self.assertIs(self.run_probe("0\n")[0], True)
        self.assertIs(self.run_probe("3 0\n")[0], True)

    def test_anything_else_is_unknown(self):
        for stdout, code in (("", 0), ("2\n", 0), ("3 999\n", 0),
                             ("junk\n", 0), ("3\n", 2)):
            with self.subTest(stdout=stdout, code=code):
                self.assertIsNone(self.run_probe(stdout, code)[0])

    def test_a_probe_that_hangs_or_cannot_start_is_unknown(self):
        self.assertIsNone(self.run_probe(
            error=subprocess.TimeoutExpired("python3", 10))[0])
        self.assertIsNone(self.run_probe(error=OSError("no python"))[0])

    def test_the_driver_is_asked_in_a_child_process(self):
        _, calls = self.run_probe("3\n")
        argv, kwargs = calls[0]
        self.assertEqual(argv[:3], [sys.executable, "-I", "-c"])
        self.assertEqual(argv[-1], "0000:2b:00.0")
        self.assertIn("nvmlDeviceGetPowerUsage", argv[3])
        self.assertTrue(kwargs.get("timeout"))

    def test_probe_script_compiles(self):
        compile(mangohud._PROBE, "<nvml probe>", "exec")

    def test_probe_reports_what_nvml_answers(self):
        # The probe itself, against a stand-in NVML that answers like the
        # reporter's driver: not supported, output left alone.
        fake = mock.MagicMock()
        fake.nvmlInit_v2.return_value = 0
        fake.nvmlDeviceGetCount_v2.return_value = 0
        fake.nvmlDeviceGetHandleByPciBusId_v2.return_value = 0
        fake.nvmlDeviceGetPowerUsage.return_value = 3
        printed = []
        with mock.patch("ctypes.CDLL", return_value=fake), \
                mock.patch.object(sys, "argv", ["-c", "0000:2b:00.0"]), \
                mock.patch("builtins.print",
                           side_effect=lambda *a, **k: printed.append(a)):
            exec(compile(mangohud._PROBE, "<nvml probe>", "exec"), {})
        self.assertEqual(printed, [("3",)])
        fake.nvmlShutdown.assert_called_once()


if __name__ == "__main__":
    unittest.main()
