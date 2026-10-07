"""Tests for bol.x11 monitor geometry and Steam window identity."""
# SPDX-License-Identifier: MIT

import subprocess
import sys
import types
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

from bol import x11


def result(stdout="", returncode=0):
    return SimpleNamespace(stdout=stdout, returncode=returncode)


DUAL_MONITOR_PRIMARY_SECOND = (
    "Monitors: 2\n"
    " 0: +HDMI-A-0 1920/530x1080/300+1920+0  HDMI-A-0\n"
    " 1: +*HDMI-A-1 1920/530x1080/300+0+0  HDMI-A-1\n"
)

SINGLE_MONITOR = (
    "Monitors: 1\n"
    " 0: +*eDP-1 1920/340x1080/190+0+0  eDP-1\n"
)

NO_PRIMARY_FLAGGED = (
    "Monitors: 2\n"
    " 0: +HDMI-A-0 1920/530x1080/300+0+0  HDMI-A-0\n"
    " 1: +HDMI-A-1 2560/597x1440/336+1920+0  HDMI-A-1\n"
)

BARE_XRANDR_COMBINED = (
    "Screen 0: minimum 320 x 200, current 3840 x 1080, maximum 16384 x "
    "16384\n"
)

CONNECTED_MIXED = (
    "DP-1 connected primary 2560x1440+0+0 (normal left inverted right x "
    "axis y axis)\n"
    "HDMI-1 connected 1366x768+2560+0 (normal left inverted right x axis "
    "y axis)\n"
)

MIXED_MONITORS = (
    "Monitors: 2\n"
    " 0: +*DP-1 2560/600x1440/340+0+0  DP-1\n"
    " 1: +HDMI-1 1366/310x768/170+2560+0  HDMI-1\n"
)

NEGATIVE_MONITOR = (
    "Monitors: 2\n"
    " 0: +DP-1 1920/530x1080/300-1920+0  DP-1\n"
    " 1: +*eDP-1 1920/340x1080/190+0+0  eDP-1\n"
)


# A 144 Hz primary next to a 60 Hz secondary, as `xrandr` with no arguments
# prints it: the driven mode carries '*', the preferred one '+'.
DUAL_REFRESH_RATES = (
    "DP-1 connected primary 1920x1080+0+0 (normal left inverted right x "
    "axis y axis) 600mm x 340mm\n"
    "   1920x1080    143.85*+ 120.00   60.00\n"
    "   1280x720      60.00\n"
    "HDMI-1 connected 1920x1080+1920+0 (normal left inverted right x axis "
    "y axis) 530mm x 300mm\n"
    "   1920x1080     60.00*+  50.00\n"
    "DP-2 disconnected (normal left inverted right x axis y axis)\n"
    "   1920x1080     59.94\n"
)

NO_MODE_DRIVEN = (
    "DP-1 disconnected (normal left inverted right x axis y axis)\n"
    "   1920x1080     60.00 +\n"
)


class XrandrRefreshCliTests(unittest.TestCase):
    """Exercises the xrandr CLI half of primary_output_refresh_hz."""

    def setUp(self):
        patcher = mock.patch.object(x11.shutil, "which", return_value="xrandr")
        patcher.start()
        self.addCleanup(patcher.stop)
        have_patcher = mock.patch.object(x11, "have", return_value=False)
        have_patcher.start()
        self.addCleanup(have_patcher.stop)

    def test_the_fastest_driven_mode_wins(self):
        def runner(args, **_kwargs):
            self.assertEqual(args, ["xrandr"])
            return result(DUAL_REFRESH_RATES)

        self.assertAlmostEqual(
            x11.primary_output_refresh_hz(runner=runner), 143.85)

    def test_a_mode_no_output_drives_is_not_a_refresh_rate(self):
        def runner(args, **_kwargs):
            return result(NO_MODE_DRIVEN)

        self.assertIsNone(x11.primary_output_refresh_hz(runner=runner))

    def test_xrandr_missing_returns_none(self):
        with mock.patch.object(x11.shutil, "which", return_value=None):
            self.assertIsNone(x11.primary_output_refresh_hz())

    def test_xrandr_failing_returns_none(self):
        def runner(args, **_kwargs):
            return result("", returncode=1)

        self.assertIsNone(x11.primary_output_refresh_hz(runner=runner))

    def test_xrandr_timing_out_returns_none(self):
        def runner(args, **_kwargs):
            raise subprocess.TimeoutExpired(args, 5)

        self.assertIsNone(x11.primary_output_refresh_hz(runner=runner))


