"""The experimental ARM64EC engine: selection, validation, install, launch.

Every test runs on a fake aarch64 host (platform.machine and the page size
patched), with fake engine trees: there is no ARM machine, and no ARM
engine published, to test against.
"""
# SPDX-License-Identifier: MIT

import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from bol import config, engine_arch, hostarch, prefix, vkd3d, winegdk
from bol import wine_registry
from bol.log import BolError

ARM_REV = "arm64ec-native1"


def _digest(data):
    return hashlib.sha256(data).hexdigest()


class FakeHost:
    """Patch the processor, the page size and glibc for one test."""

    def fake_host(self, machine="aarch64", page=4096, glibc=(2, 41),
                  rev=ARM_REV, environ=None):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch("platform.machine",
                                       return_value=machine))
        stack.enter_context(mock.patch.object(hostarch, "page_size",
                                              return_value=page))
        stack.enter_context(mock.patch.object(engine_arch, "host_glibc",
                                              return_value=glibc))
        stack.enter_context(mock.patch.object(config,
                                              "WINEGDK_ARM64_BUILD_REV", rev))
        env = {key: value for key, value in os.environ.items()
               if key not in (engine_arch.ENV, engine_arch.RUNNER_ENV)}
        env.update(environ or {})
        stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
        return stack


class SelectionTests(FakeHost, unittest.TestCase):
    ARM = {engine_arch.SETTING: engine_arch.ARM64EC}

    def test_x86_64_host_changes_nothing(self):
        self.fake_host(machine="x86_64")
        self.assertFalse(engine_arch.offered())
        self.assertEqual(engine_arch.active(self.ARM), engine_arch.X86_64)
        self.assertEqual(engine_arch.active({}), engine_arch.X86_64)
        self.assertFalse(hostarch.engine_needs_emulator())

    def test_hidden_without_a_published_engine(self):
        self.fake_host(rev="")
        self.assertFalse(engine_arch.offered())
        self.assertEqual(engine_arch.active(self.ARM), engine_arch.X86_64)
        # No published engine is not something to warn an ARM player about.
        self.assertIsNone(engine_arch.fallback_notice(self.ARM))

    def test_offered_on_4k_aarch64_with_a_pin(self):
        self.fake_host()
        self.assertTrue(engine_arch.offered())
        self.assertEqual(engine_arch.active({}), engine_arch.X86_64)
        self.assertEqual(engine_arch.active(self.ARM), engine_arch.ARM64EC)

    def test_large_pages_are_not_offered(self):
        self.fake_host(page=16384)
        self.assertFalse(engine_arch.offered())
        self.assertEqual(engine_arch.active(self.ARM), engine_arch.X86_64)
        self.assertIn("16 KiB", engine_arch.problem())

    def test_old_glibc_falls_back_with_a_reason(self):
        self.fake_host(glibc=(2, 36))
        self.assertTrue(engine_arch.offered())
        self.assertEqual(engine_arch.active(self.ARM), engine_arch.X86_64)
        notice = engine_arch.fallback_notice(self.ARM)
        self.assertIn("glibc 2.41", notice)
        self.assertIn("2.36", notice)
        self.assertIn("x86-64 engine instead", notice)

    def test_umu_runner_does_not_need_the_host_glibc(self):
        self.fake_host(glibc=(2, 36),
                       environ={engine_arch.RUNNER_ENV: "umu"})
        self.assertEqual(engine_arch.active(self.ARM), engine_arch.ARM64EC)

    def test_environment_overrides_the_setting_both_ways(self):
        self.fake_host(environ={engine_arch.ENV: "x86_64"})
        self.assertEqual(engine_arch.active(self.ARM), engine_arch.X86_64)
        with mock.patch.dict(os.environ, {engine_arch.ENV: "arm64ec"}):
            self.assertEqual(engine_arch.active({}), engine_arch.ARM64EC)

    def test_unknown_values_mean_the_default(self):
        self.fake_host()
        self.assertEqual(engine_arch.active({engine_arch.SETTING: "riscv"}),
                         engine_arch.X86_64)

    def test_host_glibc_parses_confstr(self):
        with mock.patch("os.confstr", return_value="glibc 2.41"):
            self.assertEqual(engine_arch.host_glibc(), (2, 41))
        with mock.patch("os.confstr", side_effect=ValueError):
            self.assertIsNone(engine_arch.host_glibc())

    def test_engine_for_path_follows_the_tree(self):
        self.fake_host()
        arm = engine_arch.engine_for_path(config.WINEGDK_ARM64_OUT)
        self.assertEqual(arm.arch, engine_arch.ARM64EC)
        self.assertEqual(arm.build_rev, ARM_REV)
        self.assertEqual(arm.asset, f"GDK-Proton-xuser-{ARM_REV}.tar.gz")
        self.assertEqual(arm.host_pe_dir, "files/lib/wine/aarch64-windows")
        x86 = engine_arch.engine_for_path(config.WINEGDK_OUT)
        self.assertEqual(x86.arch, engine_arch.X86_64)
        self.assertIsNone(engine_arch.engine_for_path("/elsewhere"))

    def test_doctor_summary_names_the_engine(self):
        self.fake_host()
        self.assertIn("ARM64EC", engine_arch.summary(self.ARM))
        self.assertIn("FEX-Emu inside the prefix",
                      engine_arch.summary(self.ARM))
        self.assertIn("available in Settings", engine_arch.summary({}))


