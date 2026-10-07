"""Every way of starting the launcher goes through bol.__main__.main.

``optiscaler`` is not a subcommand of bol.cli, and the OptiScaler bootstrap
has to run before the CLI does. The .pyz and the console script a wheel
installs called bol.cli.main themselves, so both answered
``bedrock-on-linux optiscaler install`` with "invalid choice" (#306).
"""
# SPDX-License-Identifier: MIT

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import __main__ as entry
from bol import cli, optiscaler

ROOT = Path(__file__).resolve().parents[1]


class DispatchTests(unittest.TestCase):
    def test_optiscaler_goes_to_its_own_cli_without_the_bootstrap(self):
        with mock.patch.object(sys, "argv",
                               ["bedrock-on-linux", "optiscaler", "status"]), \
                mock.patch.object(optiscaler, "cli_main") as cli_main, \
                mock.patch.object(optiscaler, "bootstrap") as bootstrap, \
                mock.patch.object(cli, "main") as main:
            entry.main()
        cli_main.assert_called_once_with(["status"])
        bootstrap.assert_not_called()
        main.assert_not_called()

    def test_every_other_command_runs_the_bootstrap_then_the_cli(self):
        calls = []
        with mock.patch.object(sys, "argv", ["bedrock-on-linux", "play"]), \
                mock.patch.object(optiscaler, "bootstrap",
                                  side_effect=lambda: calls.append("boot")), \
                mock.patch.object(cli, "main",
                                  side_effect=lambda: calls.append("cli")):
            entry.main()
        self.assertEqual(calls, ["boot", "cli"])

    def test_ctrl_c_exits_with_130(self):
        with mock.patch.object(sys, "argv", ["bedrock-on-linux", "play"]), \
                mock.patch.object(optiscaler, "bootstrap"), \
                mock.patch.object(cli, "main", side_effect=KeyboardInterrupt), \
                mock.patch("builtins.print"):
            with self.assertRaises(SystemExit) as stop:
                entry.main()
        self.assertEqual(stop.exception.code, 130)


class EntryPointTests(unittest.TestCase):
    def test_the_zipapp_runs_the_shared_entry_point(self):
        build = (ROOT / "scripts/build-release.sh").read_text(encoding="utf-8")
        stub = build.split("<<'PYEOF'\n", 1)[1].split("PYEOF\n", 1)[0]
        self.assertIn("from bol.__main__ import main", stub)
        self.assertNotIn("bol.cli", stub)

    def test_the_script_answers_the_optiscaler_subcommand(self):
        # The script every package but the .pyz and the wheel starts.
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, PYTHONPATH="", HOME=home,
                       XDG_DATA_HOME=os.path.join(home, "data"),
                       XDG_CONFIG_HOME=os.path.join(home, "config"),
                       XDG_CACHE_HOME=os.path.join(home, "cache"))
            env.pop("BOL_HOME", None)
            result = subprocess.run(
                [sys.executable, str(ROOT / "bedrock-on-linux"),
                 "optiscaler", "--help"],
                env=env, cwd=ROOT, text=True, capture_output=True,
                timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("bedrock-on-linux optiscaler", result.stdout)


if __name__ == "__main__":
    unittest.main()