class XrandrCliFallbackTests(unittest.TestCase):
    """Exercises _primary_via_xrandr_cli directly, and through
    primary_output_size() with the python-xlib path forced off (it isn't
    installed on the test host either way)."""

    def setUp(self):
        patcher = mock.patch.object(x11.shutil, "which", return_value="xrandr")
        patcher.start()
        self.addCleanup(patcher.stop)
        have_patcher = mock.patch.object(x11, "have", return_value=False)
        have_patcher.start()
        self.addCleanup(have_patcher.stop)

    def test_dual_monitor_uses_primary_not_combined_root(self):
        def runner(args, **_kwargs):
            self.assertIn("--listmonitors", args)
            return result(DUAL_MONITOR_PRIMARY_SECOND)

        self.assertEqual(x11.primary_output_size(runner=runner),
                          ("1920", "1080"))

    def test_single_monitor(self):
        def runner(args, **_kwargs):
            return result(SINGLE_MONITOR)

        self.assertEqual(x11.primary_output_size(runner=runner),
                          ("1920", "1080"))

    def test_no_primary_flagged_falls_back_to_first_listed_monitor(self):
        def runner(args, **_kwargs):
            return result(NO_PRIMARY_FLAGGED)

        self.assertEqual(x11.primary_output_size(runner=runner),
                          ("1920", "1080"))

    def test_monitor_geometries_include_mixed_and_negative_positions(self):
        self.assertEqual(
            x11.monitor_geometries(
                runner=lambda _args, **_kwargs: result(MIXED_MONITORS)),
            ((0, 0, 2560, 1440), (2560, 0, 1366, 768)),
        )
        self.assertEqual(
            x11.monitor_geometries(
                runner=lambda _args, **_kwargs: result(NEGATIVE_MONITOR)),
            ((-1920, 0, 1920, 1080), (0, 0, 1920, 1080)),
        )

    def test_monitor_geometries_fall_back_when_active_option_is_unsupported(self):
        def runner(args, **_kwargs):
            if "--listactivemonitors" in args:
                return result(returncode=1)
            return result(MIXED_MONITORS)

        self.assertEqual(
            x11.monitor_geometries(runner=runner),
            ((0, 0, 2560, 1440), (2560, 0, 1366, 768)),
        )

    def test_monitor_geometries_fall_back_to_connected_outputs(self):
        def runner(args, **_kwargs):
            if args[-1].startswith("--list"):
                return result(returncode=1)
            return result(CONNECTED_MIXED)

        self.assertEqual(
            x11.monitor_geometries(runner=runner),
            ((0, 0, 2560, 1440), (2560, 0, 1366, 768)),
        )

    def test_combined_root_is_not_reported_as_a_physical_monitor(self):
        def runner(args, **_kwargs):
            if args[-1].startswith("--list"):
                return result(returncode=1)
            return result(BARE_XRANDR_COMBINED)

        self.assertEqual(x11.monitor_geometries(runner=runner), ())

    def test_listmonitors_unsupported_falls_back_to_combined_root(self):
        def runner(args, **_kwargs):
            if "--listmonitors" in args:
                return result(returncode=1)
            return result(BARE_XRANDR_COMBINED)

        self.assertEqual(x11.primary_output_size(runner=runner),
                          ("3840", "1080"))

    def test_xrandr_missing_returns_none(self):
        with mock.patch.object(x11.shutil, "which", return_value=None):
            self.assertIsNone(x11.primary_output_size())

    def test_xrandr_timeout_returns_none(self):
        def runner(args, **_kwargs):
            raise subprocess.TimeoutExpired("xrandr", 5)

        self.assertIsNone(x11.primary_output_size(runner=runner))

    def test_unparseable_output_returns_none(self):
        def runner(args, **_kwargs):
            return result("nothing useful here\n")

        self.assertIsNone(x11.primary_output_size(runner=runner))


class FakeMonitor:
    def __init__(self, primary, width, height, x=0, y=0):
        self.primary = primary
        self.width_in_pixels = width
        self.height_in_pixels = height
        self.x = x
        self.y = y


class FakeMonitorsReply:
    def __init__(self, monitors):
        self.monitors = monitors


