"""SELinux's execmod rule, which stops Wine on a stock openSUSE (#380)."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bol import gamesetup, prefix, selinux

SELINUX_LABEL = "unconfined_u:unconfined_r:unconfined_t:s0-s0:c0.c1023\0"
APPARMOR_LABEL = "bwrap//&unpriv_bwrap (enforce)\n"
TUMBLEWEED = 'NAME="openSUSE Tumbleweed"\nID="opensuse-tumbleweed"\n' \
    'ID_LIKE="opensuse suse"\n'
FEDORA = 'NAME="Fedora Linux"\nID=fedora\n'


class Host:
    """A fake /sys/fs/selinux, /proc/self/attr/current and os-release."""

    def __init__(self, root, enforce=None, execmod=None, label="",
                 os_release=TUMBLEWEED):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self.selinuxfs = root / "selinux"
        if enforce is not None:
            (self.selinuxfs / "booleans").mkdir(parents=True)
            (self.selinuxfs / "enforce").write_text(enforce)
            if execmod is not None:
                (self.selinuxfs / "booleans" / selinux.BOOLEAN).write_text(
                    execmod)
        self.label = root / "attr-current"
        self.label.write_text(label)
        self.os_release = (root / "os-release",)
        self.os_release[0].write_text(os_release)

    def state(self):
        return selinux.execmod_state(self.selinuxfs, self.label)

    def problem(self, failed=False):
        return selinux.problem(failed, self.selinuxfs, self.label,
                               self.os_release)


class ExecmodStateTests(unittest.TestCase):
    def test_enforcing_with_the_boolean_off_is_denied(self):
        with tempfile.TemporaryDirectory() as td:
            host = Host(td, "1", "0 0", SELINUX_LABEL)
            self.assertEqual(host.state(), selinux.DENIED)
            said = host.problem()
        self.assertIn("selinuxuser_execmod off", said)
        self.assertIn("zypper install selinux-policy-targeted-gaming", said)

    def test_boolean_on_or_permissive_is_allowed(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(Host(Path(td) / "a", "1", "1 1",
                                  SELINUX_LABEL).state(), selinux.ALLOWED)
            self.assertEqual(Host(Path(td) / "b", "0", "0 0",
                                  SELINUX_LABEL).state(), selinux.ALLOWED)

    def test_no_selinux_is_allowed_and_says_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            host = Host(td, label=APPARMOR_LABEL)
            self.assertEqual(host.state(), selinux.ALLOWED)
            self.assertEqual(host.problem(failed=True), "")

    def test_flatpak_on_tumbleweed_cannot_read_it_and_warns(self):
        """The Flatpak has no /sys/fs: SELinux shows only in the label."""
        with tempfile.TemporaryDirectory() as td:
            host = Host(td, label=SELINUX_LABEL)
            self.assertEqual(host.state(), selinux.UNKNOWN)
            said = host.problem()
        self.assertIn("openSUSE", said)
        self.assertIn("cannot read", said)
        self.assertIn("selinux-policy-targeted-gaming", said)

    def test_flatpak_on_fedora_warns_only_after_a_failure(self):
        """Fedora turns the boolean on by default."""
        with tempfile.TemporaryDirectory() as td:
            host = Host(td, label=SELINUX_LABEL, os_release=FEDORA)
            self.assertEqual(host.problem(), "")
            said = host.problem(failed=True)
        self.assertIn("setsebool -P selinuxuser_execmod 1", said)
        self.assertNotIn("zypper", said)


class PrefixTests(unittest.TestCase):
    def test_silent_wineboot_failure_names_selinux(self):
        """#380: status 1 and nothing in the log on a stock Tumbleweed."""
        with tempfile.TemporaryDirectory() as td:
            host = Host(Path(td) / "host", "1", "0 0", SELINUX_LABEL)
            pfx = Path(td) / "pfx"
            with mock.patch.object(prefix, "LOGS", Path(td) / "logs"), \
                    mock.patch.object(selinux, "SELINUXFS", host.selinuxfs), \
                    mock.patch.object(selinux, "PROC_LABEL", host.label), \
                    mock.patch.object(selinux, "OS_RELEASE",
                                      host.os_release), \
                    mock.patch(
                        "bol.gpu_safety.require_safe_graphics_session"), \
                    mock.patch.object(
                        prefix, "proton_umu_cmd",
                        return_value=(["umu", "wineboot"], {})), \
                    mock.patch.object(
                        prefix, "seed_managed_bootstrap_cryptbase",
                        return_value=True), \
                    mock.patch.object(
                        prefix.subprocess, "run",
                        return_value=SimpleNamespace(returncode=1)) as run, \
                    mock.patch.object(prefix, "stop_prefix_procs"), \
                    mock.patch.object(prefix, "warn") as warn:
                self.assertFalse(prefix.boot_prefix(pfx))
                problem = prefix.LAST_BOOT_PROBLEM

        self.assertIn("exited with status 1", problem)
        self.assertIn("selinuxuser_execmod", problem)
        # Warned before wineboot ran, and named again in the failure.
        self.assertIn("selinuxuser_execmod", warn.call_args_list[0].args[0])
        # Wine's own errors reach the log now.
        self.assertEqual(run.call_args.kwargs["env"]["WINEDEBUG"],
                         "fixme-all")

    def test_ready_prefix_warns_only_when_known(self):
        with tempfile.TemporaryDirectory() as td:
            flatpak = Host(Path(td) / "flatpak", label=SELINUX_LABEL)
            with mock.patch.object(selinux, "SELINUXFS", flatpak.selinuxfs), \
                    mock.patch.object(selinux, "PROC_LABEL", flatpak.label), \
                    mock.patch.object(selinux, "OS_RELEASE",
                                      flatpak.os_release), \
                    mock.patch.object(
                        prefix, "_prepare_managed_prefix_layout"), \
                    mock.patch.object(prefix, "prefix_ready",
                                      return_value=True), \
                    mock.patch.object(prefix, "managed_prefix_engine_is_stale",
                                      return_value=False), \
                    mock.patch.object(prefix, "prune_engine_dll_backups"), \
                    mock.patch.object(
                        prefix, "repair_managed_prefix_user32"), \
                    mock.patch.object(prefix, "warn") as warn:
                self.assertTrue(prefix.boot_prefix(Path(td) / "pfx"))
        warn.assert_not_called()


class DiagnoseTests(unittest.TestCase):
    def test_game_that_left_no_trace_is_put_down_to_selinux(self):
        with tempfile.TemporaryDirectory() as td:
            host = Host(Path(td) / "host", label=SELINUX_LABEL)
            with mock.patch.object(gamesetup, "LOGS", Path(td)), \
                    mock.patch.object(selinux, "SELINUXFS", host.selinuxfs), \
                    mock.patch.object(selinux, "PROC_LABEL", host.label), \
                    mock.patch.object(selinux, "OS_RELEASE",
                                      host.os_release), \
                    mock.patch.object(gamesetup, "load_settings",
                                      return_value={}), \
                    mock.patch.object(gamesetup, "msa_signed_in",
                                      return_value=True):
                hits = gamesetup.diagnose(quiet=True)
        self.assertTrue(hits)
        self.assertIn("selinuxuser_execmod", hits[0])


if __name__ == "__main__":
    unittest.main()
