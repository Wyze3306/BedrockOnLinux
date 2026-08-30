"""Regression tests for launch-time engine selection."""
# SPDX-License-Identifier: MIT

import os
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from bol import launch


class ReadyLaunchHarness:
    """Runs one fully mocked `_launch_once`: no GPU, no Wine, no process."""

    def _exercise_ready_launch(self, root, popen, arm, disarm,
                               prefix_idle=True, mark=None, preauth=True,
                               lock_fds=(), managed_engine=True,
                               umu_env=None, account=None, on_started=None,
                               extra_settings=None, environ=None):
        content = root / "content"
        logs = root / "logs"
        data = root / "data"
        content.mkdir(exist_ok=True)
        logs.mkdir(exist_ok=True)
        data.mkdir(exist_ok=True)
        (content / "Minecraft.Windows.exe").write_bytes(b"MZ")
        settings = {"game_dir": str(content)}
        settings.update(extra_settings or {})
        # Discord presence opens a socket of its own: every launch test gets
        # a stand-in, and the presence tests below assert on what it was told.
        self.presence = mock.MagicMock()
        self.presence_calls = []
        patches = (
            mock.patch.dict(os.environ, dict(environ or {}), clear=True),
            mock.patch.object(launch, "CONTENT", content),
            mock.patch.object(launch, "LOGS", logs),
            mock.patch.object(launch, "DATA", data),
            mock.patch.object(launch, "load_settings", return_value=settings),
            mock.patch.object(launch, "proton_path",
                              return_value=Path("/tmp/fake-engine")),
            mock.patch.object(launch, "custom_proton",
                              return_value=not managed_engine),
            mock.patch.object(launch, "require_safe_graphics_session"),
            mock.patch.object(launch, "retire_idle_current_boot_marker"),
            mock.patch.object(launch, "_prepare_launch_engine"),
            mock.patch.object(
                launch, "msa_session_snapshot",
                return_value=(
                    {"refresh_token": "refresh"} if account is None
                    else account,
                    "a" * 32)),
            mock.patch.object(launch, "msa_refresh", return_value=None),
            mock.patch.object(launch, "msa_save_for_account_epoch",
                              return_value=True),
            mock.patch.object(launch, "account_epoch_is_current",
                              return_value=True),
            mock.patch.object(launch, "boot_prefix", return_value=True),
            mock.patch.object(launch, "wine_apply_winegdk_prereqs"),
            mock.patch.object(launch, "_install_cryptbase_in_prefix"),
            mock.patch.object(launch, "install_gameinput"),
            mock.patch.object(launch, "wine_reg_set_refresh_token",
                              return_value=True),
            mock.patch.object(launch, "ensure_login_deps"),
            mock.patch.object(launch, "xbl_preauth", return_value=preauth),
            mock.patch.object(launch, "bump_stack_reserve"),
            mock.patch.object(launch, "engine_cmd",
                              return_value=(["fake-umu"], dict(umu_env or {}))),
            mock.patch.object(launch, "patch_options"),
            mock.patch.object(launch, "snapshot_game_options"),
            mock.patch.object(launch, "restore_truncated_game_options"),
            mock.patch.object(launch, "seed_default_servers"),
            mock.patch.object(launch, "diagnose", return_value=[]),
            mock.patch.object(launch, "_prefix_stably_idle_after_wrapper",
                              return_value=prefix_idle),
            mock.patch.object(launch, "arm_gpu_launch", side_effect=arm),
            mock.patch.object(
                launch, "mark_gpu_wrapper_returned",
                side_effect=mark or (lambda _token: True)),
            mock.patch.object(launch, "disarm_gpu_launch", side_effect=disarm),
            mock.patch.object(launch.subprocess, "Popen", side_effect=popen),
            mock.patch.object(launch.discord, "start_session",
                              side_effect=self._announce_presence),
            mock.patch.object(launch, "info"),
            mock.patch.object(launch, "ok"),
            mock.patch.object(launch, "warn"),
        )
        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            # Keep the active mocks reachable: the offline-mode tests assert on
            # what the auth steps were told, not only on the launch result.
            self.launch_mocks = {
                name: getattr(launch, name)
                for name in ("warn", "wine_reg_set_refresh_token",
                             "ensure_login_deps", "xbl_preauth")
            }
            return launch._launch_once(lock_fds=lock_fds,
                                       on_started=on_started)

    def _announce_presence(self, *args, **kwargs):
        self.presence_calls.append((args, kwargs))
        return self.presence

    def _warnings(self):
        return [str(call.args[0])
                for call in self.launch_mocks["warn"].call_args_list
                if call.args]


