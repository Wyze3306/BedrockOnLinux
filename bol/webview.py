"""bol.webview — WebKitGTK for xodus-cli on hosts that ship none.

xodus-cli links wry/tao unconditionally, so ``libwebkit2gtk-4.1.so.0`` has to
be loadable before its ``main()`` runs. That makes WebKitGTK a hard dependency
of *everything* the launcher asks Xodus for: the Microsoft sign-in window, the
game download, and ``xodus-cli run``, which starts every Store-installed game
because its executable stays encrypted on disk.

Distributions package that library, so the ordinary case is to use the host's.
Immutable images are the problem: SteamOS ships no WebKitGTK, and installing
one means disabling the read-only rootfs and losing it again at the next OS
update (issue #184). For those hosts the launcher fetches the closure of that
stack — built by .github/workflows/build-xodus.yml from the same pinned Debian
snapshot as xodus-cli itself — and runs the binary against it.

Two details make the bundle work anywhere:

* WebKitGTK spawns ``WebKitWebProcess`` and ``WebKitNetworkProcess`` from a
  directory that is compiled into the library. ``WEBKIT_EXEC_PATH`` only
  overrides it in developer builds, so the launcher rewrites that literal in
  its own copy to a short path under XDG_RUNTIME_DIR and links the helpers
  there. The replacement has to fit in the original literal, which is why it
  cannot simply point back into the bundle.
* Everything else WebKit looks up by path — the injected bundle, the GIO TLS
  backend, the pixbuf loaders, the GSettings schemas — is redirected with
  environment variables that are set for xodus-cli alone.

Whichever library ends up being used, it is asked for the two settings that
keep that window alive on someone else's desktop: no DMABUF renderer, which is
how it dies on a good many Wayland compositors (issue #186), and no
accessibility bus, whose text interface aborts the web process outright
(issue #236).
"""
# SPDX-License-Identifier: MIT

import ctypes
import ctypes.util
import hashlib
import mmap
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from .archive import safe_extract_tar
from .config import (
    CACHE,
    WEBKIT_DRAWS_WITHOUT_EGL,
    WINEGDK_PREBUILT_REPO,
    XODUS_WEBVIEW_DIR,
    XODUS_WEBVIEW_EXEC_DIR,
    XODUS_WEBVIEW_REV,
    XODUS_WEBVIEW_SHA256,
)
from .log import BolError, info, ok
from .util import asset_url, download, env_flag, gh_releases

# The GUI and the game launch both ask several times per run whether the host
# can load the binary; the answer cannot change while the launcher lives.
_HOST_PROBE = {}

ASSET = f"xodus-webview-{XODUS_WEBVIEW_REV}.tar.xz"
# Written by scripts/build-xodus-webview.sh; the loader cache stores absolute
# module paths, which only exist once the bundle is unpacked here.
_PLACEHOLDER = "@BOL_WEBVIEW@"
_HELPERS = ("WebKitWebProcess", "WebKitNetworkProcess", "WebKitGPUProcess")
_WEBKIT_LIB = "lib/libwebkit2gtk-4.1.so.0"
# Set for xodus-cli only. Kept here so the generated launch wrapper can put the
# game's environment back exactly as it found it.
_VARS = ("LD_LIBRARY_PATH", "WEBKIT_INJECTED_BUNDLE_PATH", "GIO_EXTRA_MODULES",
         "GDK_PIXBUF_MODULE_FILE", "GSETTINGS_SCHEMA_DIR", "GTK_PATH",
         "GTK_MODULES", "GTK_IM_MODULE")

_PACKAGES = (("apt-get", "libwebkit2gtk-4.1-0"), ("dnf", "webkit2gtk4.1"),
             ("pacman", "webkit2gtk-4.1"), ("zypper", "libwebkit2gtk-4_1-0"))

# ld.so, when the C library is older than a binary was linked against:
# "…/libc.so.6: version `GLIBC_2.39' not found (required by …/xodus-cli)".
# xodus-cli is built on the same Debian snapshot as the bundled runtime, so no
# WebKitGTK -- the host's or the bundle's -- can get it past that (#264).
_GLIBC_TOO_OLD = re.compile(
    r"(?<!weak )version [`']GLIBC_(\d+(?:\.\d+)+)' not found")

