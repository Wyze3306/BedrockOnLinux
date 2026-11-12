"""bol.gui_content — the Add-ons & Maps page: MCPEDL and CurseForge content.

Two tabs. Discover searches the catalogue (bol.catalog) by kind and sort,
and installs a project's file in one click. Library lists every pack and
world the game will find -- installed from here, imported by hand or made
in-game -- and takes them out again, into the launcher's trash.

Every network or disk operation runs in a worker held by
``MainWindow._start_worker``; pictures come from one loader thread that
stops by itself once idle and is started again on demand.
"""
# SPDX-License-Identifier: MIT

from __future__ import annotations

import datetime
import queue
import subprocess
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPalette, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton, QScrollArea,
    QSizePolicy, QTabWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from . import catalog

CARD_WIDTH = 236
THUMB_HEIGHT = 128
ICON_SIZE = 44


# ---------------------------------------------------------------- pictures

class ImageLoader(QThread):
    """Fetch pictures from a shared queue; stops after a quiet moment."""
    loaded = Signal(str, bytes)

    IDLE_SECONDS = 4.0

    def __init__(self, pending: queue.Queue):
        super().__init__()
        self._pending = pending

    def run(self):
        while not self.isInterruptionRequested():
            try:
                url = self._pending.get(timeout=self.IDLE_SECONDS)
            except queue.Empty:
                return
            data = catalog.image(url)
            if data:
                self.loaded.emit(url, data)


def _cover(pixmap, width, height, radius=12):
    """``pixmap`` scaled to fill ``width``×``height``, cropped, rounded."""
    scaled = pixmap.scaled(width, height, Qt.KeepAspectRatioByExpanding,
                           Qt.SmoothTransformation)
    out = QPixmap(width, height)
    out.fill(Qt.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, width, height, radius, radius)
    painter.setClipPath(path)
    painter.drawPixmap((width - scaled.width()) // 2,
                       (height - scaled.height()) // 2, scaled)
    painter.end()
    return out


def _placeholder(width, height, color, text="", radius=12):
    out = QPixmap(width, height)
    out.fill(Qt.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, width, height, radius, radius)
    painter.fillPath(path, QColor(color))
    if text:
        font = painter.font()
        font.setBold(True)
        font.setPixelSize(max(12, int(min(width, height) * 0.32)))
        painter.setFont(font)
        painter.setPen(QColor("white"))
        painter.drawText(out.rect(), Qt.AlignCenter, text)
    painter.end()
    return out


def _initials(title):
    words = [w for w in str(title or "?").replace("-", " ").split() if w]
    return "".join(w[0] for w in words[:2]).upper() or "?"


# ---------------------------------------------------------------- formatting

def _two_lines(text, width, chars_per_pixel=1 / 7.2):
    """``text`` cut to what two lines of a card title hold, with an ellipsis.

    A word-wrapped QLabel has no eliding of its own, and a long title
    otherwise spills over the picture above it.
    """
    text = " ".join(str(text or "").split())
    limit = max(12, int(width * chars_per_pixel * 2) - 2)
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0] or text[:limit]
    return cut.rstrip(" -–|:,") + "…"


def _count(n):
    if n is None:
        return ""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"{n / 1_000:.1f}k".replace(".0k", "k")
    return str(n)


