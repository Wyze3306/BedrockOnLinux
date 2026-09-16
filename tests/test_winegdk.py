"""Regression tests for managed WineGDK engine installation and updates."""

import io
import hashlib
import os
import shutil
import sys
import tarfile
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from bol import proton, winegdk
from bol.log import BolError


class ManagedProtonTests(unittest.TestCase):
    def test_legacy_canonical_winegdk_path_is_not_custom(self):
        canonical = Path("/tmp/bol-managed/GDK-Proton-xuser")
        settings = {
            "proton_source": "winegdk",
            "proton_dir": str(canonical),
            "proton_url": "https://legacy.invalid/engine.tar.gz",
        }
        with mock.patch.object(proton, "WINEGDK_OUT", canonical), \
                mock.patch.object(proton, "load_settings", return_value=settings):
            self.assertFalse(proton.custom_proton())

    def test_canonical_path_without_source_is_migrated_as_managed(self):
        canonical = Path("/tmp/bol-managed/GDK-Proton-xuser")
        with mock.patch.object(proton, "WINEGDK_OUT", canonical), \
                mock.patch.object(
                    proton, "load_settings",
                    return_value={"proton_dir": str(canonical)}):
            self.assertFalse(proton.custom_proton())

    def test_managed_engine_keeps_relaxed_binary_patching(self):
        with tempfile.TemporaryDirectory() as td:
            canonical = Path(td) / "GDK-Proton-xuser"
            canonical.mkdir()
            with mock.patch.object(proton, "WINEGDK_OUT", canonical), \
                    mock.patch.object(proton, "die") as die, \
                    mock.patch.object(proton, "warn") as warn:
                proton.patch_proton(canonical, strict=True)

        die.assert_not_called()
        warn.assert_called_once()


