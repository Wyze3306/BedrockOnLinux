"""bol.gui — the desktop GUI (PySide6)."""
# SPDX-License-Identifier: MIT

from __future__ import annotations

import html
import inspect
import os
import re
import shutil
import socket
import stat
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import (
    QObject, QPoint, QPointF, Qt, QThread, QTimer, Signal, Slot,
)
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractButton, QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame,
    QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSpinBox, QStackedWidget, QTabWidget, QTextBrowser, QTextEdit,
    QToolButton, QVBoxLayout, QWidget,
)

from .auth import NativeAuth, msa_logout, msa_signed_in, msa_gamertag
from .config import (
    GAMES, LOGS, PRETTY, VERSION, get_install_location, clear_install_location,
    default_install_location, is_relocation_allowed,
)
from .relocation import migrate_data, paths_overlap, DIRS_TO_MOVE, FILES_TO_MOVE
from .content import game_content_dir, import_content
from .doctor import acknowledge_gpu_crash, gpu_crash_acknowledgement_status
from .games import installed_builds, list_editions, list_versions, remove_build
from .gamesetup import do_setup
from .inject import run_injector
from .launch import direct_launch_readiness, launch, single_window_session
from .navigation import ControllerNav
from . import log
from .log import BolError, _LEVELS, desktop_notify, warn
from .platform import IS_MAC, open_path
from .prefix import _mc_running, kill_wine, prefix_operation_lock, reset_prefix
from .profiles import (
    create_profile, current_profile_info, current_profile_name, delete_profile,
    list_profiles, play_launch_command, profile_launch_command,
    profile_shortcuts_supported, relaunch_with_profile, open_profile_window,
    rename_profile, require_profile_shortcuts_supported,
    require_shortcuts_supported, write_play_shortcut, write_profile_shortcut,
)
from .update import check_for_update, self_update
from .util import load_settings, save_settings, format_display_version, mc_releases, gh_releases

RE_MD_TOKENS = re.compile(r"(\*\*|`|__|\[[^\]]+\]\([^)]+\))")
RE_MD_LINK = re.compile(r"^\[([^\]]+)\]\(([^)]+)\)$")

try:
    import shiboken6
except ImportError:  # pragma: no cover - shiboken6 ships alongside PySide6
    shiboken6 = None

# QProgressBar counts in C++ ints, so a download measured in bytes gets a
# scale of its own and the byte counts stay in Python, where they fit. Fine
# enough for a bar: one step is a tenth of a percent.
BAR_STEPS = 1000


def _alive(widget) -> bool:
    """True if `widget`'s underlying C++ object hasn't been destroyed.

    Worker threads and QTimer.singleShot callbacks are asynchronous: they
    can still be pending when the window that owns them goes away (the
    window closed, or torn down between tests, while a background job is
    in flight). Without this guard the callback runs anyway and touches a
    widget whose C++ side is already gone, raising "libshiboken: Internal
    C++ object already deleted" from inside the Qt event loop.
    """
    if widget is None:
        return False
    if shiboken6 is None:
        return True
    return shiboken6.isValid(widget)


def _desktop_error(message: str) -> None:
    warn(message)
    desktop_notify(message)


# ======================================================================
# Stale-display (XWayland) recovery
#
# Under Wayland, $DISPLAY commonly points at an XWayland X11 socket that can
# go stale (e.g. XWayland restarts between login and launch). The old Tk GUI
# recovered by constructing CTk() and catching the resulting TclError, then
# retrying against another of the user's own X11 sockets.
#
# Qt's xcb platform plugin cannot be recovered the same way: on a failed
# server connection it logs "could not connect to display" and aborts the
# process natively, before control ever returns to Python -- there is no
# catchable exception to retry on. So this probes and repoints $DISPLAY
# *before* QApplication is ever constructed, by connecting directly to the
# candidate X11 sockets, rather than construct-and-catch.
# ======================================================================


def _owned_x11_socket_displays(socket_dir=None, uid=None):
    """Numeric-sorted (":N", ...) tuple of X11 sockets under socket_dir that
    are actually AF_UNIX sockets owned by uid (defaults to the current user
    and /tmp/.X11-unix)."""
    if socket_dir is None:
        socket_dir = Path("/tmp/.X11-unix")
    else:
        socket_dir = Path(socket_dir)
    if uid is None:
        uid = os.getuid()
    try:
        entries = list(socket_dir.iterdir())
    except OSError:
        return ()
    displays = []
    for entry in entries:
        name = entry.name
        if not name.startswith("X") or not name[1:].isdigit():
            continue
        try:
            st = entry.stat()
        except OSError:
            continue
        if not stat.S_ISSOCK(st.st_mode) or st.st_uid != uid:
            continue
        displays.append(int(name[1:]))
    return tuple(f":{n}" for n in sorted(displays))


def _x11_socket_is_live(socket_dir, display, timeout=0.5):
    """True if `display` (e.g. ':2') has a socket under socket_dir that
    actually accepts a connection -- a bound-but-unlistened or orphaned
    socket file is not enough."""
    if not display or not display.startswith(":"):
        return False
    num = display[1:].split(".", 1)[0]
    if not num.isdigit():
        return False
    path = Path(socket_dir) / f"X{num}"
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(timeout)
        probe.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _resolve_gui_display(environ=None, socket_dir=None, uid=None,
                          attempted=None):
    """Repoint environ['DISPLAY'] at a live, user-owned X11 socket before Qt
    ever tries to connect. Only probes when WAYLAND_DISPLAY is set (a pure
    X11 session has nothing more useful to recover to), and only moves off
    the current DISPLAY if it is not actually live. Returns the display
    string that should be used (also written back into environ when it
    changes); never raises."""
    if environ is None:
        environ = os.environ
    if socket_dir is None:
        socket_dir = Path("/tmp/.X11-unix")
    if uid is None:
        uid = os.getuid()
    current = environ.get("DISPLAY")
    if not environ.get("WAYLAND_DISPLAY"):
        return current
    if attempted is not None:
        attempted.append(current or "<unset>")
    if current and _x11_socket_is_live(socket_dir, current):
        return current
    for candidate in _owned_x11_socket_displays(socket_dir, uid=uid):
        if candidate == current:
            continue
        if attempted is not None:
            attempted.append(candidate)
        if _x11_socket_is_live(socket_dir, candidate):
            environ["DISPLAY"] = candidate
            return candidate
    return current


def icon_candidates(module_file=None):
    """Where data/icon.png can be, in the order to try.

    Every packaging layout puts it somewhere different, and one of them has
    no copy next to bol/ at all: the Flatpak installs only the themed icon
    under /app, so dropping that path leaves the window, the title bar and
    the hero screen with no icon at all on the Flathub build.
    """
    here = Path(module_file or __file__).resolve().parent
    return (
        # source checkout, AppImage (usr/bin/data), .deb and .rpm
        # (/usr/lib/bedrock-on-linux/data)
        here.parent / "data/icon.png",
        here / "data/icon.png",
        # macOS application bundle: Contents/Resources, beside Contents/MacOS
        here.parent.parent / "Resources/icon.png",
        # Flatpak: the manifest installs the icon under the app-id name only
        Path("/app/share/icons/hicolor/256x256/apps/"
             "io.github.wyze3306.BedrockOnLinux.png"),
        # system icon theme, for a distribution package that ships only that
        Path("/usr/share/icons/hicolor/256x256/apps/bedrock-on-linux.png"),
    )


# ======================================================================
# Theme
# ======================================================================

@dataclass
class Theme:
    """Palette used to generate the app's QSS."""
    dark: bool = True
    beta: bool = False

    def _pick(self, light, dark):
        return dark if self.dark else light

    @property
    def bg(self):        return self._pick("#eef1f6", "#0d0f14")
    @property
    def fg(self):         return self._pick("#12141a", "#eef1f6")
    @property
    def sub(self):        return self._pick("#5a6273", "#9198ab")
    @property
    def muted(self):      return self._pick("#8890a1", "#5a6273")
    @property
    def card(self):       return self._pick("#ffffff", "#161922")
    @property
    def card2(self):      return self._pick("#e6e9f0", "#1d212c")
    @property
    def card3(self):      return self._pick("#d6dae4", "#272c39")
    @property
    def border(self):     return self._pick("#cdd2de", "#2a2f3d")
    @property
    def red(self):        return "#e0574a"
    @property
    def red_hov(self):    return "#c94b3f"
    @property
    def green(self):      return self._pick("#43a047", "#43a047")
    @property
    def green_hov(self):  return self._pick("#3b8e3f", "#4fc153")
    @property
    def green_dim(self):  return self._pick("#e6f4e6", "#1c2c1c")
    @property
    def gold(self):       return self._pick("#d8a230", "#e3b34a")
    @property
    def gold_hov(self):   return self._pick("#c2912a", "#f3c35a")
    @property
    def gold_dim(self):   return self._pick("#fcf3e1", "#33291a")
    @property
    def blue(self):       return "#4a90d9"
    @property
    def blue_dim(self):   return self._pick("#e7f1fb", "#132433")
    @property
    def accent(self):     return self.gold if self.beta else self.green
    @property
    def accent_hov(self): return self.gold_hov if self.beta else self.green_hov
    @property
    def accent_dim(self): return self.gold_dim if self.beta else self.green_dim
    @property
    def console_bg(self): return self._pick("#f7f9fb", "#0a0c10")
    @property
    def console_fg(self): return self._pick("#2f9a5c", "#7fe0a0")

    def qss(self) -> str:
        return f"""
        QWidget {{
            background: transparent;
            color: {self.fg};
            font-family: -apple-system, "Segoe UI", "Inter", sans-serif;
            font-size: 13px;
        }}
        QMainWindow, #Root {{ background: {self.bg}; }}
        QDialog {{
            background: {self.bg};
            border: 1px solid {self.border};
            border-radius: 14px;
        }}
        QDialog QFrame#Card {{
            background: {self.card};
            border: 1px solid {self.border};
            border-radius: 16px;
        }}
        QFrame#Card {{
            background: {self.card};
            border: 1px solid {self.border};
            border-radius: 18px;
        }}
        QFrame#CardFlat {{
            background: {self.card};
            border: 1px solid {self.border};
            border-radius: 14px;
        }}
        QFrame#Pill {{
            background: {self.card2};
            border-radius: 14px;
        }}
        QFrame#PillOnCard {{
            background: {self.card2};
            border-radius: 12px;
        }}
        QLabel#Title {{ font-size: 16px; font-weight: 700; }}
        QLabel#Sub {{ color: {self.sub}; }}
        QLabel#Muted {{ color: {self.muted}; font-size: 11px; }}
        QLabel#Hero {{ font-size: 26px; font-weight: 700; }}
        QLabel#Chip {{
            color: {self.accent};
            background: {self.accent_dim};
            border-radius: 9px;
            padding: 4px 12px;
            font-weight: 700;
        }}
        QPushButton {{
            border: none;
            border-radius: 10px;
            padding: 6px 14px;
            background: {self.card2};
            color: {self.fg};
        }}
        QPushButton:hover {{ background: {self.card3}; }}
        QPushButton#Play {{
            background: {self.accent};
            color: white;
            font-weight: 700;
            font-size: 15px;
            border-radius: 12px;
        }}
        QPushButton#Play:hover {{ background: {self.accent_hov}; }}
        QPushButton#Kill {{
            background: {self.red};
            color: white;
            font-weight: 700;
            font-size: 15px;
            border-radius: 12px;
        }}
        QPushButton#Kill:hover {{ background: {self.red_hov}; }}
        QPushButton#Primary {{
            background: {self.accent};
            color: white;
            font-weight: 700;
        }}
        QPushButton#Primary:hover {{ background: {self.accent_hov}; }}
        QPushButton#Danger {{ background: {self.red}; color: white; }}
        QPushButton#Danger:hover {{ background: {self.red_hov}; }}
        QPushButton#Ghost {{ background: transparent; color: {self.sub}; }}
        QPushButton#Ghost:hover {{ background: {self.card2}; color: {self.fg}; }}
        QPushButton#GhostSmall {{ background: transparent; color: {self.sub}; font-size: 11px; padding: 4px 8px; }}
        QPushButton#GhostSmall:hover {{ background: {self.card2}; color: {self.fg}; }}
        QPushButton#DangerSmall {{ background: {self.card2}; color: {self.red}; font-size: 11px; padding: 4px 8px; border-radius: 10px; }}
        QPushButton#DangerSmall:hover {{ background: {self.red}; color: white; }}
        QPushButton#IconBtn {{
            background: {self.card2};
            border-radius: 8px;
        }}
        QPushButton#IconBtn:hover {{ background: {self.card3}; }}
        QPushButton#ToolRow {{
            background: {self.card2};
            border-radius: 10px;
            padding: 10px 14px;
            text-align: left;
            font-size: 13px;
            font-weight: 600;
        }}
        QPushButton#ToolRow:hover {{ background: {self.card3}; }}
        QPushButton#ToolRowDanger {{
            background: {self.card2};
            color: {self.red};
            border-radius: 10px;
            padding: 10px 14px;
            text-align: left;
            font-size: 13px;
            font-weight: 600;
        }}
        QPushButton#ToolRowDanger:hover {{ background: {self.red}; color: white; }}
        QPushButton#Toggle {{
            background: transparent;
            color: {self.sub};
            border-radius: 8px;
            font-weight: 700;
        }}
        QPushButton#Toggle:checked {{
            background: {self.accent_dim};
            color: {self.accent};
        }}
        QLineEdit, QPlainTextEdit {{
            background: {self.card2};
            border: 1px solid transparent;
            border-radius: 10px;
            padding: 6px 10px;
            selection-background-color: {self.accent};
        }}
        QLineEdit:focus {{ border: 1px solid {self.accent}; }}
        QListWidget {{
            background: {self.card2};
            border: none;
            border-radius: 8px;
            outline: none;
        }}
        QListWidget::item {{
            padding: 6px 10px;
            border-radius: 6px;
        }}
        QListWidget::item:selected {{
            background: {self.accent_dim};
            color: {self.accent};
        }}
        QListWidget::item:hover {{ background: {self.card3}; }}
        QProgressBar {{
            background: {self.card2};
            border-radius: 4px;
            height: 8px;
            text-align: center;
            color: transparent;
        }}
        QProgressBar::chunk {{ background: {self.accent}; border-radius: 4px; }}
        QTabWidget::pane {{
            border: none;
            background: {self.bg};
            border-radius: 12px;
            top: 4px;
        }}
        QTabBar {{ background: transparent; }}
        QTabBar::tab {{
            background: {self.card2};
            color: {self.sub};
            padding: 8px 18px;
            border-radius: 8px;
            margin: 4px 3px 4px 0px;
            font-weight: 600;
        }}
        QTabBar::tab:hover {{ background: {self.card3}; color: {self.fg}; }}
        QTabBar::tab:selected {{ background: {self.accent}; color: white; }}
        QScrollArea, QScrollArea > QWidget > QWidget {{ border: none; background: transparent; }}
        QScrollBar:vertical {{ width: 10px; background: transparent; }}
        QScrollBar::handle:vertical {{
            background: {self.card3}; border-radius: 5px; min-height: 24px;
        }}
        QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
        QTextBrowser {{
            background: {self.card2};
            border-radius: 12px;
            border: none;
        }}
        QMessageBox {{ background: {self.card}; }}
        /* Qt draws a message box's checkbox with the platform style, which on
           a dark palette comes out as an unlit square nobody can see they are
           allowed to tick. */
        QMessageBox QCheckBox {{ color: {self.sub}; }}
        QMessageBox QCheckBox::indicator {{
            width: 14px; height: 14px;
            border: 1px solid {self.border};
            border-radius: 4px;
            background: {self.card2};
        }}
        QMessageBox QCheckBox::indicator:checked {{
            background: {self.accent};
            border-color: {self.accent};
        }}
        #ActivityLog QTextEdit {{
            background: {self.console_bg};
            color: {self.console_fg};
            font-family: monospace;
            border-radius: 12px;
        }}
        #Popup {{
            background: {self.card2};
            border: 1px solid {self.border};
            border-radius: 12px;
        }}
        """


# ======================================================================
# Background workers
# ======================================================================

class Worker(QThread):
    """Run an arbitrary callable off the UI thread."""
    done = Signal(object)
    failed = Signal(str)
    # Byte counts, so qint64 rather than int: a Qt `int` is the C++ one, and
    # PySide6 wraps anything past 2 GiB into it with nothing but a
    # RuntimeWarning. See MainWindow.set_progress for what that cost (#216).
    progress = Signal("qint64", "qint64")

    def __init__(self, fn: Callable, *args, **kwargs):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def _takes_progress(self):
        """Whether the callable accepts a `progress` keyword.

        Read from the signature, not from __code__.co_varnames: co_varnames
        lists local variables as well as parameters, so a callee that merely
        assigns to a name called `progress` would be handed a keyword it
        cannot take -- and the resulting TypeError arrives through failed(),
        where it reads as a real failure of the work itself. Builtins and
        functools.partial have no __code__ at all.
        """
        try:
            parameters = inspect.signature(self._fn).parameters
        except (TypeError, ValueError):
            return False
        if "progress" in parameters:
            return True
        return any(p.kind is inspect.Parameter.VAR_KEYWORD
                   for p in parameters.values())

    def run(self):
        try:
            kwargs = dict(self._kwargs)
            if self._takes_progress():
                kwargs["progress"] = self._emit_progress
            self.done.emit(self._fn(*self._args, **kwargs))
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            self.failed.emit(str(exc) or type(exc).__name__)

    def _emit_progress(self, got, total):
        self.progress.emit(got, total)


