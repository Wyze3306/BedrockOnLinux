"""bol.proton — GDK-Proton acquisition and patching."""
# SPDX-License-Identifier: MIT

import posixpath
import shutil
import struct
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from .config import CACHE, GDK_PROTON_REPO, PROTON_DIR, WINEGDK_OUT
from .engine_lock import managed_engine_lock
from .log import die, info, ok, warn
from .pe import PE, _backup_once, apply_patch, code_machine
from .util import (
    asset_url,
    download,
    gh_latest,
    gh_releases,
    load_settings,
    path_exists,
    remove_path,
    save_settings,
)

def proton_path():
    p = load_settings().get("proton")
    return Path(p) if p else None


def _canonical_winegdk_path(path):
    try:
        return Path(path).expanduser().resolve() == WINEGDK_OUT.resolve()
    except (OSError, RuntimeError, TypeError):
        return False


def custom_proton():
    """A user-supplied GDK-Proton (e.g. a fork built with WineGDK XUser).
    When set, our combase/ntdll offsets may not match — patch non-strict."""
    s = load_settings()
    # Older managed WineGDK installs persisted their canonical engine path in
    # ``proton_dir`` so the patcher would use its relaxed mode.  That field is
    # otherwise the marker for a *user-supplied* directory, and consequently
    # made the managed engine look custom to the launch-time updater.  Source
    # provenance wins over those legacy fields; recognise the canonical path
    # too so a partially migrated settings file is handled safely.
    if s.get("proton_source") == "winegdk":
        return False
    pdir = s.get("proton_dir")
    if pdir and _canonical_winegdk_path(pdir):
        return False
    return bool(s.get("proton_dir") or s.get("proton_url"))


def _extract_proton_archive(archive, destination: Path):
    """Extract an untrusted Proton tarball into an empty staging directory.

    Do the same validation on every supported Python version rather than
    relying on the version-dependent tarfile extraction filter.  Hard links
    and special files are unnecessary in GDK-Proton archives and are rejected;
    relative symlinks are allowed only when they remain inside the staging
    tree.  Extraction is manual so no archive path is ever passed to
    ``TarFile.extractall``.
    """
    members = archive.getmembers()
    entries = {}
    symlinks = set()
    directories = []

    for member in members:
        raw_name = member.name
        name = posixpath.normpath(raw_name)
        pure = PurePosixPath(name)
        if (not raw_name or name in ("", ".") or pure.is_absolute()
                or name == ".." or name.startswith("../")):
            raise ValueError(f"unsafe path in Proton archive: {raw_name}")
        if name in entries:
            raise ValueError(f"duplicate path in Proton archive: {raw_name}")

        if member.isdir():
            kind = "directory"
            directories.append((name, member.mode))
        elif member.isfile():
            kind = "file"
        elif member.issym():
            kind = "symlink"
            target = member.linkname
            if not target or PurePosixPath(target).is_absolute():
                raise ValueError(
                    f"unsafe symlink in Proton archive: {raw_name} -> {target}")
            resolved = posixpath.normpath(
                posixpath.join(posixpath.dirname(name), target))
            if resolved == ".." or resolved.startswith("../"):
                raise ValueError(
                    f"unsafe symlink in Proton archive: {raw_name} -> {target}")
            symlinks.add(name)
        else:
            raise ValueError(
                f"unsupported entry in Proton archive: {raw_name}")
        entries[name] = (member, kind)

    # A member below an archive-created symlink could make older tarfile
    # implementations write outside the staging tree.  Files cannot be parent
    # directories either, so reject every structural conflict up front.
    for name in entries:
        parts = PurePosixPath(name).parts
        for end in range(1, len(parts)):
            parent = PurePosixPath(*parts[:end]).as_posix()
            parent_entry = entries.get(parent)
            if parent_entry and parent_entry[1] != "directory":
                if parent in symlinks:
                    raise ValueError(
                        f"archive member is nested below a symlink: {name}")
                raise ValueError(
                    f"archive member is nested below a file: {name}")

    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError("Proton extraction staging directory is not empty")

    # Create directories with a writable temporary mode.  Archive modes are
    # restored after every file has been written.
    for name, (_, kind) in entries.items():
        if kind == "directory":
            (destination / name).mkdir(parents=True, exist_ok=True, mode=0o700)

    for name, (member, kind) in entries.items():
        output = destination / name
        if kind == "directory":
            continue
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path_exists(output):
            raise ValueError(f"conflicting path in Proton archive: {name}")
        if kind == "symlink":
            output.symlink_to(member.linkname)
            continue
        source = archive.extractfile(member)
        if source is None:
            raise tarfile.ExtractError(
                f"could not read Proton archive member: {name}")
        with source, output.open("xb") as stream:
            shutil.copyfileobj(source, stream)
        # Keep the owner able to validate, patch and later remove the staged
        # tree even if a hostile archive supplied mode 000.
        output.chmod((member.mode & 0o777) | 0o600)

    for name, mode in sorted(
            directories, key=lambda item: len(PurePosixPath(item[0]).parts),
            reverse=True):
        (destination / name).chmod((mode & 0o777) | 0o700)


