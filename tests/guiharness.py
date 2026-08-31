"""A real MainWindow with nothing reaching outside the process.

Not collected by pytest (the filename does not match test_*.py). Shared by
the GUI test modules so none of them has to import another one for it.

The point of the patch list is that MainWindow.__init__ does real work:
it arms singleShot timers for refresh_versions and the update check, calls
xodus.signed_in() while building Settings, and installs itself as the global
log sink. Any test that runs the event loop would otherwise reach the
network, and the sink would outlive the window it writes to.

`store_signed_in` is the download account's state. It defaults to signed out
so a test says what it wants rather than inheriting whatever Microsoft Store
session the developer happens to have on the machine running the suite.
"""
# SPDX-License-Identifier: MIT

import os
from contextlib import contextmanager
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QApplication

from bol import gui, log, xodus


def qt_app():
    """The one QApplication for the test session, configured like gui()."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(["bedrock-on-linux-tests"])
    # gui() sets this, and the launcher's exit path depends on it.
    app.setQuitOnLastWindowClosed(False)
    return app


@contextmanager
def headless_window(store_signed_in=False, **settings):
    saved_sink = log._LOG_SINK
    with mock.patch.object(gui, "NativeAuth"), \
            mock.patch.object(gui, "msa_signed_in", return_value=False), \
            mock.patch.object(gui, "msa_gamertag", return_value=None), \
            mock.patch.object(gui, "current_profile_name", return_value="Default"), \
            mock.patch.object(gui, "current_profile_info", return_value={"path": None}), \
            mock.patch.object(gui, "list_profiles", return_value=[]), \
            mock.patch.object(gui, "load_settings", lambda: dict(settings)), \
            mock.patch.object(gui, "save_settings", lambda _s: None), \
            mock.patch.object(gui.MainWindow, "refresh_versions", lambda _s: None), \
            mock.patch.object(gui.MainWindow, "check_for_update_async", lambda _s: None), \
            mock.patch.object(gui, "installed_builds", return_value=[]), \
            mock.patch.object(gui, "mc_releases", return_value=[]), \
            mock.patch.object(gui, "gh_releases", return_value=[]), \
            mock.patch.object(xodus, "signed_in", return_value=store_signed_in):
        window = gui.MainWindow()
        try:
            yield window
        finally:
            # Everything this window started has to be finished before it is
            # dropped: a QThread still running when its last reference goes
            # away aborts the process, and the whole suite went down that way
            # -- a Settings tab opened in one test left its build scan running
            # into the next ones. closeEvent parks them, so they are still
            # valid objects to wait on here.
            workers = [w for w in window._workers.values() if w is not None]
            window._force_close = True
            window.close()
            for worker in workers:
                worker.wait(5000)
            window.deleteLater()
            # Flush the deferred-delete queue instead of leaving it to an
            # event loop no test runs. Without this every window built by the
            # suite stays alive, and apply_theme() -> setStyleSheet() re-
            # polishes every widget in the application -- so each new window
            # costs more than the last, and a full run degrades from seconds
            # into minutes.
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            log._LOG_SINK = saved_sink


def run_loop_until_quit(app, timeout_ms=3000):
    """Run the event loop; return True if something quit it before the
    watchdog fired."""
    timed_out = []
    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(lambda: (timed_out.append(True), app.quit()))
    watchdog.start(timeout_ms)
    app.exec()
    watchdog.stop()
    return not timed_out
