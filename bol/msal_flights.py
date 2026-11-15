"""Keep Minecraft Education's sign-in out of Windows' account broker (#351).

Education signs in through MSAL. In MSAL, flight 0x66 ("AAD to WAM") is on by
default and has no off switch: no registry value, setting or file reaches it.
With the flight on, a school account goes to the Web Account Manager.
Wine has no Web Account Manager (WebAuthenticationCoreManager is not
registered), so sign-in ends on "Something went wrong [4nsvw]" right after the
e-mail address. With the flight off, MSAL logs "disabled AAD flight" and signs
in through the IE WebBrowser control, which the engine hosts.

MSAL keeps its flights in a std::unordered_map<uint32, FlightStatus> where
status 0 means active. This module waits for that map in the running game,
then sets 0x66 to 1. It only writes after reading back the exact bytes of a
build it knows. Any other build, or a process it cannot reach (the Flatpak
sandbox isolates the game's processes), is left alone with a warning.
"""
# SPDX-License-Identifier: MIT

import os
import struct
import threading
import time
from pathlib import Path

from .log import info, warn

WAM_FOR_AAD = 0x66
# FlightStatus: 0 is active, anything else is not.
FLIGHT_OFF = 1

# Per build, as offsets from the image base:
#   getter   lea rax, [rip + global]: the function MSAL reads its global from
#   global   the MSAL global; [global] -> state, [state + 0x100] -> flights
#   defaults the dwords MSAL seeds its default flights from, 0x66 among them
# and the expected bytes at each, which pin the layout down.
KNOWN_BUILDS = (
    {
        "name": "1.26.33.0",
        "getter": 0xC0CC5AC,
        "global": 0xF5B07A0,
        "defaults": 0xC6AE250,
        "defaults_bytes": struct.pack("<4I", 0x66, 0x65, 0x74, 0x85),
    },
)

FLIGHTS_OFFSET = 0x100
# std::unordered_map (MSVC): its std::list sentinel pointer at +8.
LIST_OFFSET = 0x08
# _List_node<pair<const uint32, FlightStatus>>: next +0, key +0x10, value +0x14.
NODE_KEY = 0x10
NODE_VALUE = 0x14
MAX_NODES = 4096

POLL_S = 0.25
# MSAL starts with the game; the sign-in button is not up before it has.
DEADLINE_S = 600


def _image_base(pid, inode):
    """Return where this executable is mapped in ``pid``, or None."""
    try:
        maps = Path(f"/proc/{pid}/maps").read_text()
    except OSError:
        return None
    return parse_image_base(maps, inode)


def parse_image_base(maps, inode):
    """Return the start of the offset-0 mapping of ``inode`` in a maps text."""
    for line in maps.splitlines():
        fields = line.split(None, 5)
        if len(fields) < 6:
            continue
        span, _perms, offset, _dev, ino, _path = fields
        if ino == str(inode) and int(offset, 16) == 0:
            return int(span.split("-", 1)[0], 16)
    return None


def find_game(exe, pids):
    """Return ``(pid, base)`` of the process running ``exe``, or None."""
    try:
        inode = os.stat(os.path.realpath(exe)).st_ino
    except OSError:
        return None
    for pid in pids:
        base = _image_base(pid, inode)
        if base is not None:
            return pid, base
    return None


class _Memory:
    def __init__(self, pid):
        self._f = open(f"/proc/{pid}/mem", "r+b", buffering=0)

    def close(self):
        self._f.close()

    def read(self, address, size):
        self._f.seek(address)
        data = self._f.read(size)
        if len(data) != size:
            raise OSError("short read")
        return data

    def qword(self, address):
        return struct.unpack("<Q", self.read(address, 8))[0]

    def write(self, address, data):
        self._f.seek(address)
        self._f.write(data)


def _size_of_image(mem, base):
    """Read SizeOfImage from the mapped PE headers."""
    pe = struct.unpack("<I", mem.read(base + 0x3C, 4))[0]
    if mem.read(base + pe, 4) != b"PE\0\0":
        raise ValueError("not a PE image")
    return struct.unpack("<I", mem.read(base + pe + 0x50, 4))[0]


