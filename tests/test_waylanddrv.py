"""Keep the engine's Wayland driver part of the engine's own Wine build.

The engine is a GE-Proton base with our WineGDK build overlaid, and the
overlay only replaces what that build produced. Debian 11 splits xkbregistry
out of libxkbcommon-dev, the build container installed only the latter, and
Wine's configure dropped winewayland.drv without failing. Nothing was missing
from the candidate afterwards -- the base's own Wine 10 driver had simply
survived beside our Wine 11 win32u.so, importing the
win32u_(get|set)_window_pixel_format exports Wine removed in 2025. It failed
its PROCESS_ATTACH on every host, so BOL_INPUT=wayland could never start the
game (issue #180).
"""
# SPDX-License-Identifier: MIT

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import launch, waylanddrv


ROOT = Path(__file__).resolve().parents[1]
BULLSEYE_SCRIPT = ROOT / "scripts/build-winegdk-bullseye.sh"
CONTAINER_SCRIPT = ROOT / "scripts/build-winegdk-container.sh"
PACKAGE_SCRIPT = ROOT / "scripts/package-engine.sh"

UNIX_DIR = "files/lib/wine/x86_64-unix"
WIN32U = UNIX_DIR + "/win32u.so"
DRIVER = UNIX_DIR + "/winewayland.so"


