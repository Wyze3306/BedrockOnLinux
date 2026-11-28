"""bol.discord — show the play session on Discord (Rich Presence).

Discord's desktop client listens on a Unix socket in the user's runtime
directory and speaks a small framed protocol: a little-endian 32-bit opcode,
a little-endian 32-bit length, then a JSON payload. That is the entire
dependency — no library to install, no account of ours, no network call. When
Discord is not running the socket simply does not exist, which is the only
failure this module ever expects to meet, and it stays silent about it.

While a game runs, friends see "Playing Bedrock on Linux" with the edition
and build being played, how long for, and buttons back to the project: the
launcher is found by word of mouth, and this is the launcher saying its own
name. Nothing about the account, the world or the server is sent — Discord is
told the edition and version, and nothing else.

Discord also recognises Minecraft.Windows.exe on its own and would print
"Playing Minecraft" beside, or instead of, this. The activity is therefore
tied to the game's own process id once that process exists: Discord then
knows the detected game and this presence are one session.

Discord clears a presence by itself the moment the socket closes, so a
launcher that is killed mid-game cannot leave anyone permanently "in game".
"""
# SPDX-License-Identifier: MIT

import json
import os
import select
import socket
import struct
import threading
import time
import uuid
from pathlib import Path

from .config import (
    DISCORD_APP_ID,
    DISCORD_INVITE,
    DISCORD_LARGE_IMAGE,
    DISCORD_SMALL_IMAGE,
    PRETTY,
    REPO_URL,
)

# Frame opcodes of Discord's local IPC.
_OP_HANDSHAKE = 0
_OP_FRAME = 1
_OP_CLOSE = 2
_OP_PING = 3
_OP_PONG = 4

# Discord numbers its socket per running client: the first one gets -0, a
# second client (Canary beside Stable, say) the next free index.
_SOCKET_NAMES = tuple(f"discord-ipc-{index}" for index in range(10))

# A sandboxed Discord keeps the same socket under its own runtime directory.
# The Flatpak and Snap builds are common enough on the distributions this
# launcher targets that skipping them would look like "it does not work".
_SOCKET_SUBDIRS = (
    "",
    "app/com.discordapp.Discord",
    "app/com.discordapp.DiscordCanary",
    "app/com.discordapp.DiscordPTB",
    "snap.discord",
    "snap.discord-canary",
)

_CONNECT_TIMEOUT = 2.0   # a local socket answers at once or not at all
_REPLY_TIMEOUT = 5.0     # Discord answers a SET_ACTIVITY within milliseconds
_POLL_SECONDS = 1.0      # how often the session thread notices it must stop
# Discord is often started after the game, or restarted during it. Looking
# for its socket is a handful of stat() calls, so this can be short enough
# that the presence appears about when Discord's window does.
_RETRY_SECONDS = 5.0
# How long the game's own process is looked for after the session begins.
# Wine takes a few seconds to get from the launcher's Popen to the game.
_GAME_PID_SECONDS = 120.0
_GAME_PID_POLL = 3.0

# What each edition is called on the card. The launcher's own names ("Minecraft
# for Windows") are the Store's; people say these.
_EDITION_NAMES = {
    "release": "Minecraft Bedrock",
    "preview": "Minecraft Preview",
    "uwp": "Minecraft Bedrock",
    "education": "Minecraft Education",
    "dungeons": "Minecraft Dungeons",
    "legends": "Minecraft Legends",
}

# Discord truncates longer strings and rejects an activity outright when a
# button label is over its limit.
_TEXT_LIMIT = 128
_LABEL_LIMIT = 32


def _runtime_dirs(environ):
    """The directories Discord may have put its socket in, best first."""
    dirs = []
    for var in ("XDG_RUNTIME_DIR", "TMPDIR", "TMP", "TEMP"):
        value = (environ.get(var) or "").strip()
        if value and value not in dirs:
            dirs.append(value)
    if "/tmp" not in dirs:
        dirs.append("/tmp")
    return dirs