class FakeRoot:
    def __init__(self, monitors=None, supports_get_monitors=True,
                 get_monitors_raises=None):
        if supports_get_monitors:
            self._monitors = monitors or []
            self._get_monitors_raises = get_monitors_raises
            self.xrandr_get_monitors = self._get_monitors

    def _get_monitors(self, is_active=True):
        if self._get_monitors_raises is not None:
            raise self._get_monitors_raises
        return FakeMonitorsReply(self._monitors)


class FakeScreen:
    def __init__(self, root):
        self.root = root


class FakeDisplay:
    fail_to_connect = None
    close_raises = None
    root = None

    def __init__(self):
        if type(self).fail_to_connect is not None:
            raise type(self).fail_to_connect
        self.closed = False

    def screen(self):
        return FakeScreen(type(self).root)

    def close(self):
        self.closed = True
        if type(self).close_raises is not None:
            raise type(self).close_raises


class XlibDisplayError(Exception):
    pass


class XlibXError(Exception):
    pass


class XlibConnectionClosedError(Exception):
    pass


class XlibPathTests(unittest.TestCase):
    """Exercises _primary_via_xlib by injecting fake Xlib.display/Xlib.error
    modules. python-xlib is not installed on the test host, so this is the
    only way to cover the primary code path without a real X server."""

    def setUp(self):
        FakeDisplay.fail_to_connect = None
        FakeDisplay.close_raises = None
        FakeDisplay.root = None

        fake_xlib = types.ModuleType("Xlib")
        fake_display_mod = types.ModuleType("Xlib.display")
        fake_error_mod = types.ModuleType("Xlib.error")
        fake_display_mod.Display = FakeDisplay
        fake_error_mod.DisplayError = XlibDisplayError
        fake_error_mod.XError = XlibXError
        fake_error_mod.ConnectionClosedError = XlibConnectionClosedError
        fake_xlib.display = fake_display_mod
        fake_xlib.error = fake_error_mod

        modules_patcher = mock.patch.dict(sys.modules, {
            "Xlib": fake_xlib,
            "Xlib.display": fake_display_mod,
            "Xlib.error": fake_error_mod,
        })
        modules_patcher.start()
        self.addCleanup(modules_patcher.stop)

        have_patcher = mock.patch.object(x11, "have", return_value=True)
        have_patcher.start()
        self.addCleanup(have_patcher.stop)

    def test_primary_flagged_monitor_wins_over_first(self):
        FakeDisplay.root = FakeRoot(monitors=[
            FakeMonitor(False, "2560", "1440"),
            FakeMonitor(True, "1920", "1080"),
        ])
        self.assertEqual(x11._primary_via_xlib(), ("1920", "1080"))

    def test_no_primary_flagged_falls_back_to_first_monitor(self):
        FakeDisplay.root = FakeRoot(monitors=[
            FakeMonitor(False, "1920", "1080"),
            FakeMonitor(False, "2560", "1440"),
        ])
        self.assertEqual(x11._primary_via_xlib(), ("1920", "1080"))

    def test_monitor_geometries_preserve_xlib_positions(self):
        FakeDisplay.root = FakeRoot(monitors=[
            FakeMonitor(True, "2560", "1440", 0, 0),
            FakeMonitor(False, "1366", "768", 2560, 0),
        ])
        self.assertEqual(
            x11.monitor_geometries(),
            ((0, 0, 2560, 1440), (2560, 0, 1366, 768)),
        )

    def test_empty_monitor_list_returns_none(self):
        FakeDisplay.root = FakeRoot(monitors=[])
        self.assertIsNone(x11._primary_via_xlib())

    def test_pre_randr_1_5_server_returns_none(self):
        FakeDisplay.root = FakeRoot(supports_get_monitors=False)
        self.assertIsNone(x11._primary_via_xlib())

    def test_connection_failure_returns_none(self):
        FakeDisplay.fail_to_connect = XlibDisplayError("no display")
        self.assertIsNone(x11._primary_via_xlib())

    def test_connection_dropped_mid_query_returns_none(self):
        # Issue: a ConnectionClosedError (dropped X connection, e.g. SSH
        # X-forwarding cut or the X server restarting mid-request) is
        # neither an XError nor an OSError; it must still be caught rather
        # than crash the caller.
        FakeDisplay.root = FakeRoot(
            get_monitors_raises=XlibConnectionClosedError("gone"))
        self.assertIsNone(x11._primary_via_xlib())

    def test_close_raising_does_not_mask_a_successful_result(self):
        FakeDisplay.root = FakeRoot(monitors=[FakeMonitor(True, "1920", "1080")])
        FakeDisplay.close_raises = XlibConnectionClosedError("gone on close")
        self.assertEqual(x11._primary_via_xlib(), ("1920", "1080"))

    def test_display_is_closed_after_use(self):
        FakeDisplay.root = FakeRoot(monitors=[FakeMonitor(True, "1920", "1080")])
        seen = {}

        real_init = FakeDisplay.__init__

        def tracking_init(self):
            real_init(self)
            seen["instance"] = self

        with mock.patch.object(FakeDisplay, "__init__", tracking_init):
            x11._primary_via_xlib()
        self.assertTrue(seen["instance"].closed)

    def test_xlib_path_wins_over_cli_fallback(self):
        FakeDisplay.root = FakeRoot(monitors=[FakeMonitor(True, "1920", "1080")])

        def must_not_run(*_args, **_kwargs):
            raise AssertionError("CLI fallback must not run when Xlib succeeds")

        with mock.patch.object(x11.shutil, "which", return_value="xrandr"):
            self.assertEqual(
                x11.primary_output_size(runner=must_not_run),
                ("1920", "1080"))


