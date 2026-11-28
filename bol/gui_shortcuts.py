"""bol.gui_shortcuts — the Shortcuts page and its editor (#392).

One card per shortcut: its icon, its name, what it starts (the game, the
build, the profile), where it is (the app menu, the desktop) and how it
starts. Play starts it the way its menu entry does; Edit opens the editor;
the ⋯ menu copies the command for Steam and removes it. Under the list, the
builds installed on this machine that no shortcut starts yet, one click from
having one.

The editor is a dialog: name and icon, the game, the version, the profile,
what a click does (the launcher opening and starting the game by itself, or
the game alone), the options, and where it goes.

:func:`start_pending` is the other half of a shortcut that opens the
launcher: the window it opened picks the shortcut's build next to PLAY and
presses it, once the list of builds is loaded.
"""
# SPDX-License-Identifier: MIT

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from PySide6.QtCore import QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFormLayout, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)

from . import icons, shortcuts
from .gui_profiles import (
    COMPACT_BELOW, ElidedLabel, _Card, action_button, badge,
)
from .log import BolError
from .profiles import current_profile_info, list_profiles

ICON = 48
#: How long a shortcut that opened the launcher waits for the list of
#: builds before it gives up and says so.
START_TIMEOUT_MS = 90_000


def _pixmap(item, size, art=None):
    """A shortcut's icon at ``size``, sharp on a HiDPI screen."""
    pixmap = QPixmap()
    path = shortcuts.icon_file(item)
    if art is None and path.is_file():
        pixmap.load(str(path))
    if pixmap.isNull():
        try:
            pixmap.loadFromData(shortcuts.render_icon(item, art=art))
        except Exception:  # noqa: BLE001 - an empty square, not a failure
            pixmap = QPixmap(size, size)
            pixmap.fill(Qt.transparent)
    ratio = 2
    scaled = pixmap.scaled(size * ratio, size * ratio, Qt.KeepAspectRatio,
                           Qt.SmoothTransformation)
    scaled.setDevicePixelRatio(ratio)
    return scaled


def _target(item):
    """(build it starts now or None, why not)."""
    try:
        _entry, version, _folder = shortcuts.resolve(item)
        return version, ""
    except BolError as exc:
        return None, str(exc)


def _places(item):
    places = [label for label, on in (("App menu", item["menu"]),
                                      ("Desktop", item["desktop"])) if on]
    return " · ".join(places)


def _warn_badge(text):
    label = badge(text, "quiet")
    label.setStyleSheet("color:#e0574a;")
    return label