def socket_candidates(environ=None):
    """Every path a running Discord could be listening on."""
    environ = os.environ if environ is None else environ
    paths = []
    for base in _runtime_dirs(environ):
        for subdir in _SOCKET_SUBDIRS:
            folder = Path(base, subdir) if subdir else Path(base)
            for name in _SOCKET_NAMES:
                paths.append(folder / name)
    return paths


def discord_socket(environ=None):
    """The socket of the running Discord, or None when none is running."""
    for path in socket_candidates(environ):
        try:
            if path.is_socket():
                return path
        except OSError:
            continue
    return None


def _read_exactly(sock, size):
    """Exactly ``size`` bytes, or None if Discord closed the connection."""
    chunks = b""
    while len(chunks) < size:
        block = sock.recv(size - len(chunks))
        if not block:
            return None
        chunks += block
    return chunks


def _send(sock, opcode, payload):
    body = json.dumps(payload).encode("utf-8")
    sock.sendall(struct.pack("<II", opcode, len(body)) + body)


def _recv(sock):
    """One frame as (opcode, payload), or None once the socket is done."""
    header = _read_exactly(sock, 8)
    if header is None:
        return None
    opcode, length = struct.unpack("<II", header)
    body = _read_exactly(sock, length) if length else b""
    if body is None:
        return None
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return opcode, payload


def _handshake(sock, app_id):
    """Introduce the application and wait for Discord's READY."""
    _send(sock, _OP_HANDSHAKE, {"v": 1, "client_id": str(app_id)})
    while True:
        frame = _recv(sock)
        if frame is None:
            return False
        opcode, payload = frame
        if opcode == _OP_PING:
            _send(sock, _OP_PONG, payload)
            continue
        if opcode == _OP_CLOSE:
            return False
        if opcode != _OP_FRAME:
            continue
        event = payload.get("evt")
        if event == "READY":
            return True
        if event == "ERROR":
            # An application id Discord does not know ends here. Nothing to
            # report: the player configured no presence, or a fork did.
            return False


def _set_activity(sock, activity, pid):
    """Ask Discord to show (activity) or drop (None) the play session.

    Returns the nonce, which Discord's answer carries back.
    """
    args = {"pid": int(pid)}
    if activity is not None:
        args["activity"] = activity
    nonce = str(uuid.uuid4())
    _send(sock, _OP_FRAME, {
        "cmd": "SET_ACTIVITY",
        "nonce": nonce,
        "args": args,
    })
    return nonce


def _await_reply(sock, nonce, timeout=None):
    """Discord's answer to one command: True accepted, False refused.

    None when no answer came in time (an older client that does not echo
    nonces) or the socket closed. Pings met on the way are answered.
    """
    deadline = time.monotonic() + (_REPLY_TIMEOUT if timeout is None
                                   else timeout)
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        readable, _, _ = select.select([sock], [], [], left)
        if not readable:
            return None
        frame = _recv(sock)
        if frame is None:
            return None
        opcode, payload = frame
        if opcode == _OP_PING:
            _send(sock, _OP_PONG, payload)
            continue
        if opcode == _OP_CLOSE:
            return None
        if opcode != _OP_FRAME or payload.get("nonce") != nonce:
            continue
        return payload.get("evt") != "ERROR"


def _plain(activity):
    """The activity with only the fields every Discord client has accepted.

    Newer fields (``status_display_type``, ``details_url``) are refused as a
    whole activity by a client that predates them, and a refused activity is
    a session that shows nothing at all.
    """
    keep = ("details", "state", "timestamps", "assets", "buttons",
            "instance")
    return {key: activity[key] for key in keep if key in activity}


