"""Tests for the Discord Rich Presence session.

The fake Discord below frames its side of the conversation by hand rather
than reusing bol.discord's own reader and writer: the wire format is the
thing being checked, and a test that spoke it through the code under test
would agree with that code however wrong both of them were.
"""
# SPDX-License-Identifier: MIT

import json
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from bol import discord

_HANDSHAKE = 0
_FRAME = 1
_TIMEOUT = 10


def _read_exactly(conn, size):
    chunks = b""
    while len(chunks) < size:
        block = conn.recv(size - len(chunks))
        if not block:
            return None
        chunks += block
    return chunks


def _read_frame(conn):
    header = _read_exactly(conn, 8)
    if header is None:
        return None
    opcode, length = struct.unpack("<II", header)
    body = _read_exactly(conn, length) if length else b"{}"
    if body is None:
        return None
    return opcode, json.loads(body.decode("utf-8"))


def _write_frame(conn, opcode, payload):
    body = json.dumps(payload).encode("utf-8")
    conn.sendall(struct.pack("<II", opcode, len(body)) + body)


class FakeDiscord(threading.Thread):
    """A Unix socket that answers a handshake and records what it is told."""

    def __init__(self, folder, refuse=(), answer=True):
        super().__init__(daemon=True)
        # Activity fields this "older client" refuses the whole activity for.
        self._refuse = set(refuse)
        self._answer = answer
        self.sent = []          # every (activity, pid) in arrival order
        self.path = Path(folder) / "discord-ipc-0"
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.path))
        self._server.listen(1)
        self._server.settimeout(_TIMEOUT)
        self.handshake = None
        self.activity = None
        self.announced = threading.Event()
        self.cleared = threading.Event()
        self.hung_up = threading.Event()

    def run(self):
        try:
            conn, _ = self._server.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(_TIMEOUT)
            while True:
                try:
                    frame = _read_frame(conn)
                except OSError:
                    break
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == _HANDSHAKE:
                    self.handshake = payload
                    _write_frame(conn, _FRAME, {
                        "cmd": "DISPATCH", "evt": "READY", "data": {}})
                    continue
                if payload.get("cmd") != "SET_ACTIVITY":
                    continue
                args = payload.get("args") or {}
                activity = args.get("activity")
                if activity is not None and self._refuse & set(activity):
                    _write_frame(conn, _FRAME, {
                        "cmd": "SET_ACTIVITY", "evt": "ERROR",
                        "nonce": payload.get("nonce"),
                        "data": {"code": 4000, "message": "child fails"}})
                    continue
                if activity is not None:
                    self.sent.append((activity, args.get("pid")))
                    self.activity = activity
                    self.announced.set()
                else:
                    self.cleared.set()
                if self._answer:
                    try:
                        _write_frame(conn, _FRAME, {
                            "cmd": "SET_ACTIVITY", "evt": None,
                            "nonce": payload.get("nonce"), "data": activity})
                    except OSError:
                        break   # the clear was the last word: it hung up
        self.hung_up.set()

    def close(self):
        try:
            self._server.close()
        except OSError:
            pass


class SocketDiscoveryTests(unittest.TestCase):
    def test_the_runtime_directory_is_searched_before_tmp(self):
        paths = discord.socket_candidates(
            {"XDG_RUNTIME_DIR": "/run/user/1000"})
        self.assertEqual(paths[0], Path("/run/user/1000/discord-ipc-0"))
        self.assertIn(Path("/tmp/discord-ipc-0"), paths)

    def test_sandboxed_discord_builds_are_searched_too(self):
        paths = set(discord.socket_candidates({"XDG_RUNTIME_DIR": "/run/u"}))
        # A Flatpak or Snap Discord keeps the same socket under a runtime
        # directory of its own; missing these reads as "it does not work".
        self.assertIn(
            Path("/run/u/app/com.discordapp.Discord/discord-ipc-0"), paths)
        self.assertIn(Path("/run/u/snap.discord/discord-ipc-0"), paths)

    def test_a_second_client_gets_a_later_index(self):
        paths = set(discord.socket_candidates({"XDG_RUNTIME_DIR": "/run/u"}))
        self.assertIn(Path("/run/u/discord-ipc-9"), paths)


