"""Turning MSAL's "AAD to WAM" flight off in Minecraft Education (#351).

With the flight on, Education sends a school account to Windows' Web Account
Manager, which Wine does not have, and sign-in stops on "Something went
wrong [4nsvw]" after the e-mail address.
"""
# SPDX-License-Identifier: MIT

import os
import struct
import tempfile
import unittest
from unittest import mock

from bol import msal_flights as mf

BASE = 0x140000000
BUILD = mf.KNOWN_BUILDS[0]
STATE = 0x7000_0000
FLIGHTS = 0x7000_1000
SENTINEL = 0x7000_2000
NODES = 0x7000_3000


class FakeMemory:
    """Sparse process memory: reads of unmapped bytes fail like /proc/pid/mem."""

    def __init__(self):
        self.bytes = {}
        self.closed = False

    def put(self, address, data):
        for i, b in enumerate(data):
            self.bytes[address + i] = b

    def read(self, address, size):
        try:
            return bytes(self.bytes[address + i] for i in range(size))
        except KeyError:
            raise OSError("unmapped") from None

    def qword(self, address):
        return struct.unpack("<Q", self.read(address, 8))[0]

    def write(self, address, data):
        self.put(address, data)

    def close(self):
        self.closed = True


def pe_headers(mem, size=0x10000000):
    mem.put(BASE + 0x3C, struct.pack("<I", 0x80))
    mem.put(BASE + 0x80, b"PE\0\0")
    mem.put(BASE + 0x80 + 0x50, struct.pack("<I", size))


def game_image(mem, build=BUILD, defaults=None):
    pe_headers(mem)
    lea_at = BASE + build["getter"]
    disp = build["global"] - (build["getter"] + 7)
    mem.put(lea_at, b"\x48\x8d\x05" + struct.pack("<i", disp))
    mem.put(BASE + build["defaults"],
            defaults if defaults is not None else build["defaults_bytes"])
    mem.put(BASE + build["global"], struct.pack("<Q", 0))


def start_msal(mem, flights):
    """Lay out MSAL's flight map as MSVC does: a circular list of nodes."""
    mem.put(BASE + BUILD["global"], struct.pack("<Q", STATE))
    mem.put(STATE + mf.FLIGHTS_OFFSET, struct.pack("<Q", FLIGHTS))
    mem.put(FLIGHTS + mf.LIST_OFFSET, struct.pack("<Q", SENTINEL))
    addresses = [NODES + 0x20 * i for i in range(len(flights))]
    chain = addresses + [SENTINEL]
    mem.put(SENTINEL, struct.pack("<Q", chain[0]))
    for i, (key, value) in enumerate(flights):
        node = addresses[i]
        mem.put(node, struct.pack("<Q", chain[i + 1]))
        mem.put(node + mf.NODE_KEY, struct.pack("<IB", key, value))
    return dict(zip((k for k, _ in flights), addresses))


DEFAULTS = [(0x66, 0), (0x86, 0), (0x6D, 1), (0x65, 0), (0x85, 0),
            (0x74, 0), (0x7F, 0), (0x70, 0), (0x83, 1)]


class ImageBase(unittest.TestCase):

    def test_offset_zero_mapping_of_the_executable_inode(self):
        maps = (
            "7f00-7f10 r--p 00000000 00:35 999 /lib/other.so\n"
            "140001000-14c633000 r-xp 00001000 00:35 4242 /g/Minecraft.Windows.exe\n"
            "140000000-140001000 r--p 00000000 00:35 4242 /g/Minecraft.Windows.exe\n"
        )
        self.assertEqual(mf.parse_image_base(maps, 4242), 0x140000000)
        self.assertIsNone(mf.parse_image_base(maps, 1))

    def test_find_game_matches_the_inode_not_the_name(self):
        with tempfile.TemporaryDirectory() as d:
            exe = os.path.join(d, "Minecraft.Windows.exe")
            open(exe, "wb").close()
            inode = os.stat(exe).st_ino
            bases = {10: None, 11: BASE}
            with mock.patch.object(
                    mf, "_image_base",
                    side_effect=lambda pid, ino: bases[pid]
                    if ino == inode else None):
                self.assertEqual(mf.find_game(exe, [10, 11]), (11, BASE))
            self.assertIsNone(mf.find_game(exe + ".missing", [11]))


class Build(unittest.TestCase):

    def test_known_build_is_recognised_from_its_bytes(self):
        mem = FakeMemory()
        game_image(mem)
        self.assertIs(mf.match_build(mem, BASE), BUILD)

    def test_an_image_still_being_mapped_is_not_judged(self):
        mem = FakeMemory()
        pe_headers(mem)
        with self.assertRaises(OSError):
            mf.match_build(mem, BASE)

    def test_offsets_past_the_image_are_not_this_build(self):
        mem = FakeMemory()
        pe_headers(mem, size=0x1000)
        self.assertIsNone(mf.match_build(mem, BASE))

    def test_a_zero_filled_image_is_not_loaded_yet(self):
        """Seen in the real client: zeros for a moment, then the file."""
        mem = FakeMemory()
        game_image(mem, defaults=b"\0" * 16)
        with self.assertRaises(OSError):
            mf.match_build(mem, BASE)

    def test_other_bytes_are_not_written_to(self):
        mem = FakeMemory()
        game_image(mem, defaults=struct.pack("<4I", 1, 2, 3, 4))
        self.assertIsNone(mf.match_build(mem, BASE))


