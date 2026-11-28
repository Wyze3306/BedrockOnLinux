"""Which graphics card Minecraft renders on (#275).

A laptop starts every program on its integrated GPU unless told otherwise,
so Minecraft ran on an Intel iGPU beside an idle RTX 3050. These tests build
sysfs trees for the machines that matter and hold the inventory, the
environment a choice turns into, and the advice given when nothing was
chosen.
"""
# SPDX-License-Identifier: MIT

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import gpus

_PCI_IDS = """\
# fake pci.ids
1002  Advanced Micro Devices, Inc. [AMD/ATI]
\t1638  Cezanne [Radeon Vega Series / Radeon Vega Mobile Series]
\t73ff  Navi 23 [Radeon RX 6600/6600 XT/6600M]
10de  NVIDIA Corporation
\t25a2  GA107M [GeForce RTX 3050 Mobile]
\t2882  AD107 [GeForce RTX 4060]
8086  Intel Corporation
\t3e9b  CoffeeLake-H GT2 [UHD Graphics 630]
\t\t1028 0000  a subsystem line, never a device
\t56a5  DG2 [Arc A380]
"""


def _sysfs(root, cards):
    """cards: (card, slot, vendor, device, driver, boot_vga) tuples."""
    root = Path(root)
    drm = root / "class/drm"
    drm.mkdir(parents=True)
    for card, slot, vendor, device, driver, boot in cards:
        pci = root / "devices/pci" / slot
        pci.mkdir(parents=True)
        (pci / "vendor").write_text("0x%s\n" % vendor)
        (pci / "device").write_text("0x%s\n" % device)
        if boot is not None:
            (pci / "boot_vga").write_text("1\n" if boot else "0\n")
        if driver:
            target = root / "bus/pci/drivers" / driver
            target.mkdir(parents=True, exist_ok=True)
            os.symlink(target, pci / "driver")
        (drm / card).mkdir()
        os.symlink(pci, drm / card / "device")
        (drm / (card + "-eDP-1")).mkdir()
    ids = root / "pci.ids"
    ids.write_text(_PCI_IDS)
    return drm, ids


_OPTIMUS = (("card1", "0000:00:02.0", "8086", "3e9b", "i915", True),
            ("card0", "0000:01:00.0", "10de", "25a2", "nvidia", False))
_AMD_LAPTOP = (("card0", "0000:05:00.0", "1002", "1638", "amdgpu", True),
               ("card1", "0000:01:00.0", "10de", "25a2", "nvidia", False))
_DESKTOP = (("card0", "0000:2b:00.0", "10de", "2882", "nvidia", True),)
_DESKTOP_WITH_IGPU = (("card0", "0000:01:00.0", "1002", "73ff", "amdgpu", True),
                      ("card1", "0000:00:02.0", "8086", "3e9b", "i915", False))


class InventoryTests(unittest.TestCase):
    def _list(self, cards):
        with tempfile.TemporaryDirectory() as td:
            drm, ids = _sysfs(td, cards)
            return gpus.list_gpus(drm, ids)

    def test_a_laptop_lists_both_cards_display_first(self):
        found = self._list(_OPTIMUS)
        self.assertEqual([g.slot for g in found],
                         ["0000:00:02.0", "0000:01:00.0"])
        intel, nvidia = found
        self.assertTrue(intel.boot_vga and intel.integrated)
        self.assertEqual(intel.driver, "i915")
        self.assertEqual(intel.name, "Intel UHD Graphics 630")
        self.assertTrue(nvidia.nvidia and not nvidia.boot_vga)
        self.assertEqual(nvidia.name, "NVIDIA GeForce RTX 3050 Mobile")

    def test_connectors_and_non_pci_nodes_are_not_cards(self):
        with tempfile.TemporaryDirectory() as td:
            drm, ids = _sysfs(td, _DESKTOP)
            # simpledrm: a card node with no PCI identity behind it.
            (Path(td) / "platform").mkdir()
            (drm / "card9").mkdir()
            os.symlink(Path(td) / "platform", drm / "card9" / "device")
            found = gpus.list_gpus(drm, ids)
        self.assertEqual([g.slot for g in found], ["0000:2b:00.0"])

    def test_a_card_missing_from_pci_ids_still_has_a_name(self):
        found = self._list((("card0", "0000:03:00.0", "1af4", "1050",
                             "virtio-pci", True),))
        self.assertEqual(found[0].name, "Virtio graphics (1af4:1050)")

    def test_intel_arc_is_discrete(self):
        found = self._list((("card0", "0000:03:00.0", "8086", "56a5", "i915",
                             False),))
        self.assertFalse(found[0].integrated)
        self.assertEqual(found[0].name, "Intel Arc A380")

    def test_no_drm_at_all_is_no_cards(self):
        self.assertEqual(gpus.list_gpus("/nonexistent/drm"), [])