class HostArchTests(FakeHost, unittest.TestCase):
    def test_arm_engine_still_needs_binfmt_for_the_helpers(self):
        self.fake_host()
        with mock.patch.object(engine_arch, "active",
                               return_value=engine_arch.ARM64EC), \
                mock.patch.object(hostarch, "x86_64_emulator",
                                  return_value=None):
            self.assertFalse(hostarch.engine_needs_emulator())
            self.assertEqual(hostarch.x86_64_programs(),
                             ["xodus-cli", "the WebKitGTK sign-in window"])
            problem = hostarch.problem()
            self.assertIn("ARM game engine", problem)
            self.assertNotIn("Minecraft for Windows, Wine", problem)
            self.assertIn("its own FEX-Emu", hostarch.summary())

    def test_x86_engine_on_arm_is_unchanged(self):
        self.fake_host()
        with mock.patch.object(hostarch, "x86_64_emulator",
                               return_value=None):
            self.assertTrue(hostarch.engine_needs_emulator())
            self.assertIn("Minecraft for Windows, Wine and xodus-cli",
                          hostarch.problem())

    def test_muvm_relaunch_still_applies_on_large_pages(self):
        self.fake_host(page=16384)
        with mock.patch.object(hostarch, "muvm", return_value="/usr/bin/muvm"):
            cmd = hostarch.muvm_command(argv=["-m", "bol"], environ={})
        self.assertEqual(cmd[0], "/usr/bin/muvm")


