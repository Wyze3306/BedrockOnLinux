"""Security tests for shared untrusted TAR extraction."""

import io
import hashlib
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import fixups, prefix
from bol.archive import safe_extract_tar


class SafeTarExtractionTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.base = Path(self._td.name)
        self.destination = self.base / "extract"

    def tearDown(self):
        self._td.cleanup()

    def _extract_members(self, members):
        path = self.base / "fixture.tar"
        with tarfile.open(path, "w") as archive:
            for member, payload in members:
                archive.addfile(
                    member,
                    io.BytesIO(payload) if payload is not None else None)
        with tarfile.open(path) as archive:
            safe_extract_tar(archive, self.destination)

    @staticmethod
    def _file(name, payload=b"payload", mode=0o644):
        member = tarfile.TarInfo(name)
        member.size = len(payload)
        member.mode = mode
        return member, payload

    def test_path_traversal_is_rejected_without_outside_write(self):
        with self.assertRaisesRegex(ValueError, "unsafe path"):
            self._extract_members([
                self._file("../outside.txt", b"escaped"),
            ])

        self.assertFalse((self.base / "outside.txt").exists())
        self.assertFalse(self.destination.exists())

    def test_absolute_and_embedded_dotdot_paths_are_rejected(self):
        for name in ("/tmp/bol-absolute", "safe/../outside.txt"):
            with self.subTest(name=name), self.assertRaisesRegex(
                    ValueError, "unsafe path"):
                self._extract_members([self._file(name)])

    def test_escaping_symbolic_link_is_rejected(self):
        member = tarfile.TarInfo("bundle/link")
        member.type = tarfile.SYMTYPE
        member.linkname = "../../outside.txt"

        with self.assertRaisesRegex(ValueError, "unsafe link"):
            self._extract_members([(member, None)])

    def test_escaping_hard_link_is_rejected(self):
        member = tarfile.TarInfo("bundle/link")
        member.type = tarfile.LNKTYPE
        member.linkname = "../outside.txt"

        with self.assertRaisesRegex(ValueError, "unsafe link"):
            self._extract_members([(member, None)])

    def test_member_below_internal_symlink_is_rejected(self):
        link = tarfile.TarInfo("bundle/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "target"

        with self.assertRaisesRegex(ValueError, "nested below a symlink"):
            self._extract_members([
                (link, None),
                self._file("bundle/link/file.txt"),
            ])

    def test_fifo_and_other_special_entries_are_rejected(self):
        fifo = tarfile.TarInfo("bundle/fifo")
        fifo.type = tarfile.FIFOTYPE

        with self.assertRaisesRegex(ValueError, "unsupported entry"):
            self._extract_members([(fifo, None)])

    def test_regular_archive_and_internal_links_extract(self):
        root = tarfile.TarInfo("bundle")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        links = tarfile.TarInfo("bundle/links")
        links.type = tarfile.DIRTYPE
        links.mode = 0o755
        symlink = tarfile.TarInfo("bundle/links/current")
        symlink.type = tarfile.SYMTYPE
        symlink.linkname = "../payload.txt"
        hardlink = tarfile.TarInfo("bundle/copy.txt")
        hardlink.type = tarfile.LNKTYPE
        hardlink.linkname = "bundle/payload.txt"

        self._extract_members([
            (root, None),
            (links, None),
            self._file("bundle/payload.txt", b"trusted payload", 0o755),
            (hardlink, None),
            (symlink, None),
        ])

        payload = self.destination / "bundle/payload.txt"
        copy = self.destination / "bundle/copy.txt"
        current = self.destination / "bundle/links/current"
        self.assertEqual(payload.read_bytes(), b"trusted payload")
        self.assertEqual(copy.read_bytes(), b"trusted payload")
        self.assertEqual(payload.stat().st_ino, copy.stat().st_ino)
        self.assertTrue(os.access(payload, os.X_OK))
        self.assertTrue(current.is_symlink())
        self.assertEqual(os.readlink(current), "../payload.txt")
        self.assertEqual(current.read_bytes(), b"trusted payload")

    def test_partial_tree_is_removed_when_stream_read_fails(self):
        first, first_data = self._file("bundle/first.txt", b"first")
        second, _second_data = self._file("bundle/second.txt", b"second")

        class BrokenArchive:
            @staticmethod
            def getmembers():
                return [first, second]

            @staticmethod
            def extractfile(member):
                if member is second:
                    raise tarfile.ReadError("simulated truncated archive")
                return io.BytesIO(first_data)

        with self.assertRaises(tarfile.ReadError):
            safe_extract_tar(BrokenArchive(), self.destination)

        self.assertFalse(self.destination.exists())


class NetworkArchiveIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.base = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    @staticmethod
    def _write_traversal_archive(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = b"malicious"
        member = tarfile.TarInfo("../outside.txt")
        member.size = len(payload)
        with tarfile.open(path, "w:gz") as archive:
            archive.addfile(member, io.BytesIO(payload))

    @staticmethod
    def _write_xcurl_archive(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(path, "w:gz") as archive:
            for name, payload in (
                    ("libcurl-4.dll", b"curl"),
                    ("xcurl-cashim.dll", b"shim")):
                member = tarfile.TarInfo(name)
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))

    @staticmethod
    def _write_umu_archive(path, launcher=b"umu launcher"):
        path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(path, "w") as archive:
            directory = tarfile.TarInfo("umu/")
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o755
            archive.addfile(directory)
            member = tarfile.TarInfo("umu/umu-run")
            member.mode = 0o755
            member.size = len(launcher)
            archive.addfile(member, io.BytesIO(launcher))

    def test_bad_umu_update_keeps_existing_launcher_and_cleans_staging(self):
        cache = self.base / "cache"
        umu_dir = self.base / "umu"
        umu_dir.mkdir()
        launcher = umu_dir / "umu-run"
        launcher.write_bytes(b"known-good launcher")
        package = cache / "umu-launcher.tar.gz"
        self._write_traversal_archive(package)
        package_sha = hashlib.sha256(package.read_bytes()).hexdigest()

        with mock.patch.object(prefix, "CACHE", cache), \
                mock.patch.object(prefix, "UMU_DIR", umu_dir), \
                mock.patch.object(prefix, "UMU_ASSET", package.name), \
                mock.patch.object(
                    prefix, "UMU_ARCHIVE_SHA256", package_sha), \
                mock.patch.object(prefix, "download"):
            with self.assertRaisesRegex(ValueError, "unsafe path"):
                prefix.ensure_umu(force=True)

        self.assertEqual(launcher.read_bytes(), b"known-good launcher")
        self.assertFalse((self.base / "outside.txt").exists())
        self.assertEqual(list(umu_dir.parent.glob(".umu-extract-*")), [])
        self.assertEqual(list(umu_dir.glob(".umu-run-*")), [])

    def test_umu_archive_hash_mismatch_is_rejected_after_download(self):
        cache = self.base / "cache"
        umu_dir = self.base / "umu"
        umu_dir.mkdir()
        launcher = umu_dir / "umu-run"
        launcher.write_bytes(b"known-good launcher")
        package = cache / "umu-launcher.tar"
        package.parent.mkdir()
        package.write_bytes(b"stale archive")

        def bad_download(_url, destination, _label):
            Path(destination).write_bytes(b"wrong downloaded archive")

        with mock.patch.object(prefix, "CACHE", cache), \
                mock.patch.object(prefix, "UMU_DIR", umu_dir), \
                mock.patch.object(prefix, "UMU_ASSET", package.name), \
                mock.patch.object(
                    prefix, "UMU_ARCHIVE_SHA256", "0" * 64
                ), \
                mock.patch.object(
                    prefix, "download", side_effect=bad_download
                ):
            with self.assertRaisesRegex(
                    ValueError, "archive SHA-256 mismatch"):
                prefix.ensure_umu(force=True)

        self.assertEqual(launcher.read_bytes(), b"known-good launcher")
        self.assertFalse(package.exists())

    def test_umu_inner_launcher_hash_mismatch_keeps_existing_launcher(self):
        cache = self.base / "cache"
        umu_dir = self.base / "umu"
        umu_dir.mkdir()
        launcher = umu_dir / "umu-run"
        launcher.write_bytes(b"known-good launcher")
        package = cache / "umu-launcher.tar"
        self._write_umu_archive(package, b"unexpected inner launcher")
        package_hash = hashlib.sha256(package.read_bytes()).hexdigest()

        with mock.patch.object(prefix, "CACHE", cache), \
                mock.patch.object(prefix, "UMU_DIR", umu_dir), \
                mock.patch.object(prefix, "UMU_ASSET", package.name), \
                mock.patch.object(
                    prefix, "UMU_ARCHIVE_SHA256", package_hash
                ), \
                mock.patch.object(prefix, "UMU_RUN_SHA256", "0" * 64), \
                mock.patch.object(prefix, "download") as download:
            with self.assertRaisesRegex(ValueError, "umu-run SHA-256 mismatch"):
                prefix.ensure_umu(force=True)

        download.assert_not_called()
        self.assertEqual(launcher.read_bytes(), b"known-good launcher")
        self.assertEqual(list(umu_dir.parent.glob(".umu-extract-*")), [])
        self.assertEqual(list(umu_dir.glob(".umu-run-*")), [])

    def test_bad_xcurl_archive_keeps_existing_set_and_cleans_staging(self):
        cache = self.base / "cache"
        openssl_set = self.base / "xodus-xcurl/openssl-set"
        openssl_set.mkdir(parents=True)
        libcurl = openssl_set / "libcurl-4.dll"
        shim = openssl_set / "xcurl-cashim.dll"
        libcurl.write_bytes(b"known-good curl")
        shim.write_bytes(b"known-good shim")
        archive_name = (
            "openssl-xcurl-set-" + fixups.OPENSSL_XCURL_REV + ".tar.gz")
        package = cache / archive_name
        self._write_traversal_archive(package)
        package_sha = hashlib.sha256(package.read_bytes()).hexdigest()

        with mock.patch.object(fixups, "CACHE", cache), \
                mock.patch.object(
                    fixups, "OPENSSL_XCURL_SET", openssl_set), \
                mock.patch.object(
                    fixups, "OPENSSL_XCURL_ARCHIVE_SHA256", package_sha), \
                mock.patch.object(fixups, "gh_releases", return_value=[{}]), \
                mock.patch.object(
                    fixups, "asset_url",
                    return_value=("https://invalid/xcurl.tar.gz",
                                  archive_name, None)), \
                mock.patch.object(fixups, "download") as download:
            self.assertTrue(fixups.ensure_openssl_xcurl_set())

        download.assert_called_once()
        self.assertEqual(libcurl.read_bytes(), b"known-good curl")
        self.assertEqual(shim.read_bytes(), b"known-good shim")
        self.assertFalse((self.base / "outside.txt").exists())
        self.assertFalse(package.exists())
        self.assertEqual(
            list(openssl_set.parent.glob(".set-dl-*")), [])

    def test_xcurl_uses_hash_pinned_appimage_sibling_without_network(self):
        candidate = self.base / "candidate"
        candidate.mkdir()
        appimage = candidate / "BedrockOnLinux-1.3.0-x86_64.AppImage"
        appimage.write_bytes(b"launcher fixture")
        archive_name = (
            "openssl-xcurl-set-" + fixups.OPENSSL_XCURL_REV + ".tar.gz")
        package = candidate / archive_name
        self._write_xcurl_archive(package)
        package_sha = hashlib.sha256(package.read_bytes()).hexdigest()
        openssl_set = self.base / "installed/openssl-set"

        with mock.patch.dict(os.environ, {"APPIMAGE": str(appimage)}), \
                mock.patch.object(
                    fixups, "OPENSSL_XCURL_SET", openssl_set), \
                mock.patch.object(
                    fixups, "OPENSSL_XCURL_ARCHIVE_SHA256", package_sha), \
                mock.patch.object(fixups, "gh_releases") as releases:
            self.assertTrue(fixups.ensure_openssl_xcurl_set())

        releases.assert_not_called()
        self.assertEqual((openssl_set / "libcurl-4.dll").read_bytes(), b"curl")
        self.assertEqual((openssl_set / "xcurl-cashim.dll").read_bytes(), b"shim")
        self.assertEqual((openssl_set / ".rev").read_text(),
                         fixups.OPENSSL_XCURL_REV)

    def test_xcurl_replaces_bad_remote_cache_in_the_same_call(self):
        cache = self.base / "cache"
        archive_name = (
            "openssl-xcurl-set-" + fixups.OPENSSL_XCURL_REV + ".tar.gz")
        package = cache / archive_name
        package.parent.mkdir(parents=True)
        package.write_bytes(b"corrupt cached archive")
        reviewed = self.base / "reviewed.tar.gz"
        self._write_xcurl_archive(reviewed)
        package_sha = hashlib.sha256(reviewed.read_bytes()).hexdigest()
        openssl_set = self.base / "installed/openssl-set"

        def download_reviewed(_url, destination, _label):
            destination.write_bytes(reviewed.read_bytes())

        with mock.patch.object(fixups, "CACHE", cache), \
                mock.patch.object(
                    fixups, "OPENSSL_XCURL_SET", openssl_set), \
                mock.patch.object(
                    fixups, "OPENSSL_XCURL_ARCHIVE_SHA256", package_sha), \
                mock.patch.object(fixups, "gh_releases", return_value=[{}]), \
                mock.patch.object(
                    fixups, "asset_url",
                    return_value=("https://invalid/xcurl.tar.gz",
                                  archive_name, None)), \
                mock.patch.object(
                    fixups, "download", side_effect=download_reviewed) as download:
            self.assertTrue(fixups.ensure_openssl_xcurl_set())

        download.assert_called_once()
        self.assertEqual(hashlib.sha256(package.read_bytes()).hexdigest(),
                         package_sha)
        self.assertEqual((openssl_set / "libcurl-4.dll").read_bytes(), b"curl")
        self.assertEqual((openssl_set / "xcurl-cashim.dll").read_bytes(), b"shim")

    def test_xcurl_set_is_found_past_the_newest_thirty_releases(self):
        # Uploaded once, the set stays in its own release while newer ones
        # pile up above it; it was the 38th when fresh installs stopped
        # finding it ("not published yet").
        cache = self.base / "cache"
        archive_name = (
            "openssl-xcurl-set-" + fixups.OPENSSL_XCURL_REV + ".tar.gz")
        reviewed = self.base / "reviewed.tar.gz"
        self._write_xcurl_archive(reviewed)
        package_sha = hashlib.sha256(reviewed.read_bytes()).hexdigest()
        openssl_set = self.base / "installed/openssl-set"
        releases = [{"assets": []} for _ in range(37)] + [{"assets": [{
            "name": archive_name,
            "browser_download_url": "https://invalid/xcurl.tar.gz"}]}]

        def gh_releases(_repo, per_page=100, fetch_all=False,
                        ignore_cache=False):
            return releases if fetch_all else releases[:per_page]

        def download_reviewed(url, destination, _label):
            self.assertEqual(url, "https://invalid/xcurl.tar.gz")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(reviewed.read_bytes())

        with mock.patch.object(fixups, "CACHE", cache), \
                mock.patch.object(
                    fixups, "OPENSSL_XCURL_SET", openssl_set), \
                mock.patch.object(
                    fixups, "OPENSSL_XCURL_ARCHIVE_SHA256", package_sha), \
                mock.patch.object(fixups, "gh_releases", gh_releases), \
                mock.patch.object(
                    fixups, "download", side_effect=download_reviewed):
            self.assertTrue(fixups.ensure_openssl_xcurl_set())

        self.assertEqual((openssl_set / "libcurl-4.dll").read_bytes(), b"curl")

    def test_xcurl_retries_once_after_remote_tar_read_failure(self):
        cache = self.base / "cache"
        archive_name = (
            "openssl-xcurl-set-" + fixups.OPENSSL_XCURL_REV + ".tar.gz")
        package = cache / archive_name
        self._write_xcurl_archive(package)
        package_bytes = package.read_bytes()
        package_sha = hashlib.sha256(package_bytes).hexdigest()
        openssl_set = self.base / "installed/openssl-set"
        real_extract = fixups.safe_extract_tar
        extraction_attempts = 0

        def flaky_extract(archive, destination):
            nonlocal extraction_attempts
            extraction_attempts += 1
            if extraction_attempts == 1:
                raise tarfile.ReadError("simulated truncated cached TAR")
            return real_extract(archive, destination)

        def download_reviewed(_url, destination, _label):
            destination.write_bytes(package_bytes)

        with mock.patch.object(fixups, "CACHE", cache), \
                mock.patch.object(
                    fixups, "OPENSSL_XCURL_SET", openssl_set), \
                mock.patch.object(
                    fixups, "OPENSSL_XCURL_ARCHIVE_SHA256", package_sha), \
                mock.patch.object(fixups, "gh_releases", return_value=[{}]), \
                mock.patch.object(
                    fixups, "asset_url",
                    return_value=("https://invalid/xcurl.tar.gz",
                                  archive_name, None)), \
                mock.patch.object(
                    fixups, "safe_extract_tar", side_effect=flaky_extract), \
                mock.patch.object(
                    fixups, "download", side_effect=download_reviewed) as download:
            self.assertTrue(fixups.ensure_openssl_xcurl_set())

        self.assertEqual(extraction_attempts, 2)
        download.assert_called_once()
        self.assertEqual((openssl_set / "libcurl-4.dll").read_bytes(), b"curl")

    def test_xcurl_never_deletes_or_redownloads_invalid_local_sibling(self):
        candidate = self.base / "candidate"
        candidate.mkdir()
        appimage = candidate / "BedrockOnLinux-1.3.0-x86_64.AppImage"
        appimage.write_bytes(b"launcher fixture")
        archive_name = (
            "openssl-xcurl-set-" + fixups.OPENSSL_XCURL_REV + ".tar.gz")
        package = candidate / archive_name
        original = b"invalid explicit sibling"
        package.write_bytes(original)
        openssl_set = self.base / "installed/openssl-set"

        with mock.patch.dict(os.environ, {"APPIMAGE": str(appimage)}), \
                mock.patch.object(
                    fixups, "OPENSSL_XCURL_SET", openssl_set), \
                mock.patch.object(
                    fixups, "OPENSSL_XCURL_ARCHIVE_SHA256", "0" * 64), \
                mock.patch.object(fixups, "gh_releases") as releases, \
                mock.patch.object(fixups, "download") as download:
            self.assertFalse(fixups.ensure_openssl_xcurl_set())

        self.assertEqual(package.read_bytes(), original)
        releases.assert_not_called()
        download.assert_not_called()

    def test_xcurl_never_deletes_local_sibling_with_invalid_tar(self):
        candidate = self.base / "candidate"
        candidate.mkdir()
        appimage = candidate / "BedrockOnLinux-1.3.0-x86_64.AppImage"
        appimage.write_bytes(b"launcher fixture")
        archive_name = (
            "openssl-xcurl-set-" + fixups.OPENSSL_XCURL_REV + ".tar.gz")
        package = candidate / archive_name
        self._write_traversal_archive(package)
        original = package.read_bytes()
        package_sha = hashlib.sha256(original).hexdigest()
        openssl_set = self.base / "installed/openssl-set"

        with mock.patch.dict(os.environ, {"APPIMAGE": str(appimage)}), \
                mock.patch.object(
                    fixups, "OPENSSL_XCURL_SET", openssl_set), \
                mock.patch.object(
                    fixups, "OPENSSL_XCURL_ARCHIVE_SHA256", package_sha), \
                mock.patch.object(fixups, "gh_releases") as releases, \
                mock.patch.object(fixups, "download") as download:
            self.assertFalse(fixups.ensure_openssl_xcurl_set())

        self.assertEqual(package.read_bytes(), original)
        self.assertFalse((self.base / "outside.txt").exists())
        self.assertEqual(
            list(openssl_set.parent.glob(".set-dl-*")), [])
        releases.assert_not_called()
        download.assert_not_called()


class LibHttpClientRoutingPatchTests(unittest.TestCase):
    PREFIX = (b"\x83\xc0\xfe\xba\x04\x00\x00\x00\x48\x8d\x0d"
              b"\x11\x22\x33\x44\x83\xf8\x06")

    def test_provider_gate_is_patched_once(self):
        with tempfile.TemporaryDirectory() as td:
            game = Path(td)
            dll = game / "libHttpClient.GDK.dll"
            dll.write_bytes(b"header" + self.PREFIX
                            + b"\x0f\x87\x01\x02\x03\x04" + b"tail")
            with mock.patch.object(fixups, "warn") as warn:
                fixups._patch_lhc_xcurl_gate(game)
            self.assertIn(b"\x90" * 6, dll.read_bytes())
            warn.assert_not_called()

    def test_already_patched_provider_gate_is_silent(self):
        with tempfile.TemporaryDirectory() as td:
            game = Path(td)
            dll = game / "libHttpClient.GDK.dll"
            original = b"header" + self.PREFIX + b"\x90" * 6 + b"tail"
            dll.write_bytes(original)
            with mock.patch.object(fixups, "warn") as warn, \
                    mock.patch.object(fixups, "apply_patch") as apply:
                fixups._patch_lhc_xcurl_gate(game)
            self.assertEqual(dll.read_bytes(), original)
            warn.assert_not_called()
            apply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
