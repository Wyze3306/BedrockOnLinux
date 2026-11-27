"""bol.wim — files out of a Windows image, read where it lies.

wine-uwp runs a UWP app on Windows' own system DLLs, so it needs some 2,700
files out of a Windows 11 image: Microsoft's evaluation ISO, which is 7 GB
for the 1 GB they come to. Nothing here downloads the ISO. It reads, by HTTP
ranges, only what those files are made of: the ISO's UDF directory, to find
sources/install.wim; the WIM's lookup table and directory tree; then each
file's compressed resource, decompressed here (LZX) and checked against the
SHA-1 the image records for it. No 7-Zip, no FUSE, no root and no
third-party module, so it runs the same in the Flatpak as from a checkout.

The formats, as wimlib documents and reads them: a WIM is a header, a
lookup table of resources keyed by the SHA-1 of their contents, one
metadata resource per image (security descriptors, then the directory tree
as "dentries"), and the resources themselves, each cut into chunks of 32 KiB
that are compressed independently -- which is what lets them be
decompressed on every core at once.
"""
# SPDX-License-Identifier: MIT

import array
import concurrent.futures
import fnmatch
import hashlib
import multiprocessing
import os
import struct
import sys
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path

from .log import BolError

SECTOR = 2048
WIM_MAGIC = b"MSWIM\0\0\0"

_RES_METADATA = 0x02
_RES_COMPRESSED = 0x04
_RES_SPANNED = 0x08
_RES_SOLID = 0x10

_HDR_COMPRESSION = 0x00000002
_HDR_XPRESS = 0x00020000
_HDR_LZX = 0x00040000
_HDR_LZMS = 0x00080000
_HDR_XPRESS2 = 0x00200000

_ATTR_DIRECTORY = 0x10
_ATTR_REPARSE = 0x400


class WimError(BolError):
    """The Windows image could not be read."""


# ------------------------------------------------------------------ LZX

# Offset slots: a base and that many extra bits, for every window size.
_SLOT_BASE = []
_SLOT_EXTRA = []
_base = 0
for _slot in range(51):
    _extra = 0 if _slot < 4 else min((_slot >> 1) - 1, 17)
    _SLOT_BASE.append(_base)
    _SLOT_EXTRA.append(_extra)
    _base += 1 << _extra
del _base, _slot, _extra

_LZX_NUM_CHARS = 256
_LZX_LEN_SYMBOLS = 249
_LZX_PRECODE_SYMBOLS = 20
_LZX_ALIGNED_SYMBOLS = 8
_E8_FILE_SIZE = 12000000


def _num_main_symbols(window_order):
    """256 literals plus 8 length headers for every offset slot the window
    needs -- one fewer than its size suggests, as the format rules a match
    of the first two bytes against the last two out."""
    biggest = (1 << window_order) - 3 + 2
    slots = 30
    while biggest >= _SLOT_BASE[slots]:
        slots += 1
    return _LZX_NUM_CHARS + 8 * slots


def _decode_table(lens):
    """A one-level decode table for a canonical Huffman code.

    Each entry is ``symbol << 5 | length``, indexed by the next ``bits``
    bits of the stream; an entry no codeword reaches is -1. Returns
    ``(table, bits)``, or ``(None, 0)`` for a code with no symbol at all,
    which the format allows for a block that never uses it.
    """
    used = sorted((length, symbol) for symbol, length in enumerate(lens)
                  if length)
    if not used:
        return None, 0
    bits = used[-1][0]
    if len(used) == 1:
        # A single codeword: zlib's reading, which wimlib shares, decodes it
        # whatever the bit.
        length, symbol = used[0]
        return [symbol << 5 | length] * (1 << bits), bits
    table = [-1] * (1 << bits)
    code = 0
    last = 0
    for length, symbol in used:
        code <<= length - last
        last = length
        if code >= 1 << length:
            raise WimError("The Windows image holds an invalid Huffman code.")
        span = 1 << (bits - length)
        start = code << (bits - length)
        table[start:start + span] = [symbol << 5 | length] * span
        code += 1
    return table, bits