class EnvironmentTests(unittest.TestCase):
    def _cards(self, cards):
        with tempfile.TemporaryDirectory() as td:
            drm, ids = _sysfs(td, cards)
            return gpus.list_gpus(drm, ids)

    def test_an_nvidia_card_is_asked_for_through_every_layer(self):
        nvidia = self._cards(_OPTIMUS)[1]
        self.assertEqual(gpus.gpu_env(nvidia), {
            "MESA_VK_DEVICE_SELECT": "10de:25a2!",
            "DRI_PRIME": "pci-0000_01_00_0",
            "__NV_PRIME_RENDER_OFFLOAD": "1",
            "__VK_LAYER_NV_optimus": "NVIDIA_only",
            "__GLX_VENDOR_LIBRARY_NAME": "nvidia",
        })

    def test_another_card_is_asked_for_through_mesa(self):
        intel = self._cards(_OPTIMUS)[0]
        self.assertEqual(gpus.gpu_env(intel), {
            "MESA_VK_DEVICE_SELECT": "8086:3e9b!",
            "DRI_PRIME": "pci-0000_00_02_0",
        })

    def test_automatic_leaves_the_environment_alone(self):
        cards = self._cards(_OPTIMUS)
        env = {"DRI_PRIME": "1"}
        self.assertIsNone(gpus.apply_gpu_choice(env, {}, cards))
        self.assertIsNone(gpus.apply_gpu_choice(env, {"gpu": "auto"}, cards))
        self.assertEqual(env, {"DRI_PRIME": "1"})

    def test_a_chosen_card_replaces_what_the_desktop_asked_for(self):
        cards = self._cards(_OPTIMUS)
        # The desktop's "discrete" launch, overruled by an explicit iGPU.
        env = {"DRI_PRIME": "1", "__NV_PRIME_RENDER_OFFLOAD": "1",
               "__GLX_VENDOR_LIBRARY_NAME": "nvidia", "OTHER": "kept"}
        chosen = gpus.apply_gpu_choice(env, {"gpu": "0000:00:02.0"}, cards)
        self.assertEqual(chosen.slot, "0000:00:02.0")
        self.assertEqual(env, {"MESA_VK_DEVICE_SELECT": "8086:3e9b!",
                               "DRI_PRIME": "pci-0000_00_02_0",
                               "OTHER": "kept"})

    def test_a_card_that_is_gone_starts_on_the_default_and_says_so(self):
        cards = self._cards(_DESKTOP)
        env = {}
        with mock.patch("bol.log.warn") as warned:
            self.assertIsNone(gpus.apply_gpu_choice(
                env, {"gpu": "0000:08:00.0"}, cards))
        self.assertEqual(env, {})
        self.assertIn("0000:08:00.0", warned.call_args.args[0])


class HybridAdviceTests(unittest.TestCase):
    def _advice(self, cards, settings=None, environ=None, renderer="wined3d"):
        with tempfile.TemporaryDirectory() as td:
            drm, ids = _sysfs(td, cards)
            found = gpus.list_gpus(drm, ids)
        return gpus.hybrid_gpu_problem(settings or {}, environ or {}, found,
                                       renderer=renderer)

    def test_the_d3d12_path_already_takes_the_discrete_card(self):
        # DXVK ranks a discrete adapter first and vkd3d-proton renders on
        # DXGI's first one: nothing to warn about (#333).
        self.assertIsNone(self._advice(_OPTIMUS, renderer="d3d12"))
        self.assertIsNone(self._advice(_AMD_LAPTOP, renderer=None))

    def test_the_advice_names_the_legacy_renderer(self):
        self.assertIn("Legacy renderer", self._advice(_OPTIMUS))

    def test_an_intel_laptop_with_a_geforce_is_told_where_the_choice_is(self):
        advice = self._advice(_OPTIMUS)
        self.assertIn("Intel UHD Graphics 630", advice)
        self.assertIn("NVIDIA GeForce RTX 3050 Mobile", advice)
        self.assertIn("Settings ▸ Advanced ▸ Graphics card", advice)

    def test_an_amd_laptop_with_a_geforce_is_too(self):
        self.assertIn("NVIDIA GeForce RTX 3050 Mobile",
                      self._advice(_AMD_LAPTOP))

    def test_a_choice_already_made_is_not_questioned(self):
        self.assertIsNone(self._advice(_OPTIMUS, {"gpu": "0000:00:02.0"}))
        self.assertIsNone(self._advice(_OPTIMUS, {"gpu": "0000:01:00.0"}))

    def test_the_desktops_own_discrete_launch_is_an_answer(self):
        self.assertIsNone(self._advice(_OPTIMUS, environ={"DRI_PRIME": "1"}))
        self.assertIsNone(self._advice(
            _OPTIMUS, environ={"__NV_PRIME_RENDER_OFFLOAD": "1"}))

    def test_a_single_card_has_nothing_to_choose(self):
        self.assertIsNone(self._advice(_DESKTOP))

    def test_a_desktop_on_its_discrete_card_is_left_alone(self):
        # The enabled iGPU beside it is the slower one; nothing to suggest.
        self.assertIsNone(self._advice(_DESKTOP_WITH_IGPU))


