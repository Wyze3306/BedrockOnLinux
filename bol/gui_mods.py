"""bol.gui_mods — the Mods page: LeviLamina and the mods levimc.org lists.

The page works on one Minecraft build at a time, the way LeviLamina does:
each build has its own mods folder, and LeviLamina itself is made for one
build. Everything that touches the network or the disk runs in a worker
(``MainWindow._start_worker``); the page only paints what comes back.
"""
# SPDX-License-Identifier: MIT

from __future__ import annotations

import datetime
import re
import subprocess
from pathlib import Path

from PySide6.QtCore import QObject, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QPainter, QPainterPath, QPalette,
                           QPixmap, QTextDocument)
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QProgressBar, QPushButton, QScrollArea,
    QSizePolicy, QTabWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from . import mods

LOGO_SIZE = 52


class _Bridge(QObject):
    """Log lines from a worker thread, delivered on the UI thread."""
    line = Signal(str)


class LogoLoader(QThread):
    """Fetch the logos of a list of mods, one signal per logo."""
    loaded = Signal(str, bytes)

    def __init__(self, urls):
        super().__init__()
        self._urls = list(dict.fromkeys(u for u in urls if u))

    def run(self):
        for url in self._urls:
            if self.isInterruptionRequested():
                return
            data = mods.logo(url)
            if data:
                self.loaded.emit(url, data)


def _initials_pixmap(name, color, size=LOGO_SIZE):
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, size, size, size * 0.24, size * 0.24)
    painter.fillPath(path, QColor(color))
    font = QFont()
    font.setBold(True)
    font.setPixelSize(int(size * 0.42))
    painter.setFont(font)
    painter.setPen(QColor("white"))
    letters = "".join(w[0] for w in str(name or "?").replace("-", " ")
                      .split()[:2]).upper() or "?"
    painter.drawText(pixmap.rect(), Qt.AlignCenter, letters[:2])
    painter.end()
    return pixmap


def _rounded(pixmap, size=LOGO_SIZE):
    scaled = pixmap.scaled(size, size, Qt.KeepAspectRatio,
                           Qt.SmoothTransformation)
    out = QPixmap(size, size)
    out.fill(Qt.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, size, size, size * 0.24, size * 0.24)
    painter.setClipPath(path)
    painter.drawPixmap((size - scaled.width()) // 2,
                       (size - scaled.height()) // 2, scaled)
    painter.end()
    return out


def _ago(stamp):
    try:
        when = datetime.datetime.fromisoformat(str(stamp).replace("Z",
                                                                  "+00:00"))
    except ValueError:
        return ""
    days = (datetime.datetime.now(datetime.timezone.utc) - when).days
    if days < 1:
        return "today"
    if days < 2:
        return "yesterday"
    if days < 31:
        return f"{days} days ago"
    if days < 365:
        return f"{days // 30} months ago"
    return f"{days // 365} years ago"


def _tags(mod):
    out = []
    for tag in mod.get("tags") or []:
        tag = str(tag)
        if tag.startswith(("platform:", "type:")):
            continue
        out.append(tag.split(":", 1)[-1].replace("-", " "))
    return list(dict.fromkeys(out))


def _pretty_build(build):
    """1.26.60.05 -> 1.26.60.5, as the launcher writes versions."""
    return ".".join(str(int(p)) for p in re.findall(r"\d+", str(build)))


def _readable_markdown(text):
    """A README as Qt can show it: no remote images (badges, logos), which
    a text browser cannot fetch and shows as broken icons, and no raw HTML
    layout tags."""
    if not text:
        return ""
    out, fenced = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            out.append(line)
            continue
        if not fenced:
            line = re.sub(r"\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)", "", line)
            line = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line)
            line = re.sub(r"</?(img|p|div|picture|source|br|a|h\d|span|"
                          r"center|sup|sub|details|summary)\b[^>]*>", "",
                          line, flags=re.I)
            if not line.strip() and out and not out[-1].strip():
                continue
        out.append(line)
    return "\n".join(out).strip()


def _chip(text, color=None):
    label = QLabel(text)
    label.setObjectName("Chip")
    if color:
        label.setStyleSheet(f"color:{color}; font-size:10px; padding:1px 8px;")
    else:
        label.setStyleSheet("font-size:10px; padding:1px 8px;")
    return label