class LogBridge(QObject):
    """Marshals ``log._LOG_SINK`` calls (any thread) onto the UI thread."""
    line = Signal(str)


class AuthBridge(QObject):
    """Marshals ``NativeAuth`` callbacks (raw worker thread) onto the UI
    thread.

    ``NativeAuth.start()`` is kicked off on a plain ``threading.Thread``, not
    a ``QThread``, so it has no Qt event loop of its own. Handing its
    ``on_auth``/``on_online`` callbacks straight to ``QTimer.singleShot``
    from that thread creates a timer with affinity to a thread that never
    pumps events -- the callback is queued and then simply never fires, so
    the "Sign In" dialog never appears and the account never goes green.
    A ``QObject`` created on the UI thread queues its signals onto that
    thread automatically, from any emitting thread, which is what actually
    gets the callback to run.
    """
    auth = Signal(str, str)
    online = Signal()
    refreshed = Signal()


# ======================================================================
# Small reusable widgets
# ======================================================================

class Tooltip:
    """Thin wrapper so call sites read the same as the old ``explain()``."""
    def __init__(self, widget: QWidget, text: str):
        widget.setToolTip(text)
        self.widget = widget

    @property
    def text(self):
        return self.widget.toolTip()

    @text.setter
    def text(self, value):
        self.widget.setToolTip(value)


def btn(text, cmd=None, kind="ghost", w=None, h=32, tip=None, parent=None) -> QPushButton:
    b = QPushButton(text, parent)
    b.setObjectName({
        "play": "Play", "primary": "Primary", "danger": "Danger",
        "ghost": "Ghost", "flat": "Ghost", "icon": "IconBtn",
        "ghost-small": "GhostSmall", "danger-small": "DangerSmall",
        "toolrow": "ToolRow", "toolrow-danger": "ToolRowDanger",
    }.get(kind, "Ghost"))
    if cmd:
        b.clicked.connect(cmd)
    if w:
        b.setFixedWidth(w)
    b.setFixedHeight(h)
    b.setCursor(Qt.PointingHandCursor)
    if tip:
        b.setToolTip(tip)
    return b


def tool_row(text, cmd, tip=None, danger=False) -> QPushButton:
    """A full-width, left-aligned action row for Settings ▸ Tools."""
    return btn(text, cmd, kind="toolrow-danger" if danger else "toolrow", h=44, tip=tip)


def card_section(parent_layout, title, desc=None) -> QVBoxLayout:
    """A titled settings card, mirroring ``_settings_card`` from the Tk GUI."""
    card = QFrame()
    card.setObjectName("CardFlat")
    v = QVBoxLayout(card)
    v.setContentsMargins(16, 14, 16, 14)
    v.setSpacing(6)
    head = QLabel(title)
    head.setObjectName("Title")
    head.setStyleSheet("font-size:13px;")
    v.addWidget(head)
    if desc:
        d = QLabel(desc)
        d.setObjectName("Sub")
        d.setWordWrap(True)
        d.setStyleSheet("font-size:11px;")
        v.addWidget(d)
    body = QVBoxLayout()
    body.setSpacing(8)
    v.addLayout(body)
    parent_layout.addWidget(card)
    return body


class ToggleSwitch(QAbstractButton):
    """A painted pill-and-knob switch."""

    def __init__(self, theme: "Theme", parent=None):
        super().__init__(parent)
        self._theme = theme
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(42, 24)

    def set_theme(self, theme: "Theme"):
        self._theme = theme
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(1, 1, -1, -1)
        on = self.isChecked()
        track = QColor(self._theme.accent if on else self._theme.card3)
        p.setPen(Qt.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        knob_d = rect.height() - 4
        x = rect.right() - knob_d - 2 if on else rect.left() + 2
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(x, rect.top() + 2, knob_d, knob_d)


class SwitchRow(QWidget):
    """A labelled toggle row used everywhere in Settings."""
    toggled = Signal(bool)

    def __init__(self, text, checked=False, tip=None, theme: Optional["Theme"] = None):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        label = QLabel(text)
        lay.addWidget(label)
        lay.addStretch(1)
        self.switch = ToggleSwitch(theme or Theme())
        self.switch.setChecked(checked)
        self.switch.toggled.connect(self.toggled)
        lay.addWidget(self.switch)
        if tip:
            self.setToolTip(tip)
            label.setToolTip(tip)
            self.switch.setToolTip(tip)

    def isChecked(self):
        return self.switch.isChecked()


class Popup(QFrame):
    """A borderless floating panel positioned relative to an anchor widget."""

    def __init__(self, parent, width=260, height=300):
        super().__init__(parent, Qt.Popup)
        self.setObjectName("Popup")
        self.setFixedSize(width, height)
        self.setAttribute(Qt.WA_TranslucentBackground, False)

    def show_below(self, anchor: QWidget, gap=4):
        pos = anchor.mapToGlobal(QPoint(0, anchor.height() + gap))
        self.move(pos)
        self.show()

    def show_above(self, anchor: QWidget, gap=4):
        pos = anchor.mapToGlobal(QPoint(0, -self.height() - gap))
        self.move(pos)
        self.show()


# ======================================================================
# Version picker
# ======================================================================

class VersionPicker(Popup):
    picked = Signal(str)

    def __init__(self, parent):
        super().__init__(parent, width=260, height=320)
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter versions…")
        v.addWidget(self.search)
        self.list = QListWidget()
        v.addWidget(self.list)
        self.search.textChanged.connect(self._filter)
        self.list.itemClicked.connect(lambda it: self.picked.emit(it.text()))
        self._labels = []

    def set_labels(self, labels, current):
        self._labels = labels
        self.list.clear()
        for lab in labels:
            it = QListWidgetItem(lab)
            self.list.addItem(it)
            if lab == current:
                self.list.setCurrentItem(it)
        self.search.clear()

    def showEvent(self, e):
        super().showEvent(e)
        # A Qt::Popup takes focus itself, so without this the filter field
        # ignores everything typed until it is clicked.
        self.search.setFocus(Qt.PopupFocusReason)

    def _filter(self, text):
        text = text.strip().lower()
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setHidden(bool(text) and text not in it.text().lower())

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            # The highlighted row wins when there is one: arrowing down the
            # list (or walking it with a controller) and pressing Enter has
            # to pick what is highlighted, not the first row that survived
            # the filter.
            current = self.list.currentItem()
            if current is not None and not current.isHidden():
                self.picked.emit(current.text())
                return
            for i in range(self.list.count()):
                it = self.list.item(i)
                if not it.isHidden():
                    self.picked.emit(it.text())
                    return
        elif e.key() == Qt.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(e)


class ProfileMenu(Popup):
    switch = Signal(object)   # profile path or None for default
    new_window = Signal(object)
    create_profile = Signal()
    manage = Signal()

    def __init__(self, parent):
        super().__init__(parent, width=260, height=260)
        self._v = QVBoxLayout(self)
        self._v.setContentsMargins(6, 6, 6, 6)
        self._v.setSpacing(2)

    def rebuild(self, profiles, active_path):
        # setParent(None) as well as deleteLater(): deleteLater only schedules
        # the destruction, so until the event loop next turns, the old rows are
        # still children of this popup -- connected to the same signals, and
        # findable. rebuild() runs immediately before show(), so detaching now
        # is what makes the menu on screen the menu that was just built.
        while self._v.count():
            item = self._v.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        def add_row(name, path, active):
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            b = btn(name, lambda: self.switch.emit(path), kind="ghost", h=30)
            b.setStyleSheet("text-align:left;" + ("font-weight:700;color:%s;" % "#37b06b" if active else ""))
            h.addWidget(b, 1)
            wb = btn("New Win", lambda: self.new_window.emit(path), kind="ghost", w=64, h=30,
                     tip=f"Open {name} in a new window")
            h.addWidget(wb)
            self._v.addWidget(row)

        add_row("Default", None, active_path is None)
        for p in profiles:
            path = p.get("path")
            is_active = active_path is not None and str(Path(active_path).resolve()) == str(Path(path).resolve())
            add_row(p.get("name", ""), path, is_active)

        div = QFrame()
        div.setFixedHeight(1)
        div.setStyleSheet("background:#5a6273;")
        self._v.addWidget(div)
        self._v.addWidget(btn("+ New Profile…", lambda: self.create_profile.emit(), kind="ghost", h=30))
        self._v.addWidget(btn("Manage Profiles…", lambda: self.manage.emit(), kind="ghost", h=30))
        self._v.addStretch(1)



# ======================================================================
# Accounts menu
# ======================================================================

STORE_LINK_EXPLAINER = (
    "The Microsoft Store hands the game to the account that owns it, over a "
    "device-bound session the in-game sign-in cannot stand in for. So the "
    "launcher asks twice, once for each job. Use the same Microsoft account "
    "for both."
)

OFFLINE_PLAY_EXPLAINER = (
    "Minecraft will start, but in offline mode: no Realms, no servers, no "
    "friends and no Marketplace. Signing in for online play is a separate "
    "step from the sign-in that downloaded the game — the launcher never "
    "does it on its own, because playing offline is a legitimate thing to "
    "want."
)


class AccountRow(QWidget):
    """One account inside the menu: what it is for, where it stands, and the
    one thing you can do about it."""
    acted = Signal()

    def __init__(self, title, purpose, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(2)

        head = QHBoxLayout()
        head.setSpacing(6)
        self.dot = QLabel("●")
        head.addWidget(self.dot)
        name = QLabel(title)
        name.setStyleSheet("font-weight:700;")
        head.addWidget(name)
        head.addStretch(1)
        self.button = btn("Sign in", self.acted.emit, kind="ghost", w=92, h=28)
        head.addWidget(self.button)
        v.addLayout(head)

        self.status = QLabel("")
        self.status.setObjectName("Sub")
        v.addWidget(self.status)

        why = QLabel(purpose)
        why.setObjectName("Muted")
        why.setWordWrap(True)
        why.setStyleSheet("font-size:11px;")
        v.addWidget(why)

    def show_state(self, dot_color, status, action, danger=False):
        self.dot.setStyleSheet(f"color:{dot_color};")
        self.status.setText(status)
        # No action means there is nothing this row can do here -- the Store
        # download on macOS, where the downloader itself does not exist. Hide
        # the button rather than leave a labelless one to be clicked.
        self.button.setVisible(bool(action))
        self.button.setText(action or "")
        self.button.setStyleSheet(
            f"background:{danger}; color:white;" if danger else "")


class AccountsMenu(Popup):
    """Both sign-ins in one place.

    They were in two: the in-game one at the top of the window, the download's
    buried in Settings, with nothing anywhere saying they were different
    accounts at all. A player who had signed in once met "you have to sign in"
    at PLAY with nothing to click.

    Being a Qt::Popup also settles the confirmation problem the top-bar button
    had. "Sign out?" is a question, and a question left armed on screen is one
    the next stray click answers; a popup closes on any click outside itself,
    so hiding is the moment to withdraw it.
    """
    xbox = Signal()
    store = Signal()

    def __init__(self, parent):
        super().__init__(parent, width=352, height=272)
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(2)

        self.online = AccountRow(
            "Play online",
            "Friends, servers and Realms, from inside the game.")
        self.online.acted.connect(self.xbox)
        v.addWidget(self.online)

        self.divider = QFrame()
        self.divider.setFixedHeight(1)
        v.addWidget(self.divider)

        self.download = AccountRow(
            "Download Minecraft",
            "Fetches and updates the game from the Microsoft Store.")
        self.download.acted.connect(self.store)
        v.addWidget(self.download)

        note = QLabel(STORE_LINK_EXPLAINER)
        note.setObjectName("Muted")
        note.setWordWrap(True)
        note.setStyleSheet("font-size:10px; padding:6px 10px 0 10px;")
        v.addWidget(note)
        v.addStretch(1)

    def set_theme(self, theme: "Theme"):
        self.divider.setStyleSheet(f"background:{theme.border};")

    def hideEvent(self, event):
        # Withdraw any armed confirmation with the menu that was asking it.
        self.parent().disarm_account_confirms()
        super().hideEvent(event)


class GearButton(QAbstractButton):
    """A drawn settings-gear icon (painted, not a font glyph/emoji)."""

    def __init__(self, theme: "Theme", parent=None):
        super().__init__(parent)
        self._theme = theme
        self.setFixedSize(52, 52)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)
        self.setToolTip("Settings")

    def set_theme(self, theme: "Theme"):
        self._theme = theme
        self.update()

    def paintEvent(self, _event):
        import math
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()
        bg = QColor(self._theme.card3 if self.underMouse() else self._theme.card2)
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 12, 12)

        cx, cy = rect.center().x(), rect.center().y()
        outer_r, inner_r, hole_r, teeth = 11.5, 8.0, 4.2, 8
        p.setBrush(QColor(self._theme.fg))
        points = [
            QPointF(cx + (outer_r if i % 2 == 0 else inner_r) * math.cos(math.pi * i / teeth),
                    cy + (outer_r if i % 2 == 0 else inner_r) * math.sin(math.pi * i / teeth))
            for i in range(teeth * 2)
        ]
        p.drawPolygon(points)
        p.setBrush(bg)
        p.drawEllipse(QPointF(cx, cy), hole_r, hole_r)


# ======================================================================
# Main window
# ======================================================================

def window_action_for_launch(settings, single_window):
    if (settings or {}).get("close_on_launch", False):
        return "close"
    return "step-aside" if single_window else "stay"