class FakeMode:
    def __init__(self, mode_id, dot_clock, h_total, v_total):
        self.id = mode_id
        self.dot_clock = dot_clock
        self.h_total = h_total
        self.v_total = v_total


class FakeCrtcInfo:
    def __init__(self, mode):
        self.mode = mode


class FakeResources:
    config_timestamp = 42

    def __init__(self, modes, crtcs):
        self.modes = modes
        self.crtcs = crtcs


class FakeRandrRoot:
    def __init__(self, resources):
        self._resources = resources

    def xrandr_get_screen_resources_current(self):
        return self._resources


class FakeRandrDisplay(FakeDisplay):
    """A display whose CRTCs drive the modes the test declares."""

    crtc_modes = {}

    def xrandr_get_crtc_info(self, crtc, config_timestamp):
        assert config_timestamp == FakeResources.config_timestamp
        return FakeCrtcInfo(type(self).crtc_modes.get(crtc, 0))


# 1920x1080: over a 2000x1111 total, 319.6347 MHz is 143.85 Hz and 133.32 MHz
# is 60.00 Hz. Both are the pixel-clock-over-totals shape RandR reports.
FAST_MODE = FakeMode(1, 319_634_700, 2000, 1111)
SLOW_MODE = FakeMode(2, 133_320_000, 2000, 1111)


