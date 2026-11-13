"""Several profiles playing at once: the locks, the GPU records, the launch.

One game at a time used to be enforced in two places -- an exclusive lock on
the shared game files and a single GPU-safety marker. Both now tell another
profile's running session apart from an incident, and these tests pin where
that line is.
"""
# SPDX-License-Identifier: MIT

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import cli, gpu_safety, instances, launch, mods, prefix, profiles
from bol.log import BolError
from tests.test_launch import ReadyLaunchHarness


def _live_pid(test):
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"])
    test.addCleanup(child.wait)
    test.addCleanup(child.kill)
    return child.pid


class ConcurrentGpuMarkerTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.marker = self.root / ".gpu-launch-in-progress.json"
        self.home = self.root / "home"
        self.home.mkdir()
        for patcher in (
                mock.patch.object(gpu_safety, "GPU_LAUNCH_MARKER",
                                  self.marker),
                mock.patch.object(gpu_safety, "_boot_id",
                                  return_value="boot-now"),
                mock.patch.object(gpu_safety, "DATA", self.home),
                mock.patch.object(gpu_safety, "warn")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _other_session(self, home=None, **fields):
        """The marker another profile's launcher wrote for a running game."""
        pid = _live_pid(self)
        state = {
            "version": gpu_safety._STATE_VERSION, "engine_rev": "native23",
            "phase": "running", "token": "a" * 32, "boot_id": "boot-now",
            "launcher_pid": pid,
            "launcher_start": gpu_safety._process_start(pid), "created": 1,
        }
        if home is not None:
            state["home"] = str(home)
        state.update(fields)
        self.marker.write_text(json.dumps(state))
        return state

    def _siblings(self):
        return gpu_safety._session_markers(self.marker)

    def test_a_second_session_records_itself_beside_the_first(self):
        self._other_session()
        token = gpu_safety.arm_gpu_launch()
        siblings = self._siblings()
        self.assertEqual(len(siblings), 1)
        state = json.loads(siblings[0].read_text())
        self.assertEqual(state["token"], token)
        self.assertEqual(state["home"], str(self.home))
        self.assertEqual(json.loads(self.marker.read_text())["token"],
                         "a" * 32)

    def test_each_session_clears_only_its_own_record(self):
        self._other_session()
        token = gpu_safety.arm_gpu_launch()
        self.assertTrue(gpu_safety.mark_gpu_wrapper_returned(token))
        self.assertEqual(
            json.loads(self._siblings()[0].read_text())["phase"],
            "wrapper_returned")
        self.assertEqual(json.loads(self.marker.read_text())["phase"],
                         "running")
        self.assertTrue(gpu_safety.disarm_gpu_launch(token))
        self.assertEqual(self._siblings(), [])
        self.assertTrue(self.marker.exists())
        self.assertFalse(gpu_safety.disarm_gpu_launch(token))

    def test_the_first_session_keeps_the_markers_own_name(self):
        token = gpu_safety.arm_gpu_launch()
        self.assertEqual(self._siblings(), [])
        self.assertEqual(json.loads(self.marker.read_text())["token"], token)
        self.assertTrue(gpu_safety.disarm_gpu_launch(token))
        self.assertFalse(self.marker.exists())

    def test_another_profiles_running_session_is_not_a_problem(self):
        self._other_session()
        self.assertIsNone(gpu_safety.interrupted_launch_problem())

    def test_an_interrupted_launch_still_refuses_a_second_session(self):
        self._other_session(launcher_pid=424242, launcher_start=1)
        self.assertIn("did not return cleanly",
                      gpu_safety.interrupted_launch_problem())
        with self.assertRaisesRegex(BolError, "still marked interrupted"):
            gpu_safety.arm_gpu_launch()
        self.assertEqual(self._siblings(), [])

    def test_a_previous_boot_marker_still_refuses(self):
        self._other_session(boot_id="boot-before")
        self.assertIn("last reboot",
                      gpu_safety.interrupted_launch_problem())
        with self.assertRaises(BolError):
            gpu_safety.arm_gpu_launch()

    def test_an_interrupted_sibling_is_a_problem_too(self):
        self._other_session()
        stale = gpu_safety._session_marker(self.marker, "b" * 32)
        stale.write_text(json.dumps({
            "version": gpu_safety._STATE_VERSION, "engine_rev": "native23",
            "phase": "running", "token": "b" * 32,
            "boot_id": "boot-before", "launcher_pid": 424242, "created": 1}))
        self.assertIn("last reboot",
                      gpu_safety.interrupted_launch_problem())
        self.assertEqual(
            gpu_safety._acknowledgement_marker_scope(self.marker), "active")

    def test_acknowledging_a_previous_boot_clears_every_record(self):
        for token, target in (("a" * 32, self.marker),
                              ("b" * 32, gpu_safety._session_marker(
                                  self.marker, "b" * 32))):
            target.write_text(json.dumps({
                "version": gpu_safety._STATE_VERSION,
                "engine_rev": "native23", "phase": "running",
                "token": token, "boot_id": "boot-before",
                "launcher_pid": 424242, "created": 1}))
        self.assertEqual(
            gpu_safety._acknowledgement_marker_scope(self.marker), "previous")
        with mock.patch.object(gpu_safety, "_kernel_driver_fault_scope",
                               return_value=None):
            gpu_safety.acknowledge_gpu_safety_incident(
                ack_path=self.root / "ack.json")
        self.assertEqual(gpu_safety._all_markers(self.marker)[1:], [])
        self.assertFalse(self.marker.exists())

    def test_a_dead_launchers_record_goes_once_its_profile_lock_is_free(self):
        other = self.root / "other-profile"
        other.mkdir()
        lock = other / ".launch.lock"
        lock.touch()
        self._other_session(home=other, launcher_pid=424242,
                            launcher_start=1)
        # Its game's wrapper still holds the profile's launch lock.
        holder = os.open(lock, os.O_RDWR)
        self.addCleanup(lambda: os.close(holder) if holder else None)
        fcntl.flock(holder, fcntl.LOCK_EX)
        self.assertFalse(gpu_safety.retire_idle_current_boot_marker())
        self.assertTrue(self.marker.exists())
        os.close(holder)
        holder = None
        self.assertTrue(gpu_safety.retire_idle_current_boot_marker())
        self.assertFalse(self.marker.exists())

    def test_this_profiles_own_orphan_is_retired_as_before(self):
        self._other_session(home=self.home, launcher_pid=424242,
                            launcher_start=1)
        self.assertTrue(gpu_safety.retire_idle_current_boot_marker())

    def test_a_running_session_is_never_retired(self):
        self._other_session(home=self.root / "other")
        self.assertFalse(gpu_safety.retire_idle_current_boot_marker())
        self.assertTrue(self.marker.exists())

    def test_a_malformed_home_is_not_retired(self):
        state = self._other_session(launcher_pid=424242, launcher_start=1)
        state["home"] = 7
        self.marker.write_text(json.dumps(state))
        self.assertFalse(gpu_safety.retire_idle_current_boot_marker())

    def test_the_override_never_replaces_a_running_session(self):
        self._other_session()
        token = gpu_safety.arm_gpu_launch(
            environ={"BOL_ALLOW_UNSAFE_GPU": "1"})
        self.assertEqual(json.loads(self.marker.read_text())["token"],
                         "a" * 32)
        self.assertEqual(len(self._siblings()), 1)
        self.assertTrue(gpu_safety.disarm_gpu_launch(token))

    def test_a_token_cannot_name_a_file_elsewhere(self):
        self._other_session()
        self.assertFalse(gpu_safety.disarm_gpu_launch("../../etc/passwd"))


class JoinableLaunchLockTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        (self.root / "shared/games").mkdir(parents=True)
        patcher = mock.patch.object(prefix, "info")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _profile(self, name):
        home = self.root / name
        home.mkdir(exist_ok=True)
        return (mock.patch.object(prefix, "DATA", home),
                mock.patch.object(prefix, "GAMES",
                                  self.root / "shared/games"))

    def _lock(self, name, **kwargs):
        data, games = self._profile(name)
        with data, games:
            return prefix.launch_lock(**kwargs)

    def _enter(self, name, **kwargs):
        data, games = self._profile(name)
        data.start()
        games.start()
        try:
            manager = prefix.launch_lock(**kwargs)
            locks = manager.__enter__()
        finally:
            games.stop()
            data.stop()
        self.addCleanup(manager.__exit__, None, None, None)
        return manager, locks

    def test_the_first_game_holds_the_shared_files_alone(self):
        _manager, locks = self._enter("one", join=True)
        self.assertTrue(locks.sole)
        self.assertEqual(len(locks), 2)

    def test_a_second_profile_joins_once_the_first_has_prepared(self):
        _manager, first = self._enter("one", join=True)
        first.share()
        _manager, second = self._enter("two", join=True)
        self.assertFalse(second.sole)

    def test_a_second_profile_waits_while_the_first_prepares(self):
        _manager, first = self._enter("one", join=True)
        naps = []

        def sleep(_seconds):
            naps.append(1)
            if len(naps) == 3:
                first.share()

        with mock.patch.object(prefix.time, "sleep", sleep):
            _manager, second = self._enter("two", join=True)
        self.assertFalse(second.sole)
        self.assertEqual(len(naps), 3)

    def test_the_wait_for_an_update_is_bounded(self):
        _manager, _first = self._enter("one", join=True)
        clock = iter(range(0, 10 ** 6, 60))
        with mock.patch.object(prefix.time, "sleep"), \
                mock.patch.object(prefix.time, "monotonic",
                                  lambda: next(clock)):
            data, games = self._profile("two")
            with data, games, self.assertRaisesRegex(
                    BolError, "shared Minecraft files are in use"):
                with prefix.launch_lock(join=True):
                    pass

    def test_the_same_profile_cannot_play_twice(self):
        _manager, first = self._enter("one", join=True)
        first.share()
        data, games = self._profile("one")
        with data, games, self.assertRaisesRegex(
                BolError, "already in progress"):
            with prefix.launch_lock(join=True):
                pass

    def test_an_installation_is_refused_while_any_game_runs(self):
        _manager, first = self._enter("one", join=True)
        first.share()
        data, games = self._profile("two")
        with data, games, self.assertRaisesRegex(
                BolError, "shared Minecraft files are in use"):
            with prefix.shared_assets_lock("install", exclusive=True):
                pass

    def test_the_doctors_lock_stays_exclusive(self):
        _manager, first = self._enter("one", join=True)
        first.share()
        data, games = self._profile("two")
        with data, games, self.assertRaisesRegex(
                BolError, "shared Minecraft files are in use"):
            with prefix.launch_lock():
                pass

    def test_the_wrapper_keeps_a_shared_session_alive(self):
        manager, first = self._enter("one", join=True)
        first.share()
        child = subprocess.Popen(
            [sys.executable, "-c",
             "import sys,time;sys.stdout.write('ready\\n');"
             "sys.stdout.flush();time.sleep(60)"],
            stdout=subprocess.PIPE, text=True, pass_fds=tuple(first))
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.addCleanup(child.stdout.close)
        self.assertEqual(child.stdout.readline().strip(), "ready")
        manager.__exit__(None, None, None)
        data, games = self._profile("two")
        with data, games:
            # Another profile still joins; an installation is still refused.
            with prefix.launch_lock(join=True) as second:
                self.assertFalse(second.sole)
            with self.assertRaisesRegex(BolError, "in use"):
                with prefix.shared_assets_lock("install", exclusive=True):
                    pass

    def test_plain_descriptors_count_as_a_sole_session(self):
        self.assertTrue(getattr((71, 72), "sole", True))
        self.assertTrue(prefix.SessionLocks((71, 72)).sole)


class JoinedLaunchTests(ReadyLaunchHarness, unittest.TestCase):
    """A launch beside a running one writes nothing the other one uses."""

    def _run(self, sole, build_in_use=False, hidden=False):
        self.shared = []

        class Locks(tuple):
            pass

        locks = Locks((71, 72))
        locks.sole = sole
        locks.share = lambda: self.shared.append("shared")
        process = mock.Mock()
        process.wait.return_value = 0
        process.pid = 4242
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(launch.instances, "game_dir_in_use",
                                  return_value=build_in_use), \
                mock.patch.object(launch.mods, "prepare_launch") as prepare, \
                mock.patch.object(launch.mods, "join_launch") as join, \
                mock.patch.object(launch, "_prepare_launch_engine",
                                  create=True) as engine:
            self.engine = engine
            self.prepare, self.join = prepare, join
            rc = self._exercise_ready_launch(
                Path(td), popen=lambda *a, **k: process,
                arm=lambda: "t" * 32, disarm=lambda _t: True,
                lock_fds=locks, swap_guard=True)
            self.hide = self.launch_mocks["hide_signin_button"]
            self.branding = self.launch_mocks["apply_branding"]
        return rc

    def test_a_sole_session_prepares_everything_then_shares(self):
        self.assertEqual(self._run(sole=True), 0)
        self.assertTrue(self.hide.called)
        self.assertTrue(self.branding.called)
        self.assertTrue(self.prepare.called)
        self.assertFalse(self.join.called)
        self.assertEqual(self.shared, ["shared"])

    def test_a_joined_session_leaves_a_running_build_alone(self):
        self.assertEqual(self._run(sole=False, build_in_use=True), 0)
        self.assertFalse(self.hide.called)
        self.assertFalse(self.branding.called)
        self.assertFalse(self.prepare.called)
        self.assertTrue(self.join.called)

    def test_a_joined_session_prepares_a_build_nobody_runs(self):
        self.assertEqual(self._run(sole=False, build_in_use=False), 0)
        self.assertTrue(self.hide.called)
        self.assertTrue(self.prepare.called)

    def test_the_session_is_recorded_with_what_was_asked(self):
        self._run(sole=True)
        args, _kwargs = self.instance_calls[0]
        self.assertEqual(args[6], False)        # hidden
        self.assertEqual(Path(args[2]).name, "Minecraft.Windows.exe")


class EngineInUseTests(unittest.TestCase):
    def test_an_engine_in_use_is_not_rewritten(self):
        with mock.patch.object(launch, "prefix_processes", return_value=[]), \
                mock.patch.object(launch, "ensure_winegdk") as ensure, \
                mock.patch.object(launch, "patch_proton") as patch, \
                mock.patch.object(launch, "_prepare_graphics_engine") as gfx, \
                mock.patch.object(launch, "info"):
            self.assertIsNone(
                launch._prepare_launch_engine(engine_in_use=True))
        self.assertFalse(ensure.called or patch.called or gfx.called)

    def test_this_profiles_own_running_prefix_is_still_refused(self):
        with mock.patch.object(launch, "prefix_processes", return_value=[9]), \
                self.assertRaises(BolError):
            launch._prepare_launch_engine(engine_in_use=True)


class OpenInstanceTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name) / "instances"
        real = instances.instances_root
        patcher = mock.patch.object(
            instances, "instances_root",
            lambda base=None: real(self.root if base is None else base))
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("info", "warn"):
            patcher = mock.patch.object(launch, name)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.warn = launch.warn

    def _open(self, env, hidden=False, use_gamescope=False):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        with mock.patch.object(launch, "current_profile_info",
                               return_value={"name": "Bot 1",
                                             "slug": "bot-1"}), \
                mock.patch.object(launch, "build_version",
                                  return_value="1.26.60.5"), \
                mock.patch.object(launch.instances.WindowKeeper, "start"):
            session = launch._open_instance(
                child, env, "/content/Minecraft.Windows.exe",
                "/games/release/1.26.60.5", {"id": "release"}, "Minecraft",
                hidden, use_gamescope)
        self.addCleanup(session.close)
        return session

    def test_a_session_is_listed_while_it_lasts(self):
        session = self._open({"DISPLAY": ":0"}, hidden=True)
        listed = instances.list_instances()
        self.assertEqual(len(listed), 1)
        item = listed[0]
        self.assertEqual((item.profile, item.edition, item.version),
                         ("Bot 1", "release", "1.26.60.5"))
        self.assertTrue(item.hidden and item.can_hide)
        self.assertEqual(item.wm_class, "minecraft.windows.exe")
        self.assertFalse(self.warn.called)
        session.close()
        self.assertEqual(instances.list_instances(), [])

    def test_hidden_on_wayland_or_gamescope_warns_instead_of_failing(self):
        session = self._open({"PROTON_ENABLE_WAYLAND": "1"}, hidden=True)
        self.assertEqual(session.record.backend, instances.BACKEND_WAYLAND)
        self.assertIn("Wayland", self.warn.call_args.args[0])
        session = self._open({"DISPLAY": ":0"}, hidden=True,
                             use_gamescope=True)
        self.assertEqual(session.record.backend, instances.BACKEND_GAMESCOPE)

    def test_a_registry_that_cannot_be_written_never_fails_the_launch(self):
        with mock.patch.object(launch.instances, "register",
                               side_effect=OSError("read-only")):
            session = self._open({"DISPLAY": ":0"})
        self.assertIsNone(session.record)
        session.close()
        self.assertTrue(self.warn.called)