class GraphicsEngineLaunchTests(ReadyLaunchHarness, unittest.TestCase):
    def test_managed_install_finishes_before_graphics_validation(self):
        calls = []
        with mock.patch.object(launch, "active_prefix",
                               return_value=Path("/tmp/bol-prefix")), \
                mock.patch.object(
                    launch, "prefix_processes",
                    side_effect=lambda _prefix: calls.append("idle") or []), \
                mock.patch.object(launch, "custom_proton", return_value=False), \
                mock.patch.object(launch, "ensure_winegdk",
                                  side_effect=lambda: calls.append("install")), \
                mock.patch.object(launch, "_prepare_graphics_engine",
                                  side_effect=lambda: calls.append("graphics")):
            launch._prepare_launch_engine()
        self.assertEqual(calls, ["idle", "install", "graphics"])

    def test_corrected_engine_is_installed_before_session_safety_check(self):
        calls = []

        def stop_after_safety():
            calls.append("safety")
            raise launch.BolError("stop after order check")

        with tempfile.TemporaryDirectory() as directory:
            content = Path(directory)
            (content / "Minecraft.Windows.exe").write_bytes(b"MZ")
            with mock.patch.object(
                    launch, "load_settings",
                    return_value={"game_dir": str(content)}), \
                    mock.patch.object(launch, "CONTENT", content), \
                    mock.patch.object(launch, "proton_path",
                                      return_value=Path("/tmp/engine")), \
                    mock.patch.object(
                        launch, "_prepare_launch_engine",
                        side_effect=lambda: calls.append("engine")), \
                    mock.patch.object(
                        launch, "retire_idle_current_boot_marker",
                        side_effect=lambda: calls.append("retire")), \
                    mock.patch.object(
                        launch, "require_safe_graphics_session",
                        side_effect=stop_after_safety):
                with self.assertRaisesRegex(launch.BolError, "order check"):
                    launch._launch_once()
        self.assertEqual(calls, ["engine", "retire", "safety"])

    def test_rejected_managed_install_never_reaches_graphics_or_wine(self):
        with mock.patch.object(launch, "active_prefix",
                               return_value=Path("/tmp/bol-prefix")), \
                mock.patch.object(launch, "prefix_processes",
                                  return_value=[]) as processes, \
                mock.patch.object(launch, "custom_proton", return_value=False), \
                mock.patch.object(launch, "ensure_winegdk",
                                  side_effect=launch.BolError("stale r10")), \
                mock.patch.object(launch, "_prepare_graphics_engine") as graphics, \
                mock.patch.object(launch, "patch_proton") as patch:
            with self.assertRaisesRegex(launch.BolError, "stale r10"):
                launch._prepare_launch_engine()
        processes.assert_called_once_with(Path("/tmp/bol-prefix"))
        graphics.assert_not_called()
        patch.assert_not_called()

    def test_running_prefix_is_refused_not_killed(self):
        with mock.patch.object(launch, "active_prefix",
                               return_value=Path("/tmp/bol-prefix")), \
                mock.patch.object(launch, "prefix_processes",
                                  return_value=[12, 34]), \
                mock.patch.object(launch, "ensure_winegdk") as install:
            with self.assertRaisesRegex(launch.BolError, "already active"):
                launch._prepare_launch_engine()
        install.assert_not_called()

    def test_managed_engine_activates_compatible_variant(self):
        engine = Path("/tmp/GDK-Proton-xuser")
        with mock.patch.object(launch, "custom_proton", return_value=False), \
                mock.patch.object(launch, "proton_path", return_value=engine), \
                mock.patch.object(launch, "prepare_universal_vkd3d",
                                  return_value=("nv-dgc", True)) as prepare, \
                mock.patch.object(launch, "info"):
            self.assertEqual(launch._prepare_graphics_engine(), "nv-dgc")
        prepare.assert_called_once_with(engine, launch.WINEGDK_BUILD_REV)

    def test_user_supplied_engine_is_never_rewritten(self):
        with mock.patch.object(launch, "custom_proton", return_value=True), \
                mock.patch.object(launch, "prepare_universal_vkd3d") as prepare:
            self.assertIsNone(launch._prepare_graphics_engine())
        prepare.assert_not_called()

    def test_managed_engine_validation_error_is_visible(self):
        with mock.patch.object(launch, "custom_proton", return_value=False), \
                mock.patch.object(launch, "proton_path",
                                  return_value=Path("/tmp/engine")), \
                mock.patch.object(launch, "prepare_universal_vkd3d",
                                  side_effect=launch.BolError(
                                      "engine revision mismatch")), \
                mock.patch.object(launch, "die",
                                  side_effect=launch.BolError) as die:
            with self.assertRaises(launch.BolError):
                launch._prepare_graphics_engine()
        die.assert_called_once_with("engine revision mismatch")

    def test_raw_va_workaround_preserves_user_vkd3d_options(self):
        env = {"VKD3D_CONFIG": "breadcrumbs;single_queue"}
        launch._require_vkd3d_config(env, "force_raw_va_cbv")
        self.assertEqual(
            env["VKD3D_CONFIG"],
            "breadcrumbs,single_queue,force_raw_va_cbv",
        )

    def test_raw_va_workaround_is_idempotent(self):
        env = {"VKD3D_CONFIG": "force_raw_va_cbv,breadcrumbs"}
        launch._require_vkd3d_config(env, "force_raw_va_cbv")
        self.assertEqual(env["VKD3D_CONFIG"],
                         "force_raw_va_cbv,breadcrumbs")

    def test_ray_tracing_is_on_unless_settings_say_otherwise(self):
        env = {"VKD3D_CONFIG": "force_raw_va_cbv"}
        launch._configure_ray_tracing(env, {})
        self.assertEqual(env["VKD3D_CONFIG"], "force_raw_va_cbv")

    def test_ray_tracing_clears_an_inherited_nodxr(self):
        # A VKD3D_CONFIG exported by the session must not quietly hide the
        # Ray Traced graphics mode the switch says is available.
        env = {"VKD3D_CONFIG": "nodxr;breadcrumbs"}
        launch._configure_ray_tracing(env, {"ray_tracing": True})
        self.assertEqual(env["VKD3D_CONFIG"], "breadcrumbs")

    def test_ray_tracing_off_leaves_no_empty_vkd3d_config(self):
        env = {"VKD3D_CONFIG": "nodxr"}
        launch._configure_ray_tracing(env, {"ray_tracing": True})
        self.assertNotIn("VKD3D_CONFIG", env)

    def test_disabled_ray_tracing_keeps_the_other_vkd3d_options(self):
        env = {"VKD3D_CONFIG": "force_raw_va_cbv"}
        launch._configure_ray_tracing(env, {"ray_tracing": False})
        self.assertEqual(env["VKD3D_CONFIG"], "force_raw_va_cbv,nodxr")

    def test_disabled_ray_tracing_is_idempotent(self):
        env = {"VKD3D_CONFIG": "nodxr,force_raw_va_cbv"}
        launch._configure_ray_tracing(env, {"ray_tracing": False})
        self.assertEqual(env["VKD3D_CONFIG"], "nodxr,force_raw_va_cbv")

    def test_x11_neutralises_inherited_winewayland_and_enables_noopwr(self):
        env = {"PROTON_ENABLE_WAYLAND": "1"}
        launch._configure_runtime_compat(
            env, {}, "x11", True, host_env={"PROTON_ENABLE_WAYLAND": "1"},
            steam_deck=False,
        )
        self.assertEqual(env["PROTON_ENABLE_WAYLAND"], "0")
        self.assertEqual(env["WINE_DISABLE_VULKAN_OPWR"], "1")

    def test_native_x11_does_not_enable_wayland_presentation_workaround(self):
        env = {}
        launch._configure_runtime_compat(
            env, {}, "x11", False, host_env={}, steam_deck=False,
        )
        self.assertNotIn("WINE_DISABLE_VULKAN_OPWR", env)

    def test_wayland_backend_is_not_forced_back_to_x11(self):
        env = {}
        launch._configure_runtime_compat(
            env, {}, "wayland", True, host_env={}, steam_deck=False,
        )
        self.assertNotIn("PROTON_ENABLE_WAYLAND", env)

    def test_non_steam_launch_prefers_sdl_controller_mapping(self):
        env = {}
        launch._configure_runtime_compat(
            env, {}, "x11", False, host_env={}, steam_deck=False,
        )
        self.assertEqual(env["PROTON_PREFER_SDL"], "1")
        self.assertNotIn("PROTON_DISABLE_HIDRAW", env)

    def test_steam_launch_leaves_input_mapping_to_steam(self):
        env = {}
        launch._configure_runtime_compat(
            env, {}, "x11", False,
            host_env={
                "SteamGameId": "1234567890",
                "SteamVirtualGamepadInfo": "/run/user/1000/steam-input",
            },
            steam_deck=False,
        )
        self.assertNotIn("PROTON_PREFER_SDL", env)
        self.assertEqual(
            set(env["PROTON_DISABLE_HIDRAW"].split(",")),
            {
                "0x054C/0x05C4",
                "0x054C/0x09CC",
                "0x054C/0x0BA0",
                "0x054C/0x0CE6",
                "0x054C/0x0DF2",
            },
        )

    def test_steam_virtual_gamepad_without_app_id_keeps_steam_input(self):
        gamepad_info = "/run/user/1000/steam-virtual-gamepad-info"
        env = {"SteamVirtualGamepadInfo_Proton": gamepad_info}
        launch._configure_runtime_compat(
            env, {}, "x11", False, host_env=dict(env), steam_deck=False,
        )
        self.assertNotIn("PROTON_PREFER_SDL", env)
        self.assertIn("0x054C/0x0DF2", env["PROTON_DISABLE_HIDRAW"])
        self.assertEqual(env["SteamVirtualGamepadInfo_Proton"],
                         gamepad_info)

    def test_steam_sony_filter_replaces_global_value_and_spares_non_sony(self):
        env = {"PROTON_DISABLE_HIDRAW": "1"}
        launch._configure_runtime_compat(
            env, {}, "x11", False,
            host_env={
                "SteamGameId": "1234567890",
                "SteamVirtualGamepadInfo_Proton":
                    "/run/user/1000/steam-input",
                "PROTON_DISABLE_HIDRAW": "1",
            },
            steam_deck=False,
        )
        sony_ids = env["PROTON_DISABLE_HIDRAW"]
        self.assertIn("0x054C/0x0DF2", sony_ids)
        self.assertNotIn("0x045E/", sony_ids)  # Microsoft/Xbox
        self.assertNotIn("0x057E/", sony_ids)  # Nintendo
        self.assertNotEqual(sony_ids, "1")

    def test_empty_steam_markers_do_not_disable_standalone_sdl_input(self):
        env = {}
        launch._configure_runtime_compat(
            env, {}, "x11", False,
            host_env={
                "SteamGameId": "0",
                "SteamAppId": "",
                "SteamVirtualGamepadInfo": " ",
                "SteamVirtualGamepadInfo_Proton": "",
            },
            steam_deck=False,
        )
        self.assertEqual(env["PROTON_PREFER_SDL"], "1")

    def test_steam_app_id_without_virtual_gamepad_uses_sdl_fallback(self):
        env = {}
        launch._configure_runtime_compat(
            env, {}, "x11", False,
            host_env={
                "SteamGameId": "1234567890",
                "SteamAppId": "1234567890",
                "STEAM_COMPAT_CLIENT_INSTALL_PATH": "/opt/steam",
            },
            steam_deck=False,
        )
        self.assertEqual(env["PROTON_PREFER_SDL"], "1")
        self.assertNotIn("PROTON_DISABLE_HIDRAW", env)

    def test_winewayland_uses_sdl_even_with_virtual_steam_gamepad(self):
        env = {
            "SteamVirtualGamepadInfo_Proton":
                "/run/user/1000/steam-input",
            "PROTON_DISABLE_HIDRAW": "1",
            "PROTON_NO_STEAMINPUT": "0",
        }
        launch._configure_runtime_compat(
            env, {}, "wayland", True, host_env=dict(env), steam_deck=False,
        )
        self.assertEqual(env["PROTON_PREFER_SDL"], "1")
        self.assertNotIn("PROTON_DISABLE_HIDRAW", env)
        self.assertNotIn("PROTON_NO_STEAMINPUT", env)

    def test_steam_deck_disables_wine_window_decoration(self):
        env = {}
        launch._configure_runtime_compat(
            env, {}, "x11", True, host_env={"SteamDeck": "1"},
            steam_deck=True,
        )
        self.assertEqual(env["PROTON_NO_WM_DECORATION"], "1")

    def test_legacy_renderer_uses_supported_opengl_fallback(self):
        env = {}
        launch._configure_runtime_compat(
            env, {"renderer": "opengl"}, "x11", False, host_env={},
            steam_deck=False,
        )
        self.assertEqual(env["PROTON_USE_WINED3D"], "1")

    def test_normal_launch_does_not_enable_proton_debug_log(self):
        env = {
            "PROTON_LOG": "1",
            "PROTON_LOG_DIR": "/tmp/inherited-logs",
            "WINEDEBUG": "trace+all",
        }
        launch._configure_runtime_compat(
            env, {}, "x11", False, diagnostics=False,
            host_env=dict(env),
            steam_deck=False,
        )
        self.assertNotIn("PROTON_LOG", env)
        self.assertNotIn("PROTON_LOG_DIR", env)
        self.assertEqual(env["WINEDEBUG"], "-all")

    def test_diagnostics_keep_errors_without_hot_gdk_traces(self):
        env = {"WINEDEBUG": "trace+all"}
        with mock.patch.object(launch, "LOGS", Path("/tmp/bol-logs")):
            launch._configure_runtime_compat(
                env, {}, "x11", False, diagnostics=True,
                host_env=dict(env),
                steam_deck=False,
            )
        self.assertEqual(env["PROTON_LOG"], "1")
        self.assertEqual(env["PROTON_LOG_DIR"], "/tmp/bol-logs")
        self.assertIn("+gdkc", env["WINEDEBUG"])
        self.assertIn("trace-gdkc", env["WINEDEBUG"])
        self.assertIn("+xgameruntime", env["WINEDEBUG"])
        self.assertIn("trace-xgameruntime", env["WINEDEBUG"])
        self.assertNotIn("trace+gdkc", env["WINEDEBUG"])
        self.assertNotIn("trace+xgameruntime", env["WINEDEBUG"])
        self.assertNotIn("trace+all", env["WINEDEBUG"])

    def test_managed_graphics_cache_is_persistent_and_private(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td) / "profile"
            env = {}
            with mock.patch.object(launch, "DATA", data):
                launch._configure_graphics_cache(env, managed_engine=True)

            cache = data / "graphics-cache"
            self.assertTrue(cache.is_dir())
            self.assertEqual(cache.stat().st_mode & 0o777, 0o700)
            self.assertEqual(env["VKD3D_SHADER_CACHE_PATH"], str(cache))
            self.assertEqual(env["DXVK_SHADER_CACHE_PATH"], str(cache))

    def test_custom_engine_keeps_its_own_graphics_cache_settings(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td) / "profile"
            env = {
                "VKD3D_SHADER_CACHE_PATH": "/custom/vkd3d",
                "DXVK_SHADER_CACHE_PATH": "/custom/dxvk",
            }
            with mock.patch.object(launch, "DATA", data):
                launch._configure_graphics_cache(env, managed_engine=False)

            self.assertFalse((data / "graphics-cache").exists())
            self.assertEqual(
                env["VKD3D_SHADER_CACHE_PATH"], "/custom/vkd3d")
            self.assertEqual(env["DXVK_SHADER_CACHE_PATH"], "/custom/dxvk")

    def test_inherited_compat_values_cannot_disable_automatic_defaults(self):
        env = {
            "PROTON_ENABLE_WAYLAND": "1",
            "WINE_DISABLE_VULKAN_OPWR": "0",
            "PROTON_PREFER_SDL": "0",
            "PROTON_DISABLE_HIDRAW": "0x1234/0x5678",
            "PROTON_NO_WM_DECORATION": "0",
            "PROTON_USE_WINED3D": "0",
        }
        launch._configure_runtime_compat(
            env, {"renderer": "opengl"}, "x11", True,
            host_env=dict(env), steam_deck=True,
        )
        self.assertEqual(env["PROTON_ENABLE_WAYLAND"], "0")
        self.assertEqual(env["WINE_DISABLE_VULKAN_OPWR"], "1")
        self.assertEqual(env["PROTON_PREFER_SDL"], "1")
        self.assertNotIn("PROTON_DISABLE_HIDRAW", env)
        self.assertEqual(env["PROTON_NO_WM_DECORATION"], "1")
        self.assertEqual(env["PROTON_USE_WINED3D"], "1")

    def test_steam_input_removes_inherited_sdl_preference(self):
        env = {
            "PROTON_PREFER_SDL": "1",
            "PROTON_NO_STEAMINPUT": "1",
        }
        launch._configure_runtime_compat(
            env, {}, "x11", False,
            host_env={
                "SteamGameId": "1234567890",
                "SteamVirtualGamepadInfo_Proton":
                    "/run/user/1000/steam-input",
                "PROTON_PREFER_SDL": "1",
                "PROTON_NO_STEAMINPUT": "1",
            },
            steam_deck=False,
        )
        self.assertNotIn("PROTON_PREFER_SDL", env)
        self.assertNotIn("PROTON_NO_STEAMINPUT", env)

    def test_each_launch_discards_only_previous_proton_session_logs(self):
        with tempfile.TemporaryDirectory() as td:
            logs = Path(td)
            stale = (
                logs / "proton.log",
                logs / "steam-0.log",
                logs / "steam-umu-default.log",
            )
            for path in stale:
                path.write_text("stale crash")
            minecraft = logs / "minecraft.log"
            minecraft.write_text("keep until launch truncates it")
            unrelated = logs / "native-login.log"
            unrelated.write_text("keep")

            with mock.patch.object(launch, "LOGS", logs):
                launch._clear_previous_proton_logs()

            self.assertTrue(all(not path.exists() for path in stale))
            self.assertTrue(minecraft.exists())
            self.assertTrue(unrelated.exists())

    def test_custom_environment_still_has_final_precedence(self):
        with tempfile.TemporaryDirectory() as td:
            env = {}
            launch._configure_runtime_compat(
                env, {"renderer": "opengl"}, "x11", True, host_env={},
                steam_deck=True,
            )
            with mock.patch.object(launch, "DATA", Path(td)):
                launch._configure_graphics_cache(env, managed_engine=True)
            launch.apply_custom_env(
                env,
                "PROTON_ENABLE_WAYLAND=1 WINE_DISABLE_VULKAN_OPWR=0 "
                "PROTON_PREFER_SDL=0 PROTON_DISABLE_HIDRAW=0 "
                "PROTON_NO_WM_DECORATION=0 PROTON_USE_WINED3D=0 "
                "VKD3D_SHADER_CACHE_PATH=0 "
                "DXVK_SHADER_CACHE_PATH=/custom/dxvk",
            )
        self.assertEqual(env["PROTON_ENABLE_WAYLAND"], "1")
        self.assertEqual(env["WINE_DISABLE_VULKAN_OPWR"], "0")
        self.assertEqual(env["PROTON_PREFER_SDL"], "0")
        self.assertEqual(env["PROTON_DISABLE_HIDRAW"], "0")
        self.assertEqual(env["PROTON_NO_WM_DECORATION"], "0")
        self.assertEqual(env["PROTON_USE_WINED3D"], "0")
        self.assertEqual(env["VKD3D_SHADER_CACHE_PATH"], "0")
        self.assertEqual(env["DXVK_SHADER_CACHE_PATH"], "/custom/dxvk")

    def test_launcher_owned_override_is_warned_about_but_still_applied(self):
        # The Advanced field keeps the final word by design; the warning only
        # makes the override visible so a crash is traceable to it (#134).
        env = {}
        with mock.patch.object(launch, "warn") as warn:
            launch.apply_custom_env(env, "PROTON_USE_WINED3D=1")
            launch._warn_custom_env_overrides("PROTON_USE_WINED3D=1")
        self.assertEqual(env["PROTON_USE_WINED3D"], "1")
        warn.assert_called_once()
        message = warn.call_args[0][0]
        self.assertIn("PROTON_USE_WINED3D", message)
        self.assertIn("Legacy compatibility renderer", message)

    def test_unowned_custom_variables_are_not_warned_about(self):
        with mock.patch.object(launch, "warn") as warn:
            launch._warn_custom_env_overrides("MANGOHUD=1 DXVK_HUD=fps")
        warn.assert_not_called()

    def test_each_overridden_variable_is_named_once(self):
        with mock.patch.object(launch, "warn") as warn:
            launch._warn_custom_env_overrides(
                "PROTON_LOG=1 MANGOHUD=1 PROTON_LOG=0 PROTON_PREFER_SDL=0")
        self.assertEqual(warn.call_count, 2)
        named = " ".join(call[0][0] for call in warn.call_args_list)
        self.assertIn("PROTON_LOG", named)
        self.assertIn("PROTON_PREFER_SDL", named)

    def test_override_warning_survives_an_unparsable_field(self):
        # custom_env_keys must stay silent on bad syntax: apply_custom_env
        # already reports it, and warning twice for one typo is noise.
        with mock.patch.object(launch, "warn") as warn:
            launch._warn_custom_env_overrides('PROTON_LOG="1')
        warn.assert_not_called()

    def test_gnutls_priority_override_is_never_exported(self):
        """Regression for issue #48: exporting GNUTLS_SYSTEM_PRIORITY_FILE
        makes Wine's secur32 abandon its version-capped GnuTLS priority and
        negotiate TLS 1.3, which this Wine's schannel does not support. Every
        in-game WinHTTP TLS connection (including the XSAPI RTA WebSocket that
        MPSD session writes depend on) then dies post-handshake, and Friends
        worlds fail with "world is full". The launch environment must not
        carry the variable, even when it is inherited."""
        observed = {}

        class Process:
            @staticmethod
            def wait(timeout):
                return 0

        def popen(*_args, **kwargs):
            observed.update(kwargs)
            return Process()

        with tempfile.TemporaryDirectory() as td:
            self._exercise_ready_launch(
                Path(td),
                popen,
                lambda: "owned-token",
                lambda _token: True,
                umu_env={
                    "GNUTLS_SYSTEM_PRIORITY_FILE": "/stale/gnutls.cfg",
                    "GNUTLS_SYSTEM_PRIORITY_FAIL_ON_INVALID": "0",
                },
            )

        self.assertIn("env", observed)
        self.assertNotIn("GNUTLS_SYSTEM_PRIORITY_FILE", observed["env"])
        self.assertNotIn("GNUTLS_SYSTEM_PRIORITY_FAIL_ON_INVALID",
                         observed["env"])

    def test_gpu_safety_failure_allows_engine_update_but_blocks_wine(self):
        with tempfile.TemporaryDirectory() as td:
            game = Path(td)
            (game / "Minecraft.Windows.exe").write_bytes(b"MZ")
            with mock.patch.object(launch, "load_settings",
                                   return_value={"game_dir": str(game)}), \
                    mock.patch.object(launch, "proton_path",
                                      return_value=Path("/tmp/engine")), \
                    mock.patch.object(
                        launch, "retire_idle_current_boot_marker"
                    ), \
                    mock.patch.object(
                        launch, "require_safe_graphics_session",
                        side_effect=launch.BolError("unsafe GPU")), \
                    mock.patch.object(launch, "_prepare_launch_engine") as prep, \
                    mock.patch.object(launch, "boot_prefix") as boot:
                with self.assertRaisesRegex(launch.BolError, "unsafe GPU"):
                    launch._launch_once()
            prep.assert_called_once_with()
            boot.assert_not_called()

    def test_gpu_marker_wraps_the_only_game_process_and_clears_on_return(self):
        calls = []

        class Process:
            @staticmethod
            def wait(timeout):
                calls.append(("wait", timeout))
                return 0

        def arm():
            calls.append(("arm",))
            return "owned-token"

        def popen(*_args, **_kwargs):
            calls.append(("popen",))
            return Process()

        def disarm(token):
            calls.append(("disarm", token))
            return True

        def mark(token):
            calls.append(("mark-returned", token))
            return True

        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(
                self._exercise_ready_launch(
                    Path(td), popen, arm, disarm, mark=mark), 0)
        self.assertEqual(calls, [
            ("arm",),
            ("popen",),
            ("wait", 1),
            ("mark-returned", "owned-token"),
            ("disarm", "owned-token"),
        ])

    def test_game_wrapper_inherits_both_launch_lock_descriptors(self):
        observed = {}

        class Process:
            @staticmethod
            def wait(timeout):
                return 0

        def popen(*_args, **kwargs):
            observed.update(kwargs)
            return Process()

        with tempfile.TemporaryDirectory() as td:
            self._exercise_ready_launch(
                Path(td),
                popen,
                lambda: "owned-token",
                lambda _token: True,
                lock_fds=(71, 72),
            )

        self.assertEqual(observed["pass_fds"], (71, 72))

    def test_managed_launch_passes_persistent_graphics_cache(self):
        observed = {}

        class Process:
            @staticmethod
            def wait(timeout):
                return 0

        def popen(*_args, **kwargs):
            observed.update(kwargs)
            return Process()

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._exercise_ready_launch(
                root,
                popen,
                lambda: "owned-token",
                lambda _token: True,
            )

            cache = root / "data" / "graphics-cache"
            self.assertEqual(
                observed["env"]["VKD3D_SHADER_CACHE_PATH"], str(cache))
            self.assertEqual(
                observed["env"]["DXVK_SHADER_CACHE_PATH"], str(cache))
            self.assertTrue(cache.is_dir())

    def test_launch_hands_ray_tracing_to_the_game_by_default(self):
        """An inherited nodxr cannot silently remove the Ray Traced mode."""
        observed = {}

        class Process:
            @staticmethod
            def wait(timeout):
                return 0

        def popen(*_args, **kwargs):
            observed.update(kwargs)
            return Process()

        with tempfile.TemporaryDirectory() as td:
            self._exercise_ready_launch(
                Path(td),
                popen,
                lambda: "owned-token",
                lambda _token: True,
                umu_env={"VKD3D_CONFIG": "nodxr"},
            )

        self.assertEqual(observed["env"]["VKD3D_CONFIG"], "force_raw_va_cbv")

    @staticmethod
    def _successful_popen(observed):
        class Process:
            @staticmethod
            def wait(timeout):
                return 0

        def popen(*_args, **kwargs):
            observed.update(kwargs)
            return Process()

        return popen

    def test_xbox_preauth_failure_starts_offline_with_stage_and_action(self):
        """Unusable Xbox Live must not block single-player play (#160)."""
        observed = {}
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(
                    launch, "xbl_preauth_error_message",
                    return_value="Verify the account age, then try again."), \
                mock.patch.object(
                    launch, "xbl_preauth_diagnostic",
                    return_value={"stage": "sisu-multiplayer",
                                  "category": "age"}):
            self.assertEqual(
                self._exercise_ready_launch(
                    Path(td), self._successful_popen(observed),
                    lambda: "owned-token", lambda _token: True,
                    preauth=False,
                ),
                0,
            )

        notice = "\n".join(self._warnings())
        self.assertIn("stage: sisu-multiplayer", notice)
        self.assertIn("Verify the account age", notice)
        self.assertIn("offline mode", notice)
        # The account is linked, so the token still reaches the prefix: the
        # game may complete the sign-in itself once Xbox Live answers again.
        token_write = self.launch_mocks["wine_reg_set_refresh_token"]
        token_write.assert_called_once_with("refresh")

    def test_launch_without_a_microsoft_account_runs_offline(self):
        observed = {}
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(
                self._exercise_ready_launch(
                    Path(td), self._successful_popen(observed),
                    lambda: "owned-token", lambda _token: True,
                    account={},
                ),
                0,
            )

        self.assertIn("offline mode", "\n".join(self._warnings()))
        # Nothing to sign in with, so no token write and no Xbox Live round
        # trip: neither may stand between the player and their local worlds.
        self.launch_mocks["wine_reg_set_refresh_token"].assert_not_called()
        self.launch_mocks["xbl_preauth"].assert_not_called()
        self.launch_mocks["ensure_login_deps"].assert_not_called()
        self.assertNotIn("WINEGDK_PREAUTH_DEVICE", observed["env"])

    def test_unusable_preauth_payload_is_withheld_from_the_engine(self):
        for preauth, expected in ((True, True), (False, False)):
            observed = {}
            with self.subTest(preauth=preauth), \
                    tempfile.TemporaryDirectory() as td:
                root = Path(td)
                cache = root / "data" / "winegdk-preauth"
                cache.mkdir(parents=True)
                (cache / "device.json").write_text("{}")
                self._exercise_ready_launch(
                    root, self._successful_popen(observed),
                    lambda: "owned-token", lambda _token: True,
                    preauth=preauth,
                )
                self.assertEqual(
                    "WINEGDK_PREAUTH_DEVICE" in observed["env"], expected)

    def test_gpu_marker_is_cleared_when_process_spawn_fails(self):
        calls = []

        def arm():
            calls.append("arm")
            return "owned-token"

        def popen(*_args, **_kwargs):
            calls.append("popen")
            raise OSError("spawn interrupted")

        def disarm(_token):
            calls.append("disarm")
            return True

        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(OSError, "spawn interrupted"):
                self._exercise_ready_launch(Path(td), popen, arm, disarm)
        self.assertEqual(calls, ["arm", "popen", "disarm"])

    def test_gpu_marker_remains_when_wrapper_returns_with_live_children(self):
        calls = []

        class Process:
            @staticmethod
            def wait(timeout):
                calls.append(("wait", timeout))
                return 0

        def arm():
            calls.append(("arm",))
            return "owned-token"

        def popen(*_args, **_kwargs):
            calls.append(("popen",))
            return Process()

        def disarm(token):
            calls.append(("disarm", token))
            return True

        def mark(token):
            calls.append(("mark-returned", token))
            return True

        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(self._exercise_ready_launch(
                Path(td), popen, arm, disarm, prefix_idle=False, mark=mark), 0)
        self.assertEqual(calls, [
            ("arm",),
            ("popen",),
            ("wait", 1),
            ("mark-returned", "owned-token"),
        ])

    def test_wrapper_return_requires_three_idle_rescans_without_killing(self):
        scans = [[91], [91], [], [], []]
        with mock.patch.object(launch, "active_prefix",
                               return_value=Path("/tmp/prefix")), \
                mock.patch.object(launch, "prefix_processes",
                                  side_effect=scans) as processes, \
                mock.patch.object(launch.time, "monotonic",
                                  side_effect=[0, 0.1, 0.2, 0.3, 0.4]), \
                mock.patch.object(launch.time, "sleep"), \
                mock.patch.object(launch.os, "kill") as kill:
            self.assertTrue(launch._prefix_stably_idle_after_wrapper())
        self.assertEqual(processes.call_count, 5)
        kill.assert_not_called()


