"""bol.gui_report — the "Report a problem" window.

One window, three things on it: what the player says (a title, a comment),
what the launcher adds (every piece of bol.report, each with a box to leave
it out and its exact text one click away), and where it goes (the host,
named, above the button that sends it).

Nothing leaves until Send is pressed. Opening the window only reads this
machine, on a worker thread; closing it waits for whichever thread is still
running, so none outlives the window.
"""
# SPDX-License-Identifier: MIT

from __future__ import annotations

import subprocess
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy,
    QStackedWidget, QVBoxLayout, QWidget,
)

from . import report as problem
from .gui import Worker, _alive, _park_worker, btn

MESSAGE_KEY = "message"


class PieceRow(QFrame):
    """One part of the report: a box to keep it, and its text on demand."""

    def __init__(self, key, title, size_text, text_for, removable=True,
                 on_toggle=None, prose=False, parent=None):
        super().__init__(parent)
        self.key = key
        self._text_for = text_for
        self.setObjectName("Pill")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 6, 8, 6)
        outer.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.check = QCheckBox(title)
        self.check.setChecked(True)
        self.check.setEnabled(removable)
        if not removable:
            self.check.setToolTip("Always part of the report.")
        if on_toggle:
            self.check.toggled.connect(lambda on: on_toggle(key, on))
        head.addWidget(self.check, 1)
        self.size = QLabel(size_text)
        self.size.setObjectName("Muted")
        head.addWidget(self.size)
        self.toggle = QPushButton("Show")
        self.toggle.setObjectName("GhostSmall")
        self.toggle.setCursor(Qt.PointingHandCursor)
        self.toggle.setCheckable(True)
        self.toggle.setToolTip("Show exactly what this part contains")
        self.toggle.toggled.connect(self._show)
        head.addWidget(self.toggle)
        outer.addLayout(head)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        if prose:
            self.view.setFixedHeight(110)
        else:
            # The application style sheet names a font for every widget, so
            # the fixed one has to be asked for the same way.
            family = QFontDatabase.systemFont(QFontDatabase.FixedFont).family()
            self.view.setStyleSheet(
                f"font-family: '{family}', monospace; font-size: 12px;")
            self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
            self.view.setFixedHeight(190)
        self.view.setVisible(False)
        outer.addWidget(self.view)

    def _show(self, on):
        self.toggle.setText("Hide" if on else "Show")
        if on:
            self.view.setPlainText(self._text_for())
        self.view.setVisible(on)

    def refresh(self):
        if self.toggle.isChecked():
            self.view.setPlainText(self._text_for())


