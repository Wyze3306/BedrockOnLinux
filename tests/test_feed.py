"""The signed data channel: what is accepted, refused, and applied."""
# SPDX-License-Identifier: MIT

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import feed

from tests.feedharness import (
    KEY, OTHER_KEY, SHA_A, SHA_B, manifest, offline, sandbox, serve, signed,
    tampered, tool,
)

URL = ("https://github.com/Wyze3306/BedrockOnLinux/releases/download/"
       "xodus-r-p15/xodus-cli-r-p15.tar.gz")
PIN = {"rev": "r-p15", "sha256": SHA_A, "replaces": ["r-p14"], "url": URL}


class _Sandboxed(unittest.TestCase):
    mode = "auto"

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.dir = Path(holder.name)
        box = sandbox(self.dir, mode=self.mode)
        self.settings = box.__enter__()
        self.addCleanup(box.__exit__, None, None, None)

    def accept(self, payload, **kwargs):
        return feed.refresh(fetch=serve(signed(payload)), **kwargs)


class SignatureTests(_Sandboxed):
    def test_a_feed_signed_by_the_embedded_key_is_applied(self):
        state, _ = self.accept(manifest(
            components={"stable": {"xodus": [PIN]}}))
        self.assertEqual(state, "updated")
        self.assertEqual(feed.resolve("xodus", "r-p14", SHA_B),
                         ("r-p15", SHA_A, URL))

    def test_a_feed_signed_by_another_key_is_refused(self):
        raw = signed(manifest(components={"stable": {"xodus": [PIN]}}),
                     key=OTHER_KEY)
        state, detail = feed.refresh(fetch=serve(raw))
        self.assertEqual(state, "error")
        self.assertIn("no trusted key", detail)
        self.assertEqual(feed.resolve("xodus", "r-p14", SHA_B),
                         ("r-p14", SHA_B, None))
        self.assertFalse((self.dir / "feed" / "manifest.signed.json").exists())

    def test_a_payload_changed_after_signing_is_refused(self):
        raw = tampered(signed(manifest()))
        self.assertEqual(feed.refresh(fetch=serve(raw))[0], "error")
        self.assertIsNone(feed.current())

    def test_a_key_id_alone_proves_nothing(self):
        """The right key id over someone else's signature is still refused."""
        forged = dict(OTHER_KEY, keyid=KEY["keyid"])
        raw = signed(manifest(), key=forged)
        self.assertEqual(feed.refresh(fetch=serve(raw))[0], "error")

    def test_either_of_two_signatures_is_enough_for_a_key_rotation(self):
        raw = tool.sign_payload(manifest(), [OTHER_KEY, KEY])[0]
        self.assertEqual(feed.refresh(fetch=serve(raw))[0], "updated")

    def test_the_pure_python_verifier_reaches_the_same_verdicts(self):
        real = feed.ed25519.verify
        with mock.patch.object(
                feed.ed25519, "verify",
                lambda *a, **k: real(*a, pure=True)):
            self.assertEqual(self.accept(manifest())[0], "updated")
            other = signed(manifest(serial=200), key=OTHER_KEY)
            self.assertEqual(feed.refresh(fetch=serve(other))[0], "error")

    def test_garbage_from_the_server_is_just_a_failed_refresh(self):
        for raw in (b"", b"<html>404</html>", b"[]", b'{"payload": 1}',
                    b"x" * (feed.MAX_BYTES + 1)):
            self.assertEqual(feed.refresh(fetch=serve(raw))[0], "error")

    def test_a_cache_edited_on_disk_is_not_trusted(self):
        self.accept(manifest(components={"stable": {"xodus": [PIN]}}))
        cached = self.dir / "feed" / "manifest.signed.json"
        cached.write_bytes(tampered(cached.read_bytes()))
        feed.forget()
        self.assertIsNone(feed.current())

    def test_without_an_embedded_key_nothing_is_fetched_at_all(self):
        fetch = mock.Mock()
        with mock.patch.object(feed, "PUBLIC_KEYS", {}):
            self.assertEqual(feed.refresh(fetch=fetch)[0], "off")
            self.assertFalse(feed.due())
        fetch.assert_not_called()