class DirectLaunchReadinessTests(unittest.TestCase):
    def test_missing_game_and_account_are_both_named(self):
        with mock.patch.object(launch, "load_settings", return_value={}), \
                mock.patch.object(launch, "msa_signed_in",
                                  return_value=False):
            pending = launch.direct_launch_readiness()

        self.assertEqual(len(pending), 2)
        self.assertIn("No Minecraft version is installed", pending[0])
        self.assertIn("No Microsoft account is linked", pending[1])

    def test_prepared_installation_has_nothing_pending(self):
        with tempfile.TemporaryDirectory() as td:
            content = Path(td)
            (content / "Minecraft.Windows.exe").write_bytes(b"MZ")
            with mock.patch.object(
                    launch, "load_settings",
                    return_value={"game_dir": str(content)}), \
                    mock.patch.object(launch, "msa_signed_in",
                                      return_value=True):
                self.assertEqual(launch.direct_launch_readiness(), [])

    def test_uninstalled_game_dir_is_reported_even_when_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(
                    launch, "load_settings",
                    return_value={"game_dir": str(Path(td) / "gone")}), \
                    mock.patch.object(launch, "msa_signed_in",
                                      return_value=True):
                pending = launch.direct_launch_readiness()

        self.assertEqual(len(pending), 1)
        self.assertIn("No Minecraft version is installed", pending[0])