def _artwork():
    """The ``assets`` block, with only the images that are actually set.

    An empty value has to be left out rather than sent as "": Discord reads it
    as an asset key, finds nothing behind it, and shows a blank frame where
    the image should be. Sending no assets at all is what gets the plain card.
    """
    assets = {}
    if DISCORD_LARGE_IMAGE:
        assets["large_image"] = DISCORD_LARGE_IMAGE
        assets["large_text"] = PRETTY
    if DISCORD_SMALL_IMAGE:
        assets["small_image"] = DISCORD_SMALL_IMAGE
        assets["small_text"] = "Linux"
    return {"assets": assets} if assets else {}


def game_name(edition=None, settings=None):
    """What the card calls the game being played.

    ``edition`` is the launcher's entry for the build (bol.config.MC_PRODUCTS);
    without one, the edition recorded in the settings is used.
    """
    edition_id = ""
    if isinstance(edition, dict):
        edition_id = str(edition.get("id") or "").strip().lower()
        if edition_id not in _EDITION_NAMES and edition.get("name"):
            return str(edition["name"])
    if not edition_id:
        edition_id = str((settings or {}).get("mc_edition") or "release")
        edition_id = edition_id.strip().lower()
    return _EDITION_NAMES.get(edition_id, _EDITION_NAMES["release"])


def session_activity(settings=None, started_at=None, edition=None,
                     version=None):
    """What Discord is asked to display for one play session.

    The heading is the application's own name — "Bedrock on Linux" — so the
    two lines below say what is being played inside it, and the timestamp
    turns into the session clock Discord counts up on its own.

    ``version`` is the build as Mojang numbers it (1.26.50.25). The settings'
    ``mc_version`` is only a fallback: it holds the catalogue's build tag,
    which is not what anyone calls a version.
    """
    settings = settings or {}
    title = game_name(edition, settings)
    version = str(version or settings.get("mc_version") or "").strip()
    details = f"{title} {version}".strip()[:_TEXT_LIMIT]
    return {
        # 0 is the application's name: the member list then reads "Bedrock
        # on Linux" rather than the edition.
        "status_display_type": 0,
        "details": details,
        "details_url": REPO_URL,
        "state": "Playing on Linux",
        "timestamps": {"start": int(started_at or time.time())},
        **_artwork(),
        # The advertisement proper: whoever sees the session can get the
        # launcher from it. Discord allows two, 32 characters each, and shows
        # them to everyone but the player.
        "buttons": [
            {"label": "Get it on GitHub"[:_LABEL_LIMIT], "url": REPO_URL},
            {"label": "Join the Discord"[:_LABEL_LIMIT],
             "url": DISCORD_INVITE},
        ],
        "instance": False,
    }


def _in_flatpak():
    return Path("/.flatpak-info").exists()


def _proc_parent(pid, proc_root):
    try:
        stat = (proc_root / str(pid) / "stat").read_text()
    except OSError:
        return None
    # The command name is in parentheses and may itself hold spaces or ")".
    fields = stat[stat.rfind(")") + 2:].split()
    try:
        return int(fields[1])
    except (IndexError, ValueError):
        return None


def _runs_exe(pid, exe_name, proc_root):
    """Whether a process is Wine running ``exe_name``."""
    folder = proc_root / str(pid)
    want = exe_name.lower()
    try:
        comm = (folder / "comm").read_text().strip().lower()
    except OSError:
        return False
    # Wine names the Unix process after the Windows image; the kernel keeps
    # 15 characters of it.
    if comm and comm == want[:15]:
        return True
    try:
        argv0 = (folder / "cmdline").read_bytes().split(b"\0", 1)[0]
    except OSError:
        return False
    tail = argv0.decode("utf-8", "replace").replace("\\", "/")
    return tail.rsplit("/", 1)[-1].lower() == want


