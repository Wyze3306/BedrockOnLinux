"""bol.shortcuts — app menu and desktop shortcuts, one Minecraft each (#392).

A shortcut names exactly what it starts: a game (an edition), a build of it
or the newest one installed, the profile it plays in, and how it starts --
the launcher opening and starting it by itself, or the game alone. It can be
put in the app menu, on the desktop, or both, with a name and an icon of its
own. The list is kept with the main installation (``shortcuts/``), so every
profile's window shows the same shortcuts, and each menu or desktop file runs
``bedrock-on-linux shortcuts run <id>``: what the shortcut starts can change
without the file being written again.

Nothing a shortcut starts is downloaded by it. A build is played from the
folder it is installed in; one that is not there any more is reported, never
fetched. The one exception is asked for by the player: a shortcut to the
newest build may install a newer one first, the way Settings ▸ General's
"Update Minecraft when started from a shortcut" does.

Starting a shortcut selects its build in its profile, as picking it next to
PLAY does, for the time the game runs. When the game closes the profile's
previous choice comes back, so a shortcut to an old build does not leave the
launcher's own PLAY on that build.

The files point at the launcher wherever it was started from last. An
AppImage moved to another folder, a Nix profile, a .pyz: the first start
from the new place writes them again (:func:`refresh`). The Flatpak cannot
write outside its sandbox at all; it asks the desktop through the
DynamicLauncher portal, which shows its own confirmation, puts the shortcut
in the app menu, and starts it through ``flatpak run``.
"""
# SPDX-License-Identifier: MIT

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .config import APP, DATA, FLATPAK_APP_ID, HOME, PRETTY, XDG_CONFIG_HOME, \
    XDG_DATA_HOME
from .log import BolError, info, warn

#: The version of a shortcut that follows the newest build installed.
LATEST = "latest"
#: How a shortcut starts: the launcher opens and starts the game by itself,
#: or the game starts with no launcher window (the "Play without the
#: launcher" way).
START_LAUNCHER = "launcher"
START_DIRECT = "direct"
STARTS = (START_LAUNCHER, START_DIRECT)
#: Where the icon comes from: the game's own Store art, the launcher's
#: icon, or a picture the player chose.
ICON_EDITION = "edition"
ICON_LAUNCHER = "launcher"
ICON_CUSTOM = "custom"
ICONS = (ICON_EDITION, ICON_LAUNCHER, ICON_CUSTOM)
#: Every file written for a shortcut carries this key, with its id: nothing
#: without it is ever rewritten or deleted.
MARKER = "X-BedrockOnLinux-Shortcut"
#: Set in the environment of the launcher a shortcut opens, for the window
#: to start that shortcut once it is up (bol.gui_shortcuts).
START_ENV = "BOL_START_SHORTCUT"
_HOP_ENV = "BOL_SHORTCUT_HOP"

_ID = re.compile(r"[0-9a-f]{8}")
_NAME_MAX = 80
_ICON_SIZE = 256
_ICON_SOURCE_MAX = 16 << 20

DEFAULTS = {
    "name": "",
    "edition": "release",
    "version": LATEST,
    "profile": "",
    "start": START_LAUNCHER,
    "editor": False,
    "hidden": False,
    "update": False,
    "env": "",
    "icon": ICON_EDITION,
    "badge": True,
    "menu": True,
    "desktop": False,
}

#: What a shortcut is called by default: the game, as players name it.
_GAME_NAMES = {
    "release": "Minecraft Bedrock",
    "preview": "Minecraft Preview",
    "education": "Minecraft Education",
    "uwp": "Minecraft for Windows 10",
    "dungeons": "Minecraft Dungeons",
    "legends": "Minecraft Legends",
}


# ---------------------------------------------------------------- where

def root(base_data=None):
    """``shortcuts/`` of the main installation, whichever profile asks."""
    from .profiles import _profile_base
    return _profile_base(base_data) / "shortcuts"


def _registry(base_data=None):
    return root(base_data) / "shortcuts.json"


def icons_dir(base_data=None):
    return root(base_data) / "icons"


def icon_file(shortcut, base_data=None):
    """The icon the menu and desktop files show (a 256 px PNG)."""
    return icons_dir(base_data) / f"{shortcut['id']}.png"


def _icon_source(shortcut, base_data=None):
    """A custom icon's own picture, kept to compose it again later."""
    return icons_dir(base_data) / f"{shortcut['id']}-source.png"


def applications_dir():
    return XDG_DATA_HOME / "applications"


def desktop_dir(environ=None, config_home=None, home=None):
    """The desktop folder: XDG_DESKTOP_DIR, as xdg-user-dirs writes it.

    Read from ``user-dirs.dirs`` rather than asked of ``xdg-user-dir``, which
    is not everywhere; a French desktop is ~/Bureau, a German one ~/Schreibtisch.
    """
    env = os.environ if environ is None else environ
    home = Path(home or HOME)
    value = env.get("XDG_DESKTOP_DIR", "").strip()
    if not value:
        dirs = Path(config_home or XDG_CONFIG_HOME) / "user-dirs.dirs"
        try:
            for line in dirs.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("XDG_DESKTOP_DIR="):
                    value = line.split("=", 1)[1].strip().strip('"')
                    break
        except (OSError, UnicodeError):
            pass
    if value:
        value = value.replace("$HOME", str(home)).replace("${HOME}", str(home))
        path = Path(value).expanduser()
        if path.is_absolute() and path != home:
            return path
    return home / "Desktop"


def menu_file(shortcut):
    return applications_dir() / f"{APP}-shortcut-{shortcut['id']}.desktop"


def in_flatpak(environ=None, info_path=Path("/.flatpak-info")):
    source = os.environ if environ is None else environ
    return bool(source.get("FLATPAK_ID")) or Path(info_path).is_file()


