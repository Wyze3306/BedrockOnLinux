"""bol.deps — runtime bootstrap of the non-stdlib Python deps login needs."""
# SPDX-License-Identifier: MIT
import hashlib
import importlib
import importlib.util
import os
import re
import shutil
import site
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from .log import info, ok, warn

# Non-stdlib modules the native Microsoft login needs. `cryptography` signs the
# Xbox Live device/request tokens (ES256); without it xbl_preauth bails and the
# in-game login fails with a connection-reset (0x80072746). `requests` was
# dropped in favour of urllib, so cryptography is the only hard dependency.
#   import-name -> pip distribution name
# 43.0.3 is the newest upstream wheel line built on manylinux_2_28.  Starting
# with 44, x86-64 wheels require glibc 2.34, so installing the floating latest
# breaks the portable .pyz on Debian 11/12 and Ubuntu 22.04. Distribution-
# packaged cryptography remains accepted at any version; this pin applies only
# to the best-effort pip bootstrap when the module is absent.
LOGIN_DEPS = {"cryptography": "cryptography==43.0.3"}


def have(mod):
    """True if `mod` can be imported in this interpreter."""
    if mod in _PRIVATE_MODULES:
        activate_private_toolkit()
    try:
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False


# "libzstd.so.1: cannot open shared object file: No such file or directory" --
# the dynamic loader's own words, which Python hands on as a plain ImportError
# when an extension module's shared libraries are not all there (issue #205).
_MISSING_LIBRARY = re.compile(
    r"(lib[^\s:/]+\.so[^\s:/]*): cannot open shared object file")


def missing_shared_library(error):
    """The system library an ImportError blames, or None if it blames none."""
    found = _MISSING_LIBRARY.search(str(error))
    return found.group(1) if found else None


def gui_import_error():
    """Why importing the Qt toolkit fails, or None when it imports.

    `have("PySide6")` only proves the package is on the path: Qt loads its own
    shared libraries when the module is imported, so a host missing one of
    them looks perfectly installed and only fails once the launcher is already
    on its way to a window.
    """
    try:
        importlib.import_module("PySide6.QtCore")
    except ImportError as exc:
        return str(exc)
    return None


# The system package each library a PyPI PySide6 links but a desktop may
# lack comes in, per package manager. Qt's xcb platform plugin links the
# xcb-util family, which stock Ubuntu 22.04/24.04 desktops, and Mint with
# them, do not all install (no libxcb-cursor0); libQt6Core links
# libgthread-2.0, which openSUSE splits into a package of its own that no
# desktop pattern pulls in.
QT_LIBRARY_PACKAGES = {
    #  soname:                  (apt,                 dnf / pacman,
    #                            zypper)
    "libxcb-cursor.so.0":      ("libxcb-cursor0",      "xcb-util-cursor",
                                "libxcb-cursor0"),
    "libxcb-icccm.so.4":       ("libxcb-icccm4",       "xcb-util-wm",
                                "libxcb-icccm4"),
    "libxcb-image.so.0":       ("libxcb-image0",       "xcb-util-image",
                                "libxcb-image0"),
    "libxcb-keysyms.so.1":     ("libxcb-keysyms1",     "xcb-util-keysyms",
                                "libxcb-keysyms1"),
    "libxcb-render-util.so.0": ("libxcb-render-util0", "xcb-util-renderutil",
                                "libxcb-render-util0"),
    "libxcb-util.so.1":        ("libxcb-util1",        "xcb-util",
                                "libxcb-util1"),
    "libxcb-shape.so.0":       ("libxcb-shape0",       "libxcb",
                                "libxcb-shape0"),
    "libgthread-2.0.so.0":     ("libglib2.0-0",        "glib2",
                                "libgthread-2_0-0"),
}


def install_hint(*libraries):
    """"sudo <manager> install <packages>" for ``libraries``, or None when
    one of them is not in QT_LIBRARY_PACKAGES."""
    packages = [QT_LIBRARY_PACKAGES.get(library) for library in libraries]
    if not packages or None in packages:
        return None

    def names(column):
        return " ".join(dict.fromkeys(entry[column] for entry in packages))

    for manager, command, column in (
            ("apt-get", "sudo apt install", 0),
            ("dnf", "sudo dnf install", 1),
            ("pacman", "sudo pacman -S", 1),
            ("zypper", "sudo zypper install", 2)):
        if shutil.which(manager):
            return f"{command} {names(column)}"
    return f"{names(0)} on Debian, Ubuntu and Mint, {names(1)} on Fedora and Arch"


