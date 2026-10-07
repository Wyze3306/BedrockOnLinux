"""X11 helpers: RandR monitor geometry, and the Steam identity of a window."""
# SPDX-License-Identifier: MIT
import ctypes
import os
import re
import shutil
import subprocess
from contextlib import contextmanager

from .deps import have


def _monitors_via_xlib():
    if not have("Xlib"):
        return None
    try:
        from Xlib import display as xdisplay
        from Xlib import error as xerror
    except ImportError:
        return None
    connection_errors = (xerror.DisplayError, xerror.ConnectionClosedError,
                         OSError)
    try:
        d = xdisplay.Display()
    except connection_errors:
        return None
    try:
        root = d.screen().root
        get_monitors = getattr(root, "xrandr_get_monitors", None)
        if get_monitors is None:
            return None  # server predates RandR 1.5
        monitors = get_monitors(is_active=True).monitors
        if not monitors:
            return None
        return tuple(
            (
                int(monitor.x), int(monitor.y),
                int(monitor.width_in_pixels),
                int(monitor.height_in_pixels),
                bool(monitor.primary),
            )
            for monitor in monitors
        )
    except (
            xerror.XError, *connection_errors, AttributeError, IndexError,
            TypeError, ValueError):
        return None
    finally:
        try:
            d.close()
        except Exception:
            pass


def _primary_via_xlib():
    """Primary monitor size via RandR GetMonitors, or None."""
    monitors = _monitors_via_xlib()
    if not monitors:
        return None
    primary = next(
        (monitor for monitor in monitors if monitor[4]), monitors[0])
    return str(primary[2]), str(primary[3])


def _refresh_via_xlib():
    """Fastest active mode's refresh rate in Hz via RandR, or None.

    RandR describes a mode by its pixel clock and its total (including blanking)
    line and frame sizes, so the refresh rate is the one divided by the other.
    """
    if not have("Xlib"):
        return None
    try:
        from Xlib import display as xdisplay
        from Xlib import error as xerror
    except ImportError:
        return None
    connection_errors = (xerror.DisplayError, xerror.ConnectionClosedError,
                         OSError)
    try:
        d = xdisplay.Display()
    except connection_errors:
        return None
    try:
        resources = d.screen().root.xrandr_get_screen_resources_current()
        modes = {mode.id: mode for mode in resources.modes}
        rates = []
        for crtc in resources.crtcs:
            info = d.xrandr_get_crtc_info(crtc, resources.config_timestamp)
            mode = modes.get(info.mode)
            if mode is None:
                continue  # a disconnected or disabled CRTC drives no mode
            total = mode.h_total * mode.v_total
            if total > 0:
                rates.append(mode.dot_clock / float(total))
        return max(rates) if rates else None
    except (
            xerror.XError, *connection_errors, AttributeError, IndexError,
            TypeError, ValueError, ZeroDivisionError):
        return None
    finally:
        try:
            d.close()
        except Exception:
            pass


# xrandr marks the mode an output is currently driving with a trailing '*',
# and may append '+' to the preferred one: "1920x1080  143.85*+  60.00".
_ACTIVE_MODE_RATE = re.compile(r"(\d+(?:\.\d+)?)\*")


def _refresh_via_xrandr_cli(runner=None):
    """Fallback: fastest rate xrandr marks as current, or None."""
    run = _xrandr_runner(runner)
    if run is None:
        return None
    result = run([])
    if result is None or getattr(result, "returncode", 1) != 0:
        return None
    rates = [float(rate) for rate
             in _ACTIVE_MODE_RATE.findall(getattr(result, "stdout", ""))]
    rates = [rate for rate in rates if rate > 0]
    return max(rates) if rates else None


def primary_output_refresh_hz(runner=None):
    """Fastest refresh rate any active output is driving, or None.

    The fastest rather than the primary one: nothing here can tell which
    monitor the game window ended up on, and the only use for this number is
    an upper bound on useful frames. Taking the fastest can never put that
    bound below what a display the player is actually looking at can show.

    Tries python-xlib's structured RandR reply first and falls back to parsing
    xrandr CLI text, exactly as `primary_output_size` does; `runner` only
    affects the CLI fallback.
    """
    return _refresh_via_xlib() or _refresh_via_xrandr_cli(runner)


