"""The Xbox Live DLLs from a third party's release are pinned.

libHttpClient.GDK.dll and XCurl.dll come from a release tagged v0.0.0 in
someone else's repository -- a tag that can be uploaded to again -- and they
run inside the game. Only the reviewed bytes go in.
"""
# SPDX-License-Identifier: MIT

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import fixups
from bol.config import GDK_DEPS_DLLS, GDK_DEPS_SHA256
from bol.log import BolError

_GOOD = {name: f"reviewed {name}".encode() for name in GDK_DEPS_DLLS}
_PINS = {name: hashlib.sha256(data).hexdigest() for name, data in _GOOD.items()}


class PinnedDllTests(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.cache = Path(holder.name) / "cache"
        self.cache.mkdir()
        self.game = Path(holder.name) / "game"
        self.game.mkdir()
        for target in ("_install_openssl_xcurl", "_patch_lhc_xcurl_gate",
                       "_patch_hbui_signin_gate"):
            patcher = mock.patch.object(fixups, target)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _install(self, served):
        def fetch(url, dest, label=None, progress=None):
            dest.write_bytes(served[url.rsplit("/", 1)[1]])

        with mock.patch.object(fixups, "CACHE", self.cache), \
                mock.patch.object(fixups, "GDK_DEPS_SHA256", _PINS), \
                mock.patch.object(fixups, "download",
                                  side_effect=fetch) as fetched:
            fixups.install_gdk_xbox_dlls(self.game)
        return fetched

    def test_the_reviewed_dlls_are_installed(self):
        self._install(_GOOD)
        for name, data in _GOOD.items():
            self.assertEqual((self.game / name).read_bytes(), data)

    def test_a_cached_copy_that_changed_is_fetched_again(self):
        (self.cache / "gdkdeps-XCurl.dll").write_bytes(b"tampered")
        (self.cache / "gdkdeps-libHttpClient.GDK.dll").write_bytes(
            _GOOD["libHttpClient.GDK.dll"])
        fetched = self._install(_GOOD)
        self.assertEqual([c.args[0].rsplit("/", 1)[1]
                          for c in fetched.call_args_list], ["XCurl.dll"])
        self.assertEqual((self.game / "XCurl.dll").read_bytes(),
                         _GOOD["XCurl.dll"])

    def test_a_release_that_was_uploaded_to_again_is_refused(self):
        served = dict(_GOOD, **{"libHttpClient.GDK.dll": b"something else"})
        with self.assertRaises(BolError):
            self._install(served)
        self.assertFalse((self.game / "libHttpClient.GDK.dll").exists())
        self.assertFalse(
            (self.cache / "gdkdeps-libHttpClient.GDK.dll").exists())

    def test_every_dll_has_a_pin(self):
        self.assertEqual(set(GDK_DEPS_SHA256), set(GDK_DEPS_DLLS))
        for digest in GDK_DEPS_SHA256.values():
            self.assertRegex(digest, r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
