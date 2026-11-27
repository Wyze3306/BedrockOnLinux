"""Install and validate the reviewed prebuilt WineGDK engine."""
# SPDX-License-Identifier: MIT

import os
import posixpath
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from . import engine_arch
from .config import (
    CACHE,
    PROTON_DIR,
    WINEGDK_ARCHIVE_SHA256,
    WINEGDK_ARM64_ARCHIVE_SHA256,
    WINEGDK_ARM64_BUILD_REV,
    WINEGDK_ARM64_OUT,
    WINEGDK_BUILD_REV,
    WINEGDK_OUT,
    WINEGDK_PREBUILT_REPO,
)
from .engine_arch import Engine
from .engine_lock import managed_engine_lock
from .log import die, info, ok, warn
from .proton import patch_proton
from .util import (
    download,
    load_settings,
    remove_path,
    save_settings,
    sha256_file,
)

def _extract_archive(archive, destination: Path):
    """Extract a trusted engine archive without permitting path escapes.

    Python 3.12+'s data filter also validates links and special files.  Keep a
    lexical traversal check for supported older Python versions where that
    filter is unavailable.
    """
    members = archive.getmembers()
    symlinks = set()
    names = set()
    for member in members:
        raw_name = member.name
        name = posixpath.normpath(raw_name)
        pure = PurePosixPath(name)
        if (not raw_name or name in ("", ".") or pure.is_absolute()
                or name == ".." or name.startswith("../")):
            raise ValueError(f"unsafe path in engine archive: {raw_name}")
        if name in names:
            raise ValueError(f"duplicate path in engine archive: {raw_name}")
        names.add(name)
        if not (member.isfile() or member.isdir() or member.issym()):
            # Managed engines are packed with --hard-dereference. Rejecting
            # hard links and special/PAX-only members keeps extraction safe
            # on Python < 3.12.
            raise ValueError(f"unsupported entry in engine archive: {raw_name}")
        if member.issym():
            target = member.linkname
            if not target or PurePosixPath(target).is_absolute():
                raise ValueError(
                    f"unsafe symlink in engine archive: {raw_name} -> {target}")
            resolved = posixpath.normpath(
                posixpath.join(posixpath.dirname(name), target))
            if resolved == ".." or resolved.startswith("../"):
                raise ValueError(
                    f"unsafe symlink in engine archive: {raw_name} -> {target}")
            symlinks.add(name)

    # A regular member below a path that the archive itself makes a symlink can
    # otherwise make old tarfile versions write through that link.
    for name in names:
        parts = PurePosixPath(name).parts
        for end in range(1, len(parts)):
            if PurePosixPath(*parts[:end]).as_posix() in symlinks:
                raise ValueError(
                    f"archive member is nested below a symlink: {name}")

    if hasattr(tarfile, "data_filter"):
        archive.extractall(destination, members=members, filter="data")
    else:
        archive.extractall(destination, members=members)


def engine_for(arch):
    """The managed engine of ``arch``, from the pins of this module.

    Both engines share PROTON_DIR, the cache and the one engine lock; each
    has its own directory, archive name and rollback, so installing one
    never touches the other's tree.
    """
    if arch == engine_arch.ARM64EC:
        return Engine(engine_arch.ARM64EC, WINEGDK_ARM64_BUILD_REV,
                      WINEGDK_ARM64_ARCHIVE_SHA256, WINEGDK_ARM64_OUT)
    return Engine(engine_arch.X86_64, WINEGDK_BUILD_REV,
                  WINEGDK_ARCHIVE_SHA256, WINEGDK_OUT)


def active_engine(settings=None):
    """The managed engine this computer runs now (bol.engine_arch)."""
    return engine_for(engine_arch.active(
        load_settings() if settings is None else settings))


def _verify_engine_archive(path: Path, eng=None):
    eng = eng or engine_for(engine_arch.X86_64)
    expected = eng.archive_sha256.strip().lower()
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise ValueError(
            "launcher has no valid SHA-256 pin for engine revision %s" %
            (eng.build_rev or "(none)"))
    actual = sha256_file(path)
    if actual != expected:
        raise ValueError(
            "engine archive SHA-256 mismatch (expected %s, got %s)" %
            (expected, actual))


