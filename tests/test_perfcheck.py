"""Name the causes of "the game lags" that live outside the engine.

Exhausted memory, a full data directory, windowed vsync on a compositing
desktop and a render distance past what Bedrock's main thread can feed all
cost frame rate while leaving nothing in a Wine, Proton or vkd3d log. They
are therefore reported as engine or GPU faults. These tests hold the
detection honest in both directions: it must fire on the starved machine and
stay quiet on the healthy one, since an advisory users learn to ignore is
worth nothing when it finally matters.
"""
# SPDX-License-Identifier: MIT

import os
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

from bol import launch, perfcheck


_Usage = namedtuple("_Usage", "total used free")

_OPTIONS_RELATIVE = (
    "drive_c/users/steamuser/AppData/Roaming/Minecraft Bedrock/Users/"
    "15576315838024289709/games/com.mojang/minecraftpe/options.txt"
)


def _meminfo(root, available_mib, swap_total_mib=0, swap_free_mib=0):
    """A fake /proc/meminfo carrying only the fields that are read."""
    path = Path(root) / "meminfo"
    path.write_text(
        "MemTotal:       %8d kB\n"
        "MemFree:         %8d kB\n"
        "MemAvailable:   %8d kB\n"
        "SwapTotal:      %8d kB\n"
        "SwapFree:       %8d kB\n"
        % (16 * 1024 * 1024, 256 * 1024, available_mib * 1024,
           swap_total_mib * 1024, swap_free_mib * 1024))
    return path


def _prefix(root, relative=_OPTIONS_RELATIVE, **options):
    """A prefix tree holding one options.txt with the given settings."""
    path = Path(root) / "pfx" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join("%s:%s\n" % item for item in options.items())
    # Real files carry hundreds of unrelated keys; keep one so the parser is
    # never exercised on a file containing only what it looks for.
    path.write_text("keyboard_type_0_key.screenshot:113\n" + body)
    return Path(root) / "pfx"


