"""What a signed feed changes in the launcher, and what it cannot."""
# SPDX-License-Identifier: MIT

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import feed, fixups, xodus

from tests.feedharness import manifest, sandbox, serve, signed
from tests.test_xodus import _cli_archive

URL = ("https://github.com/Wyze3306/BedrockOnLinux/releases/download/"
       "xodus-new-p2/xodus-cli-new-p2.tar.gz")


class DownloaderPinTests(unittest.TestCase):
    """A rebuilt downloader reaches installs without a launcher release."""

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.base = Path(holder.name)
        self.embedded = self.base / "sibling" / "xodus-cli-old-p1.tar.gz"
        self.embedded_sha = _cli_archive(self.embedded)
        self.served = self.base / "served" / "xodus-cli-new-p2.tar.gz"
        self.served_sha = _cli_archive(self.served)
        self.target = self.base / "xodus"
        box = sandbox(self.base / "data")
        box.__enter__()
        self.addCleanup(box.__exit__, None, None, None)

    def _publish(self, sha256):
        feed.refresh(fetch=serve(signed(manifest(components={"stable": {
            "xodus": [{"rev": "new-p2", "sha256": sha256, "url": URL,
                       "replaces": ["old-p1"]}]}}))))

    def _ensure(self, download):
        with mock.patch.object(xodus, "XODUS_REV", "old-p1"), \
                mock.patch.object(xodus, "XODUS_ARCHIVE_SHA256",
                                  self.embedded_sha), \
                mock.patch.object(xodus, "XODUS_DIR", self.target), \
                mock.patch.object(xodus, "XODUS_BIN",
                                  self.target / "xodus-cli"), \
                mock.patch.object(xodus, "CACHE", self.base / "cache"), \
                mock.patch.object(xodus.sys, "argv",
                                  [str(self.embedded.parent / "launcher")]), \
                mock.patch.object(xodus, "warn") as warn, \
                mock.patch.object(xodus, "download",
                                  side_effect=download) as fetched:
            xodus.ensure_cli()
        return fetched, warn

    def _serve(self, url, dest, label=None, **_kwargs):
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(self.served, dest)

    def test_the_signed_pin_is_installed_from_its_release_url(self):
        self._publish(self.served_sha)
        fetched, _ = self._ensure(self._serve)
        self.assertEqual(fetched.call_args.args[0], URL)
        self.assertEqual((self.target / ".rev").read_text().strip(),
                         "new-p2")

    def test_bytes_that_miss_the_signed_digest_fall_back_to_the_embedded(self):
        self._publish("0" * 64)
        _fetched, warn = self._ensure(self._serve)
        self.assertEqual((self.target / ".rev").read_text().strip(),
                         "old-p1")
        self.assertIn("shipped with", warn.call_args.args[0])
        self.assertFalse((self.base / "cache"
                          / "xodus-cli-new-p2.tar.gz").exists())

    def test_an_unreachable_release_falls_back_to_the_embedded(self):
        self._publish(self.served_sha)

        def unreachable(*_args, **_kwargs):
            raise xodus.BolError("Download failed")
        self._ensure(unreachable)
        self.assertEqual((self.target / ".rev").read_text().strip(),
                         "old-p1")

    def test_without_a_feed_nothing_changes(self):
        fetched, _ = self._ensure(self._serve)
        fetched.assert_not_called()
        self.assertEqual((self.target / ".rev").read_text().strip(),
                         "old-p1")


# A bundle shaped like no build the launcher's own anchors know: the hook
# is called as a member, `i.Z(`, and the notice component is named differently.
_REBUNDLED = (
    'function qa(){return i.Z((e,t)=>{'
    'const n=e.isSignedInPlatformNetwork,r=e.isLoggedInWithMicrosoftAccount;'
    'return n?"x":"not-signed-in"||"msa-guest-playstation"},[],[a,b])}'
    'function next(){}'
    'const cb=useCb(()=>{go(`x_NewNotice_OreUI`)},[go]);'
    'return R.createElement(W,null)')

_RULE = {
    "id": "mc-1-27",
    "gate": {
        "function": r"(function\s+\w+\(\)\{)(return \w+\.Z\()",
        "selector": r"\(?\([\w$,\s]*\)=>\{",
        "marks": ["isSignedInPlatformNetwork", '"not-signed-in"'],
    },
    "link": {
        "anchor": r"(_NewNotice_OreUI`\)\},\[[^\]]*\]\);)"
                  r"(return \w+\.createElement\(\w+,)",
        "done": r"_NewNotice_OreUI`\)\},\[[^\]]*\]\);return null;",
    },
}


class HbuiAnchorTests(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        box = sandbox(Path(holder.name))
        box.__enter__()
        self.addCleanup(box.__exit__, None, None, None)

    def test_a_rebundled_ui_is_out_of_reach_of_the_embedded_anchors(self):
        self.assertEqual(fixups._hbui_gate_patch(_REBUNDLED)[1], "missing")
        self.assertEqual(fixups._hbui_link_patch(_REBUNDLED)[1], "missing")

    def test_signed_anchors_patch_it_without_a_launcher_release(self):
        feed.refresh(fetch=serve(signed(manifest(
            minecraft={"hbui": [_RULE]}))))
        data, gate = fixups._hbui_gate_patch(_REBUNDLED)
        data, link = fixups._hbui_link_patch(data)
        self.assertEqual((gate, link), ("applied", "applied"))
        # Only ever the two fixed insertions, at the anchored places.
        self.assertIn('i.Z((e,t)=>{return"";const n=', data)
        self.assertIn("[go]);return null;return R.createElement", data)
        self.assertEqual(
            len(data) - len(_REBUNDLED),
            len('return"";') + len("return null;"))
        self.assertEqual(fixups._hbui_gate_patch(data)[1], "already")
        self.assertEqual(fixups._hbui_link_patch(data)[1], "already")

    def test_a_feed_cannot_choose_what_is_inserted(self):
        rule = dict(_RULE, insert="alert(1)", gate=dict(
            _RULE["gate"], replacement="evil()"))
        feed.refresh(fetch=serve(signed(manifest(
            minecraft={"hbui": [rule]}))))
        data = fixups._hbui_link_patch(
            fixups._hbui_gate_patch(_REBUNDLED)[0])[0]
        self.assertNotIn("evil", data)
        self.assertNotIn("alert", data)


if __name__ == "__main__":
    unittest.main()
