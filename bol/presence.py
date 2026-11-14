"""bol.presence — tell Xbox Live the account is playing (#238, #243).

Minecraft under Wine never says it is running. Measured against a live
account, straight after a full launch, the presence service still answered

    {"state":"Offline",
     "lastSeen":{"titleName":"Minecraft Launcher","timestamp":"…12 days ago"}}

which is the whole of the report: the player's own card reads "Offline" in
the dressing room and in the social tab, and their friends never see them in
Minecraft. Presence is written by XSAPI on Windows, and XSAPI is the layer
that does not come up under Wine, so nothing writes it and no amount of
reading fixes it -- the game reads its own presence back correctly, and what
it reads is the truth.

The write the real client makes is a single POST, to an audience the
launcher already holds a token for (the ``http://xboxlive.com`` XSTS token
pre-auth mints for Friends/Social):

    POST userpresence.xboxlive.com/users/xuid(<xuid>)/devices/current/titles/current
    Authorization: XBL3.0 x=<uhs>;<token>
    X-Xbl-Contract-Version: 3
    {"state": "active"}

The title is deliberately not named in the body. The service takes it from
the token's own title claim -- sending an ``id`` is answered 400
ArgumentError -- and reports "Minecraft". So this can say only that Minecraft
is being played, by the account that is playing it, and cannot be pointed at
another title.

A session is taken down on the way out, and Xbox lets an unfed record decay
on its own besides -- measured to "Away" within seven minutes -- so a
launcher that is killed mid-game cannot leave anyone showing as playing for
long. That decay is also what ``HEARTBEAT_SECONDS`` is chosen to stay inside.

Nothing here may raise into a launch, and nothing here gates one: presence is
what other people see, never whether the game runs.
"""
# SPDX-License-Identifier: MIT

import json
import os
import threading
import time

from .config import DATA
from .log import info, warn

PRESENCE_URL = ("https://userpresence.xboxlive.com/users/xuid(%s)"
                "/devices/current/titles/current")
PRESENCE_STATE_URL = "https://userpresence.xboxlive.com/users/xuid(%s)?level=all"
# Xbox holds an "active" title for a few minutes past the last heartbeat and
# then lets it decay on its own. Measured on the live service from a single
# write: Online through minute 6, "Away" at minute 7. Away is not the same as
# online to the people reading it -- the card says "Away" and the account
# stops looking playable -- so the interval has to stay well inside that, not
# merely inside the eventual Offline. Four minutes leaves a third of the
# window spare and is far under the endpoint's own rate limit (3 requests per
# 15 seconds).
HEARTBEAT_SECONDS = 240
# First retry after a refused heartbeat; doubles up to HEARTBEAT_SECONDS.
RETRY_SECONDS = 30
TIMEOUT_SECONDS = 15
# How long the teardown waits for the "inactive" write before moving on. The
# thread is a daemon and finishes the post regardless; this only decides how
# much of the wait happens in front of the rest of the teardown.
STOP_TIMEOUT_SECONDS = 5.0


def preauth_path():
    """Where ``xbl_preauth`` leaves the payload this module reads."""
    return DATA / "winegdk-preauth" / "device.json"


def presence_enabled(settings=None, environ=None):
    """Whether the play session may be published to Xbox Live.

    On by default: it is what the account's friends expect to see, it is what
    every other Minecraft client does, and it says only that Minecraft is
    being played. Settings turns it off for good; BOL_XBL_PRESENCE overrides
    both for one run.
    """
    environ = os.environ if environ is None else environ
    forced = (environ.get("BOL_XBL_PRESENCE") or "").strip().lower()
    if forced in {"0", "false", "no", "off"}:
        return False
    if forced in {"1", "true", "yes", "on"}:
        return True
    return bool((settings or {}).get("xbl_presence", True))


