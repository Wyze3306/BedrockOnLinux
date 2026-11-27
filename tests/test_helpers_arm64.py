"""xodus-cli and its WebKitGTK runtime as aarch64 builds (#250).

Everything here runs on a fake aarch64 computer: bol.hostarch.machine() says
"aarch64", and the pins are the ones each test sets.
"""
# SPDX-License-Identifier: MIT

import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import config, hostarch, webview, xodus
from tests.test_webview import _bundle_archive, _library
from tests.test_xodus import _cli_archive

ARM_EXEC_DIR = config.XODUS_WEBVIEW_ARM64_EXEC_DIR
X86_EXEC_DIR = config.XODUS_WEBVIEW_EXEC_DIR


@contextlib.contextmanager
def host(machine="aarch64", xodus_pin="ab" * 32, webview_pin="cd" * 32):
    """A computer of ``machine`` whose launcher carries these aarch64 pins."""
    with mock.patch.object(hostarch, "machine", return_value=machine), \
            mock.patch.object(config, "XODUS_ARM64_ARCHIVE_SHA256", xodus_pin), \
            mock.patch.object(xodus, "XODUS_ARM64_ARCHIVE_SHA256", xodus_pin), \
            mock.patch.object(webview, "XODUS_WEBVIEW_ARM64_SHA256",
                              webview_pin):
        yield


def _no_feed(*_args, **_kwargs):
    raise AssertionError("the data feed names x86-64 builds only")


class NativeHelpersTests(unittest.TestCase):
    def test_x86_64_computers_keep_the_x86_64_helpers(self):
        with host(machine="x86_64"):
            self.assertFalse(hostarch.native_helpers())
            self.assertFalse(hostarch.helpers_need_emulator())

    def test_arm_without_the_pin_runs_them_emulated(self):
        with host(xodus_pin=""):
            self.assertFalse(hostarch.native_helpers())
            self.assertTrue(hostarch.helpers_need_emulator())
            self.assertIn("xodus-cli", hostarch.x86_64_programs())

    def test_arm_with_the_pin_runs_them_natively(self):
        with host():
            self.assertTrue(hostarch.native_helpers())
            self.assertFalse(hostarch.helpers_need_emulator())
            self.assertNotIn("xodus-cli", hostarch.x86_64_programs())
            self.assertNotIn("the WebKitGTK sign-in window",
                             hostarch.x86_64_programs())

    def test_the_arm_engine_with_native_helpers_needs_no_emulator(self):
        with host(), \
                mock.patch.object(hostarch, "engine_needs_emulator",
                                  return_value=False):
            self.assertEqual(hostarch.x86_64_programs(), [])
            self.assertIsNone(hostarch.problem(binfmt=Path("/nonexistent")))


class ArmCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.target = self.base / "xodus"
        self.sibling = self.base / "sibling"

    def _patches(self, pin):
        stack = contextlib.ExitStack()
        stack.enter_context(host(xodus_pin=pin))
        for name, value in (("XODUS_REV", "testrev"),
                            ("XODUS_ARCHIVE_SHA256", "00" * 32),
                            ("XODUS_DIR", self.target),
                            ("XODUS_BIN", self.target / "xodus-cli"),
                            ("CACHE", self.base / "cache")):
            stack.enter_context(mock.patch.object(xodus, name, value))
        stack.enter_context(mock.patch.object(
            xodus.sys, "argv", [str(self.sibling / "launcher")]))
        from bol import feed
        stack.enter_context(mock.patch.object(feed, "resolve", _no_feed))
        return stack

    def test_installs_the_aarch64_asset_without_the_feed(self):
        digest = _cli_archive(self.sibling / "xodus-cli-testrev-aarch64.tar.gz")
        with self._patches(digest):
            binary = xodus.ensure_cli()
            self.assertEqual(xodus.effective_rev(), "testrev")
        self.assertTrue(binary.is_file())
        self.assertEqual((self.target / ".rev").read_text().strip(),
                         "testrev-aarch64")

    def test_an_x86_64_build_installed_earlier_is_replaced(self):
        digest = _cli_archive(self.sibling / "xodus-cli-testrev-aarch64.tar.gz",
                              body=b"#!/bin/sh\necho arm\n")
        self.target.mkdir()
        (self.target / "xodus-cli").write_bytes(b"#!/bin/sh\necho x86\n")
        (self.target / "xodus-cli").chmod(0o755)
        (self.target / ".rev").write_text("testrev\n")
        with self._patches(digest):
            xodus.ensure_cli()
        self.assertIn(b"arm", (self.target / "xodus-cli").read_bytes())

    def test_downloads_from_the_aarch64_tag(self):
        digest = _cli_archive(self.base / "built.tar.gz")
        payload = (self.base / "built.tar.gz").read_bytes()
        urls = []

        def fake_download(url, dest, *_args, **_kwargs):
            urls.append(url)
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_bytes(payload)

        with self._patches(digest), \
                mock.patch.object(xodus, "download", fake_download), \
                mock.patch.object(xodus, "gh_releases", _no_feed):
            xodus.ensure_cli()
        self.assertEqual(urls, [
            f"https://github.com/{config.WINEGDK_PREBUILT_REPO}/releases/"
            "download/xodus-testrev-aarch64/xodus-cli-testrev-aarch64.tar.gz"])

    def test_without_the_pin_the_x86_64_build_is_used(self):
        digest = _cli_archive(self.sibling / "xodus-cli-testrev.tar.gz")
        with self._patches(""), \
                mock.patch.object(xodus, "XODUS_ARCHIVE_SHA256", digest), \
                mock.patch("bol.feed.resolve",
                           lambda _n, rev, sha: (rev, sha, None)):
            xodus.ensure_cli()
        self.assertEqual((self.target / ".rev").read_text().strip(), "testrev")


class ArmWebviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.dest = self.base / "webview"

    def _patches(self, pin):
        stack = contextlib.ExitStack()
        stack.enter_context(host(webview_pin=pin))
        stack.enter_context(mock.patch.object(webview, "XODUS_WEBVIEW_DIR",
                                              self.dest))
        stack.enter_context(mock.patch.object(webview, "XODUS_REV", "testrev"))
        stack.enter_context(mock.patch.object(webview, "XODUS_WEBVIEW_REV",
                                              "trixie-9"))
        stack.enter_context(mock.patch.object(webview, "ARM64_ASSET",
                                              "xodus-webview-trixie-9-aarch64"
                                              ".tar.xz"))
        from bol import feed
        stack.enter_context(mock.patch.object(feed, "resolve", _no_feed))
        return stack

    def test_the_pin_names_the_aarch64_asset_and_tag(self):
        with self._patches("cd" * 32):
            rev, sha256, url, asset = webview._pin()
        self.assertEqual(rev, "trixie-9-aarch64")
        self.assertEqual(sha256, "cd" * 32)
        self.assertEqual(asset, "xodus-webview-trixie-9-aarch64.tar.xz")
        self.assertTrue(url.endswith(
            "/releases/download/xodus-testrev-aarch64/"
            "xodus-webview-trixie-9-aarch64.tar.xz"), url)

    def test_installs_the_aarch64_runtime(self):
        archive = self.base / "sibling" / "xodus-webview-trixie-9-aarch64.tar.xz"
        _bundle_archive(archive, exec_dir=ARM_EXEC_DIR)
        import hashlib
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        with self._patches(digest), \
                mock.patch.object(webview.sys, "argv",
                                  [str(archive.parent / "launcher")]):
            webview.ensure_runtime()
            self.assertTrue(webview.installed())
            self.assertEqual((self.dest / ".rev").read_text().strip(),
                             "trixie-9-aarch64")

    def test_an_x86_64_runtime_installed_earlier_does_not_count(self):
        (self.dest / "lib").mkdir(parents=True)
        (self.dest / webview._WEBKIT_LIB).write_bytes(_library())
        (self.dest / ".rev").write_text("trixie-9\n")
        with self._patches("cd" * 32):
            self.assertFalse(webview.installed())

    def test_helpers_are_relocated_from_the_aarch64_literal(self):
        root = self.base / "bundle"
        (root / "lib").mkdir(parents=True)
        library = root / webview._WEBKIT_LIB
        library.write_bytes(_library(exec_dir=ARM_EXEC_DIR))
        with self._patches("cd" * 32):
            webview._point_helpers_at(root, Path("/run/user/1000/bol-webkit"))
        data = library.read_bytes()
        self.assertIn(b"/run/user/1000/bol-webkit\x00", data)
        self.assertNotIn(ARM_EXEC_DIR.encode() + b"\x00", data)

    def test_status_without_an_aarch64_runtime_asks_for_the_host_one(self):
        with self._patches(""), \
                mock.patch.object(webview, "host_has_webkitgtk",
                                  return_value=False), \
                mock.patch.object(webview, "host_package_name",
                                  return_value="libwebkit2gtk-4.1-0"):
            summary, package = webview.status()
        self.assertIn("MISSING", summary)
        self.assertEqual(package, "libwebkit2gtk-4.1-0")

    def test_status_with_an_aarch64_runtime_pinned(self):
        with self._patches("cd" * 32), \
                mock.patch.object(webview, "host_has_webkitgtk",
                                  return_value=False):
            summary, package = webview.status()
        self.assertIn("downloaded on first use", summary)
        self.assertIsNone(package)

    def test_x86_64_computers_keep_the_x86_64_literal(self):
        with host(machine="x86_64"):
            self.assertEqual(webview._exec_dir(), X86_EXEC_DIR)
            self.assertEqual(webview._pin(embedded=True)[3], webview.ASSET)


if __name__ == "__main__":
    unittest.main()