def _qt_lib_dir():
    try:
        spec = importlib.util.find_spec("PySide6")
    except Exception:
        return None
    located = list(getattr(spec, "submodule_search_locations", None) or [])
    qt_lib = Path(located[0]) / "Qt" / "lib" if located else None
    return qt_lib if qt_lib is not None and qt_lib.is_dir() else None


def missing_qt_libraries(first):
    """``first``, then every other library of QT_LIBRARY_PACKAGES that
    neither this host nor the toolkit's own Qt/lib provides.

    The loader stops at the first library it cannot find, so naming only that
    one made a stock openSUSE three rounds of "install this, open it again"
    (libgthread-2.0, then libxcb-cursor, then libxcb-shape).
    """
    if first not in QT_LIBRARY_PACKAGES:
        return [first]
    qt_lib = _qt_lib_dir()
    return [first] + [
        library for library in QT_LIBRARY_PACKAGES
        if library != first
        and not (qt_lib is not None and (qt_lib / library).exists())
        and not _host_loads(library)]


def qt_will_use_xcb(environ=None):
    """Whether Qt will open the launcher window through its xcb plugin.

    Qt 6 tries "wayland;xcb" in a Wayland session and "xcb" alone anywhere
    else, unless QT_QPA_PLATFORM names one.
    """
    env = os.environ if environ is None else environ
    chosen = (env.get("QT_QPA_PLATFORM") or "").strip().lower()
    if chosen:
        return chosen.split(";")[0].split(":")[0] == "xcb"
    return (env.get("XDG_SESSION_TYPE") or "").strip().lower() != "wayland"


def xcb_plugin_missing_library():
    """The system library Qt's xcb platform plugin cannot load, or None.

    Qt does not report that as an error Python could catch: it prints "could
    not load the Qt platform plugin" and aborts the process. dlopen() of the
    plugin fails the same way, but returns. Only a PySide6 wheel's own plugin
    is checked; a distribution's PySide6 declares its libraries itself.
    """
    try:
        spec = importlib.util.find_spec("PySide6")
    except Exception:
        return None
    locations = list(getattr(spec, "submodule_search_locations", None) or [])
    if not locations:
        return None
    plugin = Path(locations[0]) / "Qt" / "plugins" / "platforms" / "libqxcb.so"
    if not plugin.is_file():
        return None
    import ctypes
    try:
        ctypes.CDLL(str(plugin))
    except OSError as exc:
        return missing_shared_library(exc)
    return None


def missing_login_deps():
    """Import-names of the login deps that are not currently importable."""
    return [m for m in LOGIN_DEPS if not have(m)]


def _refresh_path():
    """Make a fresh `pip install --user` visible to the running interpreter."""
    try:
        usersite = site.getusersitepackages()
        if usersite and usersite not in sys.path:
            site.addsitedir(usersite)
    except Exception:
        pass
    importlib.invalidate_caches()


def _pip_install(pkgs):
    """Best-effort `pip install` of `pkgs`. Tries a normal --user install, then
    retries with --break-system-packages for PEP 668 'externally managed'
    distros (Debian/Ubuntu 24.04, Fedora, …). Returns True on success."""
    if not have("pip") and importlib.util.find_spec("pip") is None:
        return False
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    base = [sys.executable, "-m", "pip", "install", "--upgrade", "--quiet"]
    if not in_venv:                       # --user is invalid inside a venv
        base.append("--user")
    for extra in ([], ["--break-system-packages"]):
        try:
            subprocess.run(base + extra + list(pkgs),
                           check=True, stdout=subprocess.DEVNULL)
            return True
        except Exception:
            continue
    return False


def ensure_login_deps(install=True):
    """Ensure the login deps are importable. When `install` is set and some are
    missing, try to pip-install them. Returns the list of import-names still
    missing afterwards (empty == all good). Never raises."""
    missing = missing_login_deps()
    if not missing or not install:
        return missing
    if os.environ.get("BOL_NO_PIP") == "1":
        return missing
    info(f"Installing Python dependencies for login: {', '.join(missing)} …")
    if _pip_install(LOGIN_DEPS[m] for m in missing):
        _refresh_path()
        missing = missing_login_deps()
    if missing:
        warn("Could not auto-install Python deps "
             f"({', '.join(missing)}). Install them with your package manager "
             f"(e.g. 'sudo apt install python3-cryptography') or "
             f"'pip install --user {' '.join(LOGIN_DEPS[m] for m in missing)}'.")
    else:
        ok("Python login dependencies ready.")
    return missing