class SingleWindowSessionTests(unittest.TestCase):
    """Game Mode shows one window, so the launcher steps aside — never out."""

    def test_gamescope_session_is_single_window(self):
        with mock.patch.object(launch, "in_gamescope_session",
                               return_value=True) as probe:
            self.assertTrue(launch.single_window_session({"DISPLAY": ":1"}))
        probe.assert_called_once_with({"DISPLAY": ":1"})

    def test_ordinary_desktop_session_is_not(self):
        with mock.patch.object(launch, "in_gamescope_session",
                               return_value=False):
            self.assertFalse(launch.single_window_session({}))

    def test_first_run_state_is_never_consulted(self):
        # Whether a version is installed decided the old skip-the-window
        # behaviour. Nothing about the session depends on it any more.
        with mock.patch.object(launch, "in_gamescope_session",
                               return_value=True), \
                mock.patch.object(
                    launch, "direct_launch_readiness",
                    side_effect=AssertionError(
                        "the session shape does not depend on first-run "
                        "state")):
            self.assertTrue(launch.single_window_session({}))


class SteamGameWindowTaggerTests(unittest.TestCase):
    """Issue #199: in Game Mode the game window needs Steam's own identity.

    Gamescope attributes a window either from its STEAM_GAME property or by
    walking the owning process up to Steam's reaper. Started through the
    Flatpak portal, Minecraft has neither, so the launcher stamps the property
    on the window itself once the game has opened it.
    """

    DECK_ENV = {"SteamAppId": "2716672805",
                "SteamGameId": "11668020851441139712"}
    GAME_EXE = "/games/1.26.44.3/Minecraft.Windows.exe"

    def _tagger(self, env=None, environ=None, single_window=True, tag=None,
                clock=None, deadline=180.0):
        return launch._steam_game_window_tagger(
            {"DISPLAY": ":1"} if env is None else env,
            self.GAME_EXE,
            environ=self.DECK_ENV if environ is None else environ,
            single_window=single_window,
            deadline=deadline,
            clock=(lambda: 0.0) if clock is None else clock,
            tag=tag)

    def test_the_window_is_tagged_with_the_inherited_application_id(self):
        calls = []

        def tag(app_id, wm_class, display=None, skip=()):
            calls.append((app_id, wm_class, display, set(skip)))
            return (20971521,)

        attempt = self._tagger(tag=tag)
        with mock.patch.object(launch, "info") as told:
            self.assertFalse(attempt())
        self.assertEqual(
            calls, [("2716672805", "minecraft.windows.exe", ":1", set())])
        self.assertIn("2716672805", told.call_args[0][0])

    def test_a_window_it_already_tagged_is_never_tagged_twice(self):
        skips = []

        def tag(_app_id, _wm_class, display=None, skip=()):
            skips.append(set(skip))
            return (20971521,) if not skip else ()

        attempt = self._tagger(tag=tag)
        with mock.patch.object(launch, "info") as told:
            attempt()
            attempt()
            attempt()
        self.assertEqual(skips, [set(), {20971521}, {20971521}])
        told.assert_called_once()

    def test_a_window_the_game_replaces_is_tagged_again(self):
        found = iter([(1,), (), (2,)])

        attempt = self._tagger(tag=lambda *_a, **_k: next(found))
        with mock.patch.object(launch, "info") as told:
            for _ in range(3):
                self.assertFalse(attempt())
        # Reported once: the second window is the same fact, not a new one.
        told.assert_called_once()

    def test_it_keeps_watching_until_the_game_opens_its_window(self):
        found = iter([(), (), (7,)])
        attempt = self._tagger(tag=lambda *_a, **_k: next(found))
        with mock.patch.object(launch, "info") as told, \
                mock.patch.object(launch, "warn") as complained:
            for _ in range(3):
                self.assertFalse(attempt())
        told.assert_called_once()
        complained.assert_not_called()

    def test_a_launch_that_never_opens_a_window_says_so_once(self):
        now = [0.0]
        attempt = self._tagger(tag=lambda *_a, **_k: (), clock=lambda: now[0],
                               deadline=180.0)
        with mock.patch.object(launch, "warn") as told:
            self.assertFalse(attempt())
            told.assert_not_called()
            now[0] = 181.0
            self.assertFalse(attempt())
            self.assertFalse(attempt())
        told.assert_called_once()
        self.assertIn("2716672805", told.call_args[0][0])

    def test_a_window_found_late_is_never_reported_as_missing(self):
        now = [0.0]
        found = iter([(), (7,), ()])
        attempt = self._tagger(tag=lambda *_a, **_k: next(found),
                               clock=lambda: now[0], deadline=10.0)
        with mock.patch.object(launch, "info"), \
                mock.patch.object(launch, "warn") as told:
            attempt()
            now[0] = 5.0
            attempt()
            now[0] = 60.0
            attempt()
        told.assert_not_called()

    def test_a_broken_x_connection_never_takes_the_launch_down(self):
        def explode(*_args, **_kwargs):
            raise OSError("display gone")

        attempt = self._tagger(tag=explode)
        with mock.patch.object(launch, "warn") as told:
            self.assertTrue(attempt())
        told.assert_called_once()

    def test_a_launch_steam_did_not_start_is_left_alone(self):
        for environ in ({}, {"SteamAppId": "0"}, {"SteamAppId": "default"}):
            with self.subTest(environ=environ):
                self.assertIsNone(self._tagger(environ=environ))

    def test_an_ordinary_desktop_session_is_left_alone(self):
        # Nothing there reads STEAM_GAME, and the window is presented anyway.
        self.assertIsNone(self._tagger(single_window=False))

    def test_a_launch_without_an_x_display_is_left_alone(self):
        for env in ({}, {"DISPLAY": "  "},
                    {"DISPLAY": ":1", "PROTON_ENABLE_WAYLAND": "1"}):
            with self.subTest(env=env):
                self.assertIsNone(self._tagger(env=env))

    def test_the_session_shape_is_probed_when_the_caller_does_not_say(self):
        with mock.patch.object(launch, "single_window_session",
                               return_value=False) as probe:
            self.assertIsNone(launch._steam_game_window_tagger(
                {"DISPLAY": ":1"}, self.GAME_EXE, environ=self.DECK_ENV))
        probe.assert_called_once_with()


