"""Close what Minecraft leaves running in the prefix once it has exited.

The game's WebView2 runtime starts ``MicrosoftEdgeUpdate.exe /c`` a couple of
minutes into a session, and under Wine that updater never exits. Proton's
wrapper waits for every process in the prefix before it returns, so closing
Minecraft did not end the session: the launch lock stayed held, the prefix
stayed busy, and PLAY answered that Minecraft was still running until the
updater was killed by hand.

Only those helpers are touched, and only once the game itself has been gone
for a few seconds; Wine's own services leave on their own once they are.
"""

import os
import signal
import time

from .log import info
from .prefix import prefix_processes

GAME_IMAGE = "minecraft.windows.exe"
# Started for the game's WebView2, and of no use once the game has gone.
LEFTOVER_IMAGES = ("microsoftedgeupdate.exe", "msedgewebview2.exe")

# How long the game has to have been gone before its helpers are closed: a
# game that restarts itself (a language change, an update) comes back well
# within it.
GRACE_S = 5.0
# How long a helper gets to leave after SIGTERM before SIGKILL.
KILL_AFTER_S = 5.0
# The prefix is found by reading every process's environment; once every
# couple of seconds is plenty for something measured in seconds.
SCAN_EVERY_S = 2.0


def _image(pid):
    """Lower-case file name of a Wine process's executable, or ''.

    A Wine process carries its Windows path as argv[0]; a Linux one, such as
    the umu-run wrapper that names the game only as an argument, does not.
    """
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            argv0 = f.read().split(b"\0", 1)[0]
    except OSError:
        return ""
    name = argv0.decode("utf-8", "replace").replace("/", "\\")
    return name.rsplit("\\", 1)[-1].strip().lower()


class LeftoverReaper:
    """Call :meth:`tick` while waiting on the game wrapper."""

    def __init__(self, prefix, scan=prefix_processes, image=_image,
                 kill=os.kill, clock=time.monotonic):
        self.prefix = prefix
        self._scan = scan
        self._image = image
        self._kill = kill
        self._clock = clock
        self._next_scan = 0.0
        self._game_seen = False
        self._gone_since = None
        self._termed = {}
        self._announced = False

    def tick(self):
        now = self._clock()
        if now < self._next_scan:
            return []
        self._next_scan = now + SCAN_EVERY_S
        try:
            images = {pid: self._image(pid)
                      for pid in self._scan(self.prefix)}
        except Exception:
            return []
        if GAME_IMAGE in images.values():
            self._game_seen = True
            self._gone_since = None
            self._termed.clear()
            return []
        if not self._game_seen:
            return []
        if self._gone_since is None:
            self._gone_since = now
        if now - self._gone_since < GRACE_S:
            return []
        closed = []
        for pid, name in images.items():
            if name not in LEFTOVER_IMAGES:
                continue
            first = self._termed.setdefault(pid, now)
            sig = signal.SIGKILL if now - first >= KILL_AFTER_S \
                else signal.SIGTERM
            if sig == signal.SIGTERM and first != now:
                continue  # already asked; give it time
            try:
                self._kill(pid, sig)
            except OSError:
                continue
            closed.append((pid, name, sig))
        if closed and not self._announced:
            self._announced = True
            info("Minecraft has closed; closing what its WebView2 left "
                 "running in Wine ("
                 + ", ".join(sorted({n for _p, n, _s in closed}))
                 + ") so the session can end.")
        return closed