# The packaged builds bundle these; a portable .pyz or bare checkout fetches
# them on first GUI launch. PySide6-Essentials (not the PySide6 meta-package)
# is deliberately pinned: the meta-package pulls in PySide6-Addons too (167 MB
# of Qt modules — Multimedia, WebEngine, Charts, …) that this app never uses,
# and Essentials alone already provides the QtCore/QtGui/QtWidgets namespace
# under `PySide6.*`. 6.9.3 is also the newest PySide6 line still built on the
# manylinux_2_28 (glibc 2.28) baseline; 6.10+ wheels require glibc 2.34 and
# would break the same Debian 11/Ubuntu 22.04/Steam Runtime targets the
# cryptography==43.0.3 pin above exists to protect. But 6.9.3 declares
# Python < 3.14, and the distributions on 3.14 or 3.15 (Fedora 45, Arch) are
# exactly the ones with a glibc far past 2.34, so they get 6.12.0, the first
# line that accepts 3.15.
_PYPI = "https://files.pythonhosted.org/packages/"
#   PySide6 line -> (minimum glibc, ((wheel url, sha256), ...))
PYSIDE6_WHEELS = {
    "6.9.3": ((2, 28), (
        (_PYPI + "be/82/c1c6932f9849bc5e75c93c38a29419505a6e3e0037261e28f3e7"
         "ecbf2751/shiboken6-6.9.3-cp39-abi3-manylinux_2_28_x86_64.whl",
         "f3f5337a3a8fc660ba1462265bd9a2bdda9588f8d90fbc3d5ac4ce3134c11e59"),
        (_PYPI + "85/e8/9396cf11a60f80175bb3c5c1d498d84e87b7af653ab4ea001acf"
         "821a3981/pyside6_essentials-6.9.3-cp39-abi3-manylinux_2_28_x86_64"
         ".whl",
         "c70d5544e892b201a677b615156fab6a0fef865e7fc287f55a0eae00a682e83f"),
    )),
    "6.12.0": ((2, 34), (
        (_PYPI + "49/c2/a04fa003b4360e99d8e3fc2829abacf37396ace63ea1cd616402"
         "209ec7c6/shiboken6-6.12.0-cp310-abi3-manylinux_2_34_x86_64.whl",
         "3bde565bb0890044b5c7f63808adddede5abc241e9395a2bfed61919884d6240"),
        (_PYPI + "7b/3c/7597d8dcb36aa8315427635ec6ec9d850ffbad4ce78b9118ac85"
         "4889f2d3/pyside6_essentials-6.12.0-cp310-abi3-manylinux_2_34_x86_64"
         ".whl",
         "16f4b5e41daf49235ae048458243da9efe5f3d8040f9cc5aa454ce69893d3bd9"),
    )),
}


def pyside6_line(version_info=None):
    """The PySide6 line the bootstrap installs for this Python."""
    version_info = sys.version_info if version_info is None else version_info
    return "6.9.3" if tuple(version_info[:2]) < (3, 14) else "6.12.0"


_PYSIDE6 = pyside6_line()
GUI_DEPS = {
    "PySide6": f"PySide6-Essentials=={_PYSIDE6}",
    "Xlib": "python-xlib==0.33",
}
GUI_INSTALL_REQUIREMENTS = tuple(GUI_DEPS.values())

# The same toolkit as hash-pinned wheels, fetched straight from PyPI and
# unpacked into the launcher's own data directory. No pip: Debian, Ubuntu,
# Mint and Arch install none by default, so the .pyz could not open its
# window there at all, and where pip is installed a PEP 668 system only took
# the toolkit with --break-system-packages, into the user's own
# site-packages, where it shadows whatever else uses PySide6.
#   import-name -> (directory tag, minimum glibc, ((wheel url, sha256), ...))
PRIVATE_WHEELS = {
    "PySide6": (f"pyside6-{_PYSIDE6}",) + PYSIDE6_WHEELS[_PYSIDE6],
    "Xlib": ("python-xlib-0.33", None, (
        (_PYPI + "fc/b8/ff33610932e0ee81ae7f1269c890f697d56ff74b9f5b2ee5d9b7"
         "fa2c5355/python_xlib-0.33-py2.py3-none-any.whl",
         "c3534038d42e0df2f1392a1b30a15a4ff5fdc2b86cfa94f072bf11b10a164398"),
        (_PYPI + "b7/ce/149a00dd41f10bc29e5921b496af8b574d8413afcd5e30dfa0ed"
         "46c2cc5e/six-1.17.0-py2.py3-none-any.whl",
         "4721f391ed90541fddacab5acf947aa0d3dc7d27b2e1e8eda2be8970586c3274"),
    )),
}
_PRIVATE_MODULES = ("PySide6", "shiboken6", "Xlib", "six")
_COMPLETE = ".bol-complete"