class Credentials:
    """The three pre-auth fields a presence heartbeat needs."""

    __slots__ = ("token", "uhs", "xuid", "expiry")

    def __init__(self, token, uhs, xuid, expiry=None):
        self.token = token
        self.uhs = uhs
        self.xuid = xuid
        self.expiry = expiry

    def usable(self, now=None, min_ttl=60):
        """Whether a heartbeat sent now would still be inside the token.

        An unknown expiry counts as usable: legacy payloads predate the epoch
        field, and a token that has in fact expired is answered 401, which
        ends the session on its own.
        """
        if self.expiry is None:
            return True
        return self.expiry > (time.time() if now is None else now) + min_ttl


def load_credentials(path=None):
    """Read the pre-auth payload, or None when it cannot serve a heartbeat.

    Deliberately tolerant: this is decoration built on a file another part of
    the launcher owns, and every way it can be absent or half-written is the
    same answer -- do not publish anything.
    """
    path = preauth_path() if path is None else path
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    token = payload.get("xbl_token")
    uhs = payload.get("xbl_uhs")
    xuid = payload.get("xbl_xuid")
    if not (isinstance(token, str) and token
            and isinstance(uhs, (str, int)) and str(uhs)
            and isinstance(xuid, (str, int)) and str(xuid).isdigit()):
        return None
    expiry = payload.get("xbl_token_expiry_epoch")
    try:
        expiry = int(expiry)
    except (TypeError, ValueError):
        expiry = None
    return Credentials(token, str(uhs), str(xuid), expiry)


def _request(credentials, method, url, body=None, timeout=TIMEOUT_SECONDS,
             contract="3"):
    """One authenticated call; returns (status, text) or (None, reason)."""
    import urllib.error
    import urllib.request

    headers = {
        "Authorization": "XBL3.0 x=%s;%s" % (credentials.uhs,
                                             credentials.token),
        "x-xbl-contract-version": contract,
        "Accept": "application/json",
        "Accept-Language": "en-US",
    }
    data = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        try:
            detail = error.read().decode("utf-8", "replace")
        except Exception:  # the status is what matters; a body is a bonus
            detail = ""
        return error.code, detail
    except Exception as error:
        return None, type(error).__name__


def _is_final(status):
    """Whether a refused heartbeat is worth stopping over rather than retrying.

    Anything the service will keep answering the same way: an unaccepted
    token (401/403), and a request it considers malformed (any other 4xx),
    which would be a fault in this module rather than a passing condition.
    429 is explicitly not one of those -- it means *slow down*, and the
    back-off does.
    """
    if status is None:
        return False
    return 400 <= status < 500 and status != 429


def write_state(credentials, state, timeout=TIMEOUT_SECONDS):
    """Publish ``active`` or ``inactive`` once. Returns the HTTP status.

    None means the request never reached Xbox Live (offline, DNS, timeout),
    which is not distinguishable from -- and is treated exactly like -- a
    service that answered badly.
    """
    status, _detail = _request(credentials, "POST",
                               PRESENCE_URL % credentials.xuid,
                               {"state": state}, timeout=timeout)
    return status


def read_state(credentials, timeout=TIMEOUT_SECONDS):
    """What Xbox Live currently says about the account: "Online", "Offline"…

    Returns None when the answer cannot be read. Used by diagnostics only --
    the heartbeat never needs to ask.
    """
    status, text = _request(credentials, "GET",
                            PRESENCE_STATE_URL % credentials.xuid,
                            timeout=timeout)
    if status != 200:
        return None
    try:
        state = json.loads(text).get("state")
    except (ValueError, TypeError, AttributeError):
        return None
    return state if isinstance(state, str) else None


PEOPLEHUB_URL = ("https://peoplehub.xboxlive.com/users/me/people/social"
                 "/decoration/multiplayersummary,presencedetail")
# PeopleHub's own contract, and the one this decoration list was measured
# against. It also answers on 3, but that is not a reason to ask on 3.
PEOPLEHUB_CONTRACT = "5"
# Every Minecraft Bedrock title a friend can host a world from, whichever
# platform it runs on: crossplay makes all of them joinable from here. Read
# off live joinable activities and presence (2026-11-12): Windows,
# Win32/Android, Xbox, PlayStation, iOS. "Minecraft Launcher" (1794566092)
# is deliberately absent -- nobody hosts a world in it.
MINECRAFT_TITLE_IDS = frozenset({
    896928775, 1739947436, 1791712750, 1828326430, 2044456598, 1810924247,
})