class SteamWindowTaggingDuringLaunchTests(ReadyLaunchHarness,
                                          unittest.TestCase):
    """The tagger runs on the same tick that waits on the game process."""

    GAME_MODE = {"SteamAppId": "2716672805", "DISPLAY": ":1"}

    def _play(self, environ=None, extra_settings=None,
              gamescope_installed=False, attempts=(False, True)):
        """One launch whose game process stays up for `len(attempts)` ticks."""
        self.tagger_calls = []
        remaining = list(attempts)

        def attempt():
            self.tagger_calls.append("tried")
            return remaining.pop(0) if remaining else True

        ticks = [subprocess.TimeoutExpired("umu", 1)] * len(attempts) + [0]

        def popen(*_a, **_kw):
            proc = mock.Mock()

            def wait(timeout=None):
                tick = ticks.pop(0)
                if isinstance(tick, BaseException):
                    raise tick
                return tick

            proc.wait.side_effect = wait
            return proc

        factory = mock.Mock(return_value=attempt)
        which = (lambda name: "/usr/bin/gamescope"
                 if gamescope_installed else None)
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(
                    launch, "_steam_game_window_tagger", factory), \
                mock.patch.object(launch.shutil, "which", which), \
                mock.patch.object(launch, "_screen_wh", return_value=None):
            self._exercise_ready_launch(
                Path(td), popen,
                arm=lambda: "owned-token",
                disarm=lambda _token: True,
                environ=self.GAME_MODE if environ is None else environ,
                extra_settings=extra_settings,
            )
        return factory

    def test_the_tagger_is_built_from_the_launch_env_and_the_game(self):
        factory = self._play()
        env, exe = factory.call_args[0]
        self.assertEqual(env["DISPLAY"], ":1")
        self.assertTrue(exe.endswith("Minecraft.Windows.exe"))

    def test_it_is_retried_every_tick_until_it_reports_it_is_finished(self):
        self._play(attempts=(False, False, True))
        self.assertEqual(len(self.tagger_calls), 3)

    def test_it_is_not_called_again_once_it_is_finished(self):
        self._play(attempts=(True, False, False))
        self.assertEqual(len(self.tagger_calls), 1)

    def test_a_gamescope_of_our_own_owns_its_windows_and_is_left_alone(self):
        # BOL_GAMESCOPE nests a compositor of our own: the game window lives
        # on its display, not on the session's, and it needs no Steam identity
        # to be presented there.
        factory = self._play(extra_settings={"gamescope": "1"},
                             gamescope_installed=True)
        factory.assert_not_called()
        self.assertEqual(self.tagger_calls, [])