class ModCard(QFrame):
    """One mod of the catalogue."""

    def __init__(self, page, mod):
        super().__init__()
        self.page = page
        self.mod = mod
        self.setObjectName("CardFlat")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(122)
        self.setMinimumWidth(320)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(12)

        self.logo = QLabel()
        self.logo.setFixedSize(LOGO_SIZE, LOGO_SIZE)
        self.logo.setAlignment(Qt.AlignTop)
        row.addWidget(self.logo, 0, Qt.AlignTop)
        self.set_logo(page.logo_for(mod))

        body = QVBoxLayout()
        body.setSpacing(2)
        head = QHBoxLayout()
        head.setSpacing(6)
        name = QLabel(mod.get("name") or mod["id"].rsplit("/", 1)[-1])
        name.setStyleSheet("font-weight:700; font-size:14px;")
        head.addWidget(name)
        owner = QLabel(f"by {mod.get('owner') or '?'}")
        owner.setObjectName("Muted")
        head.addWidget(owner)
        head.addStretch(1)
        body.addLayout(head)

        desc = QLabel(mod.get("description") or "")
        desc.setObjectName("Sub")
        desc.setWordWrap(True)
        desc.setStyleSheet("font-size:12px;")
        desc.setMaximumHeight(34)
        desc.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        desc.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        body.addWidget(desc, 1)

        meta = QHBoxLayout()
        meta.setSpacing(5)
        for tag in _tags(mod)[:3]:
            meta.addWidget(_chip(tag))
        stars = QLabel(f"★ {mod.get('stars', 0)}   ·   "
                       f"updated {_ago(mod.get('updated'))}")
        stars.setObjectName("Muted")
        meta.addWidget(stars)
        meta.addStretch(1)
        body.addLayout(meta)
        row.addLayout(body, 1)

        side = QVBoxLayout()
        side.setSpacing(6)
        self.action = QPushButton()
        self.action.setCursor(Qt.PointingHandCursor)
        self.action.setFixedSize(96, 30)
        self.action.clicked.connect(self._act)
        side.addWidget(self.action, 0, Qt.AlignRight)
        self.badge = QLabel()
        self.badge.setObjectName("Muted")
        self.badge.setAlignment(Qt.AlignRight)
        side.addWidget(self.badge, 0, Qt.AlignRight)
        side.addStretch(1)
        row.addLayout(side)
        self.refresh()

    def set_logo(self, pixmap):
        self.logo.setPixmap(pixmap)

    def refresh(self):
        page = self.page
        installed = page.installed_ids.get(self.mod["id"])
        compatible = page.compatible(self.mod)
        busy = page.busy
        if installed:
            self.action.setText("✓ Installed")
            self.action.setObjectName("Ghost")
            self.action.setEnabled(not busy)
            self.action.setToolTip("Installed in this build. Click for "
                                   "details.")
        elif not page.state.get("supported"):
            self.action.setText("Install")
            self.action.setObjectName("Ghost")
            self.action.setEnabled(False)
            self.action.setToolTip("LeviLamina does not support this "
                                   "Minecraft build.")
        else:
            self.action.setText("Install")
            self.action.setObjectName("Primary")
            self.action.setEnabled(not busy)
            self.action.setToolTip("Install this mod (and LeviLamina, if "
                                   "needed) into this build.")
        self.action.style().unpolish(self.action)
        self.action.style().polish(self.action)
        if installed:
            self.badge.setText(f"v{installed}")
        elif compatible:
            self.badge.setText("for " + mods.mc_line(
                page.state.get("target_version")
                or page.state.get("game_version")))
        else:
            lines = self.mod.get("gameVersions") or []
            self.badge.setText(f"made for {lines[0]}" if lines else "")
            self.badge.setStyleSheet(f"color:{page.win.theme.gold};"
                                     "font-size:11px;")

    def _act(self):
        if self.page.installed_ids.get(self.mod["id"]):
            self.page.open_details(self.mod)
        else:
            self.page.install([self.mod["id"]], self.mod.get("name"))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.page.open_details(self.mod)
        super().mousePressEvent(event)


class InstalledRow(QFrame):
    """One folder of mods/."""

    def __init__(self, page, item):
        super().__init__()
        self.page = page
        self.item = item
        self.setObjectName("CardFlat")
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 12, 8)
        row.setSpacing(10)

        logo = QLabel()
        logo.setFixedSize(36, 36)
        mod = page.catalogue_by_id.get(item.get("id") or "")
        if mod:
            pixmap = page.logo_for(mod, 36)
        else:
            pixmap = _initials_pixmap(item["name"], page.win.theme.blue, 36)
        logo.setPixmap(pixmap)
        row.addWidget(logo)

        text = QVBoxLayout()
        text.setSpacing(1)
        name = QLabel(item["name"])
        name.setStyleSheet("font-weight:700;")
        text.addWidget(name)
        detail = []
        if item.get("version"):
            detail.append(f"v{item['version']}")
        detail.append("from levimc.org" if item["source"] == "catalogue"
                      else "imported")
        if item.get("core"):
            detail.append("the mod loader")
        elif item["source"] == "catalogue" and not item.get("explicit"):
            detail.append("needed by another mod")
        sub = QLabel("  ·  ".join(detail))
        sub.setObjectName("Muted")
        text.addWidget(sub)
        if item.get("blocked"):
            blocked = QLabel(f"Switched off: {item['blocked']}.")
            blocked.setWordWrap(True)
            blocked.setStyleSheet(f"color:{page.win.theme.red};"
                                  "font-size:11px;")
            text.addWidget(blocked)
        row.addLayout(text, 1)

        if not item.get("core"):
            from .gui import SwitchRow  # noqa: PLC0415 - avoids a cycle
            # Not MainWindow._switch: these rows are rebuilt on every
            # refresh, and that list keeps what it is given for repaints.
            switch = SwitchRow("Enabled" if item["enabled"] else "Disabled",
                               item["enabled"],
                               "Load this mod when the game starts",
                               theme=page.win.theme)
            switch.setFixedWidth(118)
            switch.switch.toggled.connect(
                lambda on, s=switch: self._toggle(on, s))
            switch.setEnabled(not page.busy)
            row.addWidget(switch)
        folder = QPushButton("Folder")
        folder.setObjectName("GhostSmall")
        folder.setCursor(Qt.PointingHandCursor)
        folder.clicked.connect(lambda: page.open_path(item["path"]))
        row.addWidget(folder)
        if not item.get("core"):
            remove = QPushButton("Remove")
            remove.setObjectName("DangerSmall")
            remove.setCursor(Qt.PointingHandCursor)
            remove.setEnabled(not page.busy)
            remove.clicked.connect(lambda: page.remove(item))
            row.addWidget(remove)

    def _toggle(self, on, switch):
        switch.label.setText("Enabled" if on else "Disabled")
        self.page.set_mod_enabled(self.item, on)


