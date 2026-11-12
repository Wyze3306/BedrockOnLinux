"""bol.pe — PE parsing and byte-level binary patching."""
# SPDX-License-Identifier: MIT

import shutil
import struct
from pathlib import Path

from .log import die, info, ok, warn

class PE:
    def __init__(self, path: Path):
        self.path = Path(path)
        d = self.data = self.path.read_bytes()
        if d[:2] != b"MZ":
            raise ValueError("not a PE")
        e = struct.unpack_from("<I", d, 0x3C)[0]
        if d[e:e + 4] != b"PE\0\0":
            raise ValueError("bad PE signature")
        coff = e + 4
        nsec = struct.unpack_from("<H", d, coff + 2)[0]
        opt = coff + 20
        if struct.unpack_from("<H", d, opt)[0] != 0x20B:
            raise ValueError("not PE32+")
        self.exp_rva = struct.unpack_from("<I", d, opt + 112)[0]
        sect = opt + struct.unpack_from("<H", d, coff + 16)[0]
        self.secs = []
        for i in range(nsec):
            b = sect + i * 40
            vsz, va, rsz, raw = struct.unpack_from("<IIII", d, b + 8)
            self.secs.append((va, vsz, raw, rsz))

    def rva2off(self, rva):
        for va, vsz, raw, rsz in self.secs:
            if va <= rva < va + max(vsz, rsz):
                return raw + (rva - va)
        return None

    def off2rva(self, off):
        for va, _, raw, rsz in self.secs:
            if raw <= off < raw + rsz:
                return va + (off - raw)
        return None

    def export_rva(self, name):
        d = self.data
        eo = self.rva2off(self.exp_rva)
        if eo is None:
            return None
        nn = struct.unpack_from("<I", d, eo + 24)[0]
        af = self.rva2off(struct.unpack_from("<I", d, eo + 28)[0])
        an = self.rva2off(struct.unpack_from("<I", d, eo + 32)[0])
        ao = self.rva2off(struct.unpack_from("<I", d, eo + 36)[0])
        t = name.encode()
        for i in range(nn):
            no = self.rva2off(struct.unpack_from("<I", d, an + 4 * i)[0])
            if d[no:d.index(b"\0", no)] == t:
                od = struct.unpack_from("<H", d, ao + 2 * i)[0]
                return struct.unpack_from("<I", d, af + 4 * od)[0]
        return None

    def export_off(self, name):
        r = self.export_rva(name)
        return self.rva2off(r) if r is not None else None


_MACHINE_AMD64 = 0x8664
_MACHINE_ARM64 = 0xAA64
# IMAGE_LOAD_CONFIG_DIRECTORY64.CHPEMetadataPointer, and the data directory
# entry that locates that structure.
_CHPE_METADATA = 0xC8
_LOAD_CONFIG_DIR = 10


def code_machine(path: Path):
    """The processor a 64-bit PE's code is for: "x86_64", "arm64ec",
    "arm64" or "other", and None when the file is not a readable PE32+.

    The header alone does not tell x86-64 from ARM64EC: an ARM64EC image
    says AMD64 there too, and carries hybrid (CHPE) metadata in its load
    configuration instead. ARM64X images say ARM64."""
    try:
        pe = PE(path)
    except (OSError, ValueError, struct.error):
        return None
    d = pe.data
    try:
        coff = struct.unpack_from("<I", d, 0x3C)[0] + 4
        machine = struct.unpack_from("<H", d, coff)[0]
        if machine == _MACHINE_ARM64:
            return "arm64"
        if machine != _MACHINE_AMD64:
            return "other"
        opt = coff + 20
        if struct.unpack_from("<I", d, opt + 108)[0] <= _LOAD_CONFIG_DIR:
            return "x86_64"
        rva, size = struct.unpack_from(
            "<II", d, opt + 112 + 8 * _LOAD_CONFIG_DIR)
        off = pe.rva2off(rva) if rva and size else None
        if off is None:
            return "x86_64"
        if struct.unpack_from("<I", d, off)[0] < _CHPE_METADATA + 8:
            return "x86_64"
        chpe = struct.unpack_from("<Q", d, off + _CHPE_METADATA)[0]
    except struct.error:
        return None
    return "arm64ec" if chpe else "x86_64"


def _backup_once(path: Path):
    bk = path.with_suffix(path.suffix + ".bol-orig")
    if not bk.exists():
        shutil.copy2(path, bk)


def apply_patch(path: Path, off, expect, new, what, strict=True, relax=False):
    raw = bytearray(path.read_bytes())
    if bytes(raw[off:off + len(new)]) == new:
        info(f"{what}: already patched")
        return False
    if bytes(raw[off:off + len(expect)]) != expect:
        got = bytes(raw[off:off + len(expect)]).hex()
        if not relax:
            msg = f"{what}: unexpected bytes at 0x{off:x} — patch skipped."
            if strict:
                die(msg)
            warn(msg)
            return False
        # relax: the offset comes from the PE export table, so it IS the
        # function entry — only the prologue varies between Wine builds.
        # A return-0 stub is safe over any prologue, so patch anyway.
        warn(f"{what}: prologue {got} at 0x{off:x} differs — stubbing anyway.")
    _backup_once(path)
    raw[off:off + len(new)] = new
    path.write_bytes(raw)
    ok(f"{what}: patched")
    return True
