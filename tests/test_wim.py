"""bol.wim: Windows files read out of an ISO's install.wim (#387)."""
# SPDX-License-Identifier: MIT

import hashlib
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import wim
from tests import wimfixture


class _MemorySource:
    """A source over bytes, counting what is read like HttpFile does."""

    def __init__(self, data):
        self.data = data
        self.size = len(data)
        self.fetched = 0

    def read(self, offset, length):
        chunk = self.data[offset:offset + length]
        if len(chunk) != length:
            raise wim.WimError("past the end")
        self.fetched += length
        return chunk

    fetch = read


def _iso():
    return _MemorySource(wimfixture.iso_bytes())


class _BitWriter:
    """LZX's bitstream, written: 16-bit little-endian words, high bit
    first, with raw bytes allowed on a word boundary."""

    def __init__(self):
        self.out = bytearray()
        self.acc = 0
        self.count = 0

    def bits(self, value, count):
        for shift in reversed(range(count)):
            self.acc = (self.acc << 1) | ((value >> shift) & 1)
            self.count += 1
            if self.count == 16:
                self.out += struct.pack("<H", self.acc)
                self.acc = self.count = 0

    def align(self):
        if self.count:
            self.bits(0, 16 - self.count)

    def raw(self, data):
        assert self.count == 0
        self.out += data


def _uncompressed_block(writer, data, recent=(1, 1, 1)):
    writer.bits(3, 3)               # block type: uncompressed
    writer.bits(0, 1)               # not the default size
    writer.bits(len(data), 16)
    writer.align()
    writer.raw(struct.pack("<III", *recent))
    writer.raw(data)
    if len(data) & 1:
        writer.raw(b"\0")


class IsoAndWimTests(unittest.TestCase):
    def test_install_wim_is_found_through_udf_without_regard_to_case(self):
        source = _iso()
        extents = wim.udf_extents(source, "SOURCES/Install.WIM")
        self.assertEqual(len(extents), 1)
        offset, length = extents[0]
        self.assertEqual(source.data[offset:offset + 8], wim.WIM_MAGIC)
        # The directory walk reads a handful of sectors, not the image.
        self.assertLess(source.fetched, 64 << 10)

    def test_a_path_the_iso_does_not_have_is_said_so(self):
        with self.assertRaisesRegex(wim.WimError, "not in the ISO"):
            wim.udf_extents(_iso(), "sources/install.esd")

    def test_something_that_is_not_udf_is_refused(self):
        with self.assertRaisesRegex(wim.WimError, "not a UDF image"):
            wim.udf_extents(_MemorySource(bytes(600 * 1024)), "a")

    def test_the_image_lists_its_files_with_their_own_casing(self):
        image = wim.open_iso_wim(_iso())
        files = image.files()
        self.assertEqual(sorted(files), [
            "Program Files/WindowsApps/Microsoft.VCLibs.140.00_14.0.30704.0_"
            "x64__8wekyb3d8bbwe/vcruntime140_app.dll",
            "Windows/Fonts/Sample.txt",
            "Windows/Fonts/run.bin",
            "Windows/System32/Test.DLL",
            "Windows/empty.txt",
            "random.bin",
        ])
        self.assertEqual(files["Windows/System32/Test.DLL"].size, 40000)
        self.assertEqual(files["Windows/empty.txt"].size, 0)

    def test_an_image_that_is_not_a_wim_or_not_lzx_is_refused(self):
        with self.assertRaisesRegex(wim.WimError, "not a Windows image"):
            wim.open_bytes(bytes(208))
        source = _iso()
        offset, length = wim.udf_extents(source, "sources/install.wim")[0]
        header = bytearray(source.data[offset:offset + length])
        struct.pack_into("<I", header, 16, 0x00080002)   # LZMS
        with self.assertRaisesRegex(wim.WimError, "LZMS"):
            wim.open_bytes(bytes(header))

    def test_extents_join_a_file_kept_in_pieces(self):
        source = _MemorySource(b"0123456789abcdefghij")
        joined = wim.Extents(source, [(10, 4), (2, 3), (15, 5)])
        self.assertEqual(joined.size, 12)
        self.assertEqual(joined.read(0, 12), b"abcd234fghij")
        self.assertEqual(joined.read(3, 4), b"d234")
        with self.assertRaises(wim.WimError):
            joined.read(10, 3)


