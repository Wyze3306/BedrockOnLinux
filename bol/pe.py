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

    def _directory(self, index):
        e = struct.unpack_from("<I", self.data, 0x3C)[0]
        opt = e + 24
        rva, size = struct.unpack_from("<II", self.data, opt + 112 + 8 * index)
        return rva, size

    def _cstr(self, off):
        end = self.data.index(b"\0", off)
        return self.data[off:end].decode("latin-1")

    def export_names(self):
        """Every name this image exports."""
        d = self.data
        eo = self.rva2off(self.exp_rva) if self.exp_rva else None
        if eo is None:
            return set()
        nn = struct.unpack_from("<I", d, eo + 24)[0]
        an = self.rva2off(struct.unpack_from("<I", d, eo + 32)[0])
        names = set()
        for i in range(nn):
            no = self.rva2off(struct.unpack_from("<I", d, an + 4 * i)[0])
            if no is not None:
                names.add(self._cstr(no))
        return names

    def imports(self):
        """{lower-case DLL name: set of imported names} -- ordinal imports
        as "#<n>" -- from both the import and the delay-import tables."""
        d = self.data
        out = {}
        for index, entry_size, name_at, thunk_at in ((1, 20, 12, 0),
                                                       (13, 32, 4, 16)):
            rva, _size = self._directory(index)
            base = self.rva2off(rva) if rva else None
            if base is None:
                continue
            while base + entry_size <= len(d):
                name_rva = struct.unpack_from("<I", d, base + name_at)[0]
                thunk_rva = struct.unpack_from("<I", d, base + thunk_at)[0]
                if index == 1 and not thunk_rva:
                    thunk_rva = struct.unpack_from("<I", d, base + 16)[0]
                if not name_rva:
                    break
                name_off = self.rva2off(name_rva)
                thunk = self.rva2off(thunk_rva) if thunk_rva else None
                if name_off is None:
                    break
                names = out.setdefault(self._cstr(name_off).lower(), set())
                while thunk is not None and thunk + 8 <= len(d):
                    value = struct.unpack_from("<Q", d, thunk)[0]
                    if not value:
                        break
                    if value & (1 << 63):
                        names.add("#%d" % (value & 0xFFFF))
                    else:
                        hint = self.rva2off(value & 0x7FFFFFFF)
                        if hint is not None:
                            names.add(self._cstr(hint + 2))
                    thunk += 8
                base += entry_size
        return out

    def export_off(self, name):
        r = self.export_rva(name)
        return self.rva2off(r) if r is not None else None


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