def _module(path, root, module_dir, sources=("display.c", "window.c")):
    """A stand-in for a Wine module compiled from a given source tree.

    Wine's TRACE and ERR macros leave each source path in .rodata as its own
    NUL-terminated string, next to the format strings of the same file; that
    neighbourhood is what the probe has to read through.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = bytearray(b"\x7fELF" + b"\x00" * 12)
    for source in sources:
        body += b"%p flags %04x\n\x00"
        body += root + b"/dlls/" + module_dir + b"/" + source.encode() + b"\x00"
    path.write_bytes(bytes(body))
    return path


def _engine(base, *, driver_root=b"/winegdk/source",
            engine_root=b"/winegdk/source", with_driver=True,
            unix_dir=UNIX_DIR):
    """A fake engine whose two modules come from the given source trees."""
    base = Path(base)
    _module(base / unix_dir / "win32u.so", engine_root, b"win32u",
            sources=("dc.c", "bitblt.c"))
    if with_driver:
        _module(base / unix_dir / "winewayland.so", driver_root,
                b"winewayland.drv")
    return base


class BuildScriptTests(unittest.TestCase):
    def test_both_build_scripts_install_the_xkbregistry_headers(self):
        # Wine's configure disables winewayland.drv when XKBREGISTRY_LIBS is
        # empty, and bullseye ships those development files in their own
        # package rather than in libxkbcommon-dev.
        for script in (BULLSEYE_SCRIPT, CONTAINER_SCRIPT):
            text = script.read_text(encoding="utf-8")
            with self.subTest(script=script.name):
                self.assertIn("libxkbregistry-dev", text)

    def test_both_build_scripts_fail_closed_without_a_wayland_driver(self):
        # The regression was silent: configure dropped the driver, make
        # succeeded, and the packager filled the gap from the Proton base.
        text = BULLSEYE_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("lib/wine/x86_64-unix/winewayland.so", text)
        self.assertIn("lib/wine/x86_64-windows/winewayland.drv", text)
        self.assertIn("Wayland driver configured out", text)
        # The container script checks the driver of the architecture it
        # builds: x86_64, or aarch64 for the ARM64EC engine (#250).
        text = CONTAINER_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('"lib/wine/$UNIX_ARCH-unix/winewayland.so"', text)
        self.assertIn('"lib/wine/$UNIX_ARCH-windows/winewayland.drv"', text)
        self.assertIn('UNIX_ARCH="x86_64"', text)
        self.assertIn('UNIX_ARCH="aarch64"', text)
        self.assertIn("Wayland driver configured out", text)

    def test_the_arm64_build_links_against_xkbregistry(self):
        # The steamrt4 SDK has no libxkbregistry-dev; without the pinned
        # build configure drops winewayland on aarch64.
        text = CONTAINER_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("pkg-config --exists xkbregistry", text)
        self.assertIn("-Denable-xkbregistry=true", text)
        self.assertRegex(text, r'XKBCOMMON_SHA256="[0-9a-f]{64}"')

    def test_the_packager_refuses_a_driver_from_another_build(self):
        text = PACKAGE_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("engine_wayland_driver_problem", text)


class BuildRootTests(unittest.TestCase):
    def test_the_source_tree_of_a_module_is_read(self):
        with tempfile.TemporaryDirectory() as td:
            path = _module(Path(td) / "winewayland.so", b"/winegdk/source",
                           b"winewayland.drv")
            self.assertEqual(
                waylanddrv._build_root(path, b"/dlls/winewayland.drv/"),
                b"/winegdk/source")

    def test_a_relative_source_tree_is_read(self):
        # Proton builds Wine from ../src-wine; ours from an absolute path.
        with tempfile.TemporaryDirectory() as td:
            path = _module(Path(td) / "winewayland.so", b"../src-wine",
                           b"winewayland.drv")
            self.assertEqual(
                waylanddrv._build_root(path, b"/dlls/winewayland.drv/"),
                b"../src-wine")

    def test_printable_bytes_that_are_not_a_path_are_not_a_verdict(self):
        # Some matches sit in a blob with no NUL in front of them, so the
        # bytes walked back over are not the build path. One such match must
        # not outvote the real one, and must never be reported on its own.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "win32u.so"
            path.write_bytes(b"\x00" + b"0C" + b"/dlls/win32u/primitives.c\x00")
            self.assertIsNone(
                waylanddrv._build_root(path, b"/dlls/win32u/"))

    def test_a_module_with_no_source_path_is_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "win32u.so"
            path.write_bytes(b"\x7fELF" + b"stripped" * 32)
            self.assertIsNone(waylanddrv._build_root(path, b"/dlls/win32u/"))

    def test_an_absent_or_empty_module_is_unknown_not_broken(self):
        with tempfile.TemporaryDirectory() as td:
            empty = Path(td) / "win32u.so"
            empty.write_bytes(b"")
            self.assertIsNone(waylanddrv._build_root(empty, b"/dlls/win32u/"))
            self.assertIsNone(
                waylanddrv._build_root(Path(td) / "absent.so",
                                       b"/dlls/win32u/"))


class EngineDriverTests(unittest.TestCase):
    def test_one_coherent_build_reports_no_problem(self):
        with tempfile.TemporaryDirectory() as td:
            root = _engine(td)
            self.assertIsNone(waylanddrv.engine_wayland_driver_problem(root))

    def test_a_coherent_proton_build_reports_no_problem(self):
        # A user-supplied Proton is built somewhere else entirely; only
        # disagreement between its own modules is a defect.
        with tempfile.TemporaryDirectory() as td:
            root = _engine(td, driver_root=b"../src-wine",
                           engine_root=b"../src-wine")
            self.assertIsNone(waylanddrv.engine_wayland_driver_problem(root))

    def test_a_driver_from_another_build_is_reported(self):
        with tempfile.TemporaryDirectory() as td:
            root = _engine(td, driver_root=b"../src-wine")
            problem = waylanddrv.engine_wayland_driver_problem(root)
            self.assertIsNotNone(problem)
            # Name both trees: it is what identifies the leftover in a report.
            self.assertIn("../src-wine", problem)
            self.assertIn("/winegdk/source", problem)

    def test_an_engine_built_without_the_driver_is_reported(self):
        with tempfile.TemporaryDirectory() as td:
            root = _engine(td, with_driver=False)
            problem = waylanddrv.engine_wayland_driver_problem(root)
            self.assertIsNotNone(problem)
            self.assertIn("no Wayland driver", problem)

    def test_a_classic_proton_layout_is_read_too(self):
        # Our WoW64 engine installs the Unix modules under files/lib; a
        # user-supplied Proton on the classic layout uses files/lib64.
        with tempfile.TemporaryDirectory() as td:
            root = _engine(td, driver_root=b"../src-wine",
                           unix_dir="files/lib64/wine/x86_64-unix")
            self.assertIsNotNone(
                waylanddrv.engine_wayland_driver_problem(root))

    def test_no_engine_at_all_is_unknown_not_broken(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(
                waylanddrv.engine_wayland_driver_problem(Path(td)))
        self.assertIsNone(waylanddrv.engine_wayland_driver_problem(None))

    def test_an_unreadable_pair_is_unknown_not_broken(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for relative in (WIN32U, DRIVER):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"\x7fELF" + b"stripped" * 32)
            self.assertIsNone(waylanddrv.engine_wayland_driver_problem(root))

    def test_summary_words_cover_each_state(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIn("unknown",
                          waylanddrv.wayland_driver_summary(Path(td)))
        self.assertIn("unknown", waylanddrv.wayland_driver_summary(None))
        with tempfile.TemporaryDirectory() as td:
            self.assertIn("OK", waylanddrv.wayland_driver_summary(_engine(td)))
        with tempfile.TemporaryDirectory() as td:
            root = _engine(td, driver_root=b"../src-wine")
            self.assertIn("another Wine build",
                          waylanddrv.wayland_driver_summary(root))
        with tempfile.TemporaryDirectory() as td:
            root = _engine(td, with_driver=False)
            self.assertIn("without winewayland",
                          waylanddrv.wayland_driver_summary(root))


class LaunchBackendTests(unittest.TestCase):
    def test_a_usable_driver_keeps_the_requested_wayland_backend(self):
        with tempfile.TemporaryDirectory() as td:
            root = _engine(td)
            with mock.patch.object(launch, "warn") as warned:
                self.assertEqual(
                    launch._resolve_input_backend("wayland", True, root),
                    "wayland")
        self.assertFalse(warned.called)

    def test_an_unusable_driver_falls_back_to_x11_and_says_why(self):
        with tempfile.TemporaryDirectory() as td:
            root = _engine(td, driver_root=b"../src-wine")
            with mock.patch.object(launch, "warn") as warned:
                self.assertEqual(
                    launch._resolve_input_backend("wayland", True, root),
                    "x11")
        self.assertTrue(warned.called)
        self.assertIn("XWayland", warned.call_args[0][0])

    def test_x11_launches_do_not_pay_for_the_check(self):
        with mock.patch.object(launch, "engine_wayland_driver_problem") as probe:
            self.assertEqual(
                launch._resolve_input_backend("x11", True, "/engine"), "x11")
        self.assertFalse(probe.called)

    def test_a_session_without_wayland_is_left_to_the_caller(self):
        # The X11 branch below reports the missing WAYLAND_DISPLAY itself; the
        # engine is irrelevant when there is no compositor to connect to.
        with mock.patch.object(launch, "engine_wayland_driver_problem") as probe:
            self.assertEqual(
                launch._resolve_input_backend("wayland", False, "/engine"),
                "wayland")
        self.assertFalse(probe.called)


if __name__ == "__main__":
    unittest.main()