def portal_file_id(shortcut):
    """The desktop file id the portal installs a Flatpak shortcut under; it
    has to start with the app's own id."""
    return f"{FLATPAK_APP_ID}.shortcut_{shortcut['id']}.desktop"


# ---------------------------------------------------------------- the list

class _Locked:
    """The list's lock, held while it is read and written back."""

    def __init__(self, base_data=None):
        self.path = root(base_data) / ".lock"
        self.fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_exc):
        os.close(self.fd)


def _read(base_data=None):
    try:
        data = json.loads(_registry(base_data).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema": 1, "launcher": None, "shortcuts": []}
    except (OSError, UnicodeError, ValueError) as exc:
        raise BolError(f"The list of shortcuts cannot be read "
                       f"({_registry(base_data)}): {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("shortcuts"),
                                                     list):
        raise BolError(f"The list of shortcuts is not one this launcher "
                       f"wrote: {_registry(base_data)}")
    data["shortcuts"] = [_with_defaults(item) for item in data["shortcuts"]
                         if isinstance(item, dict)
                         and _ID.fullmatch(str(item.get("id", "")))]
    return data


def _write(data, base_data=None):
    target = _registry(base_data)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".shortcuts-", suffix=".json",
                                dir=target.parent)
    staged = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(staged, 0o644)
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)


def _with_defaults(item):
    merged = dict(DEFAULTS)
    merged.update(item)
    return merged


def load(base_data=None):
    """Every shortcut, in the order they were made."""
    return _read(base_data)["shortcuts"]


def get(ident, base_data=None):
    """A shortcut by its id, or by its name in any case."""
    wanted = str(ident or "").strip()
    items = load(base_data)
    for item in items:
        if item["id"] == wanted.lower():
            return item
    for item in items:
        if item["name"].casefold() == wanted.casefold():
            return item
    raise BolError(f"There is no shortcut '{wanted}'. "
                   + ("The shortcuts are: "
                      + ", ".join(f"{i['name']} ({i['id']})" for i in items)
                      + "." if items else "No shortcut has been made yet."))


# ---------------------------------------------------------------- targets

def edition(edition_id):
    from . import xodus
    return xodus.edition(edition_id)


def game_name(edition_id):
    entry = edition(edition_id) or {}
    return _GAME_NAMES.get(edition_id, entry.get("name") or "Minecraft")


def single_build(edition_id):
    """Whether the Store serves one build of this title only ("current")."""
    return (edition(edition_id) or {}).get("catalogue") == "store"


def default_name(edition_id, version=LATEST):
    """"Minecraft Bedrock (1.21.120.4)", "Minecraft Preview (Latest)"..."""
    name = game_name(edition_id)
    if single_build(edition_id):
        return name
    return f"{name} ({'Latest' if version == LATEST else version})"


def installed(edition_id):
    """The complete builds of an edition on disk, newest first."""
    from . import games, xodus
    return sorted(games.installed_versions(edition_id), key=xodus.version_key,
                  reverse=True)


def resolve(shortcut):
    """(edition entry, version, build folder) a shortcut starts now.

    Raises BolError, saying what to do, when that build is not installed:
    a shortcut never downloads the build it names.
    """
    from . import games
    entry = edition(shortcut["edition"])
    if entry is None:
        raise BolError(f"This launcher does not know the game "
                       f"'{shortcut['edition']}' the shortcut "
                       f"'{shortcut['name']}' starts.")
    builds = installed(entry["id"])
    if shortcut["version"] == LATEST:
        if not builds:
            raise BolError(
                f"{game_name(entry['id'])} is not installed. Open "
                f"{PRETTY} and install it, then use the shortcut "
                f"'{shortcut['name']}' again.")
        version = builds[0]
    else:
        version = shortcut["version"]
        if version not in builds:
            raise BolError(
                f"{game_name(entry['id'])} {version} is not installed any "
                f"more. Open {PRETTY} and install it again, or change the "
                f"shortcut '{shortcut['name']}' to another version.")
    return entry, version, games.version_dir(entry["id"], version)


def describe(shortcut):
    """One line on what a shortcut starts: "Minecraft Bedrock 1.21.120.4"."""
    name = game_name(shortcut["edition"])
    if single_build(shortcut["edition"]):
        return name
    if shortcut["version"] == LATEST:
        return f"{name}, newest installed"
    return f"{name} {shortcut['version']}"


def profile_folder(shortcut, base_data=None):
    """The profile folder a shortcut plays in; None for Default."""
    from .profiles import find_profile
    return find_profile(shortcut["profile"] or "Default", base_data)


def profile_label(shortcut, base_data=None):
    if not shortcut["profile"]:
        return "Default"
    from .profiles import list_profiles
    for item in list_profiles(base_data):
        if item.get("slug") == shortcut["profile"]:
            return str(item.get("name"))
    return shortcut["profile"]


# ---------------------------------------------------------------- checks