class _Bits:
    """LZX's bitstream: 16-bit little-endian words, read high bit first.

    Reads past the end produce zeros, as wimlib's do, up to a margin;
    beyond that the data is corrupt and the IndexError says so.
    """

    __slots__ = ("data", "words", "wi", "buf", "nb")

    def __init__(self, data):
        self.data = bytes(data)
        words = array.array("H")
        words.frombytes(self.data[:len(self.data) & ~1])
        if sys.byteorder == "big":
            words.byteswap()
        if len(self.data) & 1:
            words.append(self.data[-1])
        words.extend([0] * 8)
        self.words = words
        self.wi = 0
        self.buf = 0
        self.nb = 0

    def need(self, count):
        while self.nb < count:
            self.buf = (((self.buf & ((1 << self.nb) - 1)) << 16)
                        | self.words[self.wi])
            self.wi += 1
            self.nb += 16

    def read(self, count):
        if not count:
            return 0
        self.need(count)
        self.nb -= count
        return (self.buf >> self.nb) & ((1 << count) - 1)

    def symbol(self, table, bits):
        if table is None:
            raise WimError("The Windows image uses a Huffman code it never "
                           "defined.")
        self.need(bits)
        entry = table[(self.buf >> (self.nb - bits)) & ((1 << bits) - 1)]
        if entry < 0:
            raise WimError("The Windows image holds corrupt LZX data.")
        self.nb -= entry & 31
        return entry >> 5

    def align(self):
        self.buf = 0
        self.nb = 0

    def bytes_at(self, count):
        """``count`` raw bytes from the current 16-bit boundary on."""
        start = self.wi * 2
        chunk = self.data[start:start + count]
        if len(chunk) != count:
            raise WimError("The Windows image holds truncated LZX data.")
        return start, chunk


def _read_lens(bits, lens, first, count):
    """Delta-coded codeword lengths, through a precode of their own."""
    pre = [bits.read(4) for _ in range(_LZX_PRECODE_SYMBOLS)]
    table, tbits = _decode_table(pre)
    index = first
    end = first + count
    while index < end:
        presym = bits.symbol(table, tbits)
        if presym < 17:
            lens[index] = (lens[index] - presym) % 17
            index += 1
            continue
        if presym == 17:
            run = 4 + bits.read(4)
            value = 0
        elif presym == 18:
            run = 20 + bits.read(5)
            value = 0
        else:
            run = 4 + bits.read(1)
            presym = bits.symbol(table, tbits)
            if presym > 17:
                raise WimError("The Windows image holds corrupt LZX "
                               "lengths.")
            value = (lens[index] - presym) % 17
        # An overrun is invalid data; it is cut here instead of spilling
        # into the next code's lengths.
        run = min(run, end - index)
        lens[index:index + run] = [value] * run
        index += run