class RollbackTests(_Sandboxed):
    def test_an_older_serial_is_refused_and_the_newer_one_kept(self):
        self.accept(manifest(serial=200, notices=[
            {"id": "new", "title": "New"}]))
        state, detail = self.accept(manifest(serial=150, notices=[
            {"id": "old", "title": "Old"}]))
        self.assertEqual(state, "error")
        self.assertIn("roll-back", detail)
        self.assertEqual([n["id"] for n in feed.notices()], ["new"])

    def test_the_same_serial_again_is_simply_current(self):
        self.accept(manifest(serial=200))
        self.assertEqual(self.accept(manifest(serial=200))[0], "current")

    def test_an_older_signed_file_dropped_into_the_cache_is_ignored(self):
        self.accept(manifest(serial=200))
        (self.dir / "feed" / "manifest.signed.json").write_bytes(
            signed(manifest(serial=150, notices=[
                {"id": "old", "title": "Old"}])))
        feed.forget()
        self.assertIsNone(feed.current())

    def test_an_expired_feed_is_not_accepted(self):
        state, detail = self.accept(manifest(expires=1000), now=2000)
        self.assertEqual(state, "error")
        self.assertIn("expired", detail)

    def test_a_pin_never_replaces_a_revision_it_does_not_name(self):
        """A launcher shipping something newer keeps what it ships."""
        self.accept(manifest(components={"stable": {"xodus": [PIN]}}))
        self.assertEqual(feed.resolve("xodus", "r-p16", SHA_B),
                         ("r-p16", SHA_B, None))

    def test_pins_chain_from_any_older_embedded_revision(self):
        newer = {"rev": "r-p16", "sha256": SHA_B, "replaces": ["r-p15"]}
        self.accept(manifest(components={"stable": {"xodus": [PIN, newer]}}))
        self.assertEqual(feed.resolve("xodus", "r-p14", "0" * 64)[0], "r-p16")

    def test_a_pin_for_a_later_launcher_waits_for_it(self):
        pin = dict(PIN, min_launcher="99.0.0")
        self.accept(manifest(components={"stable": {"xodus": [pin]}}))
        self.assertEqual(feed.resolve("xodus", "r-p14", SHA_B)[0], "r-p14")


class OfflineTests(_Sandboxed):
    def test_no_network_and_no_cache_means_the_embedded_pins(self):
        state, detail = feed.refresh(fetch=offline)
        self.assertEqual(state, "error")
        self.assertIn("unreachable", detail)
        self.assertEqual(feed.resolve("xodus", "r-p14", SHA_B),
                         ("r-p14", SHA_B, None))
        self.assertEqual(feed.notices(), [])
        self.assertIsNone(feed.launcher_release("stable"))

    def test_no_network_keeps_using_the_cached_feed(self):
        self.accept(manifest(components={"stable": {"xodus": [PIN]}}))
        self.assertEqual(feed.refresh(fetch=offline)[0], "error")
        self.assertEqual(feed.resolve("xodus", "r-p14", SHA_B)[0], "r-p15")

    def test_a_failed_fetch_is_not_repeated_on_every_start(self):
        feed.refresh(fetch=offline, now=1000)
        self.assertFalse(feed.due(now=1000 + 60))
        self.assertTrue(feed.due(now=1000 + feed.RETRY_AFTER + 1))

    def test_a_fresh_answer_is_not_asked_for_again_for_hours(self):
        self.accept(manifest(), now=1000)
        self.assertFalse(feed.due(now=1000 + 3600))
        self.assertTrue(feed.due(now=1000 + feed.REFRESH_AFTER + 1))

    def test_a_server_that_hangs_up_cannot_break_a_caller(self):
        def crash(url, timeout):
            raise RuntimeError("anything at all")
        self.assertEqual(feed.refresh(fetch=crash)[0], "error")
        with mock.patch.object(feed, "refresh", side_effect=RuntimeError):
            self.assertEqual(feed.refresh_if_due(), ("skipped", ""))

    def test_resolve_survives_a_feed_it_cannot_read(self):
        with mock.patch.object(feed, "component_pin",
                               side_effect=RuntimeError):
            self.assertEqual(feed.resolve("xodus", "r-p14", SHA_B),
                             ("r-p14", SHA_B, None))


