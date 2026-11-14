"""bol.gameevents: the game's Xbox Live events, forwarded (#152)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import gameevents

# One line as the WineGDK XGameEvent patch writes it, from a real session.
RECORD = {
    "time": "2026-11-12T13:55:01.123Z",
    "xuid": "2535458430309376",
    "scid": "4fc10100-5f7a-4470-899b-280835760c07",
    "session": "0D851161-F818-40C8-9007-F7CEFCBFB193",
    "event": "AwardAchievement",
    "dimensions": {"AchievementID": 40, "Cheevos": 1, "Mode": 0},
    "measurements": {"AchievementCount": 4},
}
TITLE = 0x35760C07


def _tickets():
    return {
        gameevents.UPLOADER_RELYING_PARTY: {
            "token": "uploader-token", "uhs": "111", "expiry_epoch": None},
        gameevents.PLAYER_RELYING_PARTY: {
            "token": "player-token", "uhs": "222", "expiry_epoch": None},
    }


class SwitchTests(unittest.TestCase):
    def test_on_by_default(self):
        self.assertTrue(gameevents.enabled({}, {}))

    def test_the_setting_turns_it_off(self):
        self.assertFalse(gameevents.enabled({"xbl_game_events": False}, {}))

    def test_the_environment_wins_for_one_run(self):
        self.assertFalse(gameevents.enabled({}, {"BOL_XBL_EVENTS": "0"}))
        self.assertTrue(gameevents.enabled({"xbl_game_events": False},
                                           {"BOL_XBL_EVENTS": "1"}))


class TitleAndPathTests(unittest.TestCase):
    def setUp(self):
        self.game = Path(tempfile.mkdtemp())

    def test_title_id_comes_from_the_game_config(self):
        (self.game / "MicrosoftGame.Config").write_text(
            '<?xml version="1.0"?>\r\n<Game><TitleId>35760C07</TitleId>'
            '<MSAAppId>0000000040159362</MSAAppId></Game>\r\n')
        self.assertEqual(gameevents.title_id(self.game), 896928775)

    def test_a_utf16_config_is_read_too(self):
        (self.game / "MicrosoftGame.Config").write_bytes(
            "<Game><TitleId>35760C07</TitleId></Game>".encode("utf-16"))
        self.assertEqual(gameevents.title_id(self.game), 896928775)

    def test_no_config_no_title(self):
        self.assertIsNone(gameevents.title_id(self.game))

    def test_the_game_sees_the_log_through_z(self):
        self.assertEqual(gameevents.wine_path("/home/a/logs/game-events.jsonl"),
                         "Z:\\home\\a\\logs\\game-events.jsonl")

    def test_prepare_points_the_engine_at_a_fresh_file(self):
        (self.game / "MicrosoftGame.Config").write_text(
            "<Game><TitleId>35760C07</TitleId></Game>")
        logs = Path(tempfile.mkdtemp())
        (logs / gameevents.LOG_NAME).write_text("old session\n")
        env = {}
        with mock.patch.object(gameevents, "LOGS", logs):
            title = gameevents.prepare(env, {}, self.game, True, {})
        self.assertEqual(title, 896928775)
        self.assertEqual(env["BOL_GAME_EVENT_LOG"],
                         gameevents.wine_path(logs / gameevents.LOG_NAME))
        self.assertEqual((logs / gameevents.LOG_NAME).read_text(), "")
        self.assertEqual((logs / gameevents.LOG_NAME).stat().st_mode & 0o777,
                         0o600)

    def test_prepare_does_nothing_offline_or_switched_off(self):
        (self.game / "MicrosoftGame.Config").write_text(
            "<Game><TitleId>35760C07</TitleId></Game>")
        for online, settings in ((False, {}), (True, {"xbl_game_events": False})):
            env = {}
            self.assertIsNone(gameevents.prepare(env, settings, self.game,
                                                 online, {}))
            self.assertNotIn("BOL_GAME_EVENT_LOG", env)


class EnvelopeTests(unittest.TestCase):
    def test_the_xsapi_cll_shape(self):
        line = gameevents.envelope(RECORD, TITLE, 7, 42, "2535458430309376")
        self.assertEqual(line["ver"], "2.1")
        self.assertEqual(line["name"],
                         "Microsoft.XboxLive.T896928775.AwardAchievement")
        self.assertEqual(line["iKey"], "P-XBL-T896928775")
        self.assertEqual(line["time"], RECORD["time"])
        self.assertEqual(line["seqNum"], 7)
        self.assertEqual(line["epoch"], "42")
        self.assertEqual(line["ext"]["android"]["tickets"],
                         ["2535458430309376"])
        data = line["data"]
        self.assertEqual(data["baseType"], "Microsoft.XboxLive.InGame")
        self.assertEqual(data["baseData"], {
            "name": "AwardAchievement",
            "serviceConfigId": RECORD["scid"],
            "playerSessionId": RECORD["session"],
            "titleId": "896928775",
            "userId": "2535458430309376",
            "properties": RECORD["dimensions"],
            "measurements": RECORD["measurements"],
        })

    def test_what_the_sdk_would_refuse_is_not_sent(self):
        for broken in ({**RECORD, "event": "1bad"}, {**RECORD, "xuid": "0"},
                       {**RECORD, "scid": ""}, "not a record"):
            self.assertIsNone(gameevents.envelope(broken, TITLE, 1, 1, "x"))


class ForwarderTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "game-events.jsonl"
        self.path.write_text("")
        self.posts = []
        self.status = (200, '{"acc":1,"rej":0}')
        self.minted = []
        tickets = _tickets()

        def mint(rp):
            self.minted.append(rp)
            return tickets[rp]

        def post(url, body, headers):
            self.posts.append((url, body, headers))
            return self.status

        self.forwarder = gameevents.Forwarder(self.path, TITLE, mint=mint,
                                              post=post)

    def _write(self, text):
        with open(self.path, "a") as handle:
            handle.write(text)

    def test_only_complete_lines_are_read(self):
        line = json.dumps(RECORD)
        self._write(line + "\n" + line[:20])
        self.assertEqual(self.forwarder.read_new(), 1)
        self._write(line[20:] + "\n")
        self.assertEqual(self.forwarder.read_new(), 1)
        self.assertEqual(self.forwarder.read_new(), 0)

    def test_an_upload_carries_both_tickets(self):
        self._write(json.dumps(RECORD) + "\n")
        self.forwarder.read_new()
        self.assertTrue(self.forwarder.flush())
        (url, body, headers), = self.posts
        self.assertEqual(url, gameevents.COLLECTOR_URL)
        self.assertEqual(headers["Content-Type"],
                         "application/x-json-stream; charset=utf-8")
        self.assertEqual(headers["X-AuthXToken"], "XBL3.0 x=111;uploader-token")
        self.assertEqual(headers["X-Tickets"],
                         '"2535458430309376"="x:XBL3.0 x=222;player-token"')
        sent = [json.loads(x) for x in body.decode().splitlines()]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["data"]["baseData"]["name"], "AwardAchievement")
        self.assertEqual((self.forwarder.sent, self.forwarder.awarded), (1, 1))
        self.assertIn("1 achievement report", self.forwarder.summary())

    def test_tickets_are_minted_once(self):
        for _ in range(3):
            self._write(json.dumps(RECORD) + "\n")
            self.forwarder.read_new()
            self.forwarder.flush()
        self.assertEqual(sorted(self.minted),
                         sorted([gameevents.PLAYER_RELYING_PARTY,
                                 gameevents.UPLOADER_RELYING_PARTY]))

    def test_a_refused_ticket_is_minted_again_and_nothing_is_lost(self):
        self._write(json.dumps(RECORD) + "\n")
        self.forwarder.read_new()
        self.status = (401, "")
        self.assertFalse(self.forwarder.flush())
        self.assertEqual(len(self.forwarder._pending), 1)
        self.status = (200, '{"acc":1,"rej":0}')
        self.assertTrue(self.forwarder.flush())
        self.assertEqual(len(self.minted), 4)
        self.assertEqual(self.forwarder.sent, 1)

    def test_no_network_keeps_the_events_for_later(self):
        self._write(json.dumps(RECORD) + "\n")
        self.forwarder.read_new()
        self.status = (None, "URLError")
        self.assertFalse(self.forwarder.flush())
        self.assertEqual(len(self.forwarder._pending), 1)
        self.assertIn("1 not sent", self.forwarder.summary())

    def test_no_ticket_means_no_upload(self):
        forwarder = gameevents.Forwarder(self.path, TITLE,
                                         mint=lambda rp: None,
                                         post=lambda *a: self.fail("posted"))
        self._write(json.dumps(RECORD) + "\n")
        forwarder.read_new()
        self.assertFalse(forwarder.flush())

    def test_unreadable_lines_are_counted_not_sent(self):
        self._write("{not json\n" + json.dumps({**RECORD, "xuid": "0"}) + "\n")
        self.forwarder.read_new()
        self.assertTrue(self.forwarder.flush())
        self.assertEqual(self.posts, [])
        self.assertEqual(self.forwarder.dropped, 2)

    def test_the_thread_sends_what_is_left_when_stopped(self):
        self.forwarder.start()
        self._write(json.dumps(RECORD) + "\n")
        self.forwarder.stop(timeout=10)
        self.assertEqual(self.forwarder.sent, 1)

    def test_without_a_title_it_is_inert(self):
        forwarder = gameevents.start_session(None, path=self.path)
        self.assertFalse(forwarder.active)
        forwarder.stop()
        self.assertIsNone(forwarder.summary())


if __name__ == "__main__":
    unittest.main()