class ArmManifestTests(FakeHost, unittest.TestCase):
    """bol.vkd3d's aarch64 expectations, against a fake ARM engine tree."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)

    def pin(self, stack, variant_hashes, threading_hash):
        for name, value in {
            "REQUIRED_VARIANT_HASHES_ARM64_BUILD_REV": ARM_REV,
            "REQUIRED_ARM64_COMPONENTS": {
                "dxvk": "3.1.1", "vkd3d_ext_dgc": "3.0.1-bol-dgc-arm64x",
                "vkd3d_nv_dgc": "3.0.1-bol-dgc-arm64x"},
            "REQUIRED_ARM64_VKD3D_BASE_COMMIT": "a" * 40,
            "REQUIRED_ARM64_VKD3D_REVERT": "b" * 40,
            "REQUIRED_ARM64_PROTON_BASE_ARCHIVE_SHA256": "c" * 64,
            "REQUIRED_VARIANT_HASHES_ARM64": variant_hashes,
        }.items():
            stack.enter_context(mock.patch.object(vkd3d, name, value))
        stack.enter_context(mock.patch.dict(
            vkd3d.REQUIRED_PINNED_CRITICAL_HASHES,
            {vkd3d.XGAMERUNTIME_THREADING_PATH: threading_hash}))

    def make_arm_engine(self, root, rev=ARM_REV, arch="aarch64",
                        pin=True):
        """A fake ARM64EC engine tree and its manifest; pins it when asked."""
        stack = ExitStack()
        self.addCleanup(stack.close)
        source_manifest = ("critical:" +
                           vkd3d.REQUIRED_WINEGDK_SOURCE_MANIFEST_PATH).encode()
        stack.enter_context(mock.patch.object(
            vkd3d, "WINEGDK_SOURCE_MANIFEST_SHA256", _digest(source_manifest)))
        root.mkdir(parents=True)
        variants, variant_hashes = {}, {}
        for variant in vkd3d.VARIANTS:
            files = {}
            for relative in vkd3d._variant_relative_paths(
                    variant, vkd3d.ARM64_ARCHES):
                data = (variant + ":" + relative).encode()
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                files[relative] = _digest(data)
            variants[variant] = {"files": files}
            variant_hashes[variant] = dict(files)
        provenance = {
            "winegdk": vkd3d.REQUIRED_WINEGDK_SOURCE_MANIFEST_PATH,
            "vkd3d": "files/share/bedrock-on-linux/vkd3d.env",
            "base": "files/share/bedrock-on-linux/base.env",
        }
        critical_paths = (tuple(vkd3d.REQUIRED_ARM64_CRITICAL_FILE_PATHS)
                          + tuple(provenance.values()))
        for relative in critical_paths:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("critical:" + relative).encode())
        for relative in vkd3d.REQUIRED_ARM64_EXECUTABLE_FILE_PATHS:
            (root / relative).chmod(0o755)
        critical = {relative: _digest((root / relative).read_bytes())
                    for relative in critical_paths}
        if pin:
            self.pin(stack, variant_hashes,
                     critical[vkd3d.XGAMERUNTIME_THREADING_PATH])
        expect = vkd3d.engine_expectations("aarch64")
        manifest = {
            "schema": 1,
            "build_rev": rev,
            "arch": arch,
            "components": dict(expect.components),
            "provenance": {
                "winegdk": {
                    "commit": vkd3d.WINEGDK_SOURCE_COMMIT,
                    "distribution_files": {
                        provenance["winegdk"]: critical[provenance["winegdk"]]},
                },
                "vkd3d_universal": {
                    "base_commit": expect.vkd3d_base_commit,
                    "restored_by_reverting": expect.vkd3d_revert,
                    "distribution_files": {
                        provenance["vkd3d"]: critical[provenance["vkd3d"]]},
                },
                "gdk_proton_base": dict(
                    expect.base_fields,
                    distribution_files={
                        provenance["base"]: critical[provenance["base"]]}),
                "build": {"glibc_max": "2.41"},
            },
            "critical_files": critical,
            "variants": variants,
        }
        (root / "engine-manifest.json").write_text(json.dumps(manifest))
        return manifest

    def test_pinned_arm_engine_validates(self):
        self.fake_host()
        root = self.base / "GDK-Proton-xuser-arm64"
        self.make_arm_engine(root)
        result = vkd3d.validate_engine_manifest(
            root, ARM_REV, enforce_pins=True, arch="aarch64")
        self.assertEqual(result.build_rev, ARM_REV)
        self.assertEqual(len(result.variants[vkd3d.EXT_DGC]), 6)
        self.assertIn("files/lib/wine/aarch64-windows/libarm64ecfex.dll",
                      result.critical_files)

    def test_unpinned_arm_engine_is_refused(self):
        self.fake_host()
        root = self.base / "GDK-Proton-xuser-arm64"
        self.make_arm_engine(root, pin=False)
        with self.assertRaisesRegex(BolError, "no reviewed"):
            vkd3d.validate_engine_manifest(
                root, ARM_REV, enforce_pins=True, arch="aarch64")

    def test_an_engine_never_stands_in_for_the_other(self):
        self.fake_host()
        root = self.base / "GDK-Proton-xuser-arm64"
        self.make_arm_engine(root, arch="x86_64")
        with self.assertRaisesRegex(BolError, "architecture mismatch"):
            vkd3d.validate_engine_manifest(
                root, ARM_REV, enforce_pins=True, arch="aarch64")
        other = self.base / "other"
        self.make_arm_engine(other, rev="wow64-archs-native28")
        with self.assertRaisesRegex(BolError, "architecture mismatch"):
            vkd3d.validate_engine_manifest(other, "wow64-archs-native28")

    def test_missing_fex_dll_is_refused(self):
        self.fake_host()
        root = self.base / "GDK-Proton-xuser-arm64"
        manifest = self.make_arm_engine(root)
        del manifest["critical_files"][
            "files/lib/wine/aarch64-windows/libarm64ecfex.dll"]
        (root / "engine-manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(BolError, "libarm64ecfex"):
            vkd3d.validate_engine_manifest(
                root, ARM_REV, enforce_pins=True, arch="aarch64")

    def test_vkd3d_activation_fills_all_three_arch_slots(self):
        self.fake_host()
        root = self.base / "GDK-Proton-xuser-arm64"
        self.make_arm_engine(root)
        variant, changed = vkd3d.prepare_universal_vkd3d(
            root, ARM_REV, environ={}, arch="aarch64")
        self.assertEqual(variant, vkd3d.EXT_DGC)
        self.assertTrue(changed)
        for arch in vkd3d.ARM64_ARCHES:
            for dll in vkd3d.DLL_NAMES:
                self.assertTrue(
                    (root / "files/lib/wine/vkd3d-proton" / arch / dll)
                    .is_file())

    def test_x86_expectations_are_untouched(self):
        expect = vkd3d.engine_expectations("x86_64")
        self.assertEqual(expect.vkd3d_arches, vkd3d.ARCHES)
        self.assertEqual(expect.critical_file_paths,
                         vkd3d.REQUIRED_CRITICAL_FILE_PATHS)
        self.assertEqual(expect.variant_hashes_build_rev,
                         config.WINEGDK_BUILD_REV)
        self.assertIsNone(vkd3d._incomplete_expectations(expect))

    def test_shipped_arm_pins_start_empty(self):
        # The coordinator fills these with the first published build.
        self.assertEqual(config.WINEGDK_ARM64_BUILD_REV, "")
        self.assertEqual(config.WINEGDK_ARM64_ARCHIVE_SHA256, "")
        self.assertEqual(config.WINEGDK_ARM64_PREFIX_SHA256, "")
        self.assertEqual(config.WINEGDK_ARM64_OUT.name,
                         "GDK-Proton-xuser-arm64")
        self.assertEqual(vkd3d.REQUIRED_VARIANT_HASHES_ARM64_BUILD_REV,
                         config.WINEGDK_ARM64_BUILD_REV)


class ArmInstallTests(FakeHost, unittest.TestCase):
    """bol.winegdk picks, downloads and activates the ARM engine."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.proton_dir = self.base / "proton"
        self.cache = self.base / "cache"
        self.settings = {"proton_source": "winegdk",
                         engine_arch.SETTING: engine_arch.ARM64EC}
        self.validator = mock.Mock(name="validate_engine_manifest")
        stack = self.fake_host()
        for name, value in {
            "PROTON_DIR": self.proton_dir,
            "CACHE": self.cache,
            "WINEGDK_OUT": self.proton_dir / "GDK-Proton-xuser",
            "WINEGDK_ARM64_OUT": self.proton_dir / "GDK-Proton-xuser-arm64",
            "WINEGDK_ARM64_BUILD_REV": ARM_REV,
            "WINEGDK_BUILD_REV": "wow64-archs-test",
        }.items():
            stack.enter_context(mock.patch.object(winegdk, name, value))
        stack.enter_context(mock.patch.object(
            winegdk, "load_settings", side_effect=lambda: dict(self.settings)))
        stack.enter_context(mock.patch.object(
            winegdk, "save_settings", side_effect=self.settings.update))
        stack.enter_context(mock.patch.object(winegdk, "patch_proton"))
        stack.enter_context(mock.patch.dict(
            sys.modules, {"bol.vkd3d": mock.Mock(
                validate_engine_manifest=self.validator)}))

    def archive(self, top):
        source = self.base / "src" / top
        (source / "files").mkdir(parents=True)
        (source / "proton").write_text("proton")
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w:gz") as tf:
            tf.add(source, arcname=top)
        shutil.rmtree(self.base / "src")
        return data.getvalue()

    def test_arm_engine_is_downloaded_validated_and_wired(self):
        payload = self.archive("GDK-Proton-xuser-arm64")
        urls = []

        def fake_download(url, dest, *_args):
            urls.append(url)
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_bytes(payload)

        with mock.patch.object(winegdk, "WINEGDK_ARM64_ARCHIVE_SHA256",
                               _digest(payload)), \
                mock.patch.object(winegdk, "download",
                                  side_effect=fake_download):
            out = winegdk.ensure_winegdk()
        self.assertEqual(out, self.proton_dir / "GDK-Proton-xuser-arm64")
        self.assertEqual(urls, [
            f"https://github.com/{config.WINEGDK_PREBUILT_REPO}/releases/"
            f"download/engine-{ARM_REV}/GDK-Proton-xuser-{ARM_REV}.tar.gz"])
        self.assertTrue((out / "proton").is_file())
        self.assertEqual(self.settings["proton"], str(out))
        self.assertEqual(self.settings["winegdk_arm64_built"],
                         "prebuilt:" + ARM_REV)
        self.assertEqual(self.validator.call_args.kwargs["arch"], "aarch64")
        self.assertEqual(self.validator.call_args.args[1], ARM_REV)
        # The x86-64 engine's tree was never touched.
        self.assertFalse((self.proton_dir / "GDK-Proton-xuser").exists())

    def test_unavailable_arm_engine_falls_back_to_x86(self):
        x86 = self.archive("GDK-Proton-xuser")

        def fake_download(url, dest, *_args):
            if "arm64ec" in url:
                raise OSError("404")
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_bytes(x86)

        with mock.patch.object(winegdk, "WINEGDK_ARM64_ARCHIVE_SHA256",
                               "0" * 64), \
                mock.patch.object(winegdk, "WINEGDK_ARCHIVE_SHA256",
                                  _digest(x86)), \
                mock.patch.object(winegdk, "download",
                                  side_effect=fake_download), \
                mock.patch.object(winegdk, "warn") as warned:
            out = winegdk.ensure_winegdk()
        self.assertEqual(out, self.proton_dir / "GDK-Proton-xuser")
        self.assertTrue(any("x86-64 engine this time" in str(call)
                            for call in warned.call_args_list))

    def test_cache_keeps_the_current_archive_of_both_engines(self):
        self.cache.mkdir()
        keep_arm = self.cache / f"GDK-Proton-xuser-{ARM_REV}.tar.gz"
        keep_x86 = self.cache / "GDK-Proton-xuser-wow64-archs-test.tar.gz"
        stale = self.cache / "GDK-Proton-xuser-wow64-archs-old.tar.gz"
        for path in (keep_arm, keep_x86, stale):
            path.write_bytes(b"x")
        winegdk._prune_stale_engine_archives(
            winegdk._current_archive_names())
        self.assertTrue(keep_arm.exists())
        self.assertTrue(keep_x86.exists())
        self.assertFalse(stale.exists())


