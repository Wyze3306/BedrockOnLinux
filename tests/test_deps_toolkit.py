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


if __name__ == "__main__":
    unittest.main()