# WebKitGTK composites into a DMABUF buffer and hands that to the display
# server. Where the handoff is refused the connection is torn down instead of
# degraded: the sign-in window disappears the moment it is created and the
# launcher only gets "Gdk-Message: Error 71 (Protocol error) dispatching to
# Wayland display" to show for it -- reported on KDE Plasma and GNOME alike,
# and it takes the Minecraft download down with it since nobody can sign in
# (issue #186). Turning it off falls back to shared-memory rendering, which for
# one login page costs nothing anybody can measure, so it is not worth making
# conditional on a compositor or a driver we would have to guess at.
_RENDERER = "WEBKIT_DISABLE_DMABUF_RENDERER"

# WebKitGTK also publishes the page on the accessibility bus, and its AT-SPI
# text interface is not safe to call. GetAttributes and GetAttributeRun map the
# attribute run they found back onto UTF-8 offsets by indexing an offset table
# with the end of that run -- which the code above them lets reach past the end
# of the object's own text. Indexing a WTF::Vector out of range aborts on the
# spot, so it is the *web process* that dies: the sign-in page goes blank in
# the middle of the login and the launcher is left with "sign-in did not
# complete", the reason for it only in a coredump (issue #236; still WebKitGTK
# 2.52's AccessibilityObjectTextAtspi.cpp). One accessibility client walking
# the window is enough to reach that, and a desktop running one is not
# something the user chose or can see.
#
# Set and empty, this puts WebKit's whole bridge out of reach: the page is
# never registered on the a11y bus, so nothing can call into that code. The
# window renders and behaves as before; it is simply not published to
# assistive technology. That is a real loss for anyone who needs a screen
# reader here, so it is one variable to take back -- and an address set by the
# session already wins, the same way the renderer's does.
_A11Y = "WEBKIT_A11Y_BUS_ADDRESS"
_A11Y_OPT_IN = "BOL_WEBVIEW_A11Y"

# WebKitGTK speaks TLS through GIO, and GIO through a module it finds at run
# time -- glib-networking's libgiognutls.so, in GIO's module directory or on
# GIO_EXTRA_MODULES. A host whose WebKitGTK loads but whose GIO finds no such
# module shows "TLS support is not available" where the Microsoft sign-in
# should be: a Nix or Guix WebKitGTK reached outside the wrapper that sets
# GIO_EXTRA_MODULES for it (#249). Loading the library proves nothing about
# that, so the probe asks the GIO that WebKitGTK itself loads, in the
# environment xodus-cli gets.
_TLS_PROBE = (
    "import ctypes, sys\n"
    "ctypes.CDLL('libwebkit2gtk-4.1.so.0', mode=ctypes.RTLD_GLOBAL)\n"
    "gio = ctypes.CDLL('libgio-2.0.so.0')\n"
    "gio.g_tls_backend_get_default.restype = ctypes.c_void_p\n"
    "gio.g_tls_backend_supports_tls.argtypes = [ctypes.c_void_p]\n"
    "backend = gio.g_tls_backend_get_default()\n"
    "sys.exit(0 if backend and gio.g_tls_backend_supports_tls(backend)"
    " else 3)\n"
)
_TLS_MISSING = 3
_TLS_PROBE_KEYS = ("LD_LIBRARY_PATH", "GIO_MODULE_DIR", "GIO_EXTRA_MODULES",
                   "GIO_USE_TLS")
_TLS_CACHE = {}

