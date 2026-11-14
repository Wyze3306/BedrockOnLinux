"""The list of running games, and what hiding, showing and closing one does.

No game is started here. A "game" is a sleeping child process, its windows
are a fake X tree, and the registry lives in a temporary directory.
"""
# SPDX-License-Identifier: MIT

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from bol import instances, x11
from bol.log import BolError

GAME = "minecraft.windows.exe"


def _sleeper(test):
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"])
    test.addCleanup(child.wait)
    test.addCleanup(child.kill)
    return child


class FakeWindows:
    """An X tree: a root, window-manager frames, and clients inside them."""

    def __init__(self):
        self._next = 100
        self.tree = {1: []}
        self.classes = {}
        self.pids = {}
        self.mapped = {}
        self.override = set()
        self.props = {}
        self.closed = []

    def add(self, wm_class=GAME, pid=None, mapped=True, framed=True,
            override=False):
        parent = 1
        if framed:
            parent = self._new(1)
        window = self._new(parent)
        self.classes[window] = (wm_class, wm_class)
        if pid is not None:
            self.pids[window] = pid
        self.mapped[window] = mapped
        if override:
            self.override.add(window)
        return window

    def _new(self, parent):
        self._next += 1
        self.tree.setdefault(parent, []).append(self._next)
        self.tree[self._next] = []
        return self._next

    def root(self):
        return 1

    def children(self, window):
        return tuple(self.tree.get(window, ()))

    def wm_classes(self, window):
        return self.classes.get(window, ())

    def is_override_redirect(self, window):
        return window in self.override

    def is_presentable(self, window):
        return bool(self.mapped.get(window)) and window not in self.override

    def is_mapped(self, window):
        return bool(self.mapped.get(window))

    def pid(self, window):
        return self.pids.get(window)

    def set_cardinal(self, window, name, value):
        self.props[(window, name)] = value
        return True

    def cardinal(self, window, name):
        return self.props.get((window, name))

    def delete_property(self, window, name):
        self.props.pop((window, name), None)

    def withdraw(self, window):
        self.mapped[window] = False
        return True

    def map(self, window):
        self.mapped[window] = True

    def ask_to_close(self, window):
        self.closed.append(window)
        return True

    def flush(self):
        pass


