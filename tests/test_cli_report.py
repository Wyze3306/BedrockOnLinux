"""`bedrock-on-linux report`: nothing leaves without a yes."""
# SPDX-License-Identifier: MIT

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import cli, report
from bol.log import BolError
from tests.test_scrub import KNOWN, TRAPPED

ISSUE = "https://github.com/Wyze3306/BedrockOnLinux/issues/401"


class ReportCommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        logs = Path(self.directory.name) / "logs"
        logs.mkdir()
        (logs / "minecraft.log").write_text(TRAPPED, encoding="utf-8")
        real = report.collect
        self.sent, self.browsed, self.printed = [], [], []
        patches = [
            mock.patch.object(report, "collect", lambda: real(
                settings={}, logs_dir=logs, identity=list(KNOWN))),
            mock.patch.object(report, "send", self._send),
            mock.patch.object(report, "open_in_browser", self._browse),
            mock.patch.dict("os.environ", {report.RELAY_ENV: "off"}),
            mock.patch.object(cli, "info"), mock.patch.object(cli, "ok"),
            mock.patch.object(cli, "warn"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _send(self, sending, url):
        self.sent.append((sending.payload(), url))
        return ISSUE

    def _browse(self, sending):
        self.browsed.append(sending.full_text())
        return Path("/x/report.txt"), "https://github.com/x/issues/new", True

    def run_command(self, tty=True, answers=(), **options):
        defaults = dict(title=None, comment=None, comment_file=None,
                        without=None, preview=False, output=None,
                        browser=False, yes=False)
        defaults.update(options)
        answers = iter(answers)
        with mock.patch.object(cli, "IS_TTY", tty):
            cli._cmd_report(argparse.Namespace(**defaults),
                            ask=lambda _prompt: next(answers),
                            out=self.printed.append)
        return "\n".join(str(line) for line in self.printed)

    def test_preview_prints_everything_and_sends_nothing(self):
        text = self.run_command(preview=True, title="It crashes")
        self.assertIn("Title: It crashes", text)
        self.assertIn("Game log (minecraft.log)", text)
        self.assertIn("ntsync: up and running.", text)
        self.assertNotIn("marguerite", text.lower())
        self.assertIn("Removed before anything is shown or sent", text)
        self.assertEqual((self.sent, self.browsed), ([], []))

    def test_output_writes_a_file_and_sends_nothing(self):
        target = Path(self.directory.name) / "report.txt"
        self.run_command(output=str(target), title="It crashes")
        self.assertIn("Title: It crashes", target.read_text())
        self.assertEqual((self.sent, self.browsed), ([], []))

    def test_a_no_sends_nothing(self):
        self.run_command(answers=["It crashes", "every time", "n"])
        self.assertEqual((self.sent, self.browsed), ([], []))

    def test_a_yes_opens_the_browser_when_there_is_no_relay(self):
        text = self.run_command(answers=["It crashes", "every time", "y"])
        self.assertEqual(len(self.browsed), 1)
        self.assertIn("Title: It crashes", self.browsed[0])
        self.assertIn("every time", self.browsed[0])
        self.assertIn("Nothing is sent by the launcher", text)

    def test_a_yes_sends_through_the_relay_when_there_is_one(self):
        relay = "https://reports.example/v1/report"
        with mock.patch.dict("os.environ", {report.RELAY_ENV: relay}):
            text = self.run_command(title="It crashes", comment="x",
                                    answers=["yes"])
        self.assertEqual([url for _payload, url in self.sent], [relay])
        self.assertEqual(self.browsed, [])
        self.assertIn("Sent to reports.example", text)

    def test_a_relay_failure_falls_back_to_the_browser(self):
        relay = "https://reports.example/v1/report"
        with mock.patch.dict("os.environ", {report.RELAY_ENV: relay}), \
                mock.patch.object(report, "send",
                                  side_effect=report.ReportError("down.")):
            self.run_command(title="It crashes", yes=True)
        self.assertEqual(len(self.browsed), 1)

    def test_browser_is_used_on_request_even_with_a_relay(self):
        relay = "https://reports.example/v1/report"
        with mock.patch.dict("os.environ", {report.RELAY_ENV: relay}):
            self.run_command(title="It crashes", yes=True, browser=True)
        self.assertEqual((len(self.sent), len(self.browsed)), (0, 1))

    def test_without_a_terminal_nothing_is_sent_unless_told(self):
        with self.assertRaises(BolError):
            self.run_command(tty=False, title="It crashes")
        self.assertEqual((self.sent, self.browsed), ([], []))
        self.run_command(tty=False, title="It crashes", yes=True)
        self.assertEqual(len(self.browsed), 1)

    def test_a_report_needs_a_title(self):
        with self.assertRaises(BolError):
            self.run_command(tty=False, yes=True)
        self.assertEqual((self.sent, self.browsed), ([], []))

    def test_a_part_can_be_left_out_by_name(self):
        self.run_command(title="It crashes", yes=True,
                         without=["minecraft.log", "settings"])
        self.assertNotIn("Game log (minecraft.log)", self.browsed[0])
        self.assertNotIn("Launcher settings", self.browsed[0])
        with self.assertRaises(BolError):
            self.run_command(title="It crashes", yes=True, without=["nope"])


if __name__ == "__main__":
    unittest.main()