class XlibRefreshTests(unittest.TestCase):
    """Exercises _refresh_via_xlib with the same injected-module technique."""

    def setUp(self):
        FakeRandrDisplay.fail_to_connect = None
        FakeRandrDisplay.close_raises = None
        FakeRandrDisplay.root = None
        FakeRandrDisplay.crtc_modes = {}

        fake_xlib = types.ModuleType("Xlib")
        fake_display_mod = types.ModuleType("Xlib.display")
        fake_error_mod = types.ModuleType("Xlib.error")
        fake_display_mod.Display = FakeRandrDisplay
        fake_error_mod.DisplayError = XlibDisplayError
        fake_error_mod.XError = XlibXError
        fake_error_mod.ConnectionClosedError = XlibConnectionClosedError
        fake_xlib.display = fake_display_mod
        fake_xlib.error = fake_error_mod

        modules_patcher = mock.patch.dict(sys.modules, {
            "Xlib": fake_xlib,
            "Xlib.display": fake_display_mod,
            "Xlib.error": fake_error_mod,
        })
        modules_patcher.start()
        self.addCleanup(modules_patcher.stop)

        have_patcher = mock.patch.object(x11, "have", return_value=True)
        have_patcher.start()
        self.addCleanup(have_patcher.stop)

    def _resources(self, modes, crtcs):
        FakeRandrDisplay.root = FakeRandrRoot(FakeResources(modes, crtcs))

    def test_the_fastest_active_crtc_wins(self):
        self._resources([FAST_MODE, SLOW_MODE], [10, 11])
        FakeRandrDisplay.crtc_modes = {10: 2, 11: 1}
        self.assertAlmostEqual(x11._refresh_via_xlib(), 143.85, places=2)

    def test_a_crtc_driving_no_mode_is_skipped(self):
        self._resources([SLOW_MODE], [10, 11])
        FakeRandrDisplay.crtc_modes = {10: 0, 11: 2}
        self.assertAlmostEqual(x11._refresh_via_xlib(), 60.0, places=2)

    def test_no_active_crtc_returns_none(self):
        self._resources([FAST_MODE], [10])
        FakeRandrDisplay.crtc_modes = {10: 0}
        self.assertIsNone(x11._refresh_via_xlib())

    def test_a_mode_with_no_total_cannot_divide(self):
        self._resources([FakeMode(1, 319_500_000, 0, 0)], [10])
        FakeRandrDisplay.crtc_modes = {10: 1}
        self.assertIsNone(x11._refresh_via_xlib())

    def test_connection_failure_returns_none(self):
        FakeRandrDisplay.fail_to_connect = XlibDisplayError("no display")
        self.assertIsNone(x11._refresh_via_xlib())

    def test_a_server_without_randr_returns_none(self):
        FakeRandrDisplay.root = object()
        self.assertIsNone(x11._refresh_via_xlib())

    def test_display_is_closed_after_use(self):
        self._resources([FAST_MODE], [10])
        FakeRandrDisplay.crtc_modes = {10: 1}
        seen = {}
        real_init = FakeRandrDisplay.__init__

        def tracking_init(self):
            real_init(self)
            seen["instance"] = self

        with mock.patch.object(FakeRandrDisplay, "__init__", tracking_init):
            x11._refresh_via_xlib()
        self.assertTrue(seen["instance"].closed)

    def test_xlib_path_wins_over_cli_fallback(self):
        self._resources([FAST_MODE], [10])
        FakeRandrDisplay.crtc_modes = {10: 1}

        def must_not_run(*_args, **_kwargs):
            raise AssertionError("CLI fallback must not run when Xlib succeeds")

        with mock.patch.object(x11.shutil, "which", return_value="xrandr"):
            self.assertAlmostEqual(
                x11.primary_output_refresh_hz(runner=must_not_run),
                143.85, places=2)


class FakeWindows:
    """A window tree, as bol.x11's tagger walks one.

    `tree` maps a window to its children; `classes` maps a window to its
    WM_CLASS names. Windows missing from `classes` have none, which is what
    every non-toplevel X window looks like. Windows in `hidden` are mapped
    nowhere a compositor could present them.
    """

    def __init__(self, tree, classes, root=1, hidden=()):
        self.tree = tree
        self.classes = classes
        self._root = root
        self.hidden = set(hidden)
        self.properties = {}
        self.flushes = 0
        self.visited = []

    def root(self):
        return self._root

    def children(self, window):
        self.visited.append(window)
        return tuple(self.tree.get(window, ()))

    def wm_classes(self, window):
        return tuple(self.classes.get(window, ()))

    def is_presentable(self, window):
        return window not in self.hidden

    def set_cardinal(self, window, name, value):
        self.properties[(window, name)] = value
        return True

    def iconify(self, window):
        self.iconified = getattr(self, "iconified", []) + [window]
        return True

    def flush(self):
        self.flushes += 1


GAME_CLASS = "minecraft.windows.exe"
APP_ID = "2716672805"