class JoinModsTests(unittest.TestCase):
    def test_the_loader_in_place_is_used_and_nothing_is_written(self):
        with tempfile.TemporaryDirectory() as td:
            game = Path(td)
            env = {"WINEDLLOVERRIDES": "cryptbase=n,b"}
            with mock.patch.object(mods, "info"):
                self.assertFalse(mods.join_launch(game, env))
                self.assertEqual(env["WINEDLLOVERRIDES"], "cryptbase=n,b")
                (game / mods.LOADER_NAME).write_bytes(mods.loader_bytes())
                before = sorted(p.name for p in game.iterdir())
                self.assertTrue(mods.join_launch(game, env))
            self.assertIn(mods.LOADER_OVERRIDE, env["WINEDLLOVERRIDES"])
            self.assertEqual(sorted(p.name for p in game.iterdir()), before)


class StartProfileGameTests(unittest.TestCase):
    def test_a_profile_plays_in_a_process_of_its_own(self):
        with mock.patch("subprocess.Popen") as popen, \
                mock.patch.object(profiles, "launcher_executable",
                                  return_value="/opt/bol.AppImage"):
            profiles.start_profile_game("/data/profiles/bot-1", hidden=True)
        argv = popen.call_args.args[0]
        self.assertEqual(argv, ["/opt/bol.AppImage", "play", "--hidden"])
        options = popen.call_args.kwargs
        self.assertEqual(options["env"]["BOL_HOME"],
                         str(Path("/data/profiles/bot-1").resolve()))
        self.assertTrue(options["start_new_session"])

    def test_a_python_checkout_runs_under_this_interpreter(self):
        with mock.patch("subprocess.Popen") as popen, \
                mock.patch.object(profiles, "launcher_executable",
                                  return_value="/src/bedrock-on-linux.py"):
            profiles.start_profile_game("/data/profiles/bot-1")
        self.assertEqual(popen.call_args.args[0],
                         [sys.executable, "/src/bedrock-on-linux.py", "play"])

    def test_account_and_build_are_read_from_the_profiles_own_files(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "profiles" / "bot-1"
            (home / "msa").mkdir(parents=True)
            self.assertEqual(profiles.profile_account(home),
                             {"signed_in": False, "gamertag": None})
            (home / "msa/token.json").write_text('{"refresh_token": "r"}')
            self.assertEqual(profiles.profile_account(home),
                             {"signed_in": True, "gamertag": None})
            (home / "winegdk-preauth").mkdir()
            (home / "winegdk-preauth/device.json").write_text(
                '{"xbl_gamertag": "BotOne"}')
            self.assertEqual(profiles.profile_account(home)["gamertag"],
                             "BotOne")
            self.assertIsNone(profiles.profile_build(home))
            build = Path(td) / "games/release/1.26.60.5"
            build.mkdir(parents=True)
            (home / "settings.json").write_text(
                json.dumps({"game_dir": str(build)}))
            self.assertEqual(profiles.profile_build(home),
                             "Release 1.26.60.5")


class InstancesCliTests(unittest.TestCase):
    def _item(self, **fields):
        base = dict(id="bot-1-abc123", profile="Bot 1", slug="bot-1",
                    version="1.26.60.5", launcher_pid=os.getpid(),
                    game_pid=os.getpid(), started=0.0, display=":0",
                    wm_class="minecraft.windows.exe")
        base.update(fields)
        return instances.Instance(**base)

    def _run(self, *argv, running=()):
        import contextlib
        import io
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["bedrock-on-linux", *argv]), \
                mock.patch("bol.util._ensure_xdg_storage"), \
                mock.patch.object(instances, "list_instances",
                                  return_value=list(running)), \
                mock.patch.object(instances, "set_hidden",
                                  return_value=1) as hide, \
                mock.patch.object(instances, "close_instance",
                                  return_value="closed") as close, \
                contextlib.redirect_stdout(output):
            self.hide, self.close = hide, close
            try:
                cli.main()
                code = 0
            except SystemExit as exited:
                code = exited.code
        return code, output.getvalue()

    def test_the_list_names_every_running_game(self):
        code, printed = self._run("instances", running=[
            self._item(), self._item(id="default-ffffff", profile="Default",
                                     slug="default", hidden=True)])
        self.assertEqual(code, 0)
        lines = printed.splitlines()
        self.assertIn("PROFILE", lines[0])
        self.assertRegex(lines[1], r"bot-1-abc123\s+Bot 1\s+Minecraft "
                                   r"1\.26\.60\.5\s+visible")
        self.assertRegex(lines[2], r"default-ffffff\s+Default.*hidden")

    def test_an_empty_list_says_so(self):
        _code, printed = self._run("instances", "list")
        self.assertIn("No Minecraft instance is running", printed)

    def test_hide_show_and_close_reach_the_named_instance(self):
        item = self._item()
        self._run("instances", "hide", "Bot 1", running=[item])
        self.hide.assert_called_once_with(item, True)
        self._run("instances", "show", "bot-1-abc", running=[item])
        self.hide.assert_called_once_with(item, False)
        self._run("instances", "close", "--force", "bot-1", running=[item])
        self.close.assert_called_once_with(item, force=True)

    def test_all_reaches_every_instance(self):
        items = [self._item(), self._item(id="default-ffffff",
                                          profile="Default", slug="default")]
        self._run("instances", "close", "--all", running=items)
        self.assertEqual(self.close.call_count, 2)

    def test_an_unknown_or_missing_name_is_an_error(self):
        code, printed = self._run("instances", "close", "nobody",
                                  running=[self._item()])
        self.assertEqual(code, 1)
        self.assertIn("No running instance matches", printed)
        code, _printed = self._run("instances", "hide",
                                   running=[self._item()])
        self.assertEqual(code, 1)
        self.assertFalse(self.hide.called)

    def test_play_hidden_reaches_the_launch(self):
        with mock.patch.object(sys, "argv",
                               ["bedrock-on-linux", "play", "--hidden"]), \
                mock.patch("bol.util._ensure_xdg_storage"), \
                mock.patch.object(cli, "msa_signed_in", return_value=True), \
                mock.patch.object(cli, "update_game_before_play"), \
                mock.patch.object(cli.supervision, "install"), \
                mock.patch.object(cli, "launch") as started:
            cli.main()
        started.assert_called_once_with(editor=False, hidden=True)


if __name__ == "__main__":
    unittest.main()