def find_game_pid(exe_name, ancestor=None, proc_root="/proc"):
    """The process id of the running game, or None.

    The process that runs ``exe_name`` under Wine — the one Discord itself
    recognises — preferring one started by ``ancestor`` (the launcher's own
    game process) when several profiles are playing at once.
    """
    if not exe_name:
        return None
    proc_root = Path(proc_root)
    try:
        pids = [int(entry.name) for entry in proc_root.iterdir()
                if entry.name.isdigit()]
    except OSError:
        return None
    found = [pid for pid in pids if _runs_exe(pid, exe_name, proc_root)]
    if not found:
        return None
    if ancestor:
        for pid in sorted(found, reverse=True):
            seen, parent = 0, pid
            while parent and parent > 1 and seen < 64:
                if parent == ancestor:
                    return pid
                parent, seen = _proc_parent(parent, proc_root), seen + 1
    return max(found)


def presence_app_id(environ=None):
    """The Discord application to appear as, if one is configured."""
    environ = os.environ if environ is None else environ
    return (environ.get("BOL_DISCORD_APP_ID") or DISCORD_APP_ID or "").strip()


def presence_enabled(settings=None, environ=None):
    """Whether the play session may be shown on Discord.

    On by default — it is the point of the feature, it is what every other
    game launcher does, and it says only what is being played. The switch in
    Settings turns it off for good; BOL_DISCORD_PRESENCE overrides both for
    one run.
    """
    environ = os.environ if environ is None else environ
    forced = (environ.get("BOL_DISCORD_PRESENCE") or "").strip().lower()
    if forced in {"0", "false", "no", "off"}:
        return False
    if forced in {"1", "true", "yes", "on"}:
        return True
    return bool((settings or {}).get("discord_presence", True))


class Session:
    """One play session, kept on Discord by a thread of its own.

    Inert unless it was given an application id and something to show, so
    callers can always ``start()`` it and always ``stop()`` it. Nothing here
    may raise into a launch: the game must start, and go on running, with or
    without Discord.
    """

    def __init__(self, app_id="", activity=None, environ=None, pid=None,
                 game_pid=None):
        self._app_id = str(app_id or "")
        self._activity = activity
        self._environ = os.environ if environ is None else environ
        self._pid = os.getpid() if pid is None else pid
        # A callable returning the game's process id once it exists, or None.
        # Discord is then told that pid instead of the launcher's.
        self._game_pid = game_pid
        self._game_pid_until = time.monotonic() + _GAME_PID_SECONDS
        self._next_look = 0.0
        # Set once Discord refused the full activity: what is sent from then
        # on is the plain one.
        self._plain = False
        self._stopped = threading.Event()
        self._thread = None
        # Set once Discord has accepted the presence; tests and `doctor` read
        # it, nothing else depends on it.
        self.announced = threading.Event()

    @property
    def active(self):
        """Whether anything is being sent at all."""
        return bool(self._app_id and self._activity)

    def start(self):
        """Begin announcing, in the background. Returns self."""
        if not self.active or self._thread is not None:
            return self
        self._thread = threading.Thread(
            target=self._run, name="discord-presence", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout=3.0):
        """Take the presence down and let the thread finish.

        The thread is a daemon and the socket close alone clears the session
        on Discord's side, so this waits briefly and then gives up rather
        than holding a launch teardown open.
        """
        self._stopped.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)

    # -- the thread

    def _run(self):
        sock = None
        try:
            while not self._stopped.is_set():
                if sock is None:
                    sock = self._open()
                    if sock is None and self._stopped.wait(_RETRY_SECONDS):
                        break
                    continue
                if not self._serve(sock) or not self._follow_game(sock):
                    self._close(sock, clear=False)
                    sock = None
        except Exception:
            # Presence is decoration. A protocol surprise ends it quietly
            # instead of printing anything into a launch log.
            pass
        finally:
            if sock is not None:
                self._close(sock, clear=True)

    def _open(self):
        """A handshaken socket with the activity already set, or None."""
        for path in socket_candidates(self._environ):
            if self._stopped.is_set():
                return None
            try:
                if not path.is_socket():
                    continue
            except OSError:
                continue
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(_CONNECT_TIMEOUT)
            try:
                sock.connect(str(path))
                if _handshake(sock, self._app_id):
                    self._look_for_game()
                    if self._announce(sock):
                        return sock
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        return None

    def _activity_to_send(self):
        return _plain(self._activity) if self._plain else self._activity

    def _announce(self, sock):
        """Set the activity; False when Discord would not take it at all."""
        accepted = _await_reply(
            sock, _set_activity(sock, self._activity_to_send(), self._pid))
        if accepted is False and not self._plain:
            # An older client refusing a newer field: what it has always
            # accepted still shows the session.
            self._plain = True
            accepted = _await_reply(
                sock, _set_activity(sock, self._activity_to_send(),
                                    self._pid))
        if accepted is False:
            return False
        self.announced.set()
        return True

    def _look_for_game(self):
        """Switch to the game's pid if it has appeared. True if it changed."""
        now = time.monotonic()
        if self._game_pid is None or now > self._game_pid_until \
                or now < self._next_look:
            return False
        # A walk over /proc: every few seconds is soon enough.
        self._next_look = now + _GAME_PID_POLL
        try:
            pid = self._game_pid()
        except Exception:
            pid = None
        if not pid or pid == self._pid:
            return False
        self._pid = pid
        self._game_pid = None   # found: nothing left to look for
        return True

    def _follow_game(self, sock):
        """Re-send the activity under the game's pid once it is known."""
        if not self._look_for_game():
            return True
        try:
            return self._announce(sock)
        except OSError:
            return False

    def _serve(self, sock):
        """Answer Discord for a moment. False once the connection is gone."""
        try:
            readable, _, _ = select.select([sock], [], [], _POLL_SECONDS)
            if not readable:
                return True
            frame = _recv(sock)
        except OSError:
            return False
        if frame is None:
            return False
        opcode, payload = frame
        if opcode == _OP_CLOSE:
            return False
        if opcode == _OP_PING:
            try:
                _send(sock, _OP_PONG, payload)
            except OSError:
                return False
        return True

    def _close(self, sock, clear):
        if clear:
            # Closing the socket is what actually clears the session; asking
            # first only makes it immediate rather than a moment later.
            try:
                _set_activity(sock, None, self._pid)
            except OSError:
                pass
        try:
            sock.close()
        except OSError:
            pass
        self.announced.clear()


