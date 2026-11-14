"""Unit tests for universal DGC engine validation and activation."""
# SPDX-License-Identifier: MIT

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol.log import BolError
from bol import vkd3d


def _digest(data):
    return hashlib.sha256(data).hexdigest()


class EngineFixture:
    BUILD_REV = "test-r9"
    WINEGDK_SOURCE_MANIFEST = (
        vkd3d.REQUIRED_WINEGDK_SOURCE_MANIFEST_PATH
    )
    WINEGDK_PROVENANCE = (
        "files/share/bedrock-on-linux/licenses-and-provenance/winegdk/"
        ".bol-winegdk-build.env"
    )
    VKD3D_PROVENANCE = (
        "files/share/bedrock-on-linux/licenses-and-provenance/"
        "vkd3d-proton/provenance.env"
    )
    GDK_PROTON_PROVENANCE = (
        "files/share/bedrock-on-linux/licenses-and-provenance/"
        "gdk-proton-base/provenance.env"
    )

    def make_engine(self):
        source_manifest_data = (
            "critical:" + self.WINEGDK_SOURCE_MANIFEST
        ).encode("utf-8")
        source_pin = mock.patch.object(
            vkd3d,
            "WINEGDK_SOURCE_MANIFEST_SHA256",
            _digest(source_manifest_data),
        )
        source_pin.start()
        self.addCleanup(source_pin.stop)

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "engine"
        root.mkdir()
        variants = {}
        self.variant_contents = {}
        for variant in vkd3d.VARIANTS:
            files = {}
            contents = {}
            for relative in vkd3d._variant_relative_paths(variant):
                data = (variant + ":" + relative).encode("utf-8")
                path = root.joinpath(*Path(relative).parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                files[relative] = _digest(data)
                contents[relative] = data
            variants[variant] = {"files": files}
            self.variant_contents[variant] = contents

        critical_paths = tuple(vkd3d.REQUIRED_CRITICAL_FILE_PATHS) + (
            self.WINEGDK_SOURCE_MANIFEST,
            self.WINEGDK_PROVENANCE,
            self.VKD3D_PROVENANCE,
            self.GDK_PROTON_PROVENANCE,
        )
        for relative in critical_paths:
            if relative in vkd3d.REQUIRED_RUNTIME_LINK_TARGETS:
                continue
            data = ("critical:" + relative).encode("utf-8")
            path = root.joinpath(*Path(relative).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        # The production archive intentionally uses these launch aliases. Keep
        # fixture topology realistic so manifest validation covers the links as
        # well as their resolved bytes.
        unwind_target = (root / "files/lib/x86_64-linux-gnu/"
                         "libunwind.so.8.0.1")
        unwind_target.parent.mkdir(parents=True, exist_ok=True)
        unwind_target.write_bytes(b"fixture-libunwind")
        for relative, target in vkd3d.REQUIRED_RUNTIME_LINK_TARGETS.items():
            path = root.joinpath(*Path(relative).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(target)
        for relative in vkd3d.REQUIRED_EXECUTABLE_FILE_PATHS:
            path = root.joinpath(*Path(relative).parts)
            path.chmod(path.stat().st_mode | 0o100)
        critical_files = {
            relative: _digest(root.joinpath(*Path(relative).parts).read_bytes())
            for relative in critical_paths
        }
        manifest = {
            "schema": 1,
            "build_rev": self.BUILD_REV,
            "components": dict(vkd3d.REQUIRED_COMPONENTS),
            "provenance": {
                "winegdk": {
                    "commit": vkd3d.WINEGDK_SOURCE_COMMIT,
                    "distribution_files": {
                        self.WINEGDK_SOURCE_MANIFEST:
                            critical_files[self.WINEGDK_SOURCE_MANIFEST],
                        self.WINEGDK_PROVENANCE:
                            critical_files[self.WINEGDK_PROVENANCE],
                    },
                },
                "vkd3d_universal": {
                    "base_commit": vkd3d.REQUIRED_VKD3D_BASE_COMMIT,
                    "restored_by_reverting": vkd3d.REQUIRED_VKD3D_REVERT,
                    "distribution_files": {
                        self.VKD3D_PROVENANCE:
                            critical_files[self.VKD3D_PROVENANCE],
                    },
                },
                "gdk_proton_base": {
                    "repository":
                        vkd3d.REQUIRED_GDK_PROTON_BASE_REPOSITORY,
                    "release": vkd3d.REQUIRED_GDK_PROTON_BASE_RELEASE,
                    "archive": vkd3d.REQUIRED_GDK_PROTON_BASE_ARCHIVE,
                    "archive_sha256":
                        vkd3d.REQUIRED_GDK_PROTON_BASE_ARCHIVE_SHA256,
                    "threading_dll_sha256":
                        vkd3d.XGAMERUNTIME_THREADING_SHA256,
                    "distribution_files": {
                        self.GDK_PROTON_PROVENANCE:
                            critical_files[self.GDK_PROTON_PROVENANCE],
                    },
                },
                "build": {"glibc_max": vkd3d.REQUIRED_ENGINE_GLIBC_MAX},
            },
            "critical_files": critical_files,
            "variants": variants,
        }
        manifest_path = root / "engine-manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.root = root
        self.manifest_path = manifest_path
        return root

    def read_manifest(self):
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def write_manifest(self, manifest):
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def seed_active_targets(self, prefix=b"old"):
        previous = {}
        for arch in vkd3d.ARCHES:
            for dll in vkd3d.DLL_NAMES:
                target = (self.root / "files/lib/wine/vkd3d-proton" /
                          arch / dll)
                data = prefix + b":" + arch.encode() + b":" + dll.encode()
                target.write_bytes(data)
                previous[target] = data
        return previous


class ManifestValidationTests(EngineFixture, unittest.TestCase):
    def test_gdk_proton_base_hash_pins_are_exact_sha256(self):
        self.assertEqual(
            vkd3d.REQUIRED_GDK_PROTON_BASE_ARCHIVE_SHA256,
            "1e80f4e714f877f42101d5775bd38ca0a15a38d304e24af1f15c6deec4ebac2d",
        )
        self.assertTrue(vkd3d._valid_sha256(
            vkd3d.REQUIRED_GDK_PROTON_BASE_ARCHIVE_SHA256))
        self.assertTrue(vkd3d._valid_sha256(
            vkd3d.XGAMERUNTIME_THREADING_SHA256))

    def test_valid_schema_one_manifest_and_hashes(self):
        root = self.make_engine()
        result = vkd3d.validate_engine_manifest(
            root, self.BUILD_REV, required_variant=vkd3d.EXT_DGC)
        self.assertEqual(result.build_rev, self.BUILD_REV)
        self.assertEqual(result.components, vkd3d.REQUIRED_COMPONENTS)
        self.assertEqual(
            set(result.critical_files),
            set(vkd3d.REQUIRED_CRITICAL_FILE_PATHS) |
            {self.WINEGDK_SOURCE_MANIFEST, self.WINEGDK_PROVENANCE,
             self.VKD3D_PROVENANCE,
             self.GDK_PROTON_PROVENANCE})
        self.assertEqual(set(result.variants), set(vkd3d.VARIANTS))
        self.assertEqual(len(result.variants[vkd3d.EXT_DGC]), 4)

    def test_critical_files_must_be_a_non_empty_object(self):
        root = self.make_engine()
        for invalid in (None, [], {}):
            with self.subTest(value=invalid):
                manifest = self.read_manifest()
                manifest["critical_files"] = invalid
                self.write_manifest(manifest)
                with self.assertRaisesRegex(
                        BolError, "critical_files must be a non-empty object"):
                    vkd3d.validate_engine_manifest(root, self.BUILD_REV)

    def test_all_required_runtime_files_must_be_declared(self):
        root = self.make_engine()
        manifest = self.read_manifest()
        del manifest["critical_files"]["files/bin/wine"]
        self.write_manifest(manifest)
        with self.assertRaisesRegex(
                BolError, "critical_files does not list required file .*wine"):
            vkd3d.validate_engine_manifest(root, self.BUILD_REV)

    def test_provenance_distribution_files_are_required_and_critical(self):
        root = self.make_engine()
        manifest = self.read_manifest()
        del manifest["critical_files"][self.WINEGDK_PROVENANCE]
        self.write_manifest(manifest)
        with self.assertRaisesRegex(
                BolError, "critical_files does not list required file"):
            vkd3d.validate_engine_manifest(root, self.BUILD_REV)

        root = self.make_engine()
        manifest = self.read_manifest()
        manifest["provenance"]["vkd3d_universal"][
            "distribution_files"] = {}
        self.write_manifest(manifest)
        with self.assertRaisesRegex(
                BolError, "distribution_files must be a non-empty object"):
            vkd3d.validate_engine_manifest(root, self.BUILD_REV)

    def test_winegdk_source_manifest_identity_is_pinned(self):
        root = self.make_engine()
        manifest = self.read_manifest()
        manifest["provenance"]["winegdk"]["distribution_files"][
            self.WINEGDK_SOURCE_MANIFEST
        ] = "80a3d59d35ab642ec1f2546aa6ad2c180785f0e577f30d8eccdda2066d80c72e"
        self.write_manifest(manifest)
        with self.assertRaisesRegex(BolError, "source-manifest mismatch"):
            vkd3d.validate_engine_manifest(root, self.BUILD_REV)

    def test_winegdk_source_manifest_identity_is_required(self):
        root = self.make_engine()
        manifest = self.read_manifest()
        del manifest["provenance"]["winegdk"]["distribution_files"][
            self.WINEGDK_SOURCE_MANIFEST
        ]
        self.write_manifest(manifest)
        with self.assertRaisesRegex(BolError, "source-manifest mismatch"):
            vkd3d.validate_engine_manifest(root, self.BUILD_REV)

    def test_critical_file_rejects_unsafe_additional_path(self):
        root = self.make_engine()
        manifest = self.read_manifest()
        manifest["critical_files"]["../escape"] = "0" * 64
        self.write_manifest(manifest)
        with self.assertRaisesRegex(BolError, "Unsafe path"):
            vkd3d.validate_engine_manifest(root, self.BUILD_REV)

    def test_critical_file_rejects_malformed_hash(self):
        root = self.make_engine()
        manifest = self.read_manifest()
        manifest["critical_files"]["files/bin/wine"] = "not-a-sha256"
        self.write_manifest(manifest)
        with self.assertRaisesRegex(BolError, "Invalid SHA-256.*critical_files"):
            vkd3d.validate_engine_manifest(root, self.BUILD_REV)

    def test_critical_file_must_exist_and_remain_inside_engine(self):
        root = self.make_engine()
        manifest = self.read_manifest()
        manifest["critical_files"]["missing-safe-file"] = "0" * 64
        self.write_manifest(manifest)
        with self.assertRaisesRegex(BolError, "missing or escapes the engine"):
            vkd3d.validate_engine_manifest(root, self.BUILD_REV)

        root = self.make_engine()
        outside = root.parent / "outside-engine"
        outside.write_bytes(b"outside")
        escaped = root / "escaped-link"
        escaped.symlink_to(outside)
        manifest = self.read_manifest()
        manifest["critical_files"]["escaped-link"] = _digest(b"outside")
        self.write_manifest(manifest)
        with self.assertRaisesRegex(BolError, "missing or escapes the engine"):
            vkd3d.validate_engine_manifest(root, self.BUILD_REV)

    def test_unchanged_files_are_hashed_once_per_process(self):
        root = self.make_engine()
        with mock.patch.object(vkd3d, "sha256_file",
                               wraps=vkd3d.sha256_file) as hashed:
            first = vkd3d.validate_engine_manifest(root, self.BUILD_REV)
            hashes = hashed.call_count
            self.assertGreater(hashes, 0)
            again = vkd3d.validate_engine_manifest(root, self.BUILD_REV)
            self.assertIs(again, first)
            self.assertEqual(hashed.call_count, hashes)

    def test_a_file_changed_since_is_validated_again(self):
        root = self.make_engine()
        vkd3d.validate_engine_manifest(root, self.BUILD_REV)
        wine = root / "files/bin/wine"
        data = bytearray(wine.read_bytes())
        data[0] ^= 0xFF
        wine.write_bytes(bytes(data))
        with self.assertRaisesRegex(
                BolError, "SHA-256 mismatch for critical file files/bin/wine"):
            vkd3d.validate_engine_manifest(root, self.BUILD_REV)
        # A refusal is never remembered.
        with self.assertRaisesRegex(BolError, "SHA-256 mismatch"):
            vkd3d.validate_engine_manifest(root, self.BUILD_REV)

    def test_a_mode_lost_since_is_validated_again(self):
        root = self.make_engine()
        vkd3d.validate_engine_manifest(root, self.BUILD_REV)
        wine = root / "files/bin/wine"
        wine.chmod(wine.stat().st_mode & ~0o111)
        with self.assertRaisesRegex(BolError, "not executable"):
            vkd3d.validate_engine_manifest(root, self.BUILD_REV)

    def test_critical_file_hash_mismatch_is_rejected(self):
        root = self.make_engine()
        manifest = self.read_manifest()
        manifest["critical_files"]["files/bin/wine"] = "0" * 64
        self.write_manifest(manifest)
        with self.assertRaisesRegex(
                BolError, "SHA-256 mismatch for critical file files/bin/wine"):
            vkd3d.validate_engine_manifest(root, self.BUILD_REV)

    def test_required_runtime_executable_mode_is_enforced(self):
        root = self.make_engine()
        wine = root / "files/bin/wine"
        wine.chmod(wine.stat().st_mode & ~0o111)
        with self.assertRaisesRegex(BolError, "not executable.*files/bin/wine"):
            vkd3d.validate_engine_manifest(root, self.BUILD_REV)

    def test_required_runtime_link_cannot_be_replaced_by_regular_file(self):
        root = self.make_engine()
        link = root / "files/bin-wow64/wine-preloader"
        contents = link.read_bytes()
        link.unlink()
        link.write_bytes(contents)
        link.chmod(0o755)
        with self.assertRaisesRegex(BolError, "runtime link is missing"):
            vkd3d.validate_engine_manifest(root, self.BUILD_REV)

    def test_provenance_and_critical_hashes_must_agree(self):
        root = self.make_engine()
        manifest = self.read_manifest()
        manifest["provenance"]["winegdk"]["distribution_files"][
            self.WINEGDK_PROVENANCE] = "0" * 64
        self.write_manifest(manifest)
        with self.assertRaisesRegex(
                BolError, "critical_files and distribution_files disagree"):
            vkd3d.validate_engine_manifest(root, self.BUILD_REV)

    def test_gdk_proton_base_provenance_is_exact(self):
        root = self.make_engine()
        manifest = self.read_manifest()
        manifest["provenance"]["gdk_proton_base"][
            "archive_sha256"] = "0" * 64
        self.write_manifest(manifest)
        with self.assertRaisesRegex(
                BolError, "GDK-Proton base provenance"):
            vkd3d.validate_engine_manifest(root, self.BUILD_REV)

    def test_native_threading_runtime_is_mandatory(self):
        root = self.make_engine()
        root.joinpath(*Path(vkd3d.XGAMERUNTIME_THREADING_PATH).parts).unlink()
        with self.assertRaisesRegex(
                BolError, "missing or escapes the engine"):
            vkd3d.validate_engine_manifest(root, self.BUILD_REV)

    def test_file_picker_runtimes_are_mandatory_for_both_architectures(self):
        paths = (
            "files/lib/wine/x86_64-windows/windows.storage.dll",
            "files/lib/wine/i386-windows/windows.storage.dll",
            ("files/lib/wine/x86_64-windows/"
             "windows.storage.applicationdata.dll"),
            ("files/lib/wine/i386-windows/"
             "windows.storage.applicationdata.dll"),
        )
        for relative in paths:
            with self.subTest(relative=relative):
                self.assertIn(relative, vkd3d.REQUIRED_CRITICAL_FILE_PATHS)
                root = self.make_engine()
                root.joinpath(*Path(relative).parts).unlink()
                with self.assertRaisesRegex(
                        BolError,
                        r"missing or escapes.*windows[.]storage"):
                    vkd3d.validate_engine_manifest(root, self.BUILD_REV)

    def test_managed_user32_source_is_mandatory(self):
        relative = "files/lib/wine/x86_64-windows/user32.dll"
        self.assertIn(relative, vkd3d.REQUIRED_CRITICAL_FILE_PATHS)
        root = self.make_engine()
        root.joinpath(*Path(relative).parts).unlink()
        with self.assertRaisesRegex(
                BolError, r"missing or escapes.*user32[.]dll"):
            vkd3d.validate_engine_manifest(root, self.BUILD_REV)

    def test_pinned_revision_rejects_unreviewed_native_threading(self):
        root = self.make_engine()
        manifest = self.read_manifest()
        manifest["build_rev"] = vkd3d.REQUIRED_VARIANT_HASHES_BUILD_REV
        self.write_manifest(manifest)
        with self.assertRaisesRegex(
                BolError, "Pinned engine critical-file SHA-256 mismatch"):
            vkd3d.validate_engine_manifest(
                root, vkd3d.REQUIRED_VARIANT_HASHES_BUILD_REV)

    def test_build_revision_must_match(self):
        root = self.make_engine()
        with self.assertRaisesRegex(BolError, "revision mismatch"):
            vkd3d.validate_engine_manifest(root, "other-r9")

    def test_schema_must_be_exact_integer_one(self):
        root = self.make_engine()
        manifest = self.read_manifest()
        manifest["schema"] = True
        self.write_manifest(manifest)
        with self.assertRaisesRegex(BolError, "schema"):
            vkd3d.validate_engine_manifest(root, self.BUILD_REV)

    def test_component_versions_are_required_and_exact(self):
        root = self.make_engine()
        manifest = self.read_manifest()
        manifest["components"]["vkd3d_nv_dgc"] = "3.0.1"
        self.write_manifest(manifest)
        with self.assertRaisesRegex(
                BolError, "component mismatch for vkd3d_nv_dgc"):
            vkd3d.validate_engine_manifest(root, self.BUILD_REV)

    def test_source_and_abi_provenance_are_required(self):
        root = self.make_engine()
        manifest = self.read_manifest()
        manifest["provenance"]["winegdk"]["commit"] = "unreviewed"
        self.write_manifest(manifest)
        with self.assertRaisesRegex(BolError, "WineGDK source mismatch"):
            vkd3d.validate_engine_manifest(root, self.BUILD_REV)

        manifest = self.read_manifest()
        manifest["provenance"]["winegdk"]["commit"] = (
            vkd3d.WINEGDK_SOURCE_COMMIT)
        manifest["provenance"]["build"]["glibc_max"] = "2.38"
        self.write_manifest(manifest)
        with self.assertRaisesRegex(BolError, "ABI mismatch"):
            vkd3d.validate_engine_manifest(root, self.BUILD_REV)

        manifest = self.read_manifest()
        del manifest["components"]
        self.write_manifest(manifest)
        with self.assertRaisesRegex(BolError, "components must be an object"):
            vkd3d.validate_engine_manifest(root, self.BUILD_REV)

    def test_pinned_revision_rejects_self_consistent_unreviewed_hashes(self):
        root = self.make_engine()
        manifest = self.read_manifest()
        # These hashes correctly describe the fixture files, but changing the
        # build label must not let an arbitrary payload authenticate itself.
        manifest["build_rev"] = vkd3d.REQUIRED_VARIANT_HASHES_BUILD_REV
        self.write_manifest(manifest)
        with self.assertRaisesRegex(
                BolError, "Pinned engine (?:critical-file )?SHA-256 mismatch"):
            vkd3d.validate_engine_manifest(
                root, vkd3d.REQUIRED_VARIANT_HASHES_BUILD_REV)

    def test_managed_validation_rejects_revision_without_compiled_pins(self):
        root = self.make_engine()
        with self.assertRaisesRegex(BolError, "No compiled engine hash allow-list"):
            vkd3d.validate_engine_manifest(
                root, self.BUILD_REV, enforce_pins=True)

    def test_pinned_revision_rejects_manifest_file_list_mutation(self):
        root = self.make_engine()
        manifest = self.read_manifest()
        manifest["build_rev"] = vkd3d.REQUIRED_VARIANT_HASHES_BUILD_REV
        for variant, files in vkd3d.REQUIRED_VARIANT_HASHES.items():
            manifest["variants"][variant]["files"] = dict(files)
        manifest["variants"][vkd3d.EXT_DGC]["files"][
            "files/lib/wine/vkd3d-proton/unreviewed.dll"] = "0" * 64
        self.write_manifest(manifest)
        fixture_threading_hash = manifest["critical_files"][
            vkd3d.XGAMERUNTIME_THREADING_PATH]
        with mock.patch.dict(
                vkd3d.REQUIRED_PINNED_CRITICAL_HASHES,
                {vkd3d.XGAMERUNTIME_THREADING_PATH:
                 fixture_threading_hash}, clear=True):
            with self.assertRaisesRegex(
                    BolError, "Pinned engine file list mismatch"):
                vkd3d.validate_engine_manifest(
                    root, vkd3d.REQUIRED_VARIANT_HASHES_BUILD_REV)

    def test_hash_mismatch_is_rejected_before_activation(self):
        root = self.make_engine()
        relative = vkd3d._variant_relative_paths(vkd3d.EXT_DGC)[0]
        root.joinpath(*Path(relative).parts).write_bytes(b"tampered")
        with self.assertRaisesRegex(BolError, "SHA-256 mismatch"):
            vkd3d.validate_engine_manifest(
                root, self.BUILD_REV, required_variant=vkd3d.EXT_DGC)

    def test_manifest_path_traversal_is_rejected(self):
        root = self.make_engine()
        manifest = self.read_manifest()
        manifest["variants"][vkd3d.EXT_DGC]["files"]["../escape"] = (
            "0" * 64)
        self.write_manifest(manifest)
        with self.assertRaisesRegex(BolError, "Unsafe path"):
            vkd3d.validate_engine_manifest(
                root, self.BUILD_REV, required_variant=vkd3d.EXT_DGC)

    def test_missing_required_nv_variant_has_actionable_error(self):
        root = self.make_engine()
        manifest = self.read_manifest()
        del manifest["variants"][vkd3d.NV_DGC]
        self.write_manifest(manifest)
        with self.assertRaisesRegex(
                BolError, "requires the 'nv-dgc'.*Reinstall or update"):
            vkd3d.activate_vkd3d_variant(
                root, self.BUILD_REV, vkd3d.NV_DGC,
                enforce_pins=False)


class ActivationTests(EngineFixture, unittest.TestCase):
    def assert_active_variant(self, variant):
        for relative, expected in self.variant_contents[variant].items():
            source = self.root.joinpath(*Path(relative).parts)
            target = Path(str(source)[:-len(".bol-" + variant)])
            self.assertEqual(target.read_bytes(), expected)

    def test_activation_switches_all_four_dlls_and_is_idempotent(self):
        root = self.make_engine()
        self.seed_active_targets()
        self.assertTrue(vkd3d.activate_vkd3d_variant(
            root, self.BUILD_REV, vkd3d.EXT_DGC,
            enforce_pins=False))
        self.assert_active_variant(vkd3d.EXT_DGC)

        with mock.patch("bol.vkd3d.os.replace") as replace:
            self.assertFalse(vkd3d.activate_vkd3d_variant(
                root, self.BUILD_REV, vkd3d.EXT_DGC,
                enforce_pins=False))
            replace.assert_not_called()

        self.assertTrue(vkd3d.activate_vkd3d_variant(
            root, self.BUILD_REV, vkd3d.NV_DGC,
            enforce_pins=False))
        self.assert_active_variant(vkd3d.NV_DGC)

    def test_universal_prepare_is_probe_free_and_honours_override(self):
        root = self.make_engine()
        self.seed_active_targets()
        self.assertFalse(hasattr(vkd3d, "probe_vulkan_devices"))
        variant, changed = vkd3d.prepare_universal_vkd3d(
            root, self.BUILD_REV,
            environ={"BOL_VKD3D_VARIANT": "nv-dgc"}, enforce_pins=False)
        self.assertEqual(variant, vkd3d.NV_DGC)
        self.assertTrue(changed)
        self.assert_active_variant(vkd3d.NV_DGC)

    def test_failed_replacement_rolls_back_every_changed_target(self):
        root = self.make_engine()
        previous = self.seed_active_targets()
        real_replace = os.replace
        stage_replacements = 0

        def fail_second_stage(source, destination):
            nonlocal stage_replacements
            if ".bol-stage-" in Path(source).name:
                stage_replacements += 1
                if stage_replacements == 2:
                    raise OSError("injected replacement failure")
            return real_replace(source, destination)

        with mock.patch("bol.vkd3d.os.replace", side_effect=fail_second_stage):
            with self.assertRaisesRegex(BolError, "activation failed"):
                vkd3d.activate_vkd3d_variant(
                    root, self.BUILD_REV, vkd3d.EXT_DGC,
                    enforce_pins=False)

        for target, expected in previous.items():
            self.assertEqual(target.read_bytes(), expected)
        self.assertEqual(list(root.rglob("*.bol-stage-*")), [])
        self.assertEqual(list(root.rglob("*.bol-rollback-*")), [])


if __name__ == "__main__":
    unittest.main()