def _check(fields, base_data=None):
    """``fields`` made into a shortcut, or BolError saying what is wrong."""
    item = _with_defaults(fields)
    name = " ".join(str(item["name"]).split())
    if not name:
        name = default_name(item["edition"], item["version"])
    if len(name) > _NAME_MAX:
        raise BolError(f"A shortcut's name is at most {_NAME_MAX} characters.")
    item["name"] = name
    entry = edition(str(item["edition"]))
    if entry is None:
        from . import xodus
        raise BolError(f"Unknown game '{item['edition']}'; expected "
                       + ", ".join(e["id"] for e in xodus.list_editions())
                       + ".")
    item["edition"] = entry["id"]
    version = str(item["version"] or LATEST).strip()
    if version.lower() in (LATEST, "newest", "current") or \
            single_build(entry["id"]):
        version = LATEST
    elif "/" in version or version.startswith("."):
        raise BolError(f"'{version}' is not a version.")
    item["version"] = version
    if item["start"] not in STARTS:
        raise BolError(f"A shortcut starts with 'launcher' or 'direct', not "
                       f"'{item['start']}'.")
    profile = str(item["profile"] or "").strip()
    if profile.casefold() in ("", "default"):
        item["profile"] = ""
    else:
        from .profiles import find_profile
        folder = find_profile(profile, base_data)
        item["profile"] = folder.name
    for flag in ("editor", "hidden", "update", "badge", "menu", "desktop"):
        item[flag] = bool(item[flag])
    from .games import is_bedrock
    if item["editor"] and not is_bedrock(entry):
        raise BolError(f"Bedrock Editor is part of Minecraft for Windows, "
                       f"not of {entry['name']}.")
    if item["update"] and item["version"] != LATEST:
        # A shortcut to one build is that build: installing a newer one
        # first would start something else.
        item["update"] = False
    env = str(item["env"] or "").strip()
    try:
        shlex.split(env)
    except ValueError as exc:
        raise BolError(f"The environment variables cannot be read: {exc}") \
            from exc
    for token in shlex.split(env):
        if "=" not in token or not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*", token.split("=", 1)[0]):
            raise BolError(f"'{token}' is not an environment variable; "
                           "write them as NAME=value, separated by spaces.")
    item["env"] = env
    if item["icon"] not in ICONS:
        raise BolError(f"Unknown icon kind '{item['icon']}'.")
    if not (item["menu"] or item["desktop"]):
        raise BolError("Put the shortcut in the app menu, on the desktop, "
                       "or both.")
    if item["desktop"] and in_flatpak():
        raise BolError(
            "The Flatpak cannot put a shortcut on the desktop: it can only "
            "ask the desktop to add one to the app menu. From the menu, most "
            "desktops can then add it to the desktop or a panel.")
    return item


def environment(shortcut):
    """The variables a shortcut sets for its game, as a dict."""
    from .util import custom_env_map
    return custom_env_map(shortcut.get("env") or "")


# ---------------------------------------------------------------- launcher

def launcher_argv(environ=None, argv0=None, executable=None,
                  which=shutil.which, package_dir=None):
    """The command that starts this launcher, as it was started.

    Where it was started from decides, so that an AppImage, a .pyz or a
    checkout run next to an installed copy point shortcuts at themselves.
    A store path that Nix or Guix wraps is replaced by the profile's link to
    the same program, which an upgrade keeps.
    """
    env = os.environ if environ is None else environ
    python = executable or sys.executable or "python3"
    appimage = env.get("APPIMAGE", "").strip()
    if appimage and Path(appimage).expanduser().is_file():
        return [str(Path(appimage).expanduser().resolve())]
    raw = sys.argv[0] if argv0 is None and sys.argv else (argv0 or "")
    path = Path(raw).expanduser() if raw else None
    if path is not None and path.suffix == ".pyz" and path.is_file():
        return [python, str(path.resolve())]
    if path is not None and path.name == "__main__.py" \
            and path.parent.name == "bol":
        # python3 -m bol, from a source tree or a wheel.
        source = Path(package_dir or path.parent).resolve().parent
        return ["env", f"PYTHONPATH={source}", python, "-m", "bol"]
    installed_path = which(APP)
    if path is not None and path.is_file():
        real = path.resolve()
        if installed_path:
            try:
                same = Path(installed_path).resolve().parent == real.parent
            except OSError:
                same = False
            if same:
                return [installed_path]
        return [str(real)]
    if installed_path:
        return [installed_path]
    from .profiles import launcher_executable
    return [launcher_executable()]


_RESERVED = set(" \t\n\"'\\><~|&;$*?#()`")


def _exec_arg(value):
    """One argument of an Exec key, quoted the way the desktop entry
    specification wants: inside double quotes, '"', '`', '$' and '\\' are
    escaped, and '%' is always doubled."""
    text = str(value).replace("%", "%%")
    if text and not any(c in _RESERVED for c in text):
        return text
    return '"' + re.sub(r'(["`$\\])', r"\\\1", text) + '"'


def _string(value):
    """A string value of a desktop entry: the general escapes applied."""
    text = str(value)
    text = text.replace("\\", "\\\\").replace("\n", "\\n") \
        .replace("\t", "\\t").replace("\r", "\\r")
    return "\\s" + text[1:] if text.startswith(" ") else text


def exec_line(shortcut, argv=None):
    """The Exec key of a shortcut's files."""
    args = list(argv or launcher_argv()) + ["shortcuts", "run",
                                             shortcut["id"]]
    # The string escapes apply on top of the quoting: a backslash inside a
    # quoted argument is four in the file.
    return " ".join(_exec_arg(a) for a in args).replace("\\", "\\\\")


def launcher_signature(argv=None):
    """What the files depend on, to tell when they must be written again."""
    return shlex.join(argv or launcher_argv())


def desktop_entry(shortcut, argv=None, base_data=None, portal=False):
    """The text of a shortcut's menu or desktop file.

    The portal sets Name and Icon itself, and rewrites Exec to go through
    ``flatpak run``; it is given a bare ``bedrock-on-linux`` command.
    """
    comment = f"{describe(shortcut)} · profile {profile_label(shortcut, base_data)}"
    if shortcut["editor"]:
        comment += " · Bedrock Editor"
    lines = ["[Desktop Entry]", "Type=Application", "Version=1.0"]
    if not portal:
        lines.append(f"Name={_string(shortcut['name'])}")
    lines.append(f"Comment={_string(comment + ' · ' + PRETTY)}")
    if portal:
        lines.append(f"Exec={APP} shortcuts run {shortcut['id']}")
    else:
        lines.append(f"Exec={exec_line(shortcut, argv)}")
        lines.append(f"Icon={_string(icon_file(shortcut, base_data))}")
    lines += [
        "Terminal=false",
        "Categories=Game;",
        "Keywords=minecraft;bedrock;",
        f"{MARKER}={shortcut['id']}",
    ]
    return "\n".join(lines) + "\n"


