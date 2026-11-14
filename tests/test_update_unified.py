"""One update path per install kind: checked, started once, reversible."""
# SPDX-License-Identifier: MIT

import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bol import rollback, update


def _pyz(marker=b"new"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("__main__.py", marker)
    return b"#!/usr/bin/env python3\n" + buffer.getvalue()


def _release(name, data, signed=True, version="3.0.1"):
    asset = {"name": name,
             "browser_download_url": "https://example.invalid/" + name}
    if signed:
        asset["sha256"] = hashlib.sha256(data).hexdigest()
    return {"version": version, "tag": "v" + version,
            "url": "https://example.invalid/v" + version, "assets": [asset]}


class PlanTests(unittest.TestCase):
    def test_every_install_kind_has_exactly_one_plan(self):
        self.assertEqual(
            {kind: plan.how for kind, plan in update.PLANS.items()},
            {"appimage": "swap", "file": "swap", "deb": "package",
             "rpm": "package", "flatpak": "handoff", "nix": "handoff",
             "git": "handoff", "system": "handoff"})

    def _kind(self, self_path, package, environ=None, flatpak=False):
        with mock.patch.dict(update.os.environ, environ or {}, clear=True), \
                mock.patch.object(update, "is_flatpak",
                                  return_value=flatpak), \
                mock.patch.object(update, "_self_path",
                                  return_value=Path(self_path)), \
                mock.patch.object(update, "_package_dir",
                                  return_value=Path(package)), \
                mock.patch.object(update, "package_format",
                                  return_value=None):
            return update.update_kind()

    def test_a_nix_store_path_is_left_to_nix(self):
        store = "/nix/store/abc-bedrock-on-linux-3.0.0"
        self.assertEqual(self._kind(
            store + "/bin/bedrock-on-linux",
            store + "/lib/python3.14/site-packages/bol"), "nix")

    def test_a_flatpak_is_left_to_flatpak(self):
        self.assertEqual(self._kind("/app/bin/bedrock-on-linux",
                                    "/app/lib/bol", flatpak=True), "flatpak")

    def test_handoffs_name_the_tool_that_updates_them(self):
        rel = _release("x.flatpak", b"", version="3.0.1")
        for kind, word in (("nix", "nix flake update"), ("git", "git pull"),
                           ("flatpak", "flatpak install"),
                           ("system", "package manager")):
            with mock.patch.object(update, "update_kind",
                                   return_value=kind), \
                    mock.patch.object(update, "is_flatpak",
                                      return_value=kind == "flatpak"), \
                    mock.patch.object(update, "download") as download:
                state, message = update.self_update(rel)
            self.assertIn(state, ("system", "git"))
            self.assertIn(word, message)
            download.assert_not_called()


class SwapTests(unittest.TestCase):
    """The .pyz and the AppImage share one swap."""

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.dir = Path(holder.name)
        self.target = self.dir / "bedrock-on-linux.pyz"
        self.target.write_bytes(_pyz(b"old"))
        self.target.chmod(0o755)
        self.old = self.target.read_bytes()
        self.new = _pyz(b"new")
        self.name = "BedrockOnLinux-3.0.1.pyz"
        self.probe = mock.Mock(return_value=SimpleNamespace(
            returncode=0, stdout="BedrockOnLinux 3.0.1\n", stderr=""))

    def _update(self, rel, served=None, kind="file", delta=None):
        served = self.new if served is None else served

        def fetch(url, dest, label=None, progress=None):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(served)

        environ = {"APPIMAGE": str(self.target)} if kind == "appimage" else {}
        with mock.patch.object(update, "CACHE", self.dir / "cache"), \
                mock.patch.object(update, "update_kind", return_value=kind), \
                mock.patch.object(update, "_self_path",
                                  return_value=self.target), \
                mock.patch.dict(update.os.environ, environ), \
                mock.patch.object(rollback, "DATA", self.dir / "data"), \
                mock.patch.object(update, "_appimage_delta",
                                  return_value=delta), \
                mock.patch.object(update.subprocess, "run", self.probe), \
                mock.patch.object(update, "download",
                                  side_effect=fetch) as download:
            result = update.self_update(rel)
        self.downloads = download.call_count
        return result

    def test_a_checked_file_that_starts_replaces_the_old_one(self):
        state, _ = self._update(_release(self.name, self.new))
        self.assertEqual(state, "ok")
        self.assertEqual(self.target.read_bytes(), self.new)
        # Run once before the swap, as itself, marked as a probe.
        argv = self.probe.call_args.args[0]
        self.assertEqual(argv[-1], "--version")
        self.assertEqual(
            self.probe.call_args.kwargs["env"][rollback.PROBE_ENV], "1")

    def test_the_previous_file_is_kept_and_the_rollback_armed(self):
        self._update(_release(self.name, self.new))
        self.assertEqual(rollback.backup_path(self.target).read_bytes(),
                         self.old)
        with mock.patch.object(rollback, "DATA", self.dir / "data"):
            marker = rollback._read(rollback.marker_path())
        self.assertEqual((marker["from"], marker["to"]),
                         (update.VERSION, "3.0.1"))
        self.assertEqual(marker["target"], str(self.target.resolve()))

    def test_a_file_that_does_not_start_here_is_never_swapped_in(self):
        self.probe.return_value = SimpleNamespace(
            returncode=1, stdout="",
            stderr="ImportError: libQt6Core.so.6: cannot open\n")
        state, message = self._update(_release(self.name, self.new))
        self.assertEqual(state, "error")
        self.assertIn("libQt6Core", message)
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertEqual([p.name for p in self.dir.iterdir()
                          if p.is_file()], [self.target.name])

    def test_a_file_reporting_another_version_is_not_swapped_in(self):
        self.probe.return_value = SimpleNamespace(
            returncode=0, stdout="BedrockOnLinux 2.0.0\n", stderr="")
        state, _ = self._update(_release(self.name, self.new))
        self.assertEqual(state, "error")
        self.assertEqual(self.target.read_bytes(), self.old)

    def test_bytes_that_do_not_match_the_signed_digest_change_nothing(self):
        state, message = self._update(_release(self.name, self.new),
                                      served=_pyz(b"someone else's"))
        self.assertEqual(state, "error")
        self.assertIn("checksum", message)
        self.assertEqual(self.target.read_bytes(), self.old)
        self.probe.assert_not_called()

    def test_a_release_that_states_no_digest_is_refused(self):
        state, message = self._update(
            _release(self.name, self.new, signed=False))
        self.assertEqual(state, "error")
        self.assertIn("no checksum", message)
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertEqual(self.downloads, 0)

    def test_the_signed_digest_needs_no_checksum_list_download(self):
        self._update(_release(self.name, self.new))
        self.assertEqual(self.downloads, 1)

    def test_a_delta_that_matches_the_digest_saves_the_download(self):
        def delta(target):
            target.write_bytes(self.new)
            return True
        rel = _release("BedrockOnLinux-3.0.1-x86_64.AppImage", self.new)
        state, _ = self._update(rel, kind="appimage", delta=delta)
        self.assertEqual(state, "ok")
        self.assertEqual(self.downloads, 0)
        self.assertEqual(self.target.read_bytes(), self.new)

    def test_a_delta_that_does_not_match_falls_back_to_the_download(self):
        def delta(target):
            target.write_bytes(b"what the mirror had instead")
            return True
        rel = _release("BedrockOnLinux-3.0.1-x86_64.AppImage", self.new)
        state, _ = self._update(rel, kind="appimage", delta=delta)
        self.assertEqual(state, "ok")
        self.assertEqual(self.downloads, 1)
        self.assertEqual(self.target.read_bytes(), self.new)


class PackageProbeTests(unittest.TestCase):
    def test_a_package_that_installs_but_does_not_start_is_reported(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        data = b"deb bytes"
        rel = _release("bedrock-on-linux_3.0.1_amd64.deb", data)

        def fetch(url, dest, label=None, progress=None):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)

        def run(argv, **_kwargs):
            if argv[-1] == "--version":
                return SimpleNamespace(returncode=1, stdout="",
                                       stderr="Traceback\nboom\n")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(update, "CACHE", Path(holder.name)), \
                mock.patch.object(update, "update_kind", return_value="deb"), \
                mock.patch.object(update, "download", side_effect=fetch), \
                mock.patch.object(update.shutil, "which",
                                  side_effect=lambda t: "/usr/bin/" + t), \
                mock.patch.object(update.subprocess, "run", side_effect=run):
            state, message = update.self_update(rel)
        self.assertEqual(state, "error")
        self.assertIn("does not start", message)
        self.assertIn(f"v{update.VERSION}", message)


class CheckTests(unittest.TestCase):
    def _check(self, signed, github, channel="stable"):
        with mock.patch("bol.feed.launcher_release", return_value=signed), \
                mock.patch("bol.netpolicy.channel", return_value=channel), \
                mock.patch.object(update, "_github_release",
                                  return_value=github) as asked:
            return update.check_for_update(), asked

    def test_a_newer_release_in_the_signed_feed_needs_no_other_request(self):
        signed = _release("x.pyz", b"x", version="99.0.0")
        rel, asked = self._check(signed, None)
        self.assertIs(rel, signed)
        asked.assert_not_called()

    def test_github_answers_when_the_feed_names_nothing_newer(self):
        github = dict(_release("x.pyz", b"x", version="99.1.0"),
                      source="github")
        stale = _release("x.pyz", b"x", version="0.0.1")
        for signed in (None, stale):
            rel, _ = self._check(signed, github)
            self.assertEqual(rel["version"], "99.1.0")

    def test_no_answer_at_all_is_simply_no_update(self):
        self.assertIsNone(self._check(None, None)[0])
        with mock.patch("bol.feed.launcher_release",
                        side_effect=RuntimeError), \
                mock.patch.object(update, "_github_release",
                                  side_effect=OSError):
            self.assertIsNone(update.check_for_update())

    def test_the_prerelease_channel_skips_the_rolling_nightly(self):
        releases = [
            {"tag_name": "nightly", "prerelease": True},
            {"tag_name": "v99.0.0", "prerelease": True, "assets": []},
            {"tag_name": "v98.0.0", "assets": []},
            {"tag_name": "v100.0.0", "draft": True, "assets": []}]
        with mock.patch.object(update, "gh_releases",
                               return_value=releases):
            self.assertEqual(update._github_release("prerelease")["version"],
                             "99.0.0")


if __name__ == "__main__":
    unittest.main()