class SocialSnapshot:
    """What Xbox Live says about this account and the people it plays with.

    ``state`` is the account's own presence ("Online", "Away", "Offline") or
    None when it could not be read. ``friends`` is how many the social graph
    returned, and ``in_session`` how many of them Xbox is currently
    publishing a multiplayer session for -- which is the question behind "I
    can't join my friend's world" (#243, #244) and the one nobody could
    answer, because nothing measured it.

    ``in_session`` counts the friends with at least one joinable Minecraft
    activity in ``multiplayerSummary.joinableActivities``. Up to 2.3.1 this
    asked for a decoration named ``multiplayer``, which PeopleHub ignores:
    every friend then came back with ``multiplayerSummary: null`` and the
    count was always zero, even with a friend hosting a world. The real name
    is ``multiplayersummary``; with it every friend carries a summary --
    ``{"joinableActivities": [], "partyDetails": [], "inParty": 0}`` when
    there is nothing to join -- so a summary existing means nothing and only
    a joinable activity of a Minecraft title counts (measured 2026-11-12).
    """

    __slots__ = ("state", "friends", "in_session", "error")

    def __init__(self, state=None, friends=None, in_session=None, error=None):
        self.state = state
        self.friends = friends
        self.in_session = in_session
        self.error = error


def _joinable_in_minecraft(person):
    """Whether PeopleHub lists a Minecraft world this friend can be joined in."""
    summary = person.get("multiplayerSummary") \
        if isinstance(person, dict) else None
    activities = summary.get("joinableActivities") \
        if isinstance(summary, dict) else None
    if not isinstance(activities, list):
        return False
    for activity in activities:
        if not isinstance(activity, dict):
            continue
        try:
            title = int(activity.get("titleId"))
        except (TypeError, ValueError):
            continue
        if title in MINECRAFT_TITLE_IDS:
            return True
    return False


def social_snapshot(credentials=None, timeout=TIMEOUT_SECONDS, path=None):
    """Ask Xbox Live the three questions a "can't join" report leaves open."""
    if credentials is None:
        credentials = load_credentials(path)
    if credentials is None:
        return SocialSnapshot(error="no linked account to ask about")
    if not credentials.usable():
        return SocialSnapshot(error="the Xbox Live token has expired")
    state = read_state(credentials, timeout=timeout)
    status, text = _request(credentials, "GET", PEOPLEHUB_URL,
                            timeout=timeout, contract=PEOPLEHUB_CONTRACT)
    if status != 200:
        return SocialSnapshot(
            state=state,
            error="the friends list could not be read (%s)"
                  % ("no answer" if status is None else "HTTP %s" % status))
    try:
        people = json.loads(text)["people"]
        if not isinstance(people, list):
            raise TypeError
    except (ValueError, TypeError, KeyError):
        return SocialSnapshot(state=state,
                              error="the friends list could not be read "
                                    "(unexpected answer)")
    in_session = sum(1 for person in people if _joinable_in_minecraft(person))
    return SocialSnapshot(state=state, friends=len(people),
                          in_session=in_session)


