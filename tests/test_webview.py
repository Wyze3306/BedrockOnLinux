"""Regression tests for the bundled WebKitGTK runtime (issue #184).

xodus-cli cannot start at all on a host without WebKitGTK -- not the sign-in,
not the download, and not an installed game, whose executable stays encrypted.
These cover the bundle that stands in for the missing library on the immutable
images that cannot install one.
"""
# SPDX-License-Identifier: MIT

import ast
import hashlib
import io
import json
import os
import stat
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import launch, webview, xodus

_EXEC_DIR = webview.XODUS_WEBVIEW_EXEC_DIR


def _encrypted_build(game_dir):
    """A game directory xodus-cli run has something to decrypt from."""
    game_dir.mkdir(parents=True, exist_ok=True)
    (game_dir / xodus.PACKAGE_CACHE).write_bytes(b"package")
    return game_dir


def _restore_map(wrapper):
    """The environment the generated wrapper puts back before the game runs."""
    line = next(text for text in wrapper.splitlines()
                if text.startswith("WEBVIEW_ENV = "))
    return json.loads(ast.literal_eval(
        line.split("json.loads(", 1)[1].rsplit(")", 1)[0]))


def _library(extra=b""):
    """Bytes shaped like the bundled library: one helper-directory literal."""
    return (b"\x7fELF" + b"filler\x00" * 4 + _EXEC_DIR.encode() + b"\x00"
            + b"more filler\x00" + extra)


def _bundle_archive(path, rev="testrev"):
    """A tarball shaped like the published xodus-webview asset."""
    path.parent.mkdir(parents=True, exist_ok=True)
    members = {
        "lib/libwebkit2gtk-4.1.so.0": _library(),
        "libexec/webkit2gtk-4.1/WebKitWebProcess": b"\x7fELFhelper",
        "libexec/webkit2gtk-4.1/WebKitNetworkProcess": b"\x7fELFhelper",
        "libexec/webkit2gtk-4.1/injected-bundle/"
        "libwebkit2gtkinjectedbundle.so": b"\x7fELFbundle",
        "pixbuf-loaders/loaders.cache":
            f'"{webview._PLACEHOLDER}/pixbuf-loaders/libpixbufloader-png.so"\n'
            .encode(),
        "PACKAGES": b"libwebkit2gtk-4.1-0 2.52.5-1\n",
    }
    with tarfile.open(path, "w:xz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755 if "libexec" in name else 0o644
            archive.addfile(info, io.BytesIO(data))
    return hashlib.sha256(path.read_bytes()).hexdigest()


class HostLibraryTests(unittest.TestCase):
    def test_the_package_name_follows_the_package_manager(self):
        for manager, package in (("apt-get", "libwebkit2gtk-4.1-0"),
                                 ("dnf", "webkit2gtk4.1"),
                                 ("pacman", "webkit2gtk-4.1"),
                                 ("zypper", "libwebkit2gtk-4_1-0")):
            with mock.patch.object(webview.shutil, "which",
                                   lambda name, want=manager:
                                   "/usr/bin/x" if name == want else None):
                self.assertEqual(webview.host_package_name(), package)

    def test_an_unknown_host_still_names_a_package(self):
        with mock.patch.object(webview.shutil, "which", lambda name: None):
            self.assertEqual(webview.host_package_name(),
                             "libwebkit2gtk-4.1-0")

    def test_loadability_is_measured_not_guessed(self):
        """The library can be listed by ldconfig and still not load."""
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "xodus-cli"
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)
            self.assertTrue(webview.binary_loads(binary))

            broken = Path(tmp) / "broken"
            broken.write_text("#!/bin/sh\nexit 127\n")
            broken.chmod(0o755)
            self.assertFalse(webview.binary_loads(broken))
            self.assertFalse(webview.binary_loads(Path(tmp) / "absent"))


