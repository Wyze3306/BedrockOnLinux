"""The relay, against a fake GitHub on loopback. No real issue is ever filed."""
# SPDX-License-Identifier: MIT

import json
import tempfile
import unittest
import urllib.error
import urllib.request

from tests import reportharness
from tests.test_scrub import SECRETS, TRAPPED

relay_module = reportharness.load_relay()


def _report(**changes):
    report = {
        "v": 1, "launcher": "3.0.0",
        "title": "FPS drops when breaking blocks",
        "comment": "It stutters every time.",
        "summary": "== System ==\nkernel : Linux 6.16.12",
        "attachments": [{"name": "Game log (minecraft.log)",
                         "text": "ntsync: up and running."}],
    }
    report.update(changes)
    return report


def _post(url, payload, raw=None, headers=None):
    data = raw if raw is not None else json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, method="POST",
                                     headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


class ParseTests(unittest.TestCase):
    def parse(self, **changes):
        return relay_module.parse_report(json.dumps(_report(**changes)).encode())

    def refused(self, status, **changes):
        with self.assertRaises(relay_module.Refused) as caught:
            self.parse(**changes)
        self.assertEqual(caught.exception.status, status)

    def test_a_well_formed_report_is_accepted(self):
        report = self.parse()
        self.assertEqual(report["title"], "FPS drops when breaking blocks")
        self.assertEqual(report["attachments"],
                         [("Game log (minecraft.log)", "ntsync: up and running.")])

    def test_what_is_not_a_report_is_refused(self):
        self.refused(400, v=2)
        self.refused(400, launcher="<script>")
        self.refused(400, title="")
        self.refused(400, title=["a"])
        self.refused(413, title="x" * 1000)
        self.refused(413, comment="x" * 9000)
        self.refused(400, attachments="log")
        self.refused(400, attachments=[{"name": "a", "text": "b"}] * 17)
        self.refused(400, comment="", summary="", attachments=[])
        with self.assertRaises(relay_module.Refused):
            relay_module.parse_report(b"not json")

    def test_everything_received_is_scrubbed_again(self):
        """A launcher that did not clean its report -- an old one, a forged
        one -- still files nothing personal."""
        report = self.parse(
            title="crash for bob@example.org on 82.66.142.17",
            comment="see the log", summary=TRAPPED,
            attachments=[{"name": "minecraft.log", "text": TRAPPED}])
        body, comments = relay_module.compose(report)
        filed = "\n".join([report["title"], body, *comments]).lower()
        for what in ("xuid", "e-mail", "msa refresh token", "jwt",
                     "xsts header", "bearer", "device id", "device token",
                     "cookie", "ipv4", "lan ipv4", "ipv6", "mac", "serial",
                     "machine id", "password", "github token", "other user"):
            self.assertNotIn(SECRETS[what].lower(), filed, what)
        self.assertNotIn("bob@example.org", filed)
        self.assertNotIn("82.66.142.17", filed)


class ComposeTests(unittest.TestCase):
    def compose(self, **changes):
        report = relay_module.parse_report(
            json.dumps(_report(**changes)).encode())
        return relay_module.compose(report)

    def test_logs_are_folded_and_fenced(self):
        body, comments = self.compose()
        self.assertIn("### What happened", body)
        self.assertIn("<details><summary>Game log (minecraft.log)</summary>",
                      body)
        self.assertEqual(comments, [])

    def test_nothing_sent_can_act_as_markdown(self):
        """No mention, no link, no HTML, no way out of the code block."""
        body, _ = self.compose(
            title="@everyone look",
            comment="hello @torvalds see #1\n```\n</details><img src=x>",
            attachments=[{"name": "<b>log</b> @x", "text": "```\n@octocat"}])
        fences = [line for line in body.splitlines() if line.startswith("```")]
        # Every fence is one the relay wrote: they pair up.
        self.assertEqual(len(fences) % 2, 0)
        self.assertNotIn("```\n@octocat", body)
        self.assertIn("<details><summary>blogb x</summary>", body)
        outside, inside = [], False
        for line in body.splitlines():
            if line.startswith("```"):
                inside = not inside
            elif not inside:
                outside.append(line)
        self.assertNotIn("@", "\n".join(outside))
        self.assertNotIn("<img", "\n".join(outside))
        report = relay_module.parse_report(json.dumps(_report(
            title="@everyone look")).encode())
        self.assertNotIn("@", report["title"])

    def test_what_does_not_fit_the_issue_goes_to_comments(self):
        big = "\n".join(f"line {i} of a long log" for i in range(8000))
        body, comments = self.compose(attachments=[
            {"name": "a.log", "text": big}, {"name": "b.log", "text": big}])
        self.assertLessEqual(len(body), 65536)
        self.assertGreaterEqual(len(comments), 2)
        for comment in comments:
            self.assertLessEqual(len(comment), 65536)
            self.assertTrue(comment.startswith("<details>"))


class LimiterTests(unittest.TestCase):
    def limiter(self, **environ):
        self.now = [0.0]
        config = relay_module.Config(
            {k: str(v) for k, v in environ.items()})
        return relay_module.RateLimiter(config, clock=lambda: self.now[0])

    def test_a_sender_is_held_to_the_hourly_and_daily_limits(self):
        limiter = self.limiter(BOL_RELAY_PER_SENDER_HOUR=2,
                               BOL_RELAY_PER_SENDER_DAY=3)
        limiter.accept("a")
        limiter.accept("a")
        with self.assertRaises(relay_module.Refused):
            limiter.accept("a")
        limiter.accept("b")              # someone else is not held back
        self.now[0] += 3700
        limiter.accept("a")
        self.now[0] += 3700
        with self.assertRaises(relay_module.Refused):
            limiter.accept("a")          # the daily limit
        self.now[0] += 86400
        limiter.accept("a")

    def test_the_daily_total_holds_whoever_sends(self):
        limiter = self.limiter(BOL_RELAY_TOTAL_DAY=2)
        limiter.accept("a")
        limiter.accept("b")
        with self.assertRaises(relay_module.Refused):
            limiter.accept("c")

    def test_addresses_are_not_kept(self):
        limiter = self.limiter()
        limiter.attempt("82.66.142.17")
        limiter.accept("82.66.142.17")
        self.assertNotIn("82.66.142.17", repr(vars(limiter)))


