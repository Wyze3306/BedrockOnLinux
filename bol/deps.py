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