class ShortcutCard(_Card):
    """One shortcut: what it starts, where it is, Play / Edit / ⋯."""

    def __init__(self, page, item):
        super().__init__("ProfileCard")
        self.page = page
        self.item = item
        self.setProperty("active", "false")
        body = QHBoxLayout()
        body.setSpacing(14)
        self.icon = QLabel()
        self.icon.setFixedSize(ICON, ICON)
        self.icon.setPixmap(_pixmap(item, ICON))
        body.addWidget(self.icon, 0, Qt.AlignTop)

        text = QVBoxLayout()
        text.setSpacing(3)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.name = ElidedLabel(item["name"], Qt.ElideRight)
        self.name.setObjectName("ProfileName")
        self.name.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        top.addWidget(self.name)
        version, why = _target(item)
        self.missing = why
        self.badges = []
        if version is None:
            self.badges.append(_warn_badge("Not installed"))
        if item["start"] == shortcuts.START_DIRECT:
            self.badges.append(badge("Game only", "quiet"))
        if item["editor"]:
            self.badges.append(badge("Bedrock Editor", "quiet"))
        if item["hidden"]:
            self.badges.append(badge("Hidden window", "quiet"))
        if item["update"]:
            self.badges.append(badge("Installs the newest first", "quiet"))
        for one in self.badges:
            top.addWidget(one)
        top.addStretch(1)
        text.addLayout(top)

        what = shortcuts.describe(item)
        if item["version"] == shortcuts.LATEST and version \
                and not shortcuts.single_build(item["edition"]):
            what += f" ({version})"
        self.summary = ElidedLabel(
            f"{what}  ·  Profile: {shortcuts.profile_label(item)}",
            Qt.ElideRight)
        self.summary.setObjectName("Sub")
        text.addWidget(self.summary)
        where = _places(item)
        how = ("opens the launcher" if item["start"] == shortcuts.START_LAUNCHER
               else "starts the game with no launcher window")
        self.where = ElidedLabel(
            f"In: {where}  ·  {how[0].upper() + how[1:]}" if where else
            "Not in the app menu or on the desktop any more: Edit puts "
            "it back", Qt.ElideRight)
        self.where.setObjectName("Muted")
        text.addWidget(self.where)
        body.addLayout(text, 1)
        self.finish(body)

        self.play = action_button(
            "Play", lambda: page.play(item), kind="Primary",
            tip="Start it the way its menu entry does")
        if version is None and item["start"] == shortcuts.START_DIRECT:
            self.play.setEnabled(False)
            self.play.setToolTip(why)
        elif version is None:
            self.play.setToolTip(why + " The launcher offers to install it.")
        self.flow.addWidget(self.play)
        self.edit = action_button("Edit", lambda: page.edit(item),
                                  tip="Change its name, icon, game, "
                                      "version, profile or options")
        self.flow.addWidget(self.edit)
        theme = page.window_.theme
        self.more = QPushButton()
        self.more.setObjectName("MoreBtn")
        self.more.setIcon(icons.pixmap("more", theme.fg, 18))
        self.more.setIconSize(QSize(18, 18))
        self.more.setCursor(Qt.PointingHandCursor)
        self.more.setMinimumHeight(32)
        self.more.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.more.setToolTip("More: copy the command for Steam, show the "
                             "file, remove")
        self.more.setAccessibleName(f"More actions for {item['name']}")
        self.more.clicked.connect(self.open_menu)
        self.flow.addWidget(self.more)
        self.name.setStyleSheet(f"color:{theme.fg};")

    def menu_actions(self):
        item, page = self.item, self.page
        entries = [("Copy the command for Steam",
                    lambda: page.copy_command(item), True)]
        if item.get("desktop_file") and item["desktop"]:
            entries.append(("Show it on the desktop folder",
                            lambda: page.show_file(item["desktop_file"]),
                            True))
        entries.append(("Remove…", lambda: page.remove(item), True))
        return entries

    def open_menu(self):
        menu = QMenu(self)
        for label, slot, enabled in self.menu_actions():
            if label == "Remove…":
                menu.addSeparator()
            action = menu.addAction(label)
            action.setEnabled(enabled)
            action.triggered.connect(lambda _checked=False, s=slot: s())
        menu.exec(self.more.mapToGlobal(QPoint(0, self.more.height() + 4)))


class SuggestionRow(_Card):
    """A build installed here that no shortcut starts yet."""

    def __init__(self, page, edition_id, version):
        super().__init__("ProfileCard")
        self.setProperty("active", "false")
        self.edition_id = edition_id
        self.version = version
        body = QHBoxLayout()
        body.setSpacing(12)
        preview = dict(shortcuts.DEFAULTS, id="preview", edition=edition_id,
                       version=version)
        art = QLabel()
        art.setFixedSize(32, 32)
        try:
            art.setPixmap(_pixmap(preview, 32,
                                  art=shortcuts._art_bytes(preview)))
        except Exception:  # noqa: BLE001
            pass
        body.addWidget(art)
        self.label = ElidedLabel(
            shortcuts.default_name(edition_id, version), Qt.ElideRight)
        self.label.setObjectName("Sub")
        body.addWidget(self.label, 1)
        self.finish(body)
        self.add = action_button(
            "Add shortcut", lambda: page.add(edition_id, version),
            tip="Put a shortcut to it in the app menu; Edit changes it "
                "afterwards")
        self.flow.addWidget(self.add)


