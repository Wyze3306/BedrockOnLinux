"""bol.navigation — drive the launcher window with a game controller (Qt).

`bol.gamepad` turns a controller into logical actions; this module turns those
actions into a moving focus ring over the PySide6 window, so the whole
launcher — PLAY, the version picker, sign-in, every Settings switch — can be
operated without a mouse. That is the difference between the launcher being a
five-second stop and being a dead end on a Steam Deck or any Game Mode session,
where the launcher deliberately still opens before the game.

The geometry — `choose_neighbour`, `reading_order`, `within` — knows nothing
about a toolkit; everything that touches a widget leans on what Qt already
does:

* `QScrollArea.ensureWidgetVisible` scrolls the target into view;
* `QApplication.activePopupWidget()` and `activeModalWidget()` say what is on
  top, so a dropdown or a dialog confines the ring with nothing to register;
* a `QListWidget`, a `QLineEdit` and a `QTabBar` already navigate themselves
  from arrow keys, so the ring hands those the key rather than reimplementing
  them.

The ring itself is an overlay widget that paints an outline over the target and
nothing else: nothing about the target is touched, which is why a switch, a
painted gear and a bare clickable frame all light up the same way.

What counts as a control is discovered, not declared: the widget tree of
whatever is on top is walked on each press. Qt answers most of it (a
`QAbstractButton` is a button), and the launcher's hand-made controls — the
version pill, the profile and account chips — are frames with a pointing-hand
cursor, which is exactly what they look like to a user and so is what they are
treated as here.

Two deliberate restraints, carried over because they were right:

* the ring stays hidden until a controller is actually used, and disappears
  again on the first mouse movement, so a mouse user never sees it;
* actions are dropped while the window is hidden or Minecraft is running,
  because the controller keeps reporting to whoever is listening and the
  launcher is still alive behind the game.

That second test deliberately does not ask whether the launcher holds the
input focus, which is the obvious way to write it. A window manager that
refuses focus to a window it did not see the user open — focus-stealing
prevention, focus-follows-mouse with the pointer elsewhere, a kiosk session —
would then leave the controller doing nothing at all, and the user has no
mouse with which to fix it.
"""
# SPDX-License-Identifier: MIT

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QKeyEvent, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractSlider,
    QApplication,
    QComboBox,
    QLineEdit,
    QListView,
    QPlainTextEdit,
    QScrollArea,
    QScrollBar,
    QTabBar,
    QTextEdit,
    QWidget,
)

from .gamepad import GamepadReader

# Widgets narrower or shorter than this are placeholders mid-layout.
_MIN_SIZE = 4

# How far past the edge of a view a control may sit and still be a ring item.
# A scroll area leaves the widgets below the fold in place and simply does not
# paint them, so without a bound the ring would jump to a Settings row nobody
# can see; with a margin roughly one row deep, the control just past the fold
# stays reachable, which is what makes walking down a long list scroll it.
_VIEWPORT_MARGIN = 48

# How far a candidate must lie in the pressed direction to count as a move.
_MIN_STEP = 6

# Cost multiplier for sideways drift, when the two boxes overlap on the other
# axis and when they do not. Overlapping neighbours win comfortably without a
# far-away one beating a close, slightly offset control.
_ALIGNED_DRIFT = 0.25
_UNALIGNED_DRIFT = 3.0

# Pixels the scroll stick moves a view per repeat, and how far a d-pad press
# scrolls when there is more list below the fold than ring items on screen.
_SCROLL_STEP = 26
_SCROLL_PRESS = _SCROLL_STEP * 3

_HORIZONTAL = ("left", "right")

# Widgets that navigate themselves once they have the key: the ring hands
# these the arrow press instead of moving off them.
_SELF_NAVIGATING = (QListView, QTabBar)

_ARROW_KEYS = {
    "up": Qt.Key_Up,
    "down": Qt.Key_Down,
    "left": Qt.Key_Left,
    "right": Qt.Key_Right,
}


# ----------------------------------------------------------------- geometry
def _centre(rect):
    x, y, width, height = rect
    return x + width / 2.0, y + height / 2.0


def _overlaps(start_a, size_a, start_b, size_b):
    return (start_a < start_b + size_b) and (start_b < start_a + size_a)


def within(rect, view, margin=0):
    """Whether a widget box shows up inside a view box, give or take a margin."""
    return (_overlaps(rect[0], rect[2], view[0] - margin, view[2] + 2 * margin)
            and _overlaps(rect[1], rect[3],
                          view[1] - margin, view[3] + 2 * margin))


