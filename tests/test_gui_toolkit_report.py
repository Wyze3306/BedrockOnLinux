"""Reporting a Qt toolkit the host cannot load (issue #205)."""
# SPDX-License-Identifier: MIT

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import deps, doctor

LOADER_ERROR = ("libzstd.so.1: cannot open shared object file: "
                "No such file or directory")


class MissingSharedLibraryTests(unittest.TestCase):
    def test_the_library_the_loader_names_is_read_back_out(self):
        self.assertEqual(
            deps.missing_shared_library(ImportError(LOADER_ERROR)),
            "libzstd.so.1")
        self.assertEqual(
            deps.missing_shared_library(ImportError(
                "libQt6Core.so.6: cannot open shared object file: No such "
                "file or directory")),
            "libQt6Core.so.6")

    def test_an_ordinary_import_failure_names_no_library(self):
        for message in ("No module named 'PySide6'",
                        "cannot import name 'QToolButton' from 'PySide6'",
                        "/usr/lib/libzstd.so.1: file too short"):
            with self.subTest(message=message):
                self.assertIsNone(
                    deps.missing_shared_library(ImportError(message)))


class GuiToolkitSummaryTests(unittest.TestCase):
    def test_a_toolkit_that_imports_is_reported_ready(self):
        with mock.patch.object(deps, "have", return_value=True), \
                mock.patch.object(deps, "gui_import_error", return_value=None), \
                mock.patch.object(deps, "xcb_plugin_missing_library",
                                  return_value=None):
            self.assertEqual(doctor.gui_toolkit_summary(), "OK (GUI)")

    def test_an_x11_plugin_that_cannot_load_is_not_ready(self):
        # Importing QtCore succeeds on stock Ubuntu; opening the window then
        # aborts in Qt's xcb plugin for want of libxcb-cursor0.
        with mock.patch.object(deps, "have", return_value=True), \
                mock.patch.object(deps, "gui_import_error", return_value=None), \
                mock.patch.object(deps, "xcb_plugin_missing_library",
                                  return_value="libxcb-cursor.so.0"):
            summary = doctor.gui_toolkit_summary()
        self.assertIn("X11 plugin cannot load", summary)
        self.assertIn("libxcb-cursor.so.0", summary)

    def test_a_toolkit_that_is_not_installed_yet_is_not_a_failure(self):
        # The portable .pyz and a bare checkout install it on first launch.
        with mock.patch.object(deps, "have", return_value=False):
            self.assertEqual(doctor.gui_toolkit_summary(),
                             "auto-installed on launch")

    def test_an_installed_toolkit_that_cannot_load_names_the_library(self):
        # What `have()` alone reported as "OK (GUI)" on the machine in #205,
        # where the launcher could not open at all.
        with mock.patch.object(deps, "have", return_value=True), \
                mock.patch.object(deps, "gui_import_error",
                                  return_value=LOADER_ERROR):
            summary = doctor.gui_toolkit_summary()
        self.assertIn("Qt cannot load", summary)
        self.assertIn("libzstd.so.1", summary)

    def test_an_installed_toolkit_broken_otherwise_keeps_the_reason(self):
        with mock.patch.object(deps, "have", return_value=True), \
                mock.patch.object(deps, "gui_import_error",
                                  return_value="undefined symbol: PySide6"):
            self.assertIn("undefined symbol: PySide6",
                          doctor.gui_toolkit_summary())


class GuiImportErrorTests(unittest.TestCase):
    def test_the_import_is_attempted_not_just_the_module_path(self):
        with mock.patch("importlib.import_module",
                        side_effect=ImportError(LOADER_ERROR)) as imported:
            self.assertEqual(deps.gui_import_error(), LOADER_ERROR)
        imported.assert_called_once_with("PySide6.QtCore")

    def test_an_importable_toolkit_reports_no_error(self):
        with mock.patch("importlib.import_module") as imported:
            self.assertIsNone(deps.gui_import_error())
        imported.assert_called_once_with("PySide6.QtCore")


class XcbPluginTests(unittest.TestCase):
    """Qt aborts when its xcb plugin cannot load; the launcher asks first."""

    def test_which_sessions_open_through_xcb(self):
        self.assertTrue(deps.qt_will_use_xcb({"XDG_SESSION_TYPE": "x11"}))
        self.assertTrue(deps.qt_will_use_xcb({}))
        # Qt tries "wayland;xcb" there.
        self.assertFalse(deps.qt_will_use_xcb({"XDG_SESSION_TYPE": "wayland"}))
        self.assertTrue(deps.qt_will_use_xcb(
            {"XDG_SESSION_TYPE": "wayland", "QT_QPA_PLATFORM": "xcb"}))
        self.assertFalse(deps.qt_will_use_xcb({"QT_QPA_PLATFORM": "offscreen"}))
        self.assertFalse(deps.qt_will_use_xcb(
            {"QT_QPA_PLATFORM": "wayland;xcb"}))

    def _plugin_tree(self, td):
        plugin = Path(td) / "Qt" / "plugins" / "platforms" / "libqxcb.so"
        plugin.parent.mkdir(parents=True)
        plugin.write_bytes(b"\x7fELF")
        spec = mock.Mock(submodule_search_locations=[td])
        return plugin, spec

    def test_the_library_the_loader_refuses_is_named(self):
        with tempfile.TemporaryDirectory() as td:
            plugin, spec = self._plugin_tree(td)
            with mock.patch("importlib.util.find_spec", return_value=spec), \
                    mock.patch("ctypes.CDLL", side_effect=OSError(
                        "libxcb-cursor.so.0: cannot open shared object file: "
                        "No such file or directory")) as dlopen:
                self.assertEqual(deps.xcb_plugin_missing_library(),
                                 "libxcb-cursor.so.0")
            dlopen.assert_called_once_with(str(plugin))

    def test_a_plugin_that_loads_or_is_not_a_wheels_is_fine(self):
        with tempfile.TemporaryDirectory() as td:
            _plugin, spec = self._plugin_tree(td)
            with mock.patch("importlib.util.find_spec", return_value=spec), \
                    mock.patch("ctypes.CDLL"):
                self.assertIsNone(deps.xcb_plugin_missing_library())
        with tempfile.TemporaryDirectory() as td, \
                mock.patch("importlib.util.find_spec",
                           return_value=mock.Mock(
                               submodule_search_locations=[td])), \
                mock.patch("ctypes.CDLL") as dlopen:
            # A distribution's PySide6 keeps its plugins elsewhere and
            # declares their libraries itself.
            self.assertIsNone(deps.xcb_plugin_missing_library())
            dlopen.assert_not_called()

    def test_the_package_to_install_follows_the_package_manager(self):
        for manager, expected in (
                ("apt-get", "sudo apt install libxcb-cursor0"),
                ("dnf", "sudo dnf install xcb-util-cursor"),
                ("pacman", "sudo pacman -S xcb-util-cursor"),
                ("zypper", "sudo zypper install libxcb-cursor0")):
            with self.subTest(manager=manager), \
                    mock.patch("shutil.which",
                               side_effect=lambda name, m=manager:
                               "/usr/bin/" + name if name == m else None):
                self.assertEqual(deps.install_hint("libxcb-cursor.so.0"),
                                 expected)
        self.assertIsNone(deps.install_hint("libfoo.so.1"))


if __name__ == "__main__":
    unittest.main()