class ConfigTests(unittest.TestCase):
    def test_a_relay_without_credentials_does_not_start(self):
        problems = relay_module.Config({}).problems()
        self.assertEqual(len(problems), 3)
        self.assertEqual(relay_module.Config(
            {"BOL_RELAY_DRY_RUN": "1"}).problems(), [])

    def test_the_key_comes_from_systemd_credentials(self):
        config = relay_module.Config({"CREDENTIALS_DIRECTORY": "/run/creds"})
        self.assertEqual(config.private_key, "/run/creds/app-key.pem")


@unittest.skipUnless(reportharness.HAVE_OPENSSL, "needs the openssl command")
class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # One throwaway key for the class: generating RSA keys is the slow
        # part, and nothing here depends on a fresh one.
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        reportharness.make_key(cls.directory.name)

    def relay(self, **environ):
        return reportharness.running_relay(self.directory.name, **environ)

    def test_a_report_becomes_one_issue(self):
        with self.relay() as (url, _relay, github):
            status, answer = _post(url, _report())
            self.assertEqual(status, 200, answer)
            self.assertEqual(
                answer, {"number": 401, "url":
                         "https://github.com/Wyze3306/BedrockOnLinux/issues/401"})
            self.assertEqual(len(github.issues), 1)
            issue = github.issues[0]
            self.assertEqual(issue["path"],
                             "/repos/Wyze3306/BedrockOnLinux/issues")
            self.assertEqual(issue["title"], "FPS drops when breaking blocks")
            self.assertEqual(issue["labels"], ["user-report"])
            self.assertIn("It stutters every time.", issue["body"])

    def test_the_token_is_signed_and_narrowed(self):
        with self.relay() as (url, _relay, github):
            _post(url, _report())
            _post(url, _report(title="A second, different problem"))
            # One installation token serves both: it is cached.
            self.assertEqual(len(github.token_requests), 1)
            request = github.token_requests[0]
            self.assertEqual(request["path"],
                             "/app/installations/7890/access_tokens")
            self.assertTrue(github.jwt_is_valid(request["jwt"]))
            self.assertEqual(request["body"], {
                "repositories": ["BedrockOnLinux"],
                "permissions": {"issues": "write"}})

    def test_the_same_report_twice_is_one_issue(self):
        with self.relay() as (url, _relay, github):
            first = _post(url, _report())
            second = _post(url, _report())
            self.assertEqual(first, second)
            self.assertEqual(len(github.issues), 1)

    def test_too_many_reports_are_refused(self):
        with self.relay(BOL_RELAY_PER_SENDER_HOUR="2") as (url, _r, github):
            for index in range(2):
                status, _ = _post(url, _report(title=f"Problem number {index}"))
                self.assertEqual(status, 200)
            status, answer = _post(url, _report(title="Problem number 3"))
            self.assertEqual(status, 429)
            self.assertIn("browser", answer["error"])
            self.assertEqual(len(github.issues), 2)

    def test_too_large_a_report_is_refused_unread(self):
        with self.relay(BOL_RELAY_MAX_BYTES="2000") as (url, _r, github):
            status, _ = _post(url, _report(comment="x" * 3000))
            self.assertEqual(status, 413)
            self.assertEqual(github.issues, [])

    def test_garbage_and_wrong_paths_are_refused(self):
        with self.relay() as (url, _relay, github):
            self.assertEqual(_post(url, None, raw=b"{")[0], 400)
            self.assertEqual(_post(url.replace("/v1/report", "/admin"),
                                   _report())[0], 404)
            self.assertEqual(github.issues, [])
            with urllib.request.urlopen(
                    url.replace("/v1/report", "/healthz"), timeout=10) as r:
                self.assertEqual(json.loads(r.read()), {"ok": True})

    def test_a_github_failure_is_an_answer_not_a_crash(self):
        with self.relay() as (url, _relay, github):
            github.fail_issues = 403
            status, answer = _post(url, _report())
            self.assertEqual(status, 502)
            self.assertIn("browser", answer["error"])

    def test_a_paused_relay_files_nothing(self):
        with self.relay(BOL_RELAY_PAUSED="1") as (url, _relay, github):
            self.assertEqual(_post(url, _report())[0], 503)
            self.assertEqual(github.issues, [])

    def test_the_sender_behind_the_proxy_is_the_one_counted(self):
        with self.relay(BOL_RELAY_PER_SENDER_HOUR="1") as (url, _r, _github):
            for index, sender in enumerate(("203.0.113.5", "203.0.113.6")):
                status, _ = _post(
                    url, _report(title=f"Problem from sender {index}"),
                    headers={"X-Forwarded-For": sender})
                self.assertEqual(status, 200)
            status, _ = _post(url, _report(title="One more from the first"),
                              headers={"X-Forwarded-For": "203.0.113.5"})
            self.assertEqual(status, 429)

    def test_a_dry_run_files_nothing(self):
        with self.relay(BOL_RELAY_DRY_RUN="1") as (url, _relay, github):
            status, answer = _post(url, _report())
            self.assertEqual((status, answer["number"]), (200, 0))
            self.assertEqual(github.issues, [])


if __name__ == "__main__":
    unittest.main()