def _owned(path, shortcut_id):
    """Whether ``path`` is a file this launcher wrote for that shortcut."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return f"\n{MARKER}={shortcut_id}\n" in "\n" + text


def _write_entry(path, text, executable):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".bol-shortcut-", suffix=".tmp",
                                dir=path.parent)
    staged = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(staged, 0o755 if executable else 0o644)
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def _trust(path):
    """Mark a desktop launcher as trusted, for the file managers that ask:
    GNOME's desktop icons and Nemo want ``metadata::trusted`` besides the
    executable bit, KDE the executable bit alone."""
    gio = shutil.which("gio")
    if not gio:
        return
    try:
        subprocess.run([gio, "set", "-t", "string", str(path),
                        "metadata::trusted", "true"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def _desktop_name(name, desktop, shortcut_id):
    """A desktop file named after the shortcut, which is what a desktop
    whose launchers are not trusted yet shows."""
    stem = re.sub(r"[/\\\0]", "-", name).strip(" .") or "Minecraft"
    for count in range(1, 100):
        candidate = desktop / (stem + (f" ({count})" if count > 1 else "")
                               + ".desktop")
        if not candidate.exists() or _owned(candidate, shortcut_id):
            return candidate
    return desktop / f"{APP}-shortcut-{shortcut_id}.desktop"


# ---------------------------------------------------------------- icons

def _qt_gui():
    """A QGuiApplication for painting text, made offscreen if none exists."""
    from PySide6.QtGui import QGuiApplication
    app = QGuiApplication.instance()
    if app is None:
        app = QGuiApplication([APP, "-platform", "offscreen"])
    return app


def launcher_icon_path(module_file=None):
    """The launcher's own icon file, wherever this package put it (the same
    places as bol.gui.icon_candidates, which needs Qt to be imported)."""
    here = Path(module_file or __file__).resolve().parent
    candidates = [here.parent / "data/icon.png", here / "data/icon.png"]
    if len(here.parents) > 3 and here.parent.name in ("site-packages",
                                                       "dist-packages"):
        candidates.append(here.parents[3] / "share/icons/hicolor/256x256/"
                          "apps/bedrock-on-linux.png")
    candidates += [
        Path("/app/share/icons/hicolor/256x256/apps/"
             f"{FLATPAK_APP_ID}.png"),
        Path("/usr/share/icons/hicolor/256x256/apps/bedrock-on-linux.png"),
    ]
    return next((p for p in candidates if p.is_file()), None)


def _art_bytes(shortcut, base_data=None):
    if shortcut["icon"] == ICON_CUSTOM:
        try:
            return _icon_source(shortcut, base_data).read_bytes()
        except OSError:
            pass
    elif shortcut["icon"] == ICON_EDITION:
        from . import storeart
        try:
            data = storeart.logo_bytes(
                (edition(shortcut["edition"]) or {}).get("product"))
        except Exception:  # noqa: BLE001 - offline: the launcher's icon
            data = None
        if data:
            return data
    path = launcher_icon_path()
    return path.read_bytes() if path else None


def badge_text(shortcut):
    """What is written on the icon, so that shortcuts to the same game can
    be told apart in a menu: the build, or that it is the UWP edition."""
    if not shortcut["badge"]:
        return ""
    if shortcut["version"] != LATEST:
        return shortcut["version"]
    if shortcut["edition"] == "uwp":
        return "W10"
    return ""


def render_icon(shortcut, base_data=None, size=_ICON_SIZE, art=None):
    """The shortcut's icon as PNG bytes, with its badge drawn on.

    ``art`` is the picture to draw instead of the shortcut's own, for a
    preview of one not saved yet.
    """
    _qt_gui()
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, \
        QPainter, QPainterPath
    picture = QImage()
    if art is None:
        data = _art_bytes(shortcut, base_data)
    else:
        path = launcher_icon_path()
        data = art or (path.read_bytes() if path else b"")
    if data:
        picture.loadFromData(data)
    image = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)
    if not picture.isNull():
        scaled = picture.scaled(size, size, Qt.KeepAspectRatio,
                                Qt.SmoothTransformation)
        x = (size - scaled.width()) / 2
        y = (size - scaled.height()) / 2
        # Rounded like the launcher's own tiles; a picture with its own
        # transparent edges is not affected.
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(x, y, scaled.width(), scaled.height()),
                            size * 0.16, size * 0.16)
        painter.setClipPath(clip)
        painter.drawImage(int(x), int(y), scaled)
        painter.setClipping(False)
    text = badge_text(shortcut)
    if text:
        font = QFont()
        font.setBold(True)
        pixel = int(size * 0.17)
        font.setPixelSize(pixel)
        width = size * 0.94
        while pixel > 10 and QFontMetrics(font).horizontalAdvance(text) \
                > width - size * 0.08:
            pixel -= 2
            font.setPixelSize(pixel)
        metrics = QFontMetrics(font)
        box_w = min(width, metrics.horizontalAdvance(text) + size * 0.1)
        box_h = metrics.height() + size * 0.04
        box = QRectF((size - box_w) / 2, size - box_h - size * 0.03,
                     box_w, box_h)
        path = QPainterPath()
        path.addRoundedRect(box, box_h / 3, box_h / 3)
        painter.fillPath(path, QColor(16, 18, 24, 225))
        painter.setPen(QColor("#ffffff"))
        painter.setFont(font)
        painter.drawText(box, Qt.AlignCenter, text)
    painter.end()
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(QByteArray(buffer.data()))


def set_custom_icon(shortcut, picture, base_data=None):
    """Keep a picture the player chose as a shortcut's own icon."""
    source = Path(picture).expanduser()
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise BolError(f"The picture cannot be read: {exc}") from exc
    if size > _ICON_SOURCE_MAX:
        raise BolError("That picture is too large for an icon "
                       f"({size // (1 << 20)} MiB).")
    _qt_gui()
    from PySide6.QtGui import QImage
    from PySide6.QtCore import Qt
    image = QImage(str(source))
    if image.isNull():
        raise BolError(f"{source.name} is not a picture this launcher can "
                       "read (PNG, JPEG, SVG, ICO, BMP or WebP).")
    if max(image.width(), image.height()) > 512:
        image = image.scaled(512, 512, Qt.KeepAspectRatio,
                             Qt.SmoothTransformation)
    target = _icon_source(shortcut, base_data)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(target), "PNG"):
        raise BolError(f"The icon could not be saved in {target.parent}.")


