#!/usr/bin/env python3
"""The BedrockOnLinux report relay: turns a launcher's problem report into a
GitHub issue, so the launcher never has to hold a credential.

It holds the private key of a GitHub App that can do exactly one thing --
write issues on one repository -- and does exactly that with it. What it
receives is untrusted: an old launcher, an altered one, or no launcher at
all. So before anything is filed it

* refuses what is too large or not shaped like a report,
* runs the launcher's own privacy pass (scrub.py, the same file) again,
* puts every piece inside a code block, where a mention, a link, an image or
  HTML is only text,
* and counts: per sender, and in total per day.

It keeps nothing. No report is written to disk, no address is logged, and
the rate limiter remembers a sender only as a salted hash, in memory, that a
restart forgets.

Standard library only. `openssl` signs the App's token, so no Python package
has to be installed on the server. Listens on loopback behind a TLS reverse
proxy. See docs/report-relay.md.
"""
# SPDX-License-Identifier: MIT

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if (_HERE / "scrub.py").is_file():      # deployed: scrub.py sits beside us
    # Named explicitly: the service runs Python isolated (-I), which leaves
    # the script's own folder off the import path.
    sys.path.insert(0, str(_HERE))
    from scrub import Scrubber
else:                                   # run from the repository
    sys.path.insert(0, str(_HERE.parent))
    from bol.scrub import Scrubber

PROTOCOL = 1
MAX_TITLE = 120
MAX_COMMENT = 4000
MAX_SUMMARY = 24_000
MAX_ATTACHMENTS = 16
MAX_NAME = 80
# GitHub refuses a body or a comment past 65536 characters.
MAX_ISSUE_PART = 60_000
MAX_COMMENTS = 8
_VERSION = re.compile(r"[0-9][0-9A-Za-z.+~-]{0,31}\Z")
_NAME = re.compile(r"[^\w .()+-]")


