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

    def test_setup_stops_before_downloading_anything(self):
        from bol import gamesetup
        with mock.patch.object(gamesetup.hostarch, "problem",
                               return_value="no emulator"), \
                mock.patch.object(gamesetup, "mkdirs") as made:
            with self.assertRaises(gamesetup.BolError) as raised:
                gamesetup._do_setup()
        self.assertEqual(str(raised.exception), "no emulator")
        made.assert_not_called()


if __name__ == "__main__":
    unittest.main()