class SteamGameWindowTagTests(unittest.TestCase):
    """Issue #199: gamescope focuses a window by its STEAM_GAME property, and
    neither this engine's Wine nor the game's process tree supplies one inside
    the Flatpak, so the launcher stamps it on the window itself."""

    def _tag(self, windows, skip=()):
        return x11.tag_steam_game_windows(
            APP_ID, GAME_CLASS, windows=windows, skip=skip)

    def test_a_toplevel_of_the_game_is_tagged_with_the_application_id(self):
        windows = FakeWindows({1: (2, 3)}, {3: (GAME_CLASS, GAME_CLASS)})
        self.assertEqual(self._tag(windows), (3,))
        self.assertEqual(windows.properties,
                         {(3, "STEAM_GAME"): 2716672805})
        self.assertEqual(windows.flushes, 1)

    def test_a_reparented_toplevel_is_found_inside_its_frame(self):
        # A desktop window manager puts the client window inside a frame of
        # its own, which carries no WM_CLASS; a plain compositing manager such
        # as gamescope's leaves toplevels as children of the root.
        windows = FakeWindows({1: (2,), 2: (5,)}, {5: (GAME_CLASS,)})
        self.assertEqual(self._tag(windows), (5,))

    def test_the_class_match_is_case_insensitive(self):
        windows = FakeWindows({1: (2,)}, {2: ("Minecraft.Windows.exe",)})
        self.assertEqual(
            x11.tag_steam_game_windows(
                APP_ID, "MINECRAFT.WINDOWS.EXE", windows=windows), (2,))

    def test_every_matching_toplevel_is_tagged(self):
        windows = FakeWindows(
            {1: (2, 3)}, {2: (GAME_CLASS,), 3: (GAME_CLASS,)})
        self.assertEqual(set(self._tag(windows)), {2, 3})
        self.assertEqual(len(windows.properties), 2)

    def test_a_window_already_tagged_is_watched_but_not_written_again(self):
        # Every write costs gamescope a focus recomputation, so a window that
        # already carries the identity must be left completely alone.
        windows = FakeWindows({1: (2,)}, {2: (GAME_CLASS,)})
        self.assertEqual(self._tag(windows, skip={2}), ())
        self.assertEqual(windows.properties, {})
        self.assertEqual(windows.flushes, 0)

    def test_a_window_the_game_opens_later_is_tagged_in_a_later_pass(self):
        # Wine builds a new X window when the game changes window kind, and
        # the replacement starts out with no identity of its own.
        windows = FakeWindows({1: (2,)}, {2: (GAME_CLASS,)})
        self.assertEqual(self._tag(windows), (2,))
        windows.tree[1] = (7,)
        windows.classes = {7: (GAME_CLASS,)}
        self.assertEqual(self._tag(windows, skip={2}), (7,))

    def test_the_game_own_child_windows_are_left_alone(self):
        # WM_CLASS belongs to a toplevel, so the client-area children Wine
        # creates inside the game window must not be walked into and tagged.
        windows = FakeWindows({1: (2,), 2: (3, 4)}, {2: (GAME_CLASS,)})
        self.assertEqual(self._tag(windows), (2,))
        self.assertNotIn(2, windows.visited)

    def test_another_application_window_is_not_looked_inside(self):
        windows = FakeWindows({1: (2,), 2: (3,)},
                              {2: ("steamwebhelper",), 3: (GAME_CLASS,)})
        self.assertEqual(self._tag(windows), ())
        self.assertNotIn(2, windows.visited)

    def test_wine_own_helper_windows_never_become_focus_candidates(self):
        # Wine gives its 1x1 override-redirect default-IME and message windows
        # the game's own class; giving those an identity would let gamescope
        # present one of them instead of the game.
        windows = FakeWindows(
            {1: (2, 3, 4)},
            {2: (GAME_CLASS,), 3: (GAME_CLASS,), 4: (GAME_CLASS,)},
            hidden={3, 4})
        self.assertEqual(self._tag(windows), (2,))
        self.assertEqual(list(windows.properties), [(2, "STEAM_GAME")])

    def test_a_window_not_mapped_yet_is_tagged_on_a_later_pass(self):
        windows = FakeWindows({1: (2,)}, {2: (GAME_CLASS,)}, hidden={2})
        self.assertEqual(self._tag(windows), ())
        windows.hidden.clear()
        self.assertEqual(self._tag(windows), (2,))

    def test_no_game_window_yet_tags_nothing_and_flushes_nothing(self):
        windows = FakeWindows({1: (2,)}, {2: ("steamwebhelper",)})
        self.assertEqual(self._tag(windows), ())
        self.assertEqual(windows.properties, {})
        self.assertEqual(windows.flushes, 0)

    def test_the_root_window_itself_is_never_tagged(self):
        windows = FakeWindows({1: ()}, {1: (GAME_CLASS,)})
        self.assertEqual(self._tag(windows), ())
        self.assertEqual(windows.properties, {})

    def test_a_deep_tree_is_not_walked_forever(self):
        depth = x11._WINDOW_SEARCH_DEPTH
        tree = {index: (index + 1,) for index in range(1, depth + 40)}
        windows = FakeWindows(tree, {depth + 20: (GAME_CLASS,)})
        self.assertEqual(self._tag(windows), ())
        self.assertLessEqual(len(windows.visited), depth + 1)

    def test_a_wide_tree_stays_within_its_budget(self):
        windows = FakeWindows({1: tuple(range(2, 4000))}, {})
        self.assertEqual(self._tag(windows), ())
        self.assertLessEqual(len(windows.visited),
                             x11._WINDOW_SEARCH_BUDGET)

    def test_a_value_that_is_not_a_32_bit_application_id_is_refused(self):
        for app_id in ("default", "", None, "0", "-1", str(2 ** 32),
                       "11668020851441139712"):
            with self.subTest(app_id=app_id):
                windows = FakeWindows({1: (2,)}, {2: (GAME_CLASS,)})
                self.assertEqual(
                    x11.tag_steam_game_windows(
                        app_id, GAME_CLASS, windows=windows), ())
                self.assertEqual(windows.properties, {})

    def test_an_empty_window_class_is_refused(self):
        windows = FakeWindows({1: (2,)}, {2: (GAME_CLASS,)})
        self.assertEqual(
            x11.tag_steam_game_windows(APP_ID, "  ", windows=windows), ())
        self.assertEqual(windows.properties, {})

    def test_no_display_never_opens_one(self):
        def must_not_open(_display):
            raise AssertionError("no display must not be opened")

        with mock.patch.object(x11, "_x_windows", must_not_open):
            self.assertEqual(
                x11.tag_steam_game_windows(
                    APP_ID, GAME_CLASS, display=" "), ())

    def test_an_unusable_display_tags_nothing(self):
        @contextmanager
        def no_display(_display):
            yield None

        with mock.patch.object(x11, "_x_windows", no_display):
            self.assertEqual(
                x11.tag_steam_game_windows(APP_ID, GAME_CLASS, display=":1"),
                ())

    def test_the_display_comes_from_the_environment_by_default(self):
        seen = []

        @contextmanager
        def record(display):
            seen.append(display)
            yield None

        with mock.patch.object(x11, "_x_windows", record), \
                mock.patch.dict(x11.os.environ, {"DISPLAY": ":1"}, clear=True):
            x11.tag_steam_game_windows(APP_ID, GAME_CLASS)
        self.assertEqual(seen, [":1"])

    def test_libx11_is_never_loaded_when_the_caller_supplies_the_windows(self):
        def must_not_load():
            raise AssertionError("libX11 must not be loaded")

        windows = FakeWindows({1: (2,)}, {2: (GAME_CLASS,)})
        with mock.patch.object(x11, "_load_xlib", must_not_load):
            self.assertEqual(self._tag(windows), (2,))