class InstallTests(unittest.TestCase):
    def _install(self, base, digest, rev="testrev"):
        target = base / "xodus-webview"
        return mock.patch.multiple(
            webview,
            XODUS_WEBVIEW_REV=rev,
            XODUS_WEBVIEW_SHA256=digest,
            XODUS_WEBVIEW_DIR=target,
            ASSET=f"xodus-webview-{rev}.tar.xz",
        ), target

    def test_a_matching_digest_installs_the_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            asset = base / "sibling" / "xodus-webview-testrev.tar.xz"
            digest = _bundle_archive(asset)
            patched, target = self._install(base, digest)

            with patched, mock.patch.object(webview.sys, "argv",
                                            [str(asset.parent / "launcher")]):
                root = webview.ensure_runtime()
                self.assertTrue(webview.installed())

            self.assertEqual(root, target)
            self.assertTrue((target / webview._WEBKIT_LIB).is_file())
            self.assertEqual((target / ".rev").read_text().strip(), "testrev")

    def test_the_loader_cache_is_pointed_at_the_install(self):
        """It stores absolute paths, which only exist after unpacking."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            asset = base / "sibling" / "xodus-webview-testrev.tar.xz"
            digest = _bundle_archive(asset)
            patched, target = self._install(base, digest)

            with patched, mock.patch.object(webview.sys, "argv",
                                            [str(asset.parent / "launcher")]):
                webview.ensure_runtime()

            cache = (target / "pixbuf-loaders" / "loaders.cache").read_text()
            self.assertNotIn(webview._PLACEHOLDER, cache)
            self.assertIn(str(target / "pixbuf-loaders"), cache)

    def test_a_wrong_digest_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            asset = base / "sibling" / "xodus-webview-testrev.tar.xz"
            _bundle_archive(asset)
            patched, target = self._install(base, "00" * 32)

            with patched, mock.patch.object(webview.sys, "argv",
                                            [str(asset.parent / "launcher")]), \
                    self.assertRaises(webview.BolError) as raised:
                webview.ensure_runtime()

            self.assertIn("SHA-256 mismatch", str(raised.exception))
            self.assertFalse(target.exists())

    def test_an_unpublished_runtime_installs_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            asset = base / "sibling" / "xodus-webview-testrev.tar.xz"
            _bundle_archive(asset)
            patched, target = self._install(base, "")

            with patched, mock.patch.object(webview.sys, "argv",
                                            [str(asset.parent / "launcher")]), \
                    self.assertRaises(webview.BolError) as raised:
                webview.ensure_runtime()

            self.assertIn("not been published", str(raised.exception))
            self.assertFalse(target.exists())

    def test_an_archive_without_webkitgtk_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            asset = base / "sibling" / "xodus-webview-testrev.tar.xz"
            asset.parent.mkdir(parents=True)
            with tarfile.open(asset, "w:xz") as archive:
                info = tarfile.TarInfo("PACKAGES")
                info.size = 3
                archive.addfile(info, io.BytesIO(b"nil"))
            digest = hashlib.sha256(asset.read_bytes()).hexdigest()
            patched, target = self._install(base, digest)

            with patched, mock.patch.object(webview.sys, "argv",
                                            [str(asset.parent / "launcher")]), \
                    self.assertRaises(webview.BolError):
                webview.ensure_runtime()

            self.assertFalse(webview.installed())


class HelperRelocationTests(unittest.TestCase):
    """WebKitGTK spawns its helpers from a path compiled into the library."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "bundle"
        (self.root / "lib").mkdir(parents=True)
        self.library = self.root / webview._WEBKIT_LIB
        self.library.write_bytes(_library())
        self.addCleanup(self.tmp.cleanup)

    def test_the_literal_is_replaced_in_place(self):
        target = Path("/run/user/1000/bol-webkit")
        webview._point_helpers_at(self.root, target)

        data = self.library.read_bytes()
        self.assertEqual(len(data), len(_library()))
        self.assertIn(b"/run/user/1000/bol-webkit\x00", data)
        self.assertNotIn(_EXEC_DIR.encode() + b"\x00", data)

    def test_repeating_it_is_harmless(self):
        target = Path("/run/user/1000/bol-webkit")
        webview._point_helpers_at(self.root, target)
        first = self.library.read_bytes()
        webview._point_helpers_at(self.root, target)
        self.assertEqual(self.library.read_bytes(), first)

    def test_a_path_that_does_not_fit_is_refused(self):
        with self.assertRaises(webview.BolError) as raised:
            webview._point_helpers_at(self.root, Path("/run/user/" + "x" * 80))
        self.assertIn("too long", str(raised.exception))
        self.assertIn(_EXEC_DIR.encode(), self.library.read_bytes())

    def test_a_library_without_the_literal_is_reported(self):
        self.library.write_bytes(b"\x7fELF nothing to relocate here")
        with self.assertRaises(webview.BolError) as raised:
            webview._point_helpers_at(self.root, Path("/run/user/1000/bol-wk"))
        self.assertIn("helper path", str(raised.exception))

    def test_helpers_are_linked_rather_than_copied(self):
        """A copy would lose the RUNPATH that finds the bundled libraries."""
        source = self.root / "libexec" / "webkit2gtk-4.1"
        source.mkdir(parents=True)
        (source / "WebKitWebProcess").write_bytes(b"\x7fELF")
        target = Path(self.tmp.name) / "runtime"
        target.mkdir()

        webview._link_helpers(self.root, target)
        link = target / "WebKitWebProcess"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), str(source / "WebKitWebProcess"))

        webview._link_helpers(self.root, target)      # idempotent
        self.assertEqual(os.readlink(link), str(source / "WebKitWebProcess"))

    def test_the_helper_directory_prefers_the_runtime_dir(self):
        with mock.patch.dict(os.environ,
                             {"XDG_RUNTIME_DIR": self.tmp.name}):
            self.assertEqual(webview.helper_dir(),
                             Path(self.tmp.name) / "bol-webkit")
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": ""}):
            self.assertEqual(webview.helper_dir(),
                             Path(f"/tmp/bol-webkit-{os.getuid()}"))

    def test_a_shared_helper_directory_is_refused(self):
        """/tmp is everyone's, so a directory there has to be ours alone."""
        shared = Path(self.tmp.name) / "shared"
        shared.mkdir(mode=0o777)
        os.chmod(shared, 0o777)
        with self.assertRaises(webview.BolError) as raised:
            webview._own_private_dir(shared)
        self.assertIn("not private", str(raised.exception))

        private = Path(self.tmp.name) / "private"
        self.assertEqual(webview._own_private_dir(private), private)
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o700)


