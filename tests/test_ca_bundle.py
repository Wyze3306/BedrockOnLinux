"""The CA bundle the game is given, and what stands in for it (#374)."""
# SPDX-License-Identifier: MIT

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import fixups

PEM = b"-----BEGIN CERTIFICATE-----\n" + b"A" * 60_000 + b"\n"


class CaBundleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.system = self.root / "system.pem"
        self.system.write_bytes(PEM)
        patch = mock.patch.object(fixups, "CACHE", self.cache)
        patch.start()
        self.addCleanup(patch.stop)

    def _download(self, url, dest, label=None, **_kw):
        dest.write_bytes(b"-----BEGIN CERTIFICATE-----\ndownloaded\n")

    def test_the_downloaded_bundle_is_cached_and_used(self):
        with mock.patch.object(fixups, "download", self._download) as _:
            self.assertEqual(fixups._ca_bundle(), self.cache / "cacert.pem")
        with mock.patch.object(fixups, "download",
                               side_effect=AssertionError("cached")):
            self.assertEqual(fixups._ca_bundle(), self.cache / "cacert.pem")

    def test_a_failed_download_falls_back_to_the_system_bundle(self):
        game = self.root / "games" / "release" / "1.0"
        game.mkdir(parents=True)
        with mock.patch.object(fixups, "download",
                               side_effect=OSError("[Errno 104]")), \
                mock.patch.object(fixups, "_system_ca_bundle",
                                  return_value=self.system), \
                mock.patch.object(fixups, "warn") as warned:
            fixups.install_ca_bundle(game)
        installed = game / "etc" / "ssl" / "certs" / "ca-bundle.crt"
        self.assertEqual(installed.read_bytes(), PEM)
        warned.assert_called_once()
        # Not cached: the real bundle is asked for again next launch.
        self.assertFalse((self.cache / "cacert.pem").exists())

    def test_with_no_bundle_anywhere_the_failure_is_the_download_s(self):
        with mock.patch.object(fixups, "download",
                               side_effect=OSError("[Errno 104]")), \
                mock.patch.object(fixups, "_system_ca_bundle",
                                  return_value=None):
            with self.assertRaises(OSError):
                fixups._ca_bundle()

    def test_a_stub_file_is_not_taken_for_a_bundle(self):
        stub = self.root / "stub.pem"
        stub.write_bytes(b"-----BEGIN CERTIFICATE-----\nshort\n")
        with mock.patch.object(fixups, "_SYSTEM_CA_BUNDLES", (str(stub),)), \
                mock.patch("ssl.get_default_verify_paths",
                           side_effect=RuntimeError), \
                mock.patch.dict("sys.modules", {"certifi": None}):
            self.assertIsNone(fixups._system_ca_bundle())
        with mock.patch.object(fixups, "_SYSTEM_CA_BUNDLES",
                               (str(stub), str(self.system))), \
                mock.patch("ssl.get_default_verify_paths",
                           side_effect=RuntimeError), \
                mock.patch.dict("sys.modules", {"certifi": None}):
            self.assertEqual(fixups._system_ca_bundle(), self.system)


if __name__ == "__main__":
    unittest.main()