def private_toolkit_dir():
    from .config import DATA
    return DATA / "python-gui"


def activate_private_toolkit(base=None):
    """Put every complete private wheel set for this Python on sys.path.

    Appended, not prepended: a toolkit the system or pip already provides
    stays the one that is imported.
    """
    base = Path(base) if base is not None else private_toolkit_dir()
    added = False
    for tag, _glibc, _wheels in PRIVATE_WHEELS.values():
        directory = base / tag
        if str(directory) in sys.path:
            continue
        if (directory / _COMPLETE).is_file():
            sys.path.append(str(directory))
            added = True
    if added:
        importlib.invalidate_caches()
    return added


def _host_glibc():
    try:
        text = os.confstr("CS_GNU_LIBC_VERSION") or ""
    except (ValueError, OSError):
        return None
    match = re.search(r"(\d+)\.(\d+)", text)
    return (int(match.group(1)), int(match.group(2))) if match else None


def _unpack_wheel(wheel, target):
    """Unpack a wheel's importable files into ``target``, refusing any path
    that would land outside it."""
    root = Path(target).resolve()
    with zipfile.ZipFile(wheel) as archive:
        for member in archive.infolist():
            name = member.filename
            parts = Path(name).parts
            if not parts or name.startswith("/") or ".." in parts:
                raise ValueError(f"unsafe path {name!r} in {Path(wheel).name}")
            # Scripts and headers live under <dist>.data/; nothing here
            # imports them.
            if parts[0].endswith(".data"):
                continue
            destination = (root / name).resolve()
            if root not in destination.parents and destination != root:
                raise ValueError(f"unsafe path {name!r} in {Path(wheel).name}")
            if member.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, \
                    open(destination, "wb") as output:
                shutil.copyfileobj(source, output)
            mode = (member.external_attr >> 16) & 0o777
            os.chmod(destination, (mode | stat.S_IRUSR | stat.S_IWUSR)
                     if mode else 0o644)


def _fetch_wheel_set(module, base=None):
    """Download, verify and unpack the wheels for ``module``.

    Returns None on success, otherwise why it could not be done.
    """
    from .config import CACHE
    from .util import download

    tag, glibc, wheels = PRIVATE_WHEELS[module]
    if glibc is not None:
        host = _host_glibc()
        if host is not None and host < glibc:
            return (f"its wheels need glibc {glibc[0]}.{glibc[1]} and this "
                    f"system has {host[0]}.{host[1]}")
    base = Path(base) if base is not None else private_toolkit_dir()
    base.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{tag}-", dir=base))
    try:
        for url, expected in wheels:
            name = url.rsplit("/", 1)[-1]
            wheel = CACHE / name
            try:
                download(url, wheel, name)
                with open(wheel, "rb") as handle:
                    actual = hashlib.file_digest(handle, "sha256").hexdigest() \
                        if hasattr(hashlib, "file_digest") else \
                        hashlib.sha256(handle.read()).hexdigest()
                if actual != expected:
                    return f"{name} does not match its pinned SHA-256"
                _unpack_wheel(wheel, staging)
            finally:
                try:
                    wheel.unlink()
                except OSError:
                    pass
        (staging / _COMPLETE).write_text(
            "\n".join(url.rsplit("/", 1)[-1] for url, _ in wheels) + "\n",
            encoding="utf-8")
        final = base / tag
        if final.exists():
            shutil.rmtree(final, ignore_errors=True)
        os.replace(staging, final)
        staging = None
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
    # Sets another Python line (or an older pin) left behind.
    wanted = {entry[0] for entry in PRIVATE_WHEELS.values()}
    for entry in base.iterdir():
        if entry.is_dir() and not entry.name.startswith(".") and \
                entry.name not in wanted:
            shutil.rmtree(entry, ignore_errors=True)
    return None