class DriverFileTests(unittest.TestCase):
    """A driver list naming files that are not here gives no Vulkan (#333)."""

    def test_missing_entries_are_dropped_and_present_ones_kept(self):
        here = {"/run/host/nvidia_icd.json"}
        env = {"VK_ICD_FILENAMES": "/a/missing.json:/run/host/nvidia_icd.json",
               "VK_DRIVER_FILES": "/usr/share/vulkan/icd.d/nvidia_icd.json",
               "OTHER": "kept"}
        dropped = gpus.drop_missing_driver_files(env, exists=here.__contains__)
        self.assertEqual(env, {"VK_ICD_FILENAMES": "/run/host/nvidia_icd.json",
                               "OTHER": "kept"})
        self.assertEqual(dropped, {
            "VK_DRIVER_FILES": ["/usr/share/vulkan/icd.d/nvidia_icd.json"],
            "VK_ICD_FILENAMES": ["/a/missing.json"]})

    def test_nothing_to_drop_says_nothing(self):
        env = {"VK_DRIVER_FILES": "/x.json"}
        self.assertEqual(gpus.drop_missing_driver_files(
            env, exists=lambda path: True), {})
        self.assertEqual(env, {"VK_DRIVER_FILES": "/x.json"})
        self.assertIsNone(gpus.missing_driver_files_problem({}))

    def test_the_problem_names_the_file_and_the_flatpak(self):
        message = gpus.missing_driver_files_problem(
            {"VK_DRIVER_FILES": ["/usr/share/vulkan/icd.d/nvidia_icd.json"]},
            flatpak=True)
        self.assertIn("VK_DRIVER_FILES: /usr/share/vulkan/icd.d/nvidia_icd.json",
                      message)
        self.assertIn("GL extensions", message)
        self.assertNotIn("Flatpak", gpus.missing_driver_files_problem(
            {"VK_DRIVER_FILES": ["/x.json"]}))


# What the engine's winevulkan writes on its vkdevice channel.
_GEFORCE_LINE = (b'0118:011c:trace:vkdevice:vkCreateDevice '
                 b'"NVIDIA GeForce RTX 3050 Laptop GPU", vendor 0x10de, '
                 b'device 0x25a2, driver 0x89b28100.\n')
_INTEL_LINE = (b'0118:011c:trace:vkdevice:vkCreateDevice '
               b'"Intel(R) UHD Graphics 630 (CFL GT2)", vendor 0x8086, '
               b'device 0x3e9b, driver 0x6400007.\n')


class RenderedOnTests(unittest.TestCase):
    """Which card the game really drew with, from its own log (#333)."""

    def _logs(self, td, **files):
        for name, (data, age) in files.items():
            path = Path(td) / name
            path.write_bytes(data)
            os.utime(path, (1_000_000 - age, 1_000_000 - age))
        return td

    def test_the_device_line_names_the_card(self):
        with tempfile.TemporaryDirectory() as td:
            self._logs(td, **{"minecraft.log": (
                b"info:vkd3d-proton:x\n" + _GEFORCE_LINE * 2, 0)})
            self.assertEqual(gpus.rendered_on(td), [
                ("NVIDIA GeForce RTX 3050 Laptop GPU", "10de:25a2")])

    def test_the_newest_log_answers(self):
        with tempfile.TemporaryDirectory() as td:
            self._logs(td, **{"minecraft.log": (_INTEL_LINE, 100),
                              "proton.log": (_GEFORCE_LINE, 0)})
            self.assertEqual(gpus.rendered_on(td)[0][1], "10de:25a2")

    def test_an_engine_that_does_not_say_is_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            self._logs(td, **{"minecraft.log": (b"info:vkd3d-proton:x\n", 0)})
            self.assertEqual(gpus.rendered_on(td), [])
            self.assertIn("unknown", gpus.rendered_on_summary(td, gpus=[]))

    def test_the_summary_says_which_kind_of_card_it_was(self):
        with tempfile.TemporaryDirectory() as td:
            drm, ids = _sysfs(Path(td) / "sys", _OPTIMUS)
            cards = gpus.list_gpus(drm, ids)
            self._logs(td, **{"minecraft.log": (_GEFORCE_LINE, 0)})
            self.assertEqual(
                gpus.rendered_on_summary(td, gpus=cards),
                "NVIDIA GeForce RTX 3050 Laptop GPU [10de:25a2] — the "
                "discrete GPU")
            self._logs(td, **{"minecraft.log": (_INTEL_LINE, 0)})
            self.assertIn("the integrated GPU",
                          gpus.rendered_on_summary(td, gpus=cards))

    def test_a_lone_card_needs_no_label(self):
        with tempfile.TemporaryDirectory() as td:
            drm, ids = _sysfs(Path(td) / "sys", _DESKTOP)
            cards = gpus.list_gpus(drm, ids)
            self._logs(td, **{"minecraft.log": (
                b'trace:vkdevice:vkCreateDevice "NVIDIA GeForce RTX 4060", '
                b'vendor 0x10de, device 0x2882, driver 0x1.\n', 0)})
            self.assertEqual(gpus.rendered_on_summary(td, gpus=cards),
                             "NVIDIA GeForce RTX 4060 [10de:2882]")


if __name__ == "__main__":
    unittest.main()