class WineGDKInstallTests(unittest.TestCase):
    REV = "wow64-archs-r12"

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.base = Path(self._td.name)
        self.proton_dir = self.base / "proton"
        self.engine = self.proton_dir / "GDK-Proton-xuser"
        self.cache = self.base / "cache"
        self.validator = mock.Mock(name="validate_engine_manifest")
        fake_vkd3d = types.ModuleType("bol.vkd3d")
        fake_vkd3d.validate_engine_manifest = self.validator

        self._patches = [
            mock.patch.object(winegdk, "PROTON_DIR", self.proton_dir),
            mock.patch.object(winegdk, "WINEGDK_OUT", self.engine),
            mock.patch.object(winegdk, "CACHE", self.cache),
            mock.patch.object(winegdk, "WINEGDK_BUILD_REV", self.REV),
            mock.patch.object(winegdk, "WINEGDK_ARCHIVE_SHA256", "0" * 64),
            mock.patch.dict(sys.modules, {"bol.vkd3d": fake_vkd3d}),
            mock.patch.dict(os.environ, {"BOL_ENGINE_ARCHIVE": ""}),
        ]
        for patcher in self._patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self._patches):
            patcher.stop()
        self._td.cleanup()

    def _write_engine(self, root, marker):
        root.mkdir(parents=True, exist_ok=True)
        (root / "files").mkdir()
        (root / "proton").write_text(marker)

    def _archive(self, name="engine.tar.gz", marker="new"):
        source = self.base / (name + "-source") / "GDK-Proton-xuser"
        self._write_engine(source, marker)
        archive = self.base / name
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(source, arcname=source.name)
        winegdk.WINEGDK_ARCHIVE_SHA256 = hashlib.sha256(
            archive.read_bytes()).hexdigest()
        return archive

    def test_archive_path_escape_is_rejected(self):
        archive = self.base / "escape.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            member = tarfile.TarInfo("../outside-engine")
            payload = b"escape"
            member.size = len(payload)
            tf.addfile(member, io.BytesIO(payload))
        destination = self.base / "extract"
        destination.mkdir()

        with tarfile.open(archive) as tf, self.assertRaises(
                (tarfile.TarError, ValueError)):
            winegdk._extract_archive(tf, destination)
        self.assertFalse((self.base / "outside-engine").exists())

    def test_archive_symlink_escape_is_rejected(self):
        archive = self.base / "symlink-escape.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            member = tarfile.TarInfo("engine/escape")
            member.type = tarfile.SYMTYPE
            member.linkname = "../../outside-engine"
            tf.addfile(member)
        destination = self.base / "extract"
        destination.mkdir()

        with tarfile.open(archive) as tf, self.assertRaisesRegex(
                ValueError, "unsafe symlink"):
            winegdk._extract_archive(tf, destination)

    def test_archive_member_below_symlink_is_rejected(self):
        archive = self.base / "nested-symlink.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            link = tarfile.TarInfo("engine/link")
            link.type = tarfile.SYMTYPE
            link.linkname = "target"
            tf.addfile(link)
            payload = b"must not be written through a link"
            nested = tarfile.TarInfo("engine/link/file")
            nested.size = len(payload)
            tf.addfile(nested, io.BytesIO(payload))
        destination = self.base / "extract"
        destination.mkdir()

        with tarfile.open(archive) as tf, self.assertRaisesRegex(
                ValueError, "nested below a symlink"):
            winegdk._extract_archive(tf, destination)

    def test_archive_hardlink_is_rejected_on_every_python_version(self):
        archive = self.base / "hardlink.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            payload = b"target"
            target = tarfile.TarInfo("engine/target")
            target.size = len(payload)
            tf.addfile(target, io.BytesIO(payload))
            link = tarfile.TarInfo("engine/link")
            link.type = tarfile.LNKTYPE
            link.linkname = "engine/target"
            tf.addfile(link)
        destination = self.base / "extract"
        destination.mkdir()

        with tarfile.open(archive) as tf, self.assertRaisesRegex(
                ValueError, "unsupported entry"):
            winegdk._extract_archive(tf, destination)

    def test_wire_removes_legacy_custom_fields(self):
        settings = {
            "proton_dir": str(self.engine),
            "proton_url": "https://legacy.invalid/engine.tar.gz",
            "winegdk_built": "prebuilt:old",
        }
        with mock.patch.object(winegdk, "load_settings", return_value=settings), \
                mock.patch.object(winegdk, "save_settings") as save, \
                mock.patch.object(winegdk, "patch_proton"):
            self.assertEqual(winegdk._wire_winegdk(), self.engine)

        saved = save.call_args.args[0]
        self.assertEqual(saved["proton_source"], "winegdk")
        self.assertEqual(saved["proton"], str(self.engine))
        self.assertNotIn("proton_dir", saved)
        self.assertNotIn("proton_url", saved)

    def test_local_archive_is_validated_and_activated_without_network(self):
        self._write_engine(self.engine, "old")
        archive = self._archive(marker="new")

        with mock.patch.dict(os.environ, {"BOL_ENGINE_ARCHIVE": str(archive)}), \
                mock.patch.object(winegdk, "download") as download:
            installed = winegdk._install_prebuilt_winegdk(force=True)

        self.assertTrue(installed)
        self.assertEqual((self.engine / "proton").read_text(), "new")
        self.assertTrue(archive.is_file(), "force must not delete a local archive")
        download.assert_not_called()
        self.assertEqual(self.validator.call_count, 1)
        self.assertEqual(self.validator.call_args.args[1], self.REV)
        self.assertTrue(self.validator.call_args.kwargs["enforce_pins"])

    def test_appimage_automatically_uses_exact_sibling_engine(self):
        asset = f"GDK-Proton-xuser-{self.REV}.tar.gz"
        archive = self._archive(name=asset, marker="sibling-r12")
        appimage = self.base / "BedrockOnLinux-1.3.0-x86_64.AppImage"
        appimage.write_bytes(b"fixture")

        with mock.patch.dict(
                os.environ,
                {"BOL_ENGINE_ARCHIVE": "", "APPIMAGE": str(appimage)}), \
                mock.patch.object(winegdk, "download") as download:
            installed = winegdk._install_prebuilt_winegdk()

        self.assertTrue(installed)
        self.assertEqual((self.engine / "proton").read_text(), "sibling-r12")
        self.assertEqual(archive.parent, appimage.parent)
        download.assert_not_called()

    def test_remote_download_ignores_stale_release_metadata(self):
        fresh = self._archive(name="fresh.tar.gz", marker="fresh")
        asset = f"GDK-Proton-xuser-{self.REV}.tar.gz"
        stale_metadata = self.cache / (
            "releases_Wyze3306_BedrockOnLinux_30.json")
        stale_metadata.parent.mkdir(parents=True)
        stale_metadata.write_text(
            '[{"tag_name":"engine-old","assets":[]}]',
            encoding="utf-8",
        )

        def download_fresh(_url, dest, _label, _progress):
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(fresh, dest)

        with mock.patch.object(
                winegdk, "download",
                side_effect=download_fresh) as download:
            installed = winegdk._install_prebuilt_winegdk()

        self.assertTrue(installed)
        download.assert_called_once_with(
            "https://github.com/Wyze3306/BedrockOnLinux/releases/download/"
            f"engine-{self.REV}/{asset}",
            self.cache / asset,
            "Game engine",
            None,
        )
        self.assertEqual((self.engine / "proton").read_text(), "fresh")
        self.assertTrue(stale_metadata.is_file())

    def test_download_prunes_the_archives_of_other_engine_revisions(self):
        """An update must not need room for every engine it ever installed."""
        fresh = self._archive(name="fresh.tar.gz", marker="fresh")
        asset = f"GDK-Proton-xuser-{self.REV}.tar.gz"
        self.cache.mkdir(parents=True)
        stale = [
            self.cache / "GDK-Proton-xuser-wow64-archs-native13.tar.gz",
            self.cache / "GDK-Proton-xuser-wow64-archs-native17.tar.gz",
            self.cache / "GDK-Proton-xuser-wow64-archs-native17.tar.gz.part",
        ]
        kept = [
            self.cache / "umu-launcher-1.2.9-zipapp.tar",
            self.cache / "gdk-links.json",
            self.cache / "GDK-Proton-xuser-notes.txt",
        ]
        for path in stale + kept:
            path.write_bytes(b"cached")

        def download_fresh(_url, dest, _label, _progress):
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(fresh, dest)

        with mock.patch.object(winegdk, "download",
                               side_effect=download_fresh), \
                mock.patch.object(winegdk, "info") as said:
            installed = winegdk._install_prebuilt_winegdk()

        self.assertTrue(installed)
        for path in stale:
            self.assertFalse(path.exists(), path.name)
        for path in kept:
            self.assertTrue(path.exists(), path.name)
        self.assertTrue((self.cache / asset).is_file())
        self.assertTrue(any("older game engines" in call.args[0]
                            for call in said.call_args_list))

    def test_download_keeps_the_pinned_archive_it_can_reuse(self):
        archive = self._archive(name="cached.tar.gz", marker="cached")
        asset = f"GDK-Proton-xuser-{self.REV}.tar.gz"
        self.cache.mkdir(parents=True)
        shutil.copyfile(archive, self.cache / asset)
        old = self.cache / "GDK-Proton-xuser-wow64-archs-native16.tar.gz"
        old.write_bytes(b"old")

        with mock.patch.object(winegdk, "download") as download:
            installed = winegdk._install_prebuilt_winegdk()

        self.assertTrue(installed)
        download.assert_not_called()
        self.assertTrue((self.cache / asset).is_file())
        self.assertFalse(old.exists())

    def test_a_local_archive_leaves_the_cache_alone(self):
        archive = self._archive(marker="local")
        self.cache.mkdir(parents=True)
        old = self.cache / "GDK-Proton-xuser-wow64-archs-native16.tar.gz"
        old.write_bytes(b"old")

        with mock.patch.dict(os.environ, {"BOL_ENGINE_ARCHIVE": str(archive)}):
            installed = winegdk._install_prebuilt_winegdk()

        self.assertTrue(installed)
        self.assertTrue(old.exists())

    def test_force_deletes_cached_archive_and_downloads_again(self):
        self._write_engine(self.engine, "old")
        fresh = self._archive(name="fresh.tar.gz", marker="fresh")
        asset = f"GDK-Proton-xuser-{self.REV}.tar.gz"
        cached = self.cache / asset
        cached.parent.mkdir(parents=True)
        cached.write_bytes(b"stale cached bytes")
        cached.with_suffix(cached.suffix + ".part").write_bytes(b"stale part")

        def download_fresh(_url, dest, _label, _progress):
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(fresh, dest)

        with mock.patch.object(winegdk, "download",
                               side_effect=download_fresh) as download:
            installed = winegdk._install_prebuilt_winegdk(force=True)

        self.assertTrue(installed)
        download.assert_called_once()
        self.assertFalse(cached.with_suffix(cached.suffix + ".part").exists())
        self.assertEqual((self.engine / "proton").read_text(), "fresh")

    def test_remote_download_failure_keeps_current_engine(self):
        self._write_engine(self.engine, "known-good")

        with mock.patch.object(
                winegdk, "download",
                side_effect=OSError("simulated network failure")):
            installed = winegdk._install_prebuilt_winegdk(force=True)

        self.assertFalse(installed)
        self.assertEqual((self.engine / "proton").read_text(), "known-good")
        self.validator.assert_not_called()

    def test_manifest_failure_keeps_current_engine(self):
        self._write_engine(self.engine, "known-good")
        archive = self._archive(marker="bad-update")
        self.validator.side_effect = ValueError("wrong vkd3d version")

        with mock.patch.dict(os.environ, {"BOL_ENGINE_ARCHIVE": str(archive)}):
            installed = winegdk._install_prebuilt_winegdk(force=True)

        self.assertFalse(installed)
        self.assertEqual((self.engine / "proton").read_text(), "known-good")
        self.assertFalse(self.engine.with_name(
            "." + self.engine.name + ".rollback").exists())

    def test_archive_hash_mismatch_keeps_current_engine(self):
        self._write_engine(self.engine, "known-good")
        archive = self._archive(marker="untrusted-update")
        winegdk.WINEGDK_ARCHIVE_SHA256 = "0" * 64

        with mock.patch.dict(os.environ, {"BOL_ENGINE_ARCHIVE": str(archive)}):
            installed = winegdk._install_prebuilt_winegdk(force=True)

        self.assertFalse(installed)
        self.assertEqual((self.engine / "proton").read_text(), "known-good")
        self.assertTrue(archive.is_file(), "a local candidate belongs to the user")
        self.validator.assert_not_called()

    def test_activation_failure_rolls_back_current_engine(self):
        self._write_engine(self.engine, "known-good")
        candidate = self.base / "candidate"
        self._write_engine(candidate, "candidate")
        original_replace = Path.replace

        def fail_candidate(path, target):
            if path == candidate:
                raise OSError("simulated rename failure")
            return original_replace(path, target)

        with mock.patch.object(Path, "replace", autospec=True,
                               side_effect=fail_candidate):
            with self.assertRaises(OSError):
                winegdk._activate_engine_locked(candidate)

        self.assertEqual((self.engine / "proton").read_text(), "known-good")
        self.assertTrue(candidate.exists())
        self.assertFalse(self.engine.with_name(
            "." + self.engine.name + ".rollback").exists())

    def test_rollback_cleanup_failure_does_not_reject_active_candidate(self):
        self._write_engine(self.engine, "known-good")
        candidate = self.base / "candidate"
        self._write_engine(candidate, "candidate")
        rollback = self.engine.with_name(
            "." + self.engine.name + ".rollback")
        original_remove = winegdk.remove_path

        def fail_rollback_cleanup(path):
            if Path(path) == rollback:
                raise OSError("simulated cleanup failure")
            return original_remove(path)

        with mock.patch.object(
                winegdk, "remove_path", side_effect=fail_rollback_cleanup):
            winegdk._activate_engine_locked(candidate)

        self.assertEqual((self.engine / "proton").read_text(), "candidate")
        self.assertEqual((rollback / "proton").read_text(), "known-good")
        self.assertFalse(candidate.exists())

    def _accept_only_marker(self, accepted):
        def validate(root, _revision, **_kwargs):
            marker = (Path(root) / "proton").read_text()
            if marker != accepted:
                raise ValueError("manifest marker is " + marker)
        self.validator.side_effect = validate

    def test_offline_start_restores_valid_interrupted_rollback(self):
        rollback = self.engine.with_name(
            "." + self.engine.name + ".rollback")
        self._write_engine(rollback, "current-r11")
        self._accept_only_marker("current-r11")
        settings = {"winegdk_built": "prebuilt:" + self.REV}

        with mock.patch.object(winegdk, "load_settings",
                               return_value=settings), \
                mock.patch.object(winegdk, "_install_prebuilt_winegdk") \
                as install, \
                mock.patch.object(winegdk, "_wire_winegdk",
                                  return_value=self.engine):
            result = winegdk.ensure_winegdk()

        self.assertEqual(result, self.engine)
        self.assertEqual((self.engine / "proton").read_text(), "current-r11")
        self.assertFalse(rollback.exists())
        install.assert_not_called()

    def test_valid_active_engine_discards_stale_interrupted_rollback(self):
        self._write_engine(self.engine, "current-r11")
        rollback = self.engine.with_name(
            "." + self.engine.name + ".rollback")
        self._write_engine(rollback, "stale-r10")
        self._accept_only_marker("current-r11")
        settings = {"winegdk_built": "prebuilt:" + self.REV}

        with mock.patch.object(winegdk, "load_settings",
                               return_value=settings), \
                mock.patch.object(winegdk, "_install_prebuilt_winegdk") \
                as install, \
                mock.patch.object(winegdk, "_wire_winegdk",
                                  return_value=self.engine):
            result = winegdk.ensure_winegdk()

        self.assertEqual(result, self.engine)
        self.assertEqual((self.engine / "proton").read_text(), "current-r11")
        self.assertFalse(rollback.exists())
        install.assert_not_called()

    def test_valid_rollback_replaces_invalid_interrupted_active_tree(self):
        self._write_engine(self.engine, "corrupt-active")
        rollback = self.engine.with_name(
            "." + self.engine.name + ".rollback")
        self._write_engine(rollback, "current-r11")
        self._accept_only_marker("current-r11")
        settings = {"winegdk_built": "prebuilt:" + self.REV}

        with mock.patch.object(winegdk, "load_settings",
                               return_value=settings), \
                mock.patch.object(winegdk, "_install_prebuilt_winegdk") \
                as install, \
                mock.patch.object(winegdk, "_wire_winegdk",
                                  return_value=self.engine):
            result = winegdk.ensure_winegdk()

        self.assertEqual(result, self.engine)
        self.assertEqual((self.engine / "proton").read_text(), "current-r11")
        self.assertFalse(rollback.exists())
        install.assert_not_called()

    def test_invalid_active_and_rollback_are_preserved_until_replacement(self):
        self._write_engine(self.engine, "corrupt-active")
        rollback = self.engine.with_name(
            "." + self.engine.name + ".rollback")
        self._write_engine(rollback, "stale-r10")
        self._accept_only_marker("never")
        settings = {"winegdk_built": "prebuilt:" + self.REV}

        with mock.patch.object(winegdk, "load_settings",
                               return_value=settings), \
                mock.patch.object(winegdk, "_install_prebuilt_winegdk",
                                  return_value=False) as install:
            with self.assertRaises(BolError):
                winegdk.ensure_winegdk()

        self.assertEqual((self.engine / "proton").read_text(),
                         "corrupt-active")
        self.assertEqual((rollback / "proton").read_text(), "stale-r10")
        install.assert_called_once_with(None, force=False)

    def test_unavailable_update_keeps_current_without_source_fallback(self):
        self._write_engine(self.engine, "known-good")
        settings = {"winegdk_built": "prebuilt:wow64-archs-r8"}
        with mock.patch.object(winegdk, "load_settings", return_value=settings), \
                mock.patch.object(winegdk, "_install_prebuilt_winegdk",
                                  return_value=False), \
                mock.patch.object(winegdk, "_wire_winegdk",
                                  return_value=self.engine) as wire:
            result = winegdk.ensure_winegdk(force=True)

        self.assertEqual(result, self.engine)
        self.assertEqual((self.engine / "proton").read_text(), "known-good")
        wire.assert_called_once_with()

    def test_matching_settings_do_not_hide_stale_installed_manifest(self):
        self._write_engine(self.engine, "stale-r10")
        settings = {"winegdk_built": "prebuilt:" + self.REV}
        self.validator.side_effect = ValueError("manifest has r10")

        with mock.patch.object(winegdk, "load_settings", return_value=settings), \
                mock.patch.object(winegdk, "_install_prebuilt_winegdk",
                                  return_value=False) as install, \
                mock.patch.object(winegdk, "_wire_winegdk",
                                  return_value=self.engine):
            with self.assertRaisesRegex(
                    BolError, "Flatpak and packaged-app users"):
                winegdk.ensure_winegdk()

        install.assert_called_once_with(None, force=False)

    def test_valid_manifest_repairs_stale_settings_without_downloading(self):
        self._write_engine(self.engine, "current-r11")
        settings = {"winegdk_built": "prebuilt:wow64-archs-r10"}

        with mock.patch.object(winegdk, "load_settings", return_value=settings), \
                mock.patch.object(winegdk, "save_settings") as save, \
                mock.patch.object(winegdk, "_install_prebuilt_winegdk") as install, \
                mock.patch.object(winegdk, "_wire_winegdk",
                                  return_value=self.engine):
            result = winegdk.ensure_winegdk()

        self.assertEqual(result, self.engine)
        install.assert_not_called()
        self.validator.assert_called_once()
        self.assertEqual(save.call_args.args[0]["winegdk_built"],
                         "prebuilt:" + self.REV)


if __name__ == "__main__":
    unittest.main()
