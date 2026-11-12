"""bol.gameevents — hand the game's Xbox Live events to Xbox Live (#152).

Minecraft's achievements are event based: the client never unlocks one, it
reports gameplay -- ``BlockBroken``, ``ItemCrafted``, ``AwardAchievement``
-- and Xbox Live grades the achievements from those reports. A GDK title
reports through ``XGameEventWrite``; on Windows, Gaming Services takes the
events from there to the Xbox Live event collector. Under Wine nothing did,
so in years of play nothing ever unlocked.

The engine now records every ``XGameEventWrite`` as one JSON line in the
file named by ``BOL_GAME_EVENT_LOG`` (the WineGDK ``XGameEvent`` patch), and
this module is the other half: it reads that file while the game runs and
uploads each event the way Xbox Live's own SDK does outside Gaming Services
-- the CLL ("Common Logging Library") Common Schema 2.1 stream that XSAPI's
``EventsService`` posts to ``vortex.data.microsoft.com/collect/v1``:

* event name ``Microsoft.XboxLive.T<titleId>.<event>``, instrumentation key
  ``P-XBL-T<titleId>``, part B ``Microsoft.XboxLive.InGame`` with the
  service configuration id, the play session, the user and the title's own
  property bags, untouched;
* the account proven by Xbox Live tickets, never by a password: an
  uploader token in ``X-AuthXToken`` and the player's ticket in
  ``X-Tickets``, referenced from each event, for the audiences Xbox Live's
  public title-endpoint map assigns to the collector
  (``http://vortex.microsoft.com``) and to in-game events
  (``http://events.xboxlive.com``).

Nothing here invents an event: what is uploaded is exactly what the game
wrote, for the account that is playing, which is what the same game does on
Windows. Nothing here may raise into a launch or hold one up either; the
upload runs on a thread of its own and gives up quietly.
"""
# SPDX-License-Identifier: MIT

import json
import os
import random
import re
import threading
import time
from pathlib import Path

from .config import LOGS
from .log import info

COLLECTOR_URL = "https://vortex.data.microsoft.com/collect/v1"
# Title-endpoint map (title.mgt.xboxlive.com/titles/default/endpoints):
# *.data.microsoft.com -> http://vortex.microsoft.com and
# vortex-events.xboxlive.com -> http://events.xboxlive.com. XSAPI asks for the
# first as the uploader's ticket and the second as the player's.
UPLOADER_RELYING_PARTY = "http://vortex.microsoft.com"
PLAYER_RELYING_PARTY = "http://events.xboxlive.com"
LOG_NAME = "game-events.jsonl"
# Gameplay events come in bursts (a block broken, an item picked up); one
# request every few seconds is what the SDK's own uploader does too.
FLUSH_SECONDS = 5.0
MAX_BATCH = 50
# A session that cannot reach Xbox Live keeps this many events for when it
# can again, then lets the oldest go rather than grow without bound.
MAX_PENDING = 2000
RETRY_SECONDS = 15.0
TIMEOUT_SECONDS = 20
STOP_TIMEOUT_SECONDS = 5.0
# A ticket this close to its expiry is renewed before it is used.
TICKET_MARGIN_SECONDS = 300
_EVENT_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")
_TITLE_ID = re.compile(r"<TitleId>\s*(?:0x)?([0-9A-Fa-f]{1,8})\s*</TitleId>")


def enabled(settings=None, environ=None):
    """Whether this session's events go to Xbox Live.

    On by default -- on Windows every Minecraft session sends them, and they
    are what achievements are made of. ``xbl_game_events`` in the settings
    turns it off for good; BOL_XBL_EVENTS overrides both for one run.
    """
    environ = os.environ if environ is None else environ
    forced = (environ.get("BOL_XBL_EVENTS") or "").strip().lower()
    if forced in {"0", "false", "no", "off"}:
        return False
    if forced in {"1", "true", "yes", "on"}:
        return True
    return bool((settings or {}).get("xbl_game_events", True))


def log_path():
    """Where the engine writes this session's events."""
    return LOGS / LOG_NAME


def wine_path(path):
    """``path`` as the game sees it: through Wine's Z: drive."""
    return "Z:" + str(Path(path)).replace("/", "\\")


def title_id(game_dir):
    """The title id ``MicrosoftGame.Config`` declares, or None."""
    for name in ("MicrosoftGame.Config", "MicrosoftGame.config"):
        try:
            text = (Path(game_dir) / name).read_bytes()
        except OSError:
            continue
        if text[:2] in (b"\xff\xfe", b"\xfe\xff"):
            text = text.decode("utf-16", "replace")
        else:
            text = text.decode("utf-8", "replace")
        match = _TITLE_ID.search(text)
        if match:
            value = int(match.group(1), 16)
            return value or None
    return None