def _decode_block(bits, out, block_end, main, length_code, aligned_code,
                  recent):
    """One verbatim or aligned block, appended to ``out``. The hot loop:
    everything it touches is a local."""
    main_t, main_b = main
    len_t, len_b = length_code
    al_t, al_b = aligned_code
    main_m = (1 << main_b) - 1
    len_m = (1 << len_b) - 1 if len_t is not None else 0
    al_m = (1 << al_b) - 1 if al_t is not None else 0
    words = bits.words
    wi = bits.wi
    buf = bits.buf
    nb = bits.nb
    r0, r1, r2 = recent
    base = _SLOT_BASE
    extra_bits = _SLOT_EXTRA
    min_aligned = 8 if al_t is not None else 1 << 30
    olen = len(out)
    append = out.append
    while olen < block_end:
        if nb < 16:
            buf = ((buf & ((1 << nb) - 1)) << 16) | words[wi]
            wi += 1
            nb += 16
        entry = main_t[(buf >> (nb - main_b)) & main_m]
        nb -= entry & 31
        sym = entry >> 5
        if sym < 256:
            # -1, a codeword no symbol has, fails here as a byte.
            append(sym)
            olen += 1
            continue
        sym -= 256
        length = sym & 7
        slot = sym >> 3
        if length == 7:
            if nb < 16:
                buf = ((buf & ((1 << nb) - 1)) << 16) | words[wi]
                wi += 1
                nb += 16
            entry = len_t[(buf >> (nb - len_b)) & len_m]
            if entry < 0:
                raise WimError("The Windows image holds corrupt LZX data.")
            nb -= entry & 31
            length += entry >> 5
        length += 2
        if slot < 3:
            if slot == 0:
                offset = r0
            elif slot == 1:
                offset = r1
                r1 = r0
                r0 = offset
            else:
                offset = r2
                r2 = r0
                r0 = offset
        else:
            extra = extra_bits[slot]
            offset = base[slot]
            if slot >= min_aligned:
                verbatim = extra - 3
                if verbatim:
                    while nb < verbatim:
                        buf = ((buf & ((1 << nb) - 1)) << 16) | words[wi]
                        wi += 1
                        nb += 16
                    nb -= verbatim
                    offset += ((buf >> nb) & ((1 << verbatim) - 1)) << 3
                if nb < al_b:
                    buf = ((buf & ((1 << nb) - 1)) << 16) | words[wi]
                    wi += 1
                    nb += 16
                entry = al_t[(buf >> (nb - al_b)) & al_m]
                if entry < 0:
                    raise WimError("The Windows image holds corrupt LZX "
                                   "data.")
                nb -= entry & 31
                offset += entry >> 5
            elif extra:
                while nb < extra:
                    buf = ((buf & ((1 << nb) - 1)) << 16) | words[wi]
                    wi += 1
                    nb += 16
                nb -= extra
                offset += (buf >> nb) & ((1 << extra) - 1)
            offset -= 2
            r2 = r1
            r1 = r0
            r0 = offset
        start = olen - offset
        if start < 0 or olen + length > block_end:
            raise WimError("The Windows image holds an LZX match out of "
                           "range.")
        if offset >= length:
            out += out[start:start + length]
        else:
            seg = out[start:]
            out += (seg * (length // offset + 1))[:length]
        olen += length
    bits.wi = wi
    bits.buf = buf
    bits.nb = nb
    recent[:] = [r0, r1, r2]


def _undo_e8(out):
    """Turn the absolute call targets LZX's preprocessing wrote back into
    the x86 CALL displacements they were."""
    size = len(out)
    if size <= 10:
        return
    tail = size - 10
    i = out.find(0xE8, 0, tail)
    while i >= 0:
        target = int.from_bytes(out[i + 1:i + 5], "little", signed=True)
        if 0 <= target < _E8_FILE_SIZE:
            out[i + 1:i + 5] = (target - i).to_bytes(4, "little",
                                                     signed=True)
        elif target < 0 and target >= -i:
            out[i + 1:i + 5] = (target + _E8_FILE_SIZE).to_bytes(
                4, "little", signed=True)
        i = out.find(0xE8, i + 5, tail)


def lzx_decompress(data, size, window_order=15):
    """One WIM chunk: LZX with a window of the chunk's size, its lengths
    starting from zero and the E8 translation always on."""
    try:
        return _lzx(data, size, window_order)
    except (IndexError, ValueError, TypeError) as exc:
        raise WimError(f"The Windows image holds corrupt LZX data ({exc}).") \
            from None


def _lzx(data, size, window_order):
    bits = _Bits(data)
    num_main = _num_main_symbols(window_order)
    main_lens = [0] * num_main
    len_lens = [0] * _LZX_LEN_SYMBOLS
    recent = [1, 1, 1]
    out = bytearray()
    while len(out) < size:
        block_type = bits.read(3)
        if bits.read(1):
            block_size = 32768
        else:
            block_size = bits.read(16)
            if window_order >= 16:
                block_size = (block_size << 8) | bits.read(8)
        if block_size < 1 or block_size > size - len(out):
            raise WimError("The Windows image holds an LZX block of the "
                           "wrong size.")
        if block_type in (1, 2):
            aligned = (None, 0)
            if block_type == 2:
                aligned = _decode_table(
                    [bits.read(3) for _ in range(_LZX_ALIGNED_SYMBOLS)])
            _read_lens(bits, main_lens, 0, _LZX_NUM_CHARS)
            _read_lens(bits, main_lens, _LZX_NUM_CHARS,
                       num_main - _LZX_NUM_CHARS)
            _read_lens(bits, len_lens, 0, _LZX_LEN_SYMBOLS)
            main = _decode_table(main_lens)
            if main[0] is None:
                raise WimError("The Windows image holds an LZX block with "
                               "no codes.")
            _decode_block(bits, out, len(out) + block_size, main,
                          _decode_table(len_lens), aligned, recent)
        elif block_type == 3:
            # The recent offsets start on the next 16-bit boundary -- 16 bits
            # on when the stream already is on one, a quirk of the format.
            bits.need(1)
            bits.align()
            start, head = bits.bytes_at(12)
            recent[:] = struct.unpack("<III", head)
            if 0 in recent:
                raise WimError("The Windows image holds corrupt LZX data.")
            bits.wi += 6
            start, chunk = bits.bytes_at(block_size)
            out += chunk
            bits.wi = (start + block_size + (block_size & 1)) // 2
        else:
            raise WimError(f"The Windows image holds an LZX block of an "
                           f"unknown type ({block_type}).")
    _undo_e8(out)
    return bytes(out)


def inflate_chunks(jobs):
    """Decompress ``(data, size, window_order)`` chunks, for a worker
    process: a chunk stored whole (its compressed size is its size) comes
    back as it is."""
    out = []
    for data, size, order in jobs:
        if len(data) == size:
            out.append(bytes(data))
        else:
            out.append(lzx_decompress(data, size, order))
    return out


# ------------------------------------------------------------- sources


class HttpFile:
    """Byte ranges of one remote file. Each read is a Range request of its
    own, through urllib, so a proxy the system sets is honoured; small
    reads -- the UDF directory, a dentry -- come from a block cache."""

    BLOCK = 64 << 10

    def __init__(self, url, size, timeout=60, attempts=6, user_agent=None):
        self.url = url
        self.size = int(size)
        self.timeout = timeout
        self.attempts = attempts
        self.user_agent = user_agent or "bedrock-on-linux"
        self._blocks = {}
        self._lock = threading.Lock()
        self.fetched = 0

    @classmethod
    def open(cls, url, timeout=60, user_agent=None):
        """Follow the redirects to the file itself and learn its size."""
        request = urllib.request.Request(url, method="HEAD", headers={
            "User-Agent": user_agent or "bedrock-on-linux"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final = response.geturl()
            size = int(response.headers.get("Content-Length") or 0)
        if not size:
            raise WimError("The download server did not say how big the "
                           "Windows image is.")
        return cls(final, size, timeout=timeout, user_agent=user_agent)

    def read(self, offset, length):
        if length <= 0:
            return b""
        if offset < 0 or offset + length > self.size:
            raise WimError("A read past the end of the Windows image.")
        if length <= self.BLOCK:
            first, last = offset // self.BLOCK, (offset + length - 1) // \
                self.BLOCK
            data = b"".join(self._block(i) for i in range(first, last + 1))
            start = offset - first * self.BLOCK
            return data[start:start + length]
        return self.fetch(offset, length)

    def _block(self, index):
        with self._lock:
            cached = self._blocks.get(index)
        if cached is not None:
            return cached
        start = index * self.BLOCK
        data = self.fetch(start, min(self.BLOCK, self.size - start))
        with self._lock:
            if len(self._blocks) > 256:
                self._blocks.clear()
            self._blocks[index] = data
        return data

    def fetch(self, offset, length):
        """``length`` bytes from ``offset``, retried on a dropped
        connection."""
        last = None
        for attempt in range(self.attempts):
            request = urllib.request.Request(self.url, headers={
                "Range": f"bytes={offset}-{offset + length - 1}",
                "User-Agent": self.user_agent})
            try:
                with urllib.request.urlopen(request,
                                            timeout=self.timeout) as resp:
                    if getattr(resp, "status", 206) != 206:
                        raise WimError("The download server does not serve "
                                       "parts of a file.")
                    data = resp.read()
                if len(data) == length:
                    with self._lock:
                        self.fetched += length
                    return data
                last = f"{len(data)} of {length} bytes"
            except WimError:
                raise
            except OSError as exc:
                last = exc
            time.sleep(min(2 ** attempt, 15))
        raise WimError(f"The Windows image could not be read from the "
                       f"download server ({last}).")


class LocalFile:
    """The same interface over a file on disk (tests, an ISO already
    downloaded)."""

    def __init__(self, path):
        self.path = str(path)
        self.size = os.path.getsize(self.path)
        self.fetched = 0

    def read(self, offset, length):
        with open(self.path, "rb") as handle:
            handle.seek(offset)
            data = handle.read(length)
        if len(data) != length:
            raise WimError("A read past the end of the Windows image.")
        self.fetched += length
        return data

    fetch = read


class Extents:
    """A file inside an image as one address space: the extents UDF keeps
    it in, joined. ``size`` and ``read``/``fetch`` like the sources."""

    def __init__(self, source, extents):
        self.source = source
        self.extents = list(extents)
        self.size = sum(length for _, length in self.extents)

    @property
    def fetched(self):
        return self.source.fetched

    def _map(self, offset, length, reader):
        if offset < 0 or offset + length > self.size:
            raise WimError("A read past the end of install.wim.")
        parts = []
        position = 0
        for start, extent in self.extents:
            if length <= 0:
                break
            if offset < position + extent:
                inside = offset - position
                take = min(length, extent - inside)
                parts.append(reader(start + inside, take))
                offset += take
                length -= take
            position += extent
        return b"".join(parts)

    def read(self, offset, length):
        return self._map(offset, length, self.source.read)

    def fetch(self, offset, length):
        return self._map(offset, length, self.source.fetch)


# ------------------------------------------------------------------- UDF


def _u16(data, at):
    return struct.unpack_from("<H", data, at)[0]


def _u32(data, at):
    return struct.unpack_from("<I", data, at)[0]


def _u64(data, at):
    return struct.unpack_from("<Q", data, at)[0]


def _dstring(raw):
    if not raw:
        return ""
    kind, body = raw[0], raw[1:]
    if kind == 8:
        return body.decode("latin-1")
    if kind == 16:
        return body.decode("utf-16-be", errors="replace")
    raise WimError("The ISO names a file in an unknown UDF encoding.")


def udf_extents(source, path):
    """The ``(offset, length)`` byte extents of ``path`` in a UDF image.

    Enough of UDF for Microsoft's ISOs, which oscdimg writes as UDF with a
    physical partition and a stub ISO 9660 that only carries a README:
    anchor, volume descriptors, file set, then the directories down to the
    file, matched without regard to case.
    """
    anchor = source.read(256 * SECTOR, SECTOR)
    if _u16(anchor, 0) != 2:
        raise WimError("The download is not a UDF image (no anchor).")
    vds_length, vds_location = struct.unpack_from("<II", anchor, 16)
    vds = source.read(vds_location * SECTOR, min(vds_length, 64 * SECTOR))
    partition = block = fsd = None
    for at in range(0, len(vds), SECTOR):
        tag = _u16(vds, at)
        if tag == 5:
            partition = _u32(vds, at + 188)
        elif tag == 6:
            block = _u32(vds, at + 212)
            fsd = _u32(vds, at + 252)
            maps = _u32(vds, at + 268)
            if maps and vds[at + 440] != 1:
                raise WimError("The ISO uses a UDF partition map this "
                               "reader does not know.")
        elif tag == 8:
            break
    if partition is None or block is None or fsd is None:
        raise WimError("The ISO's UDF volume descriptors are incomplete.")

    def at_block(lbn):
        return (partition + lbn) * block

    def read_entry(lbn):
        data = source.read(at_block(lbn), block)
        tag = _u16(data, 0)
        if tag == 261:
            ea, ad, start = _u32(data, 168), _u32(data, 172), 176
        elif tag == 266:
            ea, ad, start = _u32(data, 208), _u32(data, 212), 216
        else:
            raise WimError("The ISO's UDF file entry is unreadable.")
        kind = _u16(data, 34) & 7
        size = _u64(data, 56)
        file_type = data[27]
        ads = data[start + ea:start + ea + ad]
        if kind == 3:
            return file_type, size, None, ads[:size]
        extents = []
        step = 8 if kind == 0 else 16
        if kind not in (0, 1):
            raise WimError("The ISO uses UDF allocation descriptors this "
                           "reader does not know.")
        queue = [ads]
        while queue:
            table = queue.pop(0)
            for at in range(0, len(table) - step + 1, step):
                raw_length, position = struct.unpack_from("<II", table, at)
                length = raw_length & 0x3FFFFFFF
                kind_bits = raw_length >> 30
                if length == 0:
                    break
                if kind_bits == 3:
                    more = source.read(at_block(position), block)
                    queue.append(more[24:24 + _u32(more, 20)])
                    break
                if kind_bits == 0:
                    extents.append((at_block(position), length))
        remaining = size
        out = []
        for offset, length in extents:
            take = min(length, remaining)
            if take <= 0:
                break
            out.append((offset, take))
            remaining -= take
        return file_type, size, out, None

    def children(lbn):
        file_type, size, extents, inline = read_entry(lbn)
        if file_type != 4:
            raise WimError("A path in the ISO runs through a file.")
        if inline is None:
            inline = b"".join(source.read(o, n) for o, n in extents)
        at = 0
        while at + 38 <= len(inline):
            if _u16(inline, at) != 257:
                break
            characteristics = inline[at + 18]
            name_length = inline[at + 19]
            icb = _u32(inline, at + 24)
            iu_length = _u16(inline, at + 36)
            name_start = at + 38 + iu_length
            name = _dstring(inline[name_start:name_start + name_length])
            if not characteristics & 0x0C:
                yield name, icb
            at += (38 + iu_length + name_length + 3) & ~3

    fsd_block = source.read(at_block(fsd), block)
    if _u16(fsd_block, 0) != 256:
        raise WimError("The ISO's UDF file set is unreadable.")
    lbn = _u32(fsd_block, 404)
    for part in [p for p in path.replace("\\", "/").split("/") if p]:
        found = next((icb for name, icb in children(lbn)
                      if name.lower() == part.lower()), None)
        if found is None:
            raise WimError(f"{path} is not in the ISO.")
        lbn = found
    file_type, size, extents, inline = read_entry(lbn)
    if extents is None:
        raise WimError(f"{path} in the ISO is too small to be an image.")
    return extents


# ------------------------------------------------------------------- WIM


class Resource:
    __slots__ = ("offset", "stored", "size", "flags")

    def __init__(self, offset, stored, size, flags):
        self.offset = offset
        self.stored = stored
        self.size = size
        self.flags = flags


def _reshdr(data, at):
    head, offset, size = struct.unpack_from("<QQQ", data, at)
    return Resource(offset, head & 0x00FFFFFFFFFFFFFF, size, head >> 56)


class Entry:
    __slots__ = ("path", "sha1", "size")

    def __init__(self, path, sha1, size):
        self.path = path
        self.sha1 = sha1
        self.size = size


class Wim:
    """One WIM, read through a source with ``read(offset, length)``."""

    def __init__(self, source, pool=None):
        self.source = source
        self.pool = pool
        header = source.read(0, 208)
        if header[:8] != WIM_MAGIC:
            raise WimError("install.wim is not a Windows image (WIM).")
        self.flags = _u32(header, 16)
        self.chunk_size = _u32(header, 20)
        self.parts = _u16(header, 42)
        self.image_count = _u32(header, 44)
        if self.parts != 1:
            raise WimError("install.wim is split in parts, which this "
                           "reader does not join.")
        if self.flags & _HDR_COMPRESSION and not self.flags & _HDR_LZX:
            kind = ("LZMS (an install.esd)" if self.flags & _HDR_LZMS else
                    "XPRESS" if self.flags & (_HDR_XPRESS | _HDR_XPRESS2)
                    else "an unknown method")
            raise WimError(f"install.wim is compressed with {kind}; only "
                           "LZX images can be read.")
        order = self.chunk_size.bit_length() - 1
        if self.flags & _HDR_COMPRESSION and (
                1 << order != self.chunk_size or not 15 <= order <= 21):
            raise WimError("install.wim has a chunk size LZX cannot have.")
        self.window_order = order
        self.table = _reshdr(header, 48)
        self.xml = _reshdr(header, 72)
        self.blobs = {}
        self.metadata = []
        raw = self.read_resource(self.table)
        for at in range(0, len(raw) - 49, 50):
            res = _reshdr(raw, at)
            sha1 = raw[at + 30:at + 50]
            if res.flags & _RES_METADATA:
                self.metadata.append((res, sha1))
            else:
                self.blobs[sha1] = res

    # -------------------------------------------------------- resources

    def _chunks(self, res, raw):
        """The chunks of one resource as ``(data, size, order)`` jobs."""
        if not res.flags & _RES_COMPRESSED:
            return [(raw, len(raw), self.window_order)]
        if res.flags & (_RES_SOLID | _RES_SPANNED):
            raise WimError("install.wim keeps files in solid or spanned "
                           "resources, which this reader does not know.")
        chunk = self.chunk_size
        count = (res.size + chunk - 1) // chunk
        entry = 8 if res.size > 0xFFFFFFFF else 4
        table_size = (count - 1) * entry
        if count == 0:
            return []
        fmt = "<%d%s" % (count - 1, "Q" if entry == 8 else "I")
        starts = (0,) + struct.unpack_from(fmt, raw, 0) if count > 1 else (0,)
        body = memoryview(raw)[table_size:]
        jobs = []
        for i, start in enumerate(starts):
            end = starts[i + 1] if i + 1 < count else len(body)
            size = chunk if i + 1 < count else res.size - chunk * (count - 1)
            if not 0 <= start <= end <= len(body):
                raise WimError("install.wim has a corrupt chunk table.")
            jobs.append((bytes(body[start:end]), size, self.window_order))
        return jobs

    def _inflate(self, jobs):
        """Decompress jobs, in the pool when there is one."""
        if self.pool is None or len(jobs) < 2:
            return inflate_chunks(jobs)
        batch = max(1, len(jobs) // (4 * (self.pool._max_workers or 1)))
        futures = [self.pool.submit(inflate_chunks, jobs[i:i + batch])
                   for i in range(0, len(jobs), batch)]
        out = []
        for future in futures:
            out.extend(future.result())
        return out

    def read_resource(self, res):
        raw = self.source.fetch(res.offset, res.stored) if res.stored else b""
        data = b"".join(self._inflate(self._chunks(res, raw)))
        if len(data) != res.size:
            raise WimError("install.wim holds a resource of the wrong size.")
        return data

    # ------------------------------------------------------------ trees

    def files(self, image=1):
        """Every regular file of an image: ``{path: Entry}``, paths with
        forward slashes and the image's own casing."""
        if not 1 <= image <= len(self.metadata):
            raise WimError(f"install.wim has no image {image}.")
        res, sha1 = self.metadata[image - 1]
        meta = self.read_resource(res)
        if hashlib.sha1(meta).digest() != sha1:
            raise WimError("install.wim's directory tree is corrupt.")
        root = (_u32(meta, 0) + 7) & ~7
        out = {}
        self._walk(meta, root, "", out, set(), top=True)
        return out

    def _walk(self, meta, offset, prefix, out, seen, top=False):
        if offset in seen:
            raise WimError("install.wim's directory tree loops.")
        seen.add(offset)
        while True:
            if offset + 8 > len(meta):
                raise WimError("install.wim's directory tree is truncated.")
            length = _u64(meta, offset)
            if length <= 8:
                return
            if length < 102 or offset + length > len(meta):
                raise WimError("install.wim holds a corrupt directory entry.")
            attributes = _u32(meta, offset + 8)
            subdir = _u64(meta, offset + 16)
            default_hash = meta[offset + 64:offset + 84]
            streams = _u16(meta, offset + 96)
            name_bytes = _u16(meta, offset + 100)
            name = meta[offset + 102:offset + 102 + name_bytes].decode(
                "utf-16-le", errors="replace")
            nxt = offset + ((length + 7) & ~7)
            data_hash = default_hash
            size = None
            for _ in range(streams):
                stream_length = _u64(meta, nxt)
                stream_name = _u16(meta, nxt + 36)
                if stream_name == 0:
                    data_hash = meta[nxt + 16:nxt + 36]
                nxt += (stream_length + 7) & ~7
            if top:
                # The root has no name; its children are the image's top.
                if subdir:
                    self._walk(meta, subdir, "", out, seen)
                return
            path = f"{prefix}{name}"
            if attributes & _ATTR_DIRECTORY:
                if subdir and not attributes & _ATTR_REPARSE:
                    self._walk(meta, subdir, path + "/", out, seen)
            elif not attributes & _ATTR_REPARSE:
                if data_hash == bytes(20):
                    size = 0
                else:
                    blob = self.blobs.get(data_hash)
                    size = blob.size if blob else None
                if size is not None:
                    out[path] = Entry(path, data_hash, size)
            offset = nxt

    # ------------------------------------------------------- extraction

    def extract(self, targets, progress=None, fetch_threads=4,
                request_bytes=16 << 20, gap=256 << 10):
        """Write files: ``targets`` maps a content SHA-1 to the paths it
        goes to. Each is checked against its SHA-1 before it is renamed
        into place, so what is on disk is either right or absent.

        Resources are read in the order they lie in the image, neighbours
        in one request, a few requests at a time; ``progress(done, total)``
        counts compressed bytes and may raise to stop.
        """
        blobs = []
        for sha1, paths in targets.items():
            if sha1 == bytes(20):
                for path in paths:
                    _write_file(path, b"")
                continue
            res = self.blobs.get(sha1)
            if res is None:
                raise WimError("install.wim lists a file whose contents it "
                               "does not have.")
            blobs.append((res.offset, res, sha1, paths))
        blobs.sort(key=lambda item: item[0])
        requests = []
        for item in blobs:
            offset, res = item[0], item[1]
            if (requests and offset - requests[-1][1] <= gap
                    and offset + res.stored - requests[-1][0]
                    <= request_bytes):
                requests[-1][1] = max(requests[-1][1], offset + res.stored)
                requests[-1][2].append(item)
            else:
                requests.append([offset, offset + res.stored, [item]])
        total = sum(end - start for start, end, _ in requests)
        done = 0
        if progress:
            progress(0, total)

        def fetch(request):
            start, end, _ = request
            return self.source.fetch(start, end - start) if end > start \
                else b""

        ahead = max(1, fetch_threads * 2)
        with concurrent.futures.ThreadPoolExecutor(fetch_threads) as threads:
            queue = deque()
            pending = iter(requests)
            for request in pending:
                queue.append((request, threads.submit(fetch, request)))
                if len(queue) >= ahead:
                    break
            try:
                while queue:
                    request, future = queue.popleft()
                    raw = future.result()
                    nxt = next(pending, None)
                    if nxt is not None:
                        queue.append((nxt, threads.submit(fetch, nxt)))
                    start = request[0]
                    jobs, spans = [], []
                    for offset, res, sha1, paths in request[2]:
                        piece = raw[offset - start:offset - start + res.stored]
                        chunks = self._chunks(res, piece)
                        spans.append((len(jobs), len(chunks), res, sha1,
                                      paths))
                        jobs.extend(chunks)
                    results = self._inflate(jobs)
                    for first, count, res, sha1, paths in spans:
                        data = b"".join(results[first:first + count])
                        if (len(data) != res.size
                                or hashlib.sha1(data).digest() != sha1):
                            raise WimError("A file read out of install.wim "
                                           "does not match its checksum.")
                        for path in paths:
                            _write_file(path, data)
                    done += request[1] - request[0]
                    if progress:
                        progress(done, total)
            except BaseException:
                for _, future in queue:
                    future.cancel()
                raise
        return total


def _write_file(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name("." + path.name + ".part")
    with open(staged, "wb") as handle:
        handle.write(data)
    os.replace(staged, path)


def decompression_pool(workers=None):
    """Worker processes for the chunks, started fresh ("spawn"): the
    launcher may be a Qt process with threads, which a fork would copy
    half-way through whatever they were doing."""
    count = workers if workers is not None else max(
        1, min(8, (os.cpu_count() or 2) - 1))
    if count <= 1:
        return None
    return concurrent.futures.ProcessPoolExecutor(
        count, mp_context=multiprocessing.get_context("spawn"))


# ------------------------------------------------------------- manifests


def manifest_lines(text):
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def match_manifest(files, lines):
    """What wine-uwp's harvest.py takes out of an image: a line is a path
    matched without regard to case, a pattern when it holds '*' (fnmatch,
    '*' crossing '/'), or a whole directory when it ends in '/'. An exact
    line keeps the manifest's casing, a pattern the image's.

    Returns ``([(destination, Entry)], [missing lines])``.
    """
    by_lower = {}
    for path, entry in files.items():
        by_lower.setdefault(path.lower(), entry)
    found, missing = [], []
    for line in lines:
        if "*" in line or line.endswith("/"):
            pattern = line.lower() + ("*" if line.endswith("/") else "")
            hits = [entry for lower, entry in by_lower.items()
                    if fnmatch.fnmatchcase(lower, pattern)]
            found.extend((entry.path, entry) for entry in hits)
            if not hits:
                missing.append(line)
            continue
        entry = by_lower.get(line.lower())
        if entry is None:
            missing.append(line)
        else:
            found.append((line, entry))
    return found, missing


def _file_matches(path, entry):
    try:
        if path.stat().st_size != entry.size:
            return False
        if entry.size == 0:
            # A WIM gives an empty file a hash of zeros, not SHA-1("").
            return True
        digest = hashlib.sha1()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        return digest.digest() == entry.sha1
    except OSError:
        return False


def harvest(wim, lines, outdir, progress=None, image=1, fetch_threads=4):
    """Take the manifest's files out of ``wim`` into ``outdir``.

    A file already there with the right checksum is kept, so a harvest cut
    short goes on where it stopped. ``harvest-missing.txt`` lists the lines
    the image had nothing for, as harvest.py writes it.
    """
    outdir = Path(outdir)
    files = wim.files(image)
    found, missing = match_manifest(files, lines)
    targets = {}
    count = 0
    for destination, entry in found:
        path = outdir / destination
        count += 1
        if _file_matches(path, entry):
            continue
        targets.setdefault(entry.sha1, []).append(path)
    wim.extract(targets, progress=progress, fetch_threads=fetch_threads)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "harvest-missing.txt").write_text(
        "".join(line + "\n" for line in missing), encoding="utf-8")
    return count, missing


def open_iso_wim(source, pool=None):
    """The Wim inside a Windows ISO: sources/install.wim, through UDF."""
    extents = udf_extents(source, "sources/install.wim")
    return Wim(Extents(source, extents), pool=pool)


# Exposed for tests and for a reader that has the WIM bytes already.
def open_bytes(data, pool=None):
    class _Mem:
        size = len(data)
        fetched = 0

        def read(self, offset, length):
            chunk = data[offset:offset + length]
            if len(chunk) != length:
                raise WimError("A read past the end of install.wim.")
            return chunk

        fetch = read
    return Wim(_Mem(), pool=pool)


__all__ = ["WimError", "Wim", "HttpFile", "LocalFile", "Extents",
           "udf_extents", "open_iso_wim", "open_bytes", "lzx_decompress",
           "harvest", "match_manifest", "manifest_lines",
           "decompression_pool", "inflate_chunks"]