def _write_icon(shortcut, base_data=None):
    target = icon_file(shortcut, base_data)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = render_icon(shortcut, base_data)
    except ImportError:
        # No Qt in this interpreter (a .pyz before its first window): the
        # picture as it is, when it already is a PNG.
        data = _art_bytes(shortcut, base_data) or b""
        if not data.startswith(b"\x89PNG"):
            path = launcher_icon_path()
            data = path.read_bytes() if path else b""
    if data:
        staged = target.with_name(f".{target.name}.tmp")
        staged.write_bytes(data)
        os.replace(staged, target)
    return target


# ---------------------------------------------------------------- portal

class PortalError(BolError):
    """The desktop's DynamicLauncher portal refused or is missing."""


def _portal_call(bus, method, args):
    from PySide6.QtDBus import QDBusMessage
    message = QDBusMessage.createMethodCall(
        "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
        "org.freedesktop.portal.DynamicLauncher", method)
    message.setArguments(args)
    reply = bus.call(message, timeout=30000)
    if reply.type() == QDBusMessage.ErrorMessage:
        detail = reply.errorMessage() or reply.errorName()
        raise PortalError(
            "The desktop did not add the shortcut: its DynamicLauncher "
            f"portal answered \"{detail}\". It needs xdg-desktop-portal 1.17 "
            "or later with a desktop backend that offers it (GNOME, KDE, "
            "GTK).")
    return reply.arguments()


def portal_install(shortcut, icon_png, timeout=600, base_data=None):
    """Ask the desktop to add a Flatpak shortcut to the app menu.

    The portal shows its own dialog, where the player may rename the
    shortcut or decline. Returns the name it was added under.
    """
    _qt_gui()
    from PySide6.QtCore import SLOT, QByteArray, QEventLoop, QObject, QTimer, Slot
    from PySide6.QtDBus import QDBusArgument, QDBusConnection, QDBusVariant
    bus = QDBusConnection.sessionBus()
    if not bus.isConnected():
        raise PortalError("There is no D-Bus session to ask the desktop for "
                          "an app menu shortcut.")
    token = "bol" + secrets.token_hex(6)
    sender = bus.baseService().lstrip(":").replace(".", "_")
    request = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
    answer = {}
    loop = QEventLoop()

    class Listener(QObject):
        # Typed, so QtDBus hands over the results as a dict: PySide cannot
        # read the a{sv} out of a raw QDBusMessage (its QDBusArgument only
        # binds the writing half).
        @Slot("uint", "QVariantMap")
        def response(self, code, results):
            answer["code"] = int(code)
            answer["results"] = dict(results or {})
            loop.quit()

    listener = Listener()
    bus.connect("org.freedesktop.portal.Desktop", request,
                "org.freedesktop.portal.Request", "Response", listener,
                SLOT("response(uint,QVariantMap)"))
    icon = QDBusArgument()
    icon.beginStructure()
    icon.appendVariant("bytes")
    icon.appendVariant(QDBusVariant(QByteArray(icon_png)))
    icon.endStructure()
    try:
        _portal_call(bus, "PrepareInstall", [
            "", shortcut["name"], QDBusVariant(icon),
            # No launcher_type: it is a uint the portal checks strictly, and
            # QtDBus sends a Python int as a signed one. Its default is the
            # one wanted here, an application.
            {"handle_token": token, "modal": True,
             "editable_name": True, "editable_icon": False}])
        QTimer.singleShot(int(timeout * 1000), loop.quit)
        loop.exec()
    finally:
        bus.disconnect("org.freedesktop.portal.Desktop", request,
                       "org.freedesktop.portal.Request", "Response",
                       listener, SLOT("response(uint,QVariantMap)"))
    if "code" not in answer:
        # Take the portal's dialog down rather than leave it to a click
        # that would no longer do anything.
        from PySide6.QtDBus import QDBusMessage
        bus.call(QDBusMessage.createMethodCall(
            "org.freedesktop.portal.Desktop", request,
            "org.freedesktop.portal.Request", "Close"), timeout=5000)
        raise PortalError("The desktop did not answer the request to add "
                          "the shortcut.")
    if answer["code"] != 0:
        raise PortalError("The shortcut was not added: the request was "
                          "declined.")
    results = answer.get("results") or {}
    install_token = str(results.get("token") or "")
    name = str(results.get("name") or shortcut["name"])
    if not install_token:
        raise PortalError("The desktop accepted the shortcut but gave no "
                          "token to install it with.")
    _portal_call(bus, "Install", [
        install_token, portal_file_id(shortcut),
        desktop_entry(shortcut, base_data=base_data, portal=True), {}])
    return name