# WebKitGTK up to 2.52 draws every page through EGL, with or without a GPU:
# the web process opens a surfaceless EGL display when the EGL library offers
# that platform, the default display otherwise, and aborts when the one it
# picked cannot be initialized ("Could not create default EGL display:
# EGL_BAD_PARAMETER. Aborting..."). The window it was drawing for stays open
# and stays blank, with nothing on screen to say why (issue #273). That
# happens where libEGL.so.1 has nothing behind it: libglvnd without an EGL
# vendor, a Mesa built without EGL. A missing GPU is not it -- Mesa's
# software renderer serves both platforms.
#
# 2.54 draws the same page with no EGL display at all, and the bundled runtime
# carries 2.54. So the probe -- the same two questions, in the same order --
# is only asked for a host WebKitGTK older than that, and a "no" hands the
# sign-in over to the bundled runtime instead of opening a window that cannot
# draw. Should a WebKitGTK still give up, the line it prints when it does is
# recognised as it arrives (EGL_ABORT).
_EGL_PROBE = (
    "import ctypes, sys\n"
    "try:\n"
    "    egl = ctypes.CDLL('libEGL.so.1')\n"
    "except OSError as exc:\n"
    "    print(exc)\n"
    "    sys.exit(5)\n"
    "egl.eglQueryString.restype = ctypes.c_char_p\n"
    "egl.eglQueryString.argtypes = [ctypes.c_void_p, ctypes.c_int]\n"
    "egl.eglGetProcAddress.restype = ctypes.c_void_p\n"
    "egl.eglGetProcAddress.argtypes = [ctypes.c_char_p]\n"
    "egl.eglGetDisplay.restype = ctypes.c_void_p\n"
    "egl.eglGetDisplay.argtypes = [ctypes.c_void_p]\n"
    "egl.eglInitialize.argtypes = [ctypes.c_void_p] * 3\n"
    "egl.eglTerminate.argtypes = [ctypes.c_void_p]\n"
    "client = (egl.eglQueryString(None, 0x3055) or b'').split()\n"
    "base = next((name for ext, name in (\n"
    "    (b'EGL_EXT_platform_base', b'eglGetPlatformDisplayEXT'),\n"
    "    (b'EGL_KHR_platform_base', b'eglGetPlatformDisplay'))\n"
    "    if ext in client), None)\n"
    "if b'EGL_MESA_platform_surfaceless' in client and base:\n"
    "    platform = 'surfaceless'\n"
    "    get = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_uint,\n"
    "                           ctypes.c_void_p, ctypes.c_void_p)(\n"
    "        egl.eglGetProcAddress(base))\n"
    "    display = get(0x31DD, None, None)\n"
    "else:\n"
    "    platform = 'default'\n"
    "    display = egl.eglGetDisplay(None)\n"
    "if display and egl.eglInitialize(display, None, None):\n"
    "    egl.eglTerminate(display)\n"
    "    sys.exit(0)\n"
    "print(platform, egl.eglGetError())\n"
    "sys.exit(4)\n"
)
_EGL_NO_DISPLAY = 4
_EGL_NO_LIBRARY = 5
_EGL_PROBE_KEYS = ("LD_LIBRARY_PATH", "__EGL_VENDOR_LIBRARY_FILENAMES",
                   "__EGL_VENDOR_LIBRARY_DIRS", "EGL_PLATFORM",
                   "LIBGL_ALWAYS_SOFTWARE", "DISPLAY", "WAYLAND_DISPLAY")
_EGL_CACHE = {}
_EGL_ERRORS = {
    0x3001: "EGL_NOT_INITIALIZED", 0x3002: "EGL_BAD_ACCESS",
    0x3003: "EGL_BAD_ALLOC", 0x3008: "EGL_BAD_DISPLAY",
    0x3009: "EGL_BAD_MATCH", 0x300C: "EGL_BAD_PARAMETER",
}
# What the web process prints when it gives up, for a host the probe could not
# tell about.
EGL_ABORT = re.compile(
    r"Could not create (?:(\w+) )?EGL display: ([^.]+)\. Aborting")


def host_package_name():
    """What the host's package manager calls the WebKitGTK runtime."""
    for manager, package in _PACKAGES:
        if shutil.which(manager):
            return package
    return "libwebkit2gtk-4.1-0"


def host_has_webkitgtk():
    """Whether the WebKitGTK the sign-in webview needs is installed.

    ctypes rather than pkg-config: the runtime library is what matters, and the
    development package is not installed on a user's machine. Loading it is
    what proves it, so find_library -- which only reads the ldconfig cache --
    is the fallback, for layouts where the soname alone is not enough.
    """
    try:
        ctypes.CDLL("libwebkit2gtk-4.1.so.0")
        return True
    except OSError:
        pass
    located = ctypes.util.find_library("webkit2gtk-4.1")
    if not located:
        return False
    try:
        ctypes.CDLL(located)
        return True
    except OSError:
        return False