def _wire_winegdk(eng=None):
    """Select the managed WineGDK engine and apply its runtime patches."""
    eng = eng or engine_for(engine_arch.X86_64)
    s = load_settings()
    s["proton"] = str(eng.out)
    s["proton_tag"] = "winegdk"
    s["proton_source"] = "winegdk"
    # These fields mean "user-supplied engine" everywhere else.  Releases up
    # to 1.2.9 also used proton_dir for the managed canonical path, which made
    # custom_proton() suppress managed updates.  Wiring is the migration point:
    # once this function selects WineGDK, stale custom markers must disappear.
    s.pop("proton_dir", None)
    s.pop("proton_url", None)
    save_settings(s)
    # combase/ntdll patching mutates the managed tree. Serialize it with engine
    # replacement and vkd3d activation so two launchers cannot patch a tree
    # while another process renames it.
    # The ARM64EC engine has no x86-64 code to patch: patch_proton checks
    # each DLL's machine and leaves ARM64/ARM64EC/ARM64X ones alone.
    with managed_engine_lock(PROTON_DIR):
        patch_proton(eng.out, strict=False)
    return eng.out


def _validate_engine_candidate(root: Path, eng=None):
    """Reject an incomplete or mislabeled engine before touching the active one."""
    if not (root / "proton").is_file() or not (root / "files").is_dir():
        raise ValueError("archive has no complete Proton tree")
    # Imported lazily so source-only development remains usable while the
    # validator is built alongside this installer.  Managed archives carry a
    # hash-pinned content manifest; it rejects revision, component or DLL-byte
    # mismatches before the active engine is touched.
    from .vkd3d import validate_engine_manifest
    eng = eng or engine_for(engine_arch.X86_64)
    validate_engine_manifest(
        root, eng.build_rev, enforce_pins=True, arch=eng.manifest_arch)


def _recover_interrupted_engine_swap_locked(eng=None):
    """Recover a validated rollback left by a crash during atomic activation.

    The caller owns ``managed_engine_lock(PROTON_DIR)``.  A valid active engine
    tree is authoritative and makes an old rollback disposable.  If the active
    tree is absent or invalid but the rollback validates as the current pinned
    revision, restore it transactionally before any network lookup.  When
    neither validates, preserve both until a new candidate has itself been
    downloaded and validated; diagnostic leftovers are preferable to deleting
    the only recoverable bytes while offline.
    """
    eng = eng or engine_for(engine_arch.X86_64)
    out = eng.out
    backup = out.with_name("." + out.name + ".rollback")
    if not (backup.exists() or backup.is_symlink()):
        return

    active_exists = out.exists() or out.is_symlink()
    active_valid = False
    backup_valid = False
    active_error = backup_error = None
    if active_exists:
        try:
            _validate_engine_candidate(out, eng)
            active_valid = True
        except Exception as exc:
            active_error = exc
    try:
        _validate_engine_candidate(backup, eng)
        backup_valid = True
    except Exception as exc:
        backup_error = exc

    if active_valid:
        try:
            remove_path(backup)
        except OSError as exc:
            warn("Could not remove stale game-engine rollback: %s" % exc)
        return
    if not backup_valid:
        warn("Interrupted game-engine swap could not be auto-recovered "
             "(active: %s; rollback: %s); preserving both for replacement." %
             (active_error or "missing", backup_error))
        return

    displaced = out.with_name(
        "." + out.name + ".interrupted-invalid")
    if displaced.exists() or displaced.is_symlink():
        remove_path(displaced)
    if active_exists:
        out.replace(displaced)
    try:
        backup.replace(out)
    except Exception:
        if active_exists and (displaced.exists() or displaced.is_symlink()) \
                and not (out.exists() or out.is_symlink()):
            displaced.replace(out)
        raise
    else:
        if displaced.exists() or displaced.is_symlink():
            try:
                remove_path(displaced)
            except OSError as exc:
                warn("Recovered the game engine but could not remove the "
                     "invalid interrupted tree: %s" % exc)
        warn("Recovered the verified game engine after an interrupted update.")

def _activate_engine_locked(candidate: Path, eng=None):
    """Atomically activate a validated candidate while the stable lock is held."""
    out = (eng or engine_for(engine_arch.X86_64)).out
    out.parent.mkdir(parents=True, exist_ok=True)
    backup = out.with_name("." + out.name + ".rollback")

    # Recover a previous interrupted swap before attempting another one.
    if backup.exists() or backup.is_symlink():
        if not out.exists():
            backup.replace(out)
        else:
            remove_path(backup)

    had_current = out.exists() or out.is_symlink()
    if had_current:
        out.replace(backup)
    try:
        candidate.replace(out)
    except Exception:
        # rename(2) is atomic on this filesystem.  Nevertheless remove a path
        # left by an exotic filesystem before putting the known-good tree back.
        if out.exists() or out.is_symlink():
            remove_path(out)
        if had_current and (backup.exists() or backup.is_symlink()):
            backup.replace(out)
        raise
    else:
        if backup.exists() or backup.is_symlink():
            try:
                remove_path(backup)
            except OSError as exc:
                # The candidate is already atomically active. Cleanup failure
                # must not be reported as a rejected install (or make setup
                # die); recovery will validate the active tree and retry this
                # harmless stale rollback on the next invocation.
                warn("Could not remove old game-engine rollback: %s" % exc)