class HarvestTests(unittest.TestCase):
    LINES = [
        "windows/system32/test.dll",          # exact, matched in any case
        "Windows/Fonts/",                     # a whole directory
        "Program Files/WindowsApps/Microsoft.VCLibs.140.00_*_x64__"
        "8wekyb3d8bbwe/",                     # a pattern
        "Windows/empty.txt",
        "random.bin",
        "Windows/System32/missing.dll",
    ]

    def test_the_manifest_is_matched_as_harvest_py_matches_it(self):
        image = wim.open_iso_wim(_iso())
        found, missing = wim.match_manifest(image.files(), self.LINES)
        destinations = sorted(dest for dest, _entry in found)
        self.assertEqual(destinations, [
            "Program Files/WindowsApps/Microsoft.VCLibs.140.00_14.0.30704.0_"
            "x64__8wekyb3d8bbwe/vcruntime140_app.dll",
            "Windows/Fonts/Sample.txt",
            "Windows/Fonts/run.bin",
            "Windows/empty.txt",
            "random.bin",
            # An exact line keeps the manifest's own casing.
            "windows/system32/test.dll",
        ])
        self.assertEqual(missing, ["Windows/System32/missing.dll"])

    def test_every_file_is_written_and_checked_against_the_image(self):
        image = wim.open_iso_wim(_iso())
        files = image.files()
        with tempfile.TemporaryDirectory() as td:
            count, missing = wim.harvest(image, self.LINES, td)
            self.assertEqual(count, 6)
            out = Path(td)
            for dest, source in (
                    ("windows/system32/test.dll", "Windows/System32/Test.DLL"),
                    ("Windows/Fonts/Sample.txt", "Windows/Fonts/Sample.txt"),
                    ("Windows/Fonts/run.bin", "Windows/Fonts/run.bin"),
                    ("random.bin", "random.bin")):
                data = (out / dest).read_bytes()
                self.assertEqual(len(data), files[source].size)
                self.assertEqual(hashlib.sha1(data).digest(),
                                 files[source].sha1)
            self.assertEqual((out / "Windows/empty.txt").read_bytes(), b"")
            self.assertEqual((out / "harvest-missing.txt").read_text(),
                             "Windows/System32/missing.dll\n")
            # Long matches, overlapping ones, E8 calls: all came back whole.
            self.assertEqual((out / "Windows/Fonts/run.bin").read_bytes(),
                             b"ab" * 20000 + b"a" * 9000)
            self.assertFalse(list(out.rglob(".*.part")))

    def test_a_harvest_cut_short_goes_on_where_it_stopped(self):
        image = wim.open_iso_wim(_iso())
        with tempfile.TemporaryDirectory() as td:
            wim.harvest(image, self.LINES, td)
            broken = Path(td) / "Windows/Fonts/Sample.txt"
            broken.write_bytes(b"cut short")
            with mock.patch.object(image, "extract",
                                   wraps=image.extract) as extract:
                wim.harvest(image, self.LINES, td)
            targets = extract.call_args[0][0]
            self.assertEqual([[p.name for p in paths]
                              for paths in targets.values()],
                             [["Sample.txt"]])
            self.assertEqual(len(broken.read_bytes()), 68820)

    def test_a_corrupt_resource_writes_nothing(self):
        source = _iso()
        image = wim.open_iso_wim(source)
        entry = image.files()["Windows/System32/Test.DLL"]
        res = image.blobs[entry.sha1]
        base = wim.udf_extents(source, "sources/install.wim")[0][0]
        data = bytearray(source.data)
        data[base + res.offset + res.stored - 20] ^= 0x55
        image.source = wim.Extents(_MemorySource(bytes(data)),
                                   [(base, image.source.size)])
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(wim.WimError):
                wim.harvest(image, ["Windows/System32/Test.DLL"], td)
            self.assertFalse((Path(td) / "Windows/System32/Test.DLL")
                             .exists())

    def test_worker_processes_give_the_same_files(self):
        pool = wim.decompression_pool(2)
        self.addCleanup(pool.shutdown)
        image = wim.open_iso_wim(_iso(), pool=pool)
        with tempfile.TemporaryDirectory() as td:
            count, _missing = wim.harvest(image, self.LINES, td)
            self.assertEqual(count, 6)
            self.assertEqual(
                (Path(td) / "Windows/Fonts/run.bin").read_bytes(),
                b"ab" * 20000 + b"a" * 9000)


class LzxTests(unittest.TestCase):
    def test_uncompressed_blocks_with_their_padding_and_e8_calls(self):
        first = (b"\x90" * 5 + b"\xe8" + (100).to_bytes(4, "little")
                 + b"\x90" * 4 + b"\xe8" + (-3).to_bytes(4, "little",
                                                          signed=True)
                 + b"xyz")                                  # 23 bytes, odd
        second = b"tail " * 4 + b"\xe8" + b"1234"           # within the last 10
        writer = _BitWriter()
        _uncompressed_block(writer, first, recent=(7, 8, 9))
        _uncompressed_block(writer, second)
        out = wim.lzx_decompress(bytes(writer.out), len(first + second))
        expected = bytearray(first + second)
        # A target inside the file becomes a displacement from the call...
        expected[6:10] = (100 - 5).to_bytes(4, "little", signed=True)
        # ...and one before its start was moved by the file size.
        expected[15:19] = (-3 + 12000000).to_bytes(4, "little", signed=True)
        self.assertEqual(out, bytes(expected))

    def test_an_uncompressed_block_with_zero_recent_offsets_is_corrupt(self):
        writer = _BitWriter()
        _uncompressed_block(writer, b"abcd", recent=(0, 1, 1))
        with self.assertRaises(wim.WimError):
            wim.lzx_decompress(bytes(writer.out), 4)

    def test_garbage_is_an_error_not_a_crash(self):
        for seed in range(20):
            data = hashlib.sha256(bytes([seed])).digest() * 8
            try:
                wim.lzx_decompress(data, 32768)
            except wim.WimError:
                pass

    def test_an_overfull_huffman_code_is_refused(self):
        with self.assertRaises(wim.WimError):
            wim._decode_table([1, 1, 1])
        table, bits = wim._decode_table([2, 1, 3, 3])
        self.assertEqual(bits, 3)
        # Canonical: the shortest code first, by symbol within a length.
        self.assertEqual(table[0] >> 5, 1)
        self.assertEqual(table[4] >> 5, 0)
        self.assertEqual(table[6] >> 5, 2)
        self.assertEqual(table[7] >> 5, 3)


if __name__ == "__main__":
    unittest.main()