def match_build(mem, base):
    """Return the known build whose bytes this image carries, or None.

    Raises OSError while the image is still being loaded: Wine maps the
    headers first and reserves the sections zero-filled before reading the
    file into them, so a byte it cannot read, or a zero one, says nothing
    about the build yet.
    """
    try:
        size = _size_of_image(mem, base)
    except ValueError:
        return None
    for build in KNOWN_BUILDS:
        if max(build["getter"] + 7, build["global"] + 8,
               build["defaults"] + len(build["defaults_bytes"])) > size:
            continue
        lea = mem.read(base + build["getter"], 7)
        defaults = mem.read(base + build["defaults"],
                            len(build["defaults_bytes"]))
        if not any(lea) or not any(defaults):
            # Wine reserves the image zero-filled, then reads the file in.
            raise OSError("image not loaded yet")
        if lea[:3] != b"\x48\x8d\x05":
            continue
        target = build["getter"] + 7 + struct.unpack("<i", lea[3:])[0]
        if target == build["global"] and defaults == build["defaults_bytes"]:
            return build
    return None


def read_flights(mem, base, build):
    """Return ``{flight: (node address, status)}``, or None before MSAL starts."""
    state = mem.qword(base + build["global"])
    if not state:
        return None
    flights = mem.qword(state + FLIGHTS_OFFSET)
    if not flights:
        return None
    sentinel = mem.qword(flights + LIST_OFFSET)
    if not sentinel:
        return None
    found = {}
    node = mem.qword(sentinel)
    for _ in range(MAX_NODES):
        if node == sentinel:
            return found
        if not node:
            return None
        key, value = struct.unpack("<IB", mem.read(node + NODE_KEY, 5))
        found[key] = (node, value)
        node = mem.qword(node)
    return None


def turn_off(mem, base, build, flight=WAM_FOR_AAD):
    """Turn ``flight`` off. Return True once it is off, None while MSAL loads."""
    flights = read_flights(mem, base, build)
    if not flights or flight not in flights:
        return None
    node, status = flights[flight]
    if status == FLIGHT_OFF:
        return True
    mem.write(node + NODE_VALUE, bytes([FLIGHT_OFF]))
    return mem.read(node + NODE_VALUE, 1)[0] == FLIGHT_OFF


_FAILED = ("Minecraft Education's sign-in could not be kept out of Windows' "
           "account broker ({}). Signing in may stop on \"Something went "
           "wrong [4nsvw]\" after the e-mail address.")


def watch(exe, pids, alive, deadline_s=DEADLINE_S, poll_s=POLL_S,
          clock=time.monotonic, sleep=time.sleep):
    """Find the game, then turn MSAL's WAM flight off in it. Return the outcome.

    ``pids`` lists candidate processes and ``alive`` says whether the session
    still runs; both are called on every poll.
    """
    end = clock() + deadline_s
    game = mem = build = None
    try:
        while clock() < end and alive():
            if game is None:
                game = find_game(exe, pids())
                if game is not None:
                    try:
                        mem = _Memory(game[0])
                    except OSError as error:
                        warn(_FAILED.format(
                            f"its memory is not readable: {error.strerror}"))
                        return "unreachable"
            elif build is None:
                try:
                    build = match_build(mem, game[1])
                except OSError:
                    build = None  # still being mapped
                else:
                    if build is None:
                        warn(_FAILED.format(
                            "this build of Minecraft Education is not one "
                            "the launcher knows"))
                        return "unknown-build"
            else:
                try:
                    done = turn_off(mem, game[1], build)
                except OSError:
                    # The game exited, or MSAL is between two writes.
                    done = None
                if done:
                    info("Minecraft Education signs in through its own window "
                         "rather than Windows' account broker.")
                    return "off"
                if done is False:
                    warn(_FAILED.format("the change did not hold"))
                    return "failed"
            sleep(poll_s)
        if not alive():
            return "gone"
        if game is None:
            warn(_FAILED.format("the game process was not found"))
            return "not-found"
        warn(_FAILED.format("its sign-in settings never appeared"))
        return "timeout"
    finally:
        if mem is not None:
            mem.close()


def start(exe, pids, alive):
    """Run :func:`watch` beside the session; it never holds the game up."""
    thread = threading.Thread(target=_guarded, args=(exe, pids, alive),
                              name="msal-flights", daemon=True)
    thread.start()
    return thread


def _guarded(exe, pids, alive):
    try:
        watch(exe, pids, alive)
    except Exception as error:  # never take the session down with it
        warn(_FAILED.format(type(error).__name__))