def start_session(settings=None, environ=None, started_at=None,
                  edition=None, version=None, exe_name=None, game_proc=None):
    """Announce a play session on Discord for as long as it lasts.

    Always returns a :class:`Session` — an inert one when presence is off,
    when no Discord application is configured, or when Discord is not
    running — so a launch can stop it without asking whether it ever began.

    ``exe_name`` is the Windows executable of the game and ``game_proc`` the
    process the launcher started for it: together they find the process
    Discord itself recognises as the game. Inside a Flatpak, process ids are
    the sandbox's own and mean nothing to a Discord outside it, so the
    launcher's own pid is kept there.
    """
    environ = os.environ if environ is None else environ
    try:
        if not presence_enabled(settings, environ):
            return Session(environ=environ)
        app_id = presence_app_id(environ)
        if not app_id:
            return Session(environ=environ)
        activity = session_activity(settings, started_at=started_at,
                                    edition=edition, version=version)
        finder = None
        if exe_name and not _in_flatpak():
            ancestor = getattr(game_proc, "pid", None)
            finder = lambda: find_game_pid(exe_name, ancestor)  # noqa: E731
        return Session(app_id, activity, environ=environ,
                       game_pid=finder).start()
    except Exception:
        # A launch in progress is worth more than its decoration: a machine
        # that cannot even spare a thread still gets to play.
        return Session(environ=environ)


def presence_summary(settings=None, environ=None):
    """One line for `doctor`: what friends see while you play."""
    environ = os.environ if environ is None else environ
    if not presence_app_id(environ):
        return "unavailable (no Discord application is configured)"
    if not presence_enabled(settings, environ):
        return "off (Settings > General > Discord)"
    path = discord_socket(environ)
    if path is None:
        return "on, but Discord is not running (nothing is sent)"
    return f"on ({path})"
