"""The portable launcher fetches its GUI toolkit without pip.

Debian, Ubuntu, Mint and Arch install no pip by default, so the .pyz used to
answer its first GUI launch there with "Could not install the GUI toolkit".
Where pip exists, PEP 668 only let it in with --break-system-packages, into
the user's own site-packages. And PySide6 6.9.3 declares Python < 3.14, so on
Fedora 45's Python 3.15 even pip found nothing to install. These tests hold
the replacement to hash-pinned wheels unpacked into the launcher's own data
directory, chosen by Python line.
"""
# SPDX-License-Identifier: MIT

import hashlib
import io
import os
import re
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from bol import deps

ROOT = Path(__file__).resolve().parent.parent


def _wheel(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
    return buffer.getvalue()


class PinTests(unittest.TestCase):
    def test_the_line_follows_what_each_python_accepts(self):
        # 6.9.3 is the last line on glibc 2.28 and declares Python < 3.14;
        # 6.12.0 is the first that accepts 3.15 (Fedora 45).
        self.assertEqual(deps.pyside6_line((3, 9)), "6.9.3")
        self.assertEqual(deps.pyside6_line((3, 13)), "6.9.3")
        self.assertEqual(deps.pyside6_line((3, 14)), "6.12.0")
        self.assertEqual(deps.pyside6_line((3, 15)), "6.12.0")
        self.assertEqual(deps.GUI_DEPS["PySide6"],
                         "PySide6-Essentials==" + deps.pyside6_line())

    def test_wheel_urls_name_their_pinned_versions(self):
        for line, (glibc, wheels) in deps.PYSIDE6_WHEELS.items():
            names = [url.rsplit("/", 1)[-1] for url, _sha in wheels]
            self.assertEqual(len(names), 2)
            for name in names:
                self.assertIn(f"-{line}-", name)
                self.assertIn(f"manylinux_{glibc[0]}_{glibc[1]}_x86_64", name)
            for _url, sha256 in wheels:
                self.assertRegex(sha256, r"^[0-9a-f]{64}$")

    def test_the_legacy_line_is_the_same_bytes_the_deb_and_rpm_bundle(self):
        pinned = dict(re.findall(
            r"^([a-z0-9-]+)==\S+ \\\n\s+--hash=sha256:([0-9a-f]{64})",
            (ROOT / "third_party/requirements-deb.txt").read_text(),
            re.MULTILINE))
        wheels = dict((url.rsplit("/", 1)[-1].split("-")[0].replace("_", "-"),
                       sha) for url, sha in deps.PYSIDE6_WHEELS["6.9.3"][1])
        wheels.update((url.rsplit("/", 1)[-1].split("-")[0].replace("_", "-"),
                       sha) for url, sha in deps.PRIVATE_WHEELS["Xlib"][2])
        for name, sha256 in wheels.items():
            self.assertEqual(pinned.get(name), sha256, name)


class UnpackTests(unittest.TestCase):
    def test_importable_files_only(self):
        with tempfile.TemporaryDirectory() as td:
            wheel = Path(td) / "w.whl"
            wheel.write_bytes(_wheel({
                "toy/__init__.py": "X = 1\n",
                "toy-1.0.dist-info/METADATA": "Name: toy\n",
                "toy-1.0.data/scripts/toy": "#!/bin/sh\n"}))
            out = Path(td) / "out"
            out.mkdir()
            deps._unpack_wheel(wheel, out)
            self.assertTrue((out / "toy/__init__.py").is_file())
            self.assertFalse((out / "toy-1.0.data").exists())

    def test_refuses_paths_outside_the_target(self):
        for evil in ("../escape.py", "/abs.py", "toy/../../escape.py"):
            with tempfile.TemporaryDirectory() as td, self.subTest(evil=evil):
                wheel = Path(td) / "w.whl"
                wheel.write_bytes(_wheel({evil: "boom"}))
                out = Path(td) / "out"
                out.mkdir()
                with self.assertRaises(ValueError):
                    deps._unpack_wheel(wheel, out)
                self.assertFalse((Path(td) / "escape.py").exists())


class FetchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name) / "python-gui"
        self.cache = Path(tmp.name) / "cache"
        self.cache.mkdir()
        module = "bol_toy_toolkit_%d" % os.getpid()
        self.module = module
        self.payload = _wheel({f"{module}/__init__.py": "READY = True\n"})
        self.sets = {module: ("toy-1.0", None, (
            ("https://example.invalid/toy-1.0-py3-none-any.whl",
             hashlib.sha256(self.payload).hexdigest()),))}
        self.downloads = []

        def download(url, dest, label=None, **_kwargs):
            self.downloads.append(url)
            Path(dest).write_bytes(self.payload)
            return dest

        for patcher in (
                mock.patch.object(deps, "PRIVATE_WHEELS", self.sets),
                mock.patch.object(deps, "GUI_DEPS", {module: "toy==1.0"}),
                mock.patch.object(deps, "_PRIVATE_MODULES", (module,)),
                mock.patch.object(deps, "private_toolkit_dir",
                                  return_value=self.base),
                mock.patch("bol.config.CACHE", self.cache),
                mock.patch("bol.util.download", side_effect=download),
                mock.patch.object(deps, "info"),
                mock.patch.object(deps, "warn"),
                mock.patch.object(sys, "path", list(sys.path)),
                mock.patch.dict(os.environ, {}, clear=False)):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("BOL_NO_PIP", None)
        self.addCleanup(sys.modules.pop, module, None)

    def test_fetched_verified_unpacked_and_importable(self):
        self.assertEqual(deps.ensure_gui_deps(), [])
        self.assertEqual(len(self.downloads), 1)
        self.assertTrue((self.base / "toy-1.0" / deps._COMPLETE).is_file())
        # Behind whatever the system already provides, never in front of it.
        self.assertEqual(sys.path[-1], str(self.base / "toy-1.0"))
        self.assertTrue(__import__(self.module).READY)
        self.assertEqual(list(self.cache.iterdir()), [],
                         "the wheel is not kept once unpacked")

    def test_a_later_start_uses_it_without_the_network(self):
        deps.ensure_gui_deps()
        sys.path.remove(str(self.base / "toy-1.0"))
        self.assertEqual(deps.missing_gui_deps(), [])
        self.assertEqual(len(self.downloads), 1)

    def test_a_wheel_that_fails_its_pin_is_never_used(self):
        self.payload = _wheel({f"{self.module}/__init__.py": "READY = 0\n"})
        self.assertEqual(deps.ensure_gui_deps(), [self.module])
        self.assertFalse((self.base / "toy-1.0").exists())
        message = deps.warn.call_args[0][0]
        self.assertIn("SHA-256", message)
        self.assertIn("AppImage", message)

    def test_a_glibc_below_the_wheels_floor_downloads_nothing(self):
        self.sets[self.module] = ("toy-1.0", (2, 99),
                                  self.sets[self.module][2])
        self.assertEqual(deps.ensure_gui_deps(), [self.module])
        self.assertEqual(self.downloads, [])
        self.assertIn("glibc 2.99", deps.warn.call_args[0][0])

    def test_switched_off_means_nothing_is_fetched(self):
        with mock.patch.dict(os.environ, {"BOL_NO_PIP": "1"}):
            self.assertEqual(deps.ensure_gui_deps(), [self.module])
        self.assertEqual(self.downloads, [])

    def test_a_set_another_python_left_behind_is_removed(self):
        stale = self.base / "pyside6-6.9.3"
        stale.mkdir(parents=True)
        (stale / deps._COMPLETE).write_text("old\n")
        deps.ensure_gui_deps()
        self.assertFalse(stale.exists())