class XlibLoadTests(unittest.TestCase):
    def test_a_missing_libx11_is_not_an_error(self):
        with mock.patch.object(x11.ctypes.cdll, "LoadLibrary",
                               side_effect=OSError("no libX11")):
            self.assertIsNone(x11._load_xlib())

    def test_a_libx11_without_the_needed_entry_points_is_not_an_error(self):
        class Stub:
            def __getattr__(self, name):
                raise AttributeError(name)

        with mock.patch.object(x11.ctypes.cdll, "LoadLibrary",
                               return_value=Stub()):
            self.assertIsNone(x11._load_xlib())

    def test_the_error_handler_swallows_errors_instead_of_exiting(self):
        # Xlib's default handler calls exit(); windows belonging to another
        # client can disappear mid-walk, which must never take us down.
        self.assertEqual(x11._ignore_x_error(None, None), 0)


class IconifyTests(unittest.TestCase):
    """The Wine virtual desktop is minimized with the game inside it (#189)."""

    def test_the_desktop_inside_its_frame_is_minimized(self):
        windows = FakeWindows({1: (2, 3), 2: (5,)},
                              {5: ("explorer.exe", "Explorer.exe"),
                               3: (GAME_CLASS,)})
        self.assertEqual(x11.iconify_windows("explorer.exe", windows=windows),
                         1)
        self.assertEqual(windows.iconified, [5])
        self.assertEqual(windows.flushes, 1)

    def test_a_window_already_off_screen_is_left_alone(self):
        windows = FakeWindows({1: (5,)}, {5: ("explorer.exe",)}, hidden=(5,))
        self.assertEqual(x11.iconify_windows("explorer.exe", windows=windows),
                         0)
        self.assertFalse(hasattr(windows, "iconified"))

    def test_no_display_is_nothing_to_minimize(self):
        with mock.patch.dict(x11.os.environ, {"DISPLAY": ""}):
            self.assertEqual(x11.iconify_windows("explorer.exe"), 0)
        self.assertEqual(x11.iconify_windows(""), 0)


if __name__ == "__main__":
    unittest.main()
