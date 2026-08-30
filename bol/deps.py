"""bol.deps — runtime bootstrap of the non-stdlib Python deps login needs."""
# SPDX-License-Identifier: MIT
import importlib
import importlib.util
import os
import re
import site
import subprocess
import sys

from .log import info, ok, warn
from .platform import IS_MAC

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


# The packaged builds bundle these; a portable .pyz or bare checkout installs
# them on first GUI launch. PySide6-Essentials (not the PySide6 meta-package)
# is deliberately pinned: the meta-package pulls in PySide6-Addons too (167 MB
# of Qt modules — Multimedia, WebEngine, Charts, …) that this app never uses,
# and Essentials alone already provides the QtCore/QtGui/QtWidgets namespace
# under `PySide6.*`. 6.9.3 is also the newest PySide6 line still built on the
# manylinux_2_28 (glibc 2.28) baseline; 6.10+ wheels require glibc 2.34 and
# would break the same Debian 11/Ubuntu 22.04/Steam Runtime targets the
# cryptography==43.0.3 pin above exists to protect.
GUI_DEPS = {
    "PySide6": "PySide6-Essentials==6.9.3",
    "packaging": "packaging==26.2",
    "Xlib": "python-xlib==0.33",
}
if IS_MAC:
    # python-xlib talks to an X server, which macOS does not have and never
    # starts one for. It installs there perfectly well and then does nothing,
    # so leaving it in would make a first GUI launch download a package whose
    # only purpose on this platform is to fail an import test.
    GUI_DEPS.pop("Xlib")
GUI_INSTALL_REQUIREMENTS = tuple(GUI_DEPS.values())


def missing_gui_deps():
    return [m for m in GUI_DEPS if not have(m)]


def ensure_gui_deps(install=True):
    """Ensure the GUI toolkit is importable; pip-install it when missing and
    allowed. Returns import-names still missing (empty == ready). Never raises."""
    missing = missing_gui_deps()
    if not missing or not install or os.environ.get("BOL_NO_PIP") == "1":
        return missing
    info(f"Installing the GUI toolkit: {', '.join(missing)} …")
    if _pip_install(GUI_INSTALL_REQUIREMENTS):
        _refresh_path()
        missing = missing_gui_deps()
    if missing:
        warn("Could not install the GUI toolkit "
             f"('pip install --user {' '.join(GUI_INSTALL_REQUIREMENTS)}'). "
             "The AppImage and Flatpak already bundle this Python toolkit.")
    return missing