class ModDetails(QDialog):
    """A mod's page: description, README, versions, links."""

    def __init__(self, page, mod):
        super().__init__(page.win)
        self.page = page
        self.mod = mod
        self.setWindowTitle(mod.get("name") or mod["id"])
        self.resize(760, 560)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 16)
        outer.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(14)
        self.logo = QLabel()
        self.logo.setPixmap(page.logo_for(mod, 72))
        head.addWidget(self.logo, 0, Qt.AlignTop)
        titles = QVBoxLayout()
        titles.setSpacing(3)
        title = QLabel(mod.get("name") or mod["id"])
        title.setObjectName("Title")
        title.setStyleSheet("font-size:20px;")
        titles.addWidget(title)
        owner = QLabel(f"by {mod.get('owner') or '?'}   ·   ★ "
                       f"{mod.get('stars', 0)}   ·   updated "
                       f"{_ago(mod.get('updated'))}"
                       + (f"   ·   {mod['license']}" if mod.get("license")
                          else ""))
        owner.setObjectName("Muted")
        titles.addWidget(owner)
        desc = QLabel(mod.get("description") or "")
        desc.setWordWrap(True)
        desc.setObjectName("Sub")
        titles.addWidget(desc)
        chips = QHBoxLayout()
        chips.setSpacing(5)
        for tag in _tags(mod)[:6]:
            chips.addWidget(_chip(tag))
        lines = mod.get("gameVersions") or []
        if lines:
            chips.addWidget(_chip("Minecraft " + ", ".join(lines[:3]),
                                  page.win.theme.accent))
        chips.addStretch(1)
        titles.addLayout(chips)
        head.addLayout(titles, 1)

        actions = QVBoxLayout()
        actions.setSpacing(6)
        self.install_btn = QPushButton()
        self.install_btn.setFixedSize(130, 34)
        self.install_btn.setCursor(Qt.PointingHandCursor)
        self.install_btn.clicked.connect(self._install_or_remove)
        actions.addWidget(self.install_btn)
        for text, url in (("GitHub", f"https://{mod['id']}"),
                          ("levimc.org", mod.get("url") or mods.SITE_URL)):
            link = QPushButton(text)
            link.setObjectName("GhostSmall")
            link.setCursor(Qt.PointingHandCursor)
            link.clicked.connect(lambda _=False, u=url: page.open_url(u))
            actions.addWidget(link)
        actions.addStretch(1)
        head.addLayout(actions)
        outer.addLayout(head)

        self.info = QLabel("Loading the mod's page…")
        self.info.setObjectName("Muted")
        self.info.setWordWrap(True)
        outer.addWidget(self.info)
        self.readme = QTextBrowser()
        self.readme.setOpenExternalLinks(True)
        theme = page.win.theme
        palette = self.readme.palette()
        palette.setColor(QPalette.Text, QColor(theme.fg))
        palette.setColor(QPalette.Base, QColor(theme.card))
        palette.setColor(QPalette.Link, QColor(theme.accent))
        self.readme.setPalette(palette)
        self.readme.viewport().setPalette(palette)
        self.readme.setStyleSheet(
            f"QTextBrowser {{ color: {theme.fg}; background: {theme.card};"
            f" border: 1px solid {theme.border}; border-radius: 10px;"
            " padding: 10px; }")
        outer.addWidget(self.readme, 1)

        foot = QHBoxLayout()
        foot.addStretch(1)
        close = QPushButton("Close")
        close.setObjectName("Ghost")
        close.setFixedHeight(30)
        close.clicked.connect(self.close)
        foot.addWidget(close)
        outer.addLayout(foot)
        self.refresh()
        page.load_detail(mod["id"], self._show_detail)

    def refresh(self):
        installed = self.page.installed_ids.get(self.mod["id"])
        if installed:
            self.install_btn.setText("Remove")
            self.install_btn.setObjectName("Danger")
        else:
            self.install_btn.setText("Install")
            self.install_btn.setObjectName("Primary")
        self.install_btn.setEnabled(not self.page.busy and (
            bool(installed) or bool(self.page.state.get("supported"))))
        self.install_btn.style().unpolish(self.install_btn)
        self.install_btn.style().polish(self.install_btn)

    def _install_or_remove(self):
        if self.page.installed_ids.get(self.mod["id"]):
            self.page.uninstall(self.mod["id"], self.mod.get("name"))
        else:
            self.page.install([self.mod["id"]], self.mod.get("name"))

    def _show_detail(self, document):
        if document is None:
            self.info.setText("levimc.org has no page for this mod.")
            return
        versions = document.get("versions") or []
        latest = (document.get("latest") or {}).get("version")
        game = self.page.state.get("game_version")
        parts = []
        if latest:
            parts.append(f"Latest version {latest}")
        parts.append(f"{len(versions)} versions")
        if self.page.compatible(self.mod):
            parts.append(f"has a build for Minecraft {mods.mc_line(game)}")
        elif game:
            parts.append(f"no build listed for Minecraft {mods.mc_line(game)}")
        deps = [d.get("name") or k.rsplit("/", 1)[-1]
                for k, d in (document.get("dependencies") or {}).items()]
        if deps:
            parts.append("needs " + ", ".join(deps[:6]))
        self.info.setText("   ·   ".join(parts))
        text = _readable_markdown(mods.readme(document))
        if text:
            # Through HTML, to give links the theme's colour instead of the
            # dark blue Qt's Markdown import hard-codes.
            scratch = QTextDocument()
            scratch.setMarkdown(text)
            html = re.sub(r"color:#0000ff", f"color:{self.page.win.theme.accent}",
                          scratch.toHtml(), flags=re.I)
            self.readme.setHtml(html)
        else:
            self.readme.setPlainText(document.get("description") or
                                     "This mod has no README.")