class ReportDialog(QDialog):
    def __init__(self, parent=None, theme=None, activity_log="", title="",
                 comment="", collect=None, send=None, open_in_browser=None):
        super().__init__(parent)
        self.setObjectName("Root")
        self.setWindowTitle("Report a problem")
        if theme is not None:
            self.setStyleSheet(theme.qss())
        self.theme = theme
        self.report = None
        self.rows = {}
        self.result_url = None
        self.saved_path = None
        self._workers = []
        self._sending = False
        self._activity_log = activity_log
        # Injected by the tests; the real ones otherwise.
        self._collect = collect or problem.collect
        self._send = send or problem.send
        self._open_in_browser = open_in_browser or problem.open_in_browser
        self.relay = problem.relay_url()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 16)
        outer.setSpacing(10)
        self.pages = QStackedWidget()
        outer.addWidget(self.pages)
        self.pages.addWidget(self._build_form(title, comment))
        self.pages.addWidget(self._build_done())
        self.resize(720, 700)
        self.setMinimumSize(460, 420)
        self._start_collecting()

    # ---------------------------------------------------------------- form
    def _build_form(self, title, comment):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        head = QLabel("Report a problem")
        head.setObjectName("Title")
        v.addWidget(head)
        sub = QLabel("Say what goes wrong. The launcher adds the technical "
                     "details below, with your name, your account, your "
                     "addresses and your worlds removed.")
        sub.setObjectName("Sub")
        sub.setWordWrap(True)
        v.addWidget(sub)

        self.title_edit = QLineEdit(title)
        self.title_edit.setPlaceholderText(
            "Title: what goes wrong, in one line")
        self.title_edit.setMaxLength(problem.MAX_TITLE)
        self.title_edit.textChanged.connect(self._message_changed)
        v.addWidget(self.title_edit)

        self.comment_edit = QPlainTextEdit(comment)
        self.comment_edit.setPlaceholderText(
            "What were you doing, what happened, and what did you expect? "
            "Since when? Does it happen every time?")
        self.comment_edit.setFixedHeight(96)
        self.comment_edit.setTabChangesFocus(True)
        self.comment_edit.textChanged.connect(self._message_changed)
        v.addWidget(self.comment_edit)

        label = QLabel("What will be sent")
        label.setStyleSheet("font-weight:700;")
        v.addWidget(label)
        self.removed = QLabel("Collecting system information and logs…")
        self.removed.setObjectName("Muted")
        self.removed.setWordWrap(True)
        v.addWidget(self.removed)

        self.list_host = QWidget()
        self.list = QVBoxLayout(self.list_host)
        self.list.setContentsMargins(0, 0, 6, 0)
        self.list.setSpacing(6)
        self.list.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(self.list_host)
        scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        v.addWidget(scroll, 1)

        where = QFrame()
        where.setObjectName("CardFlat")
        wv = QVBoxLayout(where)
        wv.setContentsMargins(12, 10, 12, 10)
        self.where = QLabel(problem.destination())
        self.where.setWordWrap(True)
        self.where.setTextInteractionFlags(Qt.TextSelectableByMouse)
        wv.addWidget(self.where)
        v.addWidget(where)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setVisible(False)
        v.addWidget(self.status)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.save_btn = btn("Save to a file…", self._save_as, kind="ghost",
                            tip="Write the full report to a file, send "
                                "nothing")
        buttons.addWidget(self.save_btn)
        self.copy_btn = btn("Copy", self._copy, kind="ghost",
                            tip="Copy the full report, send nothing")
        buttons.addWidget(self.copy_btn)
        buttons.addStretch(1)
        buttons.addWidget(btn("Cancel", self.reject, kind="ghost"))
        self.browser_btn = btn(
            "Use my browser", self._send_by_browser, kind="ghost",
            tip="Open GitHub's issue form in your browser instead of "
                "sending through the relay")
        self.browser_btn.setVisible(bool(self.relay))
        buttons.addWidget(self.browser_btn)
        self.send_btn = btn("Send", self._send_clicked, kind="primary")
        self.send_btn.setMinimumWidth(110)
        self.send_btn.setToolTip(
            "File the report as a public issue" if self.relay else
            "Save the report and open GitHub's issue form in your browser")
        buttons.addWidget(self.send_btn)
        v.addLayout(buttons)
        self._sync_buttons()
        return page

    def _build_done(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)
        self.done_title = QLabel("")
        self.done_title.setObjectName("Title")
        v.addWidget(self.done_title)
        self.done_text = QLabel("")
        self.done_text.setWordWrap(True)
        self.done_text.setTextFormat(Qt.RichText)
        self.done_text.setOpenExternalLinks(False)
        self.done_text.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.done_text.linkActivated.connect(problem.open_url)
        v.addWidget(self.done_text)
        row = QHBoxLayout()
        self.done_open = btn("Open the issue", lambda: problem.open_url(
            self.result_url), kind="primary")
        row.addWidget(self.done_open)
        self.done_copy = btn("Copy the link", self._copy_link, kind="ghost")
        row.addWidget(self.done_copy)
        self.done_folder = btn("Show the file", self._show_file, kind="ghost")
        row.addWidget(self.done_folder)
        row.addStretch(1)
        row.addWidget(btn("Close", self.accept, kind="ghost"))
        v.addStretch(1)
        v.addLayout(row)
        return page

    # ------------------------------------------------------------- workers
    def _run(self, function, done, failed):
        worker = Worker(function)
        worker.done.connect(done)
        worker.failed.connect(failed)
        self._workers.append(worker)
        worker.start()
        return worker

    def done(self, result):
        """Closing, however it happens, waits for the threads it started."""
        for worker in self._workers:
            for signal in (worker.done, worker.failed):
                try:
                    signal.disconnect()
                except (RuntimeError, TypeError):
                    pass
            if worker.isRunning() and not worker.wait(8000):
                # A send stuck on the network: let it finish without us
                # rather than freeze the launcher on it.
                _park_worker(worker)
        self._workers.clear()
        super().done(result)

    # ---------------------------------------------------------- collecting
    def _start_collecting(self):
        activity = self._activity_log
        self._run(lambda: self._collect(activity_log=activity),
                  self._collected, self._collect_failed)

    def _collect_failed(self, message):
        self.removed.setText("Could not collect the details.")
        self._say(f"The technical details could not be collected "
                  f"({message}). You can still describe the problem and "
                  f"send that.", error=True)
        from . import scrub
        self.report = problem.Report(scrub.Scrubber(
            [(str(Path.home()), "~")]))
        self._fill()

    def _collected(self, report):
        self.report = report
        self._fill()

    def _fill(self):
        report = self.report
        self._apply_message()
        self._add_row(PieceRow(
            MESSAGE_KEY, "Your title and comment", "", self._message_text,
            removable=False, prose=True))
        for piece in report.pieces:
            row = PieceRow(piece.key, piece.title,
                           f"{len(piece.text):,} characters",
                           lambda piece=piece: piece.text,
                           on_toggle=self._toggled)
            row.check.setChecked(piece.included)
            self._add_row(row)
        removed = report.removed()
        self.removed.setText(
            ("Already removed from what follows: " + removed + "."
             if removed else "Nothing personal was found in what follows.")
            + " Untick a part to leave it out; “Show” displays exactly "
              "what it contains.")
        self.removed.setToolTip(
            "What the privacy pass took out of the logs and the system "
            "information before showing them here.")
        self._sync_buttons()

    def _add_row(self, row):
        self.rows[row.key] = row
        self.list.insertWidget(self.list.count() - 1, row)

    def _toggled(self, key, on):
        piece = self.report.piece(key) if self.report else None
        if piece is not None:
            piece.included = on

    # ------------------------------------------------------------- message
    def _apply_message(self):
        if self.report is not None:
            self.report.set_message(self.title_edit.text(),
                                    self.comment_edit.toPlainText())

    def _message_text(self):
        self._apply_message()
        return (f"Title: {self.report.title or '(none yet)'}\n\n"
                f"{self.report.comment or '(no comment yet)'}")

    def _message_changed(self):
        row = self.rows.get(MESSAGE_KEY)
        if row is not None:
            row.refresh()
        self._sync_buttons()

    def _sync_buttons(self):
        ready = self.report is not None and not self._sending
        titled = len(self.title_edit.text().strip()) >= 4
        self.send_btn.setEnabled(ready and titled)
        self.browser_btn.setEnabled(ready and titled)
        self.save_btn.setEnabled(ready)
        self.copy_btn.setEnabled(ready)
        self.send_btn.setText(
            "Sending…" if self._sending else
            "Send" if self.relay else "Send with my browser")

    def _say(self, text, error=False):
        self.status.setText(text)
        color = (self.theme.red if error else self.theme.sub) \
            if self.theme is not None else ("#e0574a" if error else "")
        self.status.setStyleSheet(f"color:{color};" if color else "")
        self.status.setVisible(bool(text))

    # ----------------------------------------------------------- ways out
    def _save_as(self):
        self._apply_message()
        name, _ = QFileDialog.getSaveFileName(
            self, "Save the report",
            str(Path.home() / f"{problem.APP}-report.txt"),
            "Text files (*.txt)")
        if not name:
            return
        try:
            Path(name).write_text(self.report.full_text(), encoding="utf-8")
        except OSError as exc:
            self._say(f"The file could not be written: {exc}", error=True)
            return
        self._say("Saved. Nothing was sent.")

    def _copy(self):
        self._apply_message()
        QApplication.clipboard().setText(self.report.full_text())
        self.copy_btn.setText("Copied ✓")
        QTimer.singleShot(1500, lambda: _alive(self.copy_btn)
                          and self.copy_btn.setText("Copy"))

    def _send_clicked(self):
        if self.relay:
            self._send_by_relay()
        else:
            self._send_by_browser()

    def _send_by_relay(self):
        self._apply_message()
        report, relay = self.report, self.relay
        self._say("Sending…")
        self._sending = True
        self._run(lambda: self._send(report, relay), self._sent,
                  self._send_failed)
        self._sync_buttons()

    def _sent(self, url):
        self._sending = False
        self.result_url = url
        self.done_title.setText("Report filed")
        self.done_text.setText(
            "Thank you. Your report is now a public issue, where the answer "
            "will be posted:<br><br>"
            f"<a href=\"{url}\" style=\"color:{self._link_color()};\">"
            f"{url}</a><br><br>"
            "Keep the link: follow-up questions are asked there.")
        self.done_folder.setVisible(False)
        self.pages.setCurrentIndex(1)

    def _link_color(self):
        # Qt's default link blue is unreadable on the dark theme.
        return self.theme.blue if self.theme is not None else "#4a90d9"

    def _send_failed(self, message):
        self._sending = False
        self._say(f"{message} You can send it with your browser instead: "
                  "press “Use my browser”.", error=True)
        self._sync_buttons()

    def _send_by_browser(self):
        self._apply_message()
        try:
            path, url, opened = self._open_in_browser(self.report)
        except OSError as exc:
            self._say(f"The report could not be saved: {exc}", error=True)
            return
        self.saved_path = path
        self.result_url = url
        # A URL cannot hold the logs, so they travel by the clipboard or by
        # the saved file, whichever the player finds easier.
        QApplication.clipboard().setText(self.report.full_text())
        self.done_title.setText("Finish in your browser" if opened
                                else "Open the form to finish")
        self.done_text.setText(
            ("Your browser is opening GitHub's new-issue form, with your "
             "title, your comment and the system summary filled in."
             if opened else
             "No browser could be opened. Use “Open the form” below, or "
             "copy the link into a browser.")
            + "<br><br>The logs do not fit in a link. To add them, do one "
              "of these in the form before you submit it:<ul>"
              f"<li>drag the file <b>{path.name}</b> into the text box "
              "(“Show the file” opens its folder);</li>"
              "<li>or paste: the full report is in your clipboard.</li></ul>"
              "Nothing has been sent by the launcher. The issue is created "
              "when you press Submit on GitHub, under your GitHub account.")
        self.done_open.setText("Open the form")
        self.done_folder.setVisible(True)
        self.pages.setCurrentIndex(1)

    def _copy_link(self):
        QApplication.clipboard().setText(self.result_url or "")
        self.done_copy.setText("Copied ✓")
        QTimer.singleShot(1500, lambda: _alive(self.done_copy)
                          and self.done_copy.setText("Copy the link"))

    def _show_file(self):
        if self.saved_path is None:
            return
        try:
            subprocess.Popen(["xdg-open", str(self.saved_path.parent)],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError:
            pass


def open_report_dialog(window, title="", comment=""):
    """Show the window over the launcher's; one at a time."""
    current = getattr(window, "_report_dialog", None)
    if current is not None and _alive(current) and current.isVisible():
        current.raise_()
        current.activateWindow()
        return current
    log_view = getattr(window, "log_view", None)
    dialog = ReportDialog(
        window, theme=getattr(window, "theme", None),
        activity_log=log_view.toPlainText() if _alive(log_view) else "",
        title=title, comment=comment)
    dialog.setAttribute(Qt.WA_DeleteOnClose, False)
    window._report_dialog = dialog
    dialog.show()
    return dialog
