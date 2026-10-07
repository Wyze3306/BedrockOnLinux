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
    def _advice(self, cards, settings=None, environ=None):
        with tempfile.TemporaryDirectory() as td:
            drm, ids = _sysfs(td, cards)
            found = gpus.list_gpus(drm, ids)
        return gpus.hybrid_gpu_problem(settings or {}, environ or {}, found)

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


if __name__ == "__main__":
    unittest.main()