# Libraries the PyPI PySide6 links that a desktop does not reliably have --
# see QT_LIBRARY_PACKAGES -- plus libzstd, which a minimal system can lack
# (issue #205). The AppImage carries exactly these, from the same pinned
# Debian 11 packages (scripts/build-appimage.sh); the private wheel set gets
# the ones this host cannot load, in PySide6/Qt/lib, where the $ORIGIN RUNPATH
# of libQt6Core and of the xcb plugin finds them. Without them the .pyz cannot
# import Qt on a stock openSUSE (libgthread-2.0) and aborts on every X11
# session of a stock Ubuntu or Mint (libxcb-cursor).
_DEBIAN_SNAPSHOT = \
    "https://snapshot.debian.org/archive/debian/20260701T000000Z/"
#   soname -> (pool file, file inside the package, SHA-256 of the package as
#              Debian's signed Packages index gives it)
QT_GAP_LIBRARIES = {
    "libzstd.so.1": (
        "pool/main/libz/libzstd/libzstd1_1.4.8+dfsg-2.1_amd64.deb",
        "libzstd.so.1.4.8",
        "5dcadfbb743bfa1c1c773bff91c018f835e8e8c821d423d3836f3ab84773507b"),
    "libxcb-util.so.1": (
        "pool/main/x/xcb-util/libxcb-util1_0.4.0-1+b1_amd64.deb",
        "libxcb-util.so.1.0.0",
        "4c48af51fb2ac1be0490067e7450aeda27bf6c6c395165de02199eee4835336f"),
    "libxcb-cursor.so.0": (
        "pool/main/x/xcb-util-cursor/libxcb-cursor0_0.1.1-4_amd64.deb",
        "libxcb-cursor.so.0.0.0",
        "bab731cd0143303f77461dd0a03ad20807bd3d767d5d6af6a11ff99c3b7aeac3"),
    "libxcb-image.so.0": (
        "pool/main/x/xcb-util-image/libxcb-image0_0.4.0-1+b3_amd64.deb",
        "libxcb-image.so.0.0.0",
        "36a381bb18c9f349a53457c66b3d1825631f3e50c4f6c12326cf05257302172d"),
    "libxcb-keysyms.so.1": (
        "pool/main/x/xcb-util-keysyms/libxcb-keysyms1_0.4.0-1+b2_amd64.deb",
        "libxcb-keysyms.so.1.0.0",
        "aed1436db9a3e63b10d00c4ed16248b5c82b5dd2963a83a761f406af65eb4b49"),
    "libxcb-render-util.so.0": (
        "pool/main/x/xcb-util-renderutil/"
        "libxcb-render-util0_0.3.9-1+b1_amd64.deb",
        "libxcb-render-util.so.0.0.0",
        "be4b38a63e65c84e2f1322f044d05a9baa677e0f3dc68b742a0a109a3ff40ae9"),
    "libxcb-icccm.so.4": (
        "pool/main/x/xcb-util-wm/libxcb-icccm4_0.4.1-1.1_amd64.deb",
        "libxcb-icccm.so.4.0.0",
        "f323194cb04cd4e5ae064fafec39db6dcf8a431cbd65a0bc53fa6c359862d8ff"),
    "libxcb-shape.so.0": (
        "pool/main/libx/libxcb/libxcb-shape0_1.14-3_amd64.deb",
        "libxcb-shape.so.0.0.0",
        "d0f942240fc5c1b11514c726a1cba1bad80506a15c837ce2a9f11c9b313c4851"),
    "libgthread-2.0.so.0": (
        "pool/main/g/glib2.0/libglib2.0-0_2.66.8-1+deb11u4_amd64.deb",
        "libgthread-2.0.so.0.6600.8",
        "7aff8c1338b58a6fa2b3a914724040a2b7f6e8dc2333f30c98834f7948f1884f"),
}