def portal_uninstall(shortcut):
    _qt_gui()
    from PySide6.QtDBus import QDBusConnection
    bus = QDBusConnection.sessionBus()
    if not bus.isConnected():
        return False
    try:
        _portal_call(bus, "Uninstall", [portal_file_id(shortcut), {}])
    except PortalError:
        return False
    return True


# ---------------------------------------------------------------- files

def _remove_files(shortcut, keep_menu=False, keep_desktop=False):
    """Delete what was written for a shortcut, and only that."""
    if in_flatpak():
        if not keep_menu:
            portal_uninstall(shortcut)
        return
    menu = menu_file(shortcut)
    if not keep_menu and _owned(menu, shortcut["id"]):
        menu.unlink(missing_ok=True)
    desktop = shortcut.get("desktop_file")
    if desktop and not keep_desktop:
        path = Path(desktop)
        if _owned(path, shortcut["id"]):
            path.unlink(missing_ok=True)


def install(shortcut, argv=None, base_data=None, ask_portal=True):
    """Write a shortcut's files where it is wanted, and its icon; remove
    the ones from places it is no longer wanted. Returns the shortcut with
    ``desktop_file`` updated."""
    item = dict(shortcut)
    _write_icon(item, base_data)
    if in_flatpak():
        if ask_portal:
            data = icon_file(item, base_data).read_bytes()
            item["name"] = portal_install(item, data, base_data=base_data)
        return item
    text = desktop_entry(item, argv, base_data)
    if item["menu"]:
        _write_entry(menu_file(item), text, executable=False)
    else:
        _remove_files(item, keep_desktop=True)
    old_desktop = item.get("desktop_file")
    if item["desktop"]:
        folder = desktop_dir()
        path = _desktop_name(item["name"], folder, item["id"])
        if old_desktop and Path(old_desktop) != path \
                and _owned(Path(old_desktop), item["id"]):
            Path(old_desktop).unlink(missing_ok=True)
        _write_entry(path, text, executable=True)
        _trust(path)
        item["desktop_file"] = str(path)
    else:
        if old_desktop and _owned(Path(old_desktop), item["id"]):
            Path(old_desktop).unlink(missing_ok=True)
        item.pop("desktop_file", None)
    return item


def _store(items, base_data=None, launcher=None):
    data = _read(base_data)
    data["shortcuts"] = items
    if launcher is not None:
        data["launcher"] = launcher
    _write(data, base_data)


def create(fields, icon_picture=None, base_data=None):
    """Make a shortcut, write its files, and keep it in the list."""
    item = _check(fields, base_data)
    item["id"] = secrets.token_hex(4)
    item["created"] = int(time.time())
    if icon_picture:
        set_custom_icon(item, icon_picture, base_data)
        item["icon"] = ICON_CUSTOM
    with _Locked(base_data):
        argv = launcher_argv()
        try:
            item = install(item, argv, base_data)
        except BaseException:
            # Declined in the portal's dialog, or a file that could not be
            # written: nothing of it stays behind.
            _remove_files(item)
            _drop_icons(item, base_data)
            raise
        items = load(base_data) + [item]
        _store(items, base_data,
               None if in_flatpak() else launcher_signature(argv))
    return item


def edit(ident, changes, icon_picture=None, base_data=None):
    """Change a shortcut and write its files again."""
    with _Locked(base_data):
        items = load(base_data)
        current = get(ident, base_data)
        merged = dict(current)
        merged.update({k: v for k, v in changes.items() if v is not None})
        item = _check(merged, base_data)
        item["id"] = current["id"]
        if icon_picture:
            set_custom_icon(item, icon_picture, base_data)
            item["icon"] = ICON_CUSTOM
        # The Flatpak's menu entry shows a name and icon the portal holds:
        # changing either goes through its dialog again. What it starts is
        # read from the list each time and needs no new entry.
        looks = ("name", "icon", "badge", "version", "edition")
        ask = icon_picture is not None or any(
            item.get(k) != current.get(k) for k in looks)
        argv = launcher_argv()
        item = install(item, argv, base_data, ask_portal=ask)
        _store([item if i["id"] == item["id"] else i for i in items],
               base_data, None if in_flatpak() else launcher_signature(argv))
    return item


def _drop_icons(item, base_data=None):
    for path in (icon_file(item, base_data), _icon_source(item, base_data)):
        path.unlink(missing_ok=True)


def remove(ident, base_data=None):
    """Delete a shortcut: its files, its icon and its entry in the list."""
    with _Locked(base_data):
        item = get(ident, base_data)
        _remove_files(item)
        _drop_icons(item, base_data)
        _store([i for i in load(base_data) if i["id"] != item["id"]],
               base_data)
    return item


def refresh(force=False, base_data=None):
    """Write the shortcuts' files again when the launcher has moved.

    Run at every start: cheap when nothing changed (one small file read).
    ``force`` writes every file again, and puts back the ones that were
    deleted by hand. Returns how many shortcuts were written.
    """
    if in_flatpak():
        return 0
    try:
        data = _read(base_data)
    except BolError:
        return 0
    if not data["shortcuts"]:
        return 0
    argv = launcher_argv()
    signature = launcher_signature(argv)
    if not force and data.get("launcher") == signature:
        return 0
    written = 0
    with _Locked(base_data):
        items = []
        for item in load(base_data):
            if not force:
                # Only what is still there: a file deleted from the menu by
                # hand stays deleted.
                item = dict(item)
                item["menu"] = item["menu"] and menu_file(item).exists()
                desktop = item.get("desktop_file")
                item["desktop"] = item["desktop"] and bool(desktop) and \
                    Path(desktop).exists()
                if not (item["menu"] or item["desktop"]):
                    items.append(item)
                    continue
            try:
                items.append(install(item, argv, base_data))
                written += 1
            except Exception as exc:  # noqa: BLE001 - one shortcut, not all
                warn(f"The shortcut '{item['name']}' could not be written "
                     f"again: {exc}")
                items.append(item)
        _store(items, base_data, signature)
    return written