class ArmLaunchTests(FakeHost, unittest.TestCase):
    """bol.prefix starts the ARM engine directly, as umu would have."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.engine = self.base / "proton" / "GDK-Proton-xuser-arm64"
        (self.engine / "files/lib/wine/aarch64-windows").mkdir(parents=True)
        self.pfx = self.base / "compatdata" / "pfx"
        (self.pfx / "drive_c/windows/system32").mkdir(parents=True)
        for hive in ("system.reg", "user.reg"):
            (self.pfx / hive).write_text(
                "WINE REGISTRY Version 2\n;; All keys relative to \\\\Machine\n\n")
        stack = self.fake_host()
        for name, value in {
            "PFX": self.pfx,
            "COMPAT": self.pfx.parent,
            "WINEGDK_ARM64_OUT": self.engine,
            "WINEGDK_ARM64_BUILD_REV": ARM_REV,
            "WINEGDK_OUT": self.base / "proton" / "GDK-Proton-xuser",
        }.items():
            stack.enter_context(mock.patch.object(prefix, name, value))
        stack.enter_context(mock.patch.object(
            prefix, "proton_path", return_value=self.engine))
        stack.enter_context(mock.patch.object(
            prefix, "steam_compat_dir", return_value=self.base / "steam"))
        stack.enter_context(mock.patch.object(
            prefix, "prefix_processes", return_value=[]))
        self.ensure_umu = stack.enter_context(mock.patch.object(
            prefix, "ensure_umu", return_value=self.base / "umu-run"))

    def test_direct_run_carries_umus_environment(self):
        exe = str(self.base / "game" / "Minecraft.Windows.exe")
        with mock.patch.dict(os.environ, {"GAMEID": "umu-1234"}):
            cmd, env = prefix.proton_umu_cmd(exe, prefix=self.pfx)
        self.assertEqual(cmd, [str(self.engine / "proton"), "run", exe])
        self.ensure_umu.assert_not_called()
        self.assertEqual(env["WINEPREFIX"], str(self.pfx))
        self.assertEqual(env["STEAM_COMPAT_DATA_PATH"], str(self.pfx))
        self.assertEqual(env["STEAM_COMPAT_INSTALL_PATH"],
                         str(self.base / "game"))
        self.assertEqual(env["STEAM_COMPAT_TOOL_PATHS"], str(self.engine))
        self.assertEqual(env["STEAM_COMPAT_CLIENT_INSTALL_PATH"],
                         str(self.base / "steam"))
        self.assertEqual(env["PROTON_USE_WOW64"], "1")
        self.assertEqual(env["UMU_ID"], "umu-1234")
        self.assertEqual(env["SteamAppId"], "1234")
        self.assertEqual(env["SteamGameId"], "1234")
        self.assertNotIn("PROTONPATH", env)
        self.assertNotIn("UMU_RUNTIME_UPDATE", env)

    def test_direct_run_gives_the_system_ca_store(self):
        with mock.patch.object(prefix, "_system_ca_environment",
                               return_value={"SSL_CERT_FILE": "/ca.pem"}), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SSL_CERT_FILE", None)
            _cmd, env = prefix.proton_umu_cmd("wineboot", prefix=self.pfx)
        self.assertEqual(env["SSL_CERT_FILE"], "/ca.pem")
        # umu's own derivation, kept as it is (see bol.prefix, #199).
        self.assertEqual(env["UMU_ID"], "umu-default")
        self.assertEqual(env["SteamAppId"], "default")

    def test_umu_runner_keeps_the_container_path(self):
        with mock.patch.dict(os.environ, {engine_arch.RUNNER_ENV: "umu"}):
            cmd, env = prefix.proton_umu_cmd("wineboot", prefix=self.pfx)
        self.assertEqual(cmd, [sys.executable, str(self.base / "umu-run"),
                               "wineboot"])
        self.assertEqual(env["PROTONPATH"], str(self.engine))

    def test_fex_is_registered_in_the_prefix(self):
        prefix.proton_umu_cmd("wineboot", prefix=self.pfx)
        hive = (self.pfx / "system.reg").read_text()
        self.assertIn("[Software\\\\Microsoft\\\\Wow64\\\\amd64]", hive)
        self.assertIn('@="libarm64ecfex.dll"', hive)
        self.assertIn('@="libwow64fex.dll"', hive)
        # Again is a no-op.
        self.assertFalse(prefix.register_fex_emulator(self.pfx))

    def test_existing_xtajit_default_is_replaced(self):
        (self.pfx / "system.reg").write_text(
            "WINE REGISTRY Version 2\n\n"
            "[Software\\\\Microsoft\\\\Wow64\\\\amd64] 1700000000\n"
            "#time=1da0000000000000\n"
            '@="xtajit64.dll"\n\n')
        self.assertTrue(prefix.register_fex_emulator(self.pfx))
        hive = (self.pfx / "system.reg").read_text()
        self.assertNotIn("xtajit64.dll", hive)
        self.assertEqual(hive.count('@="libarm64ecfex.dll"'), 1)

    def test_no_runtime_download_is_announced(self):
        self.assertFalse(prefix.runtime_setup_pending())

    def test_prefix_refresh_uses_the_arm_dlls(self):
        source = self.engine / "files/lib/wine/aarch64-windows/kernel32.dll"
        source.write_bytes(b"arm64x kernel32")
        target = self.pfx / "drive_c/windows/system32/kernel32.dll"
        target.write_bytes(b"x86-64 kernel32")
        with mock.patch.dict(os.environ, {"BOL_WINEPREFIX": ""}):
            self.assertTrue(prefix.managed_prefix_engine_is_stale(self.pfx))
            self.assertTrue(prefix.refresh_managed_prefix_runtime(self.pfx))
        self.assertEqual(target.read_bytes(), b"arm64x kernel32")


class RegistryDefaultValueTests(unittest.TestCase):
    def test_default_value_uses_wineservers_spelling(self):
        change = wine_registry.reg_sz(r"Software\Test", "", "value")
        self.assertEqual(wine_registry._value_line(change), '@="value"\n')
        named = wine_registry.reg_sz(r"Software\Test", "Name", "value")
        self.assertEqual(wine_registry._value_line(named),
                         '"Name"="value"\n')


if __name__ == "__main__":
    unittest.main()