def _prune_stale_engine_archives(keep):
    """Drop the cached archives of every engine revision but ``keep``.

    ``keep`` is one archive name or several: the current revision of each
    engine (x86-64 and ARM64EC) stays, so switching between them on an ARM
    computer does not throw away the other's download.

    Each revision is an ~860 MB download kept in the cache, and nothing ever
    removed one once the launcher pinned the next: a machine that followed
    native13 through native17 carried 3.4 GiB of archives no launcher would
    open again. An update needs room for the new archive *and* its 2.5 GiB
    unpacked tree before the old tree is let go, so on a nearly full disk
    those leftovers were the difference between updating and a launcher that
    refuses its own engine. Only engine archives named for another revision
    go, partial downloads included.
    """
    names = {keep} if isinstance(keep, str) else set(keep)
    kept = names | {name + ".part" for name in names}
    removed = freed = 0
    for stale in CACHE.glob("GDK-Proton-xuser-*.tar.gz*"):
        if stale.name in kept:
            continue
        if not stale.name.endswith((".tar.gz", ".tar.gz.part")):
            continue
        try:
            size = stale.stat().st_size
            remove_path(stale)
        except OSError as exc:
            warn(f"Could not remove the cached archive of an older game "
                 f"engine ({stale.name}): {exc}")
            continue
        removed += 1
        freed += size
    if removed:
        info(f"Removed {removed} cached archive(s) of older game engines "
             f"({freed / 1024 ** 3:.1f} GiB).")


def _install_prebuilt_winegdk(progress=None, force=False, eng=None):
    """Install while holding the stable engine lock for the whole transaction.

    The download cache uses one ``.part`` filename per asset.  Keeping lookup,
    download, extraction and activation under the same inter-process lock
    prevents two launcher instances from deleting or renaming each other's
    partial archive during an engine update.
    """
    eng = eng or engine_for(engine_arch.X86_64)
    with managed_engine_lock(PROTON_DIR):
        _recover_interrupted_engine_swap_locked(eng)
        return _install_prebuilt_winegdk_locked(progress, force, eng)


def _current_archive_names():
    """The archive of each engine revision this launcher pins."""
    return {eng.asset for eng in (engine_for(engine_arch.X86_64),
                                  engine_for(engine_arch.ARM64EC))
            if eng.build_rev}


def _install_prebuilt_winegdk_locked(progress=None, force=False, eng=None):
    """Fetch + unpack a prebuilt GDK-Proton-xuser engine so end users never
    compile a full Wine. Downloads the asset from the deterministic release tag
    used by the engine publication workflow, or uses BOL_ENGINE_ARCHIVE for an
    explicit local archive.  The candidate is validated and transactionally
    activated.  False means no safe replacement was installed; callers may keep
    the current one."""
    eng = eng or engine_for(engine_arch.X86_64)
    asset = eng.asset
    override = os.environ.get("BOL_ENGINE_ARCHIVE", "").strip()
    if not override:
        # Portable candidates are shipped beside their exact engine archive.
        # AppImage exposes its original path through APPIMAGE; zipapps use
        # argv[0]. The normal hash-pinned manifest validation still applies.
        anchors = []
        appimage = os.environ.get("APPIMAGE", "").strip()
        if appimage:
            anchors.append(Path(appimage).expanduser().resolve().parent)
        try:
            anchors.append(Path(sys.argv[0]).expanduser().resolve().parent)
        except (OSError, RuntimeError):
            pass
        for anchor in anchors:
            sibling = anchor / asset
            if sibling.is_file():
                override = str(sibling)
                break
    local_archive = bool(override)
    if local_archive:
        archive = Path(override).expanduser()
        if not archive.is_file():
            warn(f"BOL_ENGINE_ARCHIVE does not exist: {archive}")
            return False
        info(f"Using local game engine archive: {archive}")
    else:
        # Engine releases have a stable tag/asset contract.  Addressing that
        # contract directly avoids the generic release-list cache hiding an
        # engine published shortly after the launcher metadata was cached.
        url = (
            f"https://github.com/{WINEGDK_PREBUILT_REPO}/releases/download/"
            f"engine-{eng.build_rev}/{asset}"
        )
        archive = CACHE / asset
        if force:
            # A release asset may have been corrected under the same filename;
            # --force must not silently reinstall the stale cached bytes.
            remove_path(archive)
            remove_path(archive.with_suffix(archive.suffix + ".part"))
        _prune_stale_engine_archives(_current_archive_names() | {asset})
        if not archive.exists():
            info("Downloading the %s (prebuilt, one-time) …" %
                 ("ARM game engine" if eng.arch == engine_arch.ARM64EC
                  else "game engine"))
            try:
                download(url, archive, "Game engine", progress)
            except Exception as e:
                warn(f"Game engine download failed ({e}).")
                return False

    info("Unpacking and validating the game engine …")
    PROTON_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".xuser-dl-", dir=PROTON_DIR))
    try:
        _verify_engine_archive(archive, eng)
        with tarfile.open(archive) as t:
            _extract_archive(t, tmp)
        root = tmp if (tmp / "proton").is_file() else next(
            (p for p in tmp.iterdir() if p.is_dir()
             and (p / "proton").is_file()), None)
        if root is None:
            raise ValueError("archive has no Proton tree")
        _validate_engine_candidate(root, eng)
        # The installer already owns the stable lock.
        _activate_engine_locked(root, eng)
    except Exception as e:
        warn(f"Prebuilt engine rejected ({e}) — keeping the installed engine.")
        if not local_archive:
            remove_path(archive)        # retry corrected bytes next time
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    ok("Game engine ready (prebuilt — no compiler needed).")
    return True