def _ago(stamp):
    try:
        when = datetime.datetime.fromisoformat(
            str(stamp).replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
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


def _stats(item):
    parts = []
    if item.get("downloads") is not None:
        parts.append(f"⬇ {_count(item['downloads'])}")
    if item.get("rating"):
        parts.append(f"★ {item['rating']:.1f}")
    when = _ago(item.get("updated"))
    if when:
        parts.append(when)
    return "   ·   ".join(parts)


def _chip(text, color=None):
    label = QLabel(text)
    label.setObjectName("Chip")
    style = "font-size:10px; padding:1px 8px;"
    if color:
        style = f"color:{color}; " + style
    label.setStyleSheet(style)
    return label


def _button(text, kind, slot, width=None, height=30, tip=None):
    b = QPushButton(text)
    b.setObjectName(kind)
    b.setCursor(Qt.PointingHandCursor)
    b.setFixedHeight(height)
    if width:
        b.setFixedWidth(width)
    if tip:
        b.setToolTip(tip)
    b.clicked.connect(lambda _=False: slot())
    return b


def _repolish(widget):
    widget.style().unpolish(widget)
    widget.style().polish(widget)


# ---------------------------------------------------------------- cards

class ContentCard(QFrame):
    """One project in the Discover grid."""

    def __init__(self, page, item, width=CARD_WIDTH, thumb_height=None):
        super().__init__()
        self.page = page
        self.item = item
        self.setObjectName("CardFlat")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedWidth(width)
        self.setToolTip(item.get("summary") or item.get("title"))
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 10)
        v.setSpacing(4)
        # 16:9-ish, but never taller than the shelf on the home page has
        # room for.
        self._thumb_size = (width - 16, thumb_height or
                            min(150, int((width - 16) * 0.56)))
        self.thumb = QLabel()
        self.thumb.setFixedSize(*self._thumb_size)
        v.addWidget(self.thumb)
        self.set_image()

        title = QLabel(_two_lines(item.get("title") or item.get("slug"),
                                  width - 16))
        title.setStyleSheet("font-weight:700; font-size:13px;")
        title.setWordWrap(True)
        title.setFixedHeight(36)
        title.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        v.addWidget(title)
        author = QLabel(f"by {item.get('author') or '?'}")
        author.setObjectName("Muted")
        v.addWidget(author)
        foot = QHBoxLayout()
        foot.setSpacing(6)
        stats = QLabel("   ·   ".join(_stats(item).split("   ·   ")[:2]))
        stats.setObjectName("Muted")
        stats.setStyleSheet("font-size:11px;")
        foot.addWidget(stats)
        foot.addStretch(1)
        self.badge = _chip("✓ Installed", page.win.theme.accent)
        foot.addWidget(self.badge)
        v.addLayout(foot)
        # As tall as its contents: a grid row or a shelf with room to spare
        # leaves it below the card, not between its lines.
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.refresh()

    def set_image(self):
        w, h = self._thumb_size
        self.thumb.setPixmap(self.page.picture(self.item.get("image"), w, h,
                                               self.item.get("title")))

    def refresh(self):
        self.badge.setVisible(self.item.get("slug") in self.page.installed)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.page.open_details(self.item)
        super().mousePressEvent(event)


class LibraryRow(QFrame):
    """One pack or world in the Library tab."""

    def __init__(self, page, entry):
        super().__init__()
        self.page = page
        self.entry = entry
        self.setObjectName("CardFlat")
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 12, 8)
        row.setSpacing(12)
        icon = QLabel()
        icon.setFixedSize(ICON_SIZE, ICON_SIZE)
        pixmap = QPixmap()
        if entry.get("icon") and pixmap.load(entry["icon"]):
            icon.setPixmap(_cover(pixmap, ICON_SIZE, ICON_SIZE, 9))
        else:
            icon.setPixmap(_placeholder(ICON_SIZE, ICON_SIZE,
                                        page.win.theme.card3,
                                        _initials(entry["name"]), 9))
        row.addWidget(icon)
        body = QVBoxLayout()
        body.setSpacing(1)
        name = QLabel(entry["name"])
        name.setStyleSheet("font-weight:700;")
        body.addWidget(name)
        detail = entry["kind"]
        if entry.get("slug"):
            detail += "   ·   from MCPEDL / CurseForge"
        if entry.get("description"):
            detail += "   ·   " + entry["description"].splitlines()[0][:90]
        sub = QLabel(detail)
        sub.setObjectName("Muted")
        sub.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        body.addWidget(sub)
        row.addLayout(body, 1)
        if entry.get("slug"):
            row.addWidget(_button("Page", "GhostSmall",
                                  lambda: page.open_slug(entry["slug"]),
                                  tip="Its MCPEDL page"))
        row.addWidget(_button("Folder", "GhostSmall",
                              lambda: page.open_path(entry["path"]),
                              tip=entry["path"]))
        row.addWidget(_button("Remove", "DangerSmall",
                              lambda: page.remove(entry),
                              tip="Take it out of the game (kept 30 days in "
                                  "the launcher's trash)"))


# ---------------------------------------------------------------- details

