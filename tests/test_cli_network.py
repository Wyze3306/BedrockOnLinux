"""The terminal side of the update choice and of the data feed."""
# SPDX-License-Identifier: MIT

import contextlib
import io
import sys
import unittest
from unittest import mock

from bol import cli, feed, netpolicy


def _run(*argv, **patches):
    output = io.StringIO()
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(
            sys, "argv", ["bedrock-on-linux", *argv]))
        stack.enter_context(mock.patch("bol.rollback.confirm"))
        for target, value in patches.items():
            stack.enter_context(mock.patch(target.replace("__", "."), value))
        stack.enter_context(contextlib.redirect_stdout(output))
        try:
            cli.main()
        except SystemExit as exited:
            return output.getvalue(), exited.code
    return output.getvalue(), None


class NetworkCommandTests(unittest.TestCase):
    def test_it_lists_every_host_and_the_current_choice(self):
        said, _ = _run("network", **{
            "bol.update.describe_install": lambda: "test install"})
        for endpoint in netpolicy.ENDPOINTS:
            for host in endpoint.hosts:
                self.assertIn(host, said)
        self.assertIn("No telemetry", said)
        self.assertIn("Update checks: Off", said)       # conftest's default
        self.assertIn("test install", said)

    def test_it_records_a_choice(self):
        with mock.patch.object(netpolicy, "set_mode") as set_mode:
            _run("network", "ask")
        set_mode.assert_called_once_with("ask")

    def test_it_refuses_a_choice_that_does_not_exist(self):
        with contextlib.redirect_stderr(io.StringIO()):
            _said, code = _run("network", "sometimes")
        self.assertEqual(code, 2)


class VersionTests(unittest.TestCase):
    def test_version_prints_what_an_update_checks_for(self):
        from bol.config import PRETTY, VERSION
        said, code = _run("--version")
        self.assertEqual(code, 0)
        self.assertEqual(said.strip(), f"{PRETTY} {VERSION}")


class UpdateCommandTests(unittest.TestCase):
    def test_data_only_refreshes_the_feed_and_stops_there(self):
        with mock.patch.object(feed, "refresh",
                               return_value=("updated", "data 7")) as refresh, \
                mock.patch.object(cli, "check_for_update") as check:
            said, _ = _run("update", "--data-only")
        refresh.assert_called_once()
        check.assert_not_called()
        self.assertIn("data 7", said)

    def test_a_feed_that_cannot_be_fetched_does_not_stop_the_check(self):
        with mock.patch.object(feed, "refresh",
                               return_value=("error", "unreachable")), \
                mock.patch.object(cli, "check_for_update",
                                  return_value=None) as check:
            said, _ = _run("update")
        check.assert_called_once()
        self.assertIn("up to date", said)

    def test_a_first_run_refusal_in_a_terminal_checks_nothing(self):
        with mock.patch.object(netpolicy, "decided", return_value=False), \
                mock.patch.object(netpolicy, "choose_in_terminal",
                                  return_value=None), \
                mock.patch.object(cli, "IS_TTY", True), \
                mock.patch.object(feed, "refresh") as refresh, \
                mock.patch.object(cli, "check_for_update") as check:
            _run("update")
        refresh.assert_not_called()
        check.assert_not_called()


class PlayTests(unittest.TestCase):
    def _play(self, **patches):
        base = {
            "bol.cli.msa_signed_in": lambda: True,
            "bol.cli.update_game_before_play": lambda always=False: None,
            "bol.cli.supervision": mock.Mock(),
        }
        base.update(patches)
        with mock.patch.object(cli, "launch") as launch:
            said, _ = _run("play", **base)
        return said, launch

    def test_with_updates_off_play_asks_the_network_nothing(self):
        with mock.patch.object(feed, "refresh") as refresh:
            _said, launch = self._play()
        refresh.assert_not_called()
        launch.assert_called_once()

    def test_a_feed_failure_can_never_stop_play(self):
        _said, launch = self._play(**{
            "bol.netpolicy.choose_in_terminal":
                mock.Mock(side_effect=RuntimeError("boom"))})
        launch.assert_called_once()
        with mock.patch.object(feed, "refresh_if_due",
                               side_effect=RuntimeError("boom")):
            _said, launch = self._play()
        launch.assert_called_once()

    def test_incident_notices_are_printed_before_the_launch(self):
        notice = {"id": "down", "level": "warning",
                  "title": "Xbox sign-in is down",
                  "text": "Microsoft is working on it.",
                  "url": "https://example.org/status"}
        with mock.patch.object(feed, "notices", return_value=[notice]):
            said, launch = self._play()
        self.assertIn("Xbox sign-in is down", said)
        self.assertIn("https://example.org/status", said)
        launch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
