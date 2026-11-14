"""bol.icons — the launcher's line icons, drawn from SVG in the theme's colour.

One stroke weight, one 24-unit grid, round joins: the menu, the home tiles
and the profile chip all read as one set, and follow the theme because each
is painted in the colour asked for rather than shipped as an image. Without
QtSvg (a stripped Qt) an icon is simply empty; nothing else depends on it.
"""
# SPDX-License-Identifier: MIT

from __future__ import annotations

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap

try:
    from PySide6.QtSvg import QSvgRenderer
except ImportError:  # pragma: no cover - every supported build has QtSvg
    QSvgRenderer = None

# Path data on a 24×24 grid, stroked, never filled.
_PATHS = {
    "home": "M3.5 10.5 12 4l8.5 6.5M5.5 9v10.5h4.5v-6h4v6h4.5V9",
    "addons": ("M12 3 20 7.5v9L12 21l-8-4.5v-9L12 3zM4 7.5l8 4.5 8-4.5"
               "M12 12v9"),
    "mods": "M5 7h4a2 2 0 0 1 4 0h4v4a2 2 0 0 1 0 4v4H5z",
    "profiles": ("M9 11.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7zM2.5 20"
                 "c.4-3.6 3-5.8 6.5-5.8s6.1 2.2 6.5 5.8M15.5 4.6a3.5 3.5 0 0 1"
                 " 0 6.8M17.5 14.6c2.2.6 3.6 2.6 4 5.4"),
    "settings": ("M4 7h9M17 7h3M4 12h3M11 12h9M4 17h11M19 17h1"
                 "M15 5v4M9 10v4M17 15v4"),
    "news": "M12 3.5l2 5.7 5.5 2.3-5.5 2.3-2 5.7-2-5.7-5.5-2.3 5.5-2.3z",
    "user": ("M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4.5 20.5"
             "c.6-3.9 3.6-6.3 7.5-6.3s6.9 2.4 7.5 6.3"),
    "import": "M12 3.5v11M7.5 10l4.5 4.5 4.5-4.5M4.5 15.5v4h15v-4",
    "folder": ("M3.5 7a1.5 1.5 0 0 1 1.5-1.5h4l2 2h8a1.5 1.5 0 0 1 1.5 1.5"
               "v9a1.5 1.5 0 0 1-1.5 1.5H5A1.5 1.5 0 0 1 3.5 18V7z"),
    "plus": "M12 5v14M5 12h14",
    "more": ("M5 11.3a.7.7 0 1 0 0 1.4.7.7 0 0 0 0-1.4z"
             "M12 11.3a.7.7 0 1 0 0 1.4.7.7 0 0 0 0-1.4z"
             "M19 11.3a.7.7 0 1 0 0 1.4.7.7 0 0 0 0-1.4z"),
    "game": ("M6.5 8h11a3 3 0 0 1 3 3v3.5a3 3 0 0 1-5.4 1.8L14 15h-4"
             "l-1.1 1.3a3 3 0 0 1-5.4-1.8V11a3 3 0 0 1 3-3zM8 10.5v3"
             "M6.5 12h3M15.5 11.5h.01M17.5 13.5h.01"),
}

_RATIO = 2      # drawn at twice the size, for HiDPI screens


def svg(name, color, stroke=1.8):
    path = _PATHS.get(name)
    if path is None:
        return None
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
            f'fill="none" stroke="{color}" stroke-width="{stroke}" '
            'stroke-linecap="round" stroke-linejoin="round">'
            f'<path d="{path}"/></svg>')


def pixmap(name, color, size=20):
    """``name`` drawn in ``color`` at ``size`` logical pixels."""
    out = QPixmap(size * _RATIO, size * _RATIO)
    out.setDevicePixelRatio(_RATIO)
    out.fill(Qt.transparent)
    source = svg(name, color)
    if source is None or QSvgRenderer is None:
        return out
    renderer = QSvgRenderer(QByteArray(source.encode()))
    painter = QPainter(out)
    painter.setRenderHint(QPainter.Antialiasing)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return out


def toggle_icon(name, normal, hover, checked, size=20):
    """An icon for a checkable button: one colour off, one on hover, one
    when checked."""
    icon = QIcon()
    icon.addPixmap(pixmap(name, normal, size), QIcon.Normal, QIcon.Off)
    icon.addPixmap(pixmap(name, hover, size), QIcon.Active, QIcon.Off)
    icon.addPixmap(pixmap(name, checked, size), QIcon.Normal, QIcon.On)
    icon.addPixmap(pixmap(name, checked, size), QIcon.Active, QIcon.On)
    return icon
