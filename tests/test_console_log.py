"""Console logging when nobody reads the launcher's output any more (#261)."""
# SPDX-License-Identifier: MIT

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from bol import log

ROOT = Path(__file__).resolve().parents[1]


class DeadConsoleTests(unittest.TestCase):
    def _run_on_dead_pipe(self, code, *args):
        """Run ``code`` with stdout and stderr on a pipe that has no reader.

        That is what a desktop entry started through gtk-launch leaves the
        GUI with once gtk-launch itself has exited.
        """
        read_end, write_end = os.pipe()
        os.close(read_end)
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        try:
            return subprocess.run(
                [sys.executable, "-c", code, *args], cwd=ROOT, env=env,
                stdin=subprocess.DEVNULL, stdout=write_end, stderr=write_end,
                timeout=60).returncode
        finally:
            os.close(write_end)

    def test_logging_survives_a_pipe_whose_reader_exited(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = Path(tmp) / "child-status"
            status = self._run_on_dead_pipe(
                "import subprocess, sys\n"
                "from bol import log\n"
                "log.info('first line')\n"
                "log.warn('second line')\n"
                "child = subprocess.run(['sh', '-c', "
                "'echo out; echo err >&2'])\n"
                "open(sys.argv[1], 'w').write(str(child.returncode))\n",
                str(record))

            # Neither a BrokenPipeError traceback (1) nor a failed flush of
            # stdout at interpreter exit (120).
            self.assertEqual(status, 0)
            # A child inheriting the dead pipe would be killed by SIGPIPE.
            self.assertEqual(record.read_text(), "0")

    def test_only_streams_without_a_reader_count_as_gone(self):
        read_end, write_end = os.pipe()
        try:
            self.assertFalse(log._reader_gone(write_end))
            os.close(read_end)
            read_end = None
            self.assertTrue(log._reader_gone(write_end))
        finally:
            os.close(write_end)
            if read_end is not None:
                os.close(read_end)
        with tempfile.TemporaryFile() as regular:
            self.assertFalse(log._reader_gone(regular.fileno()))


if __name__ == "__main__":
    unittest.main()