class Refused(Exception):
    """A request the relay will not act on: (HTTP status, message)."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class Config:
    """Everything comes from the environment (see relay.env.example)."""

    def __init__(self, environ=None):
        env = os.environ if environ is None else environ

        def number(name, default):
            try:
                return int(env.get(name, "") or default)
            except ValueError:
                return default

        self.listen = env.get("BOL_RELAY_LISTEN", "127.0.0.1:8787")
        self.repo = env.get("BOL_RELAY_REPO", "Wyze3306/BedrockOnLinux")
        self.app_id = env.get("BOL_RELAY_APP_ID", "").strip()
        self.installation_id = env.get("BOL_RELAY_INSTALLATION_ID", "").strip()
        key = env.get("BOL_RELAY_PRIVATE_KEY", "").strip()
        if not key and env.get("CREDENTIALS_DIRECTORY"):
            key = str(Path(env["CREDENTIALS_DIRECTORY"]) / "app-key.pem")
        self.private_key = key
        self.api = env.get("BOL_RELAY_GITHUB_API",
                           "https://api.github.com").rstrip("/")
        self.labels = [label.strip() for label in env.get(
            "BOL_RELAY_LABELS", "user-report").split(",") if label.strip()]
        self.max_bytes = number("BOL_RELAY_MAX_BYTES", 450_000)
        self.per_sender_hour = number("BOL_RELAY_PER_SENDER_HOUR", 3)
        self.per_sender_day = number("BOL_RELAY_PER_SENDER_DAY", 6)
        self.total_day = number("BOL_RELAY_TOTAL_DAY", 100)
        self.attempts_hour = number("BOL_RELAY_ATTEMPTS_HOUR", 30)
        # Behind a reverse proxy the sender is in X-Forwarded-For. Believed
        # only when the connection itself comes from loopback.
        self.trust_proxy = env.get("BOL_RELAY_TRUST_PROXY", "1") == "1"
        self.paused = env.get("BOL_RELAY_PAUSED", "") == "1"
        self.dry_run = env.get("BOL_RELAY_DRY_RUN", "") == "1"

    def problems(self):
        if self.dry_run:
            return []
        missing = [name for name, value in (
            ("BOL_RELAY_APP_ID", self.app_id),
            ("BOL_RELAY_INSTALLATION_ID", self.installation_id),
            ("BOL_RELAY_PRIVATE_KEY", self.private_key)) if not value]
        problems = [f"{name} is not set" for name in missing]
        if self.private_key and not os.access(self.private_key, os.R_OK):
            problems.append(f"the private key {self.private_key} is not "
                            "readable")
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", self.repo):
            problems.append("BOL_RELAY_REPO is not owner/name")
        return problems


# ====================================================================
# Counting
# ====================================================================

class RateLimiter:
    """Sliding windows, in memory. A sender is a salted hash of its address:
    the address itself is never stored, and the salt dies with the process."""

    def __init__(self, config, clock=time.monotonic):
        self.config = config
        self.clock = clock
        self._salt = secrets.token_bytes(32)
        self._lock = threading.Lock()
        self._attempts = {}
        self._accepted = {}
        self._total = deque()

    def _sender(self, address):
        return hmac.new(self._salt, str(address).encode(), hashlib.sha256
                        ).digest()[:16]

    @staticmethod
    def _recent(stamps, now, window):
        while stamps and now - stamps[0] > window:
            stamps.popleft()
        return len(stamps)

    def _prune(self, now):
        for table in (self._attempts, self._accepted):
            for key in [k for k, stamps in table.items()
                        if not stamps or now - stamps[-1] > 86400]:
                del table[key]

    def attempt(self, address):
        """Count one request, valid or not; Refused when there are too many."""
        now = self.clock()
        with self._lock:
            stamps = self._attempts.setdefault(self._sender(address), deque())
            if self._recent(stamps, now, 3600) >= self.config.attempts_hour:
                raise Refused(429, "Too many requests. Try again later.")
            stamps.append(now)
            if len(self._attempts) > 10000:
                self._prune(now)

    def accept(self, address):
        """Count one report about to be filed; Refused past a limit."""
        now = self.clock()
        with self._lock:
            if self._recent(self._total, now, 86400) >= self.config.total_day:
                raise Refused(429, "The relay has filed its daily maximum of "
                                   "reports. Use the browser instead, or try "
                                   "again tomorrow.")
            stamps = self._accepted.setdefault(self._sender(address), deque())
            day = self._recent(stamps, now, 86400)
            hour = sum(1 for stamp in stamps if now - stamp <= 3600)
            if hour >= self.config.per_sender_hour \
                    or day >= self.config.per_sender_day:
                raise Refused(429, "Too many reports were sent from this "
                                   "connection. Try again later, or use the "
                                   "browser instead.")
            stamps.append(now)
            self._total.append(now)


# ====================================================================
# From a request to the text of an issue
# ====================================================================

def _text(value, limit, what):
    if not isinstance(value, str):
        raise Refused(400, f"{what} must be text.")
    if len(value) > limit:
        raise Refused(413, f"{what} is too long.")
    return value


def parse_report(raw):
    """The request body as a cleaned report, or Refused.

    Everything a launcher sent is scrubbed again here: this is the copy of
    the pass the project controls, whatever version sent the report.
    """
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise Refused(400, "The report is not valid JSON.") from None
    if not isinstance(data, dict) or data.get("v") != PROTOCOL:
        raise Refused(400, "Unsupported report format. Update the launcher.")
    launcher = data.get("launcher")
    if not isinstance(launcher, str) or not _VERSION.match(launcher):
        raise Refused(400, "The report does not name a launcher version.")
    scrubber = Scrubber()
    title = " ".join(_text(data.get("title"), MAX_TITLE * 2, "The title")
                     .split())
    title = scrubber.text(title, prose=True).replace("@", "")[:MAX_TITLE]
    if len(title.strip()) < 4:
        raise Refused(400, "The report needs a title.")
    comment = scrubber.text(_text(data.get("comment", ""), MAX_COMMENT * 2,
                                  "The comment"), prose=True)[:MAX_COMMENT]
    summary = scrubber.text(_text(data.get("summary", ""), MAX_SUMMARY,
                                  "The summary"))
    attachments = data.get("attachments", [])
    if not isinstance(attachments, list) or len(attachments) > MAX_ATTACHMENTS:
        raise Refused(400, "Too many attachments.")
    cleaned = []
    for item in attachments:
        if not isinstance(item, dict):
            raise Refused(400, "An attachment is malformed.")
        name = _NAME.sub("", _text(item.get("name"), MAX_NAME * 2,
                                   "An attachment name"))[:MAX_NAME].strip()
        text = scrubber.text(_text(item.get("text"), 10**7, "An attachment"))
        if name and text.strip():
            cleaned.append((scrubber.text(name, prose=True), text))
    if not (comment.strip() or summary.strip() or cleaned):
        raise Refused(400, "The report is empty.")
    return {"launcher": launcher, "title": title.strip(), "comment": comment,
            "summary": summary, "attachments": cleaned}


def _fenced(text):
    """`text` as a code block nothing inside can break out of."""
    return "```text\n" + text.replace("```", "'''").strip("\n") + "\n```"


def _details(name, text):
    return f"<details><summary>{name}</summary>\n\n{_fenced(text)}\n\n</details>"


def compose(report):
    """(issue body, [comments]): the report as Markdown within GitHub's
    limits, the logs folded, the overflow in follow-up comments."""
    body = [
        f"*Sent from the launcher's \"Report a problem\" window "
        f"(BedrockOnLinux {report['launcher']}). Names, paths, addresses and "
        "tokens were removed before sending and again on arrival.*",
        "", "### What happened", "",
        _fenced(report["comment"] or "(no comment)"),
    ]
    if report["summary"].strip():
        body += ["", "### System", "", _fenced(report["summary"])]
    parts = []
    for name, text in report["attachments"]:
        room = MAX_ISSUE_PART - 200
        chunks = [text[i:i + room] for i in range(0, len(text), room)]
        for index, chunk in enumerate(chunks):
            label = name if len(chunks) == 1 else \
                f"{name} ({index + 1}/{len(chunks)})"
            parts.append(_details(label, chunk))
    pages = ["\n".join(body)]
    for part in parts:
        if len(pages[-1]) + len(part) + 2 <= MAX_ISSUE_PART:
            pages[-1] += "\n\n" + part
        elif len(pages) <= MAX_COMMENTS:
            pages.append(part)
        else:
            pages[-1] += "\n\n*(more logs were sent than fit here)*"
            break
    return pages[0], pages[1:]


# ====================================================================
# GitHub
# ====================================================================

def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=")


class GitHub:
    """The App's side: a signed token, traded for an installation token that
    can only write issues on the one repository, then the issue itself."""

    def __init__(self, config, clock=time.time):
        self.config = config
        self.clock = clock
        self._lock = threading.Lock()
        self._token = None
        self._expires = 0

    def app_jwt(self):
        now = int(self.clock())
        header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"},
                                    separators=(",", ":")).encode())
        claims = _b64url(json.dumps(
            {"iat": now - 60, "exp": now + 540, "iss": self.config.app_id},
            separators=(",", ":")).encode())
        signing_input = header + b"." + claims
        try:
            signed = subprocess.run(
                ["openssl", "dgst", "-sha256", "-sign",
                 self.config.private_key],
                input=signing_input, capture_output=True, timeout=20,
                check=True).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise Refused(503, "The relay cannot sign in to GitHub.") from exc
        return (signing_input + b"." + _b64url(signed)).decode()

    def _call(self, method, path, token, payload=None):
        request = urllib.request.Request(
            self.config.api + path, method=method,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + token,
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "Content-Type": "application/json",
                     "User-Agent": "bol-report-relay"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read(1_000_000).decode("utf-8"))

    def installation_token(self):
        with self._lock:
            if self._token and self.clock() < self._expires - 120:
                return self._token
            name = self.config.repo.split("/", 1)[1]
            answer = self._call(
                "POST", f"/app/installations/{self.config.installation_id}"
                        "/access_tokens", self.app_jwt(),
                # Narrower than the App itself, should it ever be granted
                # more: this token writes issues on this repository only.
                {"repositories": [name], "permissions": {"issues": "write"}})
            self._token = answer["token"]
            # Tokens last an hour; trust that rather than parse the date.
            self._expires = self.clock() + 3000
            return self._token

    def file(self, title, body, comments):
        """Create the issue and its follow-up comments; (number, URL)."""
        token = self.installation_token()
        base = f"/repos/{self.config.repo}/issues"
        payload = {"title": title, "body": body}
        if self.config.labels:
            payload["labels"] = self.config.labels
        issue = self._call("POST", base, token, payload)
        number = int(issue["number"])
        for comment in comments:
            try:
                self._call("POST", f"{base}/{number}/comments", token,
                           {"body": comment})
            except (OSError, ValueError, KeyError):
                break                    # the issue exists; say so anyway
        return number, f"https://github.com/{self.config.repo}/issues/{number}"


# ====================================================================
# HTTP
# ====================================================================

class Relay:
    def __init__(self, config=None, github=None, log=None):
        self.config = config or Config()
        self.github = github or GitHub(self.config)
        self.limiter = RateLimiter(self.config)
        self.log = log or (lambda line: print(line, file=sys.stderr,
                                              flush=True))
        self._recent = {}               # digest of a report -> (time, answer)
        self._lock = threading.Lock()

    def _duplicate(self, digest):
        now = time.monotonic()
        with self._lock:
            for key in [k for k, (stamp, _) in self._recent.items()
                        if now - stamp > 86400]:
                del self._recent[key]
            hit = self._recent.get(digest)
            return hit[1] if hit else None

    def handle(self, address, raw):
        """One report in, {"url", "number"} out; Refused otherwise."""
        if self.config.paused:
            raise Refused(503, "Report sending is paused. Use the browser "
                               "instead.")
        self.limiter.attempt(address)
        report = parse_report(raw)
        digest = hashlib.sha256(json.dumps(
            [report["title"], report["comment"], report["summary"]]
        ).encode()).hexdigest()
        previous = self._duplicate(digest)
        if previous:
            return previous
        self.limiter.accept(address)
        body, comments = compose(report)
        if self.config.dry_run:
            answer = {"url": f"https://github.com/{self.config.repo}/issues/0",
                      "number": 0}
        else:
            try:
                number, url = self.github.file(report["title"], body, comments)
            except Refused:
                raise
            except urllib.error.HTTPError as exc:
                self.log(f"github refused: {exc.code}")
                raise Refused(502, "GitHub refused the report. Use the "
                                   "browser instead.") from exc
            except (OSError, ValueError, KeyError) as exc:
                self.log(f"github unreachable: {type(exc).__name__}")
                raise Refused(502, "GitHub could not be reached. Try again, "
                                   "or use the browser instead.") from exc
            answer = {"url": url, "number": number}
        with self._lock:
            self._recent[digest] = (time.monotonic(), answer)
        self.log(f"filed #{answer['number']} launcher={report['launcher']} "
                 f"bytes={len(raw)} parts={1 + len(comments)}")
        return answer

    def handler(self):
        relay = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "bol-report-relay"
            sys_version = ""
            timeout = 30

            def log_message(self, *_args):   # no access log: no addresses
                pass

            def _sender(self):
                peer = self.client_address[0]
                forwarded = self.headers.get("X-Forwarded-For", "")
                if relay.config.trust_proxy and forwarded and peer in (
                        "127.0.0.1", "::1", "::ffff:127.0.0.1"):
                    return forwarded.split(",")[-1].strip() or peer
                return peer

            def _answer(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/healthz":
                    self._answer(200, {"ok": True})
                else:
                    self._answer(404, {"error": "Not found."})

            def do_POST(self):
                try:
                    if self.path != "/v1/report":
                        raise Refused(404, "Not found.")
                    try:
                        length = int(self.headers.get("Content-Length", ""))
                    except ValueError:
                        raise Refused(411, "Content-Length is required.") \
                            from None
                    if length <= 0 or length > relay.config.max_bytes:
                        raise Refused(413, "The report is too large.")
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise Refused(400, "The report was cut short.")
                    self._answer(200, relay.handle(self._sender(), raw))
                except Refused as refusal:
                    self._answer(refusal.status, {"error": refusal.message})
                except Exception as exc:  # noqa: BLE001 - never a traceback
                    relay.log(f"internal error: {type(exc).__name__}")
                    self._answer(500, {"error": "The relay failed. Use the "
                                                "browser instead."})

        return Handler

    def server(self):
        host, _, port = self.config.listen.rpartition(":")
        return ThreadingHTTPServer((host or "127.0.0.1", int(port)),
                                   self.handler())


def main():
    config = Config()
    problems = config.problems()
    if problems:
        for problem in problems:
            print("bol-report-relay: " + problem, file=sys.stderr)
        return 2
    relay = Relay(config)
    server = relay.server()
    relay.log(f"listening on {config.listen} for {config.repo}"
              + (" (dry run: nothing is filed)" if config.dry_run else ""))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