def _find_proton_root(staging: Path):
    """Return the single complete Proton tree at the archive's top level."""
    def complete(root):
        executable = root / "proton"
        files = root / "files"
        return (executable.is_file() and not executable.is_symlink()
                and executable.stat().st_mode & 0o111
                and files.is_dir() and not files.is_symlink())

    roots = []
    if complete(staging):
        roots.append(staging)
    for child in staging.iterdir():
        if (child.is_dir() and not child.is_symlink()
                and complete(child)):
            roots.append(child)
    if len(roots) != 1:
        raise ValueError(
            "archive must contain exactly one complete Proton tree with an "
            "executable 'proton' file and a real 'files' directory")
    return roots[0]


def _installation_target(current, fallback_name):
    """Reuse the active direct child when safe, otherwise use a stable name."""
    if current:
        candidate = Path(current).expanduser()
        try:
            if (candidate.parent.resolve() == PROTON_DIR.resolve()
                    and not candidate.name.startswith(".")
                    and candidate.is_dir() and not candidate.is_symlink()):
                return candidate
        except (OSError, RuntimeError):
            pass
    return PROTON_DIR / fallback_name


def _activate_proton(candidate: Path, target: Path, commit=None):
    """Atomically swap a staged Proton tree, restoring the old tree on error.

    ``commit`` runs while the known-good tree is still retained as a rollback
    directory.  Callers use it to persist settings only after the rename; a
    settings-write failure therefore rolls the engine back too.
    """
    candidate = Path(candidate)
    target = Path(target)
    if (target.parent.resolve() != PROTON_DIR.resolve()
            or target.name.startswith(".")):
        raise ValueError(
            "Proton activation target must be a non-hidden PROTON_DIR child")
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = target.with_name("." + target.name + ".rollback")

    with managed_engine_lock(PROTON_DIR):
        # Recover or discard the residue of an interrupted earlier swap.
        if path_exists(backup):
            if not path_exists(target):
                backup.replace(target)
            else:
                remove_path(backup)

        had_target = path_exists(target)
        if had_target:
            target.replace(backup)
        try:
            candidate.replace(target)
            if commit is not None:
                commit()
        except Exception:
            if path_exists(target):
                remove_path(target)
            if had_target and path_exists(backup):
                backup.replace(target)
            raise
        else:
            if path_exists(backup):
                try:
                    remove_path(backup)
                except OSError as exc:
                    # The engine and settings are already committed.  Keep a
                    # harmless rollback tree for recovery on the next update
                    # instead of misreporting a failed installation.
                    warn(f"Could not remove old Proton rollback tree: {exc}")