def choose_neighbour(rects, current, direction, wrap=True):
    """Index of the item a d-pad press moves to, or None when there is none.

    `rects` are (x, y, width, height) boxes in a common coordinate space and
    `current` indexes the focused one. With `wrap`, a press with nothing ahead
    of it comes back around from the far side, so every control stays reachable
    with the d-pad alone; the caller turns wrapping off first to find out
    whether it should scroll rather than jump.
    """
    if not rects:
        return None
    if current is None or not 0 <= current < len(rects):
        return 0
    horizontal = direction in _HORIZONTAL
    forward = direction in ("right", "down")
    current_rect = rects[current]
    current_x, current_y = _centre(current_rect)

    def measure(rect):
        other_x, other_y = _centre(rect)
        if horizontal:
            step = other_x - current_x
            drift = abs(other_y - current_y)
            aligned = _overlaps(current_rect[1], current_rect[3],
                                rect[1], rect[3])
        else:
            step = other_y - current_y
            drift = abs(other_x - current_x)
            aligned = _overlaps(current_rect[0], current_rect[2],
                                rect[0], rect[2])
        if not forward:
            step = -step
        return step, drift, aligned

    def weigh(step, drift, aligned):
        return step + drift * (_ALIGNED_DRIFT if aligned else _UNALIGNED_DRIFT)

    best = best_cost = None
    behind = []
    for index, rect in enumerate(rects):
        if index == current:
            continue
        step, drift, aligned = measure(rect)
        if step < _MIN_STEP:
            behind.append((index, rect, drift, aligned))
            continue
        cost = weigh(step, drift, aligned)
        if best_cost is None or cost < best_cost:
            best, best_cost = index, cost
    if best is not None or not wrap:
        return best

    # Nothing ahead: wrap to the item furthest back, nearest in line with the
    # current one.
    axis = 0 if horizontal else 1
    size_axis = axis + 2
    edge = None
    for _index, rect, _drift, _aligned in behind:
        far_side = rect[axis] + (0 if forward else rect[size_axis])
        edge = far_side if edge is None else (
            min(edge, far_side) if forward else max(edge, far_side))
    if edge is None:
        return None
    for index, rect, drift, aligned in behind:
        far_side = rect[axis] + (0 if forward else rect[size_axis])
        cost = weigh(abs(far_side - edge), drift, aligned)
        if best_cost is None or cost < best_cost:
            best, best_cost = index, cost
    return best


def reading_order(items, rects):
    """`items` sorted the way the eye reads them: down, then across."""
    return [item for _rect, item in
            sorted(zip(rects, items), key=lambda pair: (pair[0][1], pair[0][0]))]


# --------------------------------------------------------------------- ring
class FocusRing(QWidget):
    """An outline painted over the control the controller is pointing at.

    A child of the window rather than of the control, and transparent to the
    mouse, so lighting a control up changes nothing about it — no border to
    reconfigure and put back, and no widget that has to support one.
    """

    OUTSET = 3          # how far outside the control the outline sits
    WIDTH = 2
    RADIUS = 10

    def __init__(self, parent: QWidget, colour="#43a047"):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.NoFocus)
        self._colour = QColor(colour)
        self.hide()

    def set_colour(self, colour):
        self._colour = QColor(colour)
        self.update()

    def surround(self, target: QWidget, clip: QRect = None):
        """Put the outline around `target`, clipped to `clip` (both global)."""
        top_level = self.parentWidget()
        if target is None or top_level is None:
            self.hide()
            return
        rect = QRect(target.mapTo(top_level, QPoint(0, 0)), target.size())
        rect = rect.adjusted(-self.OUTSET, -self.OUTSET,
                             self.OUTSET, self.OUTSET)
        if clip is not None:
            clip_local = QRect(
                top_level.mapFromGlobal(clip.topLeft()), clip.size())
            rect = rect.intersected(
                clip_local.adjusted(-self.OUTSET, -self.OUTSET,
                                    self.OUTSET, self.OUTSET))
        if rect.isEmpty():
            self.hide()
            return
        self.setGeometry(rect)
        self.raise_()
        self.show()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(self._colour)
        pen.setWidth(self.WIDTH)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        inset = self.WIDTH / 2.0
        painter.drawRoundedRect(
            self.rect().adjusted(inset, inset, -inset, -inset),
            self.RADIUS, self.RADIUS)


