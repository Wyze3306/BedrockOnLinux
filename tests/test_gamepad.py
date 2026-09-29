"""Tests for the evdev controller reader."""
# SPDX-License-Identifier: MIT

import os
import unittest

from bol import gamepad
from bol.gamepad import (
    ABS_HAT0X,
    ABS_HAT0Y,
    ABS_RY,
    ABS_X,
    BTN_DPAD_DOWN,
    BTN_EAST,
    BTN_SOUTH,
    BTN_START,
    BTN_TL,
    EV_ABS,
    EV_KEY,
    GamepadReader,
    _Device,
    _EVENT,
    axis_direction,
    looks_like_gamepad,
    normalize_axis,
)

STICK = (-32768, 32767)
HAT = (-1, 1)


def bitmap(*codes):
    """An evdev capability bitmap with `codes` set."""
    bits = bytearray(96)
    for code in codes:
        bits[code >> 3] |= 1 << (code & 7)
    return bits


class IoctlEncodingTests(unittest.TestCase):
    def test_request_numbers_match_the_kernel_headers(self):
        # <linux/input.h>: EVIOCGNAME(len) = _IOR('E', 0x06, len). The values
        # are what the ioctls actually have to be on x86-64; getting the
        # encoding wrong reads back nothing rather than failing loudly.
        self.assertEqual(gamepad._EVIOCGNAME(256), 0x81004506)
        self.assertEqual(gamepad._EVIOCGBIT(EV_KEY, 96), 0x80604521)
        self.assertEqual(gamepad._EVIOCGABS(ABS_X), 0x80184540)
        self.assertEqual(gamepad._EVIOCGID(), 0x80084502)

    def test_bit_set_reads_the_capability_bitmap(self):
        bits = bitmap(BTN_SOUTH, BTN_START)
        self.assertTrue(gamepad._bit_set(bits, BTN_SOUTH))
        self.assertTrue(gamepad._bit_set(bits, BTN_START))
        self.assertFalse(gamepad._bit_set(bits, BTN_EAST))
        self.assertFalse(gamepad._bit_set(bits, 0x2FF))
        self.assertFalse(gamepad._bit_set(None, BTN_SOUTH))


class DeviceDetectionTests(unittest.TestCase):
    def _detect(self, events, keys, axes):
        answers = {
            gamepad._EVIOCGBIT(0, 4): bitmap(*events)[:4],
            gamepad._EVIOCGBIT(EV_KEY, 96): bitmap(*keys),
            gamepad._EVIOCGBIT(EV_ABS, 8): bitmap(*axes)[:8],
        }
        original = gamepad._ioctl_bytes
        gamepad._ioctl_bytes = lambda fd, request, length: answers.get(request)
        try:
            return looks_like_gamepad(0)
        finally:
            gamepad._ioctl_bytes = original

    def test_a_gamepad_is_recognised(self):
        self.assertTrue(self._detect(
            [EV_KEY, EV_ABS], [BTN_SOUTH, BTN_EAST, BTN_START], [ABS_X, 1]))

    def test_a_keyboard_is_not(self):
        # No absolute axes: the launcher must not start taking navigation
        # from the keyboard's event node.
        self.assertFalse(self._detect([EV_KEY], [0x1E, 0x1F], []))

    def test_a_sensor_without_buttons_is_not(self):
        self.assertFalse(self._detect([EV_ABS], [], [ABS_X, 1]))

    def test_a_hat_only_pad_is_recognised(self):
        self.assertTrue(self._detect(
            [EV_KEY, EV_ABS], [BTN_SOUTH], [ABS_HAT0X]))