def profile_renamed(old_slug, new_slug, base_data=None):
    """Follow a profile to its new name (bol.profiles.rename_profile)."""
    with _Locked(base_data):
        items = load(base_data)
        targets = [i for i in items if i["profile"] == old_slug]
        if not targets:
            return 0
        for item in targets:
            item["profile"] = new_slug
        _store(items, base_data)
    if not in_flatpak():
        argv = launcher_argv()
        for item in targets:
            try:
                install(item, argv, base_data)
            except Exception:  # noqa: BLE001 - the comment only
                pass
    return len(targets)


def profile_deleted(slug, base_data=None):
    """Remove the shortcuts of a profile that is gone."""
    removed = 0
    for item in [i for i in load(base_data) if i["profile"] == slug]:
        try:
            remove(item["id"], base_data)
            removed += 1
        except BolError:
            continue
    return removed


def steam_command(shortcut, argv=None):
    """The command to add a shortcut to Steam as a non-Steam game."""
    if in_flatpak():
        return (f"flatpak run --command={APP} {FLATPAK_APP_ID} shortcuts run "
                f"{shortcut['id']}")
    return shlex.join(list(argv or launcher_argv())
                      + ["shortcuts", "run", shortcut["id"]])


# ---------------------------------------------------------------- running

def enter_profile(shortcut, base_data=None, execve=os.execve):
    """Start this command again in the shortcut's profile when it is not
    the one this process runs in: a profile is a BOL_HOME, fixed when the
    launcher starts (see bol.__main__.relaunch_in_profile)."""
    from .profiles import _profile_environment, profile_home
    folder = profile_folder(shortcut, base_data)
    home = profile_home(folder, base_data)
    try:
        here = Path(DATA).resolve()
    except OSError:
        here = Path(DATA)
    if home == here:
        return
    if os.environ.get(_HOP_ENV) == shortcut["id"]:
        raise BolError(f"The shortcut '{shortcut['name']}' could not switch "
                       f"to the profile it plays in ({home}).")
    from .__main__ import _same_launcher
    env = _profile_environment(folder, base_data)
    env[_HOP_ENV] = shortcut["id"]
    tail = ["shortcuts", "run", shortcut["id"]]
    argv = [sys.argv[0]] + tail
    cmd = _same_launcher(argv, getattr(sys, "orig_argv", None),
                         sys.executable) + tail
    execve(cmd[0], cmd, env)


def _same_dir(one, two):
    try:
        return Path(one).resolve() == Path(two).resolve()
    except OSError:
        return False


def play(shortcut, launch=None, update=None):
    """Start a shortcut's game with no launcher window, and wait for it.

    Its build is selected for the session and the profile's own choice
    restored afterwards. Raises BolError when it cannot start.
    """
    from . import supervision
    from .auth import msa_signed_in
    from .games import is_bedrock, use_game_dir
    from .prefix import _mc_running, prefix_operation_lock
    from .util import load_settings
    entry, version, folder = resolve(shortcut)
    os.environ.pop(_HOP_ENV, None)
    os.environ.update(environment(shortcut))
    previous = (load_settings().get("game_dir") or "").strip()
    with prefix_operation_lock(f"start {shortcut['name']}"):
        if _mc_running():
            raise BolError(
                "Minecraft is already running in this profile. Close it "
                f"before starting '{shortcut['name']}'.")
        if not _same_dir(previous, folder):
            use_game_dir(folder)
    selected = (load_settings().get("game_dir") or "").strip()
    try:
        if shortcut["version"] == LATEST:
            from .gamesetup import update_game_before_play
            (update or update_game_before_play)(always=shortcut["update"])
            selected = (load_settings().get("game_dir") or "").strip()
        if entry.get("xbox_live", True) and not msa_signed_in():
            warn("Not signed in for online play: no Realms, no servers, no "
                 "friends and no Marketplace." if is_bedrock(entry) else
                 "Not signed in: the game cannot reach Xbox Live.")
        build = "" if single_build(entry["id"]) else f" {version}"
        info(f"Starting '{shortcut['name']}': {game_name(entry['id'])}"
             f"{build} …")
        supervision.install()
        if launch is None:
            from .launch import launch
        options = {"hidden": True} if shortcut["hidden"] else {}
        return launch(editor=shortcut["editor"], **options)
    finally:
        _restore_selection(previous, selected)


def _restore_selection(previous, selected):
    """Put the profile's own choice back, unless it was changed meanwhile
    or another session holds the profile now."""
    from .games import _game_root, edition_exe, edition_for_dir, \
        use_game_dir
    from .prefix import prefix_operation_lock
    from .util import load_settings
    if not previous or _same_dir(previous, selected):
        return
    try:
        if _game_root(previous, edition_exe(edition_for_dir(previous))) \
                is None:
            return
        with prefix_operation_lock("restore the selected version"):
            current = (load_settings().get("game_dir") or "").strip()
            if _same_dir(current, selected):
                use_game_dir(previous)
    except Exception:  # noqa: BLE001 - the game ran; this is a nicety
        pass


# ---------------------------------------------------------------- command line