# ------------------------------------------------------------------ the nav
class ControllerNav(QObject):
    """Focus ring over the launcher window, driven by a controller.

    The window owns the widgets; this owns which one is lit and what a button
    press does to it. Callbacks keep the decisions that are not generic:
    `on_back` for the in-window pages (Settings, What's New) that Escape would
    leave, `on_start` for the Start button, `on_devices` to show or hide the
    on-screen button legend, and `accepts_input` for "not while the game is
    running".
    """

    #: Emitted from the reader thread; queued onto the GUI thread by Qt.
    action = Signal(str)
    devices_changed = Signal(tuple)

    MOUSE_POLL_MS = 250
    #: Total pointer travel, in pixels, that counts as a mouse being used.
    MOUSE_JITTER = 24

    def __init__(self, window: QWidget, accent="#43a047", on_back=None,
                 on_start=None, on_devices=None, primary_item=None,
                 accepts_input=None, reader_factory=None, parent=None):
        super().__init__(parent or window)
        self._window = window
        self._on_back = on_back
        self._on_start = on_start
        self._on_devices = on_devices
        self._primary_item = primary_item
        self._accepts_input = accepts_input
        self._reader_factory = reader_factory or GamepadReader
        self._reader = None
        self._current: QWidget = None
        self._shown = False
        self._ring = FocusRing(window, accent)
        # Only runs while the ring is up: it exists to notice a mouse being
        # picked up, which cannot happen while there is nothing to take away.
        self._mouse_timer = QTimer(self)
        self._mouse_timer.setInterval(self.MOUSE_POLL_MS)
        self._mouse_timer.timeout.connect(self._check_mouse)
        self._mouse_at = None
        self.action.connect(self.dispatch)
        self.devices_changed.connect(self._devices)
        # Resizing or moving the window leaves the outline behind, and hiding
        # it (Game Mode stepping aside) should take the outline with it. The
        # filter is on the window itself, which sees few events, rather than
        # on the application, which sees every event Qt delivers.
        window.installEventFilter(self)

    def eventFilter(self, watched, event):
        if watched is self._window and self._shown:
            kind = event.type()
            if kind in (QEvent.Resize, QEvent.Move, QEvent.LayoutRequest):
                self.refresh_ring()
            elif kind == QEvent.Hide:
                self.hide_ring()
        return False

    # -- lifecycle --------------------------------------------------------
    def start(self) -> bool:
        """Start watching for a controller. False when none can be read."""
        if self._reader is not None:
            return True
        reader = self._reader_factory(self.action.emit,
                                      lambda names: self.devices_changed.emit(
                                          tuple(names)))
        if not reader.start():
            return False
        self._reader = reader
        return True

    def stop(self):
        self.hide_ring()
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.stop()

    def set_accent(self, accent):
        self._ring.set_colour(accent)

    @property
    def device_names(self):
        return self._reader.device_names if self._reader is not None else ()

    def _devices(self, names):
        if self._on_devices is not None:
            self._on_devices(tuple(names))

    # -- what is on top ---------------------------------------------------
    def _scope(self) -> QWidget:
        """The widget subtree the ring may move in right now.

        Qt already tracks this: a dropdown is a popup, a dialog is the active
        modal window. Neither has to be registered from the outside, which is
        the whole reason the old Tk version needed the launcher's help.
        """
        app = QApplication.instance()
        if app is not None:
            for candidate in (app.activePopupWidget(), app.activeModalWidget()):
                if candidate is not None and candidate.isVisible():
                    return candidate
        return self._window

    # -- discovery --------------------------------------------------------
    def _is_item(self, widget: QWidget) -> bool:
        if isinstance(widget, (QScrollBar, QAbstractSlider)):
            return False        # chrome: the ring reaches what it scrolls to
        if getattr(widget, "_nav_skip", False):
            return False
        if isinstance(widget, (QAbstractButton, QLineEdit, QComboBox,
                               QListView, QTabBar)):
            return True
        if isinstance(getattr(widget, "switch", None), QAbstractButton):
            return True         # a labelled switch row: one control, not two
        # The launcher's hand-made controls — the version pill, the profile
        # and account chips — are frames with a click handler. What marks them
        # out to a user is the pointing-hand cursor, so that is what marks
        # them out here.
        return widget.cursor().shape() == Qt.PointingHandCursor

    def _visible_enough(self, widget: QWidget) -> bool:
        if not widget.isVisible() or not widget.isEnabled():
            return False
        size = widget.size()
        return size.width() > _MIN_SIZE and size.height() > _MIN_SIZE

    def _scroll_area(self, widget: QWidget) -> QScrollArea:
        node = widget
        while node is not None and node is not self._window:
            parent = node.parentWidget()
            if isinstance(parent, QScrollArea):
                return parent
            if (parent is not None and isinstance(parent.parentWidget(),
                                                  QScrollArea)
                    and parent is parent.parentWidget().viewport()):
                return parent.parentWidget()
            node = parent
        return None

    def _clip_rect(self, widget: QWidget) -> QRect:
        """The area a control has to show up in: its scroll view, or the
        window."""
        area = self._scroll_area(widget)
        if area is not None:
            viewport = area.viewport()
            return QRect(viewport.mapToGlobal(QPoint(0, 0)), viewport.size())
        top = widget.window()
        return QRect(top.mapToGlobal(QPoint(0, 0)), top.size())

    @staticmethod
    def _box(widget: QWidget):
        top_left = widget.mapToGlobal(QPoint(0, 0))
        return (top_left.x(), top_left.y(), widget.width(), widget.height())

    def _items(self, scope: QWidget = None):
        """Every control the ring can land on right now, with its box."""
        if scope is None:
            scope = self._scope()
        found = []
        for widget in scope.findChildren(QWidget):
            if not self._visible_enough(widget) or not self._is_item(widget):
                continue
            found.append(widget)
        # A switch row holds a switch, a combo box holds a line edit: keep the
        # outer control so one thing on screen is one stop on the ring.
        outer = []
        for widget in found:
            parent = widget.parentWidget()
            nested = False
            while parent is not None and parent is not scope:
                if parent in found:
                    nested = True
                    break
                parent = parent.parentWidget()
            if not nested:
                outer.append(widget)
        items, rects = [], []
        for widget in outer:
            rect = self._box(widget)
            clip = self._clip_rect(widget)
            if not within(rect, (clip.x(), clip.y(), clip.width(),
                                 clip.height()), _VIEWPORT_MARGIN):
                continue
            items.append(widget)
            rects.append(rect)
        return items, rects

    # -- the ring ---------------------------------------------------------
    def show_ring(self, widget: QWidget):
        if widget is None:
            return
        self._current = widget
        self._shown = True
        self._reveal(widget)
        self._ring.surround(widget, self._clip_rect(widget))
        self._mouse_at = QCursor.pos()
        self._mouse_timer.start()

    def is_showing(self) -> bool:
        """Whether the ring is on screen right now."""
        return self._shown

    def hide_ring(self):
        self._shown = False
        self._mouse_timer.stop()
        self._ring.hide()

    def refresh_ring(self):
        """Put the outline back where it belongs after a layout change."""
        if self._shown and self._current is not None:
            if self._alive(self._current):
                self._ring.surround(self._current,
                                    self._clip_rect(self._current))
            else:
                self.hide_ring()

    @staticmethod
    def _alive(widget) -> bool:
        try:
            return widget is not None and widget.isVisible()
        except RuntimeError:      # the C++ object is gone
            return False

    def _check_mouse(self):
        """A mouse user is driving: take the ring away again.

        Two things have to be true before the ring goes, and both were learned
        the hard way on a real desktop. The pointer has to have moved a
        deliberate distance, because a resting mouse reports a pixel of drift
        and a ring that vanishes between two presses makes every press look
        like it did nothing. And it has to have moved *over this window*: a
        pointer wandering on another monitor says nothing about whether the
        person in front of the launcher is using a controller.

        Polled rather than filtered: an application-wide event filter would run
        Python for every event Qt delivers, and the only thing being asked is
        where the pointer is.
        """
        position = QCursor.pos()
        if self._mouse_at is None:
            self._mouse_at = position
            return
        moved = (abs(position.x() - self._mouse_at.x())
                 + abs(position.y() - self._mouse_at.y()))
        if moved < self.MOUSE_JITTER:
            return
        if not self._window.frameGeometry().contains(position):
            self._mouse_at = position     # elsewhere: not our business
            return
        self.hide_ring()

    # -- scrolling --------------------------------------------------------
    def _reveal(self, widget: QWidget):
        area = self._scroll_area(widget)
        if area is not None:
            area.ensureWidgetVisible(widget, 24, 24)

    def _scroll(self, direction):
        widget = self._current if self._alive(self._current) else None
        area = self._scroll_area(widget) if widget is not None else None
        if area is None:
            area = self._largest_scroll_area()
        if area is None:
            return
        bar = area.verticalScrollBar()
        bar.setValue(bar.value()
                     + (-_SCROLL_STEP if direction == "scroll_up"
                        else _SCROLL_STEP))
        self.refresh_ring()

    def _largest_scroll_area(self) -> QScrollArea:
        """The biggest scrollable view on screen — what the stick scrolls when
        the ring is not sitting in one (the changelog, the activity log)."""
        best = None
        best_area = 0
        for area in self._scope().findChildren(QScrollArea):
            if not area.isVisible():
                continue
            size = area.width() * area.height()
            if size > best_area:
                best, best_area = area, size
        return best

    def _scroll_towards(self, direction) -> bool:
        """Scroll the panel the ring sits in. True when anything moved."""
        if not self._alive(self._current):
            return False
        area = self._scroll_area(self._current)
        if area is None:
            return False
        bar = area.verticalScrollBar()
        if direction == "down" and bar.value() >= bar.maximum():
            return False
        if direction == "up" and bar.value() <= bar.minimum():
            return False
        bar.setValue(bar.value()
                     + (-_SCROLL_PRESS if direction == "up" else _SCROLL_PRESS))
        return True

    # -- moving -----------------------------------------------------------
    def _entry_item(self, items, rects):
        if self._primary_item is not None and self._scope() is self._window:
            try:
                preferred = self._primary_item()
            except Exception:
                preferred = None
            if preferred is not None and preferred in items:
                return preferred
        return reading_order(items, rects)[0] if items else None

    def move_to_entry(self):
        items, rects = self._items()
        target = self._entry_item(items, rects)
        if target is not None:
            self.show_ring(target)

    def enter(self, container: QWidget):
        """Put the ring on the first control inside a container."""
        items, rects = self._items(container)
        if items:
            self.show_ring(reading_order(items, rects)[0])

    def _list_step(self, items, rects, current, direction):
        """The next control down (or up) a scrollable panel, in reading order.

        Purely spatial movement is right for a row of buttons but wrong for
        Settings: straight down from a switch on the left is the next switch on
        the left, and the buttons ranged along the right of the same panel
        would only ever be reachable by guessing which row they share. Inside a
        scroll area the ring therefore walks the list the way the eye does, and
        left/right stays spatial for the groups within a row.
        """
        area = self._scroll_area(items[current])
        if area is None:
            return None
        group = [index for index in range(len(items))
                 if self._scroll_area(items[index]) is area]
        if current not in group or len(group) < 2:
            return None
        group.sort(key=lambda index: (rects[index][1], rects[index][0]))
        position = group.index(current) + (1 if direction == "down" else -1)
        return group[position] if 0 <= position < len(group) else None

    def _move(self, direction):
        # A list or a tab bar navigates itself: give it the key rather than
        # stepping off it, so a version list scrolls its own selection.
        if isinstance(self._current, _SELF_NAVIGATING) and self._alive(
                self._current):
            if self._send_self_navigation(self._current, direction):
                return
        items, rects = self._items()
        if not items:
            return
        if self._current not in items:
            self.move_to_entry()
            return
        index = None
        current = items.index(self._current)
        if direction in ("up", "down"):
            index = self._list_step(items, rects, current, direction)
            if index is None:
                # Nothing further in that direction on screen. Scrolling comes
                # before any spatial fallback, or a press at the bottom of
                # Settings would leap to the dock underneath instead of
                # revealing the rest of the list.
                if self._scroll_towards(direction):
                    items, rects = self._items()
                    if self._current not in items:
                        self.refresh_ring()
                        return
                    index = self._list_step(items, rects,
                                            items.index(self._current),
                                            direction)
                    if index is None:
                        self.refresh_ring()
                        return
                    current = items.index(self._current)
        if index is None:
            index = choose_neighbour(rects, current, direction, wrap=False)
            if index is None:
                index = choose_neighbour(rects, current, direction)
        if index is not None:
            self.show_ring(items[index])

    def _send_self_navigation(self, widget, direction) -> bool:
        """Hand an arrow press to a widget that knows what to do with it.

        A tab bar only answers left and right, a list only up and down; the
        other axis is a move off the widget and is left to the ring.
        """
        if isinstance(widget, QTabBar):
            if direction not in _HORIZONTAL:
                return False
        elif direction in _HORIZONTAL:
            return False
        self._send_key(widget, _ARROW_KEYS[direction])
        self.refresh_ring()
        return True

    @staticmethod
    def _send_key(widget: QWidget, key, modifiers=Qt.NoModifier):
        app = QApplication.instance()
        for kind in (QEvent.KeyPress, QEvent.KeyRelease):
            app.sendEvent(widget, QKeyEvent(kind, key, modifiers))

    # -- acting -----------------------------------------------------------
    def _activate(self):
        if not self._shown or not self._alive(self._current):
            self._resume()          # never activate what is not lit up
            return
        widget = self._current
        if isinstance(widget, QAbstractButton):
            widget.click()
            return
        switch = getattr(widget, "switch", None)
        if isinstance(switch, QAbstractButton):
            switch.click()
            return
        if isinstance(widget, (QLineEdit, QTextEdit, QPlainTextEdit)):
            widget.setFocus(Qt.OtherFocusReason)
            return
        if isinstance(widget, QComboBox):
            widget.showPopup()
            return
        if isinstance(widget, (QListView, QTabBar)):
            self._send_key(widget, Qt.Key_Return)
            return
        self._click(widget)

    @staticmethod
    def _click(widget: QWidget):
        """Press and release a widget that only answers the mouse."""
        centre = widget.rect().center()
        app = QApplication.instance()
        for kind, button in ((QEvent.MouseButtonPress, Qt.LeftButton),
                             (QEvent.MouseButtonRelease, Qt.NoButton)):
            app.sendEvent(widget, QMouseEvent(
                kind, centre, widget.mapToGlobal(centre), Qt.LeftButton,
                button, Qt.NoModifier))

    def _back(self):
        app = QApplication.instance()
        popup = app.activePopupWidget() if app is not None else None
        if popup is not None:
            popup.close()
            self.refresh_ring()
            return
        modal = app.activeModalWidget() if app is not None else None
        if modal is not None:
            self._send_key(modal, Qt.Key_Escape)
            return
        if self._on_back is not None:
            self._on_back()

    def _switch_tab(self, step):
        bar = self._tab_bar()
        if bar is None or bar.count() < 2:
            return
        bar.setCurrentIndex((bar.currentIndex() + step) % bar.count())
        # The old tab's controls are gone; land on the new tab's first one
        # rather than leaving the ring on something that no longer shows.
        if self._shown:
            QTimer.singleShot(0, lambda: self._enter_current_tab(bar))

    def _tab_bar(self) -> QTabBar:
        if isinstance(self._current, QTabBar) and self._alive(self._current):
            return self._current
        for bar in self._scope().findChildren(QTabBar):
            if bar.isVisible() and bar.count():
                return bar
        return None

    def _enter_current_tab(self, bar: QTabBar):
        parent = bar.parentWidget()
        page = getattr(parent, "currentWidget", None)
        container = page() if callable(page) else None
        self.enter(container if container is not None else self._scope())

    def _resume(self):
        """Bring the ring back where it was, or start it somewhere sensible.

        The first press after the ring is hidden — at startup, or once a mouse
        movement has taken it away — only makes it visible again. It does not
        move and it does not activate, so picking the mouse up and putting it
        down again never costs the user their place.
        """
        items, _rects = self._items()
        if self._current is not None and self._current in items:
            self.show_ring(self._current)
        else:
            self.move_to_entry()

    # -- input ------------------------------------------------------------
    def ready(self) -> bool:
        """Whether the launcher should be acting on controller input at all.

        False while the window is hidden — which is what it does in Game Mode
        when it steps aside for Minecraft — and while the owner says the game
        is running.
        """
        if not self._window.isVisible() or self._window.isMinimized():
            return False
        if self._accepts_input is None:
            return True
        try:
            return bool(self._accepts_input())
        except Exception:
            return True

    def dispatch(self, action):
        """Apply one logical controller action to the window."""
        if not self.ready():
            return
        if action in ("up", "down", "left", "right"):
            if self._shown:
                self._move(action)
            else:
                self._resume()
        elif action == "accept":
            self._activate()
        elif action == "back":
            self._back()
        elif action == "start":
            if self._scope() is self._window and self._on_start is not None:
                self._on_start()
        elif action == "prev_tab":
            self._switch_tab(-1)
        elif action == "next_tab":
            self._switch_tab(1)
        elif action in ("scroll_up", "scroll_down"):
            self._scroll(action)