def ensure_proton(tag=None, force=False, progress=None):
    s = load_settings()
    cur = proton_path()

    pdir = s.get("proton_dir")
    if pdir:
        root = Path(pdir).expanduser()
        if not (root / "proton").exists():
            die(f"proton_dir has no 'proton' executable: {root}")
        s["proton"], s["proton_tag"] = str(root), "custom-dir"
        save_settings(s)
        ok(f"GDK-Proton (custom dir): {root}")
        patch_proton(root, strict=False)
        return root

    purl = s.get("proton_url")
    if purl:
        want = "custom-url:" + purl
        if cur and cur.exists() and not force and s.get("proton_tag") == want:
            info("GDK-Proton (custom URL) already installed")
            return cur
        fname = purl.rsplit("/", 1)[-1] or "gdk-proton-custom.tar.gz"
        tar = CACHE / ("custom-" + fname)
        if not tar.exists() or force:
            info("Downloading custom GDK-Proton …")
            download(purl, tar, "GDK-Proton (custom)", progress)
        info("Extracting and validating custom GDK-Proton …")
        PROTON_DIR.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".proton-dl-", dir=PROTON_DIR))
        try:
            with tarfile.open(tar) as archive:
                _extract_proton_archive(archive, staging)
            root = _find_proton_root(staging)
            # Patch the isolated candidate.  Even an unexpected exception here
            # cannot modify or remove the active engine.
            patch_proton(root, strict=False)
            target = _installation_target(cur, "GDK-Proton-custom")
            updated = dict(s)
            updated["proton"], updated["proton_tag"] = str(target), want
            _activate_proton(
                root, target, commit=lambda: save_settings(updated))
        except Exception as exc:
            die(f"Invalid custom GDK-Proton archive ({exc}).")
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        ok(f"GDK-Proton (custom URL) ready: {target}")
        return target

    if cur and cur.exists() and not force and (not tag or s.get("proton_tag") == tag):
        return cur
    rel = (next((r for r in gh_releases(GDK_PROTON_REPO, 20)
                 if r["tag_name"] == tag), None) if tag else gh_latest(GDK_PROTON_REPO))
    if not rel:
        die(f"GDK-Proton version '{tag}' not found.")
    tag = rel["tag_name"]
    if cur and cur.exists() and s.get("proton_tag") == tag:
        info(f"GDK-Proton already up to date ({tag})")
        return cur
    url, fname, _ = asset_url(rel, lambda n: n.endswith(".tar.gz"))
    if not url:
        die("GDK-Proton archive not found.")
    tar = CACHE / fname
    if not tar.exists():
        info(f"Downloading GDK-Proton {tag} …")
        download(url, tar, f"GDK-Proton {tag}", progress)
    info("Extracting and validating GDK-Proton (~1.5 GiB) …")
    PROTON_DIR.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".proton-dl-", dir=PROTON_DIR))
    try:
        with tarfile.open(tar) as archive:
            _extract_proton_archive(archive, staging)
        root = _find_proton_root(staging)
        patch_proton(root)
        target = _installation_target(cur, "GDK-Proton-stock")
        updated = dict(s)
        updated["proton"], updated["proton_tag"] = str(target), tag
        _activate_proton(root, target, commit=lambda: save_settings(updated))
    except Exception as exc:
        die(f"Invalid GDK-Proton archive ({exc}).")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    ok(f"GDK-Proton {tag} ready")
    return target