def _deb_member(deb, member):
    """The bytes of ``member`` (a path such as "./usr/lib/...") inside the
    data archive of the Debian package ``deb``."""
    import io
    import lzma
    import tarfile

    raw = Path(deb).read_bytes()
    if raw[:8] != b"!<arch>\n":
        raise ValueError(f"{Path(deb).name} is not a Debian package")
    # An ar archive: 60-byte member headers, data padded to an even length.
    members, offset = {}, 8
    while offset + 60 <= len(raw):
        header = raw[offset:offset + 60]
        name = header[:16].decode("ascii", "replace").strip().rstrip("/")
        start = offset + 60
        size = int(header[48:58])
        members[name] = raw[start:start + size]
        offset = start + size + size % 2
    if "data.tar.xz" not in members:
        raise ValueError(f"{Path(deb).name} carries no data.tar.xz")
    payload = io.BytesIO(lzma.decompress(members["data.tar.xz"]))
    with tarfile.open(fileobj=payload, mode="r:") as archive:
        try:
            entry = archive.getmember(member)
        except KeyError:
            raise ValueError(f"{Path(deb).name} has no {member}") from None
        source = archive.extractfile(entry)
        if source is None:
            raise ValueError(f"{member} is not a file in {Path(deb).name}")
        return source.read()


def _host_loads(soname):
    import ctypes
    try:
        ctypes.CDLL(soname)
    except OSError:
        return False
    return True


def _fetch_gap_library(soname, qt_lib):
    """Put ``soname`` from its pinned Debian package into ``qt_lib``.
    Returns None on success, otherwise why it could not."""
    from .config import CACHE
    from .util import download

    pool, member, expected = QT_GAP_LIBRARIES[soname]
    name = pool.rsplit("/", 1)[-1]
    deb = CACHE / name
    part = Path(qt_lib) / f".{soname}.part"
    try:
        # Two attempts, not five: this runs again at the next launch.
        download(_DEBIAN_SNAPSHOT + pool.replace("+", "%2B"), deb, name,
                 attempts=2)
        if hashlib.sha256(deb.read_bytes()).hexdigest() != expected:
            return f"{name} does not match its pinned SHA-256"
        part.write_bytes(_deb_member(deb, "./usr/lib/x86_64-linux-gnu/"
                                     + member))
        os.chmod(part, 0o755)
        os.replace(part, Path(qt_lib) / soname)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    finally:
        for leftover in (deb, part):
            try:
                leftover.unlink()
            except OSError:
                pass
    return None


def fill_qt_library_gaps(base=None, loadable=None):
    """Give the private PySide6 the QT_GAP_LIBRARIES this host cannot load.

    Returns the sonames still missing afterwards. A PySide6 the system or pip
    installed is left alone: completing it is the user's call, and the
    launcher names the package to install instead. Never raises.
    """
    import platform

    if platform.machine() != "x86_64":
        return []
    base = Path(base) if base is not None else private_toolkit_dir()
    tag = PRIVATE_WHEELS["PySide6"][0]
    package = base / tag / "PySide6"
    qt_lib = package / "Qt" / "lib"
    if not (base / tag / _COMPLETE).is_file() or not qt_lib.is_dir():
        return []
    try:
        spec = importlib.util.find_spec("PySide6")
        located = list(getattr(spec, "submodule_search_locations", None)
                       or [])
        if not located or Path(located[0]).resolve() != package.resolve():
            return []
    except Exception:
        return []
    loadable = _host_loads if loadable is None else loadable
    gaps = [soname for soname in QT_GAP_LIBRARIES
            if not (qt_lib / soname).exists() and not loadable(soname)]
    if gaps:
        info("Downloading the system libraries Qt needs and this system "
             f"lacks (one-time): {', '.join(gaps)} …")
    still = []
    for soname in gaps:
        reason = _fetch_gap_library(soname, qt_lib)
        if reason:
            warn(f"Could not download {soname} ({reason}).")
            still.append(soname)
    return still


def missing_gui_deps():
    activate_private_toolkit()
    return [m for m in GUI_DEPS if not have(m)]


def ensure_gui_deps(install=True):
    """Ensure the GUI toolkit is importable; fetch it when missing and
    allowed. Returns import-names still missing (empty == ready). Never raises."""
    missing = missing_gui_deps()
    if not missing or not install or os.environ.get("BOL_NO_PIP") == "1":
        return missing
    info(f"Downloading the GUI toolkit (one-time): {', '.join(missing)} …")
    reasons = []
    for module in missing:
        reason = _fetch_wheel_set(module)
        if reason:
            reasons.append(f"{module}: {reason}")
    activate_private_toolkit()
    missing = missing_gui_deps()
    if missing:
        detail = "; ".join(reasons) or "the download did not complete"
        warn(f"Could not install the GUI toolkit ({detail}). "
             "The AppImage, Flatpak, .deb and .rpm already carry it.")
    return missing