def load_failure(binary, env=None):
    """Why the dynamic loader cannot start ``binary``, or None when it can.

    `--version` is answered by the argument parser before Xodus touches the
    network, the keyring or a device identity, so this costs a few
    milliseconds and reports exactly what the loader thinks, rather than
    guessing from one library's presence. What it printed is the answer: a
    missing library and a C library that is too old are different problems.
    """
    try:
        proc = subprocess.run([str(binary), "--version"], env=env,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, text=True,
                              errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc) or type(exc).__name__
    if proc.returncode == 0:
        return None
    return (proc.stderr or "").strip() or f"exit status {proc.returncode}"


def binary_loads(binary, env=None):
    """Whether the dynamic loader can start ``binary`` with this environment."""
    return load_failure(binary, env) is None


def host_tls_available(env=None):
    """Whether the host's WebKitGTK can open an https:// page.

    False only when the probe ran and GIO said it has no TLS backend; a probe
    that could not run at all (no Python able to load the library, a
    timeout) is "cannot tell", and answers True so that nothing which used
    to work is taken away on a guess.
    """
    source = os.environ if env is None else env
    key = tuple(source.get(name) for name in _TLS_PROBE_KEYS)
    if key not in _TLS_CACHE:
        try:
            proc = subprocess.run([sys.executable, "-c", _TLS_PROBE],
                                  env=dict(source), stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=30)
            _TLS_CACHE[key] = proc.returncode != _TLS_MISSING
        except (OSError, subprocess.SubprocessError):
            _TLS_CACHE[key] = True
    return _TLS_CACHE[key]


def egl_failure(env=None):
    """Why WebKitGTK cannot draw on this host, or None when it can.

    None as well when the probe could not run at all: as with TLS, a sign-in
    that might work is not refused on a guess.
    """
    source = os.environ if env is None else env
    key = tuple(source.get(name) for name in _EGL_PROBE_KEYS)
    if key not in _EGL_CACHE:
        try:
            proc = subprocess.run([sys.executable, "-c", _EGL_PROBE],
                                  env=dict(source), stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True,
                                  errors="replace", timeout=30)
        except (OSError, subprocess.SubprocessError):
            proc = None
        _EGL_CACHE[key] = _egl_answer(proc)
    return _EGL_CACHE[key]


def _egl_answer(proc):
    if proc is None:
        return None
    said = (proc.stdout or "").strip()
    if proc.returncode == _EGL_NO_LIBRARY:
        return f"libEGL.so.1 could not be loaded ({said or 'not found'})"
    if proc.returncode != _EGL_NO_DISPLAY:
        return None
    platform, _, code = said.partition(" ")
    try:
        error = _EGL_ERRORS.get(int(code), hex(int(code)))
    except ValueError:
        error = "no error code"
    return f"the {platform or 'default'} EGL display did not open ({error})"


def bundled(env):
    """Whether ``env`` runs xodus-cli against the bundled runtime."""
    libraries = str(XODUS_WEBVIEW_DIR / "lib")
    return libraries in (env.get("LD_LIBRARY_PATH") or "").split(os.pathsep)


def host_webkit_version():
    """(major, minor) of the host's WebKitGTK, or None when it cannot be told."""
    try:
        library = ctypes.CDLL("libwebkit2gtk-4.1.so.0")
        major = int(library.webkit_get_major_version())
        minor = int(library.webkit_get_minor_version())
    except (OSError, AttributeError, ValueError):
        return None
    return (major, minor)


def host_egl_failure(env=None):
    """Why the host's WebKitGTK would open a blank sign-in window, or None.

    Only a WebKitGTK older than WEBKIT_DRAWS_WITHOUT_EGL can: from that
    version on it draws without an EGL display. A version that cannot be read
    is not refused on a guess.
    """
    version = host_webkit_version()
    if version is None or version >= WEBKIT_DRAWS_WITHOUT_EGL:
        return None
    failure = egl_failure(env)
    if failure is None:
        return None
    return f"its WebKitGTK {version[0]}.{version[1]} needs an EGL display, " \
           f"and {failure}"