class LaunchStartedHookTests(ReadyLaunchHarness, unittest.TestCase):
    """A launcher window steps aside once the game process exists."""

    def _play(self, hook="stepped aside", popen_fails=False):
        """Run one launch, recording the order of spawn, hook and wait.

        `hook` is the marker the started-hook records, ``None`` for no hook,
        or an exception for a hook that fails.
        """
        # Reachable after a raising launch, which never returns the list.
        self.events = events = []

        def popen(*_a, **_kw):
            events.append("spawned")
            if popen_fails:
                raise OSError("no such file")
            proc = mock.Mock()
            proc.wait.side_effect = lambda timeout=None: (
                events.append("waited") or 0)
            return proc

        if hook is None:
            on_started = None
        elif isinstance(hook, BaseException):
            def on_started():
                events.append("hook failed")
                raise hook
        else:
            def on_started():
                events.append(hook)

        with tempfile.TemporaryDirectory() as td:
            rc = self._exercise_ready_launch(
                Path(td), popen,
                arm=lambda: "owned-token",
                disarm=lambda _token: True,
                on_started=on_started,
            )
        return rc, events

    def test_hook_runs_after_the_spawn_and_before_the_wait(self):
        rc, events = self._play()
        self.assertEqual(rc, 0)
        self.assertEqual(events, ["spawned", "stepped aside", "waited"])

    def test_no_hook_is_the_launcher_free_launch(self):
        rc, events = self._play(hook=None)
        self.assertEqual(rc, 0)
        self.assertEqual(events, ["spawned", "waited"])

    def test_a_failing_hook_never_aborts_a_running_game(self):
        rc, events = self._play(hook=RuntimeError("Tk went away"))
        self.assertEqual(rc, 0)
        self.assertEqual(events, ["spawned", "hook failed", "waited"])
        self.assertTrue(any("could not step aside" in warning
                            for warning in self._warnings()))

    def test_a_spawn_that_fails_never_moves_the_window(self):
        with self.assertRaises(OSError):
            self._play(hook="stepped aside", popen_fails=True)
        self.assertEqual(self.events, ["spawned"])

    def test_launch_forwards_the_hook_through_the_launch_lock(self):
        hook = object()
        with mock.patch.object(launch, "launch_lock") as lock, \
                mock.patch.object(launch, "_launch_once",
                                  return_value=0) as once:
            lock.return_value.__enter__.return_value = (71, 72)
            self.assertEqual(launch.launch(on_started=hook), 0)
        once.assert_called_once_with((71, 72), on_started=hook)