class ContentDetails(QDialog):
    """A project's page: pictures, description, files to install, links."""

    def __init__(self, page, item):
        super().__init__(page.win)
        self.page = page
        self.item = dict(item)
        self.detail = None
        self.setWindowTitle(item.get("title") or item.get("slug"))
        self.resize(820, 620)
        theme = page.win.theme
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 16)
        outer.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(16)
        self.cover = QLabel()
        self.cover.setFixedSize(220, 124)
        head.addWidget(self.cover, 0, Qt.AlignTop)
        titles = QVBoxLayout()
        titles.setSpacing(4)
        title = QLabel(item.get("title") or item.get("slug"))
        title.setObjectName("Title")
        title.setStyleSheet("font-size:20px;")
        title.setWordWrap(True)
        titles.addWidget(title)
        self.byline = QLabel(f"by {item.get('author') or '?'}   ·   "
                             + _stats(item))
        self.byline.setObjectName("Muted")
        titles.addWidget(self.byline)
        summary = QLabel(item.get("summary") or "")
        summary.setObjectName("Sub")
        summary.setWordWrap(True)
        titles.addWidget(summary)
        self.chips = QHBoxLayout()
        self.chips.setSpacing(5)
        titles.addLayout(self.chips)
        titles.addStretch(1)
        head.addLayout(titles, 1)
        links = QVBoxLayout()
        links.setSpacing(6)
        links.addWidget(_button("MCPEDL", "GhostSmall",
                                lambda: page.open_url(item.get("url")),
                                width=110, tip=item.get("url")))
        self.cf_button = _button("CurseForge", "GhostSmall",
                                 lambda: page.open_url(
                                     (self.detail or {}).get("curseforge")),
                                 width=110)
        self.cf_button.hide()
        links.addWidget(self.cf_button)
        links.addStretch(1)
        head.addLayout(links)
        outer.addLayout(head)

        files_title = QLabel("FILES")
        files_title.setObjectName("Muted")
        outer.addWidget(files_title)
        self.files_host = QWidget()
        self.files_layout = QVBoxLayout(self.files_host)
        self.files_layout.setContentsMargins(0, 0, 0, 0)
        self.files_layout.setSpacing(6)
        loading = QLabel("Loading the project's page…")
        loading.setObjectName("Sub")
        self.files_layout.addWidget(loading)
        outer.addWidget(self.files_host)

        self.text = QTextBrowser()
        self.text.setOpenExternalLinks(True)
        palette = self.text.palette()
        palette.setColor(QPalette.Text, QColor(theme.fg))
        palette.setColor(QPalette.Base, QColor(theme.card))
        self.text.setPalette(palette)
        self.text.viewport().setPalette(palette)
        self.text.setStyleSheet(
            f"QTextBrowser {{ color: {theme.fg}; background: {theme.card};"
            f" border: 1px solid {theme.border}; border-radius: 10px;"
            " padding: 10px; }")
        outer.addWidget(self.text, 1)

        foot = QHBoxLayout()
        self.op_label = QLabel("")
        self.op_label.setObjectName("Muted")
        foot.addWidget(self.op_label, 1)
        close = _button("Close", "Ghost", self.close)
        foot.addWidget(close)
        outer.addLayout(foot)
        self.set_image()
        page.load_details(item["slug"], self.show_detail)

    def set_image(self):
        self.cover.setPixmap(self.page.picture(self.item.get("image"), 220,
                                               124, self.item.get("title")))

    def _clear(self, layout):
        while layout.count():
            child = layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
            elif child.layout():
                self._clear(child.layout())

    def show_detail(self, detail):
        self.detail = detail
        self.item.update({k: detail[k] for k in ("image", "kind")
                          if detail.get(k)})
        theme = self.page.win.theme
        self.byline.setText(f"by {detail.get('author') or '?'}   ·   "
                            + _stats(detail))
        self._clear(self.chips)
        self.chips.addWidget(_chip(catalog.KINDS.get(
            detail.get("kind"), ("Content",))[0]))
        for tag in (detail.get("tags") or [])[:4]:
            self.chips.addWidget(_chip(tag))
        version = self.page.game_version()
        fits = catalog.supports(detail, version)
        if detail.get("versions"):
            text = "Minecraft " + ", ".join(detail["versions"][:3])
            color = theme.accent if fits else theme.gold
            chip = _chip(text, color)
            if fits is False:
                chip.setToolTip(f"Not listed for {version}: it may still "
                                "work, or need a newer or older game.")
            self.chips.addWidget(chip)
        self.chips.addStretch(1)
        self.cf_button.setVisible(bool(detail.get("curseforge")))

        self._clear(self.files_layout)
        files = detail.get("files") or []
        for index, file in enumerate(files[:6]):
            self.files_layout.addWidget(self._file_row(file, index == 0))
        for link in (detail.get("links") or [])[:4]:
            self.files_layout.addWidget(self._link_row(link))
        if not files and not detail.get("links"):
            empty = QLabel("This project lists no file to download.")
            empty.setObjectName("Sub")
            self.files_layout.addWidget(empty)

        text = detail.get("description") or detail.get("summary") or ""
        if detail.get("installation"):
            text += "\n\nInstallation\n" + detail["installation"]
        self.text.setPlainText(text)
        self.refresh()

    def _file_row(self, file, latest):
        row = QFrame()
        row.setObjectName("CardFlat")
        h = QHBoxLayout(row)
        h.setContentsMargins(12, 6, 8, 6)
        h.setSpacing(10)
        name = QLabel(file.get("label") or file["name"])
        name.setStyleSheet("font-weight:600;")
        name.setToolTip(file["name"])
        name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        h.addWidget(name, 1)
        meta = "   ·   ".join(p for p in (
            Path(file["name"]).suffix.lstrip("."), file.get("size"),
            _ago(file.get("date"))) if p)
        info = QLabel(meta)
        info.setObjectName("Muted")
        h.addWidget(info)
        if file.get("installable"):
            action = _button("Install", "Primary" if latest else "Ghost",
                             lambda: self.page.install(self.item, file, self),
                             width=96)
            action.setProperty("file_row", True)
        else:
            action = _button("Download", "Ghost",
                             lambda: self.page.open_url(file["url"]),
                             width=96, tip="Not a file the game imports: "
                                           "open it in the browser")
        h.addWidget(action)
        return row

    def _link_row(self, link):
        row = QFrame()
        row.setObjectName("CardFlat")
        h = QHBoxLayout(row)
        h.setContentsMargins(12, 6, 8, 6)
        name = QLabel(link["name"])
        name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        h.addWidget(name, 1)
        host = QLabel(f"external link · {link.get('host') or '?'}")
        host.setObjectName("Muted")
        h.addWidget(host)
        h.addWidget(_button("Open ↗", "Ghost",
                            lambda: self.page.open_url(link["url"]),
                            width=96, tip="The author hosts this file "
                                          "elsewhere: open it in the "
                                          "browser"))
        return row

    def refresh(self):
        busy = self.page.busy
        for button in self.files_host.findChildren(QPushButton):
            if button.property("file_row"):
                button.setEnabled(not busy)
        installed = self.item.get("slug") in self.page.installed
        self.op_label.setText(self.page.op_text if busy else (
            "✓ Installed — it is in the game's Add-Ons and worlds lists."
            if installed else ""))