def egl_message(detail):
    """What to tell someone whose WebKitGTK gave up for want of EGL."""
    return (
        "The Microsoft sign-in window stayed blank: this WebKitGTK draws "
        "every page through EGL, and " + detail + ". Install Mesa with EGL "
        "enabled (it also draws without a GPU), or the EGL vendor file of "
        "your graphics driver if libEGL comes from libglvnd, then sign in "
        "again. The Flatpak build carries its own graphics libraries.")


def _host_failure(binary):
    key = str(binary)
    if key not in _HOST_PROBE:
        _HOST_PROBE[key] = load_failure(binary)
    return _HOST_PROBE[key]


def _host_glibc():
    """The C library version this system runs, e.g. "2.35", or None."""
    try:
        name, _, version = os.confstr("CS_GNU_LIBC_VERSION").partition(" ")
    except (AttributeError, OSError, ValueError):
        return None
    return version.strip() if name == "glibc" and version.strip() else None


def glibc_too_old_message(text):
    """What to tell a system whose glibc is older than xodus-cli needs.

    None unless the loader said so. The newest version it asked for is the
    floor; installing WebKitGTK, or the bundled runtime, cannot lower it.
    """
    wanted = _GLIBC_TOO_OLD.findall(text or "")
    if not wanted:
        return None
    floor = max(wanted, key=lambda version: tuple(
        int(part) for part in version.split(".")))
    host = _host_glibc()
    return (
        "Minecraft is downloaded and started through xodus-cli, which needs "
        f"glibc {floor} or newer"
        + (f", and this system has glibc {host}" if host else "")
        + ". Installing WebKitGTK does not change that. Use the Flatpak "
        "build, which carries its own runtime, or a distribution release "
        f"that ships glibc {floor} or newer.")


# ---------------------------------------------------------------- install


def installed():
    """True when the bundled runtime is unpacked and matches the pin."""
    marker = XODUS_WEBVIEW_DIR / ".rev"
    try:
        current = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return (current == XODUS_WEBVIEW_REV
            and (XODUS_WEBVIEW_DIR / _WEBKIT_LIB).is_file())


def _local_asset():
    """The reviewed archive shipped beside an unreleased candidate, if any."""
    anchors = []
    appimage = os.environ.get("APPIMAGE", "").strip()
    if appimage:
        anchors.append(Path(appimage).expanduser().resolve().parent)
    try:
        anchors.append(Path(sys.argv[0]).expanduser().resolve().parent)
    except (OSError, RuntimeError):
        pass
    return next((anchor / ASSET for anchor in anchors
                 if (anchor / ASSET).is_file()), None)


def _fetch():
    """The verified archive, downloaded if it is not already beside us."""
    expected = XODUS_WEBVIEW_SHA256.strip().lower()
    if not expected:
        raise BolError(
            f"The bundled WebKitGTK runtime '{ASSET}' has not been published "
            "yet, so it cannot be verified or installed.")
    archive = _local_asset()
    local = archive is not None
    if not local:
        try:
            releases = gh_releases(WINEGDK_PREBUILT_REPO, 30)
        except Exception as exc:
            raise BolError(
                f"Could not look up the WebKitGTK runtime ({exc}). Check the "
                "network connection and try again.") from exc
        url = None
        for release in releases or []:
            url, _name, _ = asset_url(release, lambda name: name == ASSET)
            if url:
                break
        if not url:
            raise BolError(
                f"The WebKitGTK runtime '{ASSET}' has not been published yet.")
        archive = CACHE / ASSET
        if not archive.is_file():
            info("Downloading WebKitGTK for the Microsoft sign-in "
                 "(one-time, ~80 MB) …")
            download(url, archive, "WebKitGTK runtime")

    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    if actual != expected:
        # Never keep bytes that failed the pin: a cached bad archive would make
        # every later retry fail before download() could fetch a good one.
        if not local:
            archive.unlink(missing_ok=True)
        raise BolError(
            f"WebKitGTK runtime SHA-256 mismatch (expected {expected}, got "
            f"{actual}); it was not installed.")
    return archive