class ConsentTests(unittest.TestCase):
    def _box(self, mode):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        box = sandbox(Path(holder.name), mode=mode)
        box.__enter__()
        self.addCleanup(box.__exit__, None, None, None)

    def test_off_makes_no_background_request(self):
        self._box("off")
        with mock.patch.object(feed, "refresh") as refresh:
            self.assertEqual(feed.refresh_if_due(), ("skipped", ""))
        refresh.assert_not_called()

    def test_ask_and_undecided_make_no_background_request_either(self):
        for mode in ("ask", None):
            self._box(mode)
            with mock.patch.object(feed, "refresh") as refresh:
                feed.refresh_if_due()
            refresh.assert_not_called()

    def test_off_ignores_a_feed_fetched_before_the_refusal(self):
        self._box("auto")
        feed.refresh(fetch=serve(signed(manifest(
            components={"stable": {"xodus": [PIN]}},
            notices=[{"id": "n", "title": "N"}]))))
        self.assertEqual(feed.resolve("xodus", "r-p14", SHA_B)[0], "r-p15")
        with mock.patch("bol.util.load_settings",
                        lambda: {"network_updates": "off"}):
            self.assertEqual(feed.resolve("xodus", "r-p14", SHA_B),
                             ("r-p14", SHA_B, None))
            self.assertEqual(feed.notices(), [])
            self.assertEqual(feed.hbui_rules(), [])

    def test_auto_refreshes_when_due(self):
        self._box("auto")
        with mock.patch.object(feed, "refresh",
                               return_value=("updated", "")) as refresh:
            self.assertEqual(feed.refresh_if_due()[0], "updated")
        refresh.assert_called_once()

    def test_the_request_carries_nothing_but_the_launcher_name(self):
        self._box("auto")
        seen = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, limit):
                return signed(manifest())

        def urlopen(request, timeout):
            seen["headers"] = dict(request.header_items())
            seen["url"] = request.full_url
            seen["data"] = request.data
            return Response()

        with mock.patch.object(feed.urllib.request, "urlopen", urlopen):
            self.assertEqual(feed.refresh()[0], "updated")
        self.assertEqual(seen["headers"], {"User-agent": "bedrock-on-linux"})
        self.assertIsNone(seen["data"])
        self.assertNotIn("?", seen["url"])