class Session:
    """One play session, kept online on Xbox Live by a thread of its own.

    Inert unless it was given credentials, so a launch can always ``start()``
    it and always ``stop()`` it without asking whether it ever began.
    """

    def __init__(self, credentials=None):
        self._credentials = credentials
        self._stopped = threading.Event()
        self._thread = None
        # Set once Xbox Live has accepted the first heartbeat; tests and
        # `doctor` read it, nothing else depends on it.
        self.announced = threading.Event()

    @property
    def active(self):
        """Whether anything is being published at all."""
        return self._credentials is not None

    def start(self):
        """Begin publishing, in the background. Returns self."""
        if not self.active or self._thread is not None:
            return self
        self._thread = threading.Thread(
            target=self._run, name="xbl-presence", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout=STOP_TIMEOUT_SECONDS):
        """Take the presence down and let the thread finish.

        Unlike a socket, an Xbox presence record is not cleared by going
        away, so the thread is asked to write "inactive" on its way out. It
        is a daemon and the service drops the record by itself a few minutes
        later, so this waits briefly and then gives up rather than holding a
        launch teardown open on a network call.
        """
        self._stopped.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)

    # -- the thread

    def _run(self):
        published = False
        backoff = RETRY_SECONDS
        try:
            while not self._stopped.is_set():
                if not self._credentials.usable():
                    # Sixteen hours in. Stop rather than spend the rest of
                    # the session collecting 401s.
                    break
                status = write_state(self._credentials, "active")
                if status == 200:
                    published = True
                    backoff = RETRY_SECONDS
                    self.announced.set()
                    if self._stopped.wait(HEARTBEAT_SECONDS):
                        break
                    continue
                if _is_final(status):
                    break
                # Everything else -- no answer at all, a rate limit, a bad
                # gateway -- is a moment, not a verdict. A session lasts
                # hours and a lost Wi-Fi minute must not be what leaves
                # someone invisible for the rest of it, so this retries for
                # as long as the game runs, backing off to the ordinary
                # heartbeat rate rather than hammering a service that is
                # already unhappy.
                if self._stopped.wait(backoff):
                    break
                backoff = min(backoff * 2, HEARTBEAT_SECONDS)
        except Exception:
            # Presence is what other people see. A surprise ends it quietly
            # instead of printing anything into a launch log.
            pass
        finally:
            self.announced.clear()
            if published:
                try:
                    write_state(self._credentials, "inactive",
                                timeout=STOP_TIMEOUT_SECONDS)
                except Exception:
                    pass


def start_session(settings=None, environ=None, path=None):
    """Publish the play session on Xbox Live for as long as it lasts.

    Always returns a :class:`Session` -- an inert one when presence is off or
    when the pre-auth payload cannot serve a heartbeat -- so a launch can
    stop it without asking whether it ever began.
    """
    environ = os.environ if environ is None else environ
    try:
        if not presence_enabled(settings, environ):
            return Session()
        credentials = load_credentials(path)
        if credentials is None or not credentials.usable():
            return Session()
        info("Telling Xbox Live you are playing, so your friends can see "
             "you and join.")
        return Session(credentials).start()
    except Exception:
        # A launch in progress is worth more than its decoration.
        return Session()


def presence_summary(settings=None, environ=None, path=None):
    """One line for `doctor`: whether friends can see this account in-game.

    Offline by design -- it says what the launcher will do at the next
    launch, and asking Xbox Live would put a network call in a check people
    run precisely when the network is the thing they suspect.
    """
    environ = os.environ if environ is None else environ
    if not presence_enabled(settings, environ):
        return ("off (Settings > Accounts) — friends see this account as "
                "offline while you play")
    credentials = load_credentials(path)
    if credentials is None:
        return "on, once an account is linked (nothing to publish yet)"
    if not credentials.usable():
        return "on, but the Xbox Live token has expired — PLAY refreshes it"
    return "on (published as Minecraft while the game runs)"


def presence_problem(settings=None, environ=None, path=None):
    """What to warn about, if anything, alongside the summary line."""
    environ = os.environ if environ is None else environ
    if not presence_enabled(settings, environ):
        return ("Xbox Live presence is switched off, so your account stays "
                "'Offline' to your friends while you play and they cannot "
                "join your world or invite you. Turn it back on in Settings "
                "> Accounts.")
    return None


def warn_if_unavailable(session, settings=None, environ=None):
    """Say so, once, when a launch that meant to publish presence could not.

    Silence would be worse than useless here: "my friends see me offline" is
    exactly what gets reported (#238, #243), and this is the launcher's own
    answer to it. Switching presence off is a choice, not a fault, so that
    case says nothing.
    """
    if session is None or session.active:
        return
    if not presence_enabled(settings, environ):
        return
    warn("Xbox Live was not told you are playing, so your account stays "
         "'Offline' to your friends and they cannot join your world or "
         "invite you. Sign in again from the launcher if this persists.")
