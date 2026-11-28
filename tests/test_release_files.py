"""The launcher's file store (docs/release-files.md): one release that holds
every file the launcher downloads, read before the releases each file was
published under before it existed, and never overwritten."""
from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bol import util  # noqa: E402
from bol.config import FILES_TAG, WINEGDK_PREBUILT_REPO  # noqa: E402
from bol.log import BolError  # noqa: E402

SCRIPT = ROOT / "scripts/release-files.sh"
WORKFLOWS = ROOT / ".github/workflows"
_BASH = shutil.which("bash") or "/bin/bash"
BASE = f"https://github.com/{WINEGDK_PREBUILT_REPO}/releases/download"


class AddressTests(unittest.TestCase):
    def test_the_store_is_one_release_of_this_repository(self):
        self.assertEqual(FILES_TAG, "files")
        self.assertEqual(util.files_url("a.tar.gz"), f"{BASE}/files/a.tar.gz")
        self.assertEqual(util.release_asset_url("engine-x", "a.tar.gz"),
                         f"{BASE}/engine-x/a.tar.gz")

    def test_the_launcher_and_the_script_name_the_same_store(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn(f'STORE_TAG="{FILES_TAG}"', text)

    def test_store_addresses_pass_the_data_feed_url_rule(self):
        # A feed pin may point into the store.
        from bol import feed
        self.assertRegex(util.files_url("xodus-cli-64d39eb87a56-p16.tar.gz"),
                         feed._ASSET_URL)


class DownloadFirstTests(unittest.TestCase):
    def _fetch(self, missing):
        calls = []

        def fetch(url, dest, label=None, **kwargs):
            calls.append((url, kwargs))
            if url in missing:
                if kwargs.get("missing_ok"):
                    raise util.AssetMissing(url)
                raise BolError(f"Download failed: {url}")
            return dest

        return fetch, calls

    def test_the_first_address_that_has_the_file_wins(self):
        fetch, calls = self._fetch(missing=set())
        dest = Path("/nonexistent/a")
        self.assertIs(util.download_first(("one", "two"), dest, "A",
                                          fetch=fetch, attempts=2), dest)
        self.assertEqual(calls, [("one", {"missing_ok": True, "attempts": 2})])

    def test_a_missing_file_moves_on_to_the_next_address(self):
        fetch, calls = self._fetch(missing={"one"})
        util.download_first(("one", "two"), Path("/nonexistent/a"),
                            fetch=fetch)
        self.assertEqual([url for url, _ in calls], ["one", "two"])
        # The last address fails the way download() does.
        self.assertEqual(calls[-1][1], {})

    def test_the_last_address_reports_a_missing_file_when_asked(self):
        fetch, _calls = self._fetch(missing={"one", "two"})
        with self.assertRaises(util.AssetMissing):
            util.download_first(("one", "two"), Path("/nonexistent/a"),
                                fetch=fetch, missing_ok=True)
        with self.assertRaises(BolError) as caught:
            util.download_first(("one", "two"), Path("/nonexistent/a"),
                                fetch=fetch)
        self.assertNotIsInstance(caught.exception, util.AssetMissing)

    def test_any_other_failure_is_not_a_reason_to_try_elsewhere(self):
        calls = []

        def fetch(url, dest, label=None, **kwargs):
            calls.append(url)
            raise BolError("Download failed after 5 attempts")

        with self.assertRaises(BolError):
            util.download_first(("one", "two"), Path("/nonexistent/a"),
                                fetch=fetch)
        self.assertEqual(calls, ["one"])

    def test_download_reports_a_404_as_missing_only_when_asked(self):
        def not_found(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found",
                                         {}, io.BytesIO())

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(util.urllib.request, "urlopen", not_found):
            dest = Path(tmp) / "a.tar.gz"
            with self.assertRaises(util.AssetMissing):
                util.download("https://x.example/a", dest, missing_ok=True)
            with self.assertRaises(BolError) as caught:
                util.download("https://x.example/a", dest)
            self.assertNotIsInstance(caught.exception, util.AssetMissing)


# A stand-in for the gh CLI, over a directory of releases: <root>/<tag>/
# holds a release's assets, <root>/<tag>.json its creation arguments.
_FAKE_GH = textwrap.dedent('''\
    import hashlib, json, os, re, shutil, sys
    from pathlib import Path

    root = Path(os.environ["FAKE_GH_ROOT"])
    args = sys.argv[1:]
    with open(root / "calls.log", "a") as log:
        log.write(json.dumps(args) + "\\n")

    def opt(name):
        return [args[i + 1] for i, a in enumerate(args) if a == name]

    if args[0] == "api":
        path = next(a for a in args[1:] if not a.startswith("-")
                    and a not in opt("--jq"))
        m = re.fullmatch(r"repos/[^/]+/[^/]+/releases/tags/([^/]+)", path)
        if m:
            if not (root / m.group(1)).is_dir():
                print('{"message":"Not Found","status":"404"}')
                sys.exit(1)
            print(m.group(1))
            sys.exit(0)
        m = re.fullmatch(r"repos/[^/]+/[^/]+/releases/([^/]+)/assets.*", path)
        name = re.search(r'\\.name == "([^"]+)"', opt("--jq")[0]).group(1)
        asset = root / m.group(1) / name
        if asset.is_file():
            if (root / m.group(1) / (name + ".nodigest")).exists():
                print("none")
            else:
                print("sha256:" + hashlib.sha256(asset.read_bytes()).hexdigest())
        sys.exit(0)

    if args[:2] == ["release", "download"]:
        tag = root / args[2]
        dest = Path(opt("-D")[0])
        if not tag.is_dir():
            print("release not found", file=sys.stderr)
            sys.exit(1)
        hits = [tag / p for p in opt("-p") if (tag / p).is_file()]
        if not hits:
            print("no assets match the file pattern", file=sys.stderr)
            sys.exit(1)
        dest.mkdir(parents=True, exist_ok=True)
        for hit in hits:
            shutil.copy(hit, dest / hit.name)
        sys.exit(0)

    if args[:2] == ["release", "create"]:
        (root / args[2]).mkdir()
        (root / (args[2] + ".json")).write_text(json.dumps(args))
        sys.exit(0)

    if args[:2] == ["release", "upload"]:
        tag, src = root / args[2], Path(args[3])
        if "--clobber" in args:
            sys.exit(3)
        if (tag / src.name).exists():
            print("asset under the same name already exists", file=sys.stderr)
            sys.exit(1)
        shutil.copy(src, tag / src.name)
        sys.exit(0)

    sys.exit(2)
''')


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class StoreScriptTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.releases = self.tmp / "releases"
        self.releases.mkdir()
        bindir = self.tmp / "bin"
        bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text(f"#!{sys.executable}\n" + _FAKE_GH)
        gh.chmod(0o755)
        self.env = dict(os.environ,
                        PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
                        FAKE_GH_ROOT=str(self.releases),
                        GITHUB_REPOSITORY="owner/repo")
        self.work = self.tmp / "work"
        self.work.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def run_script(self, *args):
        return subprocess.run([_BASH, str(SCRIPT), *args], env=self.env,
                              cwd=self.work, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, check=False)

    def built(self, name, data, sidecar=True):
        path = self.work / name
        path.write_bytes(data)
        if sidecar:
            (self.work / (name + ".sha256")).write_text(
                f"{_sha(path)}  {name}\n")
        return path

    def release(self, tag, name, data, sidecar_name=None):
        directory = self.releases / tag
        directory.mkdir(exist_ok=True)
        (directory / name).write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        (directory / (name + ".sha256")).write_text(
            f"{digest}  {sidecar_name or name}\n")
        return digest

    def test_publish_creates_the_store_on_the_first_commit_as_a_prerelease(self):
        self.built("a.tar.gz", b"one")
        result = self.run_script("publish", "a.tar.gz")
        self.assertEqual(result.returncode, 0, result.stderr)
        create = (self.releases / "files.json").read_text()
        self.assertIn('"--prerelease"', create)
        self.assertIn('"--latest=false"', create)
        root = subprocess.run(
            ["git", "-C", str(ROOT), "rev-list", "--max-parents=0", "HEAD"],
            stdout=subprocess.PIPE, text=True, check=False).stdout.split()
        target = re.search(r'"--target", "([0-9a-f]{40})"', create).group(1)
        if root:
            self.assertIn(target, root)
        self.assertEqual((self.releases / "files/a.tar.gz").read_bytes(),
                         b"one")
        self.assertTrue((self.releases / "files/a.tar.gz.sha256").is_file())

    def test_the_same_bytes_again_are_skipped(self):
        self.built("a.tar.gz", b"one")
        self.assertEqual(self.run_script("publish", "a.tar.gz").returncode, 0)
        again = self.run_script("publish", "a.tar.gz")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("already published with these bytes", again.stdout)
        calls = (self.releases / "calls.log").read_text()
        self.assertNotIn("--clobber", calls)
        self.assertEqual(calls.count('"create"'), 1)

    def test_other_bytes_under_a_published_name_are_refused(self):
        self.built("a.tar.gz", b"one")
        self.assertEqual(self.run_script("publish", "a.tar.gz").returncode, 0)
        self.built("a.tar.gz", b"two")
        result = self.run_script("publish", "a.tar.gz")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("A name holds one set of bytes", result.stderr)
        self.assertEqual((self.releases / "files/a.tar.gz").read_bytes(),
                         b"one")

    def test_an_asset_without_a_recorded_digest_is_hashed(self):
        self.built("a.tar.gz", b"one")
        self.assertEqual(self.run_script("publish", "a.tar.gz").returncode, 0)
        (self.releases / "files/a.tar.gz.nodigest").touch()
        result = self.run_script("asset-sha", "a.tar.gz")
        self.assertEqual(result.stdout.strip(),
                         hashlib.sha256(b"one").hexdigest())

    def test_asset_sha_is_empty_without_a_store_or_an_asset(self):
        result = self.run_script("asset-sha", "a.tar.gz")
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        (self.releases / "files").mkdir()
        result = self.run_script("asset-sha", "a.tar.gz")
        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_fetch_prefers_the_store(self):
        self.release("files", "a.tar.gz", b"store")
        self.release("old-tag", "a.tar.gz", b"old")
        result = self.run_script("fetch", "a.tar.gz", "out", "old-tag")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "store")
        self.assertEqual((self.work / "out/a.tar.gz").read_bytes(), b"store")

    def test_fetch_falls_back_to_the_old_release_under_the_new_name(self):
        digest = self.release("vkd3d-x", "vkd3d-universal.tar.gz", b"old")
        result = self.run_script("fetch", "vkd3d-universal-lock.tar.gz", "out",
                                 "vkd3d-x", "vkd3d-universal.tar.gz")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "vkd3d-x")
        out = self.work / "out"
        self.assertEqual((out / "vkd3d-universal-lock.tar.gz").read_bytes(),
                         b"old")
        # The sidecar names the file it sits beside, for sha256sum -c.
        self.assertEqual((out / "vkd3d-universal-lock.tar.gz.sha256")
                         .read_text(),
                         f"{digest}  vkd3d-universal-lock.tar.gz\n")
        check = subprocess.run(["sha256sum", "-c",
                                "vkd3d-universal-lock.tar.gz.sha256"],
                               cwd=out, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, check=False)
        self.assertEqual(check.returncode, 0, check.stderr)

    def test_fetch_refuses_bytes_that_do_not_match_their_sidecar(self):
        self.release("files", "a.tar.gz", b"store")
        (self.releases / "files/a.tar.gz").write_bytes(b"tampered")
        result = self.run_script("fetch", "a.tar.gz", "out")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(".sha256 says", result.stderr)
        self.assertFalse((self.work / "out/a.tar.gz").exists())

    def test_fetch_fails_when_neither_place_has_the_file(self):
        result = self.run_script("fetch", "a.tar.gz", "out", "old-tag")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("neither in the files release nor in old-tag",
                      result.stderr)


class WorkflowTests(unittest.TestCase):
    PUBLISHERS = ("build-engine.yml", "build-winegdk.yml", "build-vkd3d.yml",
                  "build-xcurl.yml", "build-xodus.yml")

    def test_builds_publish_to_the_store_and_nowhere_else(self):
        for name in self.PUBLISHERS:
            with self.subTest(workflow=name):
                text = (WORKFLOWS / name).read_text(encoding="utf-8")
                self.assertIn("scripts/release-files.sh publish", text)
                self.assertNotIn("softprops/action-gh-release", text)
                self.assertNotIn("--clobber", text)
                self.assertNotRegex(text, r"gh release (create|upload)")

    def test_the_carry_over_only_publishes_when_asked(self):
        text = (WORKFLOWS / "files-carry-over.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch", text)
        self.assertRegex(text, r"default: false")
        self.assertIn('if [ "$PUBLISH" = true ]; then', text)
        self.assertNotRegex(text, r"gh release (delete|edit)")


if __name__ == "__main__":
    unittest.main()