def _deb(files):
    """A Debian package -- an ar archive with a data.tar.xz -- of ``files``."""
    import lzma
    import tarfile

    tar = io.BytesIO()
    with tarfile.open(fileobj=tar, mode="w") as archive:
        for name, data in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
    out = io.BytesIO()
    out.write(b"!<arch>\n")
    for name, body in (("debian-binary", b"2.0\n"),
                       ("control.tar.xz", lzma.compress(b"")),
                       ("data.tar.xz", lzma.compress(tar.getvalue()))):
        out.write(f"{name:<16}{0:<12}{0:<6}{0:<6}{100644:<8}{len(body):<10}`\n"
                  .encode("ascii"))
        out.write(body + (b"\n" if len(body) % 2 else b""))
    return out.getvalue()


class GapLibraryPinTests(unittest.TestCase):
    def test_the_pyz_fills_the_same_gaps_the_appimage_carries(self):
        script = (ROOT / "scripts/build-appimage.sh").read_text(
            encoding="utf-8")
        bundled = {
            soname: (pool, member, sha256)
            for pool, sha256, member, soname in re.findall(
                r'bundle_deb_library \\\n\s+"([^"]+)" \\\n'
                r'\s+"([0-9a-f]{64})" \\\n\s+"([^"]+)" "([^"]+)"', script)}
        self.assertIn("libgthread-2.0.so.0", bundled)
        self.assertIn(deps._DEBIAN_SNAPSHOT.rstrip("/"), script)
        self.assertEqual(deps.QT_GAP_LIBRARIES, bundled)


