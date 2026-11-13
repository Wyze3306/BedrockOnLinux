"""bol.gui_profiles — the Profiles page: who plays, and what is running.

Two lists. "Running now" is every Minecraft that is up on this machine,
whichever profile, window, shortcut or terminal started it, with the three
things one does to a running game: hide it, show it, close it. "Profiles" is
one card per profile, with what keeps profiles apart on it -- the Xbox
account, the folder -- and the two ways to start its game: on screen, or
hidden for a tool that drives it.

Nothing here has a fixed width. A button is as wide as its label in the
font the desktop gave the launcher, a path that does not fit is elided in
the middle and kept whole in its tooltip, and a card whose actions no longer
fit beside its text puts them under it.
"""
# SPDX-License-Identifier: MIT

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QBoxLayout, QFrame, QHBoxLayout, QLabel, QLayout, QMenu, QPushButton,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from . import icons, instances
from .gui_mods import _initials_pixmap
from .profiles import (
    current_profile_info, list_profiles, open_profile_window,
    profile_account, profile_build, profile_home, start_profile_game,
)

#: Under this width a card puts its actions below its text.
COMPACT_BELOW = 760
#: How often the running games are read again while the page is shown.
REFRESH_MS = 1500
#: How long a game started for another profile may take to show up in the
#: list before its card stops saying "Starting…".
START_TIMEOUT_S = 180.0
AVATAR = 44