class AxisTests(unittest.TestCase):
    def test_normalisation_spans_minus_one_to_one(self):
        self.assertAlmostEqual(normalize_axis(32767, *STICK), 1.0, places=2)
        self.assertAlmostEqual(normalize_axis(-32768, *STICK), -1.0, places=2)
        self.assertAlmostEqual(normalize_axis(0, *STICK), 0.0, places=2)
        self.assertEqual(normalize_axis(5, 10, 10), 0.0)     # degenerate range

    def test_a_resting_stick_is_no_direction(self):
        self.assertIsNone(axis_direction(0, *STICK))
        self.assertIsNone(axis_direction(6000, *STICK))

    def test_deflection_reports_a_direction(self):
        self.assertEqual(axis_direction(30000, *STICK), "positive")
        self.assertEqual(axis_direction(-30000, *STICK), "negative")

    def test_release_uses_a_lower_threshold_than_press(self):
        # Hysteresis: a stick resting just under the press threshold must not
        # machine-gun the menu, and one on the way back must not re-trigger.
        held = axis_direction(15000, *STICK, previous="positive")
        self.assertEqual(held, "positive")
        self.assertIsNone(axis_direction(15000, *STICK))
        self.assertIsNone(axis_direction(9000, *STICK, previous="positive"))


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.actions = []
        self.reader = GamepadReader(self.actions.append)
        self.read_fd, self.write_fd = os.pipe()
        self.addCleanup(self._close)
        self.device = _Device(self.read_fd, "/dev/input/event9", "Test Pad",
                              {ABS_X: STICK, ABS_RY: STICK,
                               ABS_HAT0X: HAT, ABS_HAT0Y: HAT})

    def _close(self):
        for fd in (self.read_fd, self.write_fd):
            try:
                os.close(fd)
            except OSError:
                pass

    def _feed(self, *events):
        payload = b"".join(_EVENT.pack(0, 0, kind, code, value)
                           for kind, code, value in events)
        os.write(self.write_fd, payload)
        self.assertTrue(self.reader._read(self.device))

    def test_face_buttons_report_on_press_only(self):
        self._feed((EV_KEY, BTN_SOUTH, 1), (EV_KEY, BTN_SOUTH, 0),
                   (EV_KEY, BTN_EAST, 1), (EV_KEY, BTN_TL, 1))
        self.assertEqual(self.actions, ["accept", "back", "prev_tab"])

    def test_autorepeat_is_ignored(self):
        self._feed((EV_KEY, BTN_SOUTH, 2))
        self.assertEqual(self.actions, [])

    def test_a_held_dpad_repeats(self):
        self._feed((EV_KEY, BTN_DPAD_DOWN, 1))
        self.assertEqual(self.actions, ["down"])
        due = self.reader._repeats["nav"][1]
        self.reader._due_repeats(due - 0.01)
        self.assertEqual(self.actions, ["down"])          # not yet
        self.reader._due_repeats(due)
        self.assertEqual(self.actions, ["down", "down"])
        self._feed((EV_KEY, BTN_DPAD_DOWN, 0))
        self.assertNotIn("nav", self.reader._repeats)

    def test_the_stick_moves_the_ring_and_stops_when_centred(self):
        self._feed((EV_ABS, ABS_X, 32767))
        self.assertEqual(self.actions, ["right"])
        self._feed((EV_ABS, ABS_X, 30000))                # still held
        self.assertEqual(self.actions, ["right"])
        self._feed((EV_ABS, ABS_X, 0))
        self.assertEqual(self.actions, ["right"])
        self.assertNotIn("nav", self.reader._repeats)

    def test_the_hat_switch_moves_the_ring(self):
        self._feed((EV_ABS, ABS_HAT0Y, -1), (EV_ABS, ABS_HAT0Y, 0),
                   (EV_ABS, ABS_HAT0X, 1))
        self.assertEqual(self.actions, ["up", "right"])

    def test_the_right_stick_scrolls_on_its_own_repeat_channel(self):
        self._feed((EV_ABS, ABS_RY, -32768), (EV_ABS, ABS_X, -32768))
        self.assertEqual(self.actions, ["scroll_up", "left"])
        self.assertEqual(self.reader._repeats["scroll"][0], "scroll_up")
        self.assertEqual(self.reader._repeats["nav"][0], "left")
        # Scrolling repeats faster than menu movement.
        self.assertLess(self.reader._repeats["scroll"][1],
                        self.reader._repeats["nav"][1])

    def test_an_axis_the_device_never_declared_is_ignored(self):
        device = _Device(self.read_fd, "/dev/input/event9", "Pad", {})
        self.reader._handle_event(device, EV_ABS, ABS_X, 32767)
        self.assertEqual(self.actions, [])

    def test_a_closed_device_reports_gone(self):
        os.close(self.write_fd)
        os.close(self.read_fd)
        self.assertFalse(self.reader._read(self.device))
        self.read_fd = self.write_fd = -1


PROC_SAMPLE = """\
I: Bus=0019 Vendor=0000 Product=0001 Version=0000
N: Name="Power Button"
H: Handlers=kbd event0
B: EV=3

I: Bus=0003 Vendor=045e Product=028e Version=0110
N: Name="Microsoft X-Box 360 pad"
H: Handlers=event20 js0
B: EV=20000b

I: Bus=0003 Vendor=1532 Product=0098 Version=0111
N: Name="Razer DeathAdder"
H: Handlers=mouse0 event3
B: EV=17
"""


