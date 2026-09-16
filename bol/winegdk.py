"""Install and validate the reviewed prebuilt WineGDK engine."""
# SPDX-License-Identifier: MIT

import os
import posixpath
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from .config import (
    CACHE,
    PROTON_DIR,
    WINEGDK_ARCHIVE_SHA256,
    WINEGDK_BUILD_REV,
    WINEGDK_OUT,
    WINEGDK_PREBUILT_REPO,
)
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


def _verify_engine_archive(path: Path):
    expected = WINEGDK_ARCHIVE_SHA256.strip().lower()
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise ValueError(
            "launcher has no valid SHA-256 pin for engine revision %s" %
            WINEGDK_BUILD_REV)
    actual = sha256_file(path)
    if actual != expected:
        raise ValueError(
            "engine archive SHA-256 mismatch (expected %s, got %s)" %
            (expected, actual))


def _wire_winegdk():
    """Select the managed WineGDK engine and apply its runtime patches."""
    s = load_settings()
    s["proton"] = str(WINEGDK_OUT)
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
    with managed_engine_lock(PROTON_DIR):
        patch_proton(WINEGDK_OUT, strict=False)
    return WINEGDK_OUT


def _validate_engine_candidate(root: Path):
    """Reject an incomplete or mislabeled engine before touching the active one."""
    if not (root / "proton").is_file() or not (root / "files").is_dir():
        raise ValueError("archive has no complete Proton tree")
    # Imported lazily so source-only development remains usable while the
    # validator is built alongside this installer.  Managed archives carry a
    # hash-pinned content manifest; it rejects revision, component or DLL-byte
    # mismatches before the active engine is touched.
    from .vkd3d import validate_engine_manifest
    validate_engine_manifest(
        root, WINEGDK_BUILD_REV, enforce_pins=True)


def _recover_interrupted_engine_swap_locked():
    """Recover a validated rollback left by a crash during atomic activation.

    The caller owns ``managed_engine_lock(PROTON_DIR)``.  A valid active engine
    tree is authoritative and makes an old rollback disposable.  If the active
    tree is absent or invalid but the rollback validates as the current pinned
    revision, restore it transactionally before any network lookup.  When
    neither validates, preserve both until a new candidate has itself been
    downloaded and validated; diagnostic leftovers are preferable to deleting
    the only recoverable bytes while offline.
    """
    backup = WINEGDK_OUT.with_name("." + WINEGDK_OUT.name + ".rollback")
    if not (backup.exists() or backup.is_symlink()):
        return

    active_exists = WINEGDK_OUT.exists() or WINEGDK_OUT.is_symlink()
    active_valid = False
    backup_valid = False
    active_error = backup_error = None
    if active_exists:
        try:
            _validate_engine_candidate(WINEGDK_OUT)
            active_valid = True
        except Exception as exc:
            active_error = exc
    try:
        _validate_engine_candidate(backup)
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

    displaced = WINEGDK_OUT.with_name(
        "." + WINEGDK_OUT.name + ".interrupted-invalid")
    if displaced.exists() or displaced.is_symlink():
        remove_path(displaced)
    if active_exists:
        WINEGDK_OUT.replace(displaced)
    try:
        backup.replace(WINEGDK_OUT)
    except Exception:
        if active_exists and (displaced.exists() or displaced.is_symlink()) \
                and not (WINEGDK_OUT.exists() or WINEGDK_OUT.is_symlink()):
            displaced.replace(WINEGDK_OUT)
        raise
    else:
        if displaced.exists() or displaced.is_symlink():
            try:
                remove_path(displaced)
            except OSError as exc:
                warn("Recovered the game engine but could not remove the "
                     "invalid interrupted tree: %s" % exc)
        warn("Recovered the verified game engine after an interrupted update.")