class TurnOff(unittest.TestCase):

    def test_waits_for_msal_to_start(self):
        mem = FakeMemory()
        game_image(mem)
        self.assertIsNone(mf.turn_off(mem, BASE, BUILD))

    def test_turns_only_the_wam_flight_off(self):
        mem = FakeMemory()
        game_image(mem)
        nodes = start_msal(mem, DEFAULTS)
        self.assertTrue(mf.turn_off(mem, BASE, BUILD))
        flights = mf.read_flights(mem, BASE, BUILD)
        self.assertEqual(flights[0x66][1], mf.FLIGHT_OFF)
        self.assertEqual(flights[0x65][1], 0)
        self.assertEqual({k: v[1] for k, v in flights.items() if k != 0x66},
                         {k: v for k, v in DEFAULTS if k != 0x66})
        self.assertEqual(flights[0x66][0], nodes[0x66])

    def test_already_off_is_left_as_is(self):
        mem = FakeMemory()
        game_image(mem)
        start_msal(mem, [(0x66, 1)])
        with mock.patch.object(mem, "write") as write:
            self.assertTrue(mf.turn_off(mem, BASE, BUILD))
        write.assert_not_called()

    def test_a_map_without_the_flight_is_not_ready(self):
        mem = FakeMemory()
        game_image(mem)
        start_msal(mem, [(0x65, 0)])
        self.assertIsNone(mf.turn_off(mem, BASE, BUILD))


class Watch(unittest.TestCase):

    def run_watch(self, mem, found=(1234, BASE), alive=lambda: True,
                  on_poll=None, deadline=5.0):
        now = [0.0]
        polls = []

        def sleep(s):
            now[0] += s
            polls.append(now[0])
            if on_poll:
                on_poll(len(polls))

        with mock.patch.object(mf, "find_game", return_value=found), \
                mock.patch.object(mf, "_Memory", return_value=mem), \
                mock.patch.object(mf, "warn") as warn, \
                mock.patch.object(mf, "info") as info:
            outcome = mf.watch("/g/Minecraft.Windows.exe", lambda: [1234],
                               alive, deadline_s=deadline, poll_s=0.25,
                               clock=lambda: now[0], sleep=sleep)
        return outcome, warn, info

    def test_turns_the_flight_off_once_msal_is_up(self):
        mem = FakeMemory()
        game_image(mem)
        outcome, warn, info = self.run_watch(
            mem, on_poll=lambda n: n == 3 and start_msal(mem, DEFAULTS))
        self.assertEqual(outcome, "off")
        self.assertEqual(mf.read_flights(mem, BASE, BUILD)[0x66][1], 1)
        warn.assert_not_called()
        info.assert_called_once()
        self.assertTrue(mem.closed)

    def test_sections_mapped_after_the_headers_are_waited_for(self):
        """Wine maps the headers first: an early read must not condemn the
        build, which is what the real client showed at seven seconds."""
        mem = FakeMemory()
        pe_headers(mem)

        def poll(n):
            if n == 1:
                game_image(mem, defaults=b"\0" * 16)
            if n == 2:
                game_image(mem)
            if n == 4:
                start_msal(mem, DEFAULTS)

        outcome, warn, _info = self.run_watch(mem, on_poll=poll)
        self.assertEqual(outcome, "off")
        warn.assert_not_called()

    def test_unknown_build_is_reported_and_untouched(self):
        mem = FakeMemory()
        game_image(mem, defaults=b"\1" * 16)
        outcome, warn, _info = self.run_watch(mem)
        self.assertEqual(outcome, "unknown-build")
        self.assertIn("4nsvw", warn.call_args[0][0])

    def test_game_not_found_is_reported(self):
        outcome, warn, _info = self.run_watch(FakeMemory(), found=None)
        self.assertEqual(outcome, "not-found")
        warn.assert_called_once()

    def test_a_closed_game_ends_the_watch_quietly(self):
        mem = FakeMemory()
        game_image(mem)
        state = {"alive": True}
        outcome, warn, _info = self.run_watch(
            mem, alive=lambda: state["alive"],
            on_poll=lambda n: n == 2 and state.update(alive=False))
        self.assertEqual(outcome, "gone")
        warn.assert_not_called()

    def test_msal_that_never_starts_is_reported(self):
        mem = FakeMemory()
        game_image(mem)
        outcome, warn, _info = self.run_watch(mem, deadline=1.0)
        self.assertEqual(outcome, "timeout")
        warn.assert_called_once()

    def test_unreadable_memory_is_reported(self):
        with mock.patch.object(mf, "find_game", return_value=(1, BASE)), \
                mock.patch.object(mf, "_Memory",
                                  side_effect=PermissionError(1, "denied")), \
                mock.patch.object(mf, "warn") as warn:
            outcome = mf.watch("/x", lambda: [1], lambda: True,
                               deadline_s=1, clock=lambda: 0.0,
                               sleep=lambda _s: None)
        self.assertEqual(outcome, "unreachable")
        self.assertIn("denied", warn.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