class GapLibraryTests(unittest.TestCase):
    """A stock openSUSE has no libgthread-2_0-0, which libQt6Core links, and
    stock Ubuntu and Mint have no libxcb-cursor0, which the xcb plugin links:
    the .pyz could not import Qt on the first and aborted on every X11
    session of the second. The toolkit it fetches gets those from the same
    pinned Debian packages the AppImage carries, when the host lacks them."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name) / "python-gui"
        self.cache = Path(tmp.name) / "cache"
        self.cache.mkdir()
        tag = deps.PRIVATE_WHEELS["PySide6"][0]
        self.package = self.base / tag / "PySide6"
        self.qt_lib = self.package / "Qt" / "lib"
        self.qt_lib.mkdir(parents=True)
        (self.base / tag / deps._COMPLETE).write_text("wheels\n")
        self.library = b"\x7fELF gthread"
        self.deb = _deb({"./usr/lib/x86_64-linux-gnu/libgap.so.1.2.3":
                         self.library,
                         "./usr/share/doc/libgap1/copyright": b"MIT\n"})
        self.gaps = {"libgap.so.1": (
            "pool/main/g/gap/libgap1_1.2.3-1+b1_amd64.deb", "libgap.so.1.2.3",
            hashlib.sha256(self.deb).hexdigest())}
        self.downloads = []

        def download(url, dest, label=None, **_kwargs):
            self.downloads.append(url)
            Path(dest).write_bytes(self.deb)
            return dest

        self.located = [str(self.package)]
        for patcher in (
                mock.patch.object(deps, "QT_GAP_LIBRARIES", self.gaps),
                mock.patch.object(deps, "private_toolkit_dir",
                                  return_value=self.base),
                mock.patch("importlib.util.find_spec",
                           side_effect=lambda name: mock.Mock(
                               submodule_search_locations=self.located)),
                mock.patch("platform.machine", return_value="x86_64"),
                mock.patch("bol.config.CACHE", self.cache),
                mock.patch("bol.util.download", side_effect=download),
                mock.patch.object(deps, "info"),
                mock.patch.object(deps, "warn")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_only_what_the_host_cannot_load_is_fetched(self):
        self.gaps["libthere.so.2"] = ("pool/main/t/there/libthere2_2_amd64.deb",
                                      "libthere.so.2.0", "0" * 64)
        missing = deps.fill_qt_library_gaps(
            loadable=lambda soname: soname == "libthere.so.2")
        self.assertEqual(missing, [])
        self.assertEqual(self.downloads, [
            "https://snapshot.debian.org/archive/debian/20260701T000000Z/"
            "pool/main/g/gap/libgap1_1.2.3-1%2Bb1_amd64.deb"])
        fetched = self.qt_lib / "libgap.so.1"
        self.assertEqual(fetched.read_bytes(), self.library)
        self.assertTrue(os.access(fetched, os.X_OK))
        self.assertFalse((self.qt_lib / "libthere.so.2").exists())
        self.assertEqual(list(self.cache.iterdir()), [],
                         "the package is not kept once unpacked")

    def test_a_later_start_downloads_nothing(self):
        deps.fill_qt_library_gaps(loadable=lambda _soname: False)
        self.assertEqual(deps.fill_qt_library_gaps(
            loadable=lambda _soname: False), [])
        self.assertEqual(len(self.downloads), 1)

    def test_a_package_that_fails_its_pin_is_never_used(self):
        self.deb = _deb({"./usr/lib/x86_64-linux-gnu/libgap.so.1.2.3":
                         b"something else"})
        self.assertEqual(deps.fill_qt_library_gaps(
            loadable=lambda _soname: False), ["libgap.so.1"])
        self.assertEqual(list(self.qt_lib.iterdir()), [])
        self.assertIn("SHA-256", deps.warn.call_args[0][0])

    def test_a_toolkit_the_system_or_pip_installed_is_left_alone(self):
        self.located[:] = ["/usr/lib/python3/dist-packages/PySide6"]
        self.assertEqual(deps.fill_qt_library_gaps(
            loadable=lambda _soname: False), [])
        self.assertEqual(self.downloads, [])

    def test_only_on_the_architecture_the_packages_are_built_for(self):
        with mock.patch("platform.machine", return_value="aarch64"):
            self.assertEqual(deps.fill_qt_library_gaps(
                loadable=lambda _soname: False), [])
        self.assertEqual(self.downloads, [])

    def test_a_file_that_is_no_debian_package_is_refused(self):
        bogus = self.cache / "bogus.deb"
        bogus.write_bytes(b"PK\x03\x04")
        with self.assertRaises(ValueError):
            deps._deb_member(bogus, "./usr/lib/x86_64-linux-gnu/libgap.so.1")


if __name__ == "__main__":
    unittest.main()