class MemoryPressureTests(unittest.TestCase):
    def test_starved_host_is_reported_with_the_number(self):
        with tempfile.TemporaryDirectory() as td:
            problem = perfcheck.low_memory_problem(_meminfo(td, 900))
        self.assertIsNotNone(problem)
        self.assertIn("900 MiB", problem)

    def test_healthy_host_stays_quiet(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(perfcheck.low_memory_problem(_meminfo(td, 9000)))

    def test_swap_already_in_use_is_named_as_where_the_game_goes(self):
        with tempfile.TemporaryDirectory() as td:
            meminfo = _meminfo(td, 500, swap_total_mib=16384,
                               swap_free_mib=16384 - 4700)
            problem = perfcheck.low_memory_problem(meminfo)
        self.assertIn("4.6 GiB is already in swap", problem)

    def test_reclaimable_cache_counts_as_available(self):
        # MemFree is deliberately tiny in the fixture: reading it instead of
        # MemAvailable would fire on every healthy machine.
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(
                perfcheck.available_memory_mib(_meminfo(td, 7000)), 7000)

    def test_unreadable_meminfo_is_unknown_not_a_defect(self):
        self.assertIsNone(perfcheck.available_memory_mib("/nonexistent/mem"))
        self.assertIsNone(perfcheck.low_memory_problem("/nonexistent/mem"))

    def test_swap_free_of_a_swapless_host_is_zero_not_none(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(perfcheck.swap_used_mib(_meminfo(td, 8000)), 0)

    def test_total_memory_is_the_machines_not_what_is_left(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(
                perfcheck.total_memory_mib(_meminfo(td, 900)), 16 * 1024)
        self.assertIsNone(perfcheck.total_memory_mib("/nonexistent/mem"))


class DiskSpaceTests(unittest.TestCase):
    def _usage(self, free_mib, total_mib=246 * 1024):
        return _Usage(total=total_mib << 20,
                      used=(total_mib - free_mib) << 20,
                      free=free_mib << 20)

    def test_nearly_full_volume_is_reported_with_the_percentage(self):
        with mock.patch.object(perfcheck.shutil, "disk_usage",
                               return_value=self._usage(2048)):
            problem = perfcheck.free_disk_problem("/data")
        self.assertIsNotNone(problem)
        self.assertIn("2048 MiB", problem)
        self.assertIn("99% full", problem)

    def test_roomy_volume_stays_quiet(self):
        with mock.patch.object(perfcheck.shutil, "disk_usage",
                               return_value=self._usage(80 * 1024)):
            self.assertIsNone(perfcheck.free_disk_problem("/data"))

    def test_the_shader_cache_is_named_as_the_cost(self):
        with mock.patch.object(perfcheck.shutil, "disk_usage",
                               return_value=self._usage(100)):
            problem = perfcheck.free_disk_problem("/data")
        self.assertIn("shader cache", problem)

    def test_unreadable_path_is_unknown_not_a_defect(self):
        with mock.patch.object(perfcheck.shutil, "disk_usage",
                               side_effect=OSError):
            self.assertIsNone(perfcheck.free_disk_problem("/data"))
        self.assertIsNone(perfcheck.free_disk_problem(None))


class GameOptionsTests(unittest.TestCase):
    def test_options_are_found_in_the_roaming_layout(self):
        with tempfile.TemporaryDirectory() as td:
            prefix = _prefix(td, gfx_viewdistance="256")
            found = perfcheck.find_options_file(prefix)
            self.assertIsNotNone(found)
            self.assertEqual(
                perfcheck.read_game_options(found)["gfx_viewdistance"], "256")

    def test_options_are_found_in_the_uwp_package_layout(self):
        relative = (
            "drive_c/users/steamuser/AppData/Local/Packages/"
            "Microsoft.MinecraftUWP_8wekyb3d8bbwe/LocalState/games/"
            "com.mojang/minecraftpe/options.txt")
        with tempfile.TemporaryDirectory() as td:
            prefix = _prefix(td, relative=relative, gfx_vsync="1")
            found = perfcheck.find_options_file(prefix)
            self.assertIsNotNone(found)
            self.assertEqual(
                perfcheck.read_game_options(found)["gfx_vsync"], "1")

    def test_a_prefix_before_the_first_launch_is_silent(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(perfcheck.find_options_file(Path(td)))
            self.assertEqual(perfcheck.read_game_options(None), {})
            self.assertEqual(perfcheck.performance_problems(
                Path(td), None, environ={}, meminfo_path="/nonexistent"), [])

    def test_values_holding_a_colon_keep_it(self):
        with tempfile.TemporaryDirectory() as td:
            prefix = _prefix(td)
            path = perfcheck.find_options_file(prefix)
            path.write_text("keyboard_type_1_key.lookUp:38:2\ngfx_vsync:1\n")
            options = perfcheck.read_game_options(path)
        self.assertEqual(options["keyboard_type_1_key.lookUp"], "38:2")
        self.assertEqual(options["gfx_vsync"], "1")

    def test_malformed_values_are_ignored_rather_than_raising(self):
        options = {"gfx_viewdistance": "", "gfx_vsync": "yes"}
        self.assertIsNone(perfcheck.render_distance_chunks(options))
        self.assertIsNone(perfcheck.render_distance_problem(options))
        self.assertIsNone(perfcheck.windowed_vsync_problem(
            options, {"XDG_CURRENT_DESKTOP": "X-Cinnamon"}))


class RenderDistanceTests(unittest.TestCase):
    def test_blocks_are_converted_to_the_chunks_the_slider_shows(self):
        self.assertEqual(
            perfcheck.render_distance_chunks({"gfx_viewdistance": "640"}), 40)

    def test_extreme_distance_is_reported_in_chunks(self):
        problem = perfcheck.render_distance_problem(
            {"gfx_viewdistance": "640"})
        self.assertIsNotNone(problem)
        self.assertIn("40 chunks", problem)
        self.assertIn("main thread", problem)

    def test_ordinary_distance_stays_quiet(self):
        for blocks in ("128", "256", "384"):
            self.assertIsNone(perfcheck.render_distance_problem(
                {"gfx_viewdistance": blocks}), blocks)

    def test_the_threshold_itself_is_not_flagged(self):
        blocks = str(perfcheck.EXTREME_RENDER_CHUNKS * perfcheck.CHUNK_BLOCKS)
        self.assertIsNone(
            perfcheck.render_distance_problem({"gfx_viewdistance": blocks}))

    def test_absent_setting_is_silent(self):
        self.assertIsNone(perfcheck.render_distance_problem({}))


class WindowedVsyncTests(unittest.TestCase):
    WINDOWED_VSYNC = {"gfx_vsync": "1", "gfx_fullscreen": "0"}

    def test_compositing_desktop_with_windowed_vsync_is_reported(self):
        problem = perfcheck.windowed_vsync_problem(
            self.WINDOWED_VSYNC, {"XDG_CURRENT_DESKTOP": "X-Cinnamon"})
        self.assertIsNotNone(problem)
        self.assertIn("vsync", problem)

    def test_fullscreen_is_never_flagged(self):
        self.assertIsNone(perfcheck.windowed_vsync_problem(
            {"gfx_vsync": "1", "gfx_fullscreen": "1"},
            {"XDG_CURRENT_DESKTOP": "GNOME"}))

    def test_windowed_without_vsync_is_never_flagged(self):
        self.assertIsNone(perfcheck.windowed_vsync_problem(
            {"gfx_vsync": "0", "gfx_fullscreen": "0"},
            {"XDG_CURRENT_DESKTOP": "GNOME"}))

    def test_wayland_always_composites(self):
        self.assertTrue(perfcheck.session_is_composited(
            {"XDG_SESSION_TYPE": "wayland"}))
        self.assertTrue(perfcheck.session_is_composited(
            {"WAYLAND_DISPLAY": "wayland-0"}))

    def test_desktops_whose_compositor_is_optional_stay_unknown(self):
        # XFCE can run with or without compositing, and guessing wrong would
        # nag a user whose setup is fine.
        for desktop in ("XFCE", "MATE", "LXQt", ""):
            self.assertIsNone(perfcheck.session_is_composited(
                {"XDG_SESSION_TYPE": "x11", "XDG_CURRENT_DESKTOP": desktop}),
                desktop)
            self.assertIsNone(perfcheck.windowed_vsync_problem(
                self.WINDOWED_VSYNC,
                {"XDG_SESSION_TYPE": "x11",
                 "XDG_CURRENT_DESKTOP": desktop}), desktop)

    def test_desktop_name_is_matched_case_insensitively(self):
        self.assertTrue(perfcheck.session_is_composited(
            {"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}))


class AggregateReportTests(unittest.TestCase):
    def test_every_condition_is_reported_at_once(self):
        with tempfile.TemporaryDirectory() as td:
            prefix = _prefix(td, gfx_viewdistance="640", gfx_vsync="1",
                             gfx_fullscreen="0")
            meminfo = _meminfo(td, 500)
            environ = {"XDG_CURRENT_DESKTOP": "X-Cinnamon"}
            with mock.patch.object(
                    perfcheck.shutil, "disk_usage",
                    return_value=_Usage(total=1 << 40, used=1 << 40, free=0)):
                problems = perfcheck.performance_problems(
                    prefix, "/data", environ=environ, meminfo_path=meminfo)
                summary = perfcheck.performance_summary(
                    prefix, "/data", environ, meminfo)
        self.assertEqual(len(problems), 4)
        self.assertEqual(summary, "4 notices")

    def test_memory_is_reported_before_the_render_distance(self):
        # Ordered by what it costs: no amount of lowering the render distance
        # rescues a host that is paging the game out.
        with tempfile.TemporaryDirectory() as td:
            prefix = _prefix(td, gfx_viewdistance="640")
            problems = perfcheck.performance_problems(
                prefix, None, environ={}, meminfo_path=_meminfo(td, 500))
        self.assertIn("memory is available", problems[0])
        self.assertIn("render distance", problems[1])

    def test_healthy_machine_reports_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            prefix = _prefix(td, gfx_viewdistance="256", gfx_vsync="1",
                             gfx_fullscreen="1")
            meminfo = _meminfo(td, 9000)
            with mock.patch.object(
                    perfcheck.shutil, "disk_usage",
                    return_value=_Usage(total=1 << 40, used=0,
                                        free=200 << 30)):
                environ = {"XDG_CURRENT_DESKTOP": "X-Cinnamon"}
                self.assertEqual(perfcheck.performance_problems(
                    prefix, "/data", environ=environ,
                    meminfo_path=meminfo), [])
                self.assertEqual(perfcheck.performance_summary(
                    prefix, "/data", environ, meminfo), "OK (nothing found)")

    def test_an_unreachable_prefix_or_volume_never_raises_at_launch(self):
        # This runs on the launch path before the game starts; a missing
        # prefix or an unreadable mount must cost a silent report, not a
        # traceback in front of a player trying to press PLAY.
        with mock.patch.object(perfcheck.shutil, "disk_usage",
                               side_effect=OSError):
            self.assertEqual(perfcheck.performance_problems(
                Path("/nonexistent/pfx"), "/nonexistent/data", environ={},
                meminfo_path="/nonexistent/meminfo"), [])

    def test_one_condition_is_summarised_in_the_singular(self):
        with tempfile.TemporaryDirectory() as td:
            prefix = _prefix(td, gfx_viewdistance="640")
            summary = perfcheck.performance_summary(
                prefix, None, {}, _meminfo(td, 9000))
        self.assertEqual(summary, "1 notice")


class UnlimitedFrameRateTests(unittest.TestCase):
    """Nothing pacing the render loop is what pins the GPU in the menu (#150).

    Measured on an RTX 4060: with vsync off and Max Framerate on Unlimited the
    main menu runs at ~1800 FPS for 58-72% of the card; with Max Framerate at
    60 the same menu is 60.0 FPS. The detection has to separate that case from
    every settings file where the player did choose a limit.
    """

    def test_vsync_off_and_slider_unlimited_is_unpaced(self):
        self.assertTrue(perfcheck.frame_rate_is_unlimited(
            {"gfx_vsync": "0", "gfx_max_framerate": "0"}))

    def test_vsync_alone_paces_the_loop(self):
        self.assertFalse(perfcheck.frame_rate_is_unlimited(
            {"gfx_vsync": "1", "gfx_max_framerate": "0"}))

    def test_the_slider_alone_paces_the_loop(self):
        self.assertFalse(perfcheck.frame_rate_is_unlimited(
            {"gfx_vsync": "0", "gfx_max_framerate": "60"}))

    def test_settings_never_written_are_not_unlimited(self):
        # Before the first launch there is no options.txt at all, and a
        # guess there would cap a player who never chose anything.
        self.assertFalse(perfcheck.frame_rate_is_unlimited({}))

    def test_a_file_without_the_slider_is_not_unlimited(self):
        self.assertFalse(perfcheck.frame_rate_is_unlimited(
            {"gfx_vsync": "0"}))

    def test_a_malformed_value_is_not_unlimited(self):
        self.assertFalse(perfcheck.frame_rate_is_unlimited(
            {"gfx_vsync": "0", "gfx_max_framerate": "unlimited"}))

    def test_a_missing_vsync_key_still_counts_as_off(self):
        self.assertTrue(perfcheck.frame_rate_is_unlimited(
            {"gfx_max_framerate": "0"}))

    def test_it_reads_the_condition_out_of_a_real_prefix(self):
        with tempfile.TemporaryDirectory() as td:
            prefix = _prefix(td, gfx_vsync="0", gfx_max_framerate="0")
            options = perfcheck.read_game_options(
                perfcheck.find_options_file(prefix))
        self.assertTrue(perfcheck.frame_rate_is_unlimited(options))


class GameFrameLimiterTests(unittest.TestCase):
    """Minecraft's own limiter spins in Wine's message pump (#173).

    It used to be reported before every launch. The launcher now holds the
    player's limit itself (FrameRateLimitTests below), so a Max Framerate is
    no longer a condition to name — it is one that has been dealt with.
    """

    def test_a_max_framerate_is_no_longer_reported(self):
        for vsync in ("0", "1"):
            with tempfile.TemporaryDirectory() as td:
                prefix = _prefix(td, gfx_vsync=vsync, gfx_max_framerate="60",
                                 gfx_fullscreen="1")
                problems = perfcheck.performance_problems(
                    prefix, None, environ={}, meminfo_path=_meminfo(td, 9000))
            self.assertEqual(problems, [], vsync)


class FrameRateLimitTests(unittest.TestCase):
    """The cap the launcher hands vkd3d-proton, and when it declines to."""

    def _apply(self, environ=None, refresh_hz=143.85, env=None,
               settings=None, **options):
        with tempfile.TemporaryDirectory() as td:
            prefix = _prefix(td, **options) if options else None
            with mock.patch.object(launch, "info"), \
                    mock.patch.object(launch, "warn") as warned:
                applied = launch._configure_frame_rate_limit(
                    env if env is not None else {},
                    {} if settings is None else settings, prefix,
                    environ=environ or {},
                    refresh_probe=lambda: refresh_hz)
        return applied, warned

    def test_an_unpaced_loop_is_capped_at_the_display(self):
        env = {}
        applied, _ = self._apply(env=env, gfx_vsync="0",
                                 gfx_max_framerate="0")
        # Rounded up, and whole: the limiter reads "143.85" as no limit.
        self.assertEqual(applied, 144)
        self.assertEqual(env["VKD3D_FRAME_RATE"], "144")

    def test_a_whole_refresh_rate_is_not_rounded_past(self):
        env = {}
        applied, _ = self._apply(env=env, refresh_hz=60.0, gfx_vsync="0",
                                 gfx_max_framerate="0")
        self.assertEqual(applied, 60)
        self.assertEqual(env["VKD3D_FRAME_RATE"], "60")

    def test_a_paced_loop_is_left_alone(self):
        env = {}
        applied, _ = self._apply(env=env, gfx_vsync="1",
                                 gfx_max_framerate="0")
        self.assertIsNone(applied)
        self.assertNotIn("VKD3D_FRAME_RATE", env)

    def test_no_settings_file_means_no_cap(self):
        env = {}
        applied, _ = self._apply(env=env)
        self.assertIsNone(applied)
        self.assertNotIn("VKD3D_FRAME_RATE", env)

    def test_an_unreadable_refresh_rate_warns_instead_of_guessing(self):
        env = {}
        applied, warned = self._apply(env=env, refresh_hz=None,
                                      gfx_vsync="0", gfx_max_framerate="0")
        self.assertIsNone(applied)
        self.assertNotIn("VKD3D_FRAME_RATE", env)
        self.assertTrue(warned.called)

    def test_the_cap_can_be_turned_off(self):
        env = {}
        applied, _ = self._apply(env=env, environ={"BOL_FRAME_RATE": "0"},
                                 gfx_vsync="0", gfx_max_framerate="0")
        self.assertIsNone(applied)
        self.assertNotIn("VKD3D_FRAME_RATE", env)

    def test_the_settings_switch_turns_it_off(self):
        env = {}
        applied, _ = self._apply(env=env, settings={"limit_frame_rate": False},
                                 gfx_vsync="0", gfx_max_framerate="0")
        self.assertIsNone(applied)
        self.assertNotIn("VKD3D_FRAME_RATE", env)

    def test_the_switch_is_on_for_an_installation_that_predates_it(self):
        # Settings written before the switch existed carry no key, and the
        # uncapped menu they produce is the bug report, not a preference.
        env = {}
        applied, _ = self._apply(env=env, settings={}, gfx_vsync="0",
                                 gfx_max_framerate="0")
        self.assertEqual(applied, 144)

    def test_an_explicit_rate_outranks_the_switch(self):
        env = {}
        applied, _ = self._apply(env=env, settings={"limit_frame_rate": False},
                                 environ={"BOL_FRAME_RATE": "90"},
                                 gfx_vsync="0", gfx_max_framerate="0")
        self.assertEqual(applied, 90)
        self.assertEqual(env["VKD3D_FRAME_RATE"], "90")

    def test_the_switch_off_needs_no_display_probe(self):
        probed = []
        env = {}
        with mock.patch.object(launch, "info"), mock.patch.object(launch, "warn"):
            applied = launch._configure_frame_rate_limit(
                env, {"limit_frame_rate": False}, None, environ={},
                refresh_probe=lambda: probed.append(1) or 143.85)
        self.assertIsNone(applied)
        self.assertEqual(probed, [])

    def test_an_explicit_rate_applies_to_a_paced_loop_too(self):
        env = {}
        applied, _ = self._apply(env=env, environ={"BOL_FRAME_RATE": "90"},
                                 gfx_vsync="1", gfx_max_framerate="60")
        self.assertEqual(applied, 90)
        self.assertEqual(env["VKD3D_FRAME_RATE"], "90")

    def test_an_explicit_rate_needs_no_display_and_no_settings(self):
        env = {}
        applied, _ = self._apply(env=env, environ={"BOL_FRAME_RATE": "75"},
                                 refresh_hz=None)
        self.assertEqual(applied, 75)
        self.assertEqual(env["VKD3D_FRAME_RATE"], "75")

    def test_a_typo_falls_back_to_the_automatic_behaviour(self):
        env = {}
        applied, warned = self._apply(
            env=env, environ={"BOL_FRAME_RATE": "sixty"},
            gfx_vsync="0", gfx_max_framerate="0")
        self.assertEqual(applied, 144)
        self.assertTrue(warned.called)

    def test_infinity_means_no_cap_rather_than_a_crash(self):
        # float("inf") parses, and the rate is rounded up to whole frames:
        # math.ceil(inf) raised OverflowError and the launch failed.
        env = {}
        applied, _ = self._apply(env=env, environ={"BOL_FRAME_RATE": "inf"},
                                 gfx_vsync="0", gfx_max_framerate="0")
        self.assertIsNone(applied)
        self.assertNotIn("VKD3D_FRAME_RATE", env)

    def test_an_inherited_limit_is_never_overridden(self):
        env = {"VKD3D_FRAME_RATE": "30"}
        applied, _ = self._apply(env=env, gfx_vsync="0",
                                 gfx_max_framerate="0")
        self.assertIsNone(applied)
        self.assertEqual(env["VKD3D_FRAME_RATE"], "30")

    def test_a_limit_set_by_hand_in_the_custom_environment_is_kept(self):
        # Applied to the launch environment after this runs, so it is only
        # visible through the overlaid environ — and is an answer already.
        env = {}
        applied, _ = self._apply(env=env, environ={"VKD3D_FRAME_RATE": "45"},
                                 gfx_vsync="1", gfx_max_framerate="60")
        self.assertIsNone(applied)
        self.assertNotIn("VKD3D_FRAME_RATE", env)

    def test_the_players_own_limit_is_held_at_the_same_rate(self):
        env = {}
        applied, _ = self._apply(env=env, gfx_vsync="0",
                                 gfx_max_framerate="60")
        self.assertEqual(applied, 60)
        self.assertEqual(env["VKD3D_FRAME_RATE"], "60")

    def test_the_players_own_limit_is_held_with_vsync_on_too(self):
        # Measured in a loaded world: vsync on, Max Framerate 60 on a 144 Hz
        # display, and the main thread at 99% of a core, about 70 points of
        # it in the kernel. The present never waits when the cap is under the
        # refresh rate, so vsync does not save the game from its own spin.
        env = {}
        applied, _ = self._apply(env=env, gfx_vsync="1",
                                 gfx_max_framerate="60")
        self.assertEqual(applied, 60)
        self.assertEqual(env["VKD3D_FRAME_RATE"], "60")

    def test_the_players_own_limit_needs_no_display_probe(self):
        env = {}
        applied, warned = self._apply(env=env, refresh_hz=None,
                                      gfx_vsync="0", gfx_max_framerate="75")
        self.assertEqual(applied, 75)
        self.assertFalse(warned.called)

    def test_the_display_switch_does_not_drop_the_players_own_limit(self):
        # The switch turns off the cap at the display's rate; the rate the
        # player chose in game is theirs, not the launcher's.
        env = {}
        applied, _ = self._apply(env=env, settings={"limit_frame_rate": False},
                                 gfx_vsync="0", gfx_max_framerate="60")
        self.assertEqual(applied, 60)

    def test_uncapped_on_request_leaves_the_game_to_pace_itself(self):
        env = {}
        applied, _ = self._apply(env=env, environ={"BOL_FRAME_RATE": "0"},
                                 gfx_vsync="0", gfx_max_framerate="60")
        self.assertIsNone(applied)
        self.assertNotIn("VKD3D_FRAME_RATE", env)

    def test_a_limit_left_aside_by_an_unfinished_session_still_applies(self):
        env = {}
        with tempfile.TemporaryDirectory() as td:
            prefix = _prefix(td, gfx_vsync="0", gfx_max_framerate="0")
            options = perfcheck.find_options_file(prefix)
            options.with_name(options.name + ".bol-max-framerate") \
                .write_text("60\n")
            with mock.patch.object(launch, "info"), \
                    mock.patch.object(launch, "warn"):
                applied = launch._configure_frame_rate_limit(
                    env, {}, prefix, environ={},
                    refresh_probe=lambda: 143.85)
        # The player's 60, not the display's 144 the file alone would give.
        self.assertEqual(applied, 60)
        self.assertEqual(env["VKD3D_FRAME_RATE"], "60")

    def test_the_legacy_renderer_leaves_the_game_its_own_limit(self):
        # Proton leaves vkd3d-proton out of the prefix for WineD3D, so a
        # VKD3D_FRAME_RATE holds nothing there: setting the game's limiter
        # aside left the session with no limit at all (#314).
        for vsync, framerate in (("0", "60"), ("1", "60"), ("0", "0")):
            env = {}
            applied, warned = self._apply(
                env=env, settings={"renderer": "opengl"},
                gfx_vsync=vsync, gfx_max_framerate=framerate)
            self.assertIsNone(applied, (vsync, framerate))
            self.assertNotIn("VKD3D_FRAME_RATE", env)
            self.assertFalse(warned.called)

    def test_the_legacy_renderer_set_by_hand_counts_too(self):
        env = {}
        applied, _ = self._apply(
            env=env, settings={"custom_env": "PROTON_USE_WINED3D=1"},
            gfx_vsync="0", gfx_max_framerate="60")
        self.assertIsNone(applied)
        self.assertNotIn("VKD3D_FRAME_RATE", env)

    def test_the_legacy_renderer_turned_off_by_hand_is_not_it(self):
        # Read the way Proton reads it: "0" turns it off, and the field has
        # the last word over the Renderer setting.
        env = {}
        applied, _ = self._apply(
            env=env, settings={"renderer": "opengl",
                               "custom_env": "PROTON_USE_WINED3D=0"},
            gfx_vsync="0", gfx_max_framerate="60")
        self.assertEqual(applied, 60)
        self.assertEqual(env["VKD3D_FRAME_RATE"], "60")

    def test_an_inherited_legacy_switch_is_not_the_launchs(self):
        # The launch drops an inherited PROTON_USE_WINED3D before Proton
        # sees it; only the Renderer setting and the field decide.
        env = {}
        applied, _ = self._apply(
            env=env, environ={"PROTON_USE_WINED3D": "1"},
            gfx_vsync="0", gfx_max_framerate="60")
        self.assertEqual(applied, 60)

    def test_an_explicit_rate_is_named_as_ignored_by_the_legacy_renderer(self):
        env = {}
        applied, warned = self._apply(
            env=env, settings={"renderer": "opengl"},
            environ={"BOL_FRAME_RATE": "90"})
        self.assertIsNone(applied)
        self.assertNotIn("VKD3D_FRAME_RATE", env)
        self.assertIn("Legacy compatibility renderer",
                      warned.call_args.args[0])

    def test_a_display_probe_that_raises_never_fails_a_launch(self):
        env = {}
        with tempfile.TemporaryDirectory() as td:
            prefix = _prefix(td, gfx_vsync="0", gfx_max_framerate="0")
            with mock.patch.object(launch, "_screen_refresh_hz",
                                   side_effect=OSError) as probed, \
                    mock.patch.object(launch, "warn"):
                applied = launch._configure_frame_rate_limit(
                    env, {}, prefix, environ={})
        # The real probe is what a launch with no seam supplied reaches for.
        self.assertTrue(probed.called)
        self.assertIsNone(applied)
        self.assertNotIn("VKD3D_FRAME_RATE", env)


class LaunchWiringTests(unittest.TestCase):
    def _warn_once(self, environ, problems=("something costs frames",)):
        with mock.patch.object(launch, "performance_problems",
                               return_value=list(problems)) as detected, \
                mock.patch.object(launch, "active_prefix",
                                  return_value=Path("/pfx")), \
                mock.patch.object(launch, "warn") as warned, \
                mock.patch.dict(os.environ, environ, clear=True):
            launch._warn_if_performance_degraded()
        return detected, warned

    def test_each_condition_is_warned_about_at_launch(self):
        _, warned = self._warn_once({}, ("first cost", "second cost"))
        self.assertEqual(warned.call_count, 2)
        self.assertEqual(warned.call_args_list[0][0][0], "first cost")

    def test_a_clean_machine_launches_without_a_word(self):
        _, warned = self._warn_once({}, ())
        self.assertFalse(warned.called)

    def test_the_check_can_be_silenced(self):
        detected, warned = self._warn_once({"BOL_SKIP_PERF_CHECK": "1"})
        self.assertFalse(warned.called)
        # Silenced means not even looked at: no statvfs, no prefix walk.
        self.assertFalse(detected.called)

    def test_it_reads_the_live_prefix_and_data_directory(self):
        detected, _ = self._warn_once({})
        self.assertEqual(detected.call_args[0][0], Path("/pfx"))
        self.assertEqual(detected.call_args[0][1], launch.DATA)


if __name__ == "__main__":
    unittest.main()
