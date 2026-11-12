"""x86-64 programs on an ARM computer (#250).

The game, the engine and xodus-cli are x86-64. On ARM they run only through
an emulator the kernel hands them to; without one, setup has to say that
rather than fail on the first "Exec format error".
"""
# SPDX-License-Identifier: MIT

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import hostarch

# As /proc/sys/fs/binfmt_misc shows them.
_FEX = ("enabled\ninterpreter /usr/bin/FEXInterpreter\nflags: POCF\noffset 0\n"
        "magic 7f454c4602010100000000000000000002003e00\n"
        "mask fffffffffffefe00fffffffffffffffffeffffff\n")
_FEX_X86 = ("enabled\ninterpreter /usr/bin/FEXInterpreter\nflags: POCF\n"
            "offset 0\nmagic 7f454c4601010100000000000000000002000300\n")
_PYTHON = ("enabled\ninterpreter /usr/bin/python3.13\nflags: \noffset 0\n"
           "magic f30d0d0a\n")


def _binfmt(root, **entries):
    path = Path(root)
    (path / "status").write_text("enabled\n")
    (path / "register").write_text("")
    for name, text in entries.items():
        (path / name).write_text(text)
    return path


class EmulatorTests(unittest.TestCase):
    def test_fex_registered_for_x86_64_is_found(self):
        with tempfile.TemporaryDirectory() as td:
            binfmt = _binfmt(td, **{"FEX-x86": _FEX_X86, "FEX-x86_64": _FEX,
                                    "python3.13": _PYTHON})
            self.assertEqual(hostarch.x86_64_emulator(binfmt),
                             "/usr/bin/FEXInterpreter")

    def test_a_disabled_or_32_bit_only_entry_is_not_enough(self):
        with tempfile.TemporaryDirectory() as td:
            binfmt = _binfmt(td, **{"FEX-x86": _FEX_X86,
                                    "FEX-x86_64": _FEX.replace("enabled",
                                                               "disabled", 1)})
            self.assertIsNone(hostarch.x86_64_emulator(binfmt))

    def test_no_binfmt_support_is_no_emulator(self):
        self.assertIsNone(hostarch.x86_64_emulator(Path("/nonexistent")))


def _arm(pages=4096, muvm=None, environ=None):
    """Patches for an aarch64 kernel with these pages and this muvm."""
    return (mock.patch.object(hostarch.platform, "machine",
                              return_value="aarch64"),
            mock.patch.object(hostarch, "page_size", return_value=pages),
            mock.patch.object(hostarch, "muvm", return_value=muvm),
            mock.patch.dict(hostarch.os.environ, environ or {}, clear=True))


