"""AppArmor's user-namespace restriction, which stops the runtime (#384)."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bol import prefix, selinux, userns

# What umu-run printed on TUXEDO OS (Ubuntu 24.04) in #384.
REFUSED = (
    "INFO: Running 'GE-Proton10-32' using runtime 'sniper'\n"
    "pressure-vessel-wrap[19539]: E: Child process exited with code 1: "
    "bwrap: setting up uid map: Permission denied\n")


def _sysctl(root, value):
    path = Path(root) / "apparmor_restrict_unprivileged_userns"
    path.write_text(f"{value}\n")
    return path


class ProblemTests(unittest.TestCase):
    def test_the_restriction_is_named_with_its_fix(self):
        with tempfile.TemporaryDirectory() as td:
            message = userns.problem(REFUSED, _sysctl(td, 1))
        self.assertIn(userns.SYSCTL_NAME, message)
        self.assertIn("sudo sysctl --system", message)

    def test_a_refusal_without_the_restriction_is_still_explained(self):
        with tempfile.TemporaryDirectory() as td:
            message = userns.problem(REFUSED, _sysctl(td, 0))
        self.assertIn("user namespace", message)
        self.assertNotIn("sudo tee", message)

    def test_nothing_is_said_without_the_refusal_in_the_log(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(userns.problem("wine: exited", _sysctl(td, 1)),
                             "")
        self.assertFalse(userns.restricted("/nonexistent/sysctl"))


class PrefixTests(unittest.TestCase):
    def test_a_refused_container_names_apparmor_not_selinux(self):
        with tempfile.TemporaryDirectory() as td:
            logs = Path(td) / "logs"
            sysctl = _sysctl(td, 1)

            def wineboot(*args, **kwargs):
                kwargs["stdout"].write(REFUSED)
                kwargs["stdout"].flush()
                return SimpleNamespace(returncode=1)

            with mock.patch.object(prefix, "LOGS", logs), \
                    mock.patch.object(userns, "SYSCTL", sysctl), \
                    mock.patch.object(selinux, "problem",
                                      return_value="SELINUX"), \
                    mock.patch(
                        "bol.gpu_safety.require_safe_graphics_session"), \
                    mock.patch.object(
                        prefix, "proton_umu_cmd",
                        return_value=(["umu", "wineboot"], {})), \
                    mock.patch.object(
                        prefix, "seed_managed_bootstrap_cryptbase",
                        return_value=True), \
                    mock.patch.object(prefix.subprocess, "run",
                                      side_effect=wineboot), \
                    mock.patch.object(prefix, "stop_prefix_procs"), \
                    mock.patch.object(prefix, "warn"):
                self.assertFalse(prefix.boot_prefix(Path(td) / "pfx"))
                problem = prefix.LAST_BOOT_PROBLEM
                fixable = prefix.LAST_BOOT_FIXABLE
                said = prefix.boot_failure_message("Minecraft was not started")
        self.assertIn(userns.SYSCTL_NAME, problem)
        self.assertNotIn("SELINUX", problem)
        # A sysctl to change is the player's to do: no report asked for.
        self.assertTrue(fixable)
        self.assertNotIn("Report a problem", said)


if __name__ == "__main__":
    unittest.main()