class ActivityTests(unittest.TestCase):
    def test_the_build_being_played_is_what_is_shown(self):
        activity = discord.session_activity(
            {"mc_edition": "release", "mc_version": "1.26.32.2"},
            started_at=1_700_000_000)
        self.assertEqual(activity["details"], "Minecraft Bedrock 1.26.32.2")
        self.assertEqual(activity["timestamps"]["start"], 1_700_000_000)

    def test_preview_is_named_as_preview(self):
        activity = discord.session_activity({"mc_edition": "preview",
                                             "mc_version": "1.26.40.1"})
        self.assertEqual(activity["details"], "Minecraft Preview 1.26.40.1")

    def test_an_empty_settings_file_still_produces_a_line(self):
        # Settings written before the version was recorded, or a game folder
        # from outside the managed tree: still a session, just a vaguer one.
        self.assertEqual(discord.session_activity({})["details"],
                         "Minecraft Bedrock")

    def test_the_session_carries_the_project_links(self):
        buttons = discord.session_activity({})["buttons"]
        self.assertLessEqual(len(buttons), 2)  # Discord accepts two
        for button in buttons:
            self.assertTrue(button["url"].startswith("https://"))
            self.assertLessEqual(len(button["label"]), 32)

    def test_one_button_leads_straight_to_the_github_repository(self):
        from bol.config import REPO_URL
        self.assertTrue(REPO_URL.startswith("https://github.com/"))
        urls = [b["url"] for b in discord.session_activity({})["buttons"]]
        self.assertIn(REPO_URL, urls)

    def test_the_member_list_reads_the_launchers_name(self):
        # 0 = the application's name ("Bedrock on Linux"), not the edition.
        self.assertEqual(
            discord.session_activity({})["status_display_type"], 0)

    def test_the_version_the_launcher_reads_wins_over_the_build_tag(self):
        # settings["mc_version"] is the catalogue's tag ("26.44"), which is
        # not a version anyone recognises.
        activity = discord.session_activity(
            {"mc_version": "26.44"}, version="1.26.44.3")
        self.assertEqual(activity["details"], "Minecraft Bedrock 1.26.44.3")

    def test_every_title_of_the_family_is_named(self):
        for edition, name in (({"id": "dungeons"}, "Minecraft Dungeons"),
                              ({"id": "legends"}, "Minecraft Legends"),
                              ({"id": "education"}, "Minecraft Education"),
                              ({"id": "uwp"}, "Minecraft Bedrock"),
                              ({"id": "preview"}, "Minecraft Preview")):
            self.assertEqual(discord.game_name(edition), name)
        self.assertEqual(discord.game_name({"id": "new", "name": "Spin-off"}),
                         "Spin-off")


class EnablementTests(unittest.TestCase):
    def test_presence_is_on_unless_it_was_turned_off(self):
        self.assertTrue(discord.presence_enabled({}, {}))
        self.assertTrue(discord.presence_enabled({"discord_presence": True},
                                                 {}))
        self.assertFalse(discord.presence_enabled({"discord_presence": False},
                                                  {}))

    def test_the_environment_overrides_the_switch_either_way(self):
        off = {"BOL_DISCORD_PRESENCE": "0"}
        on = {"BOL_DISCORD_PRESENCE": "1"}
        self.assertFalse(discord.presence_enabled({}, off))
        self.assertTrue(discord.presence_enabled({"discord_presence": False},
                                                 on))

    def test_a_build_without_an_application_announces_nothing(self):
        with mock.patch.object(discord, "DISCORD_APP_ID", ""):
            session = discord.start_session({}, {})
        self.addCleanup(session.stop)
        self.assertFalse(session.active)


class SessionTests(unittest.TestCase):
    def setUp(self):
        # Short path: a Unix socket address is limited to about 100 bytes.
        self._tmp = tempfile.TemporaryDirectory(prefix="bol-ipc-", dir="/tmp")
        self.addCleanup(self._tmp.cleanup)
        self.folder = self._tmp.name

    def test_a_play_session_is_announced_and_then_taken_down(self):
        fake = FakeDiscord(self.folder)
        self.addCleanup(fake.close)
        fake.start()

        session = discord.start_session(
            {"mc_edition": "release", "mc_version": "1.26.32.2"},
            environ={"XDG_RUNTIME_DIR": self.folder,
                     "BOL_DISCORD_APP_ID": "424242"},
            started_at=1_700_000_000)
        self.addCleanup(session.stop)

        self.assertTrue(fake.announced.wait(_TIMEOUT),
                        "Discord was never told about the play session")
        self.assertEqual(fake.handshake.get("v"), 1)
        self.assertEqual(fake.handshake.get("client_id"), "424242")
        self.assertEqual(fake.activity["details"],
                         "Minecraft Bedrock 1.26.32.2")
        self.assertEqual(fake.activity["timestamps"]["start"], 1_700_000_000)

        session.stop()
        self.assertTrue(fake.cleared.wait(_TIMEOUT),
                        "the presence was not cleared when the game closed")
        self.assertTrue(fake.hung_up.wait(_TIMEOUT),
                        "the socket was left open after the session")

    def test_a_session_with_no_discord_running_stops_at_once(self):
        # The retry between attempts is measured in tens of seconds; stopping
        # has to interrupt it, or every launch teardown would wait it out.
        with mock.patch.object(discord, "socket_candidates",
                               return_value=[Path(self.folder) / "absent"]):
            session = discord.start_session(
                {}, environ={"XDG_RUNTIME_DIR": self.folder,
                             "BOL_DISCORD_APP_ID": "424242"})
            self.addCleanup(session.stop)
            self.assertTrue(session.active)
            time.sleep(0.2)          # let the thread reach its retry wait
            began = time.monotonic()
            session.stop(timeout=_TIMEOUT)
            waited = time.monotonic() - began
        self.assertLess(waited, discord._RETRY_SECONDS)
        self.assertIsNone(session._thread)

    def test_the_switch_keeps_the_launcher_off_discord(self):
        fake = FakeDiscord(self.folder)
        self.addCleanup(fake.close)
        fake.start()

        session = discord.start_session(
            {"discord_presence": False},
            environ={"XDG_RUNTIME_DIR": self.folder,
                     "BOL_DISCORD_APP_ID": "424242"})
        self.addCleanup(session.stop)
        self.assertFalse(session.active)
        self.assertFalse(fake.announced.wait(1))
        self.assertIsNone(fake.handshake)


class FallbackAndGamePidTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bol-ipc-", dir="/tmp")
        self.addCleanup(self._tmp.cleanup)
        self.folder = self._tmp.name
        self.env = {"XDG_RUNTIME_DIR": self.folder,
                    "BOL_DISCORD_APP_ID": "424242"}

    def test_an_older_client_still_shows_the_session(self):
        # A Discord that predates a field refuses the whole activity; the
        # session must not end up showing nothing.
        fake = FakeDiscord(self.folder, refuse={"details_url"})
        self.addCleanup(fake.close)
        fake.start()
        session = discord.start_session(
            {}, environ=self.env, version="1.26.50.25")
        self.addCleanup(session.stop)
        self.assertTrue(fake.announced.wait(_TIMEOUT))
        self.assertNotIn("details_url", fake.activity)
        self.assertEqual(fake.activity["details"],
                         "Minecraft Bedrock 1.26.50.25")
        self.assertEqual(len(fake.activity["buttons"]), 2)

    def test_a_client_that_never_answers_is_not_waited_on_forever(self):
        fake = FakeDiscord(self.folder, answer=False)
        self.addCleanup(fake.close)
        fake.start()
        with mock.patch.object(discord, "_REPLY_TIMEOUT", 0.2):
            session = discord.start_session({}, environ=self.env)
            self.addCleanup(session.stop)
            self.assertTrue(session.announced.wait(_TIMEOUT))
        self.assertIn("details_url", fake.activity)

    def test_the_activity_moves_to_the_games_pid_once_it_runs(self):
        # Discord recognises Minecraft.Windows.exe itself; naming that
        # process ties its "Minecraft" to this presence.
        fake = FakeDiscord(self.folder)
        self.addCleanup(fake.close)
        fake.start()
        game = {"pid": None}
        with mock.patch.object(discord, "_GAME_PID_POLL", 0.05), \
                mock.patch.object(discord, "_POLL_SECONDS", 0.05), \
                mock.patch.object(discord, "_in_flatpak", return_value=False), \
                mock.patch.object(discord, "find_game_pid",
                                  side_effect=lambda *a: game["pid"]):
            session = discord.start_session(
                {}, environ=self.env, exe_name="Minecraft.Windows.exe")
            self.addCleanup(session.stop)
            self.assertTrue(fake.announced.wait(_TIMEOUT))
            self.assertEqual(fake.sent[0][1], session._pid)
            game["pid"] = 999_999
            deadline = time.monotonic() + _TIMEOUT
            while time.monotonic() < deadline and fake.sent[-1][1] != 999_999:
                time.sleep(0.05)
        self.assertEqual(fake.sent[-1][1], 999_999)
        self.assertEqual(fake.sent[-1][0], fake.sent[0][0])

    def test_inside_a_flatpak_the_launchers_pid_is_kept(self):
        with mock.patch.object(discord, "_in_flatpak", return_value=True):
            session = discord.start_session(
                {}, environ=self.env, exe_name="Minecraft.Windows.exe")
        self.addCleanup(session.stop)
        self.assertIsNone(session._game_pid)