class ProblemTests(unittest.TestCase):
    def test_an_x86_64_computer_needs_nothing(self):
        with mock.patch.object(hostarch.platform, "machine",
                               return_value="x86_64"):
            self.assertIsNone(hostarch.problem(Path("/nonexistent")))
            self.assertEqual(hostarch.summary(Path("/nonexistent")),
                             "OK (x86_64)")

    def test_arm_without_an_emulator_is_told_what_to_install(self):
        with mock.patch.object(hostarch.platform, "machine",
                               return_value="aarch64"):
            problem = hostarch.problem(Path("/nonexistent"))
            summary = hostarch.summary(Path("/nonexistent"))
        self.assertIn("aarch64", problem)
        self.assertIn("FEX-Emu", problem)
        self.assertIn("MISSING", summary)

    def test_arm_with_fex_goes_ahead(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(hostarch.platform, "machine",
                                  return_value="aarch64"):
            binfmt = _binfmt(td, **{"FEX-x86_64": _FEX})
            self.assertIsNone(hostarch.problem(binfmt))
            self.assertIn("FEXInterpreter", hostarch.summary(binfmt))

    def test_asahi_with_muvm_goes_ahead_without_a_host_binfmt_entry(self):
        # muvm registers FEX inside the microVM only, so the host has none.
        a, b, c, d = _arm(16384, "/usr/bin/muvm")
        with a, b, c, d:
            self.assertIsNone(hostarch.problem(Path("/nonexistent")))
            self.assertIn("muvm", hostarch.summary(Path("/nonexistent")))

    def test_asahi_without_muvm_is_told_to_install_it(self):
        a, b, c, d = _arm(16384)
        with a, b, c, d:
            problem = hostarch.problem(Path("/nonexistent"))
            summary = hostarch.summary(Path("/nonexistent"))
        self.assertIn("16 KiB", problem)
        self.assertIn("muvm", problem)
        self.assertIn("MISSING muvm", summary)

    def test_a_host_fex_entry_does_not_help_on_16k_pages(self):
        # FEX refuses to run on a 16 KiB-page kernel.
        with tempfile.TemporaryDirectory() as td:
            binfmt = _binfmt(td, **{"FEX-x86_64": _FEX})
            a, b, c, d = _arm(16384)
            with a, b, c, d:
                self.assertIn("muvm", hostarch.problem(binfmt))

    def test_inside_muvm_the_microvm_binfmt_entry_is_what_counts(self):
        with tempfile.TemporaryDirectory() as td:
            binfmt = _binfmt(td, **{"FEX-x86_64": _FEX})
            a, b, c, d = _arm(4096, environ={hostarch.IN_MUVM: "1"})
            with a, b, c, d:
                self.assertIsNone(hostarch.problem(binfmt))
                self.assertIn("inside muvm", hostarch.summary(binfmt))

    def test_setup_stops_before_downloading_anything(self):
        from bol import gamesetup
        with mock.patch.object(gamesetup.hostarch, "problem",
                               return_value="no emulator"), \
                mock.patch.object(gamesetup, "mkdirs") as made:
            with self.assertRaises(gamesetup.BolError) as raised:
                gamesetup._do_setup()
        self.assertEqual(str(raised.exception), "no emulator")
        made.assert_not_called()


class MuvmRelaunchTests(unittest.TestCase):
    def test_16k_pages_with_muvm_runs_the_same_command_inside_it(self):
        a, b, c, d = _arm(16384, "/usr/bin/muvm")
        environ = {"BOL_DEBUG": "1", "XDG_DATA_HOME": "/data",
                   "HOME": "/home/p", "WAYLAND_DISPLAY": "wayland-0"}
        with a, b, c, d:
            cmd = hostarch.muvm_command(["-m", "bol", "gui"], environ,
                                        "/usr/bin/python3")
        self.assertEqual(cmd[0], "/usr/bin/muvm")
        self.assertEqual(cmd[cmd.index("--"):],
                         ["--", "/usr/bin/python3", "-m", "bol", "gui"])
        options = cmd[1:cmd.index("--")]
        self.assertIn(f"{hostarch.IN_MUVM}=1", options)
        self.assertIn("BOL_DEBUG=1", options)
        self.assertIn("XDG_DATA_HOME=/data", options)
        # muvm sets HOME itself and has no Wayland socket of the host's.
        self.assertFalse(any(o.startswith(("HOME=", "WAYLAND_DISPLAY="))
                             for o in options))

    def test_no_relaunch_from_inside_muvm(self):
        a, b, c, d = _arm(16384, "/usr/bin/muvm")
        with a, b, c, d:
            self.assertIsNone(hostarch.muvm_command(
                ["gui"], {hostarch.IN_MUVM: "1"}, "/usr/bin/python3"))

    def test_no_relaunch_on_4k_pages_x86_64_or_when_opted_out(self):
        a, b, c, d = _arm(4096, "/usr/bin/muvm")
        with a, b, c, d:
            self.assertIsNone(hostarch.muvm_command(["gui"], {}, "python3"))
        a, b, c, d = _arm(16384, "/usr/bin/muvm")
        with a, b, c, d:
            self.assertIsNone(hostarch.muvm_command(
                ["gui"], {hostarch.NO_MUVM: "1"}, "python3"))
        with mock.patch.object(hostarch.platform, "machine",
                               return_value="x86_64"), \
                mock.patch.object(hostarch, "muvm",
                                  return_value="/usr/bin/muvm"):
            self.assertIsNone(hostarch.muvm_command(["gui"], {}, "python3"))

    def test_without_muvm_the_launcher_stays_and_explains(self):
        a, b, c, d = _arm(16384)
        with a, b, c, d:
            self.assertIsNone(hostarch.muvm_command(["gui"], {}, "python3"))

    def test_relaunch_replaces_the_process(self):
        a, b, c, d = _arm(16384, "/usr/bin/muvm")
        with a, b, c, d, mock.patch.object(hostarch.os, "execv") as execv, \
                mock.patch.object(hostarch.sys, "orig_argv",
                                  ["/usr/bin/python3", "-m", "bol", "play"],
                                  create=True), \
                mock.patch.object(hostarch.sys, "executable",
                                  "/usr/bin/python3"):
            hostarch.relaunch_in_muvm()
        program, cmd = execv.call_args.args
        self.assertEqual(program, "/usr/bin/muvm")
        self.assertEqual(cmd[-4:], ["/usr/bin/python3", "-m", "bol", "play"])


if __name__ == "__main__":
    unittest.main()