class EnvironmentTests(unittest.TestCase):
    def test_the_runtime_is_added_and_can_be_taken_back_out(self):
        env = {"LD_LIBRARY_PATH": "/opt/lib", "GTK_IM_MODULE": "ibus",
               "PATH": "/usr/bin"}
        root = Path("/data/xodus-webview")

        updated, previous = webview.runtime_env(dict(env), root)
        self.assertEqual(updated["LD_LIBRARY_PATH"],
                         f"{root / 'lib'}{os.pathsep}/opt/lib")
        self.assertEqual(updated["GSETTINGS_SCHEMA_DIR"], str(root / "schemas"))
        self.assertEqual(updated["GTK_IM_MODULE"], "gtk-im-context-simple")
        self.assertEqual(updated["PATH"], "/usr/bin")

        webview.restore_env(updated, previous)
        self.assertEqual(updated, env)

    def test_the_dmabuf_renderer_is_turned_off(self):
        """Issue #186: with it on, the sign-in window dies on Wayland."""
        env = {"PATH": "/usr/bin"}
        previous = webview.portable_renderer(env)
        self.assertEqual(env[webview._RENDERER], "1")

        webview.restore_env(env, previous)
        self.assertEqual(env, {"PATH": "/usr/bin"})

    def test_an_explicit_renderer_setting_wins(self):
        """Someone whose desktop is fine can ask for the accelerated path."""
        env = {webview._RENDERER: "0"}
        self.assertEqual(webview.portable_renderer(env),
                         {webview._RENDERER: "0"})
        self.assertEqual(env, {webview._RENDERER: "0"})

    def test_the_accessibility_bridge_is_turned_off(self):
        """Issue #236: WebKit's AT-SPI text interface aborts the web process.

        Nothing about the window changes -- it just stops being something an
        accessibility client can walk into that code through.
        """
        env = {"PATH": "/usr/bin"}
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(webview._A11Y_OPT_IN, None)
            previous = webview.quiet_accessibility(env)
        self.assertEqual(env[webview._A11Y], "")

        webview.restore_env(env, previous)
        self.assertEqual(env, {"PATH": "/usr/bin"})

    def test_an_accessibility_bus_the_session_set_wins(self):
        """Set is what counts, not set to something: empty already means off."""
        for address in ("unix:path=/run/user/1000/at-spi/bus_0", ""):
            env = {webview._A11Y: address}
            self.assertEqual(webview.quiet_accessibility(env),
                             {webview._A11Y: address})
            self.assertEqual(env, {webview._A11Y: address})

    def test_the_accessibility_bridge_can_be_asked_back(self):
        """BOL_WEBVIEW_A11Y=1, for whoever needs that page read out to them."""
        env = {"PATH": "/usr/bin"}
        with mock.patch.dict(os.environ, {webview._A11Y_OPT_IN: "1"}):
            self.assertEqual(webview.quiet_accessibility(env),
                             {webview._A11Y: None})
        self.assertEqual(env, {"PATH": "/usr/bin"})

    def test_every_xodus_command_gets_the_renderer_setting(self):
        """Not the sign-in alone: the download draws the same window."""
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "xodus-cli"
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)

            with mock.patch.dict(os.environ, {}, clear=False), \
                    mock.patch.object(xodus, "home",
                                      return_value=Path(tmp) / "xodus-home"):
                os.environ.pop(webview._RENDERER, None)
                os.environ.pop(webview._A11Y, None)
                os.environ.pop(webview._A11Y_OPT_IN, None)
                environment = xodus._env(binary)
                self.assertEqual(environment[webview._RENDERER], "1")
                self.assertEqual(environment[webview._A11Y], "")
            # The launcher's own environment is never touched by that.
            self.assertNotIn(webview._RENDERER, os.environ)
            self.assertNotIn(webview._A11Y, os.environ)

    def test_a_host_that_can_run_the_binary_keeps_its_own_library(self):
        """Nothing of the bundle is added -- but the renderer setting is."""
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "xodus-cli"
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)
            env = {"PATH": "/usr/bin"}

            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(webview._A11Y_OPT_IN, None)
                previous = webview.apply(binary, env)
            self.assertEqual(previous, {webview._RENDERER: None,
                                        webview._A11Y: None})
            self.assertEqual(env, {"PATH": "/usr/bin", webview._RENDERER: "1",
                                   webview._A11Y: ""})
            webview.restore_env(env, previous)
            self.assertEqual(env, {"PATH": "/usr/bin"})

    def test_a_host_without_webkitgtk_gets_the_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            binary = base / "xodus-cli"
            # Stands in for the loader failure: it only starts when the
            # bundle's library directory is on LD_LIBRARY_PATH.
            binary.write_text(
                "#!/bin/sh\ncase \"$LD_LIBRARY_PATH\" in *xodus-webview*) "
                "exit 0;; *) exit 127;; esac\n")
            binary.chmod(0o755)
            asset = base / "sibling" / "xodus-webview-testrev.tar.xz"
            digest = _bundle_archive(asset)
            env = {"PATH": "/usr/bin"}

            with mock.patch.multiple(webview,
                                     XODUS_WEBVIEW_REV="testrev",
                                     XODUS_WEBVIEW_SHA256=digest,
                                     XODUS_WEBVIEW_DIR=base / "xodus-webview",
                                     ASSET="xodus-webview-testrev.tar.xz"), \
                    mock.patch.object(webview.sys, "argv",
                                      [str(asset.parent / "launcher")]), \
                    mock.patch.dict(os.environ,
                                    {"XDG_RUNTIME_DIR": str(base / "run")}):
                (base / "run").mkdir()
                previous = webview.apply(binary, env)

            self.assertIsNotNone(previous)
            self.assertIn("xodus-webview", env["LD_LIBRARY_PATH"])
            self.assertEqual(previous["LD_LIBRARY_PATH"], None)
            self.assertTrue((base / "run" / "bol-webkit" /
                             "WebKitWebProcess").is_symlink())

    def test_an_unavailable_runtime_names_the_host_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "xodus-cli"
            binary.write_text("#!/bin/sh\nexit 127\n")
            binary.chmod(0o755)
            env = {"PATH": "/usr/bin"}

            with mock.patch.multiple(webview,
                                     XODUS_WEBVIEW_SHA256="",
                                     XODUS_WEBVIEW_DIR=Path(tmp) / "absent"), \
                    self.assertRaises(webview.BolError) as raised:
                webview.apply(binary, env)

            message = str(raised.exception)
            self.assertIn(webview.host_package_name(), message)
            self.assertIn("Flatpak", message)
            self.assertEqual(env, {"PATH": "/usr/bin"})

    def test_a_bundle_that_does_not_load_says_what_the_loader_said(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            binary = base / "xodus-cli"
            binary.write_text(
                "#!/bin/sh\necho 'xodus-cli: error while loading shared "
                "libraries: libicuuc.so.76: cannot open shared object file' "
                ">&2\nexit 127\n")
            binary.chmod(0o755)
            env = {"PATH": "/usr/bin"}

            with mock.patch.object(webview, "prepare",
                                   return_value=base / "bundle"), \
                    self.assertRaises(webview.BolError) as raised:
                webview.apply(binary, env)

            self.assertIn("libicuuc.so.76", str(raised.exception))
            self.assertEqual(env, {"PATH": "/usr/bin"})


# What xodus-cli printed on Ubuntu 22.04 (glibc 2.35), as issue #264 quotes it.
_OLD_GLIBC = (
    "xodus-cli: /lib/x86_64-linux-gnu/libc.so.6: version `GLIBC_2.38' not "
    "found (required by /home/u/.local/share/bedrock-on-linux/xodus/"
    "xodus-cli)\n"
    "xodus-cli: /lib/x86_64-linux-gnu/libc.so.6: version `GLIBC_2.39' not "
    "found (required by /home/u/.local/share/bedrock-on-linux/xodus/"
    "xodus-cli)\n")


class OldGlibcTests(unittest.TestCase):
    """A C library older than xodus-cli is not a missing WebKitGTK (#264)."""

    def test_the_newest_version_asked_for_is_named(self):
        with mock.patch.object(webview, "_host_glibc", return_value="2.35"):
            message = webview.glibc_too_old_message(_OLD_GLIBC)
        self.assertIn("glibc 2.39 or newer", message)
        self.assertIn("this system has glibc 2.35", message)
        self.assertIn("Flatpak", message)
        self.assertNotIn(webview.host_package_name(), message)

    def test_versions_compare_as_numbers(self):
        message = webview.glibc_too_old_message(
            "version `GLIBC_2.9' not found\nversion `GLIBC_2.10' not found")
        self.assertIn("glibc 2.10 or newer", message)

    def test_other_loader_failures_are_not_about_glibc(self):
        self.assertIsNone(webview.glibc_too_old_message(
            "xodus-cli: error while loading shared libraries: "
            "libwebkit2gtk-4.1.so.0: cannot open shared object file"))
        self.assertIsNone(webview.glibc_too_old_message(""))
        self.assertIsNone(webview.glibc_too_old_message(None))

    def test_the_bundle_is_not_downloaded_for_an_old_glibc(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "xodus-cli"
            binary.write_text(
                "#!/bin/sh\ncat >&2 <<'EOF'\n" + _OLD_GLIBC + "EOF\nexit 1\n")
            binary.chmod(0o755)
            env = {"PATH": "/usr/bin"}

            with mock.patch.object(
                    webview, "prepare",
                    side_effect=AssertionError(
                        "the bundle is built against the same glibc")), \
                    self.assertRaises(webview.BolError) as raised:
                webview.apply(binary, env)

            self.assertIn("glibc 2.39 or newer", str(raised.exception))
            self.assertEqual(env, {"PATH": "/usr/bin"})

    def test_a_failed_xodus_command_reports_the_glibc_floor(self):
        message = xodus._loader_failure(_OLD_GLIBC)
        self.assertIn("glibc 2.39 or newer", message)
        self.assertNotIn(webview.host_package_name(), message)


class DoctorStatusTests(unittest.TestCase):
    def test_the_host_library_needs_no_package(self):
        with mock.patch.object(webview, "host_has_webkitgtk", lambda: True):
            self.assertEqual(webview.status(), ("OK (store sign-in)", None))

    def test_an_installed_bundle_needs_no_package(self):
        with mock.patch.object(webview, "host_has_webkitgtk", lambda: False), \
                mock.patch.object(webview, "installed", lambda: True):
            self.assertEqual(webview.status(), ("OK (bundled runtime)", None))

    def test_a_published_bundle_is_reported_as_pending(self):
        with mock.patch.object(webview, "host_has_webkitgtk", lambda: False), \
                mock.patch.object(webview, "installed", lambda: False), \
                mock.patch.object(webview, "XODUS_WEBVIEW_SHA256", "ab" * 32):
            summary, package = webview.status()
            self.assertIn("first use", summary)
            self.assertIsNone(package)

    def test_nothing_to_use_asks_for_the_host_package(self):
        with mock.patch.object(webview, "host_has_webkitgtk", lambda: False), \
                mock.patch.object(webview, "installed", lambda: False), \
                mock.patch.object(webview, "XODUS_WEBVIEW_SHA256", ""):
            summary, package = webview.status()
            self.assertIn("MANQUANT", summary)
            self.assertEqual(package, webview.host_package_name())


class LauncherIntegrationTests(unittest.TestCase):
    def test_the_loader_error_is_translated(self):
        """"No such file or directory" is what issue #184 was reported as."""
        message = xodus._loader_failure(
            "/home/deck/.local/share/bedrock-on-linux/xodus/xodus-cli: error "
            "while loading shared libraries: libwebkit2gtk-4.1.so.0: cannot "
            "open shared object file: No such file or directory")
        self.assertIn("libwebkit2gtk-4.1.so.0", message)
        self.assertIn(webview.host_package_name(), message)
        self.assertIsNone(xodus._loader_failure("Package was not found"))

    def test_the_game_is_not_started_with_the_bundle(self):
        """The runtime is for xodus-cli; Wine brings its own libraries."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            binary = base / "xodus" / "xodus-cli"
            binary.parent.mkdir()
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)
            _encrypted_build(base / "game")
            env = {"LD_LIBRARY_PATH": "/game/lib", "PATH": "/usr/bin"}

            with mock.patch.object(xodus, "ensure_cli", lambda: binary), \
                    mock.patch.object(xodus, "signed_in", return_value=True), \
                    mock.patch.object(xodus, "home",
                                      return_value=base / "xodus-home"), \
                    mock.patch.object(
                        xodus.webview, "apply",
                        lambda _binary, environment: (
                            environment.update(LD_LIBRARY_PATH="/bundle/lib"),
                            {"LD_LIBRARY_PATH": "/game/lib"})[1]):
                command = xodus.wrap_encrypted_launch(
                    ["/bin/wine", "Minecraft.Windows.exe"], base / "game",
                    base / "run", env=env)

            self.assertEqual(command[:2], [str(binary), "run"])
            self.assertEqual(env["LD_LIBRARY_PATH"], "/bundle/lib")
            wrapper = (base / "run" / "xodus-launch-wrapper.py").read_text()
            # HOME rides along the same way: xodus-cli reads the licence out
            # of the launcher's Xodus home, the game must not (issue #198).
            self.assertEqual(_restore_map(wrapper),
                             {"LD_LIBRARY_PATH": "/game/lib", "HOME": None})

    def test_a_host_with_webkitgtk_restores_only_the_webview_settings(self):
        """No bundle to take back out, but the game still loses #186 and #236.

        Neither belongs to Minecraft: it draws nothing through WebKitGTK, and
        its own accessibility is Wine's business.
        """
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            binary = base / "xodus" / "xodus-cli"
            binary.parent.mkdir()
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)
            _encrypted_build(base / "game")
            env = {"PATH": "/usr/bin"}

            with mock.patch.object(xodus, "ensure_cli", lambda: binary), \
                    mock.patch.object(xodus, "signed_in", return_value=True), \
                    mock.patch.object(xodus, "home",
                                      return_value=base / "xodus-home"):
                xodus.wrap_encrypted_launch(
                    ["/bin/wine", "Minecraft.Windows.exe"], base / "game",
                    base / "run", env=env)

            self.assertEqual(env[webview._RENDERER], "1")
            wrapper = (base / "run" / "xodus-launch-wrapper.py").read_text()
            self.assertEqual(_restore_map(wrapper),
                             {webview._RENDERER: None, webview._A11Y: None,
                              "HOME": None})

    def test_the_game_launch_hands_over_its_environment(self):
        """env= is what carries both the bundle and #186 into xodus-cli run.

        It was dropped once already, by an unrelated indentation fix, which
        left every encrypted launch running against the session's environment
        instead -- with no library on the hosts that need the bundle.
        """
        tree = ast.parse(Path(launch.__file__).read_text(encoding="utf-8"))
        calls = [node for node in ast.walk(tree)
                 if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)
                 and node.func.attr == "wrap_encrypted_launch"]
        self.assertTrue(calls, "bol.launch no longer wraps encrypted launches")
        for call in calls:
            self.assertIn("env", [word.arg for word in call.keywords],
                          "wrap_encrypted_launch() was called without env=")


if __name__ == "__main__":
    unittest.main()
