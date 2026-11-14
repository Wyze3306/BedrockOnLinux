"""A fake GitHub and a real relay, both on loopback, for the report tests.

Not collected by pytest. Nothing here ever reaches github.com: the relay is
pointed at FakeGitHub, which records what it was asked to file.
"""
# SPDX-License-Identifier: MIT

import base64
import importlib.util
import json
import shutil
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HAVE_OPENSSL = shutil.which("openssl") is not None


def load_relay():
    spec = importlib.util.spec_from_file_location(
        "report_relay", ROOT / "relay" / "report_relay.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    return server, thread


class FakeGitHub:
    """The three calls the relay makes, and a record of each."""

    def __init__(self, public_key=None):
        self.public_key = public_key
        self.token_requests = []
        self.issues = []
        self.comments = []
        self.fail_issues = None          # an HTTP status to answer with
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _json(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = json.loads(self.rfile.read(
                    int(self.headers["Content-Length"])) or b"{}")
                auth = self.headers.get("Authorization", "")
                if self.path.endswith("/access_tokens"):
                    fake.token_requests.append(
                        {"path": self.path, "jwt": auth[7:], "body": body})
                    if not fake.jwt_is_valid(auth[7:]):
                        return self._json(401, {"message": "bad jwt"})
                    return self._json(201, {"token": "ghs_installation"})
                if auth != "Bearer ghs_installation":
                    return self._json(401, {"message": "bad token"})
                if self.path.endswith("/comments"):
                    fake.comments.append({"path": self.path, **body})
                    return self._json(201, {"id": len(fake.comments)})
                if self.path.endswith("/issues"):
                    if fake.fail_issues:
                        return self._json(fake.fail_issues, {"message": "no"})
                    fake.issues.append({"path": self.path, **body})
                    return self._json(201, {"number": 400 + len(fake.issues)})
                return self._json(404, {})

        self.server, self.thread = _serve(Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]

    def jwt_is_valid(self, token):
        """Checked the way GitHub would: RS256 against the App's public key."""
        if self.public_key is None:
            return token.count(".") == 2
        try:
            signing_input, signature = token.rsplit(".", 1)
            raw = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
            signature_file = Path(self.public_key).with_suffix(".sig")
            signature_file.write_bytes(raw)
            return subprocess.run(
                ["openssl", "dgst", "-sha256", "-verify", self.public_key,
                 "-signature", str(signature_file)],
                input=signing_input.encode(), capture_output=True,
                timeout=20).returncode == 0
        except (OSError, ValueError, subprocess.SubprocessError):
            return False

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


def make_key(directory):
    """(private key, public key) paths of a throwaway RSA key."""
    private = Path(directory) / "app-key.pem"
    public = Path(directory) / "app-key.pub"
    if private.is_file() and public.is_file():
        return private, public
    subprocess.run(["openssl", "genrsa", "-out", str(private), "2048"],
                   check=True, capture_output=True, timeout=60)
    subprocess.run(["openssl", "rsa", "-in", str(private), "-pubout", "-out",
                    str(public)], check=True, capture_output=True, timeout=60)
    return private, public


@contextmanager
def running_relay(directory, **environ):
    """A relay on a loopback port, filing into a FakeGitHub.

    Yields (report URL, relay, fake GitHub).
    """
    module = load_relay()
    private, public = make_key(directory)
    github = FakeGitHub(public)
    settings = {
        "BOL_RELAY_LISTEN": "127.0.0.1:0",
        "BOL_RELAY_APP_ID": "123456",
        "BOL_RELAY_INSTALLATION_ID": "7890",
        "BOL_RELAY_PRIVATE_KEY": str(private),
        "BOL_RELAY_GITHUB_API": github.url,
    }
    settings.update(environ)
    relay = module.Relay(module.Config(settings), log=lambda _line: None)
    server = relay.server()
    thread = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    try:
        yield ("http://127.0.0.1:%d/v1/report" % server.server_address[1],
               relay, github)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        github.close()
