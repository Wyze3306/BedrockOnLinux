"""bol.supervision — a stop signal waits for the game it would orphan.

A launch records a GPU-safety marker before the game starts and clears it
once the game has returned (bol/gpu_safety.py). SIGTERM and SIGHUP used to
keep their default disposition, so whatever stopped the launcher -- the
computer powering off with Minecraft open, a closed terminal, a stopped
systemd scope -- ended it on the spot, before that teardown ran. The game
received the same signal and was gone two seconds later, but the marker
stayed, and the next boot refused PLAY as a "previous boot" GPU incident
until the doctor acknowledged it.

While a session is supervised, those signals are only recorded here. The
launch's wait loop sees the request, keeps waiting a bounded grace for the
game to return, and the normal teardown runs. Then the process ends with the
signal it was sent, as it would have before. With no session, a signal ends
the process at once, exactly as the default disposition did.

Everything here is imported at module level, and so is everything the
launch's teardown needs: the AppImage's FUSE server is stopped together with
the launcher, after which nothing can be imported from the mount.
"""
# SPDX-License-Identifier: MIT

import os
import signal
import threading
import time

# What a shutdown, a stopped scope or a closed terminal sends. SIGINT keeps
# Python's KeyboardInterrupt: Ctrl+C means stop now, and Wine does not exit
# on SIGINT, so waiting for the game there would only stall the terminal.
DEFERRED_SIGNALS = (signal.SIGTERM, signal.SIGHUP)

# How long a stop request waits for the game. The game gets the same SIGTERM
# from systemd at poweroff and was measured gone in two seconds; the user
# manager allows a scope 90 s before SIGKILL, and the idle-prefix check that
# follows the game's return can take up to 10 s more.
STOP_GRACE_S = 20

# The GUI's main thread is held at most this long for the launch thread to
# finish its teardown: the grace, the idle check, and the rest of it.
HOLD_LIMIT_S = STOP_GRACE_S + 25

_previous = {}
_state = {"active": False, "signum": None, "since": None}
_session_over = threading.Event()
_session_over.set()


def install():
    """Defer SIGTERM and SIGHUP while a game is supervised.

    Only the main thread may install signal handlers; anywhere else this
    returns False. A signal that is ignored (``nohup``) or already handled
    by someone else is left alone. Returns whether a handler is in place.
    """
    if threading.current_thread() is not threading.main_thread():
        return False
    for signum in DEFERRED_SIGNALS:
        if signum in _previous:
            continue
        current = signal.getsignal(signum)
        if current is not signal.SIG_DFL:
            continue
        signal.signal(signum, _on_signal)
        _previous[signum] = current
    return bool(_previous)


def _on_signal(signum, _frame):
    # Runs on the main thread between two bytecodes, possibly in the middle
    # of a print: record and return, never block here.
    if not _state["active"]:
        _redeliver(signum)
        return
    if _state["signum"] is None:
        # The first request starts the grace. At poweroff a session scope
        # sends SIGHUP right after SIGTERM, and that second signal must
        # neither restart the grace nor cut it short.
        _state["since"] = time.monotonic()
        _state["signum"] = signum


def begin_session():
    """From now until end_session(), a stop signal waits for the game."""
    _session_over.clear()
    _state.update(active=True, signum=None, since=None)


def stop_requested_for(clock=time.monotonic):
    """Seconds since a stop signal arrived during this session, or None."""
    since = _state["since"]
    if _state["signum"] is None or since is None:
        return None
    return max(0.0, clock() - since)


def end_session():
    """The session's teardown is done; deliver any stop signal it held.

    On the main thread (the CLI's ``play``) the process ends here. On a
    launch thread, the main thread waiting in hold_until_session_ends() is
    released and ends it.
    """
    _state["active"] = False
    _session_over.set()
    signum = _state["signum"]
    if (signum is not None
            and threading.current_thread() is threading.main_thread()):
        _redeliver(signum)


def hold_until_session_ends(timeout=HOLD_LIMIT_S):
    """For the GUI's main thread: wait out the session, then stop.

    Called from the event loop once a signal has woken it, never from the
    handler. Holding the main thread also keeps Qt from exiting by itself
    when the display server goes down in the same shutdown, which would
    take the launch thread's teardown with it. A no-op unless a stop
    signal is held.
    """
    signum = _state["signum"]
    if signum is None:
        return
    _session_over.wait(timeout)
    _redeliver(signum)


def _redeliver(signum):
    """End the process with ``signum``, as the default disposition would."""
    signal.signal(signum, _previous.get(signum, signal.SIG_DFL))
    signal.raise_signal(signum)
    # Only reached if the signal did not end the process.
    os._exit(128 + signum)