def _activate_engine_locked(candidate: Path):
    """Atomically activate a validated candidate while the stable lock is held."""
    WINEGDK_OUT.parent.mkdir(parents=True, exist_ok=True)
    backup = WINEGDK_OUT.with_name("." + WINEGDK_OUT.name + ".rollback")

    # Recover a previous interrupted swap before attempting another one.
    if backup.exists() or backup.is_symlink():
        if not WINEGDK_OUT.exists():
            backup.replace(WINEGDK_OUT)
        else:
            remove_path(backup)

    had_current = WINEGDK_OUT.exists() or WINEGDK_OUT.is_symlink()
    if had_current:
        WINEGDK_OUT.replace(backup)
    try:
        candidate.replace(WINEGDK_OUT)
    except Exception:
        # rename(2) is atomic on this filesystem.  Nevertheless remove a path
        # left by an exotic filesystem before putting the known-good tree back.
        if WINEGDK_OUT.exists() or WINEGDK_OUT.is_symlink():
            remove_path(WINEGDK_OUT)
        if had_current and (backup.exists() or backup.is_symlink()):
            backup.replace(WINEGDK_OUT)
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

    Each revision is an ~860 MB download kept in the cache, and nothing ever
    removed one once the launcher pinned the next: a machine that followed
    native13 through native17 carried 3.4 GiB of archives no launcher would
    open again. An update needs room for the new archive *and* its 2.5 GiB
    unpacked tree before the old tree is let go, so on a nearly full disk
    those leftovers were the difference between updating and a launcher that
    refuses its own engine. Only engine archives named for another revision
    go, partial downloads included.
    """
    removed = freed = 0
    for stale in CACHE.glob("GDK-Proton-xuser-*.tar.gz*"):
        if stale.name in (keep, keep + ".part"):
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


def _install_prebuilt_winegdk(progress=None, force=False):
    """Install while holding the stable engine lock for the whole transaction.

    The download cache uses one ``.part`` filename per asset.  Keeping lookup,
    download, extraction and activation under the same inter-process lock
    prevents two launcher instances from deleting or renaming each other's
    partial archive during an engine update.
    """
    with managed_engine_lock(PROTON_DIR):
        _recover_interrupted_engine_swap_locked()
        return _install_prebuilt_winegdk_locked(progress, force)


def _install_prebuilt_winegdk_locked(progress=None, force=False):
    """Fetch + unpack a prebuilt GDK-Proton-xuser engine so end users never
    compile a full Wine. Downloads the asset from the deterministic release tag
    used by the engine publication workflow, or uses BOL_ENGINE_ARCHIVE for an
    explicit local archive.  The candidate is validated and transactionally
    activated.  False means no safe replacement was installed; callers may keep
    the current one."""
    asset = f"GDK-Proton-xuser-{WINEGDK_BUILD_REV}.tar.gz"
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
            f"engine-{WINEGDK_BUILD_REV}/{asset}"
        )
        archive = CACHE / asset
        if force:
            # A release asset may have been corrected under the same filename;
            # --force must not silently reinstall the stale cached bytes.
            remove_path(archive)
            remove_path(archive.with_suffix(archive.suffix + ".part"))
        _prune_stale_engine_archives(asset)
        if not archive.exists():
            info("Downloading the game engine (prebuilt, one-time) …")
            try:
                download(url, archive, "Game engine", progress)
            except Exception as e:
                warn(f"Game engine download failed ({e}).")
                return False

    info("Unpacking and validating the game engine …")
    PROTON_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".xuser-dl-", dir=PROTON_DIR))
    try:
        _verify_engine_archive(archive)
        with tarfile.open(archive) as t:
            _extract_archive(t, tmp)
        root = tmp if (tmp / "proton").is_file() else next(
            (p for p in tmp.iterdir() if p.is_dir()
             and (p / "proton").is_file()), None)
        if root is None:
            raise ValueError("archive has no Proton tree")
        _validate_engine_candidate(root)
        # The installer already owns the stable lock.
        _activate_engine_locked(root)
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
    install only the reviewed, manifest-pinned compatibility archive."""
    s = load_settings()
    installed_current = False
    # Inspect and recover the on-disk state under the same stable lock used by
    # activation.  Without this, a power loss after active→rollback but before
    # candidate→active made an offline launch report "no engine" even though a
    # complete, verified rollback was still present.
    with managed_engine_lock(PROTON_DIR):
        _recover_interrupted_engine_swap_locked()
        installed = (WINEGDK_OUT / "proton").is_file()
        if installed:
            try:
                # Settings are only a cache. Releases up to 1.2.9 could leave
                # the setting and extracted tree on different revisions. The
                # hash-pinned on-disk manifest is authoritative.
                _validate_engine_candidate(WINEGDK_OUT)
                installed_current = True
            except Exception as exc:  # invalid candidates auto-heal
                warn("Installed game engine needs replacement (%s)." % exc)
    if not force and installed_current:
        expected_stamp = "prebuilt:" + WINEGDK_BUILD_REV
        if s.get("winegdk_built") != expected_stamp:
            s["winegdk_built"] = expected_stamp
            save_settings(s)
        info(f"WineGDK engine ready ({WINEGDK_BUILD_REV}).")
        return _wire_winegdk()
    if installed:
        info(f"Updating the game engine → {WINEGDK_BUILD_REV} …")
    if _install_prebuilt_winegdk(progress, force=force):
        s = load_settings()
        s["winegdk_built"] = "prebuilt:" + WINEGDK_BUILD_REV
        save_settings(s)
        return _wire_winegdk()
    # An unavailable/invalid update must never destroy a working engine or fall
    # into the source builder, which historically removed the active tree before
    # compilation had succeeded.  Keep the old revision and retry next launch.
    if installed_current:
        warn(f"Game engine {WINEGDK_BUILD_REV} is unavailable — keeping the "
             "installed engine.")
        return _wire_winegdk()
    if installed:
        die("Required game engine %s could not be installed. The previous "
            "engine was preserved but is incompatible with this launcher. "
            "Check network access and retry. AppImage/zipapp users can also "
            "keep %s beside the launcher; Flatpak and packaged-app users "
            "should retry Install / Update after connectivity is restored." %
            (WINEGDK_BUILD_REV,
             f"GDK-Proton-xuser-{WINEGDK_BUILD_REV}.tar.gz"))
    die("No verified game-engine archive is available for %s. Connect to the "
        "network and retry. AppImage/zipapp users can also place %s beside "
        "the launcher; Flatpak and packaged-app users should retry Install / "
        "Update after connectivity is restored." %
        (WINEGDK_BUILD_REV,
         f"GDK-Proton-xuser-{WINEGDK_BUILD_REV}.tar.gz"))