# ---------------------------------------------------------------- page

class ContentPage(QFrame):
    """Add-ons, maps, texture packs and skins, from MCPEDL and CurseForge."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setObjectName("Card")
        self.kind = "addons"
        self.items = []
        self.page_no = 1
        self.pages = 1
        self.total = 0
        self.installed = set()
        self.busy = False
        self.op_text = ""
        self._cards = []
        self._details = None
        self._loaded = False
        self._query = ""
        self._images = {}
        self._scaled = {}
        self._pending_images = queue.Queue()
        self._requested = set()
        self._columns = 0

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 14)
        outer.setSpacing(10)

        head = QHBoxLayout()
        sub = QLabel("Add-ons, maps, texture packs and skins from MCPEDL "
                     "and CurseForge, installed straight into the game.")
        sub.setObjectName("Sub")
        head.addWidget(sub)
        head.addStretch(1)
        head.addWidget(_button("Import file…", "Ghost", self.import_files,
                               tip=".mcpack, .mcaddon, .mcworld, "
                                   ".mctemplate or .mcskin"))
        head.addWidget(_button("Game folder", "Ghost", self._open_game_folder,
                               tip="The game's com.mojang folder"))
        outer.addLayout(head)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_discover(), "Discover")
        self.tabs.addTab(self._build_library(), "Library")
        self.tabs.currentChanged.connect(self._on_tab)
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
    def _build_discover(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 10, 0, 0)
        v.setSpacing(8)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText(
            "Search MCPEDL and CurseForge, or paste a link…")
        self.search.setClearButtonEnabled(True)
        self.search.returnPressed.connect(self._search_now)
        self.search.textChanged.connect(lambda _: self._search_timer.start())
        bar.addWidget(self.search, 1)
        from .gui import SettingComboBox  # noqa: PLC0415 - avoids a cycle
        self.sort = SettingComboBox()
        for key, (label, _api) in catalog.SORTS.items():
            self.sort.addItem(label, key)
        self.sort.currentIndexChanged.connect(lambda _: self.load())
        bar.addWidget(self.sort)
        refresh = _button("↻", "IconBtn", lambda: self.load(refresh=True),
                          width=34, tip="Reload from MCPEDL")
        bar.addWidget(refresh)
        v.addLayout(bar)

        kinds = QHBoxLayout()
        kinds.setSpacing(6)
        self.kind_group = QButtonGroup(self)
        self.kind_group.setExclusive(True)
        self.kind_buttons = {}
        for key, (label, _type, _cf) in catalog.KINDS.items():
            b = QPushButton(label)
            b.setObjectName("Toggle")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(28)
            b.clicked.connect(lambda _=False, k=key: self.show_kind(k))
            self.kind_group.addButton(b)
            self.kind_buttons[key] = b
            kinds.addWidget(b)
        self.kind_buttons[self.kind].setChecked(True)
        kinds.addStretch(1)
        self.count_label = QLabel("")
        self.count_label.setObjectName("Muted")
        kinds.addWidget(self.count_label)
        v.addLayout(kinds)

        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        self.area.setFrameShape(QFrame.NoFrame)
        self.area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        host = QWidget()
        hv = QVBoxLayout(host)
        hv.setContentsMargins(0, 0, 6, 0)
        hv.setSpacing(10)
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(10)
        self.grid.setVerticalSpacing(10)
        self.grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        hv.addWidget(self.grid_host)
        self.more_btn = _button("Load more", "Ghost", self.load_more,
                                width=160)
        self.more_btn.hide()
        hv.addWidget(self.more_btn, 0, Qt.AlignHCenter)
        hv.addStretch(1)
        self.area.setWidget(host)
        v.addWidget(self.area, 1)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(450)
        self._search_timer.timeout.connect(self._search_now)
        return page

    def _build_library(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 10, 0, 0)
        v.setSpacing(8)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.library_filter = QLineEdit()
        self.library_filter.setPlaceholderText("Filter by name…")
        self.library_filter.setClearButtonEnabled(True)
        self.library_filter.textChanged.connect(
            lambda _: self._paint_library())
        bar.addWidget(self.library_filter, 1)
        from .gui import SettingComboBox  # noqa: PLC0415
        self.library_kind = SettingComboBox()
        self.library_kind.addItem("Everything", "")
        for sub, label in catalog.LIBRARY_KINDS.items():
            self.library_kind.addItem(label + "s", sub)
        self.library_kind.currentIndexChanged.connect(
            lambda _: self._paint_library())
        bar.addWidget(self.library_kind)
        bar.addWidget(_button("Trash", "Ghost", self._open_trash,
                              tip="Removed content, kept 30 days"))
        bar.addWidget(_button("↻", "IconBtn", self.load_library, width=34,
                              tip="Look again"))
        v.addLayout(bar)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        host = QWidget()
        self.library_layout = QVBoxLayout(host)
        self.library_layout.setContentsMargins(0, 0, 6, 0)
        self.library_layout.setSpacing(6)
        self.library_layout.setAlignment(Qt.AlignTop)
        area.setWidget(host)
        v.addWidget(area, 1)
        self.library_entries = []
        return page

    # ------------------------------------------------------------ entry points
    def opened(self):
        """Called each time the page is shown."""
        self.installed = catalog.installed_slugs()
        if not self._loaded:
            self._loaded = True
            self.load()
        if self.tabs.currentIndex() == 1:
            self.load_library()
        for card in self._cards:
            card.refresh()

    def show_kind(self, kind):
        if kind not in catalog.KINDS:
            return
        self.kind = kind
        self.kind_buttons[kind].setChecked(True)
        self.tabs.setCurrentIndex(0)
        self.load()

    def search_for(self, text):
        self.tabs.setCurrentIndex(0)
        self.search.setText(text)
        self._search_now()

    def open_slug(self, slug):
        self.open_details({"slug": slug, "title": slug,
                           "url": f"{catalog.SITE}/{slug}/"})

    def game_version(self):
        picked = self.win.selected_version() if hasattr(
            self.win, "selected_version") else None
        return (picked or {}).get("tag")

    # ------------------------------------------------------------ discover
    def _search_now(self):
        self._search_timer.stop()
        text = self.search.text().strip()
        slug = catalog.resolve(text) if ("/" in text or "." in text) \
            else None
        if slug:
            self.open_slug(slug)
            return
        if text == self._query and self.items:
            return
        self._query = text
        self.load()

    def load(self, refresh=False):
        self.page_no = 1
        self._query = self.search.text().strip()
        if "/" in self._query and catalog.resolve(self._query):
            return
        self.sort.setEnabled(not self._query)
        self.count_label.setText("Loading…")
        query, kind, sort = self._query, self.kind, self.sort.currentData()
        self._run("content-search",
                  lambda: catalog.search(query, kind, sort, 1, refresh),
                  lambda result: self._loaded_page(result, (query, kind,
                                                            sort)),
                  quiet=True)

    def load_more(self):
        if self.page_no >= self.pages:
            return
        query, kind, sort = self._query, self.kind, self.sort.currentData()
        page_no = self.page_no + 1
        self.more_btn.setEnabled(False)
        self._run("content-search",
                  lambda: catalog.search(query, kind, sort, page_no),
                  lambda result: self._loaded_page(result, (query, kind,
                                                            sort),
                                                   append=True),
                  quiet=True)

    def _loaded_page(self, result, key, append=False):
        if key != (self._query, self.kind, self.sort.currentData()):
            return          # the search changed while this one ran
        seen = {i["slug"] for i in self.items} if append else set()
        new = [i for i in result["items"] if i["slug"] not in seen]
        self.items = (self.items if append else []) + new
        self.page_no = result.get("page") or 1
        self.pages = result.get("pages") or 1
        self.total = result.get("total") or len(self.items)
        label = catalog.KINDS[self.kind][0].lower()
        self.count_label.setText(
            f"{self.total:,} {label}" + (f" for “{self._query}”"
                                         if self._query else "")
            + "  ·  MCPEDL / CurseForge")
        self.more_btn.setVisible(self.page_no < self.pages)
        self.more_btn.setEnabled(True)
        if append:
            self._add_cards(new)
        else:
            self._render()
            self.area.verticalScrollBar().setValue(0)
        self._request_images(i.get("image") for i in new)

    def _column_count(self):
        width = max(1, self.area.viewport().width() - 6)
        return max(1, width // (CARD_WIDTH + 10))

    def _render(self):
        while self.grid.count():
            child = self.grid.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        self._cards = []
        self._columns = self._column_count()
        viewport = max(CARD_WIDTH, self.area.viewport().width() - 6)
        self._card_width = max(CARD_WIDTH, (viewport - 10 * (
            self._columns - 1)) // self._columns)
        if not self.items:
            empty = QLabel("Nothing found. Try other words, or another kind."
                           if self._query else
                           "MCPEDL cannot be reached right now.")
            empty.setObjectName("Sub")
            empty.setAlignment(Qt.AlignCenter)
            self.grid.addWidget(empty, 0, 0)
            return
        self._add_cards(self.items)

    def _add_cards(self, items):
        for item in items:
            index = len(self._cards)
            card = ContentCard(self, item, getattr(self, "_card_width",
                                                   CARD_WIDTH))
            self._cards.append(card)
            self.grid.addWidget(card, index // self._columns,
                                index % self._columns)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._cards and self._column_count() != self._columns:
            self._render()
        elif self._cards and abs(self._cards[0].width() - (
                (max(CARD_WIDTH, self.area.viewport().width() - 6)
                 - 10 * (self._columns - 1)) // self._columns)) > 2:
            self._render()

    # ------------------------------------------------------------ pictures
    def _request_images(self, urls):
        added = False
        for url in urls:
            if url and url not in self._images and url not in self._requested:
                self._requested.add(url)
                self._pending_images.put(url)
                added = True
        if added:
            self._ensure_loader()

    def _ensure_loader(self):
        current = self.win._workers.get("content-images")
        if current is not None and current.isRunning():
            return
        if self._pending_images.empty():
            return
        loader = ImageLoader(self._pending_images)
        loader.loaded.connect(self._image_loaded)
        # Whatever was queued as it stopped is fetched by the next one.
        loader.finished.connect(self._ensure_loader)
        self.win._start_worker("content-images", loader)

    def _image_loaded(self, url, data):
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            return
        self._images[url] = pixmap
        self._scaled = {k: v for k, v in self._scaled.items() if k[0] != url}
        for card in self._cards:
            if card.item.get("image") == url:
                card.set_image()
        if self._details is not None and \
                self._details.item.get("image") == url:
            self._details.set_image()

    def picture(self, url, width, height, title=""):
        """The picture at ``url`` for a ``width``×``height`` slot, or a tile
        with the project's initials until it has loaded."""
        key = (url, width, height)
        if key in self._scaled:
            return self._scaled[key]
        source = self._images.get(url)
        if source is None:
            if url:
                self._request_images([url])
            return _placeholder(width, height, self.win.theme.card3,
                                _initials(title))
        self._scaled[key] = _cover(source, width, height)
        return self._scaled[key]

    # ------------------------------------------------------------ details
    def open_details(self, item):
        if self._details is not None:
            self._details.close()
        self._details = ContentDetails(self, item)
        self._details.finished.connect(self._details_closed)
        self._details.show()

    def _details_closed(self, *_):
        self._details = None

    def load_details(self, slug, then):
        def failed(message):
            if self._details is not None:
                self._details.close()
            QMessageBox.warning(self.win, "Add-ons & Maps", message)

        self._run("content-detail", lambda: catalog.details(slug), then,
                  on_fail=failed)

    # ------------------------------------------------------------ install
    def install(self, item, file, dialog=None):
        if self.busy:
            return
        from .prefix import _mc_running  # noqa: PLC0415

        title = item.get("title") or item.get("slug")
        self._set_op(f"Installing {title}…")

        def work(progress=None):
            return catalog.install(item, file, progress=progress)

        def done(results):
            self._set_busy(False)
            self.installed = catalog.installed_slugs()
            message = f"Installed {title}: " + ", ".join(results)
            if _mc_running():
                message += ". Minecraft is running: restart it to see it."
            self._set_op(message)
            for card in self._cards:
                card.refresh()
            if self._details is not None:
                self._details.refresh()
            if self.tabs.currentIndex() == 1:
                self.load_library()

        def failed(message):
            self._set_busy(False)
            self._set_op(message)
            QMessageBox.warning(self.win, f"Could not install {title}",
                                message)

        if self._run("content-install", work, done, on_fail=failed):
            self._set_busy(True)

    def import_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self.win, "Import Minecraft content", "",
            "Minecraft content (*.mcpack *.mcaddon *.mcworld *.mctemplate "
            "*.mcskin *.zip);;All files (*.*)")
        if not files:
            return
        from .content import import_content  # noqa: PLC0415

        def work():
            done, errors = [], []
            for f in files:
                try:
                    done.extend(import_content(f))
                except Exception as exc:  # noqa: BLE001 - listed to the user
                    errors.append(f"{Path(f).name}: {exc}")
            return done, errors

        def finished(result):
            done, errors = result
            self._set_busy(False)
            self._set_op(f"Imported {len(done)} item(s)." if done
                         else "Nothing imported.")
            if errors:
                QMessageBox.warning(self.win, "Import",
                                    "Problems:\n• " + "\n• ".join(errors))
            self.load_library()

        self._set_op("Importing…")
        if self._run("content-install", work, finished):
            self._set_busy(True)

    # ------------------------------------------------------------ library
    def _on_tab(self, index):
        if index == 1:
            self.load_library()

    def load_library(self):
        self._run("content-library", catalog.library, self._library_loaded,
                  quiet=True)

    def _library_loaded(self, entries):
        self.library_entries = entries
        self.installed = catalog.installed_slugs()
        self._paint_library()

    def _paint_library(self):
        layout = self.library_layout
        while layout.count():
            child = layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        needle = self.library_filter.text().strip().lower()
        wanted = self.library_kind.currentData() or ""
        shown = [e for e in self.library_entries
                 if (not wanted or e["sub"] == wanted)
                 and (not needle or needle in e["name"].lower())]
        for entry in shown:
            layout.addWidget(LibraryRow(self, entry))
        if not shown:
            empty = QLabel("Nothing here yet. Find add-ons and maps in "
                           "Discover, or import a file."
                           if not self.library_entries else "No match.")
            empty.setObjectName("Sub")
            empty.setAlignment(Qt.AlignCenter)
            layout.addWidget(empty)
        count = len(self.library_entries)
        self.tabs.setTabText(1, f"Library ({count})" if count else "Library")

    def remove(self, entry):
        answer = QMessageBox.question(
            self.win, "Remove from the game",
            f"Take “{entry['name']}” ({entry['kind'].lower()}) out of the "
            "game?\n\nIt is moved to the launcher's trash and kept there for "
            "30 days.", QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel)
        if answer != QMessageBox.Yes:
            return

        def done(_target):
            self._set_op(f"Removed {entry['name']}.")
            self.load_library()

        self._run("content-library", lambda: catalog.remove(entry["path"]),
                  done)

    # ------------------------------------------------------------ plumbing
    def _run(self, slot, fn, then, quiet=False, on_fail=None):
        from .gui import Worker  # noqa: PLC0415
        worker = Worker(fn)
        worker.done.connect(then)
        worker.failed.connect(on_fail or (
            lambda message: self._failed(message, quiet)))
        worker.progress.connect(self._progress)
        return self.win._start_worker(slot, worker)

    def _failed(self, message, quiet):
        self._set_busy(False)
        self._set_op(message)
        if self.count_label.text() == "Loading…":
            self.count_label.setText("")
            if not self.items:
                self._render()
        if not quiet:
            QMessageBox.warning(self.win, "Add-ons & Maps", message)

    def _progress(self, got, total):
        if total > 0:
            self.op_bar.setRange(0, 1000)
            self.op_bar.setValue(int(got * 1000 / total))

    def _set_op(self, text):
        self.op_text = str(text)[:200]
        self.op_label.setText(self.op_text)
        if self._details is not None:
            self._details.refresh()

    def _set_busy(self, on):
        self.busy = on
        self.op_bar.setVisible(on)
        if on:
            self.op_bar.setRange(0, 0)
        if self._details is not None:
            self._details.refresh()

    def _open_game_folder(self):
        from .content import game_content_dir  # noqa: PLC0415
        path = game_content_dir()
        path.mkdir(parents=True, exist_ok=True)
        self.open_path(path)

    def _open_trash(self):
        catalog.TRASH.mkdir(parents=True, exist_ok=True)
        self.open_path(catalog.TRASH)

    def open_path(self, path):
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)

    def open_url(self, url):
        if url and str(url).startswith("https://"):
            self.open_path(url)

    def sizeHint(self):
        return QSize(960, 640)