def prepare(env, settings, game_dir, online, environ=None):
    """Point the engine at a fresh event file for this session.

    Returns the title id the session's events will be filed under, or None
    when nothing is to be forwarded (no account, switched off, unknown
    title) -- in which case the engine records nothing either.
    """
    if not online or not enabled(settings, environ):
        return None
    title = title_id(game_dir)
    if title is None:
        return None
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Truncated, not appended: a previous session's events were either
        # sent then or belong to a session that is over.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
    except OSError:
        return None
    env["BOL_GAME_EVENT_LOG"] = wine_path(path)
    return title


def envelope(record, title, sequence, epoch, ticket_id):
    """The CLL Common Schema 2.1 line for one recorded event, or None.

    ``record`` is one line the engine wrote: time, xuid, scid, session, event
    and the title's two property bags.
    """
    if not isinstance(record, dict):
        return None
    name = record.get("event")
    scid = record.get("scid")
    xuid = str(record.get("xuid") or "")
    if not (isinstance(name, str) and _EVENT_NAME.match(name)
            and isinstance(scid, str) and scid and xuid.isdigit()
            and xuid != "0"):
        return None
    dimensions = record.get("dimensions")
    measurements = record.get("measurements")
    base = {
        "name": name,
        "serviceConfigId": scid,
        "playerSessionId": str(record.get("session") or ""),
        "titleId": str(title),
        "userId": xuid,
        "properties": dimensions if isinstance(dimensions, dict) else {},
        "measurements": measurements if isinstance(measurements, dict) else {},
    }
    stamp = record.get("time")
    if not isinstance(stamp, str) or not stamp:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    return {
        "ver": "2.1",
        "name": "Microsoft.XboxLive.T%d.%s" % (title, name),
        "time": stamp,
        "popSample": 100.0,
        "epoch": str(epoch),
        "seqNum": sequence,
        "iKey": "P-XBL-T%d" % title,
        "flags": 257,
        "os": "Windows",
        "osVer": "10.0",
        "ext": {
            "user": {"localId": ""},
            "device": {"localId": "", "deviceClass": "Windows.Desktop"},
            # The CLL extension that carries the ids of the tickets in the
            # request's X-Tickets header which vouch for this event.
            "android": {"ver": "1.0", "libVer": "3.170921.0",
                        "tickets": [ticket_id]},
        },
        "data": {"baseType": "Microsoft.XboxLive.InGame", "baseData": base},
    }


