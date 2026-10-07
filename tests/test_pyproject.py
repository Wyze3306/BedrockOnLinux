"""pyproject.toml has to install what the launcher needs at run time.

Nothing builds the wheel in CI, and distributions build it from this file
(#306). It shipped without bol/injector.exe, so DLL injection could not find
its injector, and without two of the modules bol.deps checks for on start,
so the launcher pip-installed its own pinned GUI stack over the one the
package manager had installed.
"""
# SPDX-License-Identifier: MIT

import configparser
import fnmatch
import unittest
from pathlib import Path

from bol import deps

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

ROOT = Path(__file__).resolve().parents[1]

# The distributions that provide each module bol.deps imports.
PROVIDED_BY = {
    "PySide6": {"pyside6", "pyside6-essentials"},
    "cryptography": {"cryptography"},
    "packaging": {"packaging"},
    "Xlib": {"python-xlib"},
}


def _name(requirement):
    for stop in "<>=!~;[ ":
        requirement = requirement.split(stop, 1)[0]
    return requirement.strip().lower().replace("_", "-")


@unittest.skipIf(tomllib is None, "tomllib needs Python 3.11")
class PyprojectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(ROOT / "pyproject.toml", "rb") as handle:
            cls.pyproject = tomllib.load(handle)
        cls.setuptools = cls.pyproject["tool"]["setuptools"]

    def test_every_package_under_bol_is_listed(self):
        packages = {
            ".".join(init.parent.relative_to(ROOT).parts)
            for init in (ROOT / "bol").rglob("__init__.py")
        }
        self.assertEqual(packages, set(self.setuptools["packages"]))

    def test_every_file_the_package_ships_besides_python_is_package_data(self):
        patterns = self.setuptools.get("package-data", {}).get("bol", [])
        shipped = [
            path.relative_to(ROOT / "bol").as_posix()
            for path in (ROOT / "bol").rglob("*")
            if path.is_file() and path.suffix not in (".py", ".pyc")
            and "__pycache__" not in path.parts
        ]
        self.assertIn("injector.exe", shipped)
        for name in shipped:
            with self.subTest(file=name):
                self.assertTrue(
                    any(fnmatch.fnmatch(name, p) for p in patterns),
                    f"bol/{name} would be left out of the wheel")

    def test_what_bol_deps_installs_on_start_is_a_dependency(self):
        declared = {_name(r) for r in self.pyproject["project"]["dependencies"]}
        for module in {**deps.GUI_DEPS, **deps.LOGIN_DEPS}:
            with self.subTest(module=module):
                self.assertIn(module, PROVIDED_BY,
                              "say which distribution provides it")
                self.assertTrue(PROVIDED_BY[module] & declared,
                                f"nothing in dependencies provides {module}")

    def test_the_console_script_is_the_shared_entry_point(self):
        self.assertEqual(self.pyproject["project"]["scripts"],
                         {"bedrock-on-linux": "bol.__main__:main"})

    def test_the_desktop_entry_finds_the_command_and_icon_it_installs(self):
        data_files = self.setuptools["data-files"]
        for sources in data_files.values():
            for source in sources:
                with self.subTest(source=source):
                    self.assertTrue((ROOT / source).is_file())
        desktop = configparser.ConfigParser(interpolation=None)
        desktop.read(ROOT / "data/bedrock-on-linux.desktop")
        entry = desktop["Desktop Entry"]
        self.assertEqual(entry["Exec"].split()[0], "bedrock-on-linux")
        self.assertIn(f"data/{entry['Icon']}.png",
                      data_files["share/icons/hicolor/256x256/apps"])


if __name__ == "__main__":
    unittest.main()