class AutoInjectionLaunchTests(ReadyLaunchHarness, unittest.TestCase):
    """Auto-injection belongs to the launch, not to the window that asked."""

    def _play(self, settings, start_fails=False):
        events = []

        def popen(*_a, **_kw):
            events.append("spawned")
            proc = mock.Mock()
            proc.wait.side_effect = lambda timeout=None: (
                events.append("waited") or 0)
            return proc

        def start(passed):
            events.append("auto-inject")
            if start_fails:
                raise RuntimeError("no thread")
            self.injected_settings = passed
            return mock.Mock()

        self.injected_settings = None
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(launch, "start_auto_inject",
                                  side_effect=start) as started:
            rc = self._exercise_ready_launch(
                Path(td), popen,
                arm=lambda: "owned-token",
                disarm=lambda _token: True,
                extra_settings=settings,
            )
        return rc, events, started

    def test_a_launcher_free_launch_still_injects(self):
        """`bol play` is what a direct-launch shortcut and Game Mode run."""
        rc, events, started = self._play({"injector_auto_enable": True})
        self.assertEqual(rc, 0)
        self.assertEqual(events, ["spawned", "auto-inject", "waited"])
        started.assert_called_once()
        self.assertTrue(self.injected_settings["injector_auto_enable"])

    def test_the_watcher_is_offered_every_launch_and_declines_by_default(self):
        rc, events, started = self._play({})
        self.assertEqual(rc, 0)
        self.assertEqual(events, ["spawned", "auto-inject", "waited"])
        self.assertFalse(started.call_args.args[0].get("injector_auto_enable"))

    def test_a_watcher_that_cannot_start_never_aborts_a_running_game(self):
        rc, events, _started = self._play({"injector_auto_enable": True},
                                          start_fails=True)
        self.assertEqual(rc, 0)
        self.assertEqual(events, ["spawned", "auto-inject", "waited"])
        self.assertTrue(any("Automatic DLL injection" in warning
                            for warning in self._warnings()))


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()


