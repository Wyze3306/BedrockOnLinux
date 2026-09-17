"""Policy checks for the application artifact build scripts."""
# SPDX-License-Identifier: MIT

from __future__ import annotations

import re
import unittest
from pathlib import Path

from bol import deps


ROOT = Path(__file__).resolve().parents[1]


class ApplicationPackagingPolicyTests(unittest.TestCase):
    def test_appimage_is_relocatable_licensed_and_version_pinned(self):
        script = (ROOT / "scripts/build-appimage.sh").read_text(
            encoding="utf-8")
        self.assertIn("BOL_APPIMAGE_BUILD_CACHE", script)
        self.assertNotIn('CACHE="$OUT/.cache"', script)
        # The Tcl/Tk graft is gone; the bundle is now the PySide6-Essentials
        # wheel closure, dropped in by pip rather than compiled + rpath-fixed.
        self.assertIn('rm -f "$DYN"/_tkinter.*.so', script)
        self.assertIn("usr/share/licenses/bedrock-on-linux/LICENSE", script)
        self.assertIn("cat > \"$APPDIR/AppRun\" <<'EOF'\n#!/bin/sh\n", script)
        self.assertIn('/bin/sh -n "$APPDIR/AppRun"', script)
        self.assertIn('rm -f "$DYN"/_crypt.*.so', script)
        self.assertIn('"libcrypt.so.1", "libXss.so.1"', script)
        self.assertIn('runtime is not statically linked', script)
        # Wheels are hash-pinned, binary-only, no sdist builds; the pinned
        # closure lives in the requirements file the script installs from.
        self.assertIn("--require-hashes --only-binary=:all:", script)
        self.assertIn("third_party/requirements-appimage.txt", script)
        reqs = (ROOT / "third_party/requirements-appimage.txt").read_text(
            encoding="utf-8")
        for requirement in (
                "cryptography==43.0.3", "cffi==2.0.0", "pycparser==3.0",
                "shiboken6==6.9.3", "pyside6-essentials==6.9.3",
                "packaging==26.2", "python-xlib==0.33"):
            self.assertIn(requirement, reqs)
        self.assertIn("--hash=sha256:", reqs)

    def test_appimage_bundle_verification_checks_pyside6_not_tk(self):
        script = (ROOT / "scripts/build-appimage.sh").read_text(
            encoding="utf-8")
        self.assertIn("import shiboken6", script)
        self.assertIn("from PySide6 import __version__ as pyside6_version",
                      script)
        self.assertIn("from PySide6.QtCore import QLibraryInfo", script)
        self.assertIn(
            'plugins_path = QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath)',
            script)
        self.assertIn('"shiboken6": "6.9.3"', script)
        self.assertIn('"pyside6_essentials": "6.9.3"', script)
        # A real QApplication construction, gated on DISPLAY like the Tk
        # check it replaced, proves the xcb platform plugin actually loads.
        self.assertIn("from PySide6.QtWidgets import QApplication", script)
        self.assertIn('app = QApplication(["bedrock-on-linux-appimage-verify"])',
                      script)
        self.assertNotIn("customtkinter", script)
        # tkinter itself is still mentioned, but only where the script drops
        # the interpreter's bundled copy -- it is never installed or imported.
        self.assertNotIn("import tkinter", script)

    def test_appimage_advertises_zsync_delta_updates(self):
        # Issue #191: AppImageUpdate, AppImageLauncher and AM read update
        # information out of the runtime's .upd_info section and then transfer
        # only the changed blocks through the .zsync sidecar, instead of
        # ~200 MB of unchanged bundle.
        script = (ROOT / "scripts/build-appimage.sh").read_text(
            encoding="utf-8")
        self.assertIn(
            "gh-releases-zsync|${SELF_REPO%/*}|${SELF_REPO#*/}|${UPDATE_TAG}"
            "|BedrockOnLinux-*-x86_64.AppImage.zsync", script)
        # The repository is the one the launcher's own updater asks, so a fork
        # points at its own releases rather than at this one.
        self.assertIn("grep -m1 '^WINEGDK_PREBUILT_REPO = '", script)
        # A nightly follows the rolling prerelease; anything else follows the
        # newest stable release.
        self.assertIn('nightly) UPDATE_TAG="nightly" ;;', script)
        self.assertIn('*)       UPDATE_TAG="latest" ;;', script)
        self.assertIn('UPDATE_ARGS=(-u "$UPDATE_INFO")', script)

        # appimagetool names the sidecar after the destination but writes it
        # into the working directory, so the build has to run from dist/ or the
        # sidecar is left wherever the caller happened to be standing.
        packaging = script.index('ZSYNC="$APPIMG.zsync"')
        run = script.index('ARCH=x86_64 "$TOOL"', packaging)
        self.assertIn('cd "$OUT"', script[packaging:run])
        self.assertIn('"${APPIMG##*/}"', script[run:run + 200])

        # appimagetool only warns when zsyncmake is missing, which would ship
        # an AppImage advertising a delta file nobody can fetch.
        self.assertIn(
            "update information was embedded but no $ZSYNC was written",
            script)
        # What the updaters actually read is the runtime section, so that is
        # what the build verifies, along with the sidecar describing these
        # exact bytes and a timestamp pinned like every other one here.
        for check in ('readelf", "--section-headers"',
                      "no .upd_info section",
                      "embedded update information is",
                      "does not match the pattern the ",
                      'for key in ("Filename", "URL"):',
                      'rb"(?m)^MTime: .*$"'):
            self.assertIn(check, script)

    def test_deb_preserves_dependency_licenses_and_normalizes_modes(self):
        script = (ROOT / "scripts/build-deb.sh").read_text(encoding="utf-8")
        self.assertIn("--require-hashes --only-binary=:all:", script)
        self.assertIn("third_party/requirements-deb.txt", script)
        reqs = (ROOT / "third_party/requirements-deb.txt").read_text(
            encoding="utf-8")
        for requirement in (
                "shiboken6==6.9.3", "pyside6-essentials==6.9.3",
                "packaging==26.2", "python-xlib==0.33"):
            self.assertIn(requirement, reqs)
        self.assertIn("--hash=sha256:", reqs)
        self.assertNotIn('*.dist-info', script)
        self.assertIn("usr/share/doc/bedrock-on-linux/copyright", script)
        self.assertIn("-iname 'LICENSE*'", script)
        self.assertIn("-name '.DS_Store' -delete", script)
        self.assertIn("-type d -exec chmod 0755", script)
        self.assertIn("-type f -exec chmod 0644", script)
        self.assertIn('SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1782250551}"',
                      script)
        self.assertIn('touch -h -d "@$SOURCE_DATE_EPOCH"', script)

    def test_rpm_bundles_the_same_audited_payload_as_the_deb(self):
        # Fedora-based distributions (Nobara, Bazzite) asked for an .rpm; it
        # must carry the identical hash-pinned GUI stack the .deb does rather
        # than a second, quietly diverging closure.
        script = (ROOT / "scripts/build-rpm.sh").read_text(encoding="utf-8")
        self.assertIn("--require-hashes --only-binary=:all:", script)
        self.assertIn("third_party/requirements-deb.txt", script)
        self.assertIn("-iname 'LICENSE*'", script)
        self.assertIn("-name '.DS_Store' -delete", script)
        self.assertIn("-type d -exec chmod 0755", script)
        self.assertIn("-type f -exec chmod 0644", script)
        self.assertIn('SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1782250551}"',
                      script)
        self.assertIn('touch -h -d "@$SOURCE_DATE_EPOCH"', script)
        self.assertIn("usr/share/licenses/bedrock-on-linux/LICENSE", script)
        # Generated requires would be satisfied by nothing on the target
        # distributions, so the spec declares every dependency by hand.
        self.assertIn("AutoReqProv:    no", script)
        for requirement in ("python3-cryptography",
                            "/usr/bin/xrandr", "(curl or wget)",
                            "libxcb", "libxkbcommon-x11", "fontconfig"):
            self.assertIn(requirement, script)
        self.assertNotIn("python3-tkinter", script)
        deb = (ROOT / "scripts/build-deb.sh").read_text(encoding="utf-8")
        for metadata in ("shiboken6-6.9.3.dist-info",
                         "pyside6_essentials-6.9.3.dist-info",
                         "packaging-26.2.dist-info",
                         "python_xlib-0.33.dist-info",
                         "six-1.17.0.dist-info"):
            self.assertIn(metadata, deb)
            self.assertIn(metadata, script)

    def test_release_pipeline_builds_and_ships_the_rpm(self):
        build = (ROOT / "scripts/build-release.sh").read_text(encoding="utf-8")
        self.assertIn('bash "$SRC/scripts/build-rpm.sh"', build)
        self.assertIn('required_failures+=(".rpm")', build)
        workflow = (ROOT / ".github/workflows/build-app.yml").read_text(
            encoding="utf-8")
        self.assertIn("dpkg-dev rpm ", workflow)
        # Uploaded to the publish job, attested, and attached to the release.
        self.assertEqual(workflow.count("dist/bedrock-on-linux-*.rpm"), 3)

    def test_zipapp_embeds_license_and_bootstrap_pins_gui_stack(self):
        script = (ROOT / "scripts/build-release.sh").read_text(
            encoding="utf-8")
        self.assertIn('install -m644 "$SRC/LICENSE" "$STAGE/LICENSE"', script)
        self.assertIn(
            'install -Dm644 "$SRC/data/icon.png" "$STAGE/data/icon.png"',
            script,
        )
        self.assertIn('export SOURCE_DATE_EPOCH', script)
        self.assertIn('zipfile.ZipInfo(relative, date_time=date_time)', script)
        self.assertIn('sorted(stage.rglob("*")', script)
        self.assertEqual(
            deps.GUI_INSTALL_REQUIREMENTS,
            (
                "PySide6-Essentials==6.9.3",
                "packaging==26.2",
                "python-xlib==0.33",
            ),
        )

    def test_flatpak_installs_project_license(self):
        manifest = (ROOT / "flatpak/io.github.wyze3306.BedrockOnLinux.yml").read_text(
            encoding="utf-8")
        # Flathub wants every module's licence under share/licenses/$FLATPAK_ID.
        self.assertIn(
            "install -Dm644 LICENSE /app/share/licenses/"
            "io.github.wyze3306.BedrockOnLinux/bedrock-on-linux/LICENSE",
            manifest,
        )

    def test_flatpak_installs_pyside6_with_its_own_python(self):
        manifest = (
            ROOT / "flatpak/io.github.wyze3306.BedrockOnLinux.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("name: python3-dependencies", manifest)
        self.assertIn("--no-index --find-links=", manifest)
        for wheel in (
                "shiboken6-6.9.3-cp39-abi3-manylinux_2_28_x86_64.whl",
                "pyside6_essentials-6.9.3-cp39-abi3-manylinux_2_28_x86_64.whl",
                "packaging-26.2-py3-none-any.whl",
                "python_xlib-0.33-py2.py3-none-any.whl",
                "six-1.17.0-py2.py3-none-any.whl"):
            self.assertIn(wheel, manifest)
        # PySide6 6.9 refuses the runtime's Python 3.14, so the app keeps its
        # own 3.12 and installs the wheels with that interpreter's pip.
        self.assertIn("/Python-3.12.", manifest)
        self.assertIn("/app/bin/python3 -m pip install", manifest)
        self.assertIn("/lib/python3.12/tkinter", manifest)  # still cleaned up
        self.assertNotIn("name: tcl", manifest)
        self.assertNotIn("name: tk", manifest)
        self.assertNotIn("customtkinter", manifest)

    def test_flatpak_game_container_uses_the_portal_not_the_flatpak_service(self):
        manifest = (
            ROOT / "flatpak/io.github.wyze3306.BedrockOnLinux.yml"
        ).read_text(encoding="utf-8")
        # pressure-vessel asks org.freedesktop.portal.Flatpak for its
        # sub-sandbox; the Flatpak service itself is a sandbox escape (#157).
        finish_args = re.findall(r"^  - (--\S+)$", manifest, re.MULTILINE)
        self.assertIn("--allow=per-app-dev-shm", finish_args)
        self.assertFalse([arg for arg in finish_args
                          if arg.startswith("--talk-name=org.freedesktop.Flatpak")])
        self.assertIn("runtime-version: '51'", manifest)

    def test_flatpak_keeps_game_controller_device_access(self):
        manifest = (
            ROOT / "flatpak/io.github.wyze3306.BedrockOnLinux.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("- --device=all\n", manifest)

    def test_release_workflow_uses_updater_compatible_version_tags(self):
        workflow = (ROOT / ".github/workflows/build-app.yml").read_text(
            encoding="utf-8")
        self.assertIn('- "v[0-9]*"', workflow)
        self.assertIn('tag="v${ver}"', workflow)
        self.assertNotIn("app-v", workflow)
        self.assertIn("target_commitish: ${{ github.sha }}", workflow)
        self.assertIn(
            '[ "$GITHUB_REF_NAME" != "v${ver}" ]',
            workflow,
        )
        self.assertIn(
            "make_latest: ${{ env.RELEASE_PRERELEASE == 'false' }}",
            workflow,
        )
        for name in (
                "build-engine.yml", "build-winegdk.yml",
                "build-xcurl.yml", "build-vkd3d.yml"):
            component = (ROOT / ".github/workflows" / name).read_text(
                encoding="utf-8")
            self.assertIn("target_commitish: ${{ github.sha }}", component)
            self.assertIn("make_latest: false", component)

    def test_desktop_entries_launch_gui_and_match_window_class(self):
        for relative in (
                "data/bedrock-on-linux.desktop",
                "flatpak/io.github.wyze3306.BedrockOnLinux.desktop"):
            entry = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("Type=Application\n", entry)
            self.assertIn("Exec=bedrock-on-linux gui\n", entry)
            self.assertIn("Terminal=false\n", entry)
            self.assertIn("StartupWMClass=BedrockOnLinux\n", entry)

    def test_desktop_entries_offer_a_launcher_free_play_action(self):
        for relative in (
                "data/bedrock-on-linux.desktop",
                "flatpak/io.github.wyze3306.BedrockOnLinux.desktop"):
            entry = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("Actions=Play;\n", entry)
            self.assertIn("[Desktop Action Play]\n", entry)
            self.assertIn("Exec=bedrock-on-linux play\n", entry)
            # The action group must follow the main group it belongs to.
            self.assertLess(entry.index("[Desktop Entry]"),
                            entry.index("[Desktop Action Play]"))

    def test_packagers_rewrite_the_launcher_exec_without_the_play_action(self):
        # Both scripts rewrote every Exec= line, which would have redirected
        # the Play action back into the launcher window.
        installer = (ROOT / "scripts/install.sh").read_text(encoding="utf-8")
        self.assertIn(
            'sed "s|^Exec=bedrock-on-linux |Exec=$BIN/bedrock-on-linux |"',
            installer)
        appimage = (ROOT / "scripts/build-appimage.sh").read_text(
            encoding="utf-8")
        self.assertIn(
            "sed '0,/^Exec=/s|^Exec=.*|Exec=bedrock-on-linux gui|'", appimage)


# Every hard DT_NEEDED of the bundled Qt xcb platform plugin that the host has
# to provide, mapped to the name each packaging format declares it under.
# Regenerate against the pinned PySide6 wheel with:
#   readelf -d --wide .../PySide6/Qt/plugins/platforms/libqxcb.so | grep NEEDED
# libEGL is not in that list: it comes from libQt6Gui. zlib1g/libzstd1 are
# left out on purpose -- zlib is priority:required and libzstd1 arrives with
# the zstd dependency the launcher already declares for game downloads.
QT_HOST_LIBRARIES = {
    # soname:                  (Debian package,        RPM soname requires)
    "libX11.so.6":             ("libx11-6",            "libX11.so.6"),
    "libX11-xcb.so.1":         ("libx11-xcb1",         "libX11-xcb.so.1"),
    "libxkbcommon.so.0":       ("libxkbcommon0",       "libxkbcommon.so.0"),
    "libxkbcommon-x11.so.0":   ("libxkbcommon-x11-0",  "libxkbcommon-x11.so.0"),
    "libxcb.so.1":             ("libxcb1",             "libxcb.so.1"),
    "libxcb-cursor.so.0":      ("libxcb-cursor0",      "libxcb-cursor.so.0"),
    "libxcb-icccm.so.4":       ("libxcb-icccm4",       "libxcb-icccm.so.4"),
    "libxcb-image.so.0":       ("libxcb-image0",       "libxcb-image.so.0"),
    "libxcb-keysyms.so.1":     ("libxcb-keysyms1",     "libxcb-keysyms.so.1"),
    "libxcb-randr.so.0":       ("libxcb-randr0",       "libxcb-randr.so.0"),
    "libxcb-render.so.0":      ("libxcb-render0",      "libxcb-render.so.0"),
    "libxcb-render-util.so.0": ("libxcb-render-util0", "libxcb-render-util.so.0"),
    "libxcb-shape.so.0":       ("libxcb-shape0",       "libxcb-shape.so.0"),
    "libxcb-shm.so.0":         ("libxcb-shm0",         "libxcb-shm.so.0"),
    "libxcb-sync.so.1":        ("libxcb-sync1",        "libxcb-sync.so.1"),
    "libxcb-util.so.1":        ("libxcb-util1",        "libxcb-util.so.1"),
    "libxcb-xfixes.so.0":      ("libxcb-xfixes0",      "libxcb-xfixes.so.0"),
    "libxcb-xkb.so.1":         ("libxcb-xkb1",         "libxcb-xkb.so.1"),
    "libGL.so.1":              ("libgl1",              "libGL.so.1"),
    "libEGL.so.1":             ("libegl1",             "libEGL.so.1"),
}


class QtRuntimeDependencyTests(unittest.TestCase):
    """Qt aborts the process natively when its platform plugin cannot load --
    "could not load the Qt platform plugin xcb", raised by the C++ side before
    control ever returns to Python. bol.gui's own _desktop_error() therefore
    never runs, and the user is left with a launcher that exits silently. The
    only defence is declaring the libraries up front, so these are pinned."""

    def _declared_deb(self):
        script = (ROOT / "scripts/build-deb.sh").read_text(encoding="utf-8")
        start = script.index("Depends:")
        end = script.index("Recommends:", start)
        return script[start:end]

    def _declared_rpm(self):
        return (ROOT / "scripts/build-rpm.sh").read_text(encoding="utf-8")

    def test_the_deb_declares_every_library_the_xcb_plugin_loads(self):
        declared = self._declared_deb()
        missing = sorted(
            package for _soname, (package, _rpm) in QT_HOST_LIBRARIES.items()
            if package not in declared)
        self.assertEqual(
            missing, [],
            "build-deb.sh does not declare these, so the launcher aborts "
            f"before it can report anything: {missing}")

    def test_the_rpm_declares_every_library_the_xcb_plugin_loads(self):
        script = self._declared_rpm()
        missing = sorted(
            soname for _s, (_deb, soname) in QT_HOST_LIBRARIES.items()
            if f"Requires:       {soname}()(64bit)" not in script)
        self.assertEqual(
            missing, [],
            f"build-rpm.sh does not declare these sonames: {missing}")

    def test_the_appimage_documents_that_these_stay_host_dependencies(self):
        # An AppImage cannot declare dependencies, so the one thing it can do
        # is say so where a packager will read it.
        script = (ROOT / "scripts/build-appimage.sh").read_text(
            encoding="utf-8")
        self.assertIn("could not load the Qt platform plugin xcb", script)
        self.assertIn("QT_HOST_LIBRARIES", script)


class AppImageBundledLibraryTests(unittest.TestCase):
    """What the AppImage carries itself, and what it still asks the host for.

    Not every library Qt links is a host GUI library. libQt6Core links
    libzstd.so.1, which is plain compression, absent from the AppImage
    excludelist, and absent from NixOS' appimage-run environment: the launcher
    died on `import PySide6.QtCore` with "libzstd.so.1: cannot open shared
    object file" before it could report anything at all (issue #205)."""

    def _script(self):
        return (ROOT / "scripts/build-appimage.sh").read_text(encoding="utf-8")

    def _declared_host_libraries(self):
        block = re.search(r"host_libraries = \{(.*?)\n\}", self._script(),
                          re.DOTALL)
        self.assertIsNotNone(
            block, "the AppImage host dependency audit lost its library list")
        return set(re.findall(r'"([^"]+)"', block.group(1)))

    def test_the_appimage_bundles_the_zstd_runtime_qt_core_links(self):
        script = self._script()
        # Pinned bytes, from the same Debian 11 snapshot the containerised
        # builds use: identical on every build host, and old enough for the
        # glibc baseline the audit enforces.
        self.assertIn("snapshot.debian.org/archive/debian/20260701T000000Z",
                      script)
        self.assertIn("libzstd1_1.4.8+dfsg-2.1_amd64.deb", script)
        self.assertIn(
            "5dcadfbb743bfa1c1c773bff91c018f835e8e8c821d423d3836f3ab84773507b",
            script)
        # Beside the wheel's own Qt libraries, where libQt6Core's $ORIGIN
        # RUNPATH finds it, with the package's licence kept alongside ours.
        self.assertIn('"$QT_LIB/libzstd.so.1"', script)
        self.assertIn("libzstd1.copyright", script)
        self.assertNotIn("libzstd.so.1", self._declared_host_libraries())

    def test_the_appimage_audit_and_the_packages_name_the_same_host_stack(self):
        declared = self._declared_host_libraries()
        missing = sorted(soname for soname in QT_HOST_LIBRARIES
                         if soname not in declared)
        self.assertEqual(
            missing, [],
            "the AppImage audit would accept these silently even though the "
            f".deb and .rpm treat them as host libraries: {missing}")

    def test_the_appimage_audits_the_launcher_qt_import_path(self):
        # The audit walks what bol.gui imports and what Qt loads behind it, so
        # a PySide6 bump that adds a host library fails the build instead of a
        # user's first launch.
        script = self._script()
        for dependant in ("PySide6/QtCore.abi3.so", "PySide6/QtGui.abi3.so",
                          "PySide6/QtWidgets.abi3.so",
                          "PySide6/Qt/plugins/platforms/libqxcb.so"):
            self.assertIn(dependant, script)
        self.assertIn("bundled here nor declared host dependencies", script)


if __name__ == "__main__":
    unittest.main()