class LaunchWorker(QThread):
    """Runs setup + launch, and tells the UI what to do to its own window
    (close it, step aside for a single-window session, come back) once the
    game process actually exists."""
    done = Signal(object)
    failed = Signal(str)
    # Nothing is broken and nothing was downloaded: the game simply has no
    # account to be fetched with. That is an offer, not a launch failure, so
    # it is reported apart from failed() rather than being recovered by
    # matching on the message text.
    needs_store_signin = Signal(str)
    # The download was refused because the account already holds its ten
    # Microsoft Store devices. Also not a launch failure to sit and read:
    # the remedy is a web page, and this is what lets the window open it.
    store_device_limit = Signal(str)
    # qint64: this is the signal that carries the Minecraft download, and that
    # package is well past the 2 GiB a Qt `int` holds (#216).
    progress = Signal("qint64", "qint64")
    close_window = Signal()
    step_aside = Signal()
    come_back = Signal()

    def __init__(self, ver):
        super().__init__()
        self._ver = ver

    def run(self):
        try:
            do_setup(mc_edition=self._ver["edition"], mc_version=self._ver["tag"],
                      progress=lambda g, t: self.progress.emit(g, t))
            action = window_action_for_launch(load_settings(), single_window_session())

            def on_started():
                if action == "close":
                    self.close_window.emit()
                elif action == "step-aside":
                    self.step_aside.emit()

            try:
                launch(on_started=on_started)
            finally:
                self.come_back.emit()
            self.done.emit("closed")
        except Exception as exc:
            self.come_back.emit()
            message = str(exc) or type(exc).__name__
            from .xodus import DeviceLimitReached, NotSignedIn
            if isinstance(exc, NotSignedIn):
                self.needs_store_signin.emit(message)
            elif isinstance(exc, DeviceLimitReached):
                self.store_device_limit.emit(message)
            else:
                self.failed.emit(message)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.theme = Theme(dark=not self.settings.get("light_theme", False),
                            beta=self.settings.get("ui_is_beta", False))

        self.ui_state = {
            "versions": [], "labels": [], "busy": False, "details": False,
            "launch_active": False, "window_gone": False, "stepped_aside": False,
        }
        self._force_close = False
        # Armed two-step confirmations, read by _online_state /
        # _store_state. Set here because _build_settings() refreshes the
        # store row before the accounts menu is wired.
        self._acct_confirm = False
        self._store_confirm = False
        # slot name -> the QThread currently held for it; see _start_worker.
        self._workers: dict[str, QThread] = {}
        self.na = NativeAuth()
        self._switches: list[SwitchRow] = []
        # Controller navigation (bol.navigation): built once every widget the
        # ring can land on exists, at the end of __init__.
        self.nav: Optional[ControllerNav] = None
        self._nav_devices: tuple = ()
        self._log_bridge = LogBridge()
        self._log_bridge.line.connect(self._on_log_line)
        log._LOG_SINK = lambda m: self._log_bridge.line.emit(m)
        # See AuthBridge: NativeAuth's callbacks fire on a plain worker
        # thread with no event loop, so they cannot drive QTimer.singleShot
        # or touch widgets directly. Route them through a QObject built on
        # the UI thread instead, whose queued-connection signals land here
        # safely no matter which thread emits them.
        self._auth_bridge = AuthBridge()
        self._auth_bridge.auth.connect(self._on_auth)
        self._auth_bridge.online.connect(self._on_online)
        self._auth_bridge.refreshed.connect(self._on_refreshed)

        self.setWindowTitle(PRETTY)
        self.resize(1000, 660)
        self.setMinimumSize(880, 640)

        self._load_icon()

        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(22, 18, 22, 16)
        outer.setSpacing(8)

        outer.addLayout(self._build_topbar())

        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)
        self.hero_page = self._build_hero()
        self.settings_page = self._build_settings()
        self.changelog_page = self._build_changelog()
        self.profiles_page = self._build_profiles_page()
        self.stack.addWidget(self.hero_page)
        self.stack.addWidget(self.settings_page)
        self.stack.addWidget(self.changelog_page)
        self.stack.addWidget(self.profiles_page)
        self.stack.setCurrentWidget(self.hero_page)

        self.status_row = self._build_status_row()
        outer.addLayout(self.status_row)

        outer.addWidget(self._build_dock())

        self.log_drawer = self._build_log_drawer()
        outer.addWidget(self.log_drawer)
        self.log_drawer.hide()

        self.apply_theme()
        self._wire_version_picker()
        self._wire_profile_menu()
        self._wire_accounts_menu()
        self._refresh_account_row("in" if msa_signed_in() else "out")

        self._changelog_loaded = False

        QTimer.singleShot(50, self.refresh_versions)
        QTimer.singleShot(200, self.check_for_update_async)

        if self.settings.get("show_changelog_on_startup", False):
            QTimer.singleShot(0, self.toggle_changelog)

        # BOL_CONTROLLER=0 turns navigation off for one session without
        # touching the saved setting.
        if (self.settings.get("controller_nav", True)
                and os.environ.get("BOL_CONTROLLER") != "0"):
            self.apply_controller_nav(True)

    def _start_worker(self, slot, worker) -> bool:
        """Start `worker`, holding the only reference the GUI thread keeps.

        A QThread whose last Python reference goes away is destroyed by the
        C++ side while it is still running, which Qt reports as "QThread:
        Destroyed while thread is still running" and then aborts on. Every
        background job here was stored in a plain attribute, so triggering
        the same action twice -- two clicks on Import, a quick Stable/Preview
        toggle -- overwrote a running thread with its successor.

        Slots are never emptied, only replaced once idle: dropping the
        reference from finished() would put it back in the same race it
        exists to prevent. Returns False when the slot is still busy, which
        is also how repeat clicks are refused.
        """
        previous = self._workers.get(slot)
        if previous is not None and previous.isRunning():
            return False
        self._workers[slot] = worker
        worker.start()
        return True

    # ------------------------------------------------------------ icon
    def _load_icon(self):
        for candidate in icon_candidates():
            if candidate.exists():
                self.icon_pixmap = QPixmap(str(candidate))
                self.setWindowIcon(QIcon(self.icon_pixmap))
                return
        self.icon_pixmap = None

    # ------------------------------------------------------------ theme
    def _switch(self, text, checked=False, tip=None) -> SwitchRow:
        """Themed toggle row; tracked so a later theme change repaints it."""
        row = SwitchRow(text, checked, tip, theme=self.theme)
        self._switches.append(row)
        return row

    def apply_theme(self):
        self.theme.beta = self.settings.get("ui_is_beta", False)
        self.theme.dark = not self.settings.get("light_theme", False)
        QApplication.instance().setStyleSheet(self.theme.qss())
        self._paint_edition_toggle()
        for row in getattr(self, "_switches", ()):
            row.switch.set_theme(self.theme)
        if getattr(self, "settings_btn", None):
            self.settings_btn.set_theme(self.theme)
        if getattr(self, "nav", None) is not None:
            self.nav.set_accent(self.theme.accent)
        if _alive(getattr(self, "accounts_menu", None)):
            self.accounts_menu.set_theme(self.theme)
            self._refresh_accounts()

    # ------------------------------------------------------------ top bar
    def _build_topbar(self) -> QHBoxLayout:
        row = QHBoxLayout()

        brand = QFrame(); brand.setObjectName("Pill")
        bl = QHBoxLayout(brand); bl.setContentsMargins(10, 6, 10, 6)
        icon_lbl = QLabel()
        if self.icon_pixmap:
            icon_lbl.setPixmap(self.icon_pixmap.scaled(24, 24, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        bl.addWidget(icon_lbl)
        whats_new = QToolButton(); whats_new.setText("What's New")
        whats_new.setCursor(Qt.PointingHandCursor)
        whats_new.setStyleSheet("font-weight:700; border:none; background:transparent;")
        whats_new.clicked.connect(self.toggle_changelog)
        bl.addWidget(whats_new)
        ver_lbl = QLabel(f"v{VERSION}"); ver_lbl.setObjectName("Sub")
        bl.addWidget(ver_lbl)
        gh_btn = btn("GitHub", self._open_github, kind="ghost", h=26)
        bl.addWidget(gh_btn)
        row.addWidget(brand)
        row.addStretch(1)

        # Profile switcher pill
        self.prof_card = QFrame(); self.prof_card.setObjectName("Pill")
        self.prof_card.setCursor(Qt.PointingHandCursor)
        pl = QHBoxLayout(self.prof_card); pl.setContentsMargins(14, 6, 10, 6)
        self.prof_label = QLabel(f"Profile: {current_profile_name()}")
        pl.addWidget(self.prof_label)
        pl.addWidget(QLabel("▾"))
        self.prof_card.mousePressEvent = lambda e: self.open_profile_menu()
        row.addWidget(self.prof_card)

        # Accounts pill -- opens the menu holding both sign-ins
        self.acct_card = QFrame(); self.acct_card.setObjectName("Pill")
        self.acct_card.setCursor(Qt.PointingHandCursor)
        al = QHBoxLayout(self.acct_card); al.setContentsMargins(14, 6, 10, 6)
        self.acct_dot = QLabel("●")
        al.addWidget(self.acct_dot)
        self.acct_text = QLabel("Sign in")
        al.addWidget(self.acct_text)
        al.addWidget(QLabel("▾"))
        self.acct_card.mousePressEvent = lambda e: self.open_accounts_menu()
        row.addWidget(self.acct_card)

        self.update_banner_slot = row
        return row

    def _open_github(self):
        open_path("https://github.com/Wyze3306/BedrockOnLinux")

    # ------------------------------------------------------------ hero
    def _build_hero(self) -> QWidget:
        card = QFrame(); card.setObjectName("Card")
        v = QVBoxLayout(card)
        v.setAlignment(Qt.AlignCenter)
        if self.icon_pixmap:
            lbl = QLabel()
            lbl.setPixmap(self.icon_pixmap.scaled(118, 118, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            lbl.setAlignment(Qt.AlignCenter)
            v.addWidget(lbl)
        title = QLabel("Minecraft Bedrock"); title.setObjectName("Hero")
        title.setAlignment(Qt.AlignCenter)
        v.addWidget(title)
        sub = QLabel("Bedrock Edition for Linux"); sub.setObjectName("Sub")
        sub.setAlignment(Qt.AlignCenter)
        v.addWidget(sub)
        self.selected_chip = QLabel(""); self.selected_chip.setObjectName("Chip")
        self.selected_chip.setAlignment(Qt.AlignCenter)
        v.addWidget(self.selected_chip, 0, Qt.AlignCenter)
        return card

    # ------------------------------------------------------------ status row
    def _build_status_row(self) -> QVBoxLayout:
        col = QVBoxLayout()
        line = QHBoxLayout()
        self.status_label = QLabel("Ready to play.")
        self.status_label.setObjectName("Sub")
        line.addWidget(self.status_label)
        line.addStretch(1)
        # Which button does what, shown only while a controller is connected.
        # The names are spelled out rather than drawn as circled glyphs: the
        # Ⓐ/Ⓑ characters are missing from most Linux default fonts and would
        # come out as boxes.
        self.nav_legend = QLabel()
        self.nav_legend.setObjectName("Muted")
        self.nav_legend.setTextFormat(Qt.RichText)
        self.nav_legend.hide()
        line.addWidget(self.nav_legend)
        col.addLayout(line)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.hide()
        col.addWidget(self.progress)
        return col

    # ------------------------------------------------------------ dock
    def _build_dock(self) -> QFrame:
        dock = QFrame(); dock.setObjectName("Card")
        h = QHBoxLayout(dock)
        h.setContentsMargins(16, 14, 16, 14)

        # Version field
        self.ver_field = QFrame(); self.ver_field.setObjectName("PillOnCard")
        self.ver_field.setFixedSize(220, 52)
        self.ver_field.setCursor(Qt.PointingHandCursor)
        vfl = QHBoxLayout(self.ver_field); vfl.setContentsMargins(14, 0, 12, 0)
        self.ver_label = QLabel("Loading…")
        vfl.addWidget(self.ver_label, 1)
        vfl.addWidget(QLabel("▾"))
        self.ver_field.mousePressEvent = lambda e: self.open_picker()
        h.addWidget(self.ver_field)

        # Edition toggle
        ed_field = QFrame(); ed_field.setObjectName("PillOnCard")
        ed_field.setFixedHeight(52)
        efl = QHBoxLayout(ed_field); efl.setContentsMargins(6, 8, 6, 8)
        self.edition_group = QButtonGroup(self)
        self.edition_group.setExclusive(True)
        self.stable_btn = btn("Stable", None, kind="ghost", w=78, h=32)
        self.preview_btn = btn("Preview", None, kind="ghost", w=78, h=32)
        for b in (self.stable_btn, self.preview_btn):
            b.setObjectName("Toggle")
            b.setCheckable(True)
        self.edition_group.addButton(self.stable_btn)
        self.edition_group.addButton(self.preview_btn)
        efl.addWidget(self.stable_btn)
        efl.addWidget(self.preview_btn)
        self.stable_btn.clicked.connect(lambda: self.select_edition("release"))
        self.preview_btn.clicked.connect(lambda: self.select_edition("preview"))
        h.addWidget(ed_field)

        h.addStretch(1)

        self.details_btn = btn("Details", self.toggle_details, kind="ghost", w=76, h=52,
                                tip="Show Activity Logs")
        h.addWidget(self.details_btn)
        self.settings_btn = GearButton(self.theme)
        self.settings_btn.clicked.connect(self.toggle_settings)
        h.addWidget(self.settings_btn)
        self.play_btn = btn("▶  PLAY", self.do_play, kind="play", w=120, h=52, tip="Play Game")
        h.addWidget(self.play_btn)

        edition_id = "preview" if self.settings.get("show_betas", False) else "release"
        (self.preview_btn if edition_id == "preview" else self.stable_btn).setChecked(True)
        return dock

    def _paint_edition_toggle(self):
        pass  # QSS handles the checked-state colouring via #Toggle:checked

    # ------------------------------------------------------------ log drawer
    def _build_log_drawer(self) -> QFrame:
        wrap = QFrame(); wrap.setObjectName("ActivityLog")
        wrap.setFixedHeight(220)
        v = QVBoxLayout(wrap)
        head = QHBoxLayout()
        lab = QLabel("ACTIVITY LOG"); lab.setObjectName("Muted")
        head.addWidget(lab)
        head.addStretch(1)
        head.addWidget(btn("Clear", lambda: self.log_view.clear(), kind="flat",
                            w=64, h=24, tip="Empty the activity log"))
        # Wide enough for "Copied ✓", which replaces the label on click.
        self.copy_log_btn = btn("Copy", self._copy_log, kind="flat", w=84, h=24,
                                 tip="Copy the whole log, for a bug report")
        head.addWidget(self.copy_log_btn)
        v.addLayout(head)
        self.log_view = QTextEdit(); self.log_view.setReadOnly(True)
        v.addWidget(self.log_view)
        return wrap

    def _copy_log(self):
        QApplication.clipboard().setText(self.log_view.toPlainText())
        self.copy_log_btn.setText("Copied ✓")
        QTimer.singleShot(1200, lambda: _alive(self.copy_log_btn) and self.copy_log_btn.setText("Copy"))

    def toggle_details(self):
        self.ui_state["details"] = not self.ui_state["details"]
        self.log_drawer.setVisible(self.ui_state["details"])

    # ------------------------------------------------------------ logging
    _FRIENDLY = (
        ("downloading minecraft", None),
        ("building winegdk", "Setting up the game engine — first run, this can take a while…"),
        ("cloning winegdk", "Setting up the game engine — first run, this can take a while…"),
        ("updating winegdk", "Setting up the game engine — first run, this can take a while…"),
        ("installing minecraft", "Installing Minecraft…"),
        ("reinstalling minecraft", "Installing Minecraft…"),
        ("preparing gdk-proton", "Preparing the engine…"),
        ("extracting", "Preparing the engine…"),
        ("pre-auth", "Signing in to Xbox Live…"),
        ("signing in", "Signing in to Xbox Live…"),
        ("offline mode", "Starting Minecraft in offline mode…"),
        ("starting minecraft", "Starting Minecraft…"),
        ("launching minecraft", "Starting Minecraft…"),
    )

    def _friendly(self, line: str):
        low = line.lower()
        if "minecraft is running" in low:
            return "Minecraft is running — close the game to come back here.", True
        if "game closed" in low:
            return "Minecraft closed.", True
        for needle, msg in self._FRIENDLY:
            if needle in low:
                return msg, False
        return None

    def set_status(self, text, color=None):
        """The status line, colour included.

        Always writing the stylesheet is the point: a failure paints the line
        red, and nothing that came after it used to paint it back, so one
        failed launch left "Preparing…" and "Downloading…" red for the rest
        of the session.
        """
        self.status_label.setText(text)
        self.status_label.setStyleSheet(
            f"color:{color};" if color else "")

    def _append_log_html(self, markup: str):
        """Append pre-escaped markup and keep the view on the newest line."""
        self.log_view.append(markup)
        bar = self.log_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    @Slot(str)
    def _on_log_line(self, line: str):
        lvl = _LEVELS.get(line[:2])
        if lvl:
            label, _a1, _a2, level_color, msg_color = lvl
            self._append_log_html(
                f'<span style="color:{level_color}; font-weight:700;">{html.escape(label)}</span>'
                f'  <span style="color:{msg_color};">{html.escape(line[2:].strip())}</span>')
        else:
            # The wrapping span is not decoration. QTextEdit.append() decides
            # between rich and plain text with Qt::mightBeRichText(), and an
            # escaped string that contains no tag does not look like markup --
            # so "-> downloading 1.21.130.7" was appended verbatim and rendered
            # as "-&gt; downloading 1.21.130.7". Only `::`, `OK`, `!!` and `xx`
            # carry a level, so `==` and `->` -- the two most common prefixes
            # in a launch -- always took this branch.
            self._append_log_html(f"<span>{html.escape(line)}</span>")
        if not self.ui_state.get("busy"):
            return
        if line.startswith("xx"):
            self.set_status(line[2:].strip(), self.theme.red)
            return
        friendly = self._friendly(line)
        if friendly:
            txt = friendly[0] if isinstance(friendly, tuple) else friendly
            steady = friendly[1] if isinstance(friendly, tuple) else False
            self.set_status(txt, self.theme.green if steady else None)
            if steady:
                self.progress.hide()
            else:
                self._show_bar_busy()

    def _show_bar_busy(self):
        self.progress.show()
        self.progress.setRange(0, 0)  # indeterminate

    def set_progress(self, got, total):
        """Show a byte count against a total, both of them 64-bit.

        A Minecraft package is 2.3 GiB and neither a Qt `int` nor a
        QProgressBar holds that. The counts used to be handed straight to
        both: PySide6 wrapped the total into a negative 32-bit int, `max(1,
        total)` turned that into 1, and the status line read the download out
        as `100 * got` — "Downloading Minecraft…  24346886100%" (#216). The
        bar beneath it stayed empty for the whole download, because a value
        outside a QProgressBar's range is not clamped, it is ignored.
        """
        got, total = int(got), int(total)
        if total <= 0:
            # Nothing to measure against: sweep rather than invent a figure.
            self._show_bar_busy()
            return
        got = min(max(got, 0), total)
        self.progress.show()
        self.progress.setRange(0, BAR_STEPS)
        self.progress.setValue(BAR_STEPS * got // total)
        self.set_status(f"Downloading Minecraft…  {100 * got // total}%")

    def end_progress(self):
        self.progress.hide()

    # ------------------------------------------------------------ version picker
    def _wire_version_picker(self):
        self.version_popup = VersionPicker(self)
        self.version_popup.picked.connect(self.set_version)

    def open_picker(self):
        labels = [l for l, v in zip(self.ui_state.get("labels") or [],
                                     self.ui_state.get("versions") or [])
                  if self._edition_matches(v)]
        if not labels:
            return
        self.version_popup.set_labels(labels, self.ver_label.text())
        self.version_popup.setFixedWidth(max(260, self.ver_field.width()))
        self.version_popup.show_above(self.ver_field)

    def set_version(self, label):
        self.version_popup.close()
        self.ver_label.setText(label)
        self._update_selected_chip()

    def _edition_matches(self, v, wanted=None):
        wanted = (wanted or ("preview" if self.preview_btn.isChecked() else "release")) == "preview"
        return bool(v.get("beta", False)) == wanted

    def select_edition(self, edition_id):
        self.settings = load_settings()
        self.settings["show_betas"] = edition_id == "preview"
        save_settings(self.settings)
        matches = [l for l, v in zip(self.ui_state.get("labels") or [],
                                      self.ui_state.get("versions") or [])
                   if self._edition_matches(v, edition_id)]
        if matches:
            self.set_version(matches[0])
        else:
            self.selected_chip.setText("")

    def _update_selected_chip(self):
        lab = self.ver_label.text()
        if not lab or lab == "Loading…":
            self.selected_chip.setText("")
            return
        is_beta = "BETA" in lab
        self.settings = load_settings()
        changed = False
        if self.settings.get("ui_is_beta") != is_beta:
            self.settings["ui_is_beta"] = is_beta
            changed = True
        # What the picker shows is a shortened label -- "26.44" for
        # 1.26.44.3 -- and what gets saved has to be the build itself. Saving
        # the label named no build anything could find: setup answered "no
        # longer listed" and installed the newest instead, so a launcher
        # restarted after playing an older build quietly downloaded another
        # 2.5 GB of game nobody had asked for (#214).
        display = lab.split("  ")[0]
        picked = self.selected_version()
        cur_mc_ver = picked["tag"] if picked else display
        if self.settings.get("mc_version") != cur_mc_ver:
            self.settings["mc_version"] = cur_mc_ver
            changed = True
        if changed:
            save_settings(self.settings)
        self.selected_chip.setText(f"  {display}{'  ·  BETA' if is_beta else ''}  ")
        (self.preview_btn if is_beta else self.stable_btn).setChecked(True)
        self.theme.beta = is_beta
        self.apply_theme()
        cur_kill = self.ui_state.get("busy")
        self.play_btn.setToolTip(f"{'Kill' if cur_kill else 'Play'} {display}")

    def selected_version(self):
        lab = self.ver_label.text()
        if not self.ui_state["versions"] or not lab:
            return None
        labels = [format_display_version(v["tag"], v["beta"]) + ("  ·  BETA" if v["beta"] else "")
                  for v in self.ui_state["versions"]]
        try:
            idx = labels.index(lab)
            return self.ui_state["versions"][idx]
        except ValueError:
            return None

    def refresh_versions(self):
        def work():
            # Always fetch the full catalogue, stable and preview alike.
            # Which edition is *shown* is a purely client-side filter
            # (_edition_matches / open_picker / select_edition all filter
            # self.ui_state["versions"] after the fact) -- this only runs
            # once per app launch, so gating the fetch itself by the
            # currently-saved show_betas left the other edition's versions
            # never loaded for the rest of the session. That's what made
            # Preview un-selectable after a restart that happened to land
            # on Stable: the version list was fetched with
            # include_beta=False and never refetched, so no amount of
            # clicking "Preview" afterward could produce a match.
            editions = list_editions(include_beta=True)
            versions = []
            for ed in editions:
                # Per edition, so one catalogue being unreachable costs that
                # edition and not the whole picker. Without this a Preview
                # outage left the player with no Stable builds either.
                try:
                    builds = list_versions(ed["id"])
                except Exception as exc:
                    log._LOG_SINK(f"xx versions for {ed['id']}: {exc}")
                    continue
                for b in builds:
                    versions.append({"tag": b["version"], "beta": ed.get("beta", False),
                                      "edition": ed, "installed": b.get("installed", False)})
            return versions

        worker = Worker(work)
        worker.done.connect(self._on_versions_loaded)
        worker.failed.connect(lambda e: log._LOG_SINK(f"xx versions: {e}"))
        self._start_worker("versions", worker)

    def _on_versions_loaded(self, versions):
        if not _alive(self) or not _alive(self.ver_label):
            return
        if not versions:
            log._LOG_SINK("xx no versions loaded")
            return
        self.ui_state["versions"] = versions
        labels = [format_display_version(v["tag"], v["beta"]) + ("  ·  BETA" if v["beta"] else "")
                  for v in versions]
        self.ui_state["labels"] = labels
        self.ver_label.setText(
            labels[self._saved_version_index(versions)])
        self._update_selected_chip()

    @staticmethod
    def _saved_version_index(versions):
        """Which build the saved selection means; the newest when none does.

        Matched on the build tag, because that is what a selection is. Two
        older shapes still turn up in a settings file written by an earlier
        release and both keep working: the shortened label the chip used to
        save ("26.44"), and a partial version. Falling through to the newest
        build is the part that has to be rare -- it is a silent 2.5 GB
        download the next time PLAY is pressed.
        """
        wanted = (load_settings().get("mc_version") or "").strip()
        if not wanted:
            return 0
        for index, version in enumerate(versions):
            if version["tag"] == wanted:
                return index
        for index, version in enumerate(versions):
            if (version["tag"].startswith(wanted + ".")
                    or format_display_version(version["tag"],
                                              version["beta"]) == wanted):
                return index
        return 0

    # ------------------------------------------------------------ profile menu
    def _wire_profile_menu(self):
        self.profile_popup = ProfileMenu(self)
        self.profile_popup.switch.connect(self._switch_profile_target)
        self.profile_popup.new_window.connect(lambda p: open_profile_window(p))
        self.profile_popup.create_profile.connect(self._prompt_create_profile)
        self.profile_popup.manage.connect(self._open_profile_manager)

    def open_profile_menu(self):
        info = current_profile_info()
        self.profile_popup.rebuild(list_profiles(), info.get("path"))
        self.profile_popup.setFixedWidth(max(260, self.prof_card.width()))
        self.profile_popup.show_below(self.prof_card)

    def _profile_switch_blocked(self) -> bool:
        if self.ui_state.get("launch_active"):
            self.warn_box("Minecraft is running",
                "Close Minecraft first and wait for the game to exit before "
                "switching profiles in this window.\n\nThe \"New Win\" button "
                "opens another profile in a second launcher window, but only "
                "one profile can play at a time.")
            return True
        if self.ui_state.get("busy"):
            self.warn_box("Operation in progress",
                "Wait for the current preparation task to finish before "
                "switching profiles.")
            return True
        return False

    def _switch_profile_target(self, profile_path):
        self.profile_popup.close()
        if self._profile_switch_blocked():
            return
        self.close()
        relaunch_with_profile(profile_path)

    def _prompt_create_profile(self):
        self.profile_popup.close()
        if self.ui_state.get("busy") and not self.ui_state.get("launch_active"):
            self.warn_box("Operation in progress",
                "Wait for the current preparation task to finish before "
                "creating a profile.")
            return
        name, ok = QInputDialog.getText(self, "Create account profile",
            "Profile name (each profile has its own Xbox login, prefix and worlds):")
        if not ok or not name.strip():
            return
        name = name.strip()
        try:
            profile_dir = create_profile(name)
            if profile_shortcuts_supported():
                try:
                    write_profile_shortcut(name, profile_dir=profile_dir)
                except Exception:
                    pass
            if self.ui_state.get("launch_active"):
                if self.question_box("Profile Created",
                        f"Profile '{name}' was created successfully.\n\n"
                        "Minecraft is currently running, so this window can't "
                        "switch now. Open the new profile in a new window?"):
                    open_profile_window(profile_dir)
            else:
                self._switch_profile_target(profile_dir)
        except Exception as exc:
            self.error_box("Account profile", str(exc))

    def _prompt_create_profile_and_refresh(self):
        self._prompt_create_profile()
        if self.stack.currentWidget() is self.profiles_page:
            self.refresh_profiles()

    def _open_profile_manager(self):
        self.profile_popup.close()
        self.toggle_profiles()
        self.prof_label.setText(f"Profile: {current_profile_name()}")

    # ------------------------------------------------------------ accounts
    def _refresh_account_row(self, phase):
        """Record where the online sign-in stands, then redraw both places
        that show it. Kept as the single entry point the auth callbacks and
        the sign-in flow already call."""
        self._acct_mode = {"in": "out", "auth": "cancel"}.get(phase, "in")
        if phase != "auth":
            self._acct_confirm = False
        self._refresh_accounts()

    def _online_state(self):
        """(dot colour, status line, button label, danger) for the online
        account, from _acct_mode and any armed confirmation."""
        mode = getattr(self, "_acct_mode", "in")
        red = self.theme.red
        if mode == "loading":
            return self.theme.gold, "Starting sign-in…", "Loading…", None
        if mode == "out":                      # signed in; the action is out
            gt = msa_gamertag()
            return (self.theme.green,
                    f"Signed in as {gt}" if gt else "Signed in",
                    "Sign out?" if self._acct_confirm else "Sign out",
                    red if self._acct_confirm else None)
        if mode == "cancel":                   # a device code is on screen
            return (self.theme.gold, "Waiting for the sign-in to finish…",
                    "Cancel?" if self._acct_confirm else "Cancel",
                    red if self._acct_confirm else None)
        return self.theme.sub, "Not signed in", "Sign in", None

    def _store_state(self):
        """The same, for the download account."""
        if IS_MAC:
            # There is no downloader to sign in to on a Mac: xodus-cli is a
            # Linux binary. Say that in the row instead of offering a button
            # whose only possible outcome is an error.
            return (self.theme.sub, "Not available on macOS", None, None)
        if self.ui_state.get("store_login_active"):
            # A sign-in window that stops making progress is the whole of
            # issue #214, and a row that can only say "Signing in…" leaves
            # nothing to do about it but restart the launcher.
            return (self.theme.gold, "Waiting for the sign-in window…",
                    "Cancel?" if self._store_confirm else "Cancel",
                    self.theme.red if self._store_confirm else None)
        try:
            from . import xodus
            linked = xodus.signed_in()
        except Exception:
            linked = False
        if linked:
            return (self.theme.green, "Signed in",
                    "Sign out?" if self._store_confirm else "Sign out",
                    self.theme.red if self._store_confirm else None)
        return self.theme.sub, "Not signed in", "Sign in", None

    def _refresh_accounts(self):
        """Redraw the pill and the menu from the two account states."""
        if not _alive(self) or not _alive(self.acct_dot):
            return
        online_dot, online_status, online_action, online_danger = self._online_state()
        store_dot, store_status, store_action, store_danger = self._store_state()

        # The pill carries the combined state: green only when both are in,
        # because "Signed in" while the download account is missing is exactly
        # the half-truth that sent people to PLAY with nothing to click.
        if online_dot == self.theme.green and store_dot == self.theme.green:
            pill_dot = self.theme.green
        elif self.theme.gold in (online_dot, store_dot) or online_dot == self.theme.green:
            pill_dot = self.theme.gold
        else:
            pill_dot = self.theme.sub
        self.acct_dot.setStyleSheet(f"color:{pill_dot};")
        gt = msa_gamertag() if getattr(self, "_acct_mode", "in") == "out" else None
        self.acct_text.setText(gt or ("Signing in…" if pill_dot == self.theme.gold
                                       and online_dot == self.theme.gold else "Sign in"))
        self.acct_card.setToolTip(
            f"Play online: {online_status}\nDownload Minecraft: {store_status}")

        menu = getattr(self, "accounts_menu", None)
        if _alive(menu):
            menu.online.show_state(online_dot, online_status, online_action,
                                    online_danger)
            menu.download.show_state(store_dot, store_status, store_action,
                                      store_danger)

    def _wire_accounts_menu(self):
        self.accounts_menu = AccountsMenu(self)
        self.accounts_menu.xbox.connect(self.acct_click)
        self.accounts_menu.store.connect(self.store_click)
        self.accounts_menu.set_theme(self.theme)

    def open_accounts_menu(self):
        self._refresh_accounts()
        self.accounts_menu.setFixedWidth(max(352, self.acct_card.width()))
        self.accounts_menu.show_below(self.acct_card)

    def disarm_account_confirms(self):
        """Called when the menu hides: a confirmation must never outlive the
        menu that was asking for it.

        Redraws rather than only clearing the flags, so the rows are already
        back to "Sign out" the moment the menu closes instead of the next time
        it happens to be opened.
        """
        if not _alive(self):
            return
        if not (self._acct_confirm or self._store_confirm):
            return
        self._acct_confirm = False
        self._store_confirm = False
        self._refresh_accounts()

    # ---------------------------------------------------------- online account
    def acct_click(self):
        mode = getattr(self, "_acct_mode", "in")
        if mode == "loading":
            # The device-code request is already in flight. Without this a
            # second click starts a second one, and the player is handed two
            # codes for the same sign-in.
            return
        if mode == "out":
            if self._acct_confirm:
                self.na.stop()
                try:
                    msa_logout()
                except BolError as exc:
                    warn(str(exc))
                self._acct_confirm = False
                self._refresh_account_row("in" if msa_signed_in() else "out")
            else:
                self._acct_confirm = True
                self._refresh_accounts()
        elif mode == "cancel":
            if self._acct_confirm:
                self.na.stop()
                self.ui_state.pop("play_after_signin", None)
                self._acct_confirm = False
                self._refresh_account_row("in" if msa_signed_in() else "out")
                if getattr(self, "_auth_dialog", None) and _alive(self._auth_dialog):
                    self._auth_dialog.close()
            else:
                self._acct_confirm = True
                self._refresh_accounts()
        else:
            self._acct_mode = "loading"
            self._refresh_accounts()
            threading.Thread(
                target=lambda: self.na.start(
                    self._auth_bridge.auth.emit,
                    self._auth_bridge.online.emit),
                daemon=True).start()

    # ---------------------------------------------------------- download account
    def store_click(self):
        if self.ui_state.get("store_login_active"):
            if self._store_confirm:
                self._store_confirm = False
                self._cancel_store_login()
            else:
                self._store_confirm = True
                self._refresh_accounts()
            return
        from . import xodus
        if not xodus.signed_in():
            self.accounts_menu.close()
            self._link_store_account()
            return
        if self._store_confirm:
            self._store_confirm = False
            self._unlink_store_account()
        else:
            self._store_confirm = True
            self._refresh_accounts()

    def _unlink_store_account(self):
        from . import xodus
        w = Worker(xodus.logout)
        w.done.connect(lambda _r: _alive(self) and self._refresh_store_row())
        w.failed.connect(lambda e: _alive(self) and self.error_box(
            "Microsoft Store account", e))
        self._start_worker("store-account", w)

    # How long a sign-in may sit there before the launcher says something
    # about it. Microsoft's page is normally through in well under this; past
    # it, the window of issue #214 looks exactly like a window someone simply
    # has not finished typing into, and only one of the two has anything the
    # launcher can suggest.
    _STORE_LOGIN_HINT_MS = 120_000

    def _link_store_account(self, then=None):
        """Run the download's Microsoft sign-in from wherever it was asked for.

        Xodus opens its own webview window and can take a while, so the
        launcher says what it is waiting for rather than looking idle. `then`
        is whatever needed the account in the first place -- PLAY, usually,
        which resumes on its own once it is there instead of making the player
        press it again.
        """
        from . import xodus
        if IS_MAC:
            self.set_status(xodus.MAC_UNSUPPORTED)
            return
        if self.ui_state.get("store_login_active"):
            # Returning quietly here is how a sign-in that never finished
            # turned every later attempt into a button that does nothing
            # (#214): the window is already open, somewhere, and saying so is
            # the only thing that leads anywhere.
            self._store_login_already_open()
            return
        self.ui_state["store_login_active"] = True
        self._refresh_store_row()
        self.set_status("Finish the Microsoft sign-in in the window that opens…")
        self._show_bar_busy()
        # Only the hint armed by *this* sign-in may fire: a second one
        # started later must not be talked about by the first one's timer.
        token = self.ui_state["store_login_token"] = (
            self.ui_state.get("store_login_token", 0) + 1)
        QTimer.singleShot(self._STORE_LOGIN_HINT_MS,
                          lambda: self._store_login_slow(token))

        def settled():
            self.ui_state["store_login_active"] = False
            self._store_confirm = False
            if not _alive(self):
                return False
            self.end_progress()
            self._refresh_store_row()
            return True

        def finished(_result):
            if not settled():
                return
            self.set_status("Microsoft account linked.", self.theme.green)
            if then:
                then()

        def failed(message):
            from .xodus import LOGIN_CANCELLED_MESSAGE
            cancelled = message == LOGIN_CANCELLED_MESSAGE
            if not settled():
                return
            if cancelled:
                # Asked for, from the row that says "Cancel": reporting it in
                # a box the player has to dismiss would be the launcher
                # complaining about being obeyed.
                self.set_status("Microsoft sign-in cancelled.", self.theme.gold)
                return
            self.set_status("Microsoft account not linked.", self.theme.red)
            self.error_box("Microsoft account", message[:2000])

        w = Worker(xodus.login)
        w.done.connect(finished)
        w.failed.connect(failed)
        if not self._start_worker("store-account", w):
            self.ui_state["store_login_active"] = False
            self.end_progress()

    def _store_login_already_open(self):
        """Say where the sign-in that is already running went."""
        box = self._box(QMessageBox.Warning, "Sign-in already open",
                        "A Microsoft sign-in window is already open for the "
                        "Minecraft download.\n\nFinish it, or close it — if "
                        "it is stuck on a page that never stops loading, "
                        "cancel it here and start it again.")
        cancel = box.addButton("Cancel the sign-in", QMessageBox.DestructiveRole)
        box.addButton("Keep waiting", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is cancel:
            self._cancel_store_login()

    def _store_login_slow(self, token):
        """Nothing has failed, but the window has been up a long time."""
        if not _alive(self) or not self.ui_state.get("store_login_active"):
            return
        if self.ui_state.get("store_login_token") != token:
            return
        self.set_status(
            "Still waiting for the Microsoft sign-in. If its window is stuck "
            "loading, cancel it from the account menu and try again.",
            self.theme.gold)

    def _cancel_store_login(self):
        """Close the sign-in window from the launcher.

        xodus.cancel_login() signals the whole webview process group and
        waits for it, so it runs off the UI thread; the login worker's own
        failed() reports the cancellation once the process is gone.
        """
        from . import xodus
        self.set_status("Closing the Microsoft sign-in window…", self.theme.gold)
        w = Worker(xodus.cancel_login)
        w.done.connect(lambda closed: _alive(self)
                       and self._store_login_cancel_done(closed))
        self._start_worker("store-cancel", w)

    def _store_login_cancel_done(self, closed):
        """Clear the waiting state when there was no process left to close.

        Normally the login worker's failed() does this, on its way out of a
        process that has just been signalled. A flag left set with nothing
        running behind it is the state that makes every later sign-in a
        button that does nothing, so it is cleared here too.
        """
        if closed or not self.ui_state.get("store_login_active"):
            return
        self.ui_state["store_login_active"] = False
        self._store_confirm = False
        self.end_progress()
        self._refresh_store_row()
        self.set_status("Microsoft sign-in cancelled.", self.theme.gold)

    def _offer_store_account_link(self, reason="",
                                   title="Sign in to download Minecraft",
                                   action="Sign in", decline="Not now"):
        """Offer the download's sign-in where it is actually missed.

        A choice that is really an action reads far better under its own name
        than under "Yes", which makes the player re-read the question to work
        out what "Yes" is about to do. Returns True only when they asked to
        sign in now.
        """
        box = self._box(QMessageBox.Question, title,
                        (f"{reason}\n\n" if reason else "") + STORE_LINK_EXPLAINER)
        no = box.addButton(decline, QMessageBox.RejectRole)
        yes = box.addButton(action, QMessageBox.AcceptRole)
        box.setDefaultButton(yes)
        box.exec()
        return box.clickedButton() is not no

    def _offer_download_sign_in(self, then=None):
        """Chain the download's sign-in onto the one just completed.

        Two sign-ins is the shape of the system rather than a choice, and
        asking for the second here keeps them together, while the player is
        still in the middle of signing in. Returns True when a sign-in was
        started, so a caller waiting to do something afterwards knows whether
        to wait for ``then`` or to carry on itself.
        """
        from . import xodus
        if self.ui_state.get("store_login_active") or xodus.signed_in():
            return False
        if self._offer_store_account_link(
                "You are signed in for playing online. Minecraft itself is "
                "downloaded with a second, separate sign-in.",
                title="One more sign-in"):
            self._link_store_account(then=then)
            return True
        return False

    def _offer_online_sign_in(self):
        """PLAY was pressed with no account for online play.

        Nothing has failed -- offline is a real way to play -- so this warns
        rather than blocks. It exists because the sign-in people remember
        doing is the download's, which happens on its own at PLAY; the online
        one never does, and its absence used to show up only inside the game,
        as Realms and servers quietly missing (#240).

        Returns "signin" or "play".
        """
        box = self._box(QMessageBox.Warning, "Not signed in for online play",
                        OFFLINE_PLAY_EXPLAINER)
        again = QCheckBox("Don't warn me again")
        box.setCheckBox(again)
        # RejectRole so Escape lands here too: dismissing the warning leaves
        # the launcher doing what it did before the warning existed.
        box.addButton("Play offline", QMessageBox.RejectRole)
        sign = box.addButton("Sign in", QMessageBox.AcceptRole)
        box.setDefaultButton(sign)
        box.exec()
        if again.isChecked():
            self._save_setting("warn_offline_play", False)
        # Anything other than the explicit "Sign in" -- including a dismissal
        # with no button at all -- leaves PLAY doing what it has always done.
        return "signin" if box.clickedButton() is sign else "play"

    def _online_sign_in_settled(self):
        """Whether PLAY may go ahead now.

        False means the warning sent the player to the sign-in instead, and
        PLAY resumes on its own once that finishes.
        """
        if msa_signed_in() or not self.settings.get("warn_offline_play", True):
            return True
        if getattr(self, "_acct_mode", "in") in ("loading", "cancel"):
            # A device code is already in flight or on screen; warning about
            # the sign-in the player is in the middle of doing helps nobody.
            return True
        if self._offer_online_sign_in() == "play":
            return True
        self.ui_state["play_after_signin"] = True
        if _alive(getattr(self, "accounts_menu", None)):
            self.accounts_menu.close()
        self.acct_click()
        return False

    def _on_auth(self, url, code):
        # Reached via AuthBridge.auth, already queued onto the UI thread.
        if not _alive(self):
            return
        self._refresh_account_row("auth")
        self._code_dialog(url, code)

    def _on_online(self):
        # Reached via AuthBridge.online, already queued onto the UI thread.
        if not _alive(self):
            return
        # Read before closing the dialog below: closing it runs the same
        # teardown a cancel does, which withdraws a pending PLAY.
        resume = bool(self.ui_state.pop("play_after_signin", False))
        self._refresh_account_row("in")
        if getattr(self, "_auth_dialog", None) and _alive(self._auth_dialog):
            self._auth_dialog.close()
        self._warm_xbox_preauth()
        # Ask for the second sign-in while the player is still in the middle
        # of signing in, rather than letting them find out at PLAY.
        started = self._offer_download_sign_in(then=self.do_play if resume else None)
        # PLAY was what asked for this sign-in, so finish what it started --
        # unless the download's sign-in is now running, which will do it.
        if resume and not started:
            self.do_play()

    def _on_refreshed(self):
        # Reached via AuthBridge.refreshed, emitted from the raw worker
        # thread inside _warm_xbox_preauth. This has to be a real bound
        # method (not a lambda) connected here: a plain lambda has no
        # owning QObject, so Qt/PySide can't resolve a receiver thread for
        # it and silently falls back to invoking it directly on whichever
        # thread calls emit() -- i.e. back on the worker thread, touching
        # widgets off the UI thread. A bound method of this QObject gives
        # Qt a real receiver thread to marshal the call onto.
        if not _alive(self):
            return
        self._refresh_account_row("in")

    def _warm_xbox_preauth(self):
        """Mint the Xbox token chain now rather than at PLAY.

        launch.py runs xbl_preauth again on its own, so nothing breaks
        without this -- it just moves the whole SISU/XSTS round trip off the
        first launch and into the moment the player is already waiting on a
        sign-in. It also settles the account row: the row goes green on the
        MSA token alone, and only this says whether Xbox agreed.
        """
        def work():
            from .auth import (
                msa_load, msa_refresh, xbl_preauth, _account_cache_epoch)
            from .config import DATA
            try:
                token = msa_load()
                if not token:
                    return
                fresh = msa_refresh(token.get("refresh_token"))
                if not (fresh and fresh.get("access_token")):
                    return
                epoch = _account_cache_epoch(DATA / "winegdk-preauth")
                if xbl_preauth(fresh.get("access_token"), epoch):
                    # Runs on the raw worker thread started below, which has
                    # no Qt event loop -- go through AuthBridge rather than
                    # QTimer.singleShot (see AuthBridge docstring).
                    self._auth_bridge.refreshed.emit()
            except Exception:
                # Best-effort warm-up: PLAY re-runs the whole chain and is
                # where a real failure has to be reported, with its
                # diagnostic attached.
                pass

        threading.Thread(target=work, daemon=True).start()

    def _code_dialog(self, url, code):
        full_url = f"https://login.live.com/oauth20_remoteconnect.srf?otc={code}"
        dlg = QDialog(self)
        dlg.setObjectName("Root")
        dlg.setWindowTitle("Sign in to Microsoft")
        dlg.setStyleSheet(self.theme.qss())
        dlg.setMinimumWidth(400)
        self._auth_dialog = dlg

        def on_close():
            self.na.stop()
            self.ui_state.pop("play_after_signin", None)
            self._refresh_account_row("in" if msa_signed_in() else "out")
            dlg.close()
        dlg.finished.connect(lambda _r: on_close())

        outer = QVBoxLayout(dlg)
        outer.setContentsMargins(20, 20, 20, 20)
        outer.setSpacing(14)

        head = QLabel("Sign in with Microsoft"); head.setObjectName("Title")
        outer.addWidget(head)
        sub = QLabel("Use the account that owns Minecraft.")
        sub.setObjectName("Sub")
        sub.setWordWrap(True)
        outer.addWidget(sub)

        card = QFrame(); card.setObjectName("Card")
        cv = QVBoxLayout(card)
        cv.setContentsMargins(18, 18, 18, 18)
        cv.setSpacing(12)

        step1 = QLabel("1. Open the sign-in page")
        step1.setStyleSheet("font-weight:700;")
        cv.addWidget(step1)
        cv.addWidget(btn("Open Microsoft sign-in \u2197",
                        lambda: open_path(full_url),
                        kind="primary", h=42))

        step2 = QLabel("2. Enter this code")
        step2.setStyleSheet("font-weight:700; margin-top:4px;")
        cv.addWidget(step2)

        code_pill = QFrame(); code_pill.setObjectName("Pill")
        code_v = QVBoxLayout(code_pill)
        code_v.setContentsMargins(14, 10, 14, 10)
        code_lbl = QLabel(code)
        code_lbl.setAlignment(Qt.AlignCenter)
        code_lbl.setStyleSheet("font-family: monospace; font-size: 30px; font-weight: 700; "
                                f"color: {self.theme.blue}; letter-spacing: 4px;")
        code_v.addWidget(code_lbl)
        cv.addWidget(code_pill)

        self._copy_btn = btn("Copy code", lambda: self._copy_signin_code(code), kind="ghost", h=32)
        cv.addWidget(self._copy_btn)

        outer.addWidget(card)

        waiting = QLabel("Waiting for you to finish signing in in your browser\u2026")
        waiting.setObjectName("Muted")
        waiting.setWordWrap(True)
        outer.addWidget(waiting)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_row.addWidget(btn("Cancel", dlg.close, kind="ghost", w=90, h=32))
        outer.addLayout(close_row)

        dlg.resize(420, 420)
        dlg.show()

    def _copy_signin_code(self, code):
        QApplication.clipboard().setText(code)
        if getattr(self, "_copy_btn", None):
            self._copy_btn.setText("Copied \u2713")
            QTimer.singleShot(1500, lambda: self._copy_btn.setText("Copy code"))

    # ------------------------------------------------------------ play / kill
    def do_play(self):
        if self.ui_state["busy"]:
            return
        ver = self.selected_version()
        if ver is None:
            self.warn_box("No version selected", "Pick a Minecraft version first.")
            return
        if self.ui_state.get("store_login_active"):
            # The download needs the account that sign-in is for, so starting
            # it now buys one certain failure: setup gets as far as the
            # download, finds no account and stops. Three of those in a row
            # is what issue #214 was reported with.
            self._store_login_already_open()
            return
        if not self._online_sign_in_settled():
            return
        self._set_busy(True)
        self.set_status("Preparing…")
        self._show_bar_busy()

        w = LaunchWorker(ver)
        w.progress.connect(self.set_progress)
        w.done.connect(self._play_finished)
        w.failed.connect(self._play_failed)
        w.needs_store_signin.connect(self._store_signin_needed)
        w.store_device_limit.connect(self._store_device_limit)
        w.close_window.connect(self._close_for_game)
        w.step_aside.connect(self._step_aside_for_game)
        w.come_back.connect(self._come_back_from_game)
        self.ui_state["launch_active"] = True
        self._start_worker("play", w)

    def _close_for_game(self):
        """The player asked for this in Settings ▸ General: the window goes
        for good the moment the game starts, while this process stays alive
        in the background to see the launch/session out."""
        if self.ui_state.get("window_gone"):
            return
        self.ui_state["window_gone"] = True
        self.na.stop()
        self._force_close = True
        self.close()

    def _step_aside_for_game(self):
        """Single-window sessions (e.g. Steam Game Mode) show one window at
        a time, so hide instead of closing; ``_come_back_from_game`` restores it."""
        if self.ui_state.get("stepped_aside"):
            return
        self.ui_state["stepped_aside"] = True
        self.hide()

    def _come_back_from_game(self):
        if not self.ui_state.get("stepped_aside"):
            return
        self.ui_state["stepped_aside"] = False
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _play_finished(self, _result):
        self.ui_state["launch_active"] = False
        self.ui_state.pop("store_signin_offered", None)
        self.ui_state.pop("device_page_offered", None)
        if self.ui_state.get("window_gone"):
            QApplication.instance().quit()
            return
        self.set_status("Minecraft closed.")
        self.end_progress()
        self._set_busy(False)

    def _play_failed(self, message):
        self.ui_state["launch_active"] = False
        self.ui_state.pop("store_signin_offered", None)
        self.ui_state.pop("device_page_offered", None)
        log._LOG_SINK(f"xx {message}")
        if self.ui_state.get("window_gone"):
            desktop_notify(message[:400], "Minecraft could not start")
            QApplication.instance().quit()
            return
        self.set_status("Minecraft could not start.", self.theme.red)
        self.end_progress()
        self._set_busy(False)
        try:
            ack = gpu_crash_acknowledgement_status()
        except Exception:
            ack = None
        if ack and ack.can_acknowledge:
            self._offer_gpu_ack(ack, prefix=message[:2000] + "\n\n",
                                 title="Minecraft could not start")
        else:
            self.error_box("Minecraft could not start", message[:2000])

    def _store_signin_needed(self, message):
        """PLAY got as far as the download and found no account for it.

        Reporting this as "Minecraft could not start" was true and useless:
        the player is told something failed, with nothing in the window to act
        on, and the account it means is not the one they signed into. Offer
        that account instead, and resume PLAY on its own once it is there.
        """
        self.ui_state["launch_active"] = False
        log._LOG_SINK(f"xx {message}")
        if self.ui_state.get("window_gone"):
            desktop_notify(message[:400], "Minecraft could not start")
            QApplication.instance().quit()
            return
        self.end_progress()
        self._set_busy(False)
        self.set_status("Microsoft sign-in needed to download Minecraft.",
                        self.theme.gold)
        if self.ui_state.pop("store_signin_offered", False):
            # PLAY already went through the sign-in once for this attempt and
            # the download still says there is no account. Offering the same
            # window again is the loop from issue #214, not a fix.
            self.error_box("Minecraft could not be downloaded", message[:2000])
            return
        if self._offer_store_account_link(message):
            self.ui_state["store_signin_offered"] = True
            self._link_store_account(then=self.do_play)

    def _store_device_limit(self, message):
        """The download was refused because the account is out of devices.

        Microsoft licenses Store content to a device and lets an account hold
        ten of them; the eleventh download is refused until one is given back,
        which happens on a Microsoft web page and nowhere else. So the whole
        remedy for this failure sits outside the launcher, and reporting it as
        an error box left the player to notice a URL in a paragraph, copy it
        out by hand, and know to come back and press PLAY again.

        Open the page instead, and offer the download again the moment they
        are back: nothing about it changed while they were away -- it was the
        account that was full, and it is a different account state now.
        """
        self.ui_state["launch_active"] = False
        log._LOG_SINK(f"xx {message}")
        if self.ui_state.get("window_gone"):
            desktop_notify(message[:400], "Minecraft could not be downloaded")
            QApplication.instance().quit()
            return
        self.end_progress()
        self._set_busy(False)
        self.set_status("This account is out of Microsoft Store devices.",
                        self.theme.gold)
        if self.ui_state.pop("device_page_offered", False):
            # The page was opened once for this launch already and the answer
            # has not changed. Offering it again in a loop would be its own
            # bug; say it plainly and stop.
            self.error_box("Minecraft could not be downloaded", message[:2000])
            return
        if not self._offer_device_page(message):
            return
        from .xodus import DEVICE_PAGE
        self.ui_state["device_page_offered"] = True
        open_path(DEVICE_PAGE)
        if self.question_box(
                "Try the download again?",
                "Your Microsoft devices are open in your browser. Remove the "
                "ones you no longer use — removing a device does not remove "
                "anything you own — then come back here.\n\n"
                "Start the download again now?"):
            self.do_play()

    def _offer_device_page(self, message):
        """Offer the Microsoft page a Store device is given back on.

        Same shape as _offer_store_account_link, and for the same reason: the
        choice is an action, and it reads as one under its own name rather
        than under "Yes". Returns True only when they asked to open it.
        """
        box = self._box(QMessageBox.Warning,
                        "Minecraft could not be downloaded", message[:2000])
        later = box.addButton("Not now", QMessageBox.RejectRole)
        manage = box.addButton("Manage devices", QMessageBox.AcceptRole)
        box.setDefaultButton(manage)
        box.exec()
        return box.clickedButton() is not later

    def _set_busy(self, on):
        self.ui_state["busy"] = on
        if on:
            self.play_btn.setObjectName("Kill")
            self.play_btn.setText("⏹  KILL")
            self.play_btn.clicked.disconnect()
            self.play_btn.clicked.connect(kill_wine)
        else:
            self.play_btn.setObjectName("Play")
            self.play_btn.setText("▶  PLAY")
            self.play_btn.clicked.disconnect()
            self.play_btn.clicked.connect(self.do_play)
        self.play_btn.style().unpolish(self.play_btn)
        self.play_btn.style().polish(self.play_btn)

    def _offer_gpu_ack(self, ack_status, prefix="", title="Acknowledge previous GPU incident"):
        return _offer_gpu_incident_acknowledgement(
            _MainWindowMessageBoxAdapter(self), self, ack_status,
            prefix=prefix, title=title)

    # ------------------------------------------------------------ settings / changelog toggles
    def toggle_settings(self):
        if self.stack.currentWidget() is self.settings_page:
            self.stack.setCurrentWidget(self.hero_page)
            self._nav_follow_page()
        else:
            self.stack.setCurrentWidget(self.settings_page)
            self._on_settings_tab(self.settings_tabs.currentIndex())
            self._nav_follow_page(self.settings_page)

    def toggle_changelog(self):
        if self.stack.currentWidget() is self.changelog_page:
            self.stack.setCurrentWidget(self.hero_page)
            self._nav_follow_page()
        else:
            self.stack.setCurrentWidget(self.changelog_page)
            self.load_changelogs()
            self._nav_follow_page(self.changelog_page)

    def _nav_follow_page(self, page=None):
        """Move the controller highlight onto the page just opened.

        Only when it is already showing: a page opened with the mouse must not
        make a focus ring appear out of nowhere. Without this the highlight
        would sit on the gear button it came from while the user looks at a
        panel full of controls it has not reached.
        """
        nav = getattr(self, "nav", None)
        if nav is None or not nav.is_showing():
            return
        if page is None:
            nav.move_to_entry()
        else:
            nav.enter(page)

    # ------------------------------------------------------------ settings page
    def _build_settings(self) -> QWidget:
        page = QFrame(); page.setObjectName("Card")
        outer = QVBoxLayout(page)
        outer.setContentsMargins(20, 18, 20, 18)
        head = QHBoxLayout()
        t = QLabel("Settings"); t.setObjectName("Title")
        head.addWidget(t)
        head.addStretch(1)
        head.addWidget(btn("← Back", self.toggle_settings, kind="flat", w=76, h=28))
        outer.addLayout(head)

        tabs = QTabWidget()
        outer.addWidget(tabs, 1)

        tabs.addTab(self._scrollable(self._build_general_tab()), "General")
        self._versions_tab = tabs.addTab(
            self._scrollable(self._build_versions_tab()), "Versions")
        tabs.addTab(self._scrollable(self._build_advanced_tab()), "Advanced")
        tabs.addTab(self._scrollable(self._build_tools_tab()), "Tools")
        # Adding up what every build weighs means walking tens of thousands
        # of files, so it happens when someone opens the tab that shows the
        # figure -- not on the way to a window that may never show it.
        self.settings_tabs = tabs
        tabs.currentChanged.connect(self._on_settings_tab)
        return page

    def _on_settings_tab(self, index):
        if index == getattr(self, "_versions_tab", -1):
            self.refresh_builds()

    def _scrollable(self, inner: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(inner)
        return area

    def _build_general_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        appearance = card_section(v, "Appearance")
        theme_row = self._switch("Light theme", self.settings.get("light_theme", False),
                               "Switch the launcher between dark and light appearance.")
        theme_row.toggled.connect(self._save_theme_toggle)
        appearance.addWidget(theme_row)

        startup = card_section(v, "Startup")
        cl_row = self._switch("Show changelog on startup",
                            self.settings.get("show_changelog_on_startup", False),
                            "Open the What's New tab automatically each time the launcher starts.")
        cl_row.toggled.connect(lambda on: self._save_setting("show_changelog_on_startup", on))
        startup.addWidget(cl_row)

        confine_row = self._switch("Keep the mouse inside the window",
                                 self.settings.get("confine_cursor", False),
                                 "Fixes the cursor escaping the game in windowed mode.")
        confine_row.toggled.connect(lambda on: self._save_setting("confine_cursor", on))
        startup.addWidget(confine_row)

        close_row = self._switch("Close the launcher when Minecraft starts",
                               self.settings.get("close_on_launch", False),
                               "The window closes as soon as the game starts, instead of "
                               "waiting for it. Off by default.")
        close_row.toggled.connect(lambda on: self._save_setting("close_on_launch", on))
        startup.addWidget(close_row)

        controller = card_section(v, "Controller",
            "Move a highlight around the launcher with a gamepad, for Steam "
            "Game Mode and any other couch setup with no mouse in reach.")
        controller_row = self._switch(
            "Navigate with a controller",
            self.settings.get("controller_nav", True),
            "D-pad or left stick moves the highlight, A activates it, B goes "
            "back, the shoulder buttons change tab and Start plays. The "
            "highlight only appears once the controller is used, and goes "
            "away as soon as the mouse moves.")
        controller_row.toggled.connect(self._toggle_controller_nav)
        controller.addWidget(controller_row)
        self.controller_status = QLabel()
        self.controller_status.setObjectName("Muted")
        self.controller_status.setWordWrap(True)
        controller.addWidget(self.controller_status)
        self._refresh_controller_status()

        accounts = card_section(v, "Accounts",
            "Minecraft is downloaded from the Microsoft Store with the account "
            "that owns it — a separate, device-bound session from the "
            "in-game sign-in above.")
        store_row = QHBoxLayout()
        self.store_label = QLabel("Store account: …")
        store_row.addWidget(self.store_label)
        store_row.addStretch(1)
        self.store_btn = btn("Link…", self._toggle_store_account, kind="ghost", w=88, h=28)
        store_row.addWidget(self.store_btn)
        accounts.addLayout(store_row)
        self._refresh_store_row()

        offline_row = self._switch("Warn before playing offline",
                                 self.settings.get("warn_offline_play", True),
                                 "PLAY says so first when nothing is signed in "
                                 "for online play, instead of leaving Realms, "
                                 "servers and friends to come up missing "
                                 "in-game.")
        offline_row.toggled.connect(lambda on: self._save_setting("warn_offline_play", on))
        accounts.addWidget(offline_row)

        presence_row = self._switch("Appear online to your Xbox friends",
                                  self.settings.get("xbl_presence", True),
                                  "Nothing in the game tells Xbox Live it is "
                                  "running under Wine, so the launcher does "
                                  "it while you play. Off means your friends "
                                  "see you as offline and cannot join your "
                                  "world or invite you.")
        presence_row.toggled.connect(lambda on: self._save_setting("xbl_presence", on))
        accounts.addWidget(presence_row)

        v.addStretch(1)
        return w

    def _toggle_controller_nav(self, on):
        self._save_setting("controller_nav", on)
        self.apply_controller_nav(on)

    def _save_theme_toggle(self, on):
        self._save_setting("light_theme", on)
        self.apply_theme()

    def _save_setting(self, key, value):
        self.settings = load_settings()
        self.settings[key] = value
        save_settings(self.settings)

    def _refresh_store_row(self):
        """Settings ▸ Accounts still shows the download account; the menu in
        the top-right is the other place it lives now, and both are drawn
        from the same state."""
        if not _alive(self):
            return
        if _alive(getattr(self, "store_label", None)) and _alive(self.store_btn):
            if self.ui_state.get("store_login_active"):
                self.store_label.setText(
                    "Store account: waiting for the sign-in window…")
                self.store_btn.setText("Cancel")
            else:
                from . import xodus
                linked = xodus.signed_in()
                self.store_label.setText(
                    "Store account: " + ("linked" if linked else "not linked"))
                self.store_btn.setText("Unlink" if linked else "Link…")
        self._refresh_accounts()

    def _toggle_store_account(self):
        from . import xodus
        if self.ui_state.get("store_login_active"):
            self._cancel_store_login()
            return
        if xodus.signed_in():
            self._unlink_store_account()
        else:
            self._link_store_account()

    # ------------------------------------------------------- installed builds
    # What a build folder is, in one place, because it is the sentence the
    # whole tab exists to say: the download and nothing else. Worlds, options,
    # screenshots, skins and packs live in the profile's Wine prefix, beside
    # the account that made them, and no removal here touches them.
    _BUILDS_EXPLAINER = (
        "Every build is downloaded into a folder of its own, so going back to "
        "one you already have is instant — and so three builds tried out are "
        "three copies of a game that size. Remove the ones you are finished "
        "with here.\n\n"
        "Your worlds, settings, screenshots, skins and packs are not kept in "
        "these folders. They belong to the profile, so removing a build never "
        "removes anything you made — and playing that version again simply "
        "downloads it back."
    )

    def _build_versions_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        builds = card_section(v, "Downloaded Minecraft versions",
                              self._BUILDS_EXPLAINER)
        self.builds_summary = QLabel("Reading what is installed…")
        self.builds_summary.setObjectName("Muted")
        self.builds_summary.setWordWrap(True)
        builds.addWidget(self.builds_summary)

        self.builds_list = QVBoxLayout()
        self.builds_list.setSpacing(6)
        builds.addLayout(self.builds_list)

        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(btn("Refresh", self.refresh_builds, kind="ghost",
                              w=84, h=30,
                              tip="Read the installed builds and their sizes "
                                  "again."))
        builds.addLayout(actions)

        v.addStretch(1)
        return w

    def refresh_builds(self):
        """Re-read what is on disk, off the UI thread.

        Each build is a tree of tens of thousands of files, and adding up
        what they weigh is what makes this worth a worker rather than a
        function call in a paint path.
        """
        if not _alive(getattr(self, "builds_summary", None)):
            return
        self.builds_summary.setText("Reading what is installed…")
        worker = Worker(installed_builds)
        worker.done.connect(
            lambda builds: _alive(self) and self._show_builds(builds))
        worker.failed.connect(
            lambda message: _alive(self) and self.builds_summary.setText(
                f"Could not read the installed builds: {message}"))
        self._start_worker("builds", worker)

    def _show_builds(self, builds):
        if not _alive(getattr(self, "builds_summary", None)):
            return
        while self.builds_list.count():
            item = self.builds_list.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        if not builds:
            self.builds_summary.setText(
                "No Minecraft build is installed yet — PLAY downloads the "
                "version selected on the main screen.")
            return
        total = sum(build["size"] or 0 for build in builds)
        summary = (f"{len(builds)} build{'s' if len(builds) != 1 else ''} "
                   f"installed, {self._fmt_size(total)} in total.")
        # The figure the whole tab is about: what removing one would buy.
        try:
            summary += (f" {self._fmt_size(shutil.disk_usage(str(GAMES)).free)}"
                        " free on this drive.")
        except OSError:
            pass
        self.builds_summary.setText(summary)
        for build in builds:
            self.builds_list.addWidget(self._build_row(build))
        # Built after this tab, so on the first pass there is no label to
        # write the new figure into yet.
        if _alive(getattr(self, "free_space_label", None)):
            self._refresh_free_space()

    def _build_notes(self, build):
        """The short, true things to say about one build under its name."""
        notes = [self._fmt_size(build["size"] or 0)]
        if build["in_use"]:
            notes.append("in use")
        if not build["playable"]:
            # A Store build that lost the package its executable is decrypted
            # from: it looks installed and cannot start (#216).
            notes.append("incomplete — PLAY downloads it again")
        if build["legacy"]:
            notes.append("installed before the move to the Microsoft Store")
        elif not build["managed"]:
            notes.append("your own folder — the launcher will not remove it")
        return "  ·  ".join(notes)

    def _build_row(self, build):
        row = QFrame()
        row.setObjectName("CardFlat")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)

        text = QVBoxLayout()
        text.setSpacing(2)
        title = QLabel(f"{build['name']}  {build['version']}")
        title.setStyleSheet("font-weight:600;")
        text.addWidget(title)
        notes = QLabel(self._build_notes(build))
        notes.setObjectName("Muted")
        notes.setStyleSheet("font-size:11px;")
        notes.setWordWrap(True)
        text.addWidget(notes)
        layout.addLayout(text, 1)

        layout.addWidget(btn("Open", lambda: self._open_build(build),
                             kind="ghost-small", w=64, h=28,
                             tip=str(build["path"])))
        if build["managed"]:
            layout.addWidget(btn("Remove", lambda: self._remove_build(build),
                                 kind="danger-small", w=76, h=28,
                                 tip="Delete this download. Worlds and "
                                     "settings are kept."))
        return row

    def _open_build(self, build):
        open_path(build["path"])

    def _remove_build(self, build):
        if self.ui_state.get("launch_active") or _mc_running():
            self.warn_box("Minecraft is running",
                          "Close the game before removing a build.")
            return
        where = str(build["path"])
        detail = (
            f"This deletes the {self._fmt_size(build['size'] or 0)} download "
            f"in\n{where}\n\n"
            "Your worlds, settings, screenshots and packs are kept — they "
            "are stored with your profile, not with the build.")
        if build["in_use"]:
            detail += ("\n\nThis is the build the launcher currently starts. "
                       "PLAY will download it again if you pick it.")
        box = self._box(QMessageBox.Warning,
                        f"Remove {build['name']} {build['version']}?", detail)
        remove = box.addButton("Remove", QMessageBox.DestructiveRole)
        box.addButton("Keep", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not remove:
            return
        self.builds_summary.setText(f"Removing {build['version']}…")
        worker = Worker(remove_build, build["path"])
        worker.done.connect(
            lambda freed: _alive(self) and self._build_removed(build, freed))
        worker.failed.connect(
            lambda message: _alive(self) and self._build_removal_failed(message))
        # Its own slot: a listing still adding up sizes must not swallow the
        # removal that was just confirmed.
        if not self._start_worker("build-remove", worker):
            self.refresh_builds()

    def _build_removed(self, build, freed):
        self.set_status(
            f"Removed {build['name']} {build['version']} — "
            f"{self._fmt_size(freed)} freed.", self.theme.green)
        # The main screen marks the builds that are already downloaded, and
        # one of them just stopped being.
        self.refresh_versions()
        self.refresh_builds()

    def _build_removal_failed(self, message):
        self.refresh_builds()
        self.error_box("Remove build", message[:2000])

    def _build_advanced_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        graphics = card_section(v, "Graphics")
        # Every switch in this card configures vkd3d-proton, the Linux Vulkan
        # payload. On macOS Direct3D is translated by the Wine backend's own
        # layer (D3DMetal), which takes none of these — so instead of showing
        # three controls that do nothing, the card says which runtime is in
        # use and where to change it.
        if IS_MAC:
            from . import winemac

            graphics.addWidget(QLabel(
                "Windows runtime: " + winemac.summary()))
            hint = QLabel(
                "Direct3D is translated by this runtime's own layer, so the "
                "ray-tracing, frame-limit and renderer settings do not apply "
                "on macOS. Set BOL_WINE to use a different Wine.")
            hint.setWordWrap(True)
            graphics.addWidget(hint)
        else:
            rt = self._switch("Ray tracing", self.settings.get("ray_tracing", True),
                            "Hands DXR to Minecraft for its Ray Traced mode. Needs an "
                            "RTX-class GPU and a ray-tracing-capable world.")
            rt.toggled.connect(lambda on: self._save_setting("ray_tracing", on))
            graphics.addWidget(rt)

            fr = self._switch("Limit frame rate to the display",
                            self.settings.get("limit_frame_rate", True),
                            "Only applies when Minecraft has no limit of its own.")
            fr.toggled.connect(lambda on: self._save_setting("limit_frame_rate", on))
            graphics.addWidget(fr)

            lr = self._switch("Legacy compatibility renderer",
                            self.settings.get("renderer", "auto") == "opengl",
                            "Last resort for GPUs without Vulkan 1.3 — drops DXVK/vkd3d.")
            lr.toggled.connect(lambda on: self._save_setting("renderer", "opengl" if on else "auto"))
            graphics.addWidget(lr)

        env = card_section(v, "Environment")
        env.addWidget(QLabel("Custom environment variables"))
        env_entry = QLineEdit(self.settings.get("custom_env") or "")
        env_entry.setPlaceholderText("e.g., PROTON_USE_WINED3D=1 KEY=VALUE")
        env_entry.textChanged.connect(lambda t: self._save_setting("custom_env", t))
        env.addWidget(env_entry)

        if not IS_MAC:
            # gamescope is a Wayland micro-compositor; there is nothing for it
            # to nest inside on macOS.
            env.addWidget(QLabel("Gamescope arguments"))
            gs_entry = QLineEdit(self.settings.get("gamescope") or "")
            gs_entry.setPlaceholderText("1 for auto, or e.g. -w 1920 -h 1080 -f")
            gs_entry.textChanged.connect(
                lambda t: self._save_setting("gamescope", t))
            env.addWidget(gs_entry)

        self._build_storage_card(v)

        diagnostics = card_section(v, "Diagnostics")
        diag = self._switch("Advanced diagnostics", self.settings.get("diagnostics", False),
                          "Verbose logs, for attaching to bug reports.")
        diag.toggled.connect(lambda on: self._save_setting("diagnostics", on))
        diagnostics.addWidget(diag)

        v.addStretch(1)
        return w

    def _build_storage_card(self, v):
        storage = card_section(v, "Storage",
            "Where the engine, downloaded Minecraft versions, saves and "
            "settings are stored. Changing this requires a restart.")

        path_row = QHBoxLayout()
        self.loc_label = QLabel(get_install_location())
        self.loc_label.setStyleSheet("font-family: monospace;")
        path_row.addWidget(self.loc_label, 1)
        copy_btn = btn("Copy", self._copy_install_path, kind="ghost", w=54, h=28, tip="Copy path")
        copy_btn.setStyleSheet("font-size:11px;")
        open_btn = btn("Open", self._open_install_folder, kind="ghost", w=54, h=28,
                        tip="Open in file manager")
        open_btn.setStyleSheet("font-size:11px;")
        path_row.addWidget(copy_btn)
        path_row.addWidget(open_btn)
        storage.addLayout(path_row)

        self.free_space_label = QLabel("")
        self.free_space_label.setObjectName("Muted")
        storage.addWidget(self.free_space_label)
        self._refresh_free_space()

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(btn("Browse…", self._do_browse_location, kind="ghost", w=84, h=32,
                               tip="Choose a new folder and move your existing worlds, "
                                   "settings and login there."))
        btn_row.addWidget(btn("Reset", self._do_reset_location, kind="flat", w=64, h=32,
                               tip="Clear the saved preference and go back to the default location."))
        storage.addLayout(btn_row)

        self.loc_status_label = QLabel("")
        self.loc_status_label.setStyleSheet(f"color:{self.theme.gold};")
        storage.addWidget(self.loc_status_label)

    def _refresh_free_space(self):
        try:
            p = Path(self.loc_label.text())
            check_p = p if p.exists() else p.parent
            free = shutil.disk_usage(check_p).free
            self.free_space_label.setText(f"{self._fmt_size(free)} free on this drive")
        except Exception:
            self.free_space_label.setText("")

    @staticmethod
    def _fmt_size(n):
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024.0:
                return f"{n:.1f} {unit}"
            n /= 1024.0
        return f"{n:.1f} TB"

    def _copy_install_path(self):
        QApplication.clipboard().setText(self.loc_label.text())

    def _open_install_folder(self):
        open_path(self.loc_label.text())

    def _relocate_blocked(self):
        if self.ui_state.get("launch_active") or _mc_running():
            self.warn_box("Minecraft is running",
                          "Close Minecraft first before changing the game files location.")
            return True
        if self.ui_state.get("busy"):
            self.warn_box("Operation in progress",
                          "Wait for the current preparation task to finish before "
                          "changing the game files location.")
            return True
        return False

    def _do_browse_location(self):
        if self._relocate_blocked():
            return
        if not is_relocation_allowed():
            self.error_box("Relocation disabled",
                "BOL_HOME is set in the environment. The location cannot be "
                "changed via the GUI.")
            return
        chosen = QFileDialog.getExistingDirectory(self, "Choose a folder for BedrockOnLinux's files")
        if not chosen:
            return
        old_dir = Path(get_install_location())
        new_dir = Path(chosen).expanduser()
        if paths_overlap(old_dir, new_dir):
            if old_dir.resolve() == new_dir.resolve():
                return
            self.error_box("Invalid location",
                "The new location can't be inside the current location "
                "(or the other way around). Choose a separate folder.")
            return

        warning_msg = (
            "Game Location Change\n\n"
            f"Current location: {old_dir}\nNew location: {new_dir}\n\n"
            "Your worlds, saves, settings, and login tokens will be moved.\n"
            "The game engine will be re-downloaded for compatibility.\n\n"
            "Proceed with relocation?")
        if not self.question_box("Confirm Relocation", warning_msg):
            return

        existing_data = any((new_dir / item).exists() for item in DIRS_TO_MOVE + FILES_TO_MOVE)
        if existing_data and not self.question_box(
                "Existing data detected",
                "The new location already contains user data. Proceeding will "
                "overwrite matching folders (backed up with .old). Continue?"):
            return

        total_size = 0
        for sub in DIRS_TO_MOVE:
            src = old_dir / sub
            if src.exists() and not src.is_symlink():
                total_size += sum(f.stat().st_size for f in src.rglob("*") if f.is_file())
        for fname in FILES_TO_MOVE:
            src = old_dir / fname
            if src.exists() and src.is_file():
                total_size += src.stat().st_size

        new_path = new_dir if new_dir.exists() else new_dir.parent
        try:
            free_space = shutil.disk_usage(new_path).free
        except Exception as e:
            self.error_box("Could not check free space", str(e))
            return
        if total_size > free_space:
            self.error_box("Not enough free space",
                f"The new location has {self._fmt_size(free_space)} free, but "
                f"you need {self._fmt_size(total_size)}.")
            return

        self.ui_state["busy"] = True
        self.loc_status_label.setText("Moving user data…")

        def work():
            with prefix_operation_lock("relocate user data"):
                migrate_data(old_dir, new_dir)

        w = Worker(work)

        def ok(_r):
            self.ui_state["busy"] = False
            self.loc_status_label.setText("")
            self.loc_label.setText(str(new_dir))
            self._refresh_free_space()
            self.info_box("Relocation Successful",
                "User data moved successfully. The engine will be re-downloaded "
                "on the next start. The launcher will now restart.")
            self.relaunch_app()

        def fail(msg):
            self.ui_state["busy"] = False
            self.loc_status_label.setText("")
            self.error_box("Relocation Error", f"Could not relocate user data:\n{msg}")

        w.done.connect(ok)
        w.failed.connect(fail)
        self._start_worker("relocate", w)

    def _do_reset_location(self):
        if self._relocate_blocked():
            return
        if not is_relocation_allowed():
            self.error_box("Relocation disabled",
                "BOL_HOME is set in the environment. The location cannot be "
                "reset via the GUI.")
            return
        if get_install_location() == default_install_location():
            return
        if not self.question_box("Reset location",
                f"Reset to the default location ({default_install_location()})?\n\n"
                "This only clears the saved preference — it does not move or "
                "delete any files. Restart required."):
            return
        clear_install_location()
        self.loc_label.setText(default_install_location())
        self._refresh_free_space()
        self.info_box("Reset Complete", "Location reset to default. The launcher will now restart.")
        self.relaunch_app()

    def _build_tools_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        content = card_section(v, "Content")
        content.addWidget(tool_row("Import content (.mcpack / .mcworld / .mcaddon / .mcskin)…",
                               self._do_import,
                               tip="Add worlds, resource/behaviour packs, add-ons or "
                                   "skins to Minecraft."))
        content.addWidget(tool_row("Inject a client DLL…", self._do_inject,
                               tip="Load a client-side .dll into the running game. "
                                   "Native / AppImage only."))
        content.addWidget(tool_row("Open Minecraft folder",
                               lambda: open_path(game_content_dir()),
                               tip="Open the folder holding your worlds, templates "
                                   "and screenshots in your file manager."))

        injector_sec = card_section(v, "Injector Settings")

        self.auto_switch = self._switch(
            "Auto-inject client DLL on launch",
            self.settings.get("injector_auto_enable", False),
            "Automatically inject a DLL when Minecraft starts."
        )
        self.auto_switch.toggled.connect(lambda on: self._save_setting("injector_auto_enable", on))
        injector_sec.addWidget(self.auto_switch)

        mode_layout = QHBoxLayout()
        mode_label = QLabel("DLL load mode:")
        mode_label.setToolTip("Choose whether to inject a local DLL or download one from a URL.")
        mode_layout.addWidget(mode_label)

        self.dll_mode_combo = QComboBox()
        self.dll_mode_combo.addItem("File", "file")
        self.dll_mode_combo.addItem("Download", "url")
        current_mode = self.settings.get("injector_dll_type", "file")
        self.dll_mode_combo.setCurrentIndex(1 if current_mode == "url" else 0)
        self.dll_mode_combo.currentIndexChanged.connect(
            lambda idx: self._on_injector_dll_type_toggled(self.dll_mode_combo.itemData(idx))
        )
        mode_layout.addWidget(self.dll_mode_combo)
        mode_layout.addStretch(1)
        injector_sec.addLayout(mode_layout)

        dll_layout = QHBoxLayout()
        is_url = self.settings.get("injector_dll_type") == "url"
        self.dll_input = QLineEdit(
            self.settings.get("injector_dll_url" if is_url else "injector_dll_path") or ""
        )
        self.dll_input.textChanged.connect(self._on_injector_dll_text_changed)
        dll_layout.addWidget(self.dll_input)

        self.dll_browse_btn = btn("Browse…", self._do_browse_dll, kind="ghost", w=84, h=32,
                                  tip="Select a DLL file from your computer.")
        dll_layout.addWidget(self.dll_browse_btn)
        injector_sec.addLayout(dll_layout)

        delay_layout = QHBoxLayout()
        delay_label = QLabel("Injection delay (seconds):")
        delay_label.setToolTip(
            "Wait at least this long after the game starts before injecting. "
            "Injection also waits for the game's window, so a DLL never loads "
            "into a game that has not opened yet.")
        delay_layout.addWidget(delay_label)

        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(0, 60)
        self.delay_spin.setValue(int(self.settings.get("injector_delay", 5)))
        self.delay_spin.valueChanged.connect(lambda val: self._save_setting("injector_delay", val))
        self.delay_spin.setFixedWidth(80)
        self.delay_spin.setFixedHeight(30)
        delay_layout.addWidget(self.delay_spin)
        delay_layout.addStretch(1)
        injector_sec.addLayout(delay_layout)

        self._update_injector_settings_ui()

        shortcuts = card_section(v, "Shortcuts")
        shortcuts.addWidget(tool_row("Create direct launch shortcut (skips this window)…",
                                 self._do_play_shortcut,
                                 tip="Make a desktop/Steam shortcut that starts Minecraft "
                                     "straight away."))
        shortcuts.addWidget(tool_row("Create isolated Xbox account shortcut…",
                                 self._do_create_profile_shortcut,
                                 tip="Make a new profile with its own Xbox login, Wine "
                                     "prefix and worlds."))

        maintenance = card_section(v, "Maintenance")
        try:
            ack_status = gpu_crash_acknowledgement_status()
        except Exception:
            ack_status = None
        if ack_status and ack_status.can_acknowledge:
            self.gpu_ack_btn = tool_row("Acknowledge previous GPU incident…",
                lambda: self._offer_gpu_ack(ack_status) and self.gpu_ack_btn.hide(),
                danger=True,
                tip="Confirm the previous graphics-driver incident has been "
                    "checked, so PLAY is unblocked again.")
            maintenance.addWidget(self.gpu_ack_btn)
        maintenance.addWidget(tool_row("Open logs folder",
                                   lambda: open_path(LOGS),
                                   tip="Open the folder with launch and activity logs, "
                                       "useful for bug reports."))
        maintenance.addWidget(tool_row("Repair (reset Wine prefix)",
                                   lambda: threading.Thread(target=reset_prefix, daemon=True).start(),
                                   tip="Reset the Wine prefix Minecraft runs in. Fixes most "
                                       "'won't start' problems; worlds and settings are kept."))
        maintenance.addWidget(tool_row("Force stop Minecraft", kill_wine, danger=True,
                                   tip="Immediately terminate Minecraft and any Wine "
                                       "processes for this profile."))

        self.tools_status_label = QLabel("")
        self.tools_status_label.setStyleSheet(f"color:{self.theme.gold};")
        v.addWidget(self.tools_status_label)
        v.addStretch(1)
        return w

    def _do_import(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Import Minecraft content", "",
            "Minecraft content (*.mcpack *.mcaddon *.mcworld *.mctemplate *.mcskin);;All files (*.*)")
        if not files:
            return
        self.tools_status_label.setText("Importing…")

        def work():
            done, errs = [], []
            for f in files:
                try:
                    done.extend(import_content(f))
                except Exception as e:
                    errs.append(f"{Path(f).name}: {e}")
            return done, errs

        def finished(result):
            done, errs = result
            self.tools_status_label.setText("")
            msg = f"Imported {len(done)} item(s)." if done else "Nothing imported."
            if errs:
                msg += "\n\nProblems:\n• " + "\n• ".join(errs)
            if _mc_running():
                msg += "\n\nMinecraft is running — restart it to see the new content."
            self.info_box("Import", msg)

        def failed(message):
            self.tools_status_label.setText("")
            self.error_box("Import", f"Could not import:\n{message}")

        w = Worker(work)
        w.done.connect(finished)
        w.failed.connect(failed)
        self._start_worker("import", w)

    def _do_inject(self):
        if not _mc_running():
            self.warn_box("DLL injector",
                "Start Minecraft first and wait for the main menu, then inject.")
            return
        last = self.settings.get("injector_dll") or ""
        dll, _ = QFileDialog.getOpenFileName(self, "Choose a client .dll to inject",
                                              str(Path(last).parent) if last else "",
                                              "Client DLL (*.dll);;All files (*.*)")
        if not dll:
            return
        self.tools_status_label.setText("Injecting…")

        def work():
            return run_injector(dll)

        def finished(name):
            # Written here rather than in work(): _save_setting reloads and
            # reassigns self.settings, which the GUI thread reads.
            self._save_setting("injector_dll", dll)
            self.tools_status_label.setText("")
            self.info_box("DLL injector", f"Injected {name} into Minecraft. ✓\n\n"
                           "(Native / AppImage only — not inside the Flatpak sandbox.)")

        def failed(msg):
            self.tools_status_label.setText("")
            self.error_box("DLL injector", f"Couldn't inject:\n{msg}")

        w = Worker(work)
        w.done.connect(finished)
        w.failed.connect(failed)
        self._start_worker("inject", w)

    def _on_injector_dll_type_toggled(self, dll_type):
        dll_type = dll_type if dll_type in ("file", "url") else ("url" if dll_type else "file")
        self._save_setting("injector_dll_type", dll_type)
        last_val = self.settings.get("injector_dll_url" if dll_type == "url" else "injector_dll_path") or ""
        self.dll_input.setText(last_val)
        self._update_injector_settings_ui()

    def _on_injector_dll_text_changed(self, text):
        on = self.dll_mode_combo.currentData() == "url"
        key = "injector_dll_url" if on else "injector_dll_path"
        self._save_setting(key, text)

    def _do_browse_dll(self):
        last = self.settings.get("injector_dll_path") or ""
        dll, _ = QFileDialog.getOpenFileName(self, "Choose a client .dll to inject",
                                              str(Path(last).parent) if last else "",
                                              "Client DLL (*.dll);;All files (*.*)")
        if dll:
            self.dll_input.setText(dll)
            self._save_setting("injector_dll_path", dll)

    def _update_injector_settings_ui(self):
        is_url = self.dll_mode_combo.currentData() == "url"
        self.dll_browse_btn.setVisible(not is_url)
        if is_url:
            self.dll_input.setPlaceholderText("https://example.com/client.dll")
        else:
            self.dll_input.setPlaceholderText("Path to client.dll")

    def _do_create_profile_shortcut(self):
        name, ok = QInputDialog.getText(self, "Create account profile",
            "Profile name (each profile has its own Xbox login, prefix and worlds):")
        if not ok or not name:
            return
        try:
            require_profile_shortcuts_supported()
            profile = create_profile(name)
            shortcut = write_profile_shortcut(name, profile_dir=profile)
            command = profile_launch_command(profile)
        except Exception as exc:
            self.error_box("Account profile", str(exc))
            return
        self.info_box("Account profile created",
            f"Created:\n{profile}\n\nDesktop shortcut:\n{shortcut}\n\n"
            "Add that shortcut as a non-Steam game for the matching Steam "
            f"user.\n\nDirect command:\n{command}")

    def _do_play_shortcut(self):
        try:
            require_shortcuts_supported()
            shortcut = write_play_shortcut()
            command = play_launch_command()
            pending = direct_launch_readiness()
        except Exception as exc:
            self.error_box("Direct launch shortcut", str(exc))
            return
        message = (f"Created:\n{shortcut}\n\nIt starts Minecraft straight away, with "
                   "no launcher window. Add it to Steam with 'Add a Non-Steam Game'.\n\n"
                   f"Direct command:\n{command}")
        if pending:
            message += "\n\nStill to do in the launcher:\n• " + "\n• ".join(pending)
        self.info_box("Direct launch shortcut", message)

    # ------------------------------------------------------------ changelog page
    def _build_changelog(self) -> QWidget:
        page = QFrame(); page.setObjectName("Card")
        outer = QVBoxLayout(page)
        outer.setContentsMargins(20, 18, 20, 18)
        head = QHBoxLayout()
        t = QLabel("Changelog"); t.setObjectName("Title")
        head.addWidget(t)
        head.addStretch(1)
        head.addWidget(btn("← Back", self.toggle_changelog, kind="flat", w=76, h=28))
        outer.addLayout(head)

        self.changelog_tabs = QTabWidget()
        outer.addWidget(self.changelog_tabs, 1)
        self.game_changelog_view = QTextBrowser()
        self.game_changelog_view.setOpenExternalLinks(True)
        self.launcher_changelog_view = QTextBrowser()
        self.launcher_changelog_view.setOpenExternalLinks(True)
        self.changelog_tabs.addTab(self.game_changelog_view, "Game")
        self.changelog_tabs.addTab(self.launcher_changelog_view, "Launcher")
        return page

    # ------------------------------------------------------------ profiles page
    def _build_profiles_page(self) -> QWidget:
        page = QFrame(); page.setObjectName("Card")
        outer = QVBoxLayout(page)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(10)

        head = QHBoxLayout()
        t = QLabel("Manage Profiles"); t.setObjectName("Title")
        head.addWidget(t)
        head.addStretch(1)
        head.addWidget(btn("← Back", self.toggle_profiles, kind="flat", w=76, h=28))
        outer.addLayout(head)

        desc = QLabel("Each profile maintains an isolated Xbox sign-in, Wine "
                       "prefix, worlds, and settings.")
        desc.setObjectName("Sub")
        desc.setWordWrap(True)
        outer.addWidget(desc)

        area = QScrollArea(); area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        self.profiles_list_widget = QWidget()
        self.profiles_list_layout = QVBoxLayout(self.profiles_list_widget)
        self.profiles_list_layout.setSpacing(8)
        area.setWidget(self.profiles_list_widget)
        outer.addWidget(area, 1)

        footer = QHBoxLayout()
        footer.addWidget(btn("+ New Profile", self._prompt_create_profile_and_refresh, kind="primary", h=32))
        footer.addStretch(1)
        outer.addLayout(footer)

        return page

    def toggle_profiles(self):
        if self.stack.currentWidget() is self.profiles_page:
            self.stack.setCurrentWidget(self.hero_page)
            self._nav_follow_page()
        else:
            self.refresh_profiles()
            self.stack.setCurrentWidget(self.profiles_page)
            self._nav_follow_page(self.profiles_page)

    def refresh_profiles(self):
        layout = self.profiles_list_layout
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        active_info = current_profile_info()
        active_path = active_info.get("path")

        def add_row(name, path, subtitle, is_active):
            row = QFrame(); row.setObjectName("CardFlat")
            h = QHBoxLayout(row)
            h.setContentsMargins(14, 10, 14, 10)
            h.setSpacing(6)
            left = QVBoxLayout()
            nlab = QLabel(name); nlab.setStyleSheet("font-weight:700;")
            left.addWidget(nlab)
            slab = QLabel(subtitle); slab.setObjectName("Muted")
            left.addWidget(slab)
            h.addLayout(left, 1)

            h.addWidget(btn("New Window", lambda: open_profile_window(path),
                            kind="ghost-small", w=84, h=26,
                            tip="Open this profile in another window"))
            if path is not None:
                h.addWidget(btn("Folder", lambda: open_path(path),
                                kind="ghost-small", w=52, h=26))
                h.addWidget(btn("Rename", lambda: self._rename_profile_row(name, is_active),
                                kind="ghost-small", w=58, h=26))
                h.addWidget(btn("Delete", lambda: self._delete_profile_row(name, is_active),
                                kind="danger-small", w=58, h=26))
            if is_active:
                lab = QLabel("Active"); lab.setStyleSheet(f"color:{self.theme.green}; font-weight:700; font-size:11px;")
                h.addWidget(lab)
            else:
                h.addWidget(btn("Switch", lambda: self._switch_profile_row(path),
                                kind="ghost-small", w=58, h=26))
            layout.addWidget(row)

        add_row("Default", None, "Main installation root", active_path is None)
        for p in list_profiles():
            path = p.get("path")
            is_active = active_path is not None and Path(active_path).resolve() == Path(path).resolve()
            add_row(p.get("name", ""), path, f"profiles/{p.get('slug', '')}", is_active)
        layout.addStretch(1)

    def _switch_profile_row(self, path):
        if self._switch_profile_target(path):
            self.toggle_profiles()

    def _rename_profile_row(self, name, is_active):
        if is_active and (self.ui_state.get("launch_active") or self.ui_state.get("busy")):
            self.warn_box("Rename Profile",
                "Cannot rename the active profile while Minecraft or a task "
                "is running in this window.")
            return
        new_name, ok = QInputDialog.getText(self, "Rename Profile", f"New name for '{name}':")
        if not ok or not new_name.strip() or new_name.strip() == name:
            return
        try:
            new_dir = rename_profile(name, new_name.strip())
            active_path = current_profile_info().get("path")
            if is_active and active_path is not None and Path(new_dir).resolve() != Path(active_path).resolve():
                self._switch_profile_row(new_dir)
                return
            self.refresh_profiles()
        except Exception as exc:
            self.error_box("Rename Profile", str(exc))

    def _delete_profile_row(self, name, is_active):
        if is_active:
            self.warn_box("Delete Profile", "Cannot delete the currently active profile.")
            return
        if not self.question_box("Delete Profile",
                f"Are you sure you want to delete profile '{name}'?\n\n"
                "This will permanently remove its worlds, settings, and player data."):
            return
        try:
            delete_profile(name)
            self.refresh_profiles()
        except Exception as exc:
            self.error_box("Delete Profile", str(exc))

    def load_changelogs(self, force=False):
        if self._changelog_loaded and not force:
            return
        self._changelog_loaded = True

        from .config import SELF_REPO
        loading = self._wrap_changelog_html("<i class='empty'>Loading…</i>")
        self.game_changelog_view.setHtml(loading)
        self.launcher_changelog_view.setHtml(loading)

        def error_html(e):
            return self._wrap_changelog_html(
                f"<b>Could not load changelog.</b><div class='release-date' "
                f"style='text-transform:none;margin-top:6px;'>{html.escape(e)}</div>")

        gw = Worker(lambda: mc_releases(fetch_all=False))
        gw.done.connect(lambda data: _alive(self) and _alive(self.game_changelog_view)
                         and self.game_changelog_view.setHtml(self._render_game_changelog_html(data)))
        gw.failed.connect(lambda e: _alive(self) and _alive(self.game_changelog_view)
                           and self.game_changelog_view.setHtml(error_html(e)))
        self._start_worker("changelog-game", gw)

        lw = Worker(lambda: gh_releases(SELF_REPO))
        lw.done.connect(lambda data: _alive(self) and _alive(self.launcher_changelog_view)
                         and self.launcher_changelog_view.setHtml(self._render_launcher_changelog_html(data)))
        lw.failed.connect(lambda e: _alive(self) and _alive(self.launcher_changelog_view)
                           and self.launcher_changelog_view.setHtml(error_html(e)))
        self._start_worker("changelog-launcher", lw)

    def _changelog_css(self) -> str:
        """Shared typography for both changelog tabs."""
        t = self.theme
        return f"""
        body {{
            font-family: -apple-system, "Segoe UI", "Inter", sans-serif;
            font-size: 13.5px;
            line-height: 1.55;
            color: {t.fg};
        }}
        h2.release-title {{
            font-size: 17px;
            font-weight: 700;
            color: {t.accent};
            margin: 0 0 2px 0;
        }}
        h2.release-title a {{ color: {t.accent}; text-decoration: none; }}
        div.release-date {{
            font-size: 11.5px;
            font-weight: 600;
            letter-spacing: 0.02em;
            text-transform: uppercase;
            color: {t.sub};
            margin: 0 0 10px 0;
        }}
        div.release-body p {{ margin: 0 0 8px 0; }}
        div.release-body h1, div.release-body h2, div.release-body h3 {{
            font-size: 14px; font-weight: 700; margin: 12px 0 4px 0; color: {t.fg};
        }}
        div.release-body li {{ margin: 0 0 4px 18px; }}
        div.release-body code {{
            background: {t.card2}; color: {t.accent};
            padding: 1px 5px; border-radius: 4px; font-family: monospace;
        }}
        div.release-body blockquote {{
            margin: 6px 0; padding: 4px 12px;
            border-left: 3px solid {t.accent};
            color: {t.sub}; background: {t.card2};
        }}
        div.release-body a {{ color: {t.accent}; }}
        hr {{
            border: none; border-top: 1px solid {t.border};
            margin: 18px 0;
        }}
        i.empty {{ color: {t.sub}; }}
        """

    def _wrap_changelog_html(self, body_html: str) -> str:
        return f"<html><head><style>{self._changelog_css()}</style></head><body>{body_html}</body></html>"

    def _md_to_html(self, text: str) -> str:
        """Small, dependency-free Markdown → HTML used for release bodies."""
        text = html.escape(text or "")
        text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
        text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
        text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
        lines = []
        in_list = False
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("#"):
                if in_list:
                    lines.append("</ul>"); in_list = False
                n = min(3, len(stripped) - len(stripped.lstrip("#")))
                lines.append(f"<h{n+1}>{stripped.lstrip('#').strip()}</h{n+1}>")
            elif stripped.startswith(("* ", "- ", "+ ")):
                if not in_list:
                    lines.append("<ul>"); in_list = True
                lines.append(f"<li>{stripped[2:]}</li>")
            elif stripped.startswith(">"):
                if in_list:
                    lines.append("</ul>"); in_list = False
                lines.append(f"<blockquote>{stripped[1:].strip()}</blockquote>")
            elif stripped == "":
                if in_list:
                    lines.append("</ul>"); in_list = False
            else:
                if in_list:
                    lines.append("</ul>"); in_list = False
                lines.append(f"<p>{stripped}</p>")
        if in_list:
            lines.append("</ul>")
        return "\n".join(lines)

    def _render_launcher_changelog_html(self, rels) -> str:
        if not rels:
            return self._wrap_changelog_html("<i class='empty'>No releases found.</i>")
        parts = []
        for rel in rels:
            tag = rel.get("tag_name", "Unknown")
            name = rel.get("name")
            date = (rel.get("published_at") or "").split("T")[0]
            body = (rel.get("body") or "").strip()
            url = rel.get("html_url")
            title = f"{tag} — {name}" if name and name != tag else tag
            title_html = f'<a href="{url}">{html.escape(title)}</a>' if url else html.escape(title)
            parts.append(f"<h2 class='release-title'>{title_html}</h2>")
            parts.append(f"<div class='release-date'>{date}</div>")
            if body:
                parts.append(f"<div class='release-body'>{self._md_to_html(body)}</div>")
            parts.append("<hr>")
        return self._wrap_changelog_html("".join(parts))

    def _render_game_changelog_html(self, data) -> str:
        if not _alive(self) or not _alive(self.ver_label):
            return ""
        lab = self.ver_label.text()
        ui_wants_beta = "BETA" in lab if lab else False
        articles = []
        for art in data.get("articles", []):
            title = art.get("title", "Unknown Release")
            if not ("bedrock" in title.lower() or "beta" in title.lower() or "preview" in title.lower()):
                continue
            is_beta = "beta" in title.lower() or "preview" in title.lower()
            if is_beta == ui_wants_beta:
                articles.append(art)
        articles = articles[:40]

        if not articles:
            return self._wrap_changelog_html("<i class='empty'>No releases found.</i>")

        parts = []
        for art in articles:
            title = art.get("title", "Unknown Release")
            is_beta = "beta" in title.lower() or "preview" in title.lower()
            title = format_display_version(title, is_beta)
            date = (art.get("updated_at") or "").split("T")[0]
            body = art.get("body") or ""
            url = art.get("html_url")
            title_html = f'<a href="{url}">{html.escape(title)}</a>' if url else html.escape(title)
            parts.append(f"<h2 class='release-title'>{title_html}</h2>")
            parts.append(f"<div class='release-date'>{date}</div>")
            parts.append(f"<div class='release-body'>{body}</div>")  # already HTML from the API
            parts.append("<hr>")
        return self._wrap_changelog_html("".join(parts))

    # ------------------------------------------------------------ self-update
    def check_for_update_async(self):
        w = Worker(check_for_update)
        w.done.connect(lambda rel: rel and _alive(self) and self._show_update_banner(rel))
        self._start_worker("update-check", w)

    def _show_update_banner(self, rel):
        if not _alive(self) or self.centralWidget() is None:
            return
        bar = QFrame(); bar.setObjectName("CardFlat")
        h = QHBoxLayout(bar)
        lab = QLabel(f"⟳  Update available — v{rel['version']}  (you have {VERSION})")
        lab.setStyleSheet(f"color:{self.theme.blue}; font-weight:700;")
        h.addWidget(lab)
        h.addStretch(1)
        h.addWidget(btn("Later", lambda: bar.setParent(None), kind="flat", w=64, h=30))
        h.addWidget(btn("Update now", lambda: self._run_update(rel, bar), kind="primary", w=112, h=30))
        self.centralWidget().layout().insertWidget(1, bar)

    def _run_update(self, rel, banner):
        banner.setParent(None)
        self.set_status(f"Updating to v{rel['version']}…")
        self._show_bar_busy()

        w = Worker(self_update, rel)
        w.progress.connect(self.set_progress)

        def done(result):
            state, msg = result
            self.end_progress()
            self.set_status(
                msg, self.theme.green if state == "ok"
                else (self.theme.red if state == "error" else None))
            if state == "ok":
                self._restart_prompt()

        w.done.connect(done)
        self._start_worker("update", w)

    def _restart_prompt(self):
        if self.question_box("Update installed", "Restart now to run the new version?"):
            self.relaunch_app()

    def relaunch_app(self):
        self.na.stop()
        try:
            if os.environ.get("APPIMAGE"):
                os.execv(os.environ["APPIMAGE"], [os.environ["APPIMAGE"], "gui"])
            main_spec = getattr(sys.modules.get("__main__"), "__spec__", None)
            if main_spec and main_spec.name:
                os.execv(sys.executable, [sys.executable, "-m", "bol", "gui"])
            tgt = os.path.realpath(sys.argv[0] or __file__)
            os.execv(sys.executable, [sys.executable, tgt, "gui"])
        except Exception:
            QApplication.instance().quit()

    # ------------------------------------------------------------ controller
    def apply_controller_nav(self, enabled):
        """Start or stop watching for controllers, from the Settings switch."""
        if not enabled:
            nav, self.nav = self.nav, None
            if nav is not None:
                nav.stop()
                self._on_nav_devices(())
            self._refresh_controller_status()
            return
        if self.nav is not None:
            return
        nav = ControllerNav(
            self,
            accent=self.theme.accent,
            on_back=self._controller_back,
            # Whatever the big button says right now -- PLAY, or KILL once
            # the game is running.
            on_start=self.play_btn.click,
            on_devices=self._on_nav_devices,
            primary_item=lambda: self.play_btn,
            # The pad keeps reporting while Minecraft is in the foreground
            # and this window is still alive behind it.
            accepts_input=lambda: not self.ui_state.get("launch_active"))
        if not nav.start():
            self._refresh_controller_status()
            return
        self.nav = nav
        self._on_nav_devices(nav.device_names)

    def _stop_controller_nav(self):
        """Let go of the controller: the window is closing."""
        nav, self.nav = self.nav, None
        if nav is not None:
            nav.stop()

    def _controller_back(self):
        """What B does with nothing opened over the window: leave the page
        the user is in, which is what Escape would do with a keyboard."""
        if self.stack.currentWidget() is self.settings_page:
            self.toggle_settings()
        elif self.stack.currentWidget() is self.changelog_page:
            self.toggle_changelog()
        elif self.stack.currentWidget() is self.profiles_page:
            self.toggle_profiles()

    def _on_nav_devices(self, names):
        """A controller was plugged in or unplugged."""
        self._nav_devices = tuple(names)
        legend = getattr(self, "nav_legend", None)
        if legend is not None:
            if names and self.nav is not None:
                accent = self.theme.accent
                legend.setText("&nbsp;&nbsp;".join(
                    f'<b style="color:{accent}">{key}</b> {what}'
                    for key, what in (("A", "Select"), ("B", "Back"),
                                      ("Start", "Play"))))
                legend.show()
            else:
                legend.hide()
        self._refresh_controller_status()

    def _refresh_controller_status(self):
        """Say in Settings what the controller support is currently doing."""
        label = getattr(self, "controller_status", None)
        if label is None:
            return
        if self.nav is None:
            label.setText("Controller navigation is off.")
        elif self._nav_devices:
            label.setText("Ready — " + ", ".join(self._nav_devices))
        else:
            label.setText("Waiting for a controller. One plugged in now is "
                          "picked up without restarting the launcher.")

    # ------------------------------------------------------------ message boxes
    def _box(self, icon, title, message) -> QMessageBox:
        box = QMessageBox(self)
        box.setIcon(icon)
        box.setWindowTitle(title)
        box.setText(message)
        box.setStyleSheet(self.theme.qss())
        return box

    def info_box(self, title, message):
        self._box(QMessageBox.Information, title, message).exec()

    def warn_box(self, title, message):
        self._box(QMessageBox.Warning, title, message).exec()

    def error_box(self, title, message):
        self._box(QMessageBox.Critical, title, message).exec()

    def question_box(self, title, message) -> bool:
        box = self._box(QMessageBox.Question, title, message)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        return box.exec() == QMessageBox.Yes

    # ------------------------------------------------------------ close handling
    def closeEvent(self, event):
        if self._force_close:
            self._stop_controller_nav()
            event.accept()
            return
        if self.ui_state.get("launch_active"):
            self.warn_box("Minecraft is running",
                "Close Minecraft first and wait for the launcher to report that "
                "it closed. To abort it, use Settings → Tools → Force stop Minecraft.")
            event.ignore()
            return
        if self.ui_state.get("busy"):
            self.warn_box("Operation in progress",
                "Wait for the current preparation task to finish before closing "
                "the launcher.")
            event.ignore()
            return
        self.na.stop()
        self._stop_controller_nav()
        event.accept()
        # The app runs with setQuitOnLastWindowClosed(False) so that
        # "close the launcher when Minecraft starts" can drop the window while
        # the launch thread keeps supervising the game. That makes quitting an
        # explicit act: without this the event loop outlives the window and the
        # process stays resident forever. `_force_close` returns above, so the
        # close-on-launch path still leaves the loop running for LaunchWorker.
        QApplication.instance().quit()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not isinstance(
                QApplication.focusWidget(), (QLineEdit, QPlainTextEdit, QTextEdit)):
            self.do_play()
        else:
            super().keyPressEvent(event)



# ======================================================================
# GPU safety incident acknowledgement (module-level, not a MainWindow method)
# ======================================================================

def _gpu_incident_safety_instruction(status):
    """Plain-text instruction that must be shown before an incident marker
    can be acknowledged. Module-level so it, and the confirmation flow
    below, are importable/testable without a QApplication."""
    return (
        "Continue only after repairing/updating the graphics driver and rebooting."
        if status.previous_boot_fault else
        "No fatal driver event was detected for this marker. Continue only "
        "after checking why the previous session or machine stopped.")


def _offer_gpu_incident_acknowledgement(
        box, parent, status, prefix="",
        title="Acknowledge previous GPU incident"):
    """Confirm + acknowledge a GPU safety incident marker. `box` is duck-typed
    on askyesno/showinfo/showerror(title, message, parent=None), so this
    needs no real dialog widget to run or to test. The eligibility decision
    itself is never made here: acknowledge_gpu_crash() re-checks it under the
    launch lock, and a refusal is reported from its live status rather than
    the one passed in, in case it changed in the meantime."""
    instruction = _gpu_incident_safety_instruction(status)
    message = prefix + status.message + "\n\n" + instruction + " Acknowledge now?"
    if not box.askyesno(title, message, parent=parent):
        return False
    if acknowledge_gpu_crash():
        box.showinfo(
            "GPU safety",
            "The previous-boot incident was acknowledged. "
            "PLAY will still run all current graphics safety checks.",
            parent=parent)
        return True
    box.showerror("GPU safety", gpu_crash_acknowledgement_status().message,
                   parent=parent)
    return False


class _MainWindowMessageBoxAdapter:
    """Adapts MainWindow's Qt-backed info_box/error_box/question_box onto the
    askyesno/showinfo/showerror shape _offer_gpu_incident_acknowledgement
    expects. `parent` is accepted for API compatibility but unused: the
    underlying QMessageBox is already parented to the window itself."""

    def __init__(self, window):
        self._window = window

    def askyesno(self, title, message, parent=None):
        return self._window.question_box(title, message)

    def showinfo(self, title, message, parent=None):
        self._window.info_box(title, message)

    def showerror(self, title, message, parent=None):
        self._window.error_box(title, message)


# ======================================================================
# Entry point
# ======================================================================

def gui():
    """Launch the PySide6 GUI."""
    from .deps import ensure_gui_deps
    ensure_gui_deps()
    _resolve_gui_display(os.environ)

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(PRETTY)
    app.setStyle("Fusion")
    # "Close the launcher when Minecraft starts" closes the window while the
    # launch thread is still supervising the game; the process itself exits
    # once that thread finishes (see MainWindow._close_for_game).
    app.setQuitOnLastWindowClosed(False)

    try:
        window = MainWindow()
    except Exception as e:
        _desktop_error(f"GUI failed to start ({e}). Use the command line instead.")
        return

    window.show()
    app.exec()