class ModsPage(QFrame):
    """Settings-style page for LeviLamina and its mods."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setObjectName("Card")
        self.state = {}
        self.builds = []
        self.catalogue = []
        self.catalogue_by_id = {}
        self.installed_ids = {}
        self.installed_items = []
        self.busy = False
        self._logos = {}
        self._pixmaps = {}
        self._cards = []
        self._details = None
        self._loaded = False
        self._bridge = _Bridge()
        self._bridge.line.connect(self._on_log)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 14)
        outer.setSpacing(10)

        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(0)
        title = QLabel("Mods")
        title.setObjectName("Title")
        titles.addWidget(title)
        sub = QLabel("LeviLamina mods from levimc.org — installed per "
                     "Minecraft build")
        sub.setObjectName("Muted")
        titles.addWidget(sub)
        head.addLayout(titles)
        head.addStretch(1)
        build_label = QLabel("Build")
        build_label.setObjectName("Sub")
        head.addWidget(build_label)
        from .gui import SettingComboBox  # noqa: PLC0415 - avoids a cycle
        self.build_combo = SettingComboBox()
        self.build_combo.setMinimumWidth(230)
        self.build_combo.currentIndexChanged.connect(self._on_build)
        head.addWidget(self.build_combo)
        back = QPushButton("← Back")
        back.setObjectName("Ghost")
        back.setFixedSize(76, 28)
        back.setCursor(Qt.PointingHandCursor)
        back.clicked.connect(win.toggle_mods)
        head.addWidget(back)
        outer.addLayout(head)

        outer.addWidget(self._build_status())

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_browse(), "Browse")
        self.tabs.addTab(self._build_installed(), "Installed")
        outer.addWidget(self.tabs, 1)

        foot = QHBoxLayout()
        foot.setSpacing(10)
        self.op_label = QLabel("")
        self.op_label.setObjectName("Muted")
        foot.addWidget(self.op_label, 1)
        self.op_bar = QProgressBar()
        self.op_bar.setFixedWidth(220)
        self.op_bar.setFixedHeight(8)
        self.op_bar.setTextVisible(False)
        self.op_bar.hide()
        foot.addWidget(self.op_bar)
        outer.addLayout(foot)

    # ------------------------------------------------------------ layout
    def _build_status(self):
        card = QFrame()
        card.setObjectName("CardFlat")
        row = QHBoxLayout(card)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(12)
        self.ll_logo = QLabel()
        self.ll_logo.setFixedSize(44, 44)
        row.addWidget(self.ll_logo)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.status_title = QLabel("Loading…")
        self.status_title.setStyleSheet("font-weight:700; font-size:14px;")
        self.status_title.setWordWrap(True)
        text.addWidget(self.status_title)
        self.status_detail = QLabel("")
        self.status_detail.setObjectName("Sub")
        self.status_detail.setWordWrap(True)
        self.status_detail.setStyleSheet("font-size:12px;")
        text.addWidget(self.status_detail)
        row.addLayout(text, 1)
        self.mods_switch = self.win._switch(
            "Load mods", True,
            "Start this build with LeviLamina and its mods. Off: the game "
            "starts vanilla, the mods stay installed.")
        self.mods_switch.switch.toggled.connect(self._toggle_all)
        row.addWidget(self.mods_switch)
        self.force_switch = self.win._switch(
            "Try anyway", False,
            "Use the LeviLamina of the nearest build it supports on this "
            "one. LeviLamina finds the game's code by where it sits in one "
            "exact build, so on another build it may close or freeze the "
            "game at start. Turn it off to play this build vanilla again.")
        self.force_switch.switch.toggled.connect(self._toggle_force)
        row.addWidget(self.force_switch)
        self.play_build_btn = self._small("Play this build", "Ghost",
                                          self._play_this_build)
        row.addWidget(self.play_build_btn)
        self.ll_action = self._small("Install LeviLamina", "Primary",
                                     self._ll_action)
        self.ll_action.setFixedWidth(150)
        row.addWidget(self.ll_action)
        self.ll_remove = self._small("Uninstall", "DangerSmall",
                                     self._ll_remove)
        row.addWidget(self.ll_remove)
        return card

    def _small(self, text, kind, slot):
        button = QPushButton(text)
        button.setObjectName(kind)
        button.setCursor(Qt.PointingHandCursor)
        button.setFixedHeight(30)
        button.clicked.connect(slot)
        return button

    def _build_browse(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 10, 0, 0)
        v.setSpacing(8)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search mods…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _: self._filter_soon())
        bar.addWidget(self.search, 1)
        from .gui import SettingComboBox  # noqa: PLC0415
        self.category = SettingComboBox()
        self.category.addItem("All categories", "")
        self.category.currentIndexChanged.connect(lambda _: self._render())
        bar.addWidget(self.category)
        self.sort = SettingComboBox()
        for label, key in (("Most popular", "stars"),
                           ("Recently updated", "updated"),
                           ("Name", "name")):
            self.sort.addItem(label, key)
        self.sort.currentIndexChanged.connect(lambda _: self._render())
        bar.addWidget(self.sort)
        self.compat_switch = self.win._switch(
            "For this build", True,
            "Only the mods levimc.org lists for this build's Minecraft "
            "version")
        self.compat_switch.switch.toggled.connect(lambda _: self._render())
        bar.addWidget(self.compat_switch)
        refresh = self._small("↻", "Ghost",
                              lambda: self.load_catalogue(refresh=True))
        refresh.setFixedWidth(34)
        refresh.setToolTip("Reload the catalogue from levimc.org")
        bar.addWidget(refresh)
        v.addLayout(bar)

        self.count_label = QLabel("")
        self.count_label.setObjectName("Muted")
        v.addWidget(self.count_label)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 6, 0)
        self.grid.setHorizontalSpacing(10)
        self.grid.setVerticalSpacing(10)
        self.grid.setAlignment(Qt.AlignTop)
        area.setWidget(self.grid_host)
        v.addWidget(area, 1)
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(180)
        self._filter_timer.timeout.connect(self._render)
        return page

    def _build_installed(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 10, 0, 0)
        v.setSpacing(8)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.import_btn = self._small("+ Import mod…", "Primary",
                                      self._import)
        self.import_btn.setToolTip(
            "Add a mod of your own: a .zip with a LeviLamina manifest.json, "
            "a mod folder, or a lone .dll")
        bar.addWidget(self.import_btn)
        self.import_dir_btn = self._small("Import folder…", "Ghost",
                                          lambda: self._import(folder=True))
        bar.addWidget(self.import_dir_btn)
        self.update_btn = self._small("Update all", "Ghost", self._update_all)
        bar.addWidget(self.update_btn)
        bar.addStretch(1)
        self.folder_btn = self._small("Open mods folder", "Ghost",
                                      self._open_mods)
        bar.addWidget(self.folder_btn)
        self.log_btn = self._small("Loader log", "Ghost", self._show_log)
        self.log_btn.setToolTip("What the loader wrote the last time the "
                                "game started")
        bar.addWidget(self.log_btn)
        v.addLayout(bar)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        host = QWidget()
        self.installed_layout = QVBoxLayout(host)
        self.installed_layout.setContentsMargins(0, 0, 6, 0)
        self.installed_layout.setSpacing(8)
        self.installed_layout.setAlignment(Qt.AlignTop)
        area.setWidget(host)
        v.addWidget(area, 1)
        return page

    # ------------------------------------------------------------ data
    def opened(self):
        """The page is shown: load what it shows."""
        self._make_room()
        self.refresh_builds()
        if not self._loaded:
            self._loaded = True
            self.load_catalogue()

    def refresh_builds(self):
        # Through bol.gui, where the window (and its tests) take it from.
        from .gui import installed_builds  # noqa: PLC0415
        try:
            builds = [b for b in installed_builds(with_size=False)
                      if b.get("playable")]
        except Exception as exc:  # noqa: BLE001
            builds = []
            self._set_op(f"The builds could not be listed: {exc}")
        current = self.current_dir()
        self.builds = builds
        self.build_combo.blockSignals(True)
        self.build_combo.clear()
        selected = None
        for index, build in enumerate(builds):
            label = build["version"]
            if build.get("edition") == "preview":
                label += "  ·  Preview"
            if self._is_selected(build):
                label += "  ·  selected"
            self.build_combo.addItem(label, str(build["path"]))
            if current and Path(current) == Path(build["path"]):
                selected = index
        if selected is None:
            in_use = [i for i, b in enumerate(builds)
                      if self._is_selected(b)]
            selected = in_use[0] if in_use else 0
        if builds:
            self.build_combo.setCurrentIndex(selected)
        self.build_combo.blockSignals(False)
        self._on_build()

    def _is_selected(self, build):
        """Whether PLAY starts this build: the one picked next to it."""
        try:
            picked = self.win.selected_version()
        except Exception:  # noqa: BLE001 - picker not loaded yet
            picked = None
        if not picked:
            return bool(build.get("in_use"))
        return picked.get("tag") == build.get("version") and \
            bool(picked.get("beta")) == (build.get("edition") == "preview")

    def _make_room(self):
        """Grow a small window to fit the catalogue.

        The launcher opens at 1000x660, sized for PLAY; with its header, the
        LeviLamina bar and the filters, that left room for one row of mods.
        A maximized or already large window is left alone, and it never
        grows past the screen."""
        win = self.win
        if win.isMaximized() or win.isFullScreen():
            return
        screen = win.screen()
        if screen is None:
            return
        room = screen.availableGeometry()
        width = min(max(win.width(), 1120), room.width() - 40)
        height = min(max(win.height(), 820), room.height() - 60)
        if (width, height) == (win.width(), win.height()):
            return
        frame = win.frameGeometry()
        win.resize(width, height)
        # Keep it on screen: grow from where it is, moved back if needed.
        x = min(max(room.left(), frame.left()),
                room.right() - width - (frame.width() - win.width()))
        y = min(max(room.top(), frame.top()),
                room.bottom() - height - (frame.height() - win.height()))
        win.move(x, y)

    def current_dir(self):
        data = self.build_combo.currentData()
        return Path(data) if data else None

    def _on_build(self, *_):
        game_dir = self.current_dir()
        if game_dir is None:
            self.state = {}
            self._paint_status()
            return
        self._run("mods-describe", lambda: (mods.describe(game_dir),
                                            mods.installed(game_dir)),
                  self._described, quiet=True)

    def _described(self, result):
        state, items = result
        if self.current_dir() != Path(state["game_dir"]):
            return
        self.state = state
        self.installed_items = items
        self.installed_ids = {}
        for key, record in mods.packages(state["game_dir"]).items():
            self.installed_ids[record.get("id") or mods.split_key(key)[0]] = \
                record.get("version")
        self._paint_status()
        self._paint_installed()
        self._render()

    def load_catalogue(self, refresh=False):
        self.count_label.setText("Loading the catalogue from levimc.org…")
        self._run("mods-catalogue", lambda: mods.catalogue(refresh),
                  self._catalogue_loaded, quiet=True)

    def _catalogue_loaded(self, catalogue):
        self.catalogue = catalogue
        self.catalogue_by_id = {m["id"]: m for m in catalogue}
        tags = {}
        for mod in catalogue:
            for tag in _tags(mod):
                tags[tag] = tags.get(tag, 0) + 1
        wanted = self.category.currentData()
        self.category.blockSignals(True)
        self.category.clear()
        self.category.addItem("All categories", "")
        for tag, _count in sorted(tags.items(), key=lambda t: (-t[1], t[0])):
            if _count > 1:
                self.category.addItem(tag.capitalize(), tag)
        index = self.category.findData(wanted)
        self.category.setCurrentIndex(max(0, index))
        self.category.blockSignals(False)
        self._render()
        self._paint_status()
        loader = LogoLoader([mods.levilamina_logo_url()] +
                            [m.get("logo") for m in catalogue])
        loader.loaded.connect(self._logo_loaded)
        self.win._start_worker("mods-logos", loader)

    def _logo_loaded(self, url, data):
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            return
        self._logos[url] = pixmap
        self._pixmaps = {k: v for k, v in self._pixmaps.items()
                         if k[0] != url}
        for card in self._cards:
            if card.mod.get("logo") == url:
                card.set_logo(self.logo_for(card.mod))
        if url == mods.levilamina_logo_url():
            self._paint_status()
        if any((self.catalogue_by_id.get(i.get("id") or "") or {})
               .get("logo") == url for i in self.installed_items):
            self._paint_installed()

    def logo_for(self, mod, size=LOGO_SIZE):
        url = mod.get("logo") or ""
        key = (url, size)
        if key not in self._pixmaps:
            source = self._logos.get(url)
            if source is not None:
                self._pixmaps[key] = _rounded(source, size)
            else:
                return _initials_pixmap(mod.get("name") or mod["id"]
                                        .rsplit("/", 1)[-1],
                                        self.win.theme.accent, size)
        return self._pixmaps[key]

    def load_detail(self, package_id, then):
        worker_slot = "mods-detail"

        def done(document):
            if self._details is not None:
                then(document)

        if not self._run(worker_slot, lambda: mods.detail(package_id), done,
                         quiet=True):
            QTimer.singleShot(400, lambda: self.load_detail(package_id,
                                                            then))

    def compatible(self, mod):
        version = self.state.get("target_version") or \
            self.state.get("game_version")
        return bool(version) and mods.catalogue_compatible(mod, version)

    # ------------------------------------------------------------ painting
    def _paint_status(self):
        state = self.state
        ll_mod = {"name": "LeviLamina", "id": mods.LEVILAMINA,
                  "logo": mods.levilamina_logo_url()}
        self.ll_logo.setPixmap(self.logo_for(ll_mod, 44))
        version = state.get("game_version")
        installed = state.get("levilamina")
        latest = state.get("levilamina_latest")
        selected_build = any(self._is_selected(b) and self.current_dir() ==
                             Path(b["path"]) for b in self.builds)
        self.play_build_btn.setVisible(bool(state) and not selected_build
                                       and bool(installed))
        self.mods_switch.setVisible(bool(installed))
        self.ll_remove.setVisible(bool(installed))
        self.ll_action.setVisible(True)
        forced = bool(state.get("forced"))
        self.force_switch.setVisible(bool(state.get("nearest")) and (
            forced or not state.get("supported")))
        self.force_switch.switch.blockSignals(True)
        self.force_switch.switch.setChecked(forced)
        self.force_switch.switch.blockSignals(False)
        self.force_switch.setEnabled(not self.busy)
        self.status_detail.setStyleSheet("font-size:12px;")
        if not self.builds:
            self.status_title.setText("No Minecraft build is installed")
            self.status_detail.setText("Install one with PLAY first; mods "
                                       "go into a build's folder.")
            self.ll_action.setVisible(False)
        elif not state:
            self.status_title.setText("Reading this build…")
            self.status_detail.setText("")
            self.ll_action.setVisible(False)
        elif state.get("error") and not state.get("supported"):
            self.status_title.setText("LeviLamina's list of builds could "
                                      "not be read")
            self.status_detail.setText(str(state["error"]))
            self.ll_action.setVisible(False)
        elif not state.get("supported"):
            others = [b["version"] for b in self.builds
                      if any(mods.same_build(b["version"], s)
                             for s in self._supported())]
            self.status_title.setText(
                f"LeviLamina does not support Minecraft {version} yet")
            text = ("LeviLamina is made for one Minecraft build at a time. "
                    "It runs on " + ", ".join(
                        _pretty_build(b) for b in self._supported()[:3])
                    + ".")
            if others:
                text += f" You have {others[0]} installed: pick it above."
            else:
                text += (" Install one of those from Settings ▸ Versions to "
                         "use mods.")
            if state.get("nearest"):
                text += (" Or switch on \"Try anyway\" to use the one made "
                         f"for {_pretty_build(state['nearest'])} — it may "
                         "not start.")
            self.status_detail.setText(text)
            self.ll_action.setVisible(False)
        elif not installed:
            self.status_title.setText(
                f"LeviLamina {latest} is ready for Minecraft {version}"
                + (" (trying anyway)" if forced else ""))
            self.status_detail.setText(
                "The mod loader goes in this build's folder and starts with "
                "the game. Installing any mod installs it too.")
            self.ll_action.setText("Install LeviLamina")
            self.ll_action.setObjectName("Primary")
        else:
            count = len([i for i in self.installed_items if not i["core"]])
            on = len([i for i in self.installed_items
                      if not i["core"] and i["enabled"]])
            self.status_title.setText(
                f"LeviLamina {installed}  ·  {count} mod"
                f"{'s' if count != 1 else ''} ({on} on)")
            if not state.get("enabled"):
                detail = "Mods are off: this build starts vanilla."
            elif selected_build:
                detail = "Loads with the game when you press PLAY."
            else:
                detail = ("Loads when you play this build; PLAY starts "
                          "another one right now.")
            if forced:
                detail = (f"Trying the LeviLamina made for "
                          f"{_pretty_build(state['target_version'])} on "
                          f"{version}: if the game closes or freezes at "
                          "start, switch \"Try anyway\" off. " + detail)
                self.status_detail.setStyleSheet(
                    f"font-size:12px; color:{self.win.theme.gold};")
            self.status_detail.setText(detail)
            if latest and installed != latest:
                self.ll_action.setText(f"Update to {latest}")
                self.ll_action.setObjectName("Primary")
            else:
                self.ll_action.setVisible(False)
        self.mods_switch.switch.blockSignals(True)
        self.mods_switch.switch.setChecked(bool(state.get("enabled", True)))
        self.mods_switch.switch.blockSignals(False)
        for button in (self.ll_action, self.ll_remove, self.import_btn,
                       self.import_dir_btn, self.update_btn):
            button.setEnabled(not self.busy and bool(self.builds))
        self.update_btn.setEnabled(not self.busy and bool(installed))
        self.mods_switch.setEnabled(not self.busy)
        can_import = not self.busy and bool(state.get("supported"))
        self.import_btn.setEnabled(can_import)
        self.import_dir_btn.setEnabled(can_import)
        self.ll_action.style().unpolish(self.ll_action)
        self.ll_action.style().polish(self.ll_action)

    def _supported(self):
        try:
            return mods.supported_builds()
        except Exception:  # noqa: BLE001
            return []

    def _filter_soon(self):
        self._filter_timer.start()

    def _render(self):
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._cards = []
        if not self.catalogue:
            return
        query = self.search.text().strip().lower()
        category = self.category.currentData() or ""
        only_compatible = self.compat_switch.switch.isChecked() and \
            bool(self.state.get("game_version"))
        shown = []
        for mod in self.catalogue:
            if query and query not in " ".join(
                    [mod.get("name") or "", mod.get("description") or "",
                     mod.get("owner") or "", mod["id"]] + _tags(mod)).lower():
                continue
            if category and category not in _tags(mod):
                continue
            if only_compatible and not self.compatible(mod) and \
                    not self.installed_ids.get(mod["id"]):
                continue
            if mod["id"] == mods.LEVILAMINA:
                continue
            shown.append(mod)
        key = self.sort.currentData()
        if key == "stars":
            shown.sort(key=lambda m: -int(m.get("stars") or 0))
        elif key == "updated":
            shown.sort(key=lambda m: str(m.get("updated") or ""),
                       reverse=True)
        else:
            shown.sort(key=lambda m: str(m.get("name") or "").lower())
        total = len(self.catalogue)
        line = mods.mc_line(self.state.get("target_version")
                            or self.state.get("game_version"))
        self.count_label.setText(
            f"{len(shown)} of {total} client mods"
            + (f" · for Minecraft {line}" if only_compatible and line else "")
            + "  ·  source: levimc.org")
        columns = 2 if self.width() > 1000 else 1
        for column in range(2):
            self.grid.setColumnStretch(column, 1 if column < columns else 0)
        for index, mod in enumerate(shown):
            card = ModCard(self, mod)
            self._cards.append(card)
            self.grid.addWidget(card, index // columns, index % columns)
        if not shown:
            empty = QLabel("No mod matches." if query or category else
                           "No mod is listed for this build's Minecraft "
                           "version yet. Turn off \"For this build\" to see "
                           "them all.")
            empty.setObjectName("Sub")
            empty.setWordWrap(True)
            empty.setAlignment(Qt.AlignCenter)
            self.grid.addWidget(empty, 0, 0, 1, 2)

    def _paint_installed(self):
        layout = self.installed_layout
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        items = sorted(self.installed_items,
                       key=lambda i: (not i["core"], i["name"].lower()))
        for item in items:
            layout.addWidget(InstalledRow(self, item))
        if not items:
            empty = QLabel("No mod in this build yet. Install one from "
                           "Browse, or import your own.")
            empty.setObjectName("Sub")
            empty.setAlignment(Qt.AlignCenter)
            layout.addWidget(empty)
        count = len([i for i in items if not i["core"]])
        self.tabs.setTabText(1, f"Installed ({count})" if count
                             else "Installed")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        columns = 2 if self.width() > 1000 else 1
        if self._cards and columns != getattr(self, "_columns", columns):
            self._render()
        self._columns = columns

    # ------------------------------------------------------------ actions
    def _run(self, slot, fn, then, quiet=False, label=None):
        """Run ``fn`` in a worker; ``then(result)`` on the UI thread."""
        from .gui import Worker  # noqa: PLC0415
        worker = Worker(fn)
        worker.done.connect(then)
        worker.failed.connect(lambda message: self._failed(message, quiet))
        worker.progress.connect(self._progress)
        if not self.win._start_worker(slot, worker):
            return False
        return True

    def _failed(self, message, quiet):
        self._set_busy(False)
        self._set_op(message)
        if not quiet:
            QMessageBox.warning(self.win, "Mods", message)

    def _progress(self, got, total):
        if total > 0:
            self.op_bar.setRange(0, 1000)
            self.op_bar.setValue(int(got * 1000 / total))

    def _on_log(self, line):
        self._set_op(line)

    def _set_op(self, text):
        self.op_label.setText(str(text)[:160])

    def _set_busy(self, on):
        self.busy = on
        self.op_bar.setVisible(on)
        if on:
            self.op_bar.setRange(0, 0)
        self.build_combo.setEnabled(not on)
        for card in self._cards:
            card.refresh()
        if self._details is not None:
            self._details.refresh()
        self._paint_status()
        self._paint_installed()

    def _operation(self, label, fn, success):
        if self.busy:
            return
        game_dir = self.current_dir()
        if game_dir is None:
            return
        bridge = self._bridge

        def work(progress=None):
            return fn(game_dir, lambda line: bridge.line.emit(str(line)),
                      progress)

        def done(result):
            self._set_busy(False)
            self._set_op(success(result) if callable(success) else success)
            self._on_build()

        self._set_op(label)
        if self._run("mods-op", work, done, label=label):
            self._set_busy(True)

    def install(self, package_ids, name=None):
        name = name or package_ids[0].rsplit("/", 1)[-1]

        def work(d, log, progress):
            changed = mods.install(d, package_ids, progress=progress, log=log)
            return changed, mods.load_problems(d)

        def success(result):
            changed, problems = result
            if problems:
                QTimer.singleShot(0, lambda: self._warn_problems(problems))
            if not changed:
                return f"{name} is already up to date."
            if problems:
                return (f"Installed {name}, but "
                        + ", ".join(problems) + " cannot load.")
            return f"Installed {name}. It loads the next time you play " \
                   "this build."

        self._operation(f"Installing {name}…", work, success)

    def _warn_problems(self, problems):
        lines = "\n".join(f"• {folder}: {reason}."
                          for folder, reason in problems.items())
        QMessageBox.warning(
            self.win, "A mod cannot load",
            "These mods do not match the LeviLamina installed in this build "
            "and would stop Minecraft while it loads:\n\n" + lines +
            "\n\nThey will be switched off when you press PLAY. An update "
            "of the mod, or a word to its author, is what fixes it.")

    def uninstall(self, package_id, name=None):
        name = name or package_id.rsplit("/", 1)[-1]
        if QMessageBox.question(
                self.win, "Remove mod",
                f"Remove {name} from this build?") != QMessageBox.Yes:
            return
        self._operation(f"Removing {name}…",
                        lambda d, log, progress: mods.uninstall(d, package_id),
                        f"Removed {name}.")

    def remove(self, item):
        if item.get("package"):
            return self.uninstall(item["id"] or item["package"], item["name"])
        if QMessageBox.question(
                self.win, "Remove mod",
                f"Delete the imported mod {item['name']} from this build?"
                ) != QMessageBox.Yes:
            return
        self._operation(f"Removing {item['name']}…",
                        lambda d, log, progress: mods.remove_folder(
                            d, item["folder"]),
                        f"Removed {item['name']}.")

    def set_mod_enabled(self, item, on):
        game_dir = self.current_dir()
        try:
            mods.set_enabled(game_dir, item["folder"], on)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self.win, "Mods", str(exc))
        self._on_build()

    def _toggle_all(self, on):
        game_dir = self.current_dir()
        if game_dir is None:
            return
        try:
            mods.set_mods_enabled(game_dir, on)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self.win, "Mods", str(exc))
        self._on_build()

    def _toggle_force(self, on):
        game_dir = self.current_dir()
        if game_dir is None:
            return
        if on and QMessageBox.question(
                self.win, "Try LeviLamina anyway",
                "LeviLamina is made for one exact Minecraft build: it finds "
                "the game's code by where it sits in that build. On this "
                "one it will most likely close or freeze the game at start, "
                "and mods that do load can misbehave.\n\nNothing is "
                "changed in your worlds, and switching this off brings the "
                "build back to vanilla. Try anyway?") != QMessageBox.Yes:
            self.force_switch.switch.blockSignals(True)
            self.force_switch.switch.setChecked(False)
            self.force_switch.switch.blockSignals(False)
            return
        try:
            mods.set_force_unsupported(game_dir, on)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self.win, "Mods", str(exc))
        self._on_build()

    def _ll_action(self):
        self.install([mods.LEVILAMINA + "#client"], "LeviLamina")

    def _ll_remove(self):
        if QMessageBox.question(
                self.win, "Uninstall LeviLamina",
                "Uninstall LeviLamina from this build? Every mod installed "
                "from levimc.org goes with it; imported mods stay in the "
                "mods folder.") != QMessageBox.Yes:
            return
        self._operation("Uninstalling LeviLamina…",
                        lambda d, log, progress: mods.uninstall(
                            d, mods.LEVILAMINA),
                        "LeviLamina was uninstalled.")

    def _update_all(self):
        def success(changed):
            return (f"Updated {len(changed)} package(s)." if changed
                    else "Everything is up to date.")
        self._operation("Looking for updates…",
                        lambda d, log, progress: mods.update_all(
                            d, progress=progress, log=log),
                        success)

    def _import(self, folder=False):
        if folder:
            path = QFileDialog.getExistingDirectory(self.win,
                                                    "Import a mod folder")
        else:
            path, _ = QFileDialog.getOpenFileName(
                self.win, "Import a mod", str(Path.home()),
                "LeviLamina mods (*.zip *.dll);;All files (*)")
        if not path:
            return

        def work(d, log, progress):
            try:
                return mods.import_mod(d, path)
            except mods.ModError as exc:
                if "already installed" not in str(exc):
                    raise
                return ("exists", str(exc))

        def success(result):
            if isinstance(result, tuple):
                QTimer.singleShot(0, lambda: self._import_overwrite(path))
                return result[1]
            return f"Imported {result}."

        self._operation(f"Importing {Path(path).name}…", work, success)

    def _import_overwrite(self, path):
        if QMessageBox.question(
                self.win, "Replace mod",
                "A mod with this name is already in this build. Replace "
                "it?") != QMessageBox.Yes:
            return
        self._operation(f"Importing {Path(path).name}…",
                        lambda d, log, progress: mods.import_mod(
                            d, path, overwrite=True),
                        lambda name: f"Replaced {name}.")

    def _open_mods(self):
        game_dir = self.current_dir()
        if game_dir is None:
            return
        folder = game_dir / "mods"
        folder.mkdir(exist_ok=True)
        self.open_path(folder)

    def _show_log(self):
        game_dir = self.current_dir()
        text = mods.loader_log(game_dir) if game_dir else ""
        box = QMessageBox(self.win)
        box.setWindowTitle("Mod loader log")
        box.setText(text.strip() or "The loader has not written anything "
                    "yet: it writes when the game starts with mods on.")
        box.exec()

    def _play_this_build(self):
        game_dir = self.current_dir()
        build = next((b for b in self.builds
                      if Path(b["path"]) == game_dir), None)
        if build is None:
            return
        if not self.win.select_build(build["version"],
                                     build.get("edition") == "preview"):
            QMessageBox.information(
                self.win, "Mods",
                f"Pick {build['version']} in the version list next to PLAY.")
            return
        self.refresh_builds()

    def open_details(self, mod):
        if self._details is not None:
            self._details.close()
        self._details = ModDetails(self, mod)
        self._details.finished.connect(self._details_closed)
        self._details.show()

    def _details_closed(self, *_):
        self._details = None

    def open_path(self, path):
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)

    def open_url(self, url):
        self.open_path(url)

    def sizeHint(self):
        return QSize(960, 640)