class DiscordPresenceLaunchTests(ReadyLaunchHarness, unittest.TestCase):
    """The play session is announced while the game runs, and only then."""

    class _Process:
        @staticmethod
        def wait(timeout):
            return 0

    def _run(self, **kwargs):
        with tempfile.TemporaryDirectory() as td:
            return self._exercise_ready_launch(
                Path(td), lambda *a, **k: self._Process(),
                lambda: "token", lambda _token: True, **kwargs)

    def test_the_running_build_is_what_discord_is_told(self):
        self.assertEqual(
            self._run(extra_settings={"mc_edition": "preview",
                                      "mc_version": "1.26.40.1"}), 0)
        self.assertEqual(len(self.presence_calls), 1)
        (settings,), kwargs = self.presence_calls[0]
        self.assertEqual(settings["mc_version"], "1.26.40.1")
        self.assertEqual(settings["mc_edition"], "preview")
        self.assertIsInstance(kwargs["started_at"], float)

    def test_the_presence_is_taken_down_when_the_game_returns(self):
        # Nobody may be left showing as in-game by a launcher that is done
        # with the game: the teardown after it can take a while.
        self.assertEqual(self._run(), 0)
        self.presence.stop.assert_called_once_with()

    def test_a_game_that_never_started_is_never_announced(self):
        def popen(*_args, **_kwargs):
            raise OSError("no such file")

        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(OSError):
                self._exercise_ready_launch(
                    Path(td), popen, lambda: "token", lambda _token: True)
        self.assertEqual(self.presence_calls, [])
        self.presence.stop.assert_not_called()