def _rewrite_loader_cache(staging, root):
    """Point the pixbuf loader cache at where the bundle will live.

    ``root``, not ``staging``: the cache records absolute module paths, and it
    is read long after the staging directory has been renamed into place.
    """
    cache = staging / "pixbuf-loaders" / "loaders.cache"
    try:
        text = cache.read_text(encoding="utf-8")
    except OSError:
        return
    cache.write_text(text.replace(_PLACEHOLDER, str(root)), encoding="utf-8")


def ensure_runtime():
    """Unpack the bundled WebKitGTK runtime, and return its directory.

    Raises BolError with something the user can act on; callers add the
    host-package alternative, which is the better answer wherever it works.
    """
    if installed():
        return XODUS_WEBVIEW_DIR
    archive = _fetch()
    XODUS_WEBVIEW_DIR.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".webview-dl-",
                                    dir=XODUS_WEBVIEW_DIR.parent))
    previous = XODUS_WEBVIEW_DIR.with_name(XODUS_WEBVIEW_DIR.name + ".old")
    try:
        try:
            with tarfile.open(archive) as tar:
                safe_extract_tar(tar, staging)
        except Exception as exc:
            raise BolError(
                f"The WebKitGTK runtime archive is unreadable ({exc}).") \
                from exc
        if not (staging / _WEBKIT_LIB).is_file():
            raise BolError(
                "The WebKitGTK runtime archive carries no "
                "libwebkit2gtk-4.1.so.0.")
        _rewrite_loader_cache(staging, XODUS_WEBVIEW_DIR)
        (staging / ".rev").write_text(XODUS_WEBVIEW_REV + "\n",
                                      encoding="utf-8")
        shutil.rmtree(previous, ignore_errors=True)
        if XODUS_WEBVIEW_DIR.exists():
            XODUS_WEBVIEW_DIR.rename(previous)
        staging.rename(XODUS_WEBVIEW_DIR)
        staging = None
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(previous, ignore_errors=True)
    ok("WebKitGTK runtime ready (Microsoft sign-in and game download).")
    return XODUS_WEBVIEW_DIR


# ------------------------------------------------------- helper processes


def helper_dir():
    """Where WebKitGTK is told to spawn its helper processes from.

    Short by necessity: it replaces a literal inside the library, so it cannot
    be longer than the compiled-in path. XDG_RUNTIME_DIR is the right home for
    it -- per-user, 0700 and on tmpfs -- with a /tmp fallback for the sessions
    that have none.
    """
    uid = os.getuid()
    runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if runtime and Path(runtime).is_dir():
        return Path(runtime) / "bol-webkit"
    return Path(f"/tmp/bol-webkit-{uid}")


def _own_private_dir(path):
    """Create ``path`` as a private directory owned by us, or refuse it.

    /tmp is shared, so a directory that is already there is only reused when it
    is a real directory, ours, and not group- or world-writable.
    """
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise BolError(
            f"Could not create the WebKitGTK helper directory {path} "
            f"({exc}).") from exc
    entry = os.lstat(path)
    if not stat.S_ISDIR(entry.st_mode) or entry.st_uid != os.getuid() \
            or entry.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise BolError(
            f"The WebKitGTK helper directory {path} is not private to this "
            "account; refusing to run the sign-in from it.")
    return path


def _point_helpers_at(root, target):
    """Rewrite the helper directory compiled into the bundled library.

    The replacement is NUL-padded to the original length, so this is an
    in-place edit of one C string that moves nothing else in the file. The
    archive it came from was already checked against its SHA-256 pin.
    """
    original = XODUS_WEBVIEW_EXEC_DIR.encode() + b"\x00"
    replacement = str(target).encode()
    if len(replacement) >= len(original):
        raise BolError(
            f"The WebKitGTK helper directory {target} is too long to point "
            f"the runtime at (limit {len(original) - 1} characters).")
    replacement += b"\x00" * (len(original) - len(replacement))
    # mmap rather than read_bytes(): this runs on every launch, and the library
    # is ~100 MB.
    with open(root / _WEBKIT_LIB, "r+b") as handle:
        with mmap.mmap(handle.fileno(), 0) as image:
            if image.find(replacement) >= 0:
                return
            offset = _helper_literal(image, len(original))
            if offset is None:
                raise BolError(
                    "The bundled WebKitGTK does not carry the expected helper "
                    "path, so its helper processes cannot be relocated.")
            image[offset:offset + len(replacement)] = replacement