_LIST_MONITOR = re.compile(
    r"^\s*\d+:\s+\+(\*)?\S+\s+"
    r"(\d+)/\d+x(\d+)/\d+([+-]\d+)([+-]\d+)")
_CONNECTED_MONITOR = re.compile(
    r"^\S+\s+connected(?:\s+(primary))?\s+"
    r"(\d+)x(\d+)([+-]\d+)([+-]\d+)")


def _parse_list_monitors(output):
    monitors = []
    for line in output.splitlines():
        match = _LIST_MONITOR.search(line)
        if match:
            monitors.append((
                int(match.group(4)), int(match.group(5)),
                int(match.group(2)), int(match.group(3)),
                bool(match.group(1)),
            ))
    return tuple(monitors)


def _parse_connected_monitors(output):
    monitors = []
    for line in output.splitlines():
        match = _CONNECTED_MONITOR.search(line)
        if match:
            monitors.append((
                int(match.group(4)), int(match.group(5)),
                int(match.group(2)), int(match.group(3)),
                bool(match.group(1)),
            ))
    return tuple(monitors)


def _xrandr_runner(runner=None):
    binary = shutil.which("xrandr")
    if not binary and runner is not None:
        binary = "xrandr"
    if not binary:
        return None
    runner = runner or subprocess.run

    def run(args):
        try:
            return runner([binary] + args, capture_output=True, text=True,
                          errors="replace", timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            return None

    return run


def _monitors_via_xrandr_cli(runner=None):
    run = _xrandr_runner(runner)
    if run is None:
        return None
    for option in ("--listactivemonitors", "--listmonitors"):
        result = run([option])
        if result is not None and getattr(result, "returncode", 1) == 0:
            monitors = _parse_list_monitors(getattr(result, "stdout", ""))
            if monitors:
                return monitors
    result = run([])
    if result is None or getattr(result, "returncode", 1) != 0:
        return None
    return _parse_connected_monitors(getattr(result, "stdout", "")) or None


def _primary_via_xrandr_cli(runner=None):
    """Fallback: shell out to `xrandr` and parse its text output."""
    run = _xrandr_runner(runner)
    if run is None:
        return None
    result = run(["--listmonitors"])
    if result is not None and getattr(result, "returncode", 1) == 0:
        monitors = _parse_list_monitors(getattr(result, "stdout", ""))
        if monitors:
            primary = next(
                (monitor for monitor in monitors if monitor[4]), monitors[0])
            return str(primary[2]), str(primary[3])

    result = run([])
    if result is None:
        return None
    output = getattr(result, "stdout", "")
    monitors = _parse_connected_monitors(output)
    if monitors:
        primary = next(
            (monitor for monitor in monitors if monitor[4]), monitors[0])
        return str(primary[2]), str(primary[3])
    m = re.search(r"current\s+(\d+)\s+x\s+(\d+)", output)
    return (m.group(1), m.group(2)) if m else None


def monitor_geometries(runner=None):
    """Active monitor rectangles as (x, y, width, height) tuples."""
    monitors = (
        _monitors_via_xlib()
        or _monitors_via_xrandr_cli(runner)
        or ()
    )
    return tuple(monitor[:4] for monitor in monitors)


def primary_output_size(runner=None):
    """Primary monitor's (width, height) as strings, or None.

    Tries python-xlib's structured RandR GetMonitors reply first; falls back
    to parsing xrandr CLI text when python-xlib is missing, the X server
    predates RandR 1.5, or the connection fails. `runner` only affects the
    CLI fallback.
    """
    return _primary_via_xlib() or _primary_via_xrandr_cli(runner)


# ---------------------------------------------------------------------------
# The Steam application ID a window belongs to
# ---------------------------------------------------------------------------
#
# Gamescope attributes every window to a Steam application, and Game Mode only
# presents windows it could attribute to the application Steam launched. It
# reads that identity from the window's ``STEAM_GAME`` property and follows it
# live, so stamping the property on a window that is already mapped is enough
# to make it focusable.

_STEAM_GAME_PROPERTY = "STEAM_GAME"
_XA_CARDINAL = 6                # Xatom.h
_PROP_MODE_REPLACE = 0          # X.h
_IS_VIEWABLE = 2                # X.h, XWindowAttributes.map_state
# One level for a plain compositing manager such as gamescope's, which leaves
# toplevels as children of the root, and one more for a desktop window manager
# that reparents each of them into a frame. The budget bounds the walk on a
# session whose tree is neither.
_WINDOW_SEARCH_DEPTH = 3
_WINDOW_SEARCH_BUDGET = 512


class _XClassHint(ctypes.Structure):
    """XClassHint, with both strings as raw pointers so both can be freed."""

    _fields_ = [("res_name", ctypes.c_void_p),
                ("res_class", ctypes.c_void_p)]


class _XWindowAttributes(ctypes.Structure):
    """XWindowAttributes, in full: Xlib fills the whole struct."""

    _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int),
                ("width", ctypes.c_int), ("height", ctypes.c_int),
                ("border_width", ctypes.c_int), ("depth", ctypes.c_int),
                ("visual", ctypes.c_void_p), ("root", ctypes.c_ulong),
                ("class", ctypes.c_int), ("bit_gravity", ctypes.c_int),
                ("win_gravity", ctypes.c_int), ("backing_store", ctypes.c_int),
                ("backing_planes", ctypes.c_ulong),
                ("backing_pixel", ctypes.c_ulong),
                ("save_under", ctypes.c_int), ("colormap", ctypes.c_ulong),
                ("map_installed", ctypes.c_int), ("map_state", ctypes.c_int),
                ("all_event_masks", ctypes.c_long),
                ("your_event_mask", ctypes.c_long),
                ("do_not_propagate_mask", ctypes.c_long),
                ("override_redirect", ctypes.c_int),
                ("screen", ctypes.c_void_p)]


