"""Keep non-controller hidraw devices away from Wine's HID stack."""
# SPDX-License-Identifier: MIT

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import hidraw
from tests.test_wine_registry import SYSTEM, USER


# An NZXT 1e71:1714 fan/RGB controller, verbatim from sysfs. It declares a
# 21-byte input report and sends 64, which crashed winedevice.exe -- and the
# whole HID stack with it -- on every start.
NZXT_1714 = bytes.fromhex(
    "0600ff0901a10185010901150026ff0075089501b18285010901918285020902150026"
    "ff0075089540b18285020902918285030903150026ff0075089540b182850309039182"
    "8504090475089514818285050905750895018182c0")
# A Logitech receiver's three interfaces: boot mouse, keyboard, HID++.
MOUSE = bytes.fromhex(
    "05010902a1010901a10005091901290315002501950375018102950175058101"
    "0501093009311581257f750895028106c0c0")
KEYBOARD = bytes.fromhex("05010906a101050719e029e715002501750195088102c0")
VENDOR = bytes.fromhex("0600ff0901a101851095067508150026ff0009018100c0")
GAMEPAD = bytes.fromhex("05010905a101850109300931150026ff00750895028102c0")


def make_node(root, name, hid_id, descriptor, accessible=True):
    """A sysfs hidraw entry under root/sys, its device node under root/dev."""
    device = Path(root) / "sys" / name / "device"
    device.mkdir(parents=True)
    if accessible:
        (Path(root) / "dev").mkdir(exist_ok=True)
        (Path(root) / "dev" / name).touch()
    if hid_id is not None:
        (device / "uevent").write_text(
            f"DRIVER=hid-generic\nHID_ID={hid_id}\nHID_NAME=test\n")
    else:
        (device / "uevent").write_text("DRIVER=hid-generic\n")
    if descriptor is not None:
        (device / "report_descriptor").write_bytes(descriptor)


class ReportDescriptorTests(unittest.TestCase):
    def test_real_nzxt_fan_controller_is_vendor_defined(self):
        usages = hidraw.application_usages(NZXT_1714)
        self.assertEqual(usages, [(0xFF00, 0x0001)])
        self.assertFalse(hidraw.is_controller(0x1E71, usages))

    def test_only_top_level_application_collections_count(self):
        # The mouse nests a physical pointer collection inside its
        # application collection; only the outer one classifies it.
        self.assertEqual(hidraw.application_usages(MOUSE), [(0x01, 0x02)])

    def test_every_top_level_collection_is_reported(self):
        consumer = bytes.fromhex("050c0901a101c0")
        self.assertEqual(hidraw.application_usages(KEYBOARD + consumer),
                         [(0x01, 0x06), (0x0C, 0x01)])

    def test_extended_usage_carries_its_own_page(self):
        usages = hidraw.application_usages(bytes.fromhex("0b05000100a101c0"))
        self.assertEqual(usages, [(0x01, 0x05)])
        self.assertTrue(hidraw.is_controller(0x1234, usages))

    def test_pop_restores_the_pushed_usage_page(self):
        descriptor = bytes.fromhex("0501a40600ffb40904a101c0")
        self.assertEqual(hidraw.application_usages(descriptor), [(0x01, 0x04)])

    def test_long_item_is_skipped(self):
        descriptor = bytes.fromhex("fe0210aabb05010908a101c0")
        self.assertEqual(hidraw.application_usages(descriptor), [(0x01, 0x08)])

    def test_truncated_descriptor_keeps_what_was_complete(self):
        self.assertEqual(hidraw.application_usages(GAMEPAD + b"\x06\x00"),
                         [(0x01, 0x05)])
        self.assertEqual(hidraw.application_usages(b"\xfe"), [])
        self.assertEqual(hidraw.application_usages(b""), [])

    def test_controller_pages_and_valve_are_controllers(self):
        self.assertTrue(hidraw.is_controller(0x1234, [(0x02, 0x01)]))
        self.assertTrue(hidraw.is_controller(0x1234, [(0x05, 0x01)]))
        self.assertTrue(hidraw.is_controller(0x28DE, [(0xFF00, 0x01)]))
        self.assertFalse(hidraw.is_controller(0x1234, [(0x01, 0x02)]))