# A literal this launcher already rewrote: helper_dir() ends in one of these
# names, and the rest of the original literal is NUL padding.
_REWRITTEN = re.compile(rb"(?<=\x00)/[^\x00]*/bol-webkit(?:-\d+)?\x00")


def _helper_literal(image, length):
    """Where the helper directory literal is, pristine or rewritten before.

    The rewrite stays in the unpacked library, so the next launch finds the
    directory an earlier one chose, not the compiled-in path. That directory
    changes with the session: XDG_RUNTIME_DIR is set in one and not in the
    next, which falls back to /tmp, or names another place. Each launch has to
    be able to move it again.
    """
    offset = image.find(XODUS_WEBVIEW_EXEC_DIR.encode() + b"\x00")
    if offset >= 0:
        return offset
    found = [match.start() for match in _REWRITTEN.finditer(image)
             if match.end() - match.start() <= length
             and not image[match.end():match.start() + length].strip(b"\x00")]
    return found[0] if len(found) == 1 else None


def _link_helpers(root, target):
    """Publish the helper processes under the path the library now uses.

    Symlinks rather than copies: the helpers find the bundled libraries through
    a RUNPATH relative to $ORIGIN, which the loader resolves against the real
    file, so a link keeps them working without duplicating anything.
    """
    source = root / "libexec" / "webkit2gtk-4.1"
    for name in _HELPERS:
        original = source / name
        if not original.is_file():
            continue
        link = target / name
        try:
            if link.is_symlink() and os.readlink(link) == str(original):
                continue
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(original)
        except OSError as exc:
            raise BolError(
                f"Could not publish the WebKitGTK helper {name} in {target} "
                f"({exc}).") from exc


def prepare():
    """Install the runtime if needed and make it usable; return its directory.

    Both steps run on every launch: XDG_RUNTIME_DIR is cleared between
    sessions, so the helper links have to be re-made even when the bundle
    itself is already unpacked.
    """
    root = ensure_runtime()
    target = _own_private_dir(helper_dir())
    _point_helpers_at(root, target)
    _link_helpers(root, target)
    return root


def runtime_env(env, root=None):
    """Return ``env`` with the bundled runtime added, and what it replaced.

    The second value maps each variable to its previous setting (None where it
    was unset) so a child that must not inherit the bundle -- the game -- can
    be handed back the environment it would have had.
    """
    root = Path(root) if root is not None else XODUS_WEBVIEW_DIR
    previous = {name: env.get(name) for name in _VARS}
    libraries = str(root / "lib")
    existing = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = (libraries + os.pathsep + existing
                              if existing else libraries)
    env["WEBKIT_INJECTED_BUNDLE_PATH"] = str(
        root / "libexec" / "webkit2gtk-4.1" / "injected-bundle")
    env["GIO_EXTRA_MODULES"] = str(root / "gio-modules")
    env["GDK_PIXBUF_MODULE_FILE"] = str(
        root / "pixbuf-loaders" / "loaders.cache")
    env["GSETTINGS_SCHEMA_DIR"] = str(root / "schemas")
    # The host's GTK modules and input methods are built against the host's
    # GTK; loading them into the bundled one crashes the sign-in window.
    env["GTK_PATH"] = ""
    env["GTK_MODULES"] = ""
    env["GTK_IM_MODULE"] = "gtk-im-context-simple"
    return env, previous


def portable_renderer(env):
    """Ask WebKitGTK for the renderer that survives every compositor.

    Returns what it replaced, in restore_env()'s shape, so the game can be
    handed back the environment it would have had. A value the session already
    set is left alone: that is how someone whose desktop is fine asks for the
    accelerated path back.
    """
    previous = {_RENDERER: env.get(_RENDERER)}
    if not (env.get(_RENDERER) or "").strip():
        env[_RENDERER] = "1"
    return previous