def ensure_winegdk(force=False, progress=None):
    """Provide the WineGDK engine (XUser + request signing) and wire it in:
    install only the reviewed, manifest-pinned compatibility archive.

    Which engine follows bol.engine_arch: the x86-64 one everywhere, or the
    experimental ARM64EC one on an ARM computer whose player chose it. When
    that one cannot be had -- not published yet, refused, offline on first
    use -- the x86-64 engine is installed or kept instead, never nothing.
    """
    settings = load_settings()
    notice = engine_arch.fallback_notice(settings)
    if notice:
        warn(notice)
    eng = active_engine(settings)
    if eng.arch == engine_arch.ARM64EC:
        wired = _ensure_engine(eng, force, progress, required=False)
        if wired is not None:
            return wired
        warn("The ARM game engine could not be installed; using the x86-64 "
             "engine this time.")
        eng = engine_for(engine_arch.X86_64)
    return _ensure_engine(eng, force, progress)


def _ensure_engine(eng, force=False, progress=None, required=True):
    """Install or keep ``eng`` and wire it in; its directory, or None when
    ``required`` is False and it could not be installed."""
    s = load_settings()
    installed_current = False
    out = eng.out
    rev = eng.build_rev
    stamp_key = ("winegdk_arm64_built" if eng.arch == engine_arch.ARM64EC
                 else "winegdk_built")
    # Inspect and recover the on-disk state under the same stable lock used by
    # activation.  Without this, a power loss after active→rollback but before
    # candidate→active made an offline launch report "no engine" even though a
    # complete, verified rollback was still present.
    with managed_engine_lock(PROTON_DIR):
        _recover_interrupted_engine_swap_locked(eng)
        installed = (out / "proton").is_file()
        if installed:
            try:
                # Settings are only a cache. Releases up to 1.2.9 could leave
                # the setting and extracted tree on different revisions. The
                # hash-pinned on-disk manifest is authoritative.
                _validate_engine_candidate(out, eng)
                installed_current = True
            except Exception as exc:  # invalid candidates auto-heal
                warn("Installed game engine needs replacement (%s)." % exc)
    if not force and installed_current:
        expected_stamp = "prebuilt:" + rev
        if s.get(stamp_key) != expected_stamp:
            s[stamp_key] = expected_stamp
            save_settings(s)
        info(f"WineGDK engine ready ({rev}).")
        return _wire_winegdk(eng)
    if installed:
        info(f"Updating the game engine → {rev} …")
    if _install_prebuilt_winegdk(progress, force=force, eng=eng):
        s = load_settings()
        s[stamp_key] = "prebuilt:" + rev
        save_settings(s)
        return _wire_winegdk(eng)
    # An unavailable/invalid update must never destroy a working engine or fall
    # into the source builder, which historically removed the active tree before
    # compilation had succeeded.  Keep the old revision and retry next launch.
    if installed_current:
        warn(f"Game engine {rev} is unavailable — keeping the "
             "installed engine.")
        return _wire_winegdk(eng)
    if not required:
        return None
    if installed:
        die("Required game engine %s could not be installed. The previous "
            "engine was preserved but is incompatible with this launcher. "
            "Check network access and retry. AppImage/zipapp users can also "
            "keep %s beside the launcher; Flatpak and packaged-app users "
            "should retry Install / Update after connectivity is restored." %
            (rev, eng.asset))
    die("No verified game-engine archive is available for %s. Connect to the "
        "network and retry. AppImage/zipapp users can also place %s beside "
        "the launcher; Flatpak and packaged-app users should retry Install / "
        "Update after connectivity is restored." % (rev, eng.asset))