_X_ERROR_HANDLER = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)


def _ignore_x_error(_display, _error):
    """Xlib's default error handler exits the process; this one must not.

    Every window here belongs to another client and may be destroyed between
    the moment the tree is read and the moment the property is set, which is
    an ordinary BadWindow and never a reason to take the launcher down.
    """
    return 0


# Xlib stores the pointer, so this object has to outlive every call using it.
_IGNORE_X_ERROR = _X_ERROR_HANDLER(_ignore_x_error)


def _load_xlib():
    """libX11 with the entry points the window tagger uses, or None."""
    try:
        xlib = ctypes.cdll.LoadLibrary("libX11.so.6")
        xlib.XOpenDisplay.restype = ctypes.c_void_p
        xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        xlib.XCloseDisplay.argtypes = [ctypes.c_void_p]
        xlib.XSetErrorHandler.restype = ctypes.c_void_p
        xlib.XSetErrorHandler.argtypes = [ctypes.c_void_p]
        xlib.XDefaultRootWindow.restype = ctypes.c_ulong
        xlib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        xlib.XInternAtom.restype = ctypes.c_ulong
        xlib.XInternAtom.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        xlib.XQueryTree.restype = ctypes.c_int
        xlib.XQueryTree.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
            ctypes.POINTER(ctypes.c_uint)]
        xlib.XGetClassHint.restype = ctypes.c_int
        xlib.XGetClassHint.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_XClassHint)]
        xlib.XGetWindowAttributes.restype = ctypes.c_int
        xlib.XGetWindowAttributes.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(_XWindowAttributes)]
        xlib.XChangeProperty.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
            ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        xlib.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        xlib.XFree.argtypes = [ctypes.c_void_p]
        xlib.XDefaultScreen.restype = ctypes.c_int
        xlib.XDefaultScreen.argtypes = [ctypes.c_void_p]
        xlib.XIconifyWindow.restype = ctypes.c_int
        xlib.XIconifyWindow.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int]
        return xlib
    except (OSError, AttributeError):
        return None