class ShortcutsPage(QFrame):
    """The page; see the module docstring."""

    def __init__(self, window):
        super().__init__()
        self.setObjectName("Page")
        self.window_ = window
        self.cards = []
        self.suggestions = []
        self._compact = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)
        head = QHBoxLayout()
        head.setSpacing(12)
        self.intro = QLabel(
            "Each shortcut starts one Minecraft, as you set it up here: the "
            "game, the version, the profile and how it starts. It appears "
            "in your app menu or on your desktop.")
        self.intro.setObjectName("Sub")
        self.intro.setWordWrap(True)
        head.addWidget(self.intro, 1)
        self.new_button = action_button(
            "New shortcut", self.new, kind="Primary",
            tip="Make a shortcut to a game, a version and a profile")
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

    # -- data ---------------------------------------------------------------
    def _items(self):
        try:
            return shortcuts.load(), ""
        except BolError as exc:
            return [], str(exc)

    def _missing(self, items):
        """(edition, version) installed here with no shortcut to it."""
        from .games import list_editions
        covered = {(i["edition"], i["version"]) for i in items}
        found = []
        for entry in list_editions(True):
            try:
                builds = shortcuts.installed(entry["id"])
            except Exception:  # noqa: BLE001
                continue
            if not builds:
                continue
            if (entry["id"], shortcuts.LATEST) not in covered:
                found.append((entry["id"], shortcuts.LATEST))
            if shortcuts.single_build(entry["id"]):
                continue
            found += [(entry["id"], version) for version in builds
                      if (entry["id"], version) not in covered]
        return found

    # -- painting -----------------------------------------------------------
    def opened(self):
        self.refresh()

    def _clear(self):
        while self.column.count():
            child = self.column.takeAt(0)
            widget = child.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self.cards = []
        self.suggestions = []

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

    def refresh(self):
        self._clear()
        items, problem = self._items()
        if problem:
            note = QLabel(problem)
            note.setObjectName("Warn")
            note.setWordWrap(True)
            self.column.addWidget(note)
        self._section("Your shortcuts", len(items))
        if items:
            for item in items:
                card = ShortcutCard(self, item)
                self.cards.append(card)
                self.column.addWidget(card)
        else:
            self.empty = self._hint(
                "No shortcut yet",
                "A shortcut starts one Minecraft straight from your app menu "
                "or your desktop: the newest Minecraft, Preview, an older "
                "build you keep for a server, Education, the Windows 10 "
                "edition, another profile's game.",
                action_button("New shortcut", self.new))
        if shortcuts.in_flatpak():
            note = QLabel(
                "The Flatpak asks your desktop to add each shortcut, and it "
                "asks you to confirm. They go to the app menu; from there "
                "most desktops can put one on the desktop or a panel.")
            note.setObjectName("Muted")
            note.setWordWrap(True)
            self.column.addWidget(note)
        missing = self._missing(items)
        if missing:
            self._section("Installed, no shortcut yet", len(missing))
            for edition_id, version in missing:
                row = SuggestionRow(self, edition_id, version)
                self.suggestions.append(row)
                self.column.addWidget(row)
        self.column.addStretch(1)
        self._apply_compact(force=True)

    def _apply_compact(self, force=False):
        compact = self.area.viewport().width() < COMPACT_BELOW
        if not force and compact == self._compact:
            return
        self._compact = compact
        for card in self.cards + self.suggestions:
            card.set_compact(compact)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_compact()

    # -- actions ------------------------------------------------------------
    def new(self, preset=None):
        return self._edit(ShortcutDialog(self.window_, preset=preset))

    def edit(self, item):
        return self._edit(ShortcutDialog(self.window_, item=item))

    def _edit(self, dialog):
        accepted = dialog.exec() == QDialog.Accepted
        saved = dialog.saved
        dialog.deleteLater()
        if accepted:
            self.refresh()
        return saved

    def add(self, edition_id, version):
        try:
            shortcuts.create({"edition": edition_id, "version": version})
        except BolError as exc:
            self.window_.error_box("Add shortcut", str(exc))
        self.refresh()

    def remove(self, item):
        if not self.window_.question_box(
                "Remove shortcut",
                f"Remove '{item['name']}' from the app menu and the "
                "desktop? The game, its worlds and its settings stay."):
            return
        try:
            shortcuts.remove(item["id"])
        except BolError as exc:
            self.window_.error_box("Remove shortcut", str(exc))
        self.refresh()

    def copy_command(self, item):
        command = shortcuts.steam_command(item)
        QGuiApplication.clipboard().setText(command)
        self.window_.info_box(
            "Command for Steam",
            "Copied:\n\n" + command + "\n\nIn Steam, \"Add a Non-Steam "
            "Game\", then put this in the shortcut's properties: the first "
            "word as Target, the rest as Launch Options.")

    def show_file(self, path):
        try:
            subprocess.Popen(["xdg-open", str(Path(path).parent)],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError as exc:
            self.window_.error_box("Show file", str(exc))

    def play(self, item):
        """What the menu entry does. A shortcut that opens the launcher, in
        this window's own profile, starts here instead of in a second
        window."""
        here = current_profile_info().get("slug") or "default"
        mine = (item["profile"] or "default") == here
        if item["start"] == shortcuts.START_LAUNCHER and mine:
            start_in_window(self.window_, item["id"])
            return
        try:
            subprocess.Popen(
                shortcuts.launcher_argv()
                + ["shortcuts", "run", item["id"]],
                start_new_session=True, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            self.window_.error_box("Play", str(exc))


class _ChoiceCard(QFrame):
    """One of two answers, picked by a click, Space or Enter."""

    picked = Signal()

    def __init__(self, title, text):
        super().__init__()
        self.setObjectName("ProfileCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(2)
        self._title = title
        self.title = QLabel()
        self.title.setObjectName("ProfileName")
        layout.addWidget(self.title)
        body = QLabel(text)
        body.setObjectName("Sub")
        body.setWordWrap(True)
        layout.addWidget(body)
        self.set_on(False)

    def set_on(self, on):
        self.on = on
        self.title.setText(("●  " if on else "○  ") + self._title)
        self.setProperty("active", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event):  # noqa: N802 - Qt override
        self.picked.emit()
        event.accept()

    def keyPressEvent(self, event):  # noqa: N802 - Qt override
        if event.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            self.picked.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class ShortcutDialog(QDialog):
    """Make or change a shortcut."""

    def __init__(self, window, item=None, preset=None):
        super().__init__(window)
        self.window_ = window
        self.item = dict(item) if item else None
        self.saved = None
        start = dict(shortcuts.DEFAULTS)
        start.update(preset or {})
        if item:
            start.update(item)
        self.picture = None          # a picture chosen in this dialog
        self.icon_kind = start["icon"]
        self.setWindowTitle("Edit shortcut" if item else "New shortcut")
        self.setStyleSheet(window.theme.qss())
        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 20, 22, 18)
        outer.setSpacing(12)
        title = QLabel(self.windowTitle())
        title.setObjectName("Title")
        outer.addWidget(title)

        top = QHBoxLayout()
        top.setSpacing(16)
        icon_column = QVBoxLayout()
        icon_column.setSpacing(6)
        self.icon = QLabel()
        self.icon.setFixedSize(96, 96)
        icon_column.addWidget(self.icon, 0, Qt.AlignHCenter)
        self.icon_button = action_button("Change icon…", self._icon_menu)
        icon_column.addWidget(self.icon_button)
        icon_column.addStretch(1)
        top.addLayout(icon_column)

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.name = QLineEdit(start["name"])
        self.name.setMaxLength(80)
        form.addRow("Name", self.name)
        self.game = QComboBox()
        from .games import list_editions
        for entry in list_editions(True):
            label = shortcuts.game_name(entry["id"])
            if not shortcuts.installed(entry["id"]):
                label += "  (not installed)"
            self.game.addItem(label, entry["id"])
        index = self.game.findData(start["edition"])
        self.game.setCurrentIndex(max(0, index))
        form.addRow("Game", self.game)
        self.version = QComboBox()
        form.addRow("Version", self.version)
        self.profile = QComboBox()
        self.profile.addItem("Default", "")
        for one in list_profiles():
            self.profile.addItem(str(one.get("name")), one.get("slug"))
        self.profile.setCurrentIndex(
            max(0, self.profile.findData(start["profile"] or "")))
        form.addRow("Profile", self.profile)
        top.addLayout(form, 1)
        outer.addLayout(top)

        outer.addWidget(self._section("When you open it"))
        row = QHBoxLayout()
        row.setSpacing(10)
        self.by_launcher = _ChoiceCard(
            "Open the launcher",
            "It starts the game by itself. You see the progress, the "
            "sign-in and any error.")
        self.game_only = _ChoiceCard(
            "Start the game only",
            "No launcher window. An error shows as a desktop notification.")
        self.by_launcher.picked.connect(
            lambda: self._set_start(shortcuts.START_LAUNCHER))
        self.game_only.picked.connect(
            lambda: self._set_start(shortcuts.START_DIRECT))
        row.addWidget(self.by_launcher, 1)
        row.addWidget(self.game_only, 1)
        outer.addLayout(row)
        self._set_start(start["start"])

        outer.addWidget(self._section("Options"))
        # Not window._switch(): that list outlives this dialog, and a theme
        # change would repaint switches that are gone.
        from .gui import SwitchRow  # noqa: PLC0415 - gui imports this module

        def switch(text, on, tip=None):
            return SwitchRow(text, on, tip, theme=window.theme)
        grid = QGridLayout()
        grid.setHorizontalSpacing(28)
        grid.setVerticalSpacing(6)
        self.editor = switch(
            "Open Bedrock Editor instead of the game", start["editor"])
        self.hidden = switch(
            "Keep the game window hidden (for tools)", start["hidden"],
            tip="The game runs with its window off the screen, for a tool "
                "that drives it; show it from Profiles ▸ Running now")
        self.update_first = switch(
            "Install the newest build first", start["update"],
            tip="Before starting, install the newest build of this game if "
                "a newer one is out. Only for Newest installed.")
        self.badge = switch(
            "Write the version on the icon", start["badge"],
            tip="So that shortcuts to the same game can be told apart in "
                "the menu")
        grid.addWidget(self.editor, 0, 0)
        grid.addWidget(self.hidden, 0, 1)
        grid.addWidget(self.update_first, 1, 0)
        grid.addWidget(self.badge, 1, 1)
        outer.addLayout(grid)
        env_row = QHBoxLayout()
        env_label = QLabel("Environment variables")
        env_label.setObjectName("Sub")
        env_row.addWidget(env_label)
        self.env = QLineEdit(start["env"])
        self.env.setPlaceholderText("e.g. MANGOHUD=1 DXVK_HUD=fps")
        self.env.setToolTip("Set for this shortcut's game, as NAME=value "
                            "separated by spaces. Settings ▸ Advanced's own "
                            "variables apply too, and win when both set "
                            "the same one.")
        env_row.addWidget(self.env, 1)
        outer.addLayout(env_row)

        outer.addWidget(self._section("Put it in"))
        places = QGridLayout()
        places.setHorizontalSpacing(28)
        self.menu = switch("App menu", start["menu"])
        self.desktop = switch("Desktop", start["desktop"])
        if shortcuts.in_flatpak():
            self.desktop.setEnabled(False)
            self.desktop.switch.setChecked(False)
            self.desktop.setToolTip(
                "The Flatpak can only ask the desktop for an app menu "
                "entry; from the menu, most desktops can put it on the "
                "desktop.")
        places.addWidget(self.menu, 0, 0)
        places.addWidget(self.desktop, 0, 1)
        outer.addLayout(places)

        self.error = QLabel()
        self.error.setObjectName("Warn")
        self.error.setWordWrap(True)
        self.error.hide()
        outer.addWidget(self.error)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel = action_button("Cancel", self.reject)
        buttons.addWidget(self.cancel)
        self.save = action_button("Save" if item else "Create shortcut",
                                  self.accept_changes, kind="Primary")
        self.save.setDefault(True)
        buttons.addWidget(self.save)
        outer.addLayout(buttons)

        self._auto_name = (not start["name"]) or start["name"] == \
            shortcuts.default_name(start["edition"], start["version"])
        self._fill_versions(start["version"])
        self.game.currentIndexChanged.connect(lambda _i: self._game_changed())
        self.version.currentIndexChanged.connect(
            lambda _i: self._target_changed())
        self.name.textEdited.connect(self._name_edited)
        self.badge.toggled.connect(lambda _on: self._paint_icon())
        self._target_changed()
        self.resize(720, 640)

    # -- pieces -------------------------------------------------------------
    @staticmethod
    def _section(text):
        label = QLabel(text.upper())
        label.setObjectName("SectionTitle")
        return label

    def _set_start(self, start):
        self.start = start
        self.by_launcher.set_on(start == shortcuts.START_LAUNCHER)
        self.game_only.set_on(start == shortcuts.START_DIRECT)

    def edition_id(self):
        return self.game.currentData()

    def version_value(self):
        return self.version.currentData() or shortcuts.LATEST

    def _fill_versions(self, wanted):
        edition_id = self.edition_id()
        builds = shortcuts.installed(edition_id)
        self.version.blockSignals(True)
        self.version.clear()
        if shortcuts.single_build(edition_id):
            self.version.addItem("From the Microsoft Store", shortcuts.LATEST)
        else:
            newest = f" ({builds[0]})" if builds else " (none yet)"
            self.version.addItem("Newest installed" + newest,
                                 shortcuts.LATEST)
            for version in builds:
                self.version.addItem(version, version)
            if wanted not in (None, shortcuts.LATEST) and wanted not in builds:
                self.version.addItem(f"{wanted}  (not installed)", wanted)
        self.version.setCurrentIndex(max(0, self.version.findData(wanted)))
        self.version.setEnabled(not shortcuts.single_build(edition_id))
        self.version.blockSignals(False)

    def _game_changed(self):
        self._fill_versions(shortcuts.LATEST)
        self._target_changed()

    def _target_changed(self):
        from .games import is_bedrock
        edition_id = self.edition_id()
        bedrock = is_bedrock(shortcuts.edition(edition_id))
        self.editor.setEnabled(bedrock)
        if not bedrock:
            self.editor.switch.setChecked(False)
        latest = self.version_value() == shortcuts.LATEST \
            and not shortcuts.single_build(edition_id)
        self.update_first.setEnabled(latest)
        if not latest:
            self.update_first.switch.setChecked(False)
        if self._auto_name:
            self.name.setText(shortcuts.default_name(edition_id,
                                                     self.version_value()))
        self._paint_icon()

    def _name_edited(self, text):
        self._auto_name = not text.strip()

    def _preview_item(self):
        item = dict(shortcuts.DEFAULTS)
        item.update(self.item or {})
        item.update(id=(self.item or {}).get("id", "preview"),
                    edition=self.edition_id(), version=self.version_value(),
                    badge=self.badge.isChecked(), icon=self.icon_kind)
        return item

    def _paint_icon(self):
        item = self._preview_item()
        art = None
        if self.picture is not None:
            try:
                art = Path(self.picture).read_bytes()
            except OSError:
                art = None
        try:
            if art is None:
                art = shortcuts._art_bytes(item)
            self.icon.setPixmap(_pixmap(item, 96, art=art or b""))
        except Exception:  # noqa: BLE001 - no preview, still a shortcut
            self.icon.clear()

    def _icon_menu(self):
        menu = QMenu(self)
        for label, kind in (("The game's own picture", shortcuts.ICON_EDITION),
                            ("The launcher's icon", shortcuts.ICON_LAUNCHER),
                            ("A picture of your own…", shortcuts.ICON_CUSTOM)):
            action = menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, k=kind: self._pick_icon(k))
        menu.exec(self.icon_button.mapToGlobal(
            QPoint(0, self.icon_button.height() + 4)))

    def _pick_icon(self, kind):
        if kind == shortcuts.ICON_CUSTOM:
            path, _filter = QFileDialog.getOpenFileName(
                self, "Choose a picture", str(Path.home()),
                "Pictures (*.png *.jpg *.jpeg *.svg *.webp *.bmp *.ico)")
            if not path:
                return
            self.picture = path
        else:
            self.picture = None
        self.icon_kind = kind
        self._paint_icon()

    def fields(self):
        return {
            "name": self.name.text(),
            "edition": self.edition_id(),
            "version": self.version_value(),
            "profile": self.profile.currentData() or "",
            "start": self.start,
            "editor": self.editor.isChecked(),
            "hidden": self.hidden.isChecked(),
            "update": self.update_first.isChecked(),
            "env": self.env.text(),
            "icon": self.icon_kind if self.picture is None
            else shortcuts.ICON_CUSTOM,
            "badge": self.badge.isChecked(),
            "menu": self.menu.isChecked(),
            "desktop": self.desktop.isChecked(),
        }

    def accept_changes(self):
        fields = self.fields()
        try:
            if self.item:
                self.saved = shortcuts.edit(self.item["id"], fields,
                                            icon_picture=self.picture)
            else:
                self.saved = shortcuts.create(fields,
                                              icon_picture=self.picture)
        except BolError as exc:
            self.error.setText(str(exc))
            self.error.show()
            return
        self.accept()


# ---------------------------------------------------------------- starting

def start_pending(window, environ=None):
    """Start the shortcut that opened this launcher, if one did."""
    env = os.environ if environ is None else environ
    ident = env.pop(shortcuts.START_ENV, None)
    if not ident:
        return False
    QTimer.singleShot(0, lambda: start_in_window(window, ident))
    return True


def start_in_window(window, ident):
    try:
        item = shortcuts.get(ident)
    except BolError as exc:
        window.error_box("Shortcut", str(exc))
        return None
    starter = _Starter(window, item)
    window._shortcut_starter = starter
    starter.begin()
    return starter


class _Starter:
    """Pick a shortcut's build next to PLAY, then press it."""

    def __init__(self, window, item):
        self.window = window
        self.item = item
        self.timer = QTimer(window)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self._try)
        self.waited = 0
        self.saved_env = {}

    def begin(self):
        self.window.go_home()
        self.window.set_status(f"Starting '{self.item['name']}'…")
        self.timer.start()
        self._try()

    def _listed(self, edition_id):
        return [v for v in self.window.ui_state.get("versions") or []
                if _edition_of(v) == edition_id]

    def _try(self):
        window = self.window
        if window.ui_state.get("busy") or window.ui_state.get("launch_active"):
            return self._wait_more()
        listed = self._listed(self.item["edition"])
        installed_here = shortcuts.installed(self.item["edition"])
        if not listed and not installed_here:
            return self._wait_more()
        if not any(v.get("installed") for v in listed) and installed_here \
                and self.waited < 20_000:
            # The picker lists the catalogue first; a build on disk shows
            # once the list is complete.
            return self._wait_more()
        self.timer.stop()
        version = self._version(listed, installed_here)
        if version is None:
            window.set_status("Ready to play.")
            return
        if not window.select_build(version, self.item["edition"] == "preview",
                                   self.item["edition"]):
            window.error_box(
                "Shortcut",
                f"{shortcuts.game_name(self.item['edition'])} {version} is "
                "not in the list of versions next to PLAY.")
            window.set_status("Ready to play.")
            return
        self._apply_env()
        window.do_play(editor=self.item["editor"], hidden=self.item["hidden"])
        worker = window._workers.get("play")
        if window.ui_state.get("launch_active") and worker is not None:
            worker.finished.connect(self._restore_env)
        else:
            self._restore_env()

    def _wait_more(self):
        self.waited += self.timer.interval()
        if self.waited >= START_TIMEOUT_MS:
            self.timer.stop()
            self.window.error_box(
                "Shortcut",
                f"'{self.item['name']}' could not start: the list of "
                "Minecraft versions did not load. Check the connection, "
                "then press PLAY.")
            self.window.set_status("Ready to play.")
        return None

    def _version(self, listed, installed_here):
        """The build to pick, asking before anything is downloaded."""
        item, window = self.item, self.window
        name = shortcuts.game_name(item["edition"])
        if item["version"] != shortcuts.LATEST:
            if item["version"] in installed_here:
                return item["version"]
            if any(v.get("tag") == item["version"] for v in listed) and \
                    window.question_box(
                        "Install it?",
                        f"{name} {item['version']}, which the shortcut "
                        f"'{item['name']}' starts, is not installed any more."
                        "\n\nDownload and install it now?"):
                return item["version"]
            if not any(v.get("tag") == item["version"] for v in listed):
                window.error_box("Shortcut", f"{name} {item['version']} is "
                                 "not installed, and can no longer be "
                                 "downloaded.")
            return None
        from .gamesetup import AUTO_UPDATE_SETTING
        from .util import load_settings
        newest = listed[0].get("tag") if listed else None
        wants_newest = item["update"] or \
            load_settings().get(AUTO_UPDATE_SETTING, False)
        if installed_here and not (wants_newest and newest):
            return installed_here[0]
        if installed_here:
            return newest
        if newest and window.question_box(
                "Install it?",
                f"{name} is not installed yet.\n\nDownload and install "
                "the newest build now?"):
            return newest
        return None

    def _apply_env(self):
        for key, value in shortcuts.environment(self.item).items():
            self.saved_env[key] = os.environ.get(key)
            os.environ[key] = value

    def _restore_env(self):
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.saved_env = {}


def _edition_of(entry):
    edition = (entry or {}).get("edition")
    if isinstance(edition, dict):
        return edition.get("id") or "release"
    if isinstance(edition, str) and edition:
        return edition
    return "preview" if (entry or {}).get("beta") else "release"