class FlowLayout(QLayout):
    """Lays widgets out in a row and wraps them when the row is full."""

    def __init__(self, parent=None, spacing=8, align_right=False):
        super().__init__(parent)
        self._items = []
        self._spacing = spacing
        self._align_right = align_right
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._place(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._place(rect, apply=True)

    def _visible(self):
        return [item for item in self._items
                if item.widget() is None or not item.widget().isHidden()]

    def sizeHint(self):
        """Everything on one row: what the layout takes when it may."""
        items = self._visible()
        width = sum(item.sizeHint().width() for item in items) \
            + self._spacing * max(0, len(items) - 1)
        height = max((item.sizeHint().height() for item in items), default=0)
        return QSize(width, height)

    def minimumSize(self):
        size = QSize(0, 0)
        for item in self._visible():
            size = size.expandedTo(item.minimumSize())
        return size

    def _place(self, rect, apply):
        rows, row, used = [], [], 0
        for item in self._visible():
            hint = item.sizeHint()
            needed = hint.width() + (self._spacing if row else 0)
            if row and used + needed > rect.width():
                rows.append((row, used))
                row, used, needed = [], 0, hint.width()
            row.append((item, hint))
            used += needed
        if row:
            rows.append((row, used))
        y = rect.y()
        for row, used in rows:
            height = max(hint.height() for _item, hint in row)
            x = rect.x() + (max(0, rect.width() - used)
                            if self._align_right else 0)
            for item, hint in row:
                if apply:
                    item.setGeometry(QRect(
                        QPoint(x, y + (height - hint.height()) // 2), hint))
                x += hint.width() + self._spacing
            y += height + self._spacing
        return max(0, y - rect.y() - (self._spacing if rows else 0))


class ElidedLabel(QLabel):
    """One line of text that gives way instead of being cut off.

    What does not fit is elided, in the middle by default since both ends of
    a path matter, and the whole text is the tooltip whenever it is.
    """

    def __init__(self, text="", mode=Qt.ElideMiddle, parent=None):
        super().__init__(parent)
        self._full = ""
        self._mode = mode
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setText(text)

    def setText(self, text):
        self._full = str(text or "")
        self.updateGeometry()
        self.update()

    def text(self):
        return self._full

    def sizeHint(self):
        metrics = QFontMetrics(self.font())
        return QSize(metrics.horizontalAdvance(self._full) + 2,
                     metrics.height() + 2)

    def minimumSizeHint(self):
        metrics = QFontMetrics(self.font())
        return QSize(metrics.horizontalAdvance("…") * 6, metrics.height() + 2)

    def shown_text(self):
        """What is painted at the current width."""
        return QFontMetrics(self.font()).elidedText(
            self._full, self._mode, max(0, self.width()))

    def paintEvent(self, _event):
        shown = self.shown_text()
        self.setToolTip(self._full if shown != self._full else "")
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.drawText(self.rect(), Qt.AlignLeft | Qt.AlignVCenter, shown)
        painter.end()


def action_button(text, slot=None, kind="Ghost", tip=None) -> QPushButton:
    """A button as wide as its label needs, in whatever font is in use."""
    button = QPushButton(text)
    button.setObjectName(kind)
    button.setCursor(Qt.PointingHandCursor)
    button.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
    button.setMinimumHeight(32)
    if tip:
        button.setToolTip(tip)
    if slot is not None:
        button.clicked.connect(lambda _checked=False: slot())
    return button


def badge(text, tone="quiet") -> QLabel:
    label = QLabel(text)
    label.setObjectName("Badge")
    label.setProperty("tone", tone)
    label.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
    return label


def _display_path(path) -> str:
    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home):] if text.startswith(home + "/") else text


class _Card(QFrame):
    """Text on the left, actions on the right; actions below when narrow."""

    def __init__(self, object_name):
        super().__init__()
        self.setObjectName(object_name)
        self._box = QBoxLayout(QBoxLayout.LeftToRight, self)
        self._box.setContentsMargins(16, 14, 16, 14)
        self._box.setSpacing(14)
        self.actions = QWidget()
        self.flow = FlowLayout(self.actions, spacing=8, align_right=True)
        self.actions.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)

    def finish(self, body_layout):
        self._box.addLayout(body_layout, 1)
        self._box.addWidget(self.actions, 0, Qt.AlignVCenter)

    def set_compact(self, compact):
        self._box.setDirection(QBoxLayout.TopToBottom if compact
                               else QBoxLayout.LeftToRight)
        self.flow._align_right = not compact
        self._box.setAlignment(self.actions,
                               Qt.Alignment() if compact else Qt.AlignVCenter)
        self.flow.invalidate()


class InstanceRow(_Card):
    """One running game: what it is, and Show / Hide / Close."""

    def __init__(self, page, instance):
        super().__init__("InstanceRow")
        self.page = page
        self.instance = instance
        theme = page.window_.theme
        body = QVBoxLayout()
        body.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.name = QLabel(instance.profile)
        self.name.setObjectName("ProfileName")
        top.addWidget(self.name)
        self.state = badge("Hidden" if instance.hidden else "Visible",
                           "quiet" if instance.hidden else "live")
        top.addWidget(self.state)
        top.addStretch(1)
        body.addLayout(top)
        self.detail = ElidedLabel(mode=Qt.ElideRight)
        self.detail.setObjectName("Sub")
        body.addWidget(self.detail)
        self.finish(body)

        closing = instance.id in page.closing
        if instance.can_hide:
            if instance.hidden:
                toggle = action_button(
                    "Show", lambda: page.set_instance_hidden(instance, False),
                    tip="Put this game's window back on the screen")
            else:
                toggle = action_button(
                    "Hide", lambda: page.set_instance_hidden(instance, True),
                    tip="Take this game's window off the screen. It keeps "
                        "running, for a tool that drives it")
        else:
            toggle = action_button("Hide")
            toggle.setEnabled(False)
            toggle.setToolTip(
                "Only a game drawn through X11 can be hidden; this one "
                + ("runs inside its own gamescope."
                   if instance.backend == instances.BACKEND_GAMESCOPE
                   else "uses Wine's Wayland driver."))
        toggle.setEnabled(toggle.isEnabled() and not closing)
        self.toggle = toggle
        self.flow.addWidget(toggle)
        self.close_button = action_button(
            "Closing…" if closing else "Close",
            lambda: page.close_instance(instance), kind="DangerGhost",
            tip="Close this game. Its window is asked to close first, so "
                "the open world is saved")
        self.close_button.setEnabled(not closing)
        self.flow.addWidget(self.close_button)
        self.name.setStyleSheet(f"color:{theme.fg};")
        self.tick()

    def tick(self, now=None):
        """Refresh what changes while nothing else does: the duration."""
        item = self.instance
        parts = [" ".join(p for p in (item.title, item.version) if p),
                 "up " + instances.format_uptime(item.uptime(now)),
                 f"PID {item.pid}"]
        self.detail.setText("  ·  ".join(part for part in parts if part))


class ProfileCard(_Card):
    """One profile: who it is, what it is doing, and how to start it."""

    def __init__(self, page, profile, running, starting):
        super().__init__("ProfileCard")
        self.page = page
        self.profile = profile
        theme = page.window_.theme
        active = profile["active"]
        self.setProperty("active", "true" if active else "false")

        body = QHBoxLayout()
        body.setSpacing(14)
        avatar = QLabel()
        avatar.setFixedSize(AVATAR, AVATAR)
        avatar.setPixmap(_initials_pixmap(
            profile["name"],
            theme.accent if active
            else (theme.card3 if theme.dark else theme.muted), AVATAR))
        body.addWidget(avatar, 0, Qt.AlignTop)

        text = QVBoxLayout()
        text.setSpacing(3)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.name = ElidedLabel(profile["name"], Qt.ElideRight)
        self.name.setObjectName("ProfileName")
        self.name.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        top.addWidget(self.name)
        self.badges = []
        if active:
            self.badges.append(badge("This window", "accent"))
        if running:
            hidden = sum(1 for item in running if item.hidden)
            count = len(running)
            label = "Running" if count == 1 else f"Running · {count} windows"
            if hidden == count:
                label = "Running hidden" if count == 1 \
                    else f"Running · {count} hidden"
            self.badges.append(badge(label, "live"))
        elif starting:
            self.badges.append(badge("Starting…", "quiet"))
        for item in self.badges:
            top.addWidget(item)
        top.addStretch(1)
        text.addLayout(top)

        account = profile["account"]
        if account["gamertag"]:
            who = f"Xbox: {account['gamertag']}"
        elif account["signed_in"]:
            who = "Xbox: signed in"
        else:
            who = "Xbox: not signed in"
        build = profile["build"] or "No Minecraft version chosen yet"
        self.summary = ElidedLabel(f"{who}  ·  {build}", Qt.ElideRight)
        self.summary.setObjectName("Sub")
        text.addWidget(self.summary)
        self.path = ElidedLabel(_display_path(profile["home"]))
        self.path.setObjectName("Muted")
        text.addWidget(self.path)
        body.addLayout(text, 1)
        self.finish(body)

        path = profile["path"]
        # A profile plays one game at a time, and "Running now" is where a
        # running one is handled: the card only offers what can be done.
        self.play = self.play_hidden = self.setup = None
        if running or starting:
            pass
        elif not profile["build"]:
            if active:
                self.setup = action_button(
                    "Choose a version", page.window_.go_home, kind="Primary",
                    tip="Pick the Minecraft version this profile plays")
            else:
                self.setup = action_button(
                    "Set up", lambda: open_profile_window(path),
                    kind="Primary",
                    tip="Open this profile in a new window to sign in and "
                        "install Minecraft")
            self.flow.addWidget(self.setup)
        else:
            self.play = action_button(
                "Play", lambda: page.play(profile, hidden=False),
                kind="Primary", tip="Start Minecraft for this profile")
            self.play_hidden = action_button(
                "Play hidden", lambda: page.play(profile, hidden=True),
                tip="Start Minecraft with its window kept off the screen, "
                    "for a tool that drives the game. Show it any time from "
                    "\"Running now\"")
            self.flow.addWidget(self.play)
            self.flow.addWidget(self.play_hidden)
        if not active:
            self.switch = action_button(
                "Switch", lambda: page.window_._switch_profile_row(path),
                tip="Make this window use this profile")
            self.flow.addWidget(self.switch)

        self.more = QPushButton()
        self.more.setObjectName("MoreBtn")
        self.more.setIcon(icons.pixmap("more", theme.fg, 18))
        self.more.setIconSize(QSize(18, 18))
        self.more.setCursor(Qt.PointingHandCursor)
        self.more.setMinimumHeight(32)
        self.more.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.more.setToolTip("More: open in a new window, folder, rename, "
                             "delete")
        self.more.setAccessibleName(f"More actions for {profile['name']}")
        self.more.clicked.connect(self.open_menu)
        self.flow.addWidget(self.more)

    def menu_actions(self):
        """(label, slot, enabled, why not) for the secondary actions."""
        profile = self.profile
        page = self.page
        window = page.window_
        name, path = profile["name"], profile["path"]
        busy = bool(page.running_for(profile["home"]))
        entries = [
            ("Open in a new window", lambda: open_profile_window(path),
             True, ""),
            ("Open folder", lambda: page.open_folder(profile["home"]),
             True, ""),
        ]
        if path is not None:
            locked = "Close this profile's game first." if busy else ""
            entries.append((
                "Rename…",
                lambda: window._rename_profile_row(name, profile["active"]),
                not busy, locked))
            entries.append((
                "Delete…",
                lambda: window._delete_profile_row(name, profile["active"]),
                not busy and not profile["active"],
                locked or ("Switch to another profile before deleting "
                           "this one." if profile["active"] else "")))
        return entries

    def open_menu(self):
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        for label, slot, enabled, why in self.menu_actions():
            if label == "Rename…":
                menu.addSeparator()
            action = menu.addAction(label)
            action.setEnabled(enabled)
            if why:
                action.setToolTip(why)
            action.triggered.connect(lambda _checked=False, s=slot: s())
        menu.exec(self.more.mapToGlobal(QPoint(0, self.more.height() + 4)))


class ProfilesPage(QFrame):
    """The page itself; see the module docstring."""

    def __init__(self, window):
        super().__init__()
        self.setObjectName("Page")
        self.window_ = window
        self.closing = set()
        self._starting = {}     # profile home -> (Popen, monotonic start)
        self._failed = {}       # profile home -> message
        self._signature = None
        self._instances = []
        self._compact = False
        self.profile_cards = []
        self.instance_rows = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(12)
        self.intro = QLabel(
            "Each profile has its own Xbox sign-in, worlds and settings, and "
            "its own game: several profiles can play at the same time.")
        self.intro.setObjectName("Sub")
        self.intro.setWordWrap(True)
        head.addWidget(self.intro, 1)
        self.new_button = action_button(
            "New profile", window._prompt_create_profile_and_refresh,
            kind="Primary",
            tip="Add a profile with its own Xbox account, worlds and "
                "settings")
        self.new_button.setIcon(icons.pixmap("plus", "#ffffff", 16))
        head.addWidget(self.new_button, 0, Qt.AlignTop)
        outer.addLayout(head)

        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        self.area.setFrameShape(QFrame.NoFrame)
        self.area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.content = QWidget()
        self.column = QVBoxLayout(self.content)
        self.column.setContentsMargins(0, 0, 6, 0)
        self.column.setSpacing(10)
        self.area.setWidget(self.content)
        outer.addWidget(self.area, 1)

        self.timer = QTimer(self)
        self.timer.setInterval(REFRESH_MS)
        self.timer.timeout.connect(self.poll)

    # -- data ---------------------------------------------------------------
    def _profiles(self):
        active = current_profile_info().get("path")
        rows = [{"name": "Default", "path": None, "slug": "default"}]
        rows += [{"name": p.get("name", ""), "path": p.get("path"),
                  "slug": p.get("slug", "")} for p in list_profiles()]
        for row in rows:
            row["home"] = str(profile_home(row["path"]))
            if row["path"] is None:
                row["active"] = active is None
            else:
                row["active"] = active is not None and \
                    Path(active).resolve() == Path(row["path"]).resolve()
            try:
                row["account"] = profile_account(row["path"])
            except Exception:  # noqa: BLE001 - a card never fails the page
                row["account"] = {"signed_in": False, "gamertag": None}
            try:
                row["build"] = profile_build(row["path"])
            except Exception:  # noqa: BLE001
                row["build"] = None
        return rows

    def _running(self):
        try:
            return instances.list_instances()
        except Exception:  # noqa: BLE001 - an unreadable list is an empty one
            return []

    def running_for(self, home):
        return [item for item in self._instances
                if item.home and instances._same_path(item.home, home)]

    def _settle_starting(self):
        """Forget the launches that have shown up, failed or timed out."""
        now = time.monotonic()
        for home, (process, since) in list(self._starting.items()):
            if self.running_for(home):
                del self._starting[home]
            elif process.poll() is not None:
                del self._starting[home]
                if process.returncode:
                    self._failed[home] = (
                        "Minecraft did not start for this profile. Open it "
                        "in a new window and press PLAY there to see why.")
            elif now - since > START_TIMEOUT_S:
                del self._starting[home]

    # -- painting -----------------------------------------------------------
    def refresh(self, force=True):
        """Read everything again and rebuild the page if it changed."""
        self._instances = self._running()
        self.closing &= {item.id for item in self._instances}
        self._settle_starting()
        profiles = self._profiles()
        signature = (
            tuple((p["name"], p["path"], p["active"], p["build"],
                   p["account"]["signed_in"], p["account"]["gamertag"],
                   p["home"] in self._starting, self._failed.get(p["home"]))
                  for p in profiles),
            tuple((i.id, i.hidden, i.id in self.closing)
                  for i in self._instances),
            self.window_.theme.dark, self.window_.theme.beta,
        )
        if not force and signature == self._signature:
            for row in self.instance_rows:
                row.tick()
            return
        self._signature = signature
        self._rebuild(profiles)

    def poll(self):
        self.refresh(force=False)

    def _clear(self):
        while self.column.count():
            item = self.column.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self.profile_cards = []
        self.instance_rows = []

    def _section(self, title, count=None):
        row = QHBoxLayout()
        row.setSpacing(8)
        label = QLabel(title.upper())
        label.setObjectName("SectionTitle")
        row.addWidget(label)
        if count is not None:
            row.addWidget(badge(str(count), "quiet"))
        row.addStretch(1)
        holder = QWidget()
        holder.setLayout(row)
        row.setContentsMargins(2, 6, 0, 0)
        self.column.addWidget(holder)

    def _hint(self, title, text, button=None):
        box = QFrame()
        box.setObjectName("EmptyHint")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(4)
        head = QLabel(title)
        head.setObjectName("HintTitle")
        head.setWordWrap(True)
        layout.addWidget(head)
        body = QLabel(text)
        body.setObjectName("Sub")
        body.setWordWrap(True)
        layout.addWidget(body)
        if button is not None:
            line = QHBoxLayout()
            line.setContentsMargins(0, 8, 0, 0)
            line.addWidget(button)
            line.addStretch(1)
            layout.addLayout(line)
        self.column.addWidget(box)
        return box

    def _rebuild(self, profiles):
        self._clear()
        running = self._instances
        self._section("Running now", len(running))
        if running:
            for item in running:
                row = InstanceRow(self, item)
                self.instance_rows.append(row)
                self.column.addWidget(row)
        else:
            self.empty_running = self._hint(
                "No game is running",
                "Press Play on a profile below. \"Play hidden\" starts the "
                "game with no window on screen, for a tool that drives it; "
                "it can be shown here at any time.")

        self._section("Profiles", len(profiles))
        for profile in profiles:
            card = ProfileCard(
                self, profile, self.running_for(profile["home"]),
                profile["home"] in self._starting)
            self.profile_cards.append(card)
            self.column.addWidget(card)
            failed = self._failed.get(profile["home"])
            if failed:
                note = QLabel(failed)
                note.setObjectName("Warn")
                note.setWordWrap(True)
                self.column.addWidget(note)
        if len(profiles) == 1:
            self.empty_profiles = self._hint(
                "One profile so far",
                "Add a profile to play with a second Xbox account, to keep "
                "a separate set of worlds and settings, or to run two games "
                "side by side.",
                action_button(
                    "New profile",
                    self.window_._prompt_create_profile_and_refresh,
                    tip="Add a profile with its own Xbox account, worlds "
                        "and settings"))
        self.column.addStretch(1)
        self._apply_compact(force=True)

    def _apply_compact(self, force=False):
        compact = self.area.viewport().width() < COMPACT_BELOW
        if not force and compact == self._compact:
            return
        self._compact = compact
        for card in self.profile_cards + self.instance_rows:
            card.set_compact(compact)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_compact()

    def showEvent(self, event):
        super().showEvent(event)
        self.timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.timer.stop()

    # -- actions ------------------------------------------------------------
    def play(self, profile, hidden):
        """Start a profile's game: here for this window's own profile, in a
        launcher process of its own for any other."""
        window = self.window_
        self._failed.pop(profile["home"], None)
        if profile["active"]:
            window.go_home()
            window.do_play(hidden=hidden)
            return
        try:
            process = start_profile_game(profile["path"], hidden=hidden)
        except Exception as exc:  # noqa: BLE001 - shown, never raised
            window.error_box("Play", str(exc))
            return
        self._starting[profile["home"]] = (process, time.monotonic())
        self.refresh()

    def set_instance_hidden(self, instance, hidden):
        try:
            instances.set_hidden(instance, hidden)
        except Exception as exc:  # noqa: BLE001
            self.window_.error_box("Hide" if hidden else "Show", str(exc))
        self.refresh()

    def close_instance(self, instance):
        if instance.id in self.closing:
            return
        from .gui import Worker  # noqa: PLC0415 - gui imports this module
        worker = Worker(instances.close_instance, instance)
        worker.done.connect(lambda _result: self._closed(instance.id))
        worker.failed.connect(
            lambda message: self._close_failed(instance.id, message))
        if not self.window_._start_worker(f"close-{instance.id}", worker):
            return
        self.closing.add(instance.id)
        self.refresh()

    def _closed(self, instance_id):
        self.closing.discard(instance_id)
        self.refresh()

    def _close_failed(self, instance_id, message):
        self.closing.discard(instance_id)
        self.window_.error_box("Close", message)
        self.refresh()

    def open_folder(self, home):
        try:
            subprocess.Popen(["xdg-open", str(home)],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError as exc:
            self.window_.error_box("Open folder", str(exc))