class _XlibWindows:
    """The few X operations the window tagger needs, over libX11.

    Kept behind this handful of methods so the walk that uses them stays
    ordinary Python: the ctypes marshalling is the part no test can drive.
    """

    def __init__(self, lib, display):
        self._lib = lib
        self._display = display
        self._atoms = {}

    def root(self):
        return int(self._lib.XDefaultRootWindow(self._display))

    def children(self, window):
        root = ctypes.c_ulong()
        parent = ctypes.c_ulong()
        children = ctypes.POINTER(ctypes.c_ulong)()
        count = ctypes.c_uint()
        if not self._lib.XQueryTree(
                self._display, ctypes.c_ulong(window), ctypes.byref(root),
                ctypes.byref(parent), ctypes.byref(children),
                ctypes.byref(count)):
            return ()
        if not children:
            return ()
        try:
            return tuple(int(children[i]) for i in range(count.value))
        finally:
            self._lib.XFree(ctypes.cast(children, ctypes.c_void_p))

    def wm_classes(self, window):
        """Lowercased WM_CLASS instance and class names of `window`."""
        hint = _XClassHint()
        if not self._lib.XGetClassHint(
                self._display, ctypes.c_ulong(window), ctypes.byref(hint)):
            return ()
        names = []
        for pointer in (hint.res_name, hint.res_class):
            if not pointer:
                continue
            try:
                names.append(ctypes.string_at(pointer)
                             .decode("utf-8", "replace").lower())
            finally:
                self._lib.XFree(ctypes.c_void_p(pointer))
        return tuple(names)

    def is_presentable(self, window):
        """Whether `window` is one a compositor could actually present."""
        attributes = _XWindowAttributes()
        if not self._lib.XGetWindowAttributes(
                self._display, ctypes.c_ulong(window),
                ctypes.byref(attributes)):
            return False
        return (attributes.map_state == _IS_VIEWABLE
                and not attributes.override_redirect)

    def set_cardinal(self, window, name, value):
        atom = self._atoms.get(name)
        if atom is None:
            atom = int(self._lib.XInternAtom(
                self._display, name.encode(), False))
            self._atoms[name] = atom
        if not atom:
            return False
        payload = (ctypes.c_ulong * 1)(value)
        self._lib.XChangeProperty(
            self._display, ctypes.c_ulong(window), ctypes.c_ulong(atom),
            ctypes.c_ulong(_XA_CARDINAL), 32, _PROP_MODE_REPLACE,
            ctypes.cast(payload, ctypes.c_void_p), 1)
        return True

    def iconify(self, window):
        """Ask the window manager to minimize `window` (ICCCM 4.1.4)."""
        return bool(self._lib.XIconifyWindow(
            self._display, ctypes.c_ulong(window),
            self._lib.XDefaultScreen(self._display)))

    def flush(self):
        self._lib.XSync(self._display, False)


@contextmanager
def _x_windows(display):
    """Open `display` for the window tagger, or yield None when it cannot be.

    Xlib's error handler is process-wide and the launcher's own Tk shares it,
    so it is swapped only for the length of the walk and restored afterwards.
    """
    lib = _load_xlib()
    handle = None
    if lib is not None and display:
        handle = lib.XOpenDisplay(str(display).encode())
    if not handle:
        yield None
        return
    handle = ctypes.c_void_p(handle)
    previous = lib.XSetErrorHandler(_IGNORE_X_ERROR)
    try:
        yield _XlibWindows(lib, handle)
    finally:
        try:
            lib.XSetErrorHandler(previous)
        finally:
            lib.XCloseDisplay(handle)


