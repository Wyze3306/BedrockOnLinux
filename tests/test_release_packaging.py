"""Smoke tests for unreleased application candidate packaging."""
# SPDX-License-Identifier: MIT

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from bol.config import (
    OPENSSL_XCURL_ARCHIVE_SHA256,
    OPENSSL_XCURL_REV,
    UMU_ARCHIVE_SHA256,
    UMU_RUN_SHA256,
    UMU_VERSION,
    VERSION,
    WINEGDK_ARCHIVE_SHA256,
    WINEGDK_BUILD_REV,
    WINEGDK_SOURCE_COMMIT,
)


ROOT = Path(__file__).resolve().parents[1]
VERIFY = ROOT / "scripts/verify-release-candidate.sh"
BUILD_RELEASE = ROOT / "scripts/build-release.sh"
PACKAGE_ENGINE = ROOT / "scripts/package-engine.sh"
RUN_CANDIDATE = ROOT / "scripts/run-candidate.sh"

# The scripts these tests write stand in for real ones and are executed. A
# build sandbox such as Guix's has no /usr/bin/env to find bash through, so
# they name the bash on PATH, as its packaging does for the real ones (#303).
_BASH = shutil.which("bash") or "/usr/bin/env bash"


# The engine pin is blank between adding an engine patch and publishing the
# build that carries it, and the release scripts refuse a candidate in that
# state on purpose. These tests exercise the packaging mechanics rather than
# that gate (verify-release-candidate.sh owns it), so they stand in a
# syntactically valid pin while the real one is unset.
_PLACEHOLDER_ENGINE_SHA = "ee" * 32
_ENGINE_SHA = WINEGDK_ARCHIVE_SHA256 or _PLACEHOLDER_ENGINE_SHA


def _config(version=VERSION, rev=WINEGDK_BUILD_REV,
            source_commit=WINEGDK_SOURCE_COMMIT,
            archive_sha=_ENGINE_SHA,
            xcurl_rev=OPENSSL_XCURL_REV,
            xcurl_sha=OPENSSL_XCURL_ARCHIVE_SHA256,
            umu_version=UMU_VERSION,
            umu_archive_sha=UMU_ARCHIVE_SHA256,
            umu_run_sha=UMU_RUN_SHA256):
    return (
        f'VERSION = "{version}"\n'
        f'WINEGDK_BUILD_REV = "{rev}"\n'
        f'WINEGDK_SOURCE_COMMIT = "{source_commit}"\n'
        f'WINEGDK_ARCHIVE_SHA256 = "{archive_sha}"\n'
        f'OPENSSL_XCURL_REV = "{xcurl_rev}"\n'
        f'OPENSSL_XCURL_ARCHIVE_SHA256 = "{xcurl_sha}"\n'
        f'UMU_VERSION = "{umu_version}"\n'
        f'UMU_ARCHIVE_SHA256 = "{umu_archive_sha}"\n'
        f'UMU_RUN_SHA256 = "{umu_run_sha}"\n'
    )


def _bol_regular_files():
    """Yield the exact source payload accepted by the candidate verifier."""
    for path in sorted((ROOT / "bol").rglob("*")):
        relative = path.relative_to(ROOT)
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        if path.is_file() and not path.is_symlink():
            yield path, relative.as_posix()


def _payload_bytes(source, relative):
    """The payload byte-for-byte, except for an engine pin that is still unset.

    Between adding an engine patch and publishing the build that carries it,
    WINEGDK_ARCHIVE_SHA256 is deliberately blank and the release scripts refuse
    such a candidate. That gate has its own test; here it would only stop the
    packaging mechanics from being exercised at all.
    """
    data = source.read_bytes()
    if relative == "bol/config.py" and not WINEGDK_ARCHIVE_SHA256:
        data = data.replace(
            b'WINEGDK_ARCHIVE_SHA256 = ""',
            b'WINEGDK_ARCHIVE_SHA256 = "%s"' % _PLACEHOLDER_ENGINE_SHA.encode())
    return data


def _copy_bol_payload(destination):
    for source, relative in _bol_regular_files():
        output = destination / Path(relative).relative_to("bol")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(_payload_bytes(source, relative))
        shutil.copystat(source, output)


class CandidateMetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _staged_checkout(self):
        """A checkout the verifier can read, with the engine pin filled in.

        verify-release-candidate.sh resolves the checkout from its own path and
        refuses outright while WINEGDK_ARCHIVE_SHA256 is unset, which is the
        release gate doing its job (test_release_gate_* covers it). Staging a
        copy keeps these packaging tests measuring packaging rather than
        whether an engine happens to be published today.
        """
        staged = self.base / "checkout"
        if staged.exists():
            return staged
        _copy_bol_payload(staged / "bol")
        (staged / "scripts").mkdir(parents=True)
        shutil.copy2(VERIFY, staged / "scripts" / VERIFY.name)
        (staged / "data").mkdir(parents=True)
        for relative in ("LICENSE", "data/icon.png",
                         "data/bedrock-on-linux.desktop"):
            shutil.copy2(ROOT / relative, staged / relative)
        return staged

    def _run(self, *artifacts):
        staged = self._staged_checkout()
        return subprocess.run(
            [str(staged / "scripts" / VERIFY.name),
             *(str(path) for path in artifacts)],
            cwd=staged,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def _pyz(self, name="candidate.pyz", version=VERSION,
             rev=WINEGDK_BUILD_REV, *, missing=(), replacements=None,
             extras=None):
        path = self.base / name
        missing = set(missing)
        replacements = replacements or {}
        extras = extras or {}
        with zipfile.ZipFile(path, "w") as archive:
            for source, relative in _bol_regular_files():
                if relative in missing:
                    continue
                if relative == "bol/config.py" \
                        and (version != VERSION or rev != WINEGDK_BUILD_REV):
                    archive.writestr(relative, _config(version, rev))
                elif relative in replacements:
                    archive.writestr(relative, replacements[relative])
                else:
                    archive.writestr(relative, _payload_bytes(source, relative))
            for relative, payload in extras.items():
                archive.writestr(relative, payload)
            archive.write(ROOT / "LICENSE", "LICENSE")
            archive.write(ROOT / "data/icon.png", "data/icon.png")
        path.chmod(0o755)
        return path

    def _appimage(self, name="candidate.AppImage", version=VERSION,
                  rev=WINEGDK_BUILD_REV):
        if shutil.which("mksquashfs") is None \
                or shutil.which("unsquashfs") is None:
            self.skipTest("squashfs-tools not installed")
        # A tiny non-executable runtime prefix plus a real SquashFS exercises
        # the verifier's safe, read-only AppImage inspection path.
        path = self.base / name
        root = self.base / (name + ".AppDir")
        bol_root = root / "usr/bin/bol"
        _copy_bol_payload(bol_root)
        config = bol_root / "config.py"
        license_path = root / "usr/share/licenses/bedrock-on-linux/LICENSE"
        license_path.parent.mkdir(parents=True)
        if version != VERSION or rev != WINEGDK_BUILD_REV:
            config.write_text(_config(version, rev), encoding="utf-8")
        shutil.copy2(ROOT / "LICENSE", license_path)
        desktop = root / "bedrock-on-linux.desktop"
        shutil.copy2(ROOT / "data/bedrock-on-linux.desktop", desktop)
        shared_desktop = root / "usr/share/applications/bedrock-on-linux.desktop"
        shared_desktop.parent.mkdir(parents=True)
        shutil.copy2(desktop, shared_desktop)
        shutil.copy2(ROOT / "data/icon.png", root / "bedrock-on-linux.png")
        launcher = root / "usr/bin/bedrock-on-linux"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        launcher.chmod(0o755)
        app_run = root / "AppRun"
        app_run.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        app_run.chmod(0o755)
        squashfs = self.base / (name + ".squashfs")
        subprocess.run(
            ["mksquashfs", str(root), str(squashfs), "-noappend",
             "-all-root", "-processors", "1", "-quiet"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
        )
        path.write_bytes(b"#!/bin/sh\nexit 1\n" + squashfs.read_bytes())
        path.chmod(0o755)
        return path

    def _deb(self, architecture="amd64", version=VERSION,
             rev=WINEGDK_BUILD_REV):
        if shutil.which("dpkg-deb") is None:
            self.skipTest("dpkg-deb not installed")
        root = self.base / ("deb-root-" + architecture)
        control = root / "DEBIAN/control"
        bol_root = root / "usr/lib/bedrock-on-linux/bol"
        _copy_bol_payload(bol_root)
        config = bol_root / "config.py"
        control.parent.mkdir(parents=True)
        copyright_file = root / "usr/share/doc/bedrock-on-linux/copyright"
        copyright_file.parent.mkdir(parents=True)
        desktop = root / "usr/share/applications/bedrock-on-linux.desktop"
        desktop.parent.mkdir(parents=True)
        icon = root / "usr/share/icons/hicolor/256x256/apps/bedrock-on-linux.png"
        icon.parent.mkdir(parents=True)
        launcher = root / "usr/lib/bedrock-on-linux/bedrock-on-linux"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        control.write_text(
            "Package: bedrock-on-linux-test\n"
            f"Version: {version}\n"
            f"Architecture: {architecture}\n"
            "Maintainer: Tests <tests@example.invalid>\n"
            "Description: packaging verifier fixture\n",
            encoding="utf-8",
        )
        if version != VERSION or rev != WINEGDK_BUILD_REV:
            config.write_text(_config(version, rev), encoding="utf-8")
        shutil.copy2(ROOT / "LICENSE", copyright_file)
        shutil.copy2(ROOT / "data/bedrock-on-linux.desktop", desktop)
        shutil.copy2(ROOT / "data/icon.png", icon)
        launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        launcher.chmod(0o755)
        package = self.base / f"candidate-{architecture}.deb"
        subprocess.run(
            ["dpkg-deb", "--build", "--root-owner-group", str(root),
             str(package)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
        )
        return package

    def _rpm(self, architecture="x86_64", version=VERSION,
             rev=WINEGDK_BUILD_REV, requires=()):
        if shutil.which("rpmbuild") is None or shutil.which("rpm") is None \
                or shutil.which("rpm2cpio") is None \
                or shutil.which("cpio") is None:
            self.skipTest("rpm tooling not installed")
        root = self.base / ("rpm-root-" + architecture)
        top = self.base / ("rpm-top-" + architecture)
        bol_root = root / "usr/lib/bedrock-on-linux/bol"
        _copy_bol_payload(bol_root)
        config = bol_root / "config.py"
        license_file = root / "usr/share/licenses/bedrock-on-linux/LICENSE"
        license_file.parent.mkdir(parents=True)
        desktop = root / "usr/share/applications/bedrock-on-linux.desktop"
        desktop.parent.mkdir(parents=True)
        icon = root / "usr/share/icons/hicolor/256x256/apps/bedrock-on-linux.png"
        icon.parent.mkdir(parents=True)
        launcher = root / "usr/lib/bedrock-on-linux/bedrock-on-linux"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        if version != VERSION or rev != WINEGDK_BUILD_REV:
            config.write_text(_config(version, rev), encoding="utf-8")
        shutil.copy2(ROOT / "LICENSE", license_file)
        shutil.copy2(ROOT / "data/bedrock-on-linux.desktop", desktop)
        shutil.copy2(ROOT / "data/icon.png", icon)
        launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        launcher.chmod(0o755)
        spec = top / "SPECS/candidate.spec"
        spec.parent.mkdir(parents=True)
        (top / "BUILD").mkdir(parents=True)
        spec.write_text(
            "%global debug_package %{nil}\n"
            "%global __os_install_post %{nil}\n"
            "Name: bedrock-on-linux-test\n"
            f"Version: {version}\n"
            "Release: 1\n"
            "Summary: packaging verifier fixture\n"
            "License: MIT\n"
            "AutoReqProv: no\n"
            + "".join(f"Requires: {item}\n" for item in requires) +
            "%description\nfixture\n%files\n"
            "/usr/lib/bedrock-on-linux\n"
            "/usr/share/applications/bedrock-on-linux.desktop\n"
            "/usr/share/icons/hicolor/256x256/apps/bedrock-on-linux.png\n"
            "%dir /usr/share/licenses/bedrock-on-linux\n"
            "/usr/share/licenses/bedrock-on-linux/LICENSE\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["rpmbuild", "-bb", "--target", architecture,
             "--define", f"_topdir {top}", "--buildroot", str(root),
             str(spec)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
        )
        built = next((top / "RPMS").rglob("*.rpm"))
        package = self.base / f"candidate-{architecture}.rpm"
        shutil.move(str(built), package)
        return package

    def test_accepts_matching_pyz_deb_and_appimage(self):
        result = self._run(self._pyz(), self._deb(), self._appimage())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("VERSION="), 3)
        self.assertIn("Candidate metadata verified", result.stdout)

    def test_release_gate_refuses_an_unpinned_engine(self):
        # Adding an engine patch blanks WINEGDK_ARCHIVE_SHA256 until the build
        # carrying it is published. No candidate may be verified in that state,
        # otherwise a release could ship pointing at an engine nobody built.
        staged = self._staged_checkout()
        config = staged / "bol/config.py"
        original = config.read_text(encoding="utf-8")
        config.write_text(
            re.sub(r'^WINEGDK_ARCHIVE_SHA256 = ".*"$',
                   'WINEGDK_ARCHIVE_SHA256 = ""',
                   original, count=1, flags=re.MULTILINE),
            encoding="utf-8")
        try:
            result = subprocess.run(
                [str(staged / "scripts" / VERIFY.name), str(self._pyz())],
                cwd=staged, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, check=False)
        finally:
            config.write_text(original, encoding="utf-8")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("WINEGDK_ARCHIVE_SHA256 is not a lowercase SHA-256 pin",
                      result.stderr)

    def test_rejects_stale_engine_revision_inside_versioned_pyz(self):
        artifact = self._pyz(
            name=f"bedrock-on-linux-{VERSION}.pyz",
            rev="wow64-archs-stale",
        )
        result = self._run(artifact)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("embeds WINEGDK_BUILD_REV=wow64-archs-stale",
                      result.stderr)

    def test_rejects_debian_architecture_all(self):
        result = self._run(self._deb(architecture="all"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Architecture=all, expected amd64", result.stderr)

    def test_accepts_matching_rpm(self):
        result = self._run(self._rpm())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Candidate metadata verified", result.stdout)

    def test_rejects_noarch_rpm(self):
        # The launcher only makes sense where the managed engine and the game
        # exist, so the package must stay x86_64 like the .deb is amd64.
        result = self._run(self._rpm(architecture="noarch"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Arch=noarch, expected x86_64", result.stderr)

    def test_rejects_rpm_with_generated_python_dependencies(self):
        # The bundled wheels ship .dist-info; if a build host's Python
        # generator turned those into requires, the package would be
        # uninstallable on the distributions it targets.
        result = self._run(self._rpm(requires=("python3dist(customtkinter)",)))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("generated python3dist() dependencies", result.stderr)

    def test_rejects_stale_bol_payload_inside_rpm(self):
        artifact = self._rpm(rev="wow64-archs-stale")
        result = self._run(artifact)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("embeds WINEGDK_BUILD_REV=wow64-archs-stale",
                      result.stderr)

    def test_rejects_missing_bol_payload_file(self):
        result = self._run(self._pyz(missing={"bol/gpu_safety.py"}))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing: gpu_safety.py", result.stderr)

    def test_rejects_stale_bol_payload_file(self):
        result = self._run(self._pyz(replacements={
            "bol/wine_registry.py": b"# stale registry implementation\n",
        }))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stale: wine_registry.py", result.stderr)

    def test_rejects_extra_bol_payload_file(self):
        result = self._run(self._pyz(extras={
            "bol/obsolete_release_helper.py": b"# obsolete\n",
        }))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("extra: obsolete_release_helper.py", result.stderr)

    def test_ignores_runtime_bytecode_and_cache_files(self):
        result = self._run(self._pyz(extras={
            "bol/__pycache__/gpu_safety.cpython-312.pyc": b"cache",
            "bol/leftover.pyc": b"cache",
        }))
        self.assertEqual(result.returncode, 0, result.stderr)


class BuildReleaseHygieneTests(unittest.TestCase):
    def test_flatpak_app_builder_is_detected_before_failure_gate(self):
        script = BUILD_RELEASE.read_text(encoding="utf-8")
        detection = script.index("flatpak info org.flatpak.Builder")
        build = script.index('bash "$SRC/scripts/build-flatpak.sh"', detection)
        required = script.index("incomplete candidate: required formats failed")
        self.assertLess(detection, build)
        self.assertLess(build, required)
        self.assertIn('required_failures+=("Flatpak")', script[build:required])

    def test_nightly_builds_the_flatpak_from_the_checkout(self):
        # The manifest's pinned tag is behind the default branch between
        # releases, so a nightly that built it would ship a Flatpak unrelated
        # to the .deb/AppImage/.pyz beside it — and fail the payload audit,
        # which is what stopped every nightly release after 2.1.2.
        script = BUILD_RELEASE.read_text(encoding="utf-8")
        self.assertIn('CHANNEL="${BOL_RELEASE_CHANNEL:-release}"', script)
        guard = script.index('if [[ "$CHANNEL" == "release" ]]; then')
        build = script.index('bash "$SRC/scripts/build-flatpak.sh"', guard)
        self.assertIn("FLATPAK_ARGS=(--release)", script[guard:build])
        self.assertNotIn(
            'build-flatpak.sh" --release', script,
            "the Flatpak mode must follow the channel, not be hard-coded")

        workflow = (ROOT / ".github/workflows/build-app.yml").read_text(
            encoding="utf-8")
        self.assertIn("BOL_RELEASE_CHANNEL: ${{ inputs.channel || 'release' }}",
                      workflow)

    def test_the_appimage_delta_sidecar_ships_with_the_appimage(self):
        # Issue #191: the .zsync holds block checksums of one exact AppImage.
        # Published without it, or kept from a previous build, every updater
        # would compute deltas against bytes nobody is serving — so it is
        # cleared, checksummed, attested and uploaded with the AppImage, and
        # never handed to the candidate verifier, which reads payloads.
        script = BUILD_RELEASE.read_text(encoding="utf-8")
        self.assertIn('APPIMAGE_ZSYNC="$APPIMAGE.zsync"', script)
        self.assertIn('rm -f -- "$APPIMAGE" "$APPIMAGE_ZSYNC"', script)
        self.assertIn('built_artifacts+=("$APPIMAGE_ZSYNC")', script)
        self.assertNotIn('verified_artifacts+=("$APPIMAGE_ZSYNC")', script)

        # The AppImage glob does not match the sidecar; each list needs it.
        workflow = (ROOT / ".github/workflows/build-app.yml").read_text(
            encoding="utf-8")
        self.assertEqual(
            workflow.count("dist/BedrockOnLinux-*-x86_64.AppImage\n"),
            workflow.count("dist/BedrockOnLinux-*-x86_64.AppImage.zsync\n"))
        self.assertEqual(
            workflow.count("dist/BedrockOnLinux-*-x86_64.AppImage.zsync\n"), 3)

    def test_stale_app_artifacts_are_removed_but_shared_assets_survive(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            scripts = checkout / "scripts"
            bol = checkout / "bol"
            data = checkout / "data"
            dist = checkout / "dist"
            scripts.mkdir()
            bol.mkdir()
            data.mkdir()
            dist.mkdir()

            shutil.copy2(BUILD_RELEASE, scripts / BUILD_RELEASE.name)
            shutil.copy2(VERIFY, scripts / VERIFY.name)
            (bol / "__init__.py").write_text("", encoding="utf-8")
            (checkout / "LICENSE").write_text(
                "fixture project license\n", encoding="utf-8")
            (data / "icon.png").write_bytes(b"fixture icon")

            engine = dist / f"GDK-Proton-xuser-{WINEGDK_BUILD_REV}.tar.gz"
            engine.write_bytes(b"reviewed engine fixture")
            engine_sha = hashlib.sha256(engine.read_bytes()).hexdigest()
            (engine.with_suffix(engine.suffix + ".sha256")).write_text(
                f"{engine_sha}  {engine.name}\n", encoding="utf-8")
            xcurl = dist / f"openssl-xcurl-set-{OPENSSL_XCURL_REV}.tar.gz"
            xcurl.write_bytes(b"reviewed xcurl fixture")
            xcurl_sha = hashlib.sha256(xcurl.read_bytes()).hexdigest()
            (bol / "config.py").write_text(
                _config(archive_sha=engine_sha, xcurl_sha=xcurl_sha),
                encoding="utf-8")

            # Avoid network and heavyweight package construction.  These
            # failures must be handled as optional formats by build-release.sh.
            for name in ("build-deb.sh", "build-rpm.sh", "build-appimage.sh",
                         "build-flatpak.sh"):
                (scripts / name).write_text(
                    f"#!{_BASH}\nexit 1\n", encoding="utf-8")

            stale = (
                dist / "BedrockOnLinux-x86_64.AppImage",
                dist / f"BedrockOnLinux-{VERSION}-x86_64.AppImage",
                dist / "BedrockOnLinux-1.2.9-x86_64.AppImage",
                dist / "BedrockOnLinux-1.2.9-x86_64.AppImage.zsync",
                dist / f"BedrockOnLinux-{VERSION}-x86_64.AppImage.zsync",
                dist / "bedrock-on-linux-1.2.9.pyz",
                dist / "bedrock-on-linux_1.2.9_all.deb",
                dist / "bedrock-on-linux-1.2.9-1.x86_64.rpm",
                dist / "BedrockOnLinux-1.2.9-SHA256SUMS",
                dist / "BedrockOnLinux-1.2.9-portable.tar.gz",
            )
            for path in stale:
                path.write_text("stale", encoding="utf-8")

            preserved = (
                dist / "config.txt",
                dist / "openssl-xcurl-set-fixture.tar.gz",
                dist / "GDK-Proton-xuser-fixture.tar.gz",
            )
            for path in preserved:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("keep", encoding="utf-8")
            legacy_cache = dist / ".cache/toolchain.tar.gz"
            legacy_cache.parent.mkdir(parents=True)
            legacy_cache.write_text("stale build cache", encoding="utf-8")

            result = subprocess.run(
                ["bash", str(scripts / "build-release.sh")],
                cwd=checkout,
                env={**os.environ, "BOL_ALLOW_PARTIAL_ARTIFACTS": "1"},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            for path in stale:
                self.assertFalse(path.exists(), path)
            for path in preserved:
                self.assertEqual(path.read_text(encoding="utf-8"), "keep")
            self.assertFalse(legacy_cache.exists())

            current_pyz = dist / f"bedrock-on-linux-{VERSION}.pyz"
            checksum = dist / f"BedrockOnLinux-{VERSION}-SHA256SUMS"
            self.assertTrue(current_pyz.is_file())
            self.assertFalse(any(dist.glob("*.AppImage")))
            self.assertNotIn(
                f"\u2713 dist/BedrockOnLinux-{VERSION}-x86_64.AppImage",
                result.stdout,
            )

            expected = hashlib.sha256(current_pyz.read_bytes()).hexdigest()
            # The app checksum file lists only the attached app artifacts.
            self.assertEqual(
                checksum.read_text(encoding="utf-8"),
                f"{expected}  {current_pyz.name}\n",
            )
            # Engine + XCurl inputs (separately released, not attached) live in
            # a sidecar inputs checksum file, not the app SHA256SUMS.
            inputs = dist / f"BedrockOnLinux-{VERSION}-inputs.sha256"
            self.assertEqual(
                inputs.read_text(encoding="utf-8"),
                f"{engine_sha}  {engine.name}\n"
                f"{xcurl_sha}  {xcurl.name}\n",
            )

            first_pyz = current_pyz.read_bytes()
            os.utime(bol / "config.py", (1900000000, 1900000000))
            second = subprocess.run(
                ["bash", str(scripts / "build-release.sh")],
                cwd=checkout,
                env={**os.environ, "BOL_ALLOW_PARTIAL_ARTIFACTS": "1"},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(current_pyz.read_bytes(), first_pyz)


class RunCandidateSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.checkout = Path(self.tmpdir.name)
        (self.checkout / "scripts").mkdir()
        (self.checkout / "bol").mkdir()
        shutil.copy2(RUN_CANDIDATE,
                     self.checkout / "scripts/run-candidate.sh")
        (self.checkout / "bol/__init__.py").write_text(
            "", encoding="utf-8")
        (self.checkout / "bol/config.py").write_text(
            _config(), encoding="utf-8")
        self.archive = self.checkout / "dist" / (
            f"GDK-Proton-xuser-{WINEGDK_BUILD_REV}.tar.gz")
        self.archive.parent.mkdir()
        self.archive.write_bytes(b"local candidate fixture")
        self.engine = self.checkout / "installed-engine"
        self.engine.mkdir()
        self.launch_marker = self.checkout / "launched.txt"
        self.validation_marker = self.checkout / "validated.txt"

        (self.checkout / "bol/winegdk.py").write_text(
            "import os\n"
            "from pathlib import Path\n"
            "WINEGDK_OUT = Path(os.environ['TEST_ENGINE'])\n"
            "def _install_prebuilt_winegdk(force=False):\n"
            "    assert force is True\n"
            "    return os.environ.get('TEST_ACCEPT') == '1'\n"
            "def ensure_winegdk(force=False):\n"
            "    assert force is False\n"
            "    return WINEGDK_OUT\n",
            encoding="utf-8",
        )
        (self.checkout / "bol/vkd3d.py").write_text(
            "import os\n"
            "from pathlib import Path\n"
            "from types import SimpleNamespace\n"
            "def validate_engine_manifest(root, revision, enforce_pins=False):\n"
            "    assert enforce_pins is True\n"
            "    Path(os.environ['TEST_VALIDATION_MARKER']).write_text(\n"
            "        revision, encoding='utf-8')\n"
            "    if os.environ.get('TEST_VALIDATE') != '1':\n"
            "        raise ValueError('stub manifest rejection')\n"
            "    actual = os.environ.get('TEST_MANIFEST_REV', revision)\n"
            "    return SimpleNamespace(build_rev=actual)\n",
            encoding="utf-8",
        )
        launcher = self.checkout / "bedrock-on-linux"
        launcher.write_text(
            f"#!{_BASH}\n"
            "printf '%s\\n' \"$@\" > \"$TEST_LAUNCH_MARKER\"\n",
            encoding="utf-8",
        )
        launcher.chmod(0o755)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _run(self, *, accept, validate=True, manifest_rev=None):
        env = os.environ.copy()
        env.update({
            "TEST_ENGINE": str(self.engine),
            "TEST_ACCEPT": "1" if accept else "0",
            "TEST_VALIDATE": "1" if validate else "0",
            "TEST_LAUNCH_MARKER": str(self.launch_marker),
            "TEST_VALIDATION_MARKER": str(self.validation_marker),
        })
        if manifest_rev is not None:
            env["TEST_MANIFEST_REV"] = manifest_rev
        return subprocess.run(
            [str(self.checkout / "scripts/run-candidate.sh")],
            cwd=self.checkout,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_rejected_candidate_never_runs_with_preserved_engine(self):
        result = self._run(accept=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("candidate was rejected", result.stderr)
        self.assertFalse(self.launch_marker.exists())
        self.assertFalse(self.validation_marker.exists())

    def test_only_launches_after_installed_r12_manifest_is_revalidated(self):
        result = self._run(accept=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.validation_marker.read_text(encoding="utf-8"),
            WINEGDK_BUILD_REV,
        )
        self.assertEqual(self.launch_marker.read_text(encoding="utf-8"),
                         "gui\n")
        self.assertIn(
            f"Installed game engine validated: {WINEGDK_BUILD_REV}",
            result.stdout,
        )

    def test_mislabeled_installed_manifest_prevents_launch(self):
        result = self._run(
            accept=True, manifest_rev="wow64-archs-r10")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("installed candidate is wow64-archs-r10", result.stderr)
        self.assertFalse(self.launch_marker.exists())


class EnginePackagingComplianceTests(unittest.TestCase):
    def test_package_embeds_and_rechecks_distribution_provenance(self):
        script = PACKAGE_ENGINE.read_text(encoding="utf-8")
        for name in (
            "COPYING.LGPL-2.1",
            "provenance.env",
            "submodules.lock",
            "OUTPUT-SHA256SUMS",
            "restore-nv-dgc.patch",
            ".bol-winegdk-build.env",
            ".bol-winegdk-package-versions.tsv",
            "online-patches-after-user-ready.patch",
            "0001-winegdk-native5-Xbox-and-file-picker-runtime.patch",
            "0002-windows.storage-use-legacy-single-file-dialog.patch",
            "SOURCE-SHA256SUMS",
            "xgameruntime.dll.threading",
            "windows.storage.dll",
            "Microsoft.Windows.Storage.Pickers.FileOpenPicker",
            "PickSingleFileAsync",
            "PickMultipleFilesAsync",
            "GDK-Proton10-32.tar.gz",
            "1e80f4e714f877f42101d5775bd38ca0a15a38d304e24af1f15c6deec4ebac2d",
            "4c3e3faf5bcd86779e4a69677902dd8b36a16e1136a5901e616c36d8a02b68f6",
        ):
            self.assertIn(name, script)
        self.assertIn("licenses-and-provenance", script)
        self.assertIn('rm -rf "$STAGED_ENGINE/files/include"', script)
        self.assertIn(
            '"$WINEGDK_PROVENANCE_DEST/COPYING.LGPL-2.1"', script)
        self.assertIn("ARCHIVE_MEMBERS", script)
        self.assertIn("verify_winegdk_source_provenance", script)
        self.assertIn("native Xbox app configuration ready", script)
        self.assertIn('"critical_files": critical_files', script)
        self.assertIn("REQUIRED_CRITICAL_FILE_PATHS", script)
        self.assertIn("REQUIRED_PINNED_CRITICAL_HASHES", script)
        self.assertIn("GDK_PROTON_THREADING_MEMBER", script)
        self.assertIn(
            'SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$WINEGDK_SOURCE_DATE_EPOCH}"',
            script,
        )

    def test_package_requires_native_file_picker_in_both_architectures(self):
        script = PACKAGE_ENGINE.read_text(encoding="utf-8")
        loop = script.index('for arch in "${ARCHES[@]}"; do',
                            script.index("# The native engine must expose"))
        module = script.index(
            'storage="$STAGED_ENGINE/files/lib/wine/$arch/windows.storage.dll"',
            loop,
        )
        class_marker = script.index(
            '"Microsoft.Windows.Storage.Pickers.FileOpenPicker"', module)
        method_marker = script.index('"PickSingleFileAsync"', class_marker)
        multi_method_marker = script.index(
            '"PickMultipleFilesAsync"', method_marker)
        registration = script.index(
            'has_file_picker_registration "$storage"', method_marker)
        loop_end = script.index("\ndone", registration)
        self.assertLess(loop, module)
        self.assertLess(module, class_marker)
        self.assertLess(class_marker, method_marker)
        self.assertLess(method_marker, multi_method_marker)
        self.assertLess(method_marker, registration)
        self.assertLess(registration, loop_end)

        registration_helper = script[
            script.index("has_file_picker_registration()"):
            script.index("\n}\n", script.index(
                "has_file_picker_registration()")) + 3]
        self.assertIn("ForceRemove Microsoft", registration_helper)
        self.assertIn("'DllPath' = s '%MODULE%'", registration_helper)

    def test_memory_patch_import_guard_is_pipefail_safe(self):
        script = PACKAGE_ENGINE.read_text(encoding="utf-8")
        guard = script[script.index('if objdump -p "$xgdk"'):
                       script.index("\ndone", script.index(
                           'if objdump -p "$xgdk"'))]
        self.assertIn('objdump -p "$xgdk" | grep -E', guard)
        self.assertNotIn("grep -Eq", guard)
        self.assertIn(">/dev/null", guard)

    def test_stage_is_prepatched_before_critical_hashes_are_computed(self):
        script = PACKAGE_ENGINE.read_text(encoding="utf-8")
        patch = script.index("patch_proton(stage, strict=False)")
        remove_backups = script.index('stage.rglob("*.bol-orig")')
        critical_hashes = script.index("critical_files = {}")
        self.assertLess(patch, remove_backups)
        self.assertLess(remove_backups, critical_hashes)
        self.assertIn('path.name + ".bol-detached"', script)
        self.assertIn("os.replace(detached, path)", script)
        self.assertNotIn("patch_proton(Path(ENGINE_DIR", script)

    def test_packaging_snapshot_is_locked_and_avoids_unlocked_hardlinks(self):
        script = PACKAGE_ENGINE.read_text(encoding="utf-8")
        lock = script.index('flock "$ENGINE_LOCK_FD"')
        hardlink = script.index('cp -al "$ENGINE_DIR/."')
        archive = script.index("LC_ALL=C tar --sort=name")
        self.assertLess(lock, hardlink)
        self.assertLess(hardlink, archive)
        self.assertIn("cp -a --reflink=always", script)


if __name__ == "__main__":
    unittest.main()
