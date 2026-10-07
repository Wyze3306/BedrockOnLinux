"""bol.waylanddrv — report whether the engine's Wayland driver can load.

The engine is a hybrid: a reviewed GE-Proton base with our own WineGDK build
overlaid on top. The overlay only replaces the modules that build produced, so
a module Wine's configure quietly dropped does not leave a hole in the
candidate — the base's own module survives in its place, one Wine major
version behind everything around it.

That is what happened to ``winewayland.drv`` (issue #180). The build container
lacked ``libxkbregistry-dev``, configure disabled the Wayland driver without
failing, and the shipped engine ended up with GE-Proton's Wine 10 driver next
to our Wine 11 ``win32u.so``. Wine 11 no longer exports the
``win32u_(get|set)_window_pixel_format`` entry points that driver imports, so
it fails its ``PROCESS_ATTACH`` on an undefined symbol and no window is ever
created — ``BOL_INPUT=wayland`` could not start the game on any host.

Detection is a byte search, like the ntsync probe: Wine's TRACE and ERR macros
put each module's source path in its ``.rodata``, so a module carries the tree
it was compiled from. Two modules of one Wine build share that tree; a
leftover from another build does not. Nothing is loaded, no Wine process is
started and no graphics library is opened, so this can run before a launch.
"""
# SPDX-License-Identifier: MIT

import mmap
import re
from collections import Counter
from pathlib import Path

# Our WoW64 engine installs the Unix modules under files/lib; a user-supplied
# Proton on the classic layout uses files/lib64. An engine on neither is one
# we cannot read, not one we may accuse.
_UNIX_DIRS = ("files/lib/wine/x86_64-unix", "files/lib64/wine/x86_64-unix")

# The driver takes its Wine entry points from win32u, so win32u is the module
# its build has to agree with.
_WIN32U = ("win32u.so", b"/dlls/win32u/")
_DRIVER = ("winewayland.so", b"/dlls/winewayland.drv/")

# A source path is NUL-terminated in .rodata, so the tree it was built from is
# whatever precedes the marker back to the previous NUL. Bound the walk: a
# marker that is not preceded by one within this many bytes is not a string.
_MAX_ROOT = 512

# Only an absolute or dot-relative directory counts, which rejects the
# occasional match whose preceding bytes are printable but are not a path.
_ROOT = re.compile(rb"\A(?:/|\.{1,2}/)[\w.+-]+(?:/[\w.+-]+)*\Z")


def _build_root(path, marker):
    """The source tree a Wine module was compiled from, or None.

    None covers every case we must not turn into a verdict: the module is not
    installed, it cannot be read, or it carries no source path at all.
    """
    try:
        with Path(path).open("rb") as handle:
            with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
                roots = Counter()
                for match in re.finditer(re.escape(marker), data):
                    head = data[max(0, match.start() - _MAX_ROOT):match.start()]
                    root = head[head.rfind(b"\x00") + 1:]
                    if _ROOT.match(root):
                        roots[root] += 1
    except (OSError, ValueError):
        # ValueError: an empty file cannot be mapped.
        return None
    if not roots:
        return None
    # One stray match cannot outvote the module's real build path.
    return roots.most_common(1)[0][0]


def unix_module_dir(engine_root):
    """The engine's x86_64 Unix module directory, or None.

    Located through win32u, the one module every Wine build installs there.
    """
    if not engine_root:
        return None
    for relative in _UNIX_DIRS:
        candidate = Path(engine_root) / relative
        if (candidate / _WIN32U[0]).exists():
            return candidate
    return None


def engine_wayland_driver_problem(engine_root):
    """Actionable message when winewayland cannot be used, else None.

    Returns None whenever nothing can be determined, so a caller can treat any
    string as a reason not to ask Wine for a Wayland session.
    """
    modules = unix_module_dir(engine_root)
    if modules is None:
        return None
    if not (modules / _DRIVER[0]).exists():
        return (
            "This engine has no Wayland driver: winewayland.so was not built, "
            "so Wine has no way to open a native Wayland window. Run Install "
            "/ Update to fetch an engine that carries it."
        )
    driver = _build_root(modules / _DRIVER[0], _DRIVER[1])
    engine = _build_root(modules / _WIN32U[0], _WIN32U[1])
    if driver is None or engine is None or driver == engine:
        return None
    return (
        "This engine's Wayland driver is left over from a different Wine "
        "build than the engine itself: winewayland.so was compiled from %s "
        "while win32u.so, which it takes its Wine entry points from, was "
        "compiled from %s. Wine fails to load the driver on an undefined "
        "symbol, so it would never open a window. Run Install / Update to "
        "fetch an engine whose Wayland driver matches it."
        % (driver.decode("utf-8", "replace"), engine.decode("utf-8", "replace"))
    )


def wayland_driver_summary(engine_root):
    """One short status word for Doctor's aligned report."""
    modules = unix_module_dir(engine_root)
    if modules is None:
        return "unknown (no engine to read)"
    problem = engine_wayland_driver_problem(engine_root)
    if problem is None:
        return "OK (BOL_INPUT=wayland available)"
    if not (modules / _DRIVER[0]).exists():
        return "MISSING (engine built without winewayland)"
    return "MISSING (winewayland is from another Wine build)"
