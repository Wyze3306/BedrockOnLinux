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
build it knows. A build it does not know is looked up in its executable,
which Education ships unencrypted: the code that reads the map sits next to
two strings MSAL logs from it (locate_build). A build where that fails, or a
process it cannot reach (the Flatpak sandbox isolates the game's processes),
is left alone with a warning.
"""
# SPDX-License-Identifier: MIT

import json
import mmap
import os
import re
import struct
import threading
import time
from pathlib import Path

from .config import CACHE
from .log import info, warn

WAM_FOR_AAD = 0x66
# FlightStatus: 0 is active, anything else is not.
FLIGHT_OFF = 1

# Per build, as offsets from the image base:
#   getter   lea rax, [rip + global]: the function MSAL reads its global from
#   global   the MSAL global; [global] -> state, [state + 0x100] -> flights
#   defaults the dwords MSAL seeds its default flights from, 0x66 among them
# and the expected bytes at each, which pin the layout down. A build found by
# locate_build has no defaults but says where the map is ("flights").
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

# What MSAL's IsFlightActive logs when it is called before MSAL starts: this
# message and its own name, loaded into r9 and r8 right after it has asked
# the getter for the global, and before it reads [global] + FLIGHTS_OFFSET.
NOT_STARTED_MESSAGE = (b"Flights cannot be checked before starting up MSAL "
                       b"nor after shutting down.\0")
FUNCTION_NAME = b"\0IsFlightActive\0"
# lea r9, [rip + message]; lea r8, [rip + name]
_LOG_ARGS = re.compile(rb"\x4c\x8d\x0d(.{4})\x4c\x8d\x05(.{4})", re.S)
# How far the call to the getter may sit from that pair, and how far into the
# getter its lea of the global may be (a thread-safe static guard comes first).
CALL_WINDOW = 0x40
GETTER_SIZE = 0x80
LOCATED_CACHE = CACHE / "msal-flights.json"


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


def match_build(mem, base, builds=KNOWN_BUILDS):
    """Return the build among ``builds`` whose bytes this image carries.

    Returns None when it carries none of them. Raises OSError while the
    image is still being loaded: Wine maps the headers first and reserves the
    sections zero-filled before reading the file into them, so a byte it
    cannot read, or a zero one, says nothing about the build yet. A build
    found by locate_build has no default flights to compare.
    """
    try:
        size = _size_of_image(mem, base)
    except ValueError:
        return None
    for build in builds:
        expected = build.get("defaults_bytes") or b""
        if max(build["getter"] + 7, build["global"] + 8,
               build.get("defaults", 0) + len(expected)) > size:
            continue
        lea = mem.read(base + build["getter"], 7)
        defaults = (mem.read(base + build["defaults"], len(expected))
                    if expected else b"\1")
        if not any(lea) or not any(defaults):
            # Wine reserves the image zero-filled, then reads the file in.
            raise OSError("image not loaded yet")
        if lea[:3] != b"\x48\x8d\x05":
            continue
        target = build["getter"] + 7 + struct.unpack("<i", lea[3:])[0]
        if target == build["global"] and (not expected
                                          or defaults == expected):
            return build
    return None


class _Image:
    """An executable's sections as they sit in its file."""

    def __init__(self, data):
        self.data = data
        if bytes(data[:2]) != b"MZ":
            raise ValueError("not a PE file")
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if bytes(data[pe:pe + 4]) != b"PE\0\0":
            raise ValueError("not a PE file")
        count, = struct.unpack_from("<H", data, pe + 6)
        optional, = struct.unpack_from("<H", data, pe + 20)
        self.sections = {}
        for i in range(count):
            at = pe + 24 + optional + 40 * i
            name = bytes(data[at:at + 8]).rstrip(b"\0").decode("ascii",
                                                                "replace")
            vsize, rva, raw_size, raw = struct.unpack_from("<IIII", data,
                                                            at + 8)
            self.sections[name] = (rva, vsize, raw_size, raw)

    def span(self, name):
        """Return ``(rva, file offset, size)`` of a section's bytes on disk."""
        rva, vsize, raw_size, raw = self.sections[name]
        return rva, raw, min(vsize, raw_size)

    def holds(self, name, rva):
        """Whether ``rva`` is in a section once loaded, zero-filled tail
        (where MSVC puts .bss) included."""
        start, vsize, _raw_size, _raw = self.sections[name]
        return start <= rva < start + vsize

    def offset(self, rva, size=1):
        """Return the file offset of ``size`` bytes at ``rva``, or None."""
        for start, vsize, raw_size, raw in self.sections.values():
            if start <= rva and rva + size <= start + min(vsize, raw_size):
                return raw + rva - start
        return None

    def read(self, rva, size):
        at = self.offset(rva, size)
        if at is None:
            raise ValueError(f"RVA {rva:#x} is not in the file")
        return bytes(self.data[at:at + size])

    def find_once(self, section, needle):
        """Return the RVA of ``needle``'s only occurrence in a section."""
        rva, raw, size = self.span(section)
        at = self.data.find(needle, raw, raw + size)
        if at < 0 or self.data.find(needle, at + 1, raw + size) >= 0:
            return None
        return rva + at - raw


def _rel32_target(image, rva, length):
    """Target of the rip-relative operand ending an instruction at ``rva``."""
    disp, = struct.unpack("<i", image.read(rva + length - 4, 4))
    return rva + length + disp


def _getter_lea(image, getter):
    """Return the RVA of the getter's ``lea rax, [rip + global]``, or None.

    It is the last instruction before the getter returns: ``ret``, maybe
    after ``add rsp, imm8``.
    """
    try:
        code = image.read(getter, GETTER_SIZE)
    except ValueError:
        return None
    for at in range(len(code) - 8):
        if code[at:at + 3] != b"\x48\x8d\x05":
            continue
        tail = code[at + 7:at + 12]
        if tail[:1] == b"\xc3" or (tail[:3] == b"\x48\x83\xc4"
                                   and tail[4:5] == b"\xc3"):
            return getter + at
    return None


def _flights_offset(image, after, getter):
    """Return the offset IsFlightActive reads the flight map at, or None.

    Past the log call, it asks the getter again: ``call getter; mov rcx,
    [rax]; mov reg, [rcx + offset]``.
    """
    code = image.read(after, 0x80)
    for at in range(len(code) - 16):
        if code[at] != 0xE8 or _rel32_target(image, after + at, 5) != getter:
            continue
        load = code[at + 5:at + 16]
        if load[:3] != b"\x48\x8b\x08" or load[3] not in (0x48, 0x4C) \
                or load[4] != 0x8B or load[5] & 0x07 != 0x01:
            return None
        if load[5] >> 6 == 2:
            return struct.unpack("<i", load[6:10])[0]
        if load[5] >> 6 == 1:
            return struct.unpack("<b", load[6:7])[0]
        return None
    return None


def _locate(image):
    message = image.find_once(".rdata", NOT_STARTED_MESSAGE)
    name = image.find_once(".rdata", FUNCTION_NAME)
    if message is None or name is None:
        return None
    name += 1  # past the NUL that makes the match a whole string
    text_rva, text_raw, text_size = image.span(".text")
    calls = []
    for match in _LOG_ARGS.finditer(image.data, text_raw,
                                    text_raw + text_size):
        at = text_rva + match.start() - text_raw
        if (_rel32_target(image, at, 7) == message
                and _rel32_target(image, at + 7, 7) == name):
            calls.append(at)
    if len(calls) != 1:
        return None
    log_call = calls[0]
    # The getter is the nearest call before the log arguments.
    before = image.read(log_call - CALL_WINDOW, CALL_WINDOW)
    getter = lea = None
    for at in range(CALL_WINDOW - 5, -1, -1):
        if before[at] != 0xE8:
            continue
        target = _rel32_target(image, log_call - CALL_WINDOW + at, 5)
        lea = _getter_lea(image, target)
        if lea is not None:
            getter = target
            break
    if getter is None:
        return None
    msal_global = _rel32_target(image, lea, 7)
    if not image.holds(".data", msal_global):
        return None
    offset = _flights_offset(image, log_call + 14, getter)
    if offset is None or not 0 < offset < 0x1000 or offset % 8:
        return None
    return {"name": "located", "getter": lea, "global": msal_global,
            "flights": offset}


def locate_build(exe):
    """Find a build's offsets in its executable on disk; None if not found.

    For a build missing from KNOWN_BUILDS. What it finds is checked against
    the running image (match_build) and the map MSAL builds (read_flights)
    before anything is written.
    """
    try:
        with open(exe, "rb") as handle, \
                mmap.mmap(handle.fileno(), 0,
                          access=mmap.ACCESS_READ) as data:
            return _locate(_Image(data))
    except (OSError, ValueError, KeyError, struct.error):
        return None


def located_build(exe, cache=None):
    """locate_build, remembered per executable size and modification time.

    A search reads a few hundred megabytes; a build keeps its answer, a
    failed search included.
    """
    cache = Path(cache or LOCATED_CACHE)
    try:
        st = os.stat(os.path.realpath(exe))
    except OSError:
        return None
    key = f"{st.st_size}:{st.st_mtime_ns}"
    try:
        known = json.loads(cache.read_text())
        if not isinstance(known, dict):
            known = {}
    except (OSError, ValueError):
        known = {}
    if key in known:
        return known[key] or None
    build = locate_build(os.path.realpath(exe))
    known[key] = build
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(known))
    except OSError:
        pass
    return build


def read_flights(mem, base, build):
    """Return ``{flight: (node address, status)}``, or None before MSAL starts."""
    state = mem.qword(base + build["global"])
    if not state:
        return None
    flights = mem.qword(state + build.get("flights", FLIGHTS_OFFSET))
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
    game = mem = build = located = None
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
                    if build is None:
                        # Read once the image is in: the search takes seconds.
                        if located is None:
                            located = located_build(exe) or False
                        if located:
                            build = match_build(mem, game[1], (located,))
                            if build is not None:
                                info("This Minecraft Education build is new "
                                     "to the launcher: its sign-in settings "
                                     "were found in the game's executable.")
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
