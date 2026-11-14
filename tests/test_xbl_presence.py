"""Tests for the Xbox Live presence heartbeat (#238, #243).

The fake service below is a real HTTP server and reads the request by hand
rather than through bol.presence: the wire format is the thing being checked,
and what Xbox Live accepts was established by measuring the live service --
``{"state": "active"}`` and nothing else, because naming the title in the body
is answered 400 ArgumentError. A test that built its expectation out of the
code under test would agree with that code however wrong both of them were.
"""
# SPDX-License-Identifier: MIT

import http.server
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from bol import presence

_TIMEOUT = 10


class FakePresenceService(threading.Thread):
    """Records every request, and answers whatever the test asked it to."""

    def __init__(self, statuses=None):
        super().__init__(daemon=True)
        self.requests = []
        self._statuses = list(statuses or [])
        self._lock = threading.Lock()
        service = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _record(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                with service._lock:
                    service.requests.append({
                        "method": self.command,
                        "path": self.path,
                        # HTTP header names are case-insensitive and
                        # urllib re-cases them; compare on one casing.
                        "headers": {k.lower(): v
                                    for k, v in self.headers.items()},
                        "body": body.decode("utf-8") if body else "",
                    })
                    status = (service._statuses.pop(0)
                              if service._statuses else 200)
                return status

            def do_POST(self):
                status = self._record()
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                self._record()
                payload = json.dumps({"state": "Online"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                pass

        self._server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        host, port = self._server.server_address
        self.url = "http://%s:%d/users/xuid(%%s)/titles/current" % (host, port)
        self.state_url = "http://%s:%d/users/xuid(%%s)?level=all" % (host, port)

    def run(self):
        self._server.serve_forever(poll_interval=0.01)

    def close(self):
        self._server.shutdown()
        self._server.server_close()

    def wait_for(self, count, timeout=_TIMEOUT):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if len(self.requests) >= count:
                    return True
            time.sleep(0.01)
        return False

    def bodies(self):
        with self._lock:
            return [json.loads(r["body"]) for r in self.requests if r["body"]]


def _credentials():
    return presence.Credentials("a-token", "1234", "2535416128702156",
                                expiry=int(time.time()) + 3600)


class ServiceTestCase(unittest.TestCase):
    """A test whose requests go to a fake service instead of Xbox Live."""

    def start_service(self, statuses=None):
        service = FakePresenceService(statuses)
        service.start()
        self.addCleanup(service.close)
        patch = mock.patch.multiple(presence,
                                    PRESENCE_URL=service.url,
                                    PRESENCE_STATE_URL=service.state_url)
        patch.start()
        self.addCleanup(patch.stop)
        return service


class CredentialTests(unittest.TestCase):
    def _payload(self, **overrides):
        payload = {
            "xbl_token": "a-token",
            "xbl_uhs": "1234",
            "xbl_xuid": "2535416128702156",
            "xbl_token_expiry_epoch": int(time.time()) + 3600,
        }
        payload.update(overrides)
        return payload

    def _write(self, payload):
        path = Path(tempfile.mkdtemp()) / "device.json"
        path.write_text(json.dumps(payload) if payload is not None else "{")
        return path

    def test_reads_a_complete_payload(self):
        credentials = presence.load_credentials(self._write(self._payload()))
        self.assertIsNotNone(credentials)
        self.assertEqual(credentials.uhs, "1234")
        self.assertEqual(credentials.xuid, "2535416128702156")
        self.assertTrue(credentials.usable())

    def test_numeric_fields_are_accepted_as_numbers(self):
        credentials = presence.load_credentials(
            self._write(self._payload(xbl_uhs=1234,
                                      xbl_xuid=2535416128702156)))
        self.assertIsNotNone(credentials)
        self.assertEqual(credentials.uhs, "1234")
        self.assertEqual(credentials.xuid, "2535416128702156")

    def test_missing_field_yields_nothing_to_publish(self):
        for field in ("xbl_token", "xbl_uhs", "xbl_xuid"):
            with self.subTest(field=field):
                self.assertIsNone(presence.load_credentials(
                    self._write(self._payload(**{field: ""}))))

    def test_a_non_numeric_xuid_is_refused(self):
        self.assertIsNone(presence.load_credentials(
            self._write(self._payload(xbl_xuid="me"))))

    def test_absent_or_torn_payloads_are_not_an_error(self):
        self.assertIsNone(presence.load_credentials(self._write(None)))
        self.assertIsNone(presence.load_credentials(self._write([])))
        self.assertIsNone(presence.load_credentials(
            Path(tempfile.mkdtemp()) / "missing.json"))

    def test_an_expired_token_is_not_usable(self):
        credentials = presence.load_credentials(self._write(self._payload(
            xbl_token_expiry_epoch=int(time.time()) - 1)))
        self.assertFalse(credentials.usable())

    def test_an_unknown_expiry_stays_usable(self):
        # Legacy payloads predate the epoch field; a token that has in fact
        # expired is answered 401, which ends the session on its own.
        payload = self._payload()
        payload.pop("xbl_token_expiry_epoch")
        credentials = presence.load_credentials(self._write(payload))
        self.assertIsNone(credentials.expiry)
        self.assertTrue(credentials.usable())


class EnablementTests(unittest.TestCase):
    def test_on_by_default(self):
        self.assertTrue(presence.presence_enabled({}, {}))

    def test_settings_turn_it_off(self):
        self.assertFalse(presence.presence_enabled({"xbl_presence": False},
                                                   {}))

    def test_environment_overrides_settings_both_ways(self):
        self.assertFalse(presence.presence_enabled(
            {"xbl_presence": True}, {"BOL_XBL_PRESENCE": "0"}))
        self.assertTrue(presence.presence_enabled(
            {"xbl_presence": False}, {"BOL_XBL_PRESENCE": "1"}))


class WireFormatTests(ServiceTestCase):
    def test_the_heartbeat_is_what_xbox_live_accepts(self):
        service = self.start_service()
        self.assertEqual(presence.write_state(_credentials(), "active"), 200)
        self.assertTrue(service.wait_for(1))
        request = service.requests[0]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["path"],
                         "/users/xuid(2535416128702156)/titles/current")
        self.assertEqual(request["headers"]["authorization"],
                         "XBL3.0 x=1234;a-token")
        self.assertEqual(request["headers"]["x-xbl-contract-version"], "3")
        self.assertEqual(request["headers"]["content-type"],
                         "application/json")
        self.assertEqual(json.loads(request["body"]), {"state": "active"})

    def test_the_body_never_names_a_title(self):
        # The service reads the title from the token's own claim and answers
        # 400 ArgumentError to a body that carries an "id"; that is also what
        # keeps this from being able to claim any other game.
        service = self.start_service()
        presence.write_state(_credentials(), "active")
        self.assertTrue(service.wait_for(1))
        self.assertEqual(set(service.bodies()[0]), {"state"})

    def test_a_refusal_is_reported_not_raised(self):
        self.start_service(statuses=[401])
        self.assertEqual(presence.write_state(_credentials(), "active"), 401)

    def test_an_unreachable_service_is_reported_as_no_status(self):
        with mock.patch.object(presence, "PRESENCE_URL",
                               "http://127.0.0.1:1/users/xuid(%s)"):
            self.assertIsNone(presence.write_state(_credentials(), "active"))

    def test_reading_the_state_back(self):
        self.start_service()
        self.assertEqual(presence.read_state(_credentials()), "Online")


class SessionTests(ServiceTestCase):
    def _session(self, service, credentials=None):
        session = presence.Session(credentials or _credentials())
        self.addCleanup(session.stop)
        return session

    def test_a_session_publishes_and_then_takes_itself_down(self):
        service = self.start_service()
        with mock.patch.object(presence, "HEARTBEAT_SECONDS", 60):
            session = self._session(service)
            session.start()
            self.assertTrue(service.wait_for(1))
            self.assertTrue(session.announced.wait(_TIMEOUT))
            session.stop()
        self.assertTrue(service.wait_for(2))
        self.assertEqual(service.bodies(),
                         [{"state": "active"}, {"state": "inactive"}])
        self.assertFalse(session.announced.is_set())

    def test_it_keeps_beating_while_the_game_runs(self):
        service = self.start_service()
        with mock.patch.object(presence, "HEARTBEAT_SECONDS", 0.02):
            session = self._session(service)
            session.start()
            self.assertTrue(service.wait_for(3))
            session.stop()
        self.assertEqual(service.bodies()[-1], {"state": "inactive"})

    def test_a_rejected_token_ends_the_session_instead_of_retrying(self):
        service = self.start_service(statuses=[401])
        with mock.patch.object(presence, "HEARTBEAT_SECONDS", 0.02):
            session = self._session(service)
            session.start()
            self.assertTrue(service.wait_for(1))
            session._thread.join(_TIMEOUT)
            self.assertFalse(session._thread.is_alive())
        # Nothing was ever published, so nothing is taken down either.
        self.assertEqual(service.bodies(), [{"state": "active"}])

    def test_a_lost_minute_does_not_end_the_session(self):
        # A session lasts hours; a Wi-Fi blip must not be what leaves someone
        # invisible for the rest of it.
        service = self.start_service(statuses=[503, 503])
        with mock.patch.multiple(presence, HEARTBEAT_SECONDS=0.02,
                                 RETRY_SECONDS=0.01):
            session = self._session(service)
            session.start()
            self.assertTrue(session.announced.wait(_TIMEOUT))
            session.stop()
        self.assertEqual(service.bodies()[:3],
                         [{"state": "active"}] * 3)

    def test_a_malformed_request_stops_rather_than_repeating_itself(self):
        service = self.start_service(statuses=[400])
        with mock.patch.multiple(presence, HEARTBEAT_SECONDS=0.02,
                                 RETRY_SECONDS=0.01):
            session = self._session(service)
            session.start()
            session._thread.join(_TIMEOUT)
            self.assertFalse(session._thread.is_alive())
        self.assertEqual(service.bodies(), [{"state": "active"}])

    def test_a_rate_limit_is_a_slow_down_not_a_stop(self):
        service = self.start_service(statuses=[429, 429])
        with mock.patch.multiple(presence, HEARTBEAT_SECONDS=0.02,
                                 RETRY_SECONDS=0.01):
            session = self._session(service)
            session.start()
            self.assertTrue(session.announced.wait(_TIMEOUT))
            session.stop()

    def test_an_expired_token_publishes_nothing(self):
        service = self.start_service()
        expired = presence.Credentials("a-token", "1234", "2535416128702156",
                                       expiry=int(time.time()) - 1)
        session = self._session(service, expired)
        session.start()
        session._thread.join(_TIMEOUT)
        self.assertEqual(service.requests, [])

    def test_an_inert_session_starts_and_stops_without_a_thread(self):
        session = presence.Session()
        self.assertFalse(session.active)
        self.assertIs(session.start(), session)
        session.stop()


class StartSessionTests(ServiceTestCase):
    def _payload_path(self, **overrides):
        payload = {
            "xbl_token": "a-token",
            "xbl_uhs": "1234",
            "xbl_xuid": "2535416128702156",
            "xbl_token_expiry_epoch": int(time.time()) + 3600,
        }
        payload.update(overrides)
        path = Path(tempfile.mkdtemp()) / "device.json"
        path.write_text(json.dumps(payload))
        return path

    def test_it_publishes_when_everything_is_in_place(self):
        service = self.start_service()
        with mock.patch.object(presence, "HEARTBEAT_SECONDS", 60):
            session = presence.start_session({}, {}, self._payload_path())
            self.addCleanup(session.stop)
            self.assertTrue(session.active)
            self.assertTrue(service.wait_for(1))

    def test_switched_off_means_nothing_is_sent(self):
        service = self.start_service()
        session = presence.start_session({"xbl_presence": False}, {},
                                         self._payload_path())
        session.stop()
        self.assertFalse(session.active)
        self.assertEqual(service.requests, [])

    def test_an_unusable_payload_is_inert_rather_than_fatal(self):
        service = self.start_service()
        session = presence.start_session(
            {}, {}, self._payload_path(
                xbl_token_expiry_epoch=int(time.time()) - 1))
        session.stop()
        self.assertFalse(session.active)
        self.assertEqual(service.requests, [])

    def test_a_broken_payload_never_raises_into_a_launch(self):
        path = Path(tempfile.mkdtemp()) / "device.json"
        path.write_text("{ not json")
        session = presence.start_session({}, {}, path)
        session.stop()
        self.assertFalse(session.active)


class SocialSnapshotTests(ServiceTestCase):
    """What Xbox Live is asked when someone reports "I can't join"."""

    def _service_with_people(self, people):
        service = self.start_service()
        payload = json.dumps({"people": people}).encode()
        # The fake answers every GET with the presence body, so the friends
        # list is served by patching the one call that wants it instead.
        original = presence._request

        def routed(credentials, method, url, body=None, **kwargs):
            if url == presence.PEOPLEHUB_URL:
                return 200, payload.decode()
            return original(credentials, method, url, body, **kwargs)

        patch = mock.patch.object(presence, "_request", routed)
        patch.start()
        self.addCleanup(patch.stop)
        return service

    # The shape PeopleHub answers with for the multiplayersummary decoration,
    # measured 2026-11-12: every friend has one, joinable or not.
    NOTHING_TO_JOIN = {"joinableActivities": [], "partyDetails": [],
                       "inParty": 0}
    HOSTING = {"joinableActivities": [{
        "sequenceNumber": "639300829398969168", "titleId": 896928775,
        "joinRestriction": "Followed", "maxPlayers": 30, "currentPlayers": 1,
        "groupId": "DBDC", "platform": None,
        "activationInfo": {"win32": {"titleId": 1739947436}}}],
        "partyDetails": [], "inParty": 0}

    def test_it_asks_for_the_decoration_peoplehub_knows(self):
        # "multiplayer" is silently ignored: every summary comes back null.
        self.assertIn("/decoration/multiplayersummary,", presence.PEOPLEHUB_URL)

    def test_it_counts_friends_and_the_sessions_among_them(self):
        self._service_with_people([
            {"gamertag": "a", "multiplayerSummary": self.NOTHING_TO_JOIN},
            {"gamertag": "b", "multiplayerSummary": self.HOSTING},
            {"gamertag": "c"},
        ])
        snapshot = presence.social_snapshot(_credentials())
        self.assertIsNone(snapshot.error)
        self.assertEqual(snapshot.state, "Online")
        self.assertEqual(snapshot.friends, 3)
        self.assertEqual(snapshot.in_session, 1)

    def test_a_summary_with_nothing_to_join_is_not_a_session(self):
        self._service_with_people([
            {"gamertag": "a", "multiplayerSummary": self.NOTHING_TO_JOIN},
            {"gamertag": "b", "multiplayerSummary": {}},
            {"gamertag": "c", "multiplayerSummary": None},
        ])
        self.assertEqual(presence.social_snapshot(_credentials()).in_session, 0)

    def test_a_joinable_session_of_another_game_is_not_a_world(self):
        roblox = {"joinableActivities": [{"titleId": 1746894998}],
                  "partyDetails": [], "inParty": 0}
        self._service_with_people([
            {"gamertag": "a", "multiplayerSummary": roblox}])
        self.assertEqual(presence.social_snapshot(_credentials()).in_session, 0)

    def test_a_refused_friends_list_is_reported_not_guessed_at(self):
        self.start_service()
        with mock.patch.object(presence, "_request",
                               side_effect=[(200, '{"state": "Online"}'),
                                            (403, "")]):
            snapshot = presence.social_snapshot(_credentials())
        self.assertEqual(snapshot.state, "Online")
        self.assertIn("HTTP 403", snapshot.error)
        self.assertIsNone(snapshot.friends)

    def test_an_unreadable_answer_is_reported_not_guessed_at(self):
        self.start_service()
        with mock.patch.object(presence, "_request",
                               side_effect=[(200, '{"state": "Online"}'),
                                            (200, "not json")]):
            self.assertIn("unexpected answer",
                          presence.social_snapshot(_credentials()).error)

    def test_nothing_is_asked_without_a_usable_account(self):
        service = self.start_service()
        missing = Path(tempfile.mkdtemp()) / "device.json"
        self.assertIn("no linked account",
                      presence.social_snapshot(path=missing).error)
        self.assertEqual(service.requests, [])


class DoctorSummaryTests(unittest.TestCase):
    def _path(self, payload):
        path = Path(tempfile.mkdtemp()) / "device.json"
        path.write_text(json.dumps(payload))
        return path

    def _fresh(self):
        return self._path({
            "xbl_token": "a-token",
            "xbl_uhs": "1234",
            "xbl_xuid": "2535416128702156",
            "xbl_token_expiry_epoch": int(time.time()) + 3600,
        })

    def test_the_three_answers_are_told_apart(self):
        missing = Path(tempfile.mkdtemp()) / "device.json"
        self.assertIn("off", presence.presence_summary(
            {"xbl_presence": False}, {}, self._fresh()))
        self.assertIn("once an account is linked",
                      presence.presence_summary({}, {}, missing))
        self.assertIn("published as Minecraft",
                      presence.presence_summary({}, {}, self._fresh()))

    def test_an_expired_token_says_what_refreshes_it(self):
        expired = self._path({
            "xbl_token": "a-token",
            "xbl_uhs": "1234",
            "xbl_xuid": "2535416128702156",
            "xbl_token_expiry_epoch": int(time.time()) - 1,
        })
        self.assertIn("expired",
                      presence.presence_summary({}, {}, expired))

    def test_only_being_switched_off_is_worth_a_warning(self):
        self.assertIsNone(presence.presence_problem({}, {}, self._fresh()))
        self.assertIn("Offline", presence.presence_problem(
            {"xbl_presence": False}, {}, self._fresh()))


class LaunchWarningTests(unittest.TestCase):
    def test_a_publishing_session_says_nothing(self):
        with mock.patch.object(presence, "warn") as warned:
            presence.warn_if_unavailable(presence.Session(_credentials()),
                                         {}, {})
        warned.assert_not_called()

    def test_switching_it_off_is_a_choice_not_a_fault(self):
        with mock.patch.object(presence, "warn") as warned:
            presence.warn_if_unavailable(presence.Session(),
                                         {"xbl_presence": False}, {})
        warned.assert_not_called()

    def test_meaning_to_publish_and_failing_is_reported(self):
        with mock.patch.object(presence, "warn") as warned:
            presence.warn_if_unavailable(presence.Session(), {}, {})
        warned.assert_called_once()
        self.assertIn("Offline", warned.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
