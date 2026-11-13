"""An update that cannot start is undone at the next start."""
# SPDX-License-Identifier: MIT

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import rollback


class RollbackTests(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.dir = Path(holder.name)
        self.app = self.dir / "BedrockOnLinux.AppImage"
        self.app.write_bytes(b"new launcher")
        rollback.backup_path(self.app).write_bytes(b"old launcher")
        for patch in (
                mock.patch.object(rollback, "DATA", self.dir / "data"),
                mock.patch.dict("os.environ", {"APPIMAGE": str(self.app),
                                               rollback.PROBE_ENV: ""}),
                mock.patch.object(rollback.sys, "argv",
                                  [str(self.app), "play"])):
            patch.start()
            self.addCleanup(patch.stop)
        self.execv = mock.Mock()

    def _start(self, version="3.0.1"):
        with mock.patch.object(rollback, "VERSION", version):
            rollback.guard(execv=self.execv)

    def _confirm(self, version="3.0.1"):
        with mock.patch.object(rollback, "VERSION", version):
            rollback.confirm()

    def test_a_launcher_that_comes_up_keeps_its_update(self):
        rollback.arm(self.app, "3.0.0", "3.0.1")
        self._start()
        self._confirm()
        self.assertFalse(rollback.marker_path().exists())
        for _ in range(5):
            self._start()
        self.execv.assert_not_called()
        self.assertEqual(self.app.read_bytes(), b"new launcher")

    def test_a_launcher_that_never_comes_up_is_replaced_by_the_previous(self):
        rollback.arm(self.app, "3.0.0", "3.0.1")
        for _ in range(rollback.MAX_UNCONFIRMED):
            self._start()                       # starts, never confirms
            self.assertEqual(self.app.read_bytes(), b"new launcher")
        self._start()
        self.assertEqual(self.app.read_bytes(), b"old launcher")
        self.execv.assert_called_once_with(
            str(self.app), [str(self.app), "play"])
        self.assertFalse(rollback.marker_path().exists())
        self.assertEqual(rollback.last_rollback()["from"], "3.0.1")
        self.assertIsNone(rollback.last_rollback())     # told once

    def test_a_portable_file_is_restarted_through_its_interpreter(self):
        with mock.patch.dict("os.environ", {"APPIMAGE": ""}):
            rollback.arm(self.app, "3.0.0", "3.0.1")
            for _ in range(rollback.MAX_UNCONFIRMED + 1):
                self._start()
        self.execv.assert_called_once_with(
            rollback.sys.executable,
            [rollback.sys.executable, str(self.app), "play"])

    def test_the_version_check_before_a_swap_does_not_count_as_a_start(self):
        rollback.arm(self.app, "3.0.0", "3.0.1")
        with mock.patch.dict("os.environ", {rollback.PROBE_ENV: "1"}):
            for _ in range(5):
                self._start()
        self.execv.assert_not_called()

    def test_asking_for_the_version_is_not_a_start_either(self):
        rollback.arm(self.app, "3.0.0", "3.0.1")
        with mock.patch.object(rollback.sys, "argv",
                               [str(self.app), "--version"]):
            for _ in range(5):
                self._start()
        self.execv.assert_not_called()
        self.assertEqual(self.app.read_bytes(), b"new launcher")

    def test_another_install_sharing_the_data_directory_is_left_alone(self):
        rollback.arm(self.dir / "another.AppImage", "3.0.0", "3.0.1")
        for _ in range(5):
            self._start()
        self.execv.assert_not_called()
        self.assertTrue(rollback.marker_path().exists())

    def test_the_previous_version_running_again_clears_the_marker(self):
        rollback.arm(self.app, "3.0.0", "3.0.1")
        self._start(version="3.0.0")
        self.assertFalse(rollback.marker_path().exists())
        self.execv.assert_not_called()

    def test_without_the_previous_file_the_launcher_just_carries_on(self):
        rollback.arm(self.app, "3.0.0", "3.0.1")
        rollback.backup_path(self.app).unlink()
        for _ in range(rollback.MAX_UNCONFIRMED + 1):
            self._start()
        self.execv.assert_not_called()
        self.assertEqual(self.app.read_bytes(), b"new launcher")

    def test_the_marker_cannot_name_the_file_that_is_put_back(self):
        planted = self.dir / "planted"
        planted.write_bytes(b"someone else's launcher")
        rollback.arm(self.app, "3.0.0", "3.0.1")
        marker = rollback._read(rollback.marker_path())
        marker["backup"] = str(planted)
        rollback._write(rollback.marker_path(), marker)
        for _ in range(rollback.MAX_UNCONFIRMED + 1):
            self._start()
        self.assertEqual(self.app.read_bytes(), b"old launcher")
        self.assertTrue(planted.exists())

    def test_a_corrupt_marker_never_stops_a_start(self):
        rollback.marker_path().parent.mkdir(parents=True)
        rollback.marker_path().write_text("{ not json")
        self._start()
        rollback.marker_path().write_text('["a list"]')
        self._start()
        self.execv.assert_not_called()


if __name__ == "__main__":
    unittest.main()
