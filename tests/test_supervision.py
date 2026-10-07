"""A stop signal waits for the game it would otherwise orphan.

Powering off with Minecraft open killed the launcher on SIGTERM before its
teardown could clear the GPU-safety marker. The game closed two seconds
later, but the next boot refused PLAY as a "previous boot" incident until
the doctor acknowledged it. These tests pin the deferral: what it waits for,
what it never waits for, and that the process still ends with the signal it
was sent.
"""
# SPDX-License-Identifier: MIT

import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from bol import launch, supervision


def _reset():
    supervision._state.update(active=False, signum=None, since=None)
    supervision._session_over.set()


class DeferralTests(unittest.TestCase):
    def setUp(self):
        _reset()
        self.addCleanup(_reset)
        self.redelivered = []
        patcher = mock.patch.object(supervision, "_redeliver",
                                    side_effect=self.redelivered.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_without_a_session_a_signal_ends_the_process_at_once(self):
        supervision._on_signal(signal.SIGTERM, None)
        self.assertEqual(self.redelivered, [signal.SIGTERM])
        self.assertIsNone(supervision.stop_requested_for())

    def test_during_a_session_the_first_signal_is_only_recorded(self):
        supervision.begin_session()
        supervision._on_signal(signal.SIGTERM, None)
        self.assertEqual(self.redelivered, [])
        self.assertIsNotNone(supervision.stop_requested_for())

    def test_a_second_signal_neither_replaces_nor_restarts_the_first(self):
        # A logind session scope sends SIGHUP right after SIGTERM at poweroff.
        supervision.begin_session()
        supervision._on_signal(signal.SIGTERM, None)
        since = supervision._state["since"]
        supervision._on_signal(signal.SIGHUP, None)
        self.assertEqual(supervision._state["signum"], signal.SIGTERM)
        self.assertEqual(supervision._state["since"], since)
        self.assertEqual(self.redelivered, [])

    def test_the_wait_is_measured_from_the_first_signal(self):
        supervision.begin_session()
        supervision._on_signal(signal.SIGTERM, None)
        since = supervision._state["since"]
        self.assertEqual(
            supervision.stop_requested_for(clock=lambda: since + 7.5), 7.5)

    def test_ending_the_session_delivers_the_held_signal_once(self):
        supervision.begin_session()
        supervision._on_signal(signal.SIGHUP, None)
        supervision.end_session()
        self.assertEqual(self.redelivered, [signal.SIGHUP])

    def test_ending_a_session_nobody_stopped_ends_nothing(self):
        supervision.begin_session()
        supervision.end_session()
        self.assertEqual(self.redelivered, [])

    def test_a_launch_thread_leaves_the_delivery_to_the_main_thread(self):
        supervision.begin_session()
        supervision._on_signal(signal.SIGTERM, None)
        worker = threading.Thread(target=supervision.end_session)
        worker.start()
        worker.join()
        self.assertEqual(self.redelivered, [])
        self.assertTrue(supervision._session_over.is_set())

    def test_the_main_thread_holds_until_the_launch_thread_is_done(self):
        supervision.begin_session()
        supervision._on_signal(signal.SIGTERM, None)
        ended = []

        def teardown():
            time.sleep(0.2)
            ended.append(time.monotonic())
            supervision.end_session()

        worker = threading.Thread(target=teardown)
        worker.start()
        supervision.hold_until_session_ends(timeout=5)
        delivered = time.monotonic()
        worker.join()
        self.assertEqual(self.redelivered, [signal.SIGTERM])
        self.assertLessEqual(ended[0], delivered)

    def test_holding_is_a_no_op_without_a_held_signal(self):
        supervision.begin_session()
        started = time.monotonic()
        supervision.hold_until_session_ends(timeout=5)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(self.redelivered, [])

    def test_a_hold_gives_up_after_its_limit(self):
        supervision.begin_session()
        supervision._on_signal(signal.SIGTERM, None)
        supervision.hold_until_session_ends(timeout=0.05)
        self.assertEqual(self.redelivered, [signal.SIGTERM])


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.saved = {signum: signal.getsignal(signum)
                      for signum in supervision.DEFERRED_SIGNALS}
        self.saved_previous = dict(supervision._previous)
        supervision._previous.clear()

    def tearDown(self):
        for signum, handler in self.saved.items():
            signal.signal(signum, handler)
        supervision._previous.clear()
        supervision._previous.update(self.saved_previous)

    def test_default_dispositions_are_taken_over(self):
        for signum in supervision.DEFERRED_SIGNALS:
            signal.signal(signum, signal.SIG_DFL)
        self.assertTrue(supervision.install())
        for signum in supervision.DEFERRED_SIGNALS:
            self.assertIs(signal.getsignal(signum), supervision._on_signal)

    def test_an_ignored_or_foreign_handler_is_left_alone(self):
        def foreign(_signum, _frame):
            pass

        signal.signal(signal.SIGHUP, signal.SIG_IGN)   # as under nohup
        signal.signal(signal.SIGTERM, foreign)
        self.assertFalse(supervision.install())
        self.assertIs(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)
        self.assertIs(signal.getsignal(signal.SIGTERM), foreign)

    def test_only_the_main_thread_installs(self):
        for signum in supervision.DEFERRED_SIGNALS:
            signal.signal(signum, signal.SIG_DFL)
        result = []
        worker = threading.Thread(
            target=lambda: result.append(supervision.install()))
        worker.start()
        worker.join()
        self.assertEqual(result, [False])
        for signum in supervision.DEFERRED_SIGNALS:
            self.assertIs(signal.getsignal(signum), signal.SIG_DFL)


class LaunchEndsTheSessionTests(unittest.TestCase):
    """launch() owns the end of the session, however _launch_once ends."""

    def setUp(self):
        _reset()
        self.addCleanup(_reset)
        self.redelivered = []
        patcher = mock.patch.object(supervision, "_redeliver",
                                    side_effect=self.redelivered.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    @contextmanager
    def _no_lock(self):
        yield ()

    def _launch(self, body):
        with mock.patch.object(launch, "launch_lock", self._no_lock), \
                mock.patch.object(launch, "_launch_once", side_effect=body):
            return launch.launch()

    def test_a_held_signal_is_delivered_after_the_launch_returns(self):
        order = []

        def body(*_args, **_kwargs):
            supervision.begin_session()
            supervision._on_signal(signal.SIGTERM, None)
            order.append("teardown")
            return 0

        with mock.patch.object(supervision, "_redeliver",
                               side_effect=order.append):
            self.assertEqual(self._launch(body), 0)
        self.assertEqual(order, ["teardown", signal.SIGTERM])

    def test_a_failed_launch_still_ends_its_session(self):
        def body(*_args, **_kwargs):
            supervision.begin_session()
            supervision._on_signal(signal.SIGTERM, None)
            raise launch.BolError("teardown failed")

        with self.assertRaises(launch.BolError):
            self._launch(body)
        self.assertEqual(self.redelivered, [signal.SIGTERM])
        self.assertFalse(supervision._state["active"])

    def test_a_launch_nobody_stopped_ends_nothing(self):
        def body(*_args, **_kwargs):
            supervision.begin_session()
            return 0

        self._launch(body)
        self.assertEqual(self.redelivered, [])
        self.assertFalse(supervision._state["active"])


_CHILD = textwrap.dedent("""
    import os, sys, time
    from pathlib import Path
    sys.path.insert(0, {root!r})
    from bol import supervision

    work = Path(sys.argv[1])
    session = sys.argv[2] == "session"
    supervision.install()
    if session:
        supervision.begin_session()
    (work / "ready").write_text("")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if session and supervision.stop_requested_for() is not None:
            break
        time.sleep(0.02)
    (work / "stop-seen").write_text("")
    # The game is still closing: the second signal arrives meanwhile.
    while not (work / "game-closed").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    (work / "teardown").write_text("")
    supervision.end_session()
    (work / "survived").write_text("")
""")


class RealSignalTests(unittest.TestCase):
    """The real thing, in a child process: no patched delivery."""

    def _start(self, mode):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        work = Path(tmp.name)
        root = str(Path(__file__).resolve().parent.parent)
        child = subprocess.Popen(
            [sys.executable, "-c", _CHILD.format(root=root), str(work), mode],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self._wait_for(work / "ready", child)
        return child, work

    def _wait_for(self, path, child, timeout=10):
        deadline = time.monotonic() + timeout
        while not path.exists():
            if child.poll() is not None or time.monotonic() > deadline:
                self.fail(f"{path.name} never appeared "
                          f"(rc={child.poll()})")
            time.sleep(0.02)

    def test_a_signal_with_no_session_ends_the_process_at_once(self):
        child, work = self._start("idle")
        started = time.monotonic()
        child.send_signal(signal.SIGTERM)
        self.assertEqual(child.wait(timeout=10), -signal.SIGTERM)
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse((work / "teardown").exists())

    def test_sigterm_then_sighup_wait_for_the_teardown(self):
        child, work = self._start("session")
        child.send_signal(signal.SIGTERM)
        self._wait_for(work / "stop-seen", child)
        child.send_signal(signal.SIGHUP)
        time.sleep(0.2)
        self.assertIsNone(child.poll(), "the second signal cut the wait short")
        (work / "game-closed").write_text("")
        self.assertEqual(child.wait(timeout=10), -signal.SIGTERM)
        self.assertTrue((work / "teardown").exists())
        self.assertFalse((work / "survived").exists())


if __name__ == "__main__":
    unittest.main()