class FindGamePidTests(unittest.TestCase):
    """A fake /proc: what Wine leaves for a running Windows game."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "self").mkdir()       # not a pid: must be skipped

    def _proc(self, pid, ppid, comm, argv0):
        folder = self.root / str(pid)
        folder.mkdir()
        (folder / "comm").write_text(comm + "\n")
        (folder / "cmdline").write_bytes(argv0.encode() + b"\0--arg\0")
        (folder / "stat").write_text(
            f"{pid} ({comm}) S {ppid} {pid} {pid} 0 -1\n")

    def test_wines_process_name_is_recognised(self):
        self._proc(10, 1, "python3", "/usr/bin/python3")
        self._proc(20, 10, "Minecraft.Windo",
                   "Z:\\games\\Minecraft.Windows.exe")
        self.assertEqual(
            discord.find_game_pid("Minecraft.Windows.exe",
                                  proc_root=self.root), 20)

    def test_the_command_line_is_enough_when_the_name_is_not(self):
        self._proc(30, 1, "wine64-preload", "C:\\x\\Dungeons.exe")
        self.assertEqual(
            discord.find_game_pid("Dungeons.exe", proc_root=self.root), 30)

    def test_this_launchers_own_game_wins_over_another_profiles(self):
        self._proc(100, 1, "umu-run", "/bin/umu-run")
        self._proc(110, 100, "Minecraft.Windo", "Minecraft.Windows.exe")
        self._proc(200, 1, "umu-run", "/bin/umu-run")
        self._proc(210, 200, "Minecraft.Windo", "Minecraft.Windows.exe")
        self.assertEqual(
            discord.find_game_pid("Minecraft.Windows.exe", ancestor=100,
                                  proc_root=self.root), 110)

    def test_nothing_running_is_none(self):
        self._proc(10, 1, "bash", "/bin/bash")
        self.assertIsNone(discord.find_game_pid("Minecraft.Windows.exe",
                                                proc_root=self.root))
        self.assertIsNone(discord.find_game_pid("", proc_root=self.root))


class ArtworkTests(unittest.TestCase):
    """The image beside the session.

    Discord takes each of these as either an asset key uploaded to the
    application or a full URL it fetches itself. The distinction matters
    because an application can be configured and announcing sessions while its
    Art Assets are still empty -- which is what "it works but there is no
    logo" was: a key with no artwork behind it, sent and silently ignored.
    """

    def _assets(self, large, small):
        with mock.patch.object(discord, "DISCORD_LARGE_IMAGE", large), \
                mock.patch.object(discord, "DISCORD_SMALL_IMAGE", small):
            return discord.session_activity({}).get("assets")

    def test_the_shipped_default_is_a_url_not_a_bare_key(self):
        # The default has to work with no developer-portal step at all.
        large = discord.session_activity({})["assets"]["large_image"]
        self.assertTrue(large.startswith("https://"), large)

    def test_the_default_image_is_one_this_project_publishes(self):
        from bol.config import SITE_URL
        self.assertTrue(
            discord.session_activity({})["assets"]["large_image"]
            .startswith(SITE_URL))

    def test_an_asset_key_is_still_accepted(self):
        assets = self._assets("logo", "linux")
        self.assertEqual(assets["large_image"], "logo")
        self.assertEqual(assets["small_image"], "linux")

    def test_the_large_image_is_labelled_with_the_app_name(self):
        from bol.config import PRETTY
        self.assertEqual(self._assets("logo", "")["large_text"], PRETTY)

    def test_an_unset_small_image_is_left_out_entirely(self):
        # Not sent as "": Discord reads an empty string as a key, finds
        # nothing, and leaves a blank frame where the badge should be.
        assets = self._assets("logo", "")
        self.assertNotIn("small_image", assets)
        self.assertNotIn("small_text", assets)

    def test_no_artwork_at_all_sends_no_assets_block(self):
        self.assertIsNone(self._assets("", ""))

    def test_the_session_still_carries_its_other_fields_without_artwork(self):
        with mock.patch.object(discord, "DISCORD_LARGE_IMAGE", ""), \
                mock.patch.object(discord, "DISCORD_SMALL_IMAGE", ""):
            activity = discord.session_activity(
                {"mc_edition": "release", "mc_version": "26.32"})
        self.assertIn("26.32", activity["details"])
        self.assertEqual(len(activity["buttons"]), 2)


class DoctorSummaryTests(unittest.TestCase):
    def test_each_reason_for_seeing_nothing_reads_differently(self):
        no_app = discord.presence_summary({}, {})
        self.assertIn("no Discord application", no_app)
        env = {"BOL_DISCORD_APP_ID": "424242"}
        self.assertIn("off", discord.presence_summary(
            {"discord_presence": False}, env))
        with tempfile.TemporaryDirectory(prefix="bol-ipc-", dir="/tmp") as tmp:
            with mock.patch.object(discord, "socket_candidates",
                                   return_value=[Path(tmp) / "absent"]):
                self.assertIn("not running",
                              discord.presence_summary({}, env))


if __name__ == "__main__":
    unittest.main()