def add_arguments(sub):
    """The ``shortcuts`` command and its subcommands (bol.cli)."""
    sp = sub.add_parser(
        "shortcuts",
        help="app menu and desktop shortcuts that each start one Minecraft: "
             "a game, a version, a profile and how it starts")
    cmds = sp.add_subparsers(dest="shortcuts_cmd")
    cmds.add_parser("list", help="list the shortcuts (default)")

    def target_options(parser, creating):
        parser.add_argument("--name", help="what the menu shows (default: "
                                           "the game and its version)")
        parser.add_argument("--mc", metavar="EDITION",
                            help="the game: release, preview, education, "
                                 "uwp, dungeons or legends"
                                 + (" (default: release)" if creating else ""))
        parser.add_argument("--version", metavar="BUILD",
                            help="an installed build, or 'latest' for the "
                                 "newest one installed"
                                 + (" (default)" if creating else ""))
        parser.add_argument("--profile", metavar="NAME",
                            help="the profile it plays in (default: Default)"
                            if creating else "the profile it plays in")
        parser.add_argument("--start", choices=STARTS,
                            help="launcher: the launcher opens and starts "
                                 "the game by itself; direct: the game "
                                 "starts with no launcher window"
                                 + (" (default: launcher)" if creating
                                    else ""))
        flag = argparse.BooleanOptionalAction
        parser.add_argument("--editor", action=flag, default=None,
                            help="open Bedrock Editor instead of the game")
        parser.add_argument("--hidden", action=flag, default=None,
                            help="keep the game's window off the screen")
        parser.add_argument("--update", action=flag, default=None,
                            help="with --version latest: install the newest "
                                 "build before starting")
        parser.add_argument("--env", metavar="'NAME=value ...'",
                            help="environment variables for the game")
        parser.add_argument("--icon", metavar="PICTURE",
                            help="a picture file, 'edition' for the game's "
                                 "own art, or 'launcher'")
        parser.add_argument("--badge", action=flag, default=None,
                            help="write the version on the icon "
                                 "(default: on)")
        parser.add_argument("--menu", action=flag, default=None,
                            help="put it in the app menu (default: on)")
        parser.add_argument("--desktop", action=flag, default=None,
                            help="put it on the desktop (default: off)")

    create_p = cmds.add_parser("create", help="make a shortcut")
    target_options(create_p, True)
    edit_p = cmds.add_parser("edit", help="change a shortcut")
    edit_p.add_argument("shortcut", metavar="SHORTCUT",
                        help="its id, or its name")
    target_options(edit_p, False)
    for name, text in (
            ("remove", "delete a shortcut from the menu and the desktop"),
            ("run", "start what a shortcut starts (what its menu entry "
                    "runs)"),
            ("command", "print the command that starts a shortcut, to add "
                        "it to Steam as a non-Steam game")):
        one = cmds.add_parser(name, help=text)
        one.add_argument("shortcut", metavar="SHORTCUT",
                         help="its id, or its name")
    cmds.add_parser("refresh", help="write every shortcut's menu and desktop "
                                    "file again, for this launcher")
    return sp


def _fields(a):
    fields = {"name": a.name, "edition": a.mc and a.mc.strip().lower(),
              "version": a.version, "start": a.start, "editor": a.editor,
              "hidden": a.hidden, "update": a.update, "env": a.env,
              "badge": a.badge, "menu": a.menu, "desktop": a.desktop}
    if a.profile is not None:
        fields["profile"] = a.profile
    picture = None
    if a.icon is not None:
        if a.icon in (ICON_EDITION, ICON_LAUNCHER):
            fields["icon"] = a.icon
        else:
            picture = a.icon
    return {k: v for k, v in fields.items() if v is not None}, picture


def _print(item):
    places = [p for p, on in (("app menu", item["menu"]),
                              ("desktop", item["desktop"])) if on]
    print(f"  {item['id']}  {item['name']}")
    print(f"      {describe(item)} · profile {profile_label(item)} · "
          f"{'opens the launcher' if item['start'] == START_LAUNCHER else 'starts the game directly'}"
          f" · in {' and '.join(places) or 'no menu'}")
    extras = [x for x, on in (("Bedrock Editor", item["editor"]),
                              ("hidden window", item["hidden"]),
                              ("installs the newest first", item["update"]))
              if on]
    if item["env"]:
        extras.append(item["env"])
    if extras:
        print("      " + " · ".join(extras))


def cli(a, parser):
    """Run ``shortcuts <command>``; ``parser`` prints the help."""
    from .log import ok
    cmd = a.shortcuts_cmd or "list"
    if cmd == "list":
        items = load()
        if not items:
            info("No shortcut yet. Make one with:  "
                 f"{APP} shortcuts create --mc release --version latest")
        for item in items:
            _print(item)
    elif cmd == "create":
        fields, picture = _fields(a)
        item = create(fields, icon_picture=picture)
        ok(f"Shortcut '{item['name']}' ({item['id']}) made.")
        _print(item)
        try:
            resolve(item)
        except BolError as exc:
            warn(str(exc))
    elif cmd == "edit":
        fields, picture = _fields(a)
        item = edit(a.shortcut, fields, icon_picture=picture)
        ok(f"Shortcut '{item['name']}' changed.")
        _print(item)
    elif cmd == "remove":
        item = remove(a.shortcut)
        ok(f"Shortcut '{item['name']}' removed.")
    elif cmd == "command":
        print(steam_command(get(a.shortcut)))
    elif cmd == "refresh":
        count = refresh(force=True)
        ok(f"{count} shortcut(s) written again for "
           f"{launcher_signature()}.")
    elif cmd == "run":
        run(a.shortcut)
    else:
        parser.print_help()


def run(ident):
    """What a shortcut's menu entry runs."""
    item = get(ident)
    enter_profile(item)
    if item["start"] == START_LAUNCHER:
        os.environ.pop(_HOP_ENV, None)
        os.environ[START_ENV] = item["id"]
        from .cli import _open_gui
        _open_gui()
        return
    play(item)
