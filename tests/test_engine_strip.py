"""The engine is built stripped, and its prefix release is named for it.

Stripping rewrites each PE module with the time of the run, so the builders
put the link time back; a prefix built another way is another set of bytes
and must not take the name of the one already published.
"""
# SPDX-License-Identifier: MIT

import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAMP = ROOT / "scripts/pe-stamp.py"
TAG = ROOT / "scripts/winegdk-release-tag.sh"


def _pe(timestamp, checksum, magic=0x20B):
    dos = bytearray(0x80)
    dos[:2] = b"MZ"
    dos[0x40:0x50] = b"Wine builtin DLL"
    struct.pack_into("<I", dos, 0x3C, 0x80)
    coff = struct.pack("<4sHHIIIHH", b"PE\0\0", 0x8664, 1, timestamp,
                       0, 0, 240, 0x2022)
    optional = bytearray(240)
    struct.pack_into("<H", optional, 0, magic)
    struct.pack_into("<I", optional, 64, checksum)
    return bytes(dos) + coff + bytes(optional) + b"section data"


class StampTests(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run([sys.executable, str(STAMP), *map(str, args)],
                              capture_output=True, text=True)

    def test_two_strips_at_different_times_end_as_the_same_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / "x86_64-windows"
            folder.mkdir()
            (folder / "a.dll").write_bytes(_pe(1794596414, 0x1234))
            (folder / "b.dll").write_bytes(_pe(1794596416, 0x1236))
            (folder / "ntdll.so").write_bytes(b"\x7fELF" + bytes(200))
            done = self._run(1784308597, td)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("stamped 2 PE files", done.stdout)
            first = (folder / "a.dll").read_bytes()
            self.assertEqual(first, (folder / "b.dll").read_bytes())
            self.assertEqual(first, _pe(1784308597, 0))
            # The mark Wine knows its own modules by, and everything else.
            self.assertEqual(first[0x40:0x50], b"Wine builtin DLL")
            self.assertTrue(first.endswith(b"section data"))
            self.assertEqual((folder / "ntdll.so").read_bytes(),
                             b"\x7fELF" + bytes(200))

    def test_a_folder_without_any_module_is_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "readme.txt").write_text("MZ is not enough")
            self.assertNotEqual(self._run(1784308597, td).returncode, 0)


class RecipeTests(unittest.TestCase):
    def test_both_builders_install_stripped_and_restore_the_link_time(self):
        for name in ("build-winegdk-container.sh", "build-winegdk-bullseye.sh"):
            text = (ROOT / "scripts" / name).read_text()
            with self.subTest(script=name):
                self.assertIn("make install INSTALL_PROGRAM_FLAGS=-s", text)
                self.assertNotRegex(text, r"(?m)^\s*make install\s*\)?\s*$")
                self.assertIn("pe-stamp.py", text)

    def test_the_prefix_release_is_named_after_its_recipe(self):
        from bol import config
        tag = subprocess.run(["bash", str(TAG)], capture_output=True,
                             text=True, check=True).stdout.strip()
        self.assertEqual(
            tag, "winegdk-%s-%s-%s" % (
                config.WINEGDK_SOURCE_COMMIT[:12],
                config.WINEGDK_SOURCE_MANIFEST_SHA256[:12],
                config.WINEGDK_PREFIX_RECIPE))
        for workflow in ("build-winegdk.yml", "build-engine.yml",
                         "release.yml"):
            with self.subTest(workflow=workflow):
                text = (ROOT / ".github/workflows" / workflow).read_text()
                self.assertIn("scripts/winegdk-release-tag.sh", text)
                self.assertNotIn("winegdk-${short}-${manifest_short}", text)

    def test_the_packager_leaves_mono_out_and_checks_the_strip(self):
        text = (ROOT / "scripts/package-engine.sh").read_text()
        self.assertIn('rm -rf "$STAGED_ENGINE/files/share/wine/mono"', text)
        self.assertIn("Wine builtin DLL", text)
        self.assertIn(".debug_info", text)


if __name__ == "__main__":
    unittest.main()
