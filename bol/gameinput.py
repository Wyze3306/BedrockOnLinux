"""bol.gameinput — Microsoft GameInput redistributable install (MSI/CAB extraction)."""
# SPDX-License-Identifier: MIT

import struct
import zlib
from pathlib import Path

from .log import BolError, info, ok, warn
from .prefix import prefix_ready, require_prefix_idle
from .wine_registry import reg_dword, reg_sz, update_prefix_registry

def gameinput_redist_ok(prefix: Path):
    """True when the NATIVE Microsoft GameInput redist is fully installed.
    Testing system32/gameinput.dll proves nothing: wineboot pre-seeds every
    fresh prefix with the Wine BUILTIN gameinput.dll, whose mouse/controller
    readers need HID devices winebus never exposes — that builtin taking over
    is exactly the "keyboard works but mouse is dead in-game" bug. The game
    loads GameInputRedist.dll directly (it exports GameInputCreate) via the
    RedistDir registry, bypassing the builtin, only when this tree exists."""
    return ((prefix / "drive_c/Program Files/Microsoft GameInput/x64"
                      "/GameInputRedist.dll").exists()
            and (prefix / "drive_c/Program Files/Microsoft GameInput/x64"
                          "/GameInputRedistService.exe").exists())


def _msi_embedded_cab(msi: bytes):
    """Return the embedded MSZip CAB bytes from an MSI, parsing the OLE
    compound-file container in pure Python (no msiexec, no host tools). The
    CAB is stored as a stream scattered across OLE sectors — not contiguous —
    so we must walk the FAT chains. Returns the CAB bytes, or None."""
    if msi[:8] != bytes.fromhex("d0cf11e0a1b11ae1"):
        return None
    u = struct.unpack_from
    ssz = 1 << u("<H", msi, 0x1e)[0]
    mssz = 1 << u("<H", msi, 0x20)[0]
    dir0 = u("<I", msi, 0x30)[0]
    minicut = u("<I", msi, 0x38)[0]
    minifat0 = u("<I", msi, 0x3c)[0]
    difat0, ndifat = u("<I", msi, 0x44)[0], u("<I", msi, 0x48)[0]
    FREE, ENDC = 0xFFFFFFFF, 0xFFFFFFFE
    sect = lambda n: msi[(n + 1) * ssz:(n + 2) * ssz]
    difat = list(u("<109I", msi, 0x4c))
    nxt = difat0
    for _ in range(ndifat):
        if nxt in (FREE, ENDC):
            break
        vals = list(u("<%dI" % (ssz // 4), sect(nxt), 0))
        difat += vals[:-1]
        nxt = vals[-1]
    fat = []
    for fs in (d for d in difat if d != FREE):
        fat += list(u("<%dI" % (ssz // 4), sect(fs), 0))

    def chain(start):
        out, n, seen = [], start, set()
        while n not in (ENDC, FREE) and n < len(fat) and n not in seen:
            seen.add(n)
            out.append(n)
            n = fat[n]
        return out

    rbig = lambda s, sz: b"".join(sect(n) for n in chain(s))[:sz]
    dird = rbig(dir0, len(chain(dir0)) * ssz)
    ents = []
    for i in range(0, len(dird), 128):
        e = dird[i:i + 128]
        if len(e) < 128:
            break
        if u("<H", e, 64)[0]:
            ents.append((e[66], u("<I", e, 116)[0], u("<Q", e, 120)[0]))
    root = next((e for e in ents if e[0] == 5), None)
    if not root:
        return None
    ministream = rbig(root[1], root[2])
    mfat = []
    for ms in chain(minifat0):
        mfat += list(u("<%dI" % (ssz // 4), sect(ms), 0))

    def rmini(s, sz):
        out, n, seen = b"", s, set()
        while n not in (ENDC, FREE) and n < len(mfat) and n not in seen:
            seen.add(n)
            out += ministream[n * mssz:(n + 1) * mssz]
            n = mfat[n]
        return out[:sz]

    for typ, start, size in ents:
        if typ != 2 or size < 4:
            continue
        head = rbig(start, size) if size >= minicut else rmini(start, size)
        if head[:4] == b"MSCF":
            return head
    return None


def _cab_payload(cab: bytes):
    """Decompress an MSZip CAB. Returns [(uncompressed_size, bytes), …]."""
    if not cab or cab[:4] != b"MSCF":
        return []
    u = struct.unpack_from
    coff_files = u("<I", cab, 16)[0]
    cfolders, cfiles, flags = u("<HHH", cab, 26)
    o, cb_folder, cb_data = 36, 0, 0
    if flags & 4:
        cb_header, cb_folder, cb_data = u("<HBB", cab, o)
        o += 4 + cb_header
    folders = []
    for _ in range(cfolders):
        coff, ndata, _ = u("<IHH", cab, o)
        o += 8 + cb_folder
        folders.append((coff, ndata))
    files, p = [], coff_files
    for _ in range(cfiles):
        cb, uoff, ifol = u("<IIH", cab, p)
        p += 16
        p = cab.index(b"\x00", p) + 1
        files.append((cb, uoff, ifol))
    fdata = []
    for coff, ndata in folders:
        q, out, prev = coff, b"", b""
        for _ in range(ndata):
            cb_d = u("<IHH", cab, q)[1]
            q += 8 + cb_data
            blk = cab[q:q + cb_d]
            q += cb_d
            if blk[:2] != b"CK":
                return []
            d = zlib.decompressobj(-15, zdict=prev[-32768:] if prev else b"")
            out += d.decompress(blk[2:]) + d.flush()
            prev = out
        fdata.append(out)
    return [(cb, fdata[ifol][uoff:uoff + cb]) for cb, uoff, ifol in files]


def _pe_kind(data: bytes):
    """'dll' / 'exe' for a PE image, else None (e.g. the redist's .cat)."""
    if data[:2] != b"MZ" or len(data) < 0x40:
        return None
    pe = struct.unpack_from("<I", data, 0x3c)[0]
    if pe + 24 > len(data) or data[pe:pe + 4] != b"PE\0\0":
        return None
    return "dll" if struct.unpack_from("<H", data, pe + 22)[0] & 0x2000 else "exe"


_VS_FIXEDFILEINFO = struct.pack("<I", 0xFEEF04BD)


def _pe_file_version(data: bytes):
    """A PE image's FileVersion from its VS_FIXEDFILEINFO, e.g. (2, 2, 26100,
    6106); None when the image carries no version resource."""
    at = data.find(_VS_FIXEDFILEINFO)
    if at < 0 or at + 16 > len(data):
        return None
    high, low = struct.unpack_from("<II", data, at + 8)
    return (high >> 16, high & 0xFFFF, low >> 16, low & 0xFFFF)


def _bundled_redist_version(msi_path: Path):
    """The version of the GameInput redist a game package carries, or None.
    The redist itself is the largest DLL in the MSI, as in
    _extract_gameinput_redist."""
    cab = _msi_embedded_cab(msi_path.read_bytes())
    dlls = sorted((d for _, d in _cab_payload(cab) if _pe_kind(d) == "dll"),
                  key=len, reverse=True)
    return _pe_file_version(dlls[0]) if dlls else None


def _redist_outdated(prefix: Path, msi_path: Path):
    """True when the game ships a newer GameInput redist than the prefix has.

    A prefix keeps the redist it was first given, for every Minecraft build
    after it. On Windows the game's installer brings the machine's GameInput
    up to the version the build was made against; here nothing did, so a
    build that needs the newer one found an older redist answering instead,
    and the devices it reads through GameInput -- the camera's mouse
    movement and every controller -- never reached it (#300). A redist newer
    than the game's is left alone: GameInput keeps older callers working.
    """
    if not msi_path.exists():
        return False
    try:
        bundled = _bundled_redist_version(msi_path)
    except (OSError, ValueError, IndexError, struct.error, zlib.error):
        return False
    if bundled is None:
        return False
    try:
        installed = _pe_file_version(
            (prefix / "drive_c/Program Files/Microsoft GameInput/x64"
                      "/GameInputRedist.dll").read_bytes())
    except OSError:
        installed = None
    return installed is None or bundled > installed


def _extract_gameinput_redist(msi_path: Path, prefix: Path):
    """Install the GameInput redist by EXTRACTING the MSI's CAB ourselves and
    placing the files + registry — no Windows Installer, so the RtlGenRandom
    custom-action hang that stalls msiexec on some hosts can't happen. Files
    are identified structurally (PE dll/exe + size rank), which matches the
    redist's stable shape; the MSI's own OriginalFilename fields are unreliable
    (GameInputBridge reports itself as GameInputRedist). Returns True on
    success, or False for an unrecognised payload."""
    cab = _msi_embedded_cab(msi_path.read_bytes())
    pes = [(len(d), k, d) for _, d in _cab_payload(cab)
           for k in [_pe_kind(d)] if k]
    dlls = sorted([d for _, k, d in pes if k == "dll"], key=len, reverse=True)
    exes = sorted([d for _, k, d in pes if k == "exe"], key=len, reverse=True)
    if not dlls or not exes:
        return False
    x64 = prefix / "drive_c/Program Files/Microsoft GameInput/x64"
    x86 = prefix / "drive_c/Program Files/Microsoft GameInput/x86"
    sys32 = prefix / "drive_c/windows/system32"
    x64.mkdir(parents=True, exist_ok=True)
    # The redist has no reliable filenames; size order is its stable structure.
    (x64 / "GameInputRedist.dll").write_bytes(dlls[0])
    (sys32 / "GameInputRedist.dll").write_bytes(dlls[0])
    (x64 / "GameInputRedistService.exe").write_bytes(exes[0])
    if len(dlls) >= 2:
        (x64 / "GameInputBridge.dll").write_bytes(dlls[1])
    if len(exes) >= 2:
        (x64 / "GameInputRawInputProxy.exe").write_bytes(exes[1])
    if len(dlls) >= 3:
        x86.mkdir(parents=True, exist_ok=True)
        (x86 / "GameInputRedist.dll").write_bytes(dlls[2])
    return gameinput_redist_ok(prefix)


def _set_gameinput_registry(prefix: Path):
    """Point the game's GameInput loader at the extracted redist: RedistDir
    (both registry views) + the demand-start service entry, matching what the
    MSI writes.  Write the stopped prefix directly: starting ``reg.exe`` here
    also starts Wine Explorer and may initialise the GPU before Minecraft."""
    redist = r"C:\Program Files\Microsoft GameInput\x64"
    service = r"System\CurrentControlSet\Services\GameInputRedistService"
    changes = [
        reg_sz(r"Software\Microsoft\GameInput", "RedistDir", redist),
        reg_sz(r"Software\Wow6432Node\Microsoft\GameInput", "RedistDir",
               redist),
        reg_sz(service, "DisplayName", "GameInput Redist Service"),
        reg_sz(service, "Description", "GameInput Redist Service"),
        reg_sz(service, "ImagePath",
               redist + r"\GameInputRedistService.exe"),
        reg_sz(service, "ObjectName", "LocalSystem"),
        reg_dword(service, "ErrorControl", 0),
        reg_dword(service, "Start", 3),
        reg_dword(service, "Type", 0x10),
    ]
    try:
        update_prefix_registry(prefix, machine=changes)
        return True
    except Exception as e:
        warn("GameInput RedistDir offline registry update failed "
             f"({type(e).__name__}).")
        return False


def install_gameinput(prefix: Path, game_dir: Path):
    """Install the NATIVE Microsoft GameInput redist into the prefix. Heals
    prefixes from older releases (≤ 1.0.9) that fell back to Wine's builtin
    GameInput (no mouse) because the redist never actually installed.

    The installer EXTRACTS the redist straight from the game's
    Installers/GameInputRedist.msi (pure-Python OLE+CAB) and writes the
    registry — this avoids running Windows Installer entirely, whose post-copy
    RtlGenRandom custom action hangs msiexec for minutes on some hosts. An
    unrecognised payload fails closed with a warning; PLAY never starts an MSI,
    Wine Explorer, or a second graphics session as a fallback."""
    prefix = Path(prefix)
    if not prefix_ready(prefix):
        raise BolError(
            "Cannot install Microsoft GameInput: the Wine prefix is "
            "incomplete (system.reg, user.reg, or system32 is missing). "
            "Re-run 'Install / Update' to initialise it first."
        )
    require_prefix_idle(prefix, "install Microsoft GameInput offline")
    msi = game_dir / "Installers" / "GameInputRedist.msi"
    if gameinput_redist_ok(prefix):
        if not _redist_outdated(prefix, msi):
            _set_gameinput_registry(prefix)
            return
        info("Updating Microsoft GameInput to the version this Minecraft "
             "build ships …")
    elif not msi.exists():
        warn("GameInputRedist.msi missing from the game package — native "
             "GameInput not installed; the in-game mouse and controller "
             "will not work (Wine's builtin GameInput has no mouse backend).")
        return
    else:
        info("Installing Microsoft GameInput (native redist — in-game "
             "mouse) …")
    try:
        if _extract_gameinput_redist(msi, prefix) \
                and _set_gameinput_registry(prefix):
            ok("Microsoft GameInput installed (native redist)")
            return
    except Exception as e:
        warn(f"GameInput direct extraction failed ({e}).")
    # Do not automatically run msiexec as a fallback.  A setup-only failure is
    # preferable to silently starting another Wine/Explorer/GPU session in the
    # PLAY path; the current Bedrock MSI is supported by the pure-Python CAB
    # extractor above.
    warn("GameInput install incomplete — no Windows MSI helper was started. "
         "Re-run 'Install / Update' after checking the game package.")