def quiet_accessibility(env):
    """Keep the sign-in page off the accessibility bus.

    Returns what it replaced, in restore_env()'s shape, so the game can be
    handed back the environment it would have had. Whether the variable is
    already there is what decides, not whether it says anything: empty is the
    value that means "no bridge", so a session that set it has already made
    this choice. BOL_WEBVIEW_A11Y=1 is how someone who needs a screen reader on
    that window asks for the bridge back, and takes the abort with it.
    """
    previous = {_A11Y: env.get(_A11Y)}
    if _A11Y not in env and not env_flag(os.environ.get(_A11Y_OPT_IN)):
        env[_A11Y] = ""
    return previous


def restore_env(env, previous):
    """Undo runtime_env() on ``env`` (a mapping, usually os.environ)."""
    for name, value in (previous or {}).items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return env


def missing_message(detail=None):
    """Why the sign-in/download cannot run, and what to do about it."""
    return (
        (detail or "Minecraft is downloaded and started through xodus-cli, "
                   "which needs the WebKitGTK library and cannot find it.")
        + f"\nInstall it with your package manager ({host_package_name()}), "
        "or use the Flatpak build, which carries its own.")


def apply(binary, env, force=False):
    """Make ``binary`` usable from ``env``; return what that replaced.

    The renderer and accessibility settings go in either way -- the sign-in
    window is just as fragile against the host's WebKitGTK as against the
    bundled one. The library itself is only added where the host has none,
    which is the exception; everywhere it is packaged, that half is a no-op.

    The return value is always a restore map, in restore_env()'s shape. A
    failure leaves ``env`` exactly as it was found, and raises BolError with
    the host-package alternative spelled out.
    """
    previous = portable_renderer(env)
    previous.update(quiet_accessibility(env))
    if not force:
        failure = _host_failure(binary)
        if failure is None:
            blank = host_egl_failure(env)
            if host_tls_available(env) and blank is None:
                return previous
            if blank is not None:
                # The bundle's WebKitGTK draws without one (#273).
                info(f"This system's WebKitGTK cannot draw the Microsoft "
                     f"sign-in: {blank}. Using the bundled WebKitGTK runtime "
                     "instead, which draws without one.")
            else:
                # The bundle carries its own GIO TLS module, and its library
                # directory goes in front of the host's.
                info("This system's WebKitGTK has no TLS support (GIO finds "
                     "no glib-networking module), so the Microsoft sign-in "
                     "would only say \"TLS support is not available\". Using "
                     "the bundled WebKitGTK runtime instead.")
        else:
            too_old = glibc_too_old_message(failure)
            if too_old:
                # The bundle is ~80 MB built against the same glibc floor.
                restore_env(env, previous)
                raise BolError(too_old)
    try:
        root = prepare()
    except BolError as exc:
        restore_env(env, previous)
        raise BolError(missing_message(str(exc))) from exc
    _, replaced = runtime_env(env, root)
    previous.update(replaced)
    failure = load_failure(binary, env)
    if failure is not None:
        restore_env(env, previous)
        raise BolError(glibc_too_old_message(failure) or missing_message(
            "The bundled WebKitGTK runtime did not load on this system: "
            + failure.splitlines()[0]))
    return previous


def status():
    """For `doctor`: where the webview's WebKitGTK comes from on this host.

    Returns (summary, package) where package is the host package to install,
    or None when the sign-in has a working library to use.
    """
    if host_has_webkitgtk():
        if not host_tls_available():
            return ("host WebKitGTK has no TLS support (glib-networking); "
                    "the bundled runtime is used instead"), None
        blank = host_egl_failure()
        if blank is not None:
            return (f"host WebKitGTK cannot draw: {blank}; the bundled "
                    "runtime is used instead"), None
        return "OK (store sign-in)", None
    if installed():
        return "OK (bundled runtime)", None
    if XODUS_WEBVIEW_SHA256.strip():
        return "bundled runtime, downloaded on first use", None
    return "MISSING (store sign-in)", host_package_name()