class WindowControlTests(unittest.TestCase):
    def test_hide_withdraws_only_the_windows_of_the_named_processes(self):
        windows = FakeWindows()
        mine = windows.add(pid=10)
        theirs = windows.add(pid=20)
        other_app = windows.add(wm_class="firefox", pid=10)
        self.assertEqual(
            x11.hide_instance_windows(GAME, {10}, windows=windows), 1)
        self.assertFalse(windows.mapped[mine])
        self.assertTrue(windows.mapped[theirs])
        self.assertTrue(windows.mapped[other_app])
        self.assertEqual(windows.props[(mine, x11.HIDDEN_PROPERTY)], 1)

    def test_show_maps_back_what_was_hidden_and_nothing_else(self):
        windows = FakeWindows()
        game = windows.add(pid=10)
        # Wine keeps helper windows of the same class unmapped for itself.
        helper = windows.add(pid=10, mapped=False)
        x11.hide_instance_windows(GAME, {10}, windows=windows)
        self.assertEqual(
            x11.show_instance_windows(GAME, {10}, windows=windows), 1)
        self.assertTrue(windows.mapped[game])
        self.assertFalse(windows.mapped[helper])
        self.assertNotIn((game, x11.HIDDEN_PROPERTY), windows.props)

    def test_a_window_without_a_frame_is_found_too(self):
        windows = FakeWindows()
        game = windows.add(pid=10, framed=False)
        self.assertEqual(
            x11.hide_instance_windows(GAME, {10}, windows=windows), 1)
        self.assertFalse(windows.mapped[game])

    def test_no_pid_filter_means_every_window_of_the_class(self):
        windows = FakeWindows()
        windows.add(pid=10)
        windows.add()
        self.assertEqual(
            x11.count_instance_windows(GAME, None, windows=windows), 2)
        self.assertEqual(
            x11.count_instance_windows(GAME, {10}, windows=windows), 1)

    def test_tooltips_and_menus_are_left_alone(self):
        windows = FakeWindows()
        windows.add(pid=10, override=True)
        self.assertEqual(
            x11.hide_instance_windows(GAME, {10}, windows=windows), 0)

    def test_close_asks_visible_and_hidden_windows_but_not_helpers(self):
        windows = FakeWindows()
        shown = windows.add(pid=10)
        hidden = windows.add(pid=10)
        windows.withdraw(hidden)
        windows.set_cardinal(hidden, x11.HIDDEN_PROPERTY, 1)
        windows.add(pid=10, mapped=False)
        self.assertEqual(
            x11.close_instance_windows(GAME, {10}, windows=windows), 2)
        self.assertEqual(sorted(windows.closed), sorted([shown, hidden]))

    def test_no_display_is_zero_and_never_an_error(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(x11.hide_instance_windows(GAME, {1}), 0)
            self.assertEqual(x11.show_instance_windows(GAME, {1}), 0)
            self.assertEqual(x11.close_instance_windows(GAME, {1}), 0)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name) / "instances"

    def _register(self, game=None, launcher=None, **kwargs):
        game = game or _sleeper(self)
        options = dict(
            profile="Bot 1", slug="bot-1", home=self._dir.name + "/bot-1",
            prefix=self._dir.name + "/bot-1/compatdata/pfx",
            game_pid=game.pid, version="1.26.60.5",
            game_dir=self._dir.name + "/games/release/1.26.60.5",
            display=":0", wm_class="Minecraft.Windows.exe", root=self.root)
        options.update(kwargs)
        if launcher is not None:
            options["launcher_pid"] = launcher.pid
        return instances.register(**options)

    def test_a_registered_game_is_listed_with_what_was_recorded(self):
        record = self._register(hidden=True)
        listed = instances.list_instances(self.root)
        self.assertEqual([item.id for item in listed], [record.id])
        item = listed[0]
        self.assertEqual((item.profile, item.version, item.hidden),
                         ("Bot 1", "1.26.60.5", True))
        self.assertEqual(item.wm_class, GAME)
        self.assertEqual(item.launcher_pid, os.getpid())
        self.assertTrue(item.can_hide)
        self.assertTrue(record.id.startswith("bot-1-"))
        mode = (self.root / f"{record.id}.json").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_a_game_outlives_its_launcher_in_the_list(self):
        launcher = _sleeper(self)
        record = self._register(launcher=launcher)
        launcher.kill()
        launcher.wait()
        listed = instances.list_instances(self.root)
        self.assertEqual([item.id for item in listed], [record.id])
        self.assertFalse(listed[0].launcher_alive)
        self.assertTrue(listed[0].game_alive)
        self.assertEqual(listed[0].pid, record.game_pid)

    def test_a_record_nobody_is_left_behind_is_dropped_and_removed(self):
        launcher, game = _sleeper(self), _sleeper(self)
        record = self._register(game=game, launcher=launcher)
        for process in (launcher, game):
            process.kill()
            process.wait()
        self.assertEqual(instances.list_instances(self.root), [])
        self.assertFalse((self.root / f"{record.id}.json").exists())

    def test_a_recycled_pid_is_not_the_recorded_process(self):
        launcher, game = _sleeper(self), _sleeper(self)
        record = self._register(game=game, launcher=launcher)
        path = self.root / f"{record.id}.json"
        data = json.loads(path.read_text())
        data["launcher_start"] += 1
        data["game_start"] += 1
        path.write_text(json.dumps(data))
        self.assertEqual(instances.list_instances(self.root, prune=False), [])

    def test_a_record_from_another_boot_is_not_running(self):
        record = self._register()
        path = self.root / f"{record.id}.json"
        data = json.loads(path.read_text())
        data["boot_id"] = "another-boot"
        path.write_text(json.dumps(data))
        self.assertEqual(instances.list_instances(self.root), [])

    def test_malformed_records_are_ignored_and_kept(self):
        self.root.mkdir(parents=True)
        for name, content in (("junk.json", "{"),
                              ("wrong.json", json.dumps({"schema": 99})),
                              ("typed.json", json.dumps(
                                  {"schema": 1, "id": "typed",
                                   "launcher_pid": "12"}))):
            (self.root / name).write_text(content)
        self.assertEqual(instances.list_instances(self.root), [])
        self.assertTrue((self.root / "junk.json").exists())

    def test_no_directory_is_an_empty_list(self):
        self.assertEqual(instances.list_instances(self.root), [])

    def test_unregister_removes_the_record(self):
        record = self._register()
        self.assertTrue(instances.unregister(record.id, self.root))
        self.assertEqual(instances.list_instances(self.root), [])
        self.assertFalse(instances.unregister(record.id, self.root))

    def test_an_identifier_cannot_name_a_file_elsewhere(self):
        self.assertFalse(instances.unregister("../escape", self.root))
        self.assertIsNone(instances.get("../escape", self.root))

    def test_find_by_id_prefix_and_profile(self):
        one = self._register()
        two = self._register(profile="Bot 2", slug="bot-2")
        self.assertEqual(instances.find(one.id, self.root).id, one.id)
        self.assertEqual(instances.find("Bot 2", self.root).id, two.id)
        self.assertEqual(instances.find("bot-2", self.root).id, two.id)
        with self.assertRaisesRegex(BolError, "matches 2"):
            instances.find("bot-", self.root)
        with self.assertRaisesRegex(BolError, "No running instance"):
            instances.find("nobody", self.root)

    def test_game_dir_in_use_ignores_the_asking_profile(self):
        record = self._register()
        self.assertTrue(instances.game_dir_in_use(
            record.game_dir, self.root, exclude_home="/elsewhere"))
        self.assertFalse(instances.game_dir_in_use(
            record.game_dir, self.root, exclude_home=record.home))
        self.assertFalse(instances.game_dir_in_use(
            "/another/build", self.root))

    def test_for_home_lists_one_profile(self):
        one = self._register()
        self._register(profile="Bot 2", slug="bot-2",
                       home=self._dir.name + "/bot-2")
        self.assertEqual(
            [item.id for item in instances.for_home(one.home, self.root)],
            [one.id])

    # -- hide / show ------------------------------------------------------
    def test_hiding_changes_the_record_and_the_windows(self):
        record = self._register()
        windows = FakeWindows()
        game = windows.add(pid=4242)
        changed = instances.set_hidden(
            record, True, self.root, windows=windows, scan=lambda _i: [4242])
        self.assertEqual(changed, 1)
        self.assertFalse(windows.mapped[game])
        self.assertTrue(instances.get(record.id, self.root).hidden)
        changed = instances.set_hidden(
            record, False, self.root, windows=windows, scan=lambda _i: [4242])
        self.assertEqual(changed, 1)
        self.assertTrue(windows.mapped[game])
        self.assertFalse(instances.get(record.id, self.root).hidden)

    def test_two_instances_never_touch_each_others_windows(self):
        one = self._register()
        two = self._register(profile="Bot 2", slug="bot-2",
                             prefix=self._dir.name + "/bot-2/pfx")
        windows = FakeWindows()
        first, second = windows.add(pid=11), windows.add(pid=22)
        scan = lambda item: [11] if item.id == one.id else [22]  # noqa: E731
        instances.set_hidden(two, True, self.root, windows=windows, scan=scan)
        self.assertTrue(windows.mapped[first])
        self.assertFalse(windows.mapped[second])

    def test_unseen_processes_fall_back_to_the_class_for_a_lone_game(self):
        record = self._register()
        windows = FakeWindows()
        game = windows.add(pid=999)
        instances.set_hidden(record, True, self.root, windows=windows,
                             scan=lambda _i: [])
        self.assertFalse(windows.mapped[game])

    def test_unseen_processes_hide_nothing_when_two_games_share_a_display(self):
        one = self._register()
        self._register(profile="Bot 2", slug="bot-2")
        windows = FakeWindows()
        game = windows.add(pid=999)
        self.assertEqual(instances.set_hidden(
            one, True, self.root, windows=windows, scan=lambda _i: []), 0)
        self.assertTrue(windows.mapped[game])

    def test_a_wayland_or_gamescope_game_cannot_be_hidden(self):
        for backend, words in ((instances.BACKEND_WAYLAND, "Wayland"),
                               (instances.BACKEND_GAMESCOPE, "gamescope")):
            record = self._register(backend=backend)
            self.assertFalse(record.can_hide)
            with self.assertRaisesRegex(BolError, words):
                instances.set_hidden(record, True, self.root)
            self.assertFalse(instances.get(record.id, self.root).hidden)

    def test_hiding_a_game_that_is_gone_says_so(self):
        record = self._register()
        instances.unregister(record.id, self.root)
        with self.assertRaisesRegex(BolError, "no longer running"):
            instances.set_hidden(record, True, self.root,
                                 windows=FakeWindows())

    def test_the_keeper_hides_windows_built_after_the_request(self):
        record = self._register(hidden=True)
        applied = []
        keeper = instances.WindowKeeper(
            record.id, self.root, interval=0.01,
            apply=lambda item, _root: applied.append(item.id))
        keeper.start()
        self.addCleanup(keeper.stop)
        deadline = time.monotonic() + 5
        while len(applied) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertGreaterEqual(len(applied), 2)
        # Shown from somewhere else: the keeper stops hiding.
        instances.update(record.id, self.root, hidden=False)
        time.sleep(0.05)
        seen = len(applied)
        time.sleep(0.1)
        self.assertEqual(len(applied), seen)
        keeper.stop()
        self.assertFalse(keeper.is_alive())

    # -- close ------------------------------------------------------------
    def _close(self, record, windows, alive_for, **kwargs):
        """Close with a game that leaves after `alive_for` polls."""
        polls = {"n": 0}
        stopped = []

        def scan(_item):
            return [4242]

        def image(_pid):
            polls["n"] += 1
            return GAME if polls["n"] <= alive_for else "wineserver"

        clock = {"t": 0.0}

        def sleep(seconds):
            clock["t"] += seconds

        result = instances.close_instance(
            record, self.root, windows=windows, scan=scan, image=image,
            stop=stopped.append, sleep=sleep, clock=lambda: clock["t"],
            **kwargs)
        return result, stopped

    def test_close_asks_the_window_and_leaves_the_rest_to_the_launcher(self):
        record = self._register()
        windows = FakeWindows()
        game = windows.add(pid=4242)
        result, stopped = self._close(record, windows, alive_for=3)
        self.assertEqual(result, "closed")
        self.assertEqual(windows.closed, [game])
        self.assertEqual(stopped, [])
        # Still listed: its own session removes it once the teardown is done.
        self.assertIsNotNone(instances.get(record.id, self.root))

    def test_a_game_that_ignores_the_request_is_stopped(self):
        record = self._register()
        windows = FakeWindows()
        windows.add(pid=4242)
        result, stopped = self._close(record, windows, alive_for=10 ** 6,
                                      grace=2.0)
        self.assertEqual(result, "stopped")
        self.assertEqual([item.id for item in stopped], [record.id])

    def test_a_game_with_no_window_is_stopped_at_once(self):
        record = self._register()
        result, stopped = self._close(record, FakeWindows(), alive_for=10)
        self.assertEqual(result, "stopped")
        self.assertEqual(len(stopped), 1)

    def test_force_never_asks(self):
        record = self._register()
        windows = FakeWindows()
        windows.add(pid=4242)
        result, stopped = self._close(record, windows, alive_for=10,
                                      force=True)
        self.assertEqual((result, windows.closed), ("stopped", []))
        self.assertEqual(len(stopped), 1)

    def test_an_orphaned_game_is_cleaned_up_and_unlisted(self):
        launcher = _sleeper(self)
        record = self._register(launcher=launcher)
        launcher.kill()
        launcher.wait()
        windows = FakeWindows()
        windows.add(pid=4242)
        result, stopped = self._close(record, windows, alive_for=2)
        self.assertEqual(result, "closed")
        # Nobody is left to reap what the game leaves in its prefix.
        self.assertEqual(len(stopped), 1)
        self.assertIsNone(instances.get(record.id, self.root))

    def test_closing_one_real_process_tree_leaves_the_other_running(self):
        """End to end with real processes standing in for two games."""
        one_game, two_game = _sleeper(self), _sleeper(self)
        one = self._register(game=one_game)
        two = self._register(game=two_game, profile="Bot 2", slug="bot-2")
        pids = {one.id: one_game.pid, two.id: two_game.pid}

        def stop(item):
            os.kill(pids[item.id], 15)

        result = instances.close_instance(
            one, self.root, windows=FakeWindows(),
            scan=lambda item: [pids[item.id]], image=lambda _pid: GAME,
            stop=stop)
        self.assertEqual(result, "stopped")
        one_game.wait(timeout=5)
        self.assertIsNone(two_game.poll())
        self.assertEqual(
            [item.id for item in instances.list_instances(self.root)
             if item.game_alive], [two.id])


class UptimeTests(unittest.TestCase):
    def test_durations_read_like_a_table(self):
        self.assertEqual(instances.format_uptime(5), "5 s")
        self.assertEqual(instances.format_uptime(125), "2 min")
        self.assertEqual(instances.format_uptime(3900), "1 h 05")
        self.assertEqual(instances.format_uptime(-3), "0 s")


if __name__ == "__main__":
    unittest.main()
