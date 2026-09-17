"""bol.log — console logging, the BolError exception and die()."""
# SPDX-License-Identifier: MIT

import os
import select
import shutil
import subprocess
import sys

IS_TTY = sys.stdout.isatty()
_LOG_SINK = None       # GUI hook: callable(str)

# The leading tag is the protocol consumed by the GUI log sink.
_LEVELS = {
    "::": ("info ", "\033[38;5;111m", "",               "#6ea8fe", "#aeb4bf"),
    "OK": ("ok   ", "\033[38;5;78m",  "",               "#5bc46a", "#aeb4bf"),
    "!!": ("warn ", "\033[38;5;179m", "\033[38;5;179m", "#e0b341", "#e6cd86"),
    "xx": ("error", "\033[38;5;167m", "\033[38;5;167m", "#e06c5b", "#f0a39a"),
}
_ANSI_RESET = "\033[0m"


def _reader_gone(fd):
    """Whether nothing reads ``fd`` any more.

    A pipe whose reader exited reports POLLERR, a terminal that hung up
    POLLHUP; a file, /dev/null or a live pipe reports neither.
    """
    try:
        poller = select.poll()
        poller.register(fd, select.POLLOUT)
        ready = poller.poll(0)
    except (OSError, ValueError):
        return False
    dead = select.POLLERR | select.POLLHUP | select.POLLNVAL
    return any(mask & dead for _fd, mask in ready)


def _detach_dead_console():
    """Point standard streams nobody reads any more at /dev/null.

    Later lines, the flush at interpreter exit and every child process that
    inherits the descriptors then write somewhere that accepts them, instead
    of failing on -- or, for a child, being killed by SIGPIPE from -- a pipe
    with no reader.
    """
    try:
        null = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        return
    try:
        for stream in (sys.stdout, sys.stderr):
            try:
                fd = stream.fileno()
            except (AttributeError, OSError, ValueError):
                continue
            if _reader_gone(fd):
                os.dup2(null, fd)
    finally:
        os.close(null)


def _print(line):
    """Write one console line, surviving a console that has gone away.

    A desktop entry can start the launcher with its output on a pipe whose
    reader exits right after the launch -- gtk-launch does -- so this print
    raised BrokenPipeError and the PLAY worker that logged the line died with
    it, never reaching the sign-in dialog (#261). The GUI has already shown
    the line through its sink by then.
    """
    try:
        print(line, flush=True)
    except OSError:
        _detach_dead_console()


def _emit(tag, m):
    if _LOG_SINK:
        try:
            _LOG_SINK(f"{tag} {m}")
        except Exception:
            pass
    lvl = _LEVELS.get(tag)
    if not lvl:
        _print(f"{tag} {m}")
        return
    label, alab, amsg, _, _ = lvl
    if IS_TTY:
        tail = f"{amsg}{m}{_ANSI_RESET}" if amsg else m
        _print(f"{alab}{label}{_ANSI_RESET}  {tail}")
    else:
        _print(f"{label}  {m}")


def info(m): _emit("::", m)
def ok(m):   _emit("OK", m)
def warn(m): _emit("!!", m)
def err(m):  _emit("xx", m)


def desktop_notify(message, summary=None):
    """Put a message on screen for runs started without a visible terminal.

    A desktop or Steam shortcut discards stdout, so an unreported failure is
    indistinguishable from the launcher doing nothing at all.
    """
    notifier = shutil.which("notify-send")
    if not notifier:
        return False
    try:
        subprocess.run(
            [notifier, "--app-name", "BedrockOnLinux",
             summary or "BedrockOnLinux", str(message)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


class BolError(Exception):
    pass


def die(m):
    err(m)
    exc = BolError(m)
    exc.reported = True
    raise exc