class JoystickNodeTests(unittest.TestCase):
    def _proc(self, text=PROC_SAMPLE):
        import tempfile
        handle = tempfile.NamedTemporaryFile("w", suffix=".devices",
                                             delete=False)
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_only_devices_the_joystick_driver_claimed_are_listed(self):
        self.assertEqual(gamepad.joystick_event_nodes(self._proc()),
                         {"event20"})

    def test_a_missing_proc_file_is_not_an_error(self):
        self.assertEqual(gamepad.joystick_event_nodes("/nonexistent"), set())


class SummaryTests(unittest.TestCase):
    def _tree(self, proc_text):
        import tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        node = os.path.join(tmp, "event20")
        open(node, "w").close()
        os.chmod(node, 0o000)
        proc = os.path.join(tmp, "devices")
        with open(proc, "w") as handle:
            handle.write(proc_text)
        return tmp, proc

    @unittest.skipIf(os.geteuid() == 0, "root opens anything")
    def test_a_controller_that_cannot_be_opened_says_so(self):
        tmp, proc = self._tree(PROC_SAMPLE)
        self.assertIn("not readable", gamepad.summary(tmp, proc))

    @unittest.skipIf(os.geteuid() == 0, "root opens anything")
    def test_an_unreadable_keyboard_is_not_reported_as_a_controller(self):
        # Every user has unreadable keyboard and mouse nodes; only a refused
        # joystick is worth a permission hint.
        tmp, proc = self._tree(PROC_SAMPLE.replace("event20 js0", "event20"))
        self.assertEqual(gamepad.summary(tmp, proc), "none connected")

    def test_no_input_directory_at_all(self):
        self.assertIn("unavailable", gamepad.summary("/nonexistent/input"))


class DiscoveryTests(unittest.TestCase):
    def test_only_event_nodes_are_probed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("event0", "event12", "mouse0", "js0", "by-id"):
                open(os.path.join(tmp, name), "w").close()
            reader = GamepadReader(lambda action: None, input_dir=tmp)
            self.assertEqual(
                [os.path.basename(path) for path in reader._candidate_paths()],
                ["event0", "event12"])

    def test_a_node_that_is_not_a_pad_is_only_examined_once(self):
        # Opening an input device can wake the hardware behind it, so the
        # two-second rescan must not keep reopening the keyboard.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("event0", "event1"):
                open(os.path.join(tmp, name), "w").close()
            reader = GamepadReader(lambda action: None, input_dir=tmp)
            probed = []
            reader._probe = lambda path: (probed.append(path)
                                          or gamepad.NOT_A_GAMEPAD)
            reader._rescan()
            reader._rescan()
            self.assertEqual(len(probed), 2)
            # …until the node is replaced by a different device.
            os.unlink(os.path.join(tmp, "event0"))
            reader._rescan()
            open(os.path.join(tmp, "event0"), "w").close()
            reader._rescan()
            self.assertEqual(len(probed), 3)

    def test_an_unreadable_node_is_examined_again_next_time(self):
        # udev applies the ACL a moment after creating the node; a pad plugged
        # in right now is briefly unreadable and must not be written off.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            open(os.path.join(tmp, "event0"), "w").close()
            reader = GamepadReader(lambda action: None, input_dir=tmp)
            probed = []
            reader._probe = lambda path: probed.append(path) and None
            reader._rescan()
            reader._rescan()
            self.assertEqual(len(probed), 2)

    def test_a_missing_input_directory_is_not_an_error(self):
        reader = GamepadReader(lambda action: None,
                               input_dir="/nonexistent/input")
        self.assertFalse(reader.start())
        self.assertEqual(reader.device_names, ())
        self.assertFalse(gamepad.available("/nonexistent/input"))


# What Steam puts in the environment of a shortcut it starts with Steam Input
# on, for a DualShock 4 it has taken over.
DS4 = (0x054C, 0x09CC)
STEAM_VIRTUAL_PAD = (0x28DE, 0x11FF)
STEAM_ENV = {
    "SDL_GAMECONTROLLER_IGNORE_DEVICES": "0x054c/0x05c4,0x054c/0x09cc",
    "SDL_GAMECONTROLLER_ALLOW_STEAM_VIRTUAL_GAMEPAD": "1",
}


