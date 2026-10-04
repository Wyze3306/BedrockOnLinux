"""The GameInput redist a prefix holds follows the game's (issue #300)."""
# SPDX-License-Identifier: MIT

import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import gameinput


def _image(version=None, filler=4096):
    """Bytes shaped enough like a PE for these checks: an optional
    VS_FIXEDFILEINFO carrying ``version``."""
    data = bytearray(b"MZ" + b"\x00" * filler)
    if version is not None:
        major, minor, build, revision = version
        data += struct.pack("<IIII", 0xFEEF04BD, 0x00010000,
                            (major << 16) | minor, (build << 16) | revision)
        data += b"\x00" * 64
    return bytes(data)


class FileVersionTests(unittest.TestCase):
    def test_the_fixed_file_info_is_read(self):
        self.assertEqual(gameinput._pe_file_version(_image((2, 2, 26100, 6106))),
                         (2, 2, 26100, 6106))

    def test_an_image_without_a_version_resource_has_none(self):
        self.assertIsNone(gameinput._pe_file_version(_image()))
        self.assertIsNone(gameinput._pe_file_version(b""))


class RedistRefreshTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.prefix = root / "pfx"
        (self.prefix / "drive_c/windows/system32").mkdir(parents=True)
        for hive in ("system.reg", "user.reg"):
            (self.prefix / hive).write_bytes(b"WINE REGISTRY Version 2\n")
        self.redist = (self.prefix /
                       "drive_c/Program Files/Microsoft GameInput/x64")
        self.redist.mkdir(parents=True)
        self.game = root / "game"
        (self.game / "Installers").mkdir(parents=True)
        self.msi = self.game / "Installers" / "GameInputRedist.msi"
        self.msi.write_bytes(b"msi")

    def _installed(self, version):
        (self.redist / "GameInputRedist.dll").write_bytes(_image(version))
        (self.redist / "GameInputRedistService.exe").write_bytes(b"MZ")

    def _install(self, bundled):
        with mock.patch.object(gameinput, "require_prefix_idle"), \
                mock.patch.object(gameinput, "_bundled_redist_version",
                                  return_value=bundled), \
                mock.patch.object(gameinput, "_set_gameinput_registry",
                                  return_value=True) as registry, \
                mock.patch.object(gameinput, "_extract_gameinput_redist",
                                  return_value=True) as extract:
            gameinput.install_gameinput(self.prefix, self.game)
        registry.assert_called()
        return extract

    def test_an_older_redist_is_replaced_by_the_games(self):
        self._installed((1, 0, 22621, 2))
        extract = self._install(bundled=(2, 2, 26100, 6106))
        extract.assert_called_once_with(self.msi, self.prefix)

    def test_the_same_redist_is_left_in_place(self):
        self._installed((2, 2, 26100, 6106))
        self._install(bundled=(2, 2, 26100, 6106)).assert_not_called()

    def test_a_newer_redist_than_the_games_is_kept(self):
        # GameInput keeps older callers working; an older build of the game
        # must not take a newer redist away from the prefix.
        self._installed((3, 0, 26100, 7000))
        self._install(bundled=(2, 2, 26100, 6106)).assert_not_called()

    def test_an_unversioned_install_is_replaced(self):
        self._installed(None)
        self._install(bundled=(2, 2, 26100, 6106)).assert_called_once()

    def test_a_package_without_a_readable_redist_changes_nothing(self):
        self._installed((1, 0, 22621, 2))
        self._install(bundled=None).assert_not_called()

    def test_an_unreadable_msi_is_not_taken_for_a_newer_one(self):
        self._installed((1, 0, 22621, 2))
        self.msi.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\xff" * 64)
        self.assertFalse(gameinput._redist_outdated(self.prefix, self.msi))

    def test_a_package_without_an_msi_keeps_the_installed_redist(self):
        self._installed((1, 0, 22621, 2))
        self.msi.unlink()
        self.assertFalse(gameinput._redist_outdated(self.prefix, self.msi))


if __name__ == "__main__":
    unittest.main()
