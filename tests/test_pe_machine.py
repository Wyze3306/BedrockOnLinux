"""The x86-64 byte patches must never land in an ARM64EC or ARM64 Wine (#250)."""

import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import proton
from bol.pe import code_machine

AMD64, ARM64, I386 = 0x8664, 0xAA64, 0x14C


def _pe(machine=AMD64, chpe=None, load_config_size=0x140):
    """A minimal PE32+ image with one section. chpe, when given, is written
    as the load configuration's CHPEMetadataPointer, which is what tells an
    ARM64EC image from an x86-64 one with the same header machine."""
    d = bytearray(0x400)
    d[0:2] = b"MZ"
    struct.pack_into("<I", d, 0x3C, 0x80)
    d[0x80:0x84] = b"PE\0\0"
    coff = 0x84
    struct.pack_into("<HH", d, coff, machine, 1)
    struct.pack_into("<H", d, coff + 16, 0xF0)
    opt = coff + 20
    struct.pack_into("<H", d, opt, 0x20B)
    struct.pack_into("<I", d, opt + 108, 16)
    if chpe is not None:
        struct.pack_into("<II", d, opt + 112 + 8 * 10, 0x1000, load_config_size)
    section = opt + 0xF0
    struct.pack_into("<IIII", d, section + 8, 0x200, 0x1000, 0x200, 0x200)
    if chpe is not None:
        struct.pack_into("<I", d, 0x200, load_config_size)
        if load_config_size >= 0xD0:
            struct.pack_into("<Q", d, 0x200 + 0xC8, chpe)
    return bytes(d)


class CodeMachineTests(unittest.TestCase):
    def _machine(self, data):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "x.dll"
            path.write_bytes(data)
            return code_machine(path)

    def test_plain_x86_64(self):
        self.assertEqual(self._machine(_pe()), "x86_64")

    def test_x86_64_with_a_load_config_but_no_hybrid_metadata(self):
        self.assertEqual(self._machine(_pe(chpe=0)), "x86_64")

    def test_old_short_load_config_is_x86_64(self):
        self.assertEqual(
            self._machine(_pe(chpe=0x180001000, load_config_size=0x70)),
            "x86_64")

    def test_arm64ec_says_amd64_but_carries_chpe_metadata(self):
        self.assertEqual(self._machine(_pe(chpe=0x180003000)), "arm64ec")

    def test_arm64_and_arm64x(self):
        self.assertEqual(self._machine(_pe(machine=ARM64)), "arm64")

    def test_other_machine(self):
        self.assertEqual(self._machine(_pe(machine=0x1C4)), "other")

    def test_not_a_pe32plus(self):
        self.assertIsNone(self._machine(b"fixture"))
        pe32 = bytearray(_pe(machine=I386))
        struct.pack_into("<H", pe32, 0x84 + 20, 0x10B)
        self.assertIsNone(self._machine(bytes(pe32)))

    def test_missing_file(self):
        self.assertIsNone(code_machine(Path("/nonexistent/x.dll")))


class PatchProtonArchitectureTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name) / "engine"
        self.engine_out = mock.patch.object(
            proton, "WINEGDK_OUT", Path(self._td.name) / "different-engine")
        self.engine_out.start()

    def tearDown(self):
        self.engine_out.stop()
        self._td.cleanup()

    def _dlls(self, arch_dir, combase, ntdll):
        wine = self.root / "files/lib/wine" / arch_dir
        wine.mkdir(parents=True)
        (wine / "combase.dll").write_bytes(combase)
        (wine / "ntdll.dll").write_bytes(ntdll)
        return wine

    def test_arm64ec_dlls_are_left_untouched_even_when_strict(self):
        arm64ec = _pe(chpe=0x180003000)
        wine = self._dlls("x86_64-windows", arm64ec, arm64ec)
        with mock.patch.object(proton, "apply_patch") as apply, \
                mock.patch.object(proton, "die") as die, \
                mock.patch.object(proton, "warn") as warn:
            proton.patch_proton(self.root, strict=True)

        apply.assert_not_called()
        die.assert_not_called()
        self.assertIn("arm64ec", str(warn.call_args.args[0]))
        self.assertEqual((wine / "combase.dll").read_bytes(), arm64ec)
        self.assertEqual((wine / "ntdll.dll").read_bytes(), arm64ec)
        self.assertFalse(list(wine.glob("*.bol-orig")))

    def test_arm64_ntdll_alone_is_enough_to_stop(self):
        self._dlls("x86_64-windows", _pe(), _pe(machine=ARM64))
        with mock.patch.object(proton, "apply_patch") as apply, \
                mock.patch.object(proton, "die") as die, \
                mock.patch.object(proton, "warn"):
            proton.patch_proton(self.root, strict=True)

        apply.assert_not_called()
        die.assert_not_called()

    def test_arm64_only_proton_is_skipped_not_refused(self):
        self._dlls("aarch64-windows", _pe(machine=ARM64), _pe(machine=ARM64))
        with mock.patch.object(proton, "die") as die, \
                mock.patch.object(proton, "warn") as warn:
            proton.patch_proton(self.root, strict=True)

        die.assert_not_called()
        self.assertIn("ARM64", str(warn.call_args.args[0]))

    def test_proton_without_any_wine_dlls_is_still_refused(self):
        (self.root / "files/lib/wine").mkdir(parents=True)
        with mock.patch.object(proton, "die") as die:
            proton.patch_proton(self.root, strict=True)

        self.assertIn("Wine DLLs missing", str(die.call_args.args[0]))

    def test_x86_64_dlls_still_reach_the_patches(self):
        self._dlls("x86_64-windows", _pe(), _pe())
        with mock.patch.object(proton, "warn") as warn:
            proton.patch_proton(self.root, strict=False)

        # The fixture exports nothing, so the first patch's own lookup is
        # what stops it: the architecture check let it through.
        self.assertIn("RoOriginateErrorW", str(warn.call_args.args[0]))


if __name__ == "__main__":
    unittest.main()