def patch_proton(root: Path, strict=True):
    """Two binary patches that let Bedrock GDK 1.26 run under Wine.
    Idempotent; offsets found structurally; backups as *.bol-orig.
    strict=False (custom builds): a mismatch warns instead of aborting —
    a fork may already handle this or use a different Wine."""
    # Managed WineGDK builds historically used proton_dir to select relaxed
    # patching.  They are no longer classified as custom, but their fork may
    # still have already applied a patch or use different prologues.  Preserve
    # that safe behaviour based on the canonical engine path, independently of
    # user/custom classification.
    if strict and _canonical_winegdk_path(root):
        strict = False

    def fail(m):
        if strict:
            die(m)
        warn(m + " (custom build — continuing)")

    wine = root / "files/lib/wine/x86_64-windows"
    combase, ntdll = wine / "combase.dll", wine / "ntdll.dll"
    if not combase.exists() or not ntdll.exists():
        if (root / "files/lib/wine/aarch64-windows/ntdll.dll").exists():
            warn("This Proton's Wine is built for ARM64; the x86-64 combase "
                 "and ntdll patches do not apply to it and are skipped.")
            return
        return fail(f"Wine DLLs missing in {wine}")
    # Both patches write x86-64 instructions. An ARM64EC or ARM64X Wine
    # (#250) keeps the same file names, and relax=True below stubs over any
    # prologue, so without this check they would land in ARM code.
    for dll in (combase, ntdll):
        arch = code_machine(dll)
        if arch not in (None, "x86_64"):
            warn(f"{dll.name} holds {arch} code, not x86-64; the combase and "
                 "ntdll patches are skipped.")
            return

    # (1) combase.RoOriginateErrorW stub aborts at the package-identity check.
    off = PE(combase).export_off("RoOriginateErrorW")
    if off is None:
        return fail("RoOriginateErrorW not found in combase.dll")
    # The prologue varies by Wine build (4883ec28 / 4883ec48 / …), but the
    # entry offset is resolved from the export table, so stub it regardless:
    # plant `xor eax,eax; ret; nop` so RoOriginateErrorW always returns S_OK.
    # Only replace the four-byte prologue: overwriting farther into the body can
    # clobber an adjacent entry point when Wine changes the function layout.
    apply_patch(combase, off, bytes.fromhex("4883ec28"),
                bytes.fromhex("31c0c390"),
                "combase.RoOriginateErrorW", strict=strict, relax=True)

    # (2) ntdll: neutralise every unimplemented-stub funnel (prologue + call
    #     to RtlRaiseException + EB F6 never-return loop) so unimplemented
    #     imports (NtQueryWnfStateData via GameInput) return STATUS_NOT_IMPL
    #     instead of killing the game ~10 min in.
    pe = PE(ntdll)
    rre = pe.export_rva("RtlRaiseException")
    d = pe.data
    sig = bytes.fromhex("55534881ecc8000000488dac24c0000000")
    new = bytes.fromhex("b8020000c0c3") + b"\x90\x90"
    funnels = []
    if rre is not None:
        i = d.find(sig)
        while i >= 0:
            j = d.find(bytes.fromhex("4889d9e8"), i, i + 0x90)
            if j >= 0:
                cf = j + 3
                rel = struct.unpack_from("<i", d, cf + 1)[0]
                call_rva = pe.off2rva(cf)
                if (call_rva is not None and call_rva + 5 + rel == rre
                        and d[cf + 5:cf + 7] == b"\xeb\xf6"):
                    funnels.append(i)
            i = d.find(sig, i + 1)
    if funnels:
        raw = bytearray(d)
        for o in funnels:
            raw[o:o + len(new)] = new
        _backup_once(ntdll)
        ntdll.write_bytes(raw)
        ok(f"ntdll: {len(funnels)} stub(s) neutralised")
    elif d.count(new + bytes.fromhex("00488dac24c0000000")):
        info("ntdll: already patched")
    else:
        # Current WineGDK implements this import as a direct, safe
        # STATUS_NOT_IMPLEMENTED return.  There is no shared raise-exception
        # funnel to patch in that layout; recognise the exported stub itself
        # so the reviewed engine is not reported as suspicious on every run.
        wnf = pe.export_off("NtQueryWnfStateData")
        # Optimised builds may collapse the function to ``mov eax,status;
        # ret``.  The reviewed WineGDK build keeps its FIXME logging and the
        # documented ``if (buffer_size) *buffer_size = 0`` guard, producing
        # the longer canonical epilogue below.  Recognise both forms: neither
        # reaches Wine's aborting unimplemented-import funnel.
        direct_return = bytes.fromhex("b8020000c0c3")
        guarded_return = bytes.fromhex(
            "4885db7406c70300000000b8020000c04883c4505bc3")
        body = d[wnf:wnf + 0x100] if wnf is not None else b""
        if (body.startswith(direct_return)
                or guarded_return in body):
            info("ntdll.NtQueryWnfStateData: already returns "
                 "STATUS_NOT_IMPLEMENTED")
            return
        if rre is None:
            return fail("RtlRaiseException not resolved in ntdll.dll")
        fail("ntdll: no stub found — Proton layout changed.")