class ContentTests(_Sandboxed):
    def test_the_prerelease_channel_adds_its_pins_to_the_stable_ones(self):
        pre = {"rev": "r-p16", "sha256": SHA_B, "replaces": ["r-p14"]}
        self.accept(manifest(components={
            "stable": {"xodus": [PIN]}, "prerelease": {"xodus": [pre]}}))
        self.assertEqual(feed.resolve("xodus", "r-p14", SHA_B)[0], "r-p15")
        self.settings["update_channel"] = "prerelease"
        self.assertEqual(feed.resolve("xodus", "r-p14", SHA_B)[0], "r-p16")

    def test_the_launcher_release_carries_signed_digests(self):
        self.accept(manifest(launcher={
            "stable": {"version": "3.0.1", "tag": "v3.0.1", "assets": {
                "BedrockOnLinux-3.0.1-x86_64.AppImage": {"sha256": SHA_A}}},
            "prerelease": {"version": "3.1.0", "tag": "v3.1.0", "assets": {
                "BedrockOnLinux-3.1.0.pyz": {"sha256": SHA_B}}}}))
        stable = feed.launcher_release("stable")
        self.assertEqual(stable["version"], "3.0.1")
        self.assertEqual(stable["assets"][0]["sha256"], SHA_A)
        self.assertEqual(
            stable["assets"][0]["browser_download_url"],
            "https://github.com/Wyze3306/BedrockOnLinux/releases/download/"
            "v3.0.1/BedrockOnLinux-3.0.1-x86_64.AppImage")
        self.assertEqual(feed.launcher_release("prerelease")["version"],
                         "3.1.0")

    def test_notices_are_filtered_and_sorted_by_severity(self):
        self.accept(manifest(notices=[
            {"id": "info", "title": "FYI"},
            {"id": "gone", "title": "Over", "until": 10},
            {"id": "later", "title": "Later", "min_launcher": "99.0"},
            {"id": "pre", "title": "Pre", "channels": ["prerelease"]},
            {"id": "mc", "title": "Broken", "level": "warning",
             "minecraft": ["1.26.70"]},
            {"id": "down", "title": "Sign-in is down", "level": "critical",
             "url": "https://example.org/status"}]))
        self.assertEqual([n["id"] for n in feed.notices()],
                         ["down", "info"])
        self.assertEqual([n["id"] for n in feed.notices("1.26.70.4")],
                         ["down", "mc", "info"])
        self.assertEqual([n["id"] for n in feed.notices("1.26.7")],
                         ["down", "info"])

    def test_values_the_launcher_would_act_on_are_validated(self):
        bad = (
            {"schema": 2},
            {"serial": 0},
            {"serial": True},
            {"components": {"stable": {"engine": [PIN]}}},
            {"components": {"nightly": {}}},
            {"components": {"stable": {"xodus": [dict(PIN, sha256="abc")]}}},
            {"components": {"stable": {"xodus": [dict(PIN, replaces=[])]}}},
            {"components": {"stable": {"xodus": [
                dict(PIN, url="https://evil.example/xodus.tar.gz")]}}},
            {"components": {"stable": {"xodus": [
                dict(PIN, rev="../../etc")]}}},
            {"launcher": {"stable": {"version": "3.0.1", "tag": "v3.0.1",
                                     "assets": {"../x": {"sha256": SHA_A}}}}},
            {"notices": [{"id": "x", "title": "T",
                          "url": "javascript:alert(1)"}]},
            {"notices": [{"id": "x", "title": "T", "level": "panic"}]},
            {"minecraft": {"hbui": [{"id": "x", "link": {
                "anchor": "(unclosed", "done": "x"}}]}},
            {"minecraft": {"hbui": [{"id": "x", "link": {
                "anchor": "no groups", "done": "x"}}]}},
        )
        for change in bad:
            payload = manifest()
            payload.update(change)
            with self.assertRaises(feed.FeedError, msg=json.dumps(change)):
                feed.validate(payload)

    def test_unknown_keys_are_left_for_a_later_launcher(self):
        feed.validate(manifest(future_section={"anything": 1}))

    def test_the_repository_source_is_a_valid_feed(self):
        source = tool.read_source()
        feed.validate(tool.stamp(source))
        self.assertNotIn("serial", source)


class ToolTests(unittest.TestCase):
    def test_the_signing_key_is_refused_inside_a_repository(self):
        repo = Path(__file__).resolve().parent.parent
        with self.assertRaises(SystemExit), \
                mock.patch("sys.stderr"):
            tool.key_path(str(repo / "feed" / "signing.key"))

    def test_no_public_key_ships_without_a_reason(self):
        """Empty until the maintainer generates the real key: the feed is
        then off, not trusting a key nobody holds."""
        for key_id, encoded in feed.PUBLIC_KEYS.items():
            self.assertRegex(key_id, r"^[a-z0-9-]+$")
            self.assertEqual(len(__import__("base64").b64decode(encoded)), 32)

    def test_each_signing_moves_the_serial_forward(self):
        first = tool.stamp({"schema": 1}, floor=0, now=1000)
        again = tool.stamp({"schema": 1}, floor=first["serial"], now=900)
        self.assertEqual(first["serial"], 1000)
        self.assertEqual(again["serial"], 1001)
        self.assertEqual(tool.previous_serial(signed(again)), 1001)

    def test_a_release_checksum_list_becomes_launcher_assets(self):
        assets = tool.parse_sums(
            f"{SHA_A}  BedrockOnLinux-3.0.1.pyz\n"
            f"{SHA_B} *bedrock-on-linux_3.0.1_amd64.deb\nnot a line\n")
        self.assertEqual(assets, {
            "BedrockOnLinux-3.0.1.pyz": {"sha256": SHA_A},
            "bedrock-on-linux_3.0.1_amd64.deb": {"sha256": SHA_B}})


if __name__ == "__main__":
    unittest.main()