def _post(url, body, headers, timeout=TIMEOUT_SECONDS):
    """POST once; returns (status, text) or (None, reason)."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, data=body, headers=headers,
                                     method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        try:
            detail = error.read().decode("utf-8", "replace")
        except Exception:  # the status is what matters
            detail = ""
        return error.code, detail
    except Exception as error:
        return None, type(error).__name__


def _mint(relying_party):
    from .auth import mint_xsts_from_preauth

    return mint_xsts_from_preauth(relying_party)


class Forwarder:
    """Reads the engine's event file and uploads what it finds.

    Inert unless given a title, so a launch can always ``start()`` and
    ``stop()`` it without asking whether the session has events at all.
    """

    def __init__(self, path=None, title=None, mint=None, post=None,
                 clock=None):
        self.path = Path(path) if path is not None else log_path()
        self.title = title
        self._mint = mint or _mint
        self._post = post or _post
        self._clock = clock or time.time
        self._stopped = threading.Event()
        self._thread = None
        self._offset = 0
        self._partial = b""
        self._pending = []
        self._tickets = {}
        self._sequence = 0
        self._epoch = random.getrandbits(62)
        self.sent = 0
        self.refused = 0
        self.dropped = 0
        self.awarded = 0

    @property
    def active(self):
        return self.title is not None

    def start(self):
        if not self.active or self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._run,
                                        name="xbl-game-events", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout=STOP_TIMEOUT_SECONDS):
        """Send what is left, briefly, and let the thread go."""
        self._stopped.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)

    # -- reading

    def read_new(self):
        """Queue every complete line written since the last call."""
        try:
            with open(self.path, "rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
        except OSError:
            return 0
        if not chunk:
            return 0
        self._offset += len(chunk)
        data = self._partial + chunk
        lines = data.split(b"\n")
        self._partial = lines.pop()
        added = 0
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                self.dropped += 1
                continue
            self._pending.append(record)
            added += 1
        overflow = len(self._pending) - MAX_PENDING
        if overflow > 0:
            del self._pending[:overflow]
            self.dropped += overflow
        return added

    # -- uploading

    def _ticket(self, relying_party):
        cached = self._tickets.get(relying_party)
        now = self._clock()
        if cached and (cached.get("expiry_epoch") is None
                       or cached["expiry_epoch"] - TICKET_MARGIN_SECONDS > now):
            return cached
        fresh = self._mint(relying_party)
        if fresh:
            self._tickets[relying_party] = fresh
        return fresh

    def flush(self):
        """Upload pending events in batches. Returns False to retry later."""
        while self._pending:
            batch = self._pending[:MAX_BATCH]
            uploader = self._ticket(UPLOADER_RELYING_PARTY)
            player = self._ticket(PLAYER_RELYING_PARTY)
            if not uploader or not player:
                return False
            lines, by_xuid, used = [], set(), 0
            for record in batch:
                used += 1
                xuid = str(record.get("xuid") or "") \
                    if isinstance(record, dict) else ""
                line = envelope(record, self.title, self._sequence + 1,
                                self._epoch, xuid)
                if line is None:
                    self.dropped += 1
                    continue
                self._sequence += 1
                by_xuid.add(xuid)
                lines.append(json.dumps(line, separators=(",", ":")))
            if not lines:
                del self._pending[:used]
                continue
            # One player is signed in; a ticket is only valid for its own
            # user, so the events of anyone else are not ours to vouch for.
            tickets = ";".join(
                '"%s"="x:XBL3.0 x=%s;%s"' % (xuid, player["uhs"],
                                              player["token"])
                for xuid in sorted(by_xuid))
            body = ("\n".join(lines) + "\n").encode()
            status, text = self._post(COLLECTOR_URL, body, {
                "Content-Type": "application/x-json-stream; charset=utf-8",
                "Accept": "application/json",
                "X-UploadTime": time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                              time.gmtime(self._clock())),
                "X-AuthXToken": "XBL3.0 x=%s;%s" % (uploader["uhs"],
                                                    uploader["token"]),
                "X-Tickets": tickets,
            })
            if status in (401, 403):
                # A ticket the collector no longer takes: mint new ones next
                # time round instead of resending into the same refusal.
                self._tickets.clear()
                return False
            if status != 200:
                return False
            accepted, refused = len(lines), 0
            try:
                answer = json.loads(text)
                accepted = int(answer.get("acc", accepted))
                refused = int(answer.get("rej", 0))
            except (ValueError, TypeError, AttributeError):
                pass
            self.sent += accepted
            self.refused += refused
            self.awarded += sum(
                1 for record in batch[:used]
                if isinstance(record, dict)
                and record.get("event") == "AwardAchievement")
            del self._pending[:used]
        return True

    def _run(self):
        last_flush = 0.0
        delay = FLUSH_SECONDS
        try:
            while True:
                stopping = self._stopped.wait(1.0)
                self.read_new()
                now = self._clock()
                due = (len(self._pending) >= MAX_BATCH
                       or now - last_flush >= delay or stopping)
                if self._pending and due:
                    if self.flush():
                        delay = FLUSH_SECONDS
                    else:
                        delay = RETRY_SECONDS
                    last_flush = now
                if stopping:
                    break
        except Exception:
            # What other people see is never worth a traceback in a launch.
            pass

    def summary(self):
        """One line for the launch log, or None when nothing happened."""
        if not (self.sent or self.refused or self.dropped or self._pending):
            return None
        parts = ["%d game event%s sent to Xbox Live"
                 % (self.sent, "" if self.sent == 1 else "s")]
        if self.awarded:
            parts.append("%d achievement report%s among them"
                         % (self.awarded, "" if self.awarded == 1 else "s"))
        if self.refused:
            parts.append("%d refused" % self.refused)
        unsent = len(self._pending) + self.dropped
        if unsent:
            parts.append("%d not sent" % unsent)
        return ", ".join(parts) + "."


def start_session(title, path=None):
    """Forward this session's events; always returns a :class:`Forwarder`."""
    try:
        forwarder = Forwarder(path=path, title=title)
        if forwarder.active:
            forwarder.start()
        return forwarder
    except Exception:
        return Forwarder()


def stop_session(forwarder, timeout=STOP_TIMEOUT_SECONDS):
    """Stop forwarding and say in the log what was sent."""
    try:
        forwarder.stop(timeout)
        line = forwarder.summary()
        if line:
            info(line)
    except Exception:
        pass
