"""Restarting the launcher after it updated itself.

A .pyz's __main__ has a module spec too, named "__main__". Taken for
`python3 -m bol`, it sent the restart to `python3 -m bol gui`, which cannot
find bol outside the archive: the window closed and nothing came back.
"""
# SPDX-License-Identifier: MIT

import importlib.machinery
import os
import sys
import types
import unittest
from unittest import mock

from bol.gui import MainWindow


def _first_exec(main_spec_name, argv0):
    main = types.ModuleType("__main__")
    main.__spec__ = (importlib.machinery.ModuleSpec(main_spec_name, None)
                     if main_spec_name else None)
    window = types.SimpleNamespace(na=mock.Mock())
    environ = {k: v for k, v in os.environ.items() if k != "APPIMAGE"}
    with mock.patch.dict(os.environ, environ, clear=True), \
            mock.patch.dict(sys.modules, {"__main__": main}), \
            mock.patch.object(sys, "argv", [argv0]), \
            mock.patch("os.execv") as execv:
        MainWindow.relaunch_app(window)
    return execv.call_args_list[0].args


class RelaunchTests(unittest.TestCase):
    def test_a_pyz_restarts_itself(self):
        self.assertEqual(
            _first_exec("__main__", "/opt/bol/bedrock-on-linux.pyz"),
            (sys.executable,
             [sys.executable, os.path.realpath("/opt/bol/bedrock-on-linux.pyz"),
              "gui"]))

    def test_python_m_bol_restarts_with_m(self):
        self.assertEqual(
            _first_exec("bol.__main__", "/src/bol/__main__.py"),
            (sys.executable, [sys.executable, "-m", "bol", "gui"]))

    def test_a_script_restarts_itself(self):
        self.assertEqual(
            _first_exec(None, "/usr/bin/bedrock-on-linux"),
            (sys.executable,
             [sys.executable, os.path.realpath("/usr/bin/bedrock-on-linux"),
              "gui"]))


if __name__ == "__main__":
    unittest.main()
