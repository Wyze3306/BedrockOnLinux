"""A stop signal reaches the launch thread while Qt idles in C++.

Python runs a signal handler only when the main thread next runs Python, and
Qt's event loop can sit in C++ for as long as nothing happens on screen. The
window watches the wakeup pipe the C-level handler writes to, runs the
handler, and holds the main thread until the launch thread has cleared the
game's GPU marker; only then does the process end with the signal.
"""
# SPDX-License-Identifier: MIT

import os
import signal
import threading
import time
import unittest
from unittest import mock

from PySide6.QtWidgets import QApplication

from bol import gui, supervision
from tests.guiharness import qt_app, run_loop_until_quit


class StopSignalWatchTests(unittest.TestCase):
    def setUp(self):
        self.app = qt_app()
        supervision._state.update(active=False, signum=None, since=None)
        supervision._session_over.set()
        self.addCleanup(supervision._session_over.set)
        self.addCleanup(supervision._state.update,
                        active=False, signum=None, since=None)
        read_fd, write_fd = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        self.addCleanup(os.close, read_fd)
        self.addCleanup(os.close, write_fd)
        self.fds = (read_fd, write_fd)
        self.events = []

        def delivered(signum):
            self.events.append(("delivered", signum))
            QApplication.instance().quit()

        patcher = mock.patch.object(supervision, "_redeliver",
                                    side_effect=delivered)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_process_ends_only_after_the_launch_thread_is_done(self):
        notifier = gui._watch_stop_signals(self.app, fds=self.fds)
        self.addCleanup(notifier.setEnabled, False)
        supervision.begin_session()

        def launch_thread():
            # The game takes a moment to close and the marker to be cleared.
            time.sleep(0.3)
            self.events.append(("teardown done",))
            supervision.end_session()

        worker = threading.Thread(target=launch_thread)
        # What the C-level handler does: record, then write to the pipe.
        supervision._on_signal(signal.SIGTERM, None)
        os.write(self.fds[1], b"\x0f")
        worker.start()
        self.assertTrue(run_loop_until_quit(self.app, timeout_ms=5000))
        worker.join()
        self.assertEqual(self.events,
                         [("teardown done",), ("delivered", signal.SIGTERM)])

    def test_a_wakeup_with_nothing_held_changes_nothing(self):
        notifier = gui._watch_stop_signals(self.app, fds=self.fds)
        self.addCleanup(notifier.setEnabled, False)
        os.write(self.fds[1], b"\x02")   # SIGINT also writes to the pipe
        run_loop_until_quit(self.app, timeout_ms=300)
        self.assertEqual(self.events, [])


if __name__ == "__main__":
    unittest.main()
