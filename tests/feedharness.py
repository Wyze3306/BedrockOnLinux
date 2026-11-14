"""A throwaway signing key and a feed sandbox for the data-feed tests.

Not collected by pytest. The key is generated per process: no private key,
test or otherwise, is ever stored in the repository.
"""
# SPDX-License-Identifier: MIT

import base64
import importlib.util
import json
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock

from bol import feed

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "data-feed.py"
_spec = importlib.util.spec_from_file_location("data_feed_tool", _SCRIPT)
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)

KEY = tool.generate_key("test-key")
OTHER_KEY = tool.generate_key("someone-else")
PUBLIC = {KEY["keyid"]: KEY["public"]}

SHA_A = "a" * 64
SHA_B = "b" * 64


def manifest(serial=100, **parts):
    payload = {"schema": 1, "serial": serial}
    payload.update(parts)
    return payload


def signed(payload, key=KEY):
    return tool.sign_payload(payload, [key])[0]


def tampered(raw):
    """The same envelope with one payload field changed after signing."""
    envelope = json.loads(raw)
    payload = json.loads(base64.b64decode(envelope["payload"]))
    payload["serial"] += 1
    envelope["payload"] = base64.b64encode(
        json.dumps(payload).encode()).decode()
    return json.dumps(envelope).encode()


@contextmanager
def sandbox(directory, mode="auto", channel="stable", keys=PUBLIC):
    """Point the feed at `directory`, trust the test key, set the mode."""
    settings = {"update_channel": channel}
    if mode is not None:
        settings["network_updates"] = mode
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(feed, "DATA", Path(directory)))
        stack.enter_context(mock.patch.object(feed, "PUBLIC_KEYS", keys))
        stack.enter_context(mock.patch.dict(
            "os.environ", {"BOL_FEED_PUBKEY": "", "BOL_FEED_URL": "",
                           "BOL_NETWORK_UPDATES": ""}))
        stack.enter_context(mock.patch(
            "bol.util.load_settings", lambda: dict(settings)))
        feed.forget()
        try:
            yield settings
        finally:
            feed.forget()


def serve(raw):
    """A fetch() that answers every mirror with `raw`."""
    return lambda url, timeout: raw


def offline(url, timeout):
    raise OSError("Network is unreachable")
