"""What the window says while Minecraft comes down from Microsoft.

The download is a single 2.3 GiB package, and both halves of the machinery
that used to report it counted in C++ ints: a Qt `int` in the worker's signal,
and the QProgressBar's own range. PySide6 wraps anything past 2 GiB into that
with nothing louder than a RuntimeWarning, so the total arrived negative,
`max(1, total)` turned it into 1, and the status line multiplied the byte
count by a hundred — "Downloading Minecraft…  24346886100%" on a bar that had
been full since the first chunk (issue #216).

These tests use the real figures from that report, so they fail on any signal
or widget that is narrower than the download it carries.
"""
# SPDX-License-Identifier: MIT

import unittest

from bol import gui

from tests.guiharness import headless_window, qt_app


# The 1.26.44.3 package, and roughly where the screenshot in #216 was taken.
PACKAGE_BYTES = 2469606195
PART_WAY = 243468861


class ProgressSignalWidthTests(unittest.TestCase):
    """The counts have to survive being emitted at all."""

    def _round_trip(self, worker):
        seen = []
        worker.progress.connect(lambda got, total: seen.append((got, total)))
        worker.progress.emit(PART_WAY, PACKAGE_BYTES)
        return seen

    def test_the_launch_worker_carries_a_multi_gigabyte_total(self):
        qt_app()
        seen = self._round_trip(gui.LaunchWorker({"edition": None,
                                                  "tag": "1.26.44.3"}))
        self.assertEqual(seen, [(PART_WAY, PACKAGE_BYTES)])

    def test_the_generic_worker_carries_a_multi_gigabyte_total(self):
        qt_app()
        seen = self._round_trip(gui.Worker(lambda: None))
        self.assertEqual(seen, [(PART_WAY, PACKAGE_BYTES)])


class ProgressReadoutTests(unittest.TestCase):
    """And to be reported as a proportion once they arrive."""

    def test_a_multi_gigabyte_download_reads_as_a_sane_percentage(self):
        qt_app()
        with headless_window() as window:
            window.set_progress(PART_WAY, PACKAGE_BYTES)
            status = window.status_label.text()

        self.assertIn("9%", status)
        self.assertNotIn("24346886100", status)

    def test_the_download_reads_the_same_through_the_worker_that_reports_it(self):
        # The whole path, wired as do_play() and _run_update() wire it: the
        # figure in the #216 screenshot came out of the signal, not out of
        # set_progress alone, and a signal that wraps its total makes every
        # reading downstream of it wrong however careful the reading is.
        qt_app()
        with headless_window() as window:
            worker = gui.LaunchWorker({"edition": None, "tag": "1.26.44.3"})
            worker.progress.connect(window.set_progress)
            worker.progress.emit(PART_WAY, PACKAGE_BYTES)
            status = window.status_label.text()

        self.assertEqual(status.split()[-1], "9%")

    def test_the_bar_tracks_the_download_instead_of_pinning_full(self):
        qt_app()
        with headless_window() as window:
            window.set_progress(PART_WAY, PACKAGE_BYTES)
            fraction = (window.progress.value() /
                        max(1, window.progress.maximum()))
            window.set_progress(PACKAGE_BYTES, PACKAGE_BYTES)
            finished = window.progress.value()
            maximum = window.progress.maximum()

        self.assertAlmostEqual(fraction, PART_WAY / PACKAGE_BYTES, places=2)
        self.assertEqual(finished, maximum)

    def test_the_percentage_never_passes_a_hundred(self):
        qt_app()
        with headless_window() as window:
            # xodus.py clamps this, but the reading must not depend on it:
            # a resumed download reports the bytes already on disk too.
            window.set_progress(PACKAGE_BYTES * 2, PACKAGE_BYTES)
            self.assertIn("100%", window.status_label.text())
            self.assertEqual(window.progress.value(),
                             window.progress.maximum())

    def test_a_total_nobody_knows_yet_sweeps_instead_of_claiming_zero(self):
        qt_app()
        with headless_window() as window:
            window.set_progress(PART_WAY, 0)
            # An indeterminate QProgressBar is the 0..0 range, and reporting
            # "0%" for a download already under way would be worse than
            # reporting nothing.
            self.assertEqual(
                (window.progress.minimum(), window.progress.maximum()), (0, 0))
            self.assertNotIn("%", window.status_label.text())


if __name__ == "__main__":
    unittest.main()


class KillStopsTheDownloadTests(unittest.TestCase):
    """KILL during PLAY's download stops the download (#202)."""

    def test_a_stop_ends_the_download_at_its_next_progress_report(self):
        from unittest import mock
        from bol.log import Cancelled
        qt_app()
        worker = gui.LaunchWorker({"edition": None, "tag": "1.26.63.1"})
        seen = []
        worker.progress.connect(lambda got, total: seen.append(got))
        worker._progress(1, PACKAGE_BYTES)
        with mock.patch("bol.xodus.cancel_download") as stop:
            self.assertTrue(worker.cancel())
        stop.assert_called_once_with()
        with self.assertRaises(Cancelled):
            worker._progress(2, PACKAGE_BYTES)
        self.assertEqual(seen, [1])

    def test_a_stop_after_setup_never_starts_the_game(self):
        from unittest import mock
        qt_app()
        worker = gui.LaunchWorker({"edition": None, "tag": "1.26.63.1"})
        stopped, failed = [], []
        worker.cancelled.connect(stopped.append)
        worker.failed.connect(failed.append)

        def setup(*args, **kwargs):
            # The stop arrives while setup is busy with something that never
            # reports progress, a prefix being made, say.
            worker.cancel()

        with mock.patch.object(gui, "setup_or_join", side_effect=setup), \
                mock.patch("bol.xodus.cancel_download"), \
                mock.patch.object(gui, "launch") as launch:
            worker.run()
        launch.assert_not_called()
        self.assertEqual(len(stopped), 1)
        self.assertEqual(failed, [])

    def test_once_the_game_runs_kill_is_for_wine_again(self):
        qt_app()
        worker = gui.LaunchWorker({"edition": None, "tag": "1.26.63.1"})
        worker._setting_up = False
        self.assertFalse(worker.cancel())