def _tag_windows(windows, wm_class, value, skip=(),
                 depth=_WINDOW_SEARCH_DEPTH, budget=_WINDOW_SEARCH_BUDGET):
    """Stamp STEAM_GAME on each unstamped `wm_class` toplevel; return them.

    WM_CLASS is a toplevel's property, so a window that has one is somebody's
    toplevel and is never looked inside — that keeps the walk out of the
    client-area children Wine creates within the game's own window, and leaves
    only the frames a reparenting window manager inserts to descend through.

    A matching window still has to be one a compositor could present. Wine
    gives its 1x1 override-redirect helpers — the default IME window and the
    message window — the same class as the game, and an identity would make
    those candidates to be shown instead of it.

    Windows in `skip` were stamped by an earlier pass. Writing the property
    again would change nothing and cost gamescope a focus recomputation each
    time, so a window is stamped once and then only watched.
    """
    tagged = []
    pending = [(windows.root(), 0)]
    while pending and budget > 0:
        window, level = pending.pop(0)
        budget -= 1
        names = ()
        if level:
            names = tuple(str(name).lower()
                          for name in windows.wm_classes(window))
            if wm_class in names:
                if (window not in skip and windows.is_presentable(window)
                        and windows.set_cardinal(
                            window, _STEAM_GAME_PROPERTY, value)):
                    tagged.append(window)
                continue
        if not names and level < depth:
            pending.extend(
                (child, level + 1) for child in windows.children(window))
    if tagged:
        windows.flush()
    return tuple(tagged)


def tag_steam_game_windows(app_id, wm_class, display=None, windows=None,
                           skip=()):
    """Give every `wm_class` toplevel the Steam application ID `app_id`.

    Returns the IDs of the windows stamped by this call, which is empty when
    the display cannot be opened, when libX11 is unavailable, when no such
    window exists yet, or when every one of them is already in `skip` — all
    ordinary states a caller keeps watching from rather than errors.
    """
    try:
        value = int(app_id)
    except (TypeError, ValueError):
        return ()
    if not 0 < value < 2 ** 32:         # a 32-bit CARDINAL, or nothing
        return ()
    wanted = str(wm_class or "").strip().lower()
    if not wanted:
        return ()
    if windows is not None:
        return _tag_windows(windows, wanted, value, skip)
    target = str(display if display is not None
                 else os.environ.get("DISPLAY", "")).strip()
    if not target:
        return ()
    with _x_windows(target) as opened:
        if opened is None:
            return ()
        return _tag_windows(opened, wanted, value, skip)


def find_presentable_window(wm_class, display=None):
    wanted = str(wm_class or "").strip().lower()
    if not wanted:
        return False
    target = str(display if display is not None
                 else os.environ.get("DISPLAY", "")).strip()
    if not target:
        return False
    try:
        with _x_windows(target) as opened:
            if opened is None:
                return False
            pending = [(opened.root(), 0)]
            depth = _WINDOW_SEARCH_DEPTH
            budget = _WINDOW_SEARCH_BUDGET
            while pending and budget > 0:
                window, level = pending.pop(0)
                budget -= 1
                names = ()
                if level:
                    names = tuple(str(name).lower()
                                  for name in opened.wm_classes(window))
                    if wanted in names:
                        if opened.is_presentable(window):
                            return True
                        continue
                if not names and level < depth:
                    pending.extend(
                        (child, level + 1) for child in opened.children(window))
    except Exception:
        pass
    return False


def _presentable_toplevels(windows, wanted, depth=_WINDOW_SEARCH_DEPTH,
                           budget=_WINDOW_SEARCH_BUDGET):
    """The on-screen `wanted` toplevels, looking inside WM frames only."""
    found = []
    pending = [(windows.root(), 0)]
    while pending and budget > 0:
        window, level = pending.pop(0)
        budget -= 1
        names = ()
        if level:
            names = tuple(str(name).lower()
                          for name in windows.wm_classes(window))
            if wanted in names:
                if windows.is_presentable(window):
                    found.append(window)
                continue
        if not names and level < depth:
            pending.extend(
                (child, level + 1) for child in windows.children(window))
    return found


def iconify_windows(wm_class, display=None, windows=None):
    """Minimize every on-screen `wm_class` toplevel; how many were asked.

    Zero, never an error, when the display or libX11 is not there.
    """
    wanted = str(wm_class or "").strip().lower()
    if not wanted:
        return 0
    if windows is not None:
        found = _presentable_toplevels(windows, wanted)
        asked = sum(1 for window in found if windows.iconify(window))
        windows.flush()
        return asked
    target = str(display if display is not None
                 else os.environ.get("DISPLAY", "")).strip()
    if not target:
        return 0
    try:
        with _x_windows(target) as opened:
            if opened is None:
                return 0
            return iconify_windows(wanted, windows=opened)
    except Exception:
        return 0