# ---------------------------------------------------------------- home shelf

class FeaturedShelf(QFrame):
    """This week's popular projects, on the home page.

    Speaks the same small interface a ContentCard expects of its page
    (``win``, ``installed``, ``picture``, ``open_details``), so the home page
    shows the very cards Discover does; opening one goes to its page there.
    """

    KINDS = ("addons", "maps", "textures")
    VISIBLE = 4

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setObjectName("Card")
        self.kind = "addons"
        self.installed = set()
        self.shelves = {}
        self._images = {}
        self._scaled = {}
        self._pending_images = queue.Queue()
        self._requested = set()
        self._cards = []
        self._loading = False
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 14, 18, 14)
        v.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(6)
        title = QLabel("Popular this week")
        title.setObjectName("Title")
        head.addWidget(title)
        head.addSpacing(10)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons = {}
        for key in self.KINDS:
            b = QPushButton(catalog.KINDS[key][0])
            b.setObjectName("Toggle")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(26)
            b.clicked.connect(lambda _=False, k=key: self.show_kind(k))
            self.group.addButton(b)
            self.buttons[key] = b
            head.addWidget(b)
        self.buttons[self.kind].setChecked(True)
        head.addStretch(1)
        head.addWidget(_button("See all →", "GhostSmall",
                               lambda: win.open_content(kind=self.kind),
                               tip="Browse everything on MCPEDL and "
                                   "CurseForge"))
        v.addLayout(head)
        self.row = QHBoxLayout()
        self.row.setSpacing(10)
        self.row.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        v.addLayout(self.row)
        v.addStretch(1)
        self.note = QLabel("Loading from MCPEDL…")
        self.note.setObjectName("Sub")
        self.row.addWidget(self.note)

    def load(self, refresh=False):
        if self._loading:
            return
        self._loading = True
        from .gui import Worker  # noqa: PLC0415
        worker = Worker(lambda: catalog.featured(self.KINDS, self.VISIBLE + 2,
                                                 refresh))
        worker.done.connect(self._loaded)
        worker.failed.connect(self._failed)
        if not self.win._start_worker("home-featured", worker):
            self._loading = False

    def _loaded(self, shelves):
        self._loading = False
        self.shelves = shelves
        self.installed = catalog.installed_slugs()
        self._render()
        urls = [i.get("image") for items in shelves.values() for i in items]
        self._request_images(urls)

    def _failed(self, _message):
        self._loading = False
        self.note.setText("MCPEDL cannot be reached right now.")

    def show_kind(self, kind):
        self.kind = kind
        self.buttons[kind].setChecked(True)
        self._render()

    def _visible(self):
        width = max(1, self.width() - 36)
        return max(1, min(self.VISIBLE + 2, (width + 10) // (CARD_WIDTH + 10)))

    def _render(self):
        while self.row.count():
            child = self.row.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        self._cards = []
        self.note = None
        items = self.shelves.get(self.kind) or []
        if not items:
            note = QLabel("MCPEDL cannot be reached right now."
                          if self.shelves else "Loading from MCPEDL…")
            note.setObjectName("Sub")
            self.row.addWidget(note)
            self.note = note
            return
        width, thumb = self._card_size()
        for item in items[:self._visible()]:
            card = ContentCard(self, item, width, thumb)
            self._cards.append(card)
            self.row.addWidget(card)
        self.row.addStretch(1)

    # Header row, margins, and a card's title, author and stats lines.
    _CHROME_HEIGHT = 64
    _CARD_TEXT_HEIGHT = 112

    def _card_size(self):
        """Card width filling the row, and a picture height the shelf has
        room for: a short window takes it from the pictures, not the text."""
        capacity = self._visible()
        width = max(CARD_WIDTH, (self.width() - 36 - 10 * (capacity - 1))
                    // capacity)
        room = self.height() - self._CHROME_HEIGHT - self._CARD_TEXT_HEIGHT
        thumb = max(64, min(150, int((width - 16) * 0.56), room))
        return width, thumb

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self._cards:
            return
        width, thumb = self._card_size()
        if (len(self._cards) != min(self._visible(),
                                    len(self.shelves.get(self.kind) or []))
                or abs(self._cards[0].width() - width) > 2
                or abs(self._cards[0]._thumb_size[1] - thumb) > 2):
            self._render()

    def open_details(self, item):
        self.win.open_content(slug=item.get("slug"))

    # The picture machinery is ContentPage's, with its own queue.
    _request_images = ContentPage._request_images
    picture = ContentPage.picture

    def _ensure_loader(self):
        current = self.win._workers.get("home-images")
        if current is not None and current.isRunning():
            return
        if self._pending_images.empty():
            return
        loader = ImageLoader(self._pending_images)
        loader.loaded.connect(self._image_loaded)
        loader.finished.connect(self._ensure_loader)
        self.win._start_worker("home-images", loader)

    def _image_loaded(self, url, data):
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            return
        self._images[url] = pixmap
        self._scaled = {k: v for k, v in self._scaled.items() if k[0] != url}
        for card in self._cards:
            if card.item.get("image") == url:
                card.set_image()