class SysfsTests(unittest.TestCase):
    def make_tree(self, root):
        make_node(root, "hidraw0", "0003:0000046D:0000C547", MOUSE)
        make_node(root, "hidraw1", "0003:0000046D:0000C547", KEYBOARD)
        make_node(root, "hidraw2", "0003:0000046D:0000C547", VENDOR)
        make_node(root, "hidraw3", "0003:00001E71:00001714", NZXT_1714)
        make_node(root, "hidraw4", "0005:0000054C:00000CE6", GAMEPAD)
        # A composite device keeps hidraw if any interface is a controller.
        make_node(root, "hidraw5", "0003:00001234:00005678", VENDOR)
        make_node(root, "hidraw6", "0003:00001234:00005678", GAMEPAD)
        make_node(root, "hidraw7", "0003:000028DE:00001205", VENDOR)
        make_node(root, "hidraw8", "0003:0000AAAA:0000BBBB", None)
        make_node(root, "hidraw9", None, VENDOR)
        make_node(root, "hidraw10", "0003:0000CCCC:0000DDDD", b"")
        # Root-only, as most hidraw nodes are: Wine cannot open it either.
        make_node(root, "hidraw11", "0003:00001532:000000B7", MOUSE,
                  accessible=False)
        return Path(root) / "sys", Path(root) / "dev"

    def test_interfaces_are_pooled_per_device(self):
        with tempfile.TemporaryDirectory() as td:
            devices = hidraw.hidraw_devices(*self.make_tree(td))
        self.assertEqual(devices[(0x046D, 0xC547)],
                         [(0x01, 0x02), (0x01, 0x06), (0xFF00, 0x01)])
        self.assertNotIn((0xAAAA, 0xBBBB), devices)

    def test_nodes_the_user_cannot_open_are_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            devices = hidraw.hidraw_devices(*self.make_tree(td))
        self.assertNotIn((0x1532, 0x00B7), devices)

    def test_only_classified_non_controllers_are_selected(self):
        with tempfile.TemporaryDirectory() as td:
            idents = hidraw.non_controllers(
                hidraw.hidraw_devices(*self.make_tree(td)))
        self.assertEqual(idents, [(0x046D, 0xC547), (0x1E71, 0x1714)])

    def test_missing_sysfs_class_is_empty(self):
        self.assertEqual(hidraw.hidraw_devices(Path("/nonexistent/hidraw")), {})

    def test_summary_names_the_devices(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIn("046d:c547, 1e71:1714",
                          hidraw.summary(*self.make_tree(td)))
        with tempfile.TemporaryDirectory() as td:
            self.assertIn("no non-controller",
                          hidraw.summary(Path(td), Path(td)))


class PrefixTests(unittest.TestCase):
    def make_prefix(self, root):
        prefix = Path(root) / "pfx"
        prefix.mkdir()
        (prefix / "system.reg").write_bytes(SYSTEM)
        (prefix / "user.reg").write_bytes(USER)
        return prefix

    def make_tree(self, root):
        make_node(root, "hidraw0", "0003:00001E71:00001714", NZXT_1714)
        make_node(root, "hidraw1", "0003:00001E71:0000170E", NZXT_1714)
        make_node(root, "hidraw2", "0005:0000054C:00000CE6", GAMEPAD)
        return Path(root) / "sys", Path(root) / "dev"

    def test_winebus_device_options_land_in_the_selected_control_set(self):
        with tempfile.TemporaryDirectory() as td:
            prefix = self.make_prefix(td)
            tree = self.make_tree(td)
            with mock.patch.object(hidraw, "info") as info:
                idents = hidraw.keep_non_controllers_off_hidraw(
                    prefix, *tree, environ={})
                self.assertEqual(idents, [(0x1E71, 0x170E), (0x1E71, 0x1714)])
                system = (prefix / "system.reg").read_text()
                for key in ("1e71/1714", "1e71/170e"):
                    section = (r"[System\\ControlSet002\\Services\\winebus"
                               r"\\Devices\\" + key + "]")
                    self.assertIn(section, system)
                self.assertEqual(system.count('"Hidraw"=dword:00000000'), 2)
                self.assertNotIn("054c", system)
                self.assertEqual(info.call_count, 1)

                # Re-applied at every PLAY without rewriting or re-logging.
                before = (prefix / "system.reg").read_bytes()
                hidraw.keep_non_controllers_off_hidraw(prefix, *tree, environ={})
                self.assertEqual((prefix / "system.reg").read_bytes(), before)
                self.assertEqual(info.call_count, 1)

    def test_opt_out_restores_wines_default(self):
        with tempfile.TemporaryDirectory() as td:
            prefix = self.make_prefix(td)
            tree = self.make_tree(td)
            with mock.patch.object(hidraw, "info"):
                hidraw.keep_non_controllers_off_hidraw(prefix, *tree, environ={})
            self.assertEqual(hidraw.keep_non_controllers_off_hidraw(
                prefix, *tree, environ={"BOL_HIDRAW": "all"}), [])
            self.assertNotIn('"Hidraw"=', (prefix / "system.reg").read_text())

    def test_nothing_to_keep_off_leaves_the_prefix_alone(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(hidraw, "update_prefix_registry") as update:
                self.assertEqual(hidraw.keep_non_controllers_off_hidraw(
                    Path(td), Path(td) / "empty", environ={}), [])
            update.assert_not_called()

    def test_playstation_pads_go_through_sdl(self):
        # winebus prefers hidraw for these, which leaves the game only the
        # touchpad (#57); Hidraw = 0 sends them through SDL's gamepad mapping.
        with tempfile.TemporaryDirectory() as td:
            prefix = self.make_prefix(td)
            self.assertTrue(hidraw.playstation_pads_through_sdl(
                prefix, environ={}))
            system = (prefix / "system.reg").read_text()
            for key in ("054c/05c4", "054c/09cc", "054c/0ba0", "054c/0ce6",
                        "054c/0df2"):
                section = (r"[System\\ControlSet002\\Services\\winebus"
                           r"\\Devices\\" + key + "]")
                self.assertIn(section, system)
            self.assertEqual(system.count('"Hidraw"=dword:00000000'), 5)

            # Re-applied at every PLAY without rewriting the hive.
            before = (prefix / "system.reg").read_bytes()
            self.assertFalse(hidraw.playstation_pads_through_sdl(
                prefix, environ={}))
            self.assertEqual((prefix / "system.reg").read_bytes(), before)

    def test_playstation_opt_out_restores_wines_default(self):
        with tempfile.TemporaryDirectory() as td:
            prefix = self.make_prefix(td)
            hidraw.playstation_pads_through_sdl(prefix, environ={})
            hidraw.playstation_pads_through_sdl(
                prefix, environ={"BOL_HIDRAW": "all"})
            self.assertNotIn('"Hidraw"=', (prefix / "system.reg").read_text())


if __name__ == "__main__":
    unittest.main()