class SteamInputFilterTests(unittest.TestCase):
    """#288: one press on a pad Steam Input drives must count once."""

    def test_nothing_is_skipped_without_steam(self):
        keep = gamepad.ControllerFilter({})
        self.assertFalse(keep.skips(DS4))
        self.assertFalse(keep.skips(STEAM_VIRTUAL_PAD))

    def test_the_pad_steam_took_over_is_skipped_and_its_virtual_pad_kept(self):
        steam = gamepad.ControllerFilter(STEAM_ENV)
        self.assertTrue(steam.skips(DS4))
        self.assertTrue(steam.skips((0x054C, 0x05C4)))
        self.assertFalse(steam.skips(STEAM_VIRTUAL_PAD))
        # A controller Steam did not list is somebody else's to use.
        self.assertFalse(steam.skips((0x045E, 0x0B12)))

    def test_an_unreadable_identity_is_never_skipped(self):
        self.assertFalse(gamepad.ControllerFilter(STEAM_ENV).skips(None))

    def test_an_allow_list_skips_everything_it_does_not_name(self):
        only = gamepad.ControllerFilter({
            "SDL_GAMECONTROLLER_IGNORE_DEVICES_EXCEPT": "0x28DE/0x11FF"})
        self.assertFalse(only.skips(STEAM_VIRTUAL_PAD))
        self.assertTrue(only.skips(DS4))

    def test_the_list_can_come_from_a_file(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write("0x054c/0x09cc\n")
        self.addCleanup(os.unlink, handle.name)
        from_file = gamepad.ControllerFilter({
            "SDL_GAMECONTROLLER_IGNORE_DEVICES": "@" + handle.name})
        self.assertTrue(from_file.skips(DS4))
        missing = gamepad.ControllerFilter({
            "SDL_GAMECONTROLLER_IGNORE_DEVICES": "@/nonexistent/list"})
        self.assertFalse(missing.skips(DS4))

    def test_a_malformed_list_skips_nothing(self):
        junk = gamepad.ControllerFilter({
            "SDL_GAMECONTROLLER_IGNORE_DEVICES": "054c:09cc, nonsense"})
        self.assertFalse(junk.skips(DS4))

    def _probe_as(self, usb_id, environ):
        import tempfile
        from unittest import mock
        tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        node = os.path.join(tmp, "event5")
        open(node, "w").close()
        reader = GamepadReader(lambda action: None, input_dir=tmp,
                               environ=environ)
        with mock.patch.object(gamepad, "looks_like_gamepad",
                               return_value=True), \
                mock.patch.object(gamepad, "device_usb_id",
                                  return_value=usb_id):
            found = reader._probe(node)
        if isinstance(found, _Device):
            self.addCleanup(found.close)
        return found

    def test_the_reader_listens_to_the_virtual_pad_only(self):
        self.assertIs(self._probe_as(DS4, STEAM_ENV), gamepad.NOT_A_GAMEPAD)
        self.assertIsInstance(self._probe_as(STEAM_VIRTUAL_PAD, STEAM_ENV),
                              _Device)
        # Outside Steam the physical pad is the only one there is.
        self.assertIsInstance(self._probe_as(DS4, {}), _Device)

    def test_the_controller_status_lists_what_the_reader_uses(self):
        import tempfile
        from unittest import mock
        ids = {"event3": DS4, "event7": STEAM_VIRTUAL_PAD}
        nodes = {}
        real_open = os.open

        def tracked_open(path, flags):
            fd = real_open(path, flags)
            nodes[fd] = os.path.basename(path)
            return fd

        with tempfile.TemporaryDirectory() as tmp:
            for name in ids:
                open(os.path.join(tmp, name), "w").close()
            with mock.patch.object(gamepad.os, "open",
                                   side_effect=tracked_open), \
                    mock.patch.object(gamepad, "looks_like_gamepad",
                                      return_value=True), \
                    mock.patch.object(gamepad, "device_name",
                                      side_effect=lambda fd: nodes[fd]), \
                    mock.patch.object(gamepad, "device_usb_id",
                                      side_effect=lambda fd: ids[nodes[fd]]):
                steam = gamepad.connected(tmp, "/nonexistent",
                                          environ=STEAM_ENV)
                plain = gamepad.connected(tmp, "/nonexistent", environ={})
        self.assertEqual(steam, (["event7"], 0))
        self.assertEqual(plain, (["event3", "event7"], 0))


if __name__ == "__main__":
    unittest.main()
