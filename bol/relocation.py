# SPDX-License-Identifier: MIT
"""Transactional relocation of BedrockOnLinux user data."""
import errno
import json
import os
import shutil
from pathlib import Path

from . import log
from .config import INSTALL_LOCATION_FILE, set_install_location

# "xodus-home" holds the Microsoft Store sign-in. It is the one directory here
# that cannot simply be fetched again: a lost keyring costs one of the
# account's ten Store download devices (issue #198), so moving the data root
# must take it along rather than leave the user to sign in from nothing.
# "backups" holds the copies of the player's worlds made before a version
# change; left behind, they would be out of the launcher's sight for good.
DIRS_TO_MOVE = ["games", "compatdata/pfx", "content", "msa", "xodus-home",
                "backups"]
# GPU safety state must move with the data root to preserve incident history.
FILES_TO_MOVE = [
    "settings.json",
    ".gpu-launch-in-progress.json",
    ".gpu-safety-ack.json",
]


class RelocationError(Exception):
    """Raised after a failed relocation and best-effort rollback."""


def paths_overlap(old_dir: Path, new_dir: Path) -> bool:
    """Return whether either canonical path contains the other."""
    old_r = old_dir.resolve()
    new_r = new_dir.resolve()
    if old_r == new_r:
        return True
    try:
        new_r.relative_to(old_r)
        return True
    except ValueError:
        pass
    try:
        old_r.relative_to(new_r)
        return True
    except ValueError:
        pass
    return False


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _transfer(src_path: Path, dst_path: Path, item: dict) -> None:
    """Move one path; ``item["complete"]`` once the destination holds it all.

    Within one filesystem that is a rename. Across two it is a copy and then
    a deletion, and the two can fail apart: a copy cut short (a full drive)
    leaves a partial destination beside an intact source, which is removed
    here; a deletion cut short leaves a complete destination beside what is
    left of the source, which rollback puts back.
    """
    try:
        os.rename(src_path, dst_path)
        item["complete"] = True
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    try:
        if src_path.is_dir() and not src_path.is_symlink():
            shutil.copytree(src_path, dst_path, symlinks=True)
        else:
            shutil.copy2(src_path, dst_path, follow_symlinks=False)
    except BaseException:
        try:
            _remove(dst_path)
        except OSError as e:
            log.warn(f"Could not remove the partial copy {dst_path}: {e}")
        raise
    item["complete"] = True
    _remove(src_path)


def _move_item(src_path: Path, dst_path: Path, moved_items: list) -> None:
    """Move one path, backing up its destination for rollback."""
    if not src_path.exists() and not src_path.is_symlink():
        return
    backup = None
    if dst_path.exists() or dst_path.is_symlink():
        backup = dst_path.with_name(dst_path.name + ".old")
        if backup.exists() or backup.is_symlink():
            if backup.is_dir() and not backup.is_symlink():
                shutil.rmtree(backup)
            else:
                backup.unlink()
        shutil.move(str(dst_path), str(backup))
    # Record before moving: cross-filesystem moves may fail after the backup.
    item = {"src": src_path, "dst": dst_path, "backup": backup,
            "complete": False}
    moved_items.append(item)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    _transfer(src_path, dst_path, item)


def _rollback(moved_items: list) -> None:
    """Undo recorded moves in reverse order without masking the root error."""
    for item in reversed(moved_items):
        src, dst, backup = item["src"], item["dst"], item["backup"]
        try:
            if item["complete"] and (dst.exists() or dst.is_symlink()):
                # The destination holds all of it; whatever the source still
                # has is the remainder of a deletion that was cut short.
                if src.exists() or src.is_symlink():
                    _remove(src)
                shutil.move(str(dst), str(src))
            if backup is not None and (backup.exists() or backup.is_symlink()):
                shutil.move(str(backup), str(dst))
        except Exception as e:
            log.warn(f"Rollback step failed for {src} <- {dst}: {e}")


def _restore_original_settings(old_dir: Path, original_bytes) -> None:
    """Restore settings content that may have been rewritten before failure."""
    if original_bytes is None:
        return
    try:
        (old_dir / "settings.json").write_bytes(original_bytes)
    except Exception as e:
        log.warn(f"Could not restore original settings.json: {e}")


def _restore_original_content_link(old_dir: Path, original_target) -> None:
    """Restore the content symlink's exact pre-relocation target."""
    if original_target is None:
        return
    old_content = old_dir / "content"
    try:
        if old_content.is_symlink() or old_content.exists():
            if old_content.is_dir() and not old_content.is_symlink():
                shutil.rmtree(old_content)
            else:
                old_content.unlink()
        old_content.symlink_to(original_target)
    except Exception as e:
        log.warn(f"Could not restore original content symlink: {e}")


def _rewrite_game_dir(new_dir: Path, old_games_dir: Path) -> None:
    """Re-anchor the absolute game_dir while preserving its relative version."""
    settings_path = new_dir / "settings.json"
    if not settings_path.exists():
        return
    with open(settings_path, "r") as f:
        settings_data = json.load(f)
    old_game_dir = settings_data.get("game_dir")
    if old_game_dir:
        try:
            rel = Path(old_game_dir).resolve().relative_to(
                old_games_dir.resolve())
            settings_data["game_dir"] = str((new_dir / "games" / rel).resolve())
        except ValueError:
            # A manually configured external game directory must remain intact.
            log.warn(
                "game_dir was not under the old games directory; "
                "leaving it unchanged")
    with open(settings_path, "w") as f:
        json.dump(settings_data, f, indent=2)


def _recreate_content_symlink(old_content_target, old_dir: Path, new_dir: Path) -> None:
    """Recreate a moved content link, re-anchoring only internal targets."""
    if old_content_target is None:
        return
    new_content = new_dir / "content"
    target = old_content_target
    try:
        rel = target.relative_to(old_dir.resolve())
        target = new_dir.resolve() / rel
    except ValueError:
        pass
    if new_content.is_symlink() or new_content.exists():
        if new_content.is_dir() and not new_content.is_symlink():
            shutil.rmtree(new_content)
        else:
            new_content.unlink()
    new_content.symlink_to(target)


def migrate_data(old_dir, new_dir) -> None:
    """Move user data and persist the new location transactionally."""
    old_dir = Path(old_dir)
    new_dir = Path(new_dir)
    if paths_overlap(old_dir, new_dir):
        raise RelocationError(
            "The new location overlaps with the current location.")
    profiles = old_dir / "profiles"
    try:
        # Only metadata-backed profile roots contain links relocation can break.
        has_profiles = profiles.is_dir() and any(
            entry.is_dir() and (entry / "profile.json").is_file()
            for entry in profiles.iterdir()
        )
    except OSError as exc:
        raise RelocationError(
            f"Could not inspect isolated profiles before relocation: {exc}"
        ) from exc
    if has_profiles:
        raise RelocationError(
            "This data root contains isolated account profiles. Relocation is "
            "disabled because moving their shared base would break the "
            "profile links. Move the data location before creating profiles, "
            "or remove/recreate the profile shortcuts after a manual move."
        )

    moved_items = []

    # Capture mutable state before any step so later failures are fully undoable.
    old_content = old_dir / "content"
    content_target = old_content.resolve() if old_content.is_symlink() else None
    original_content_link = (
        os.readlink(str(old_content)) if old_content.is_symlink() else None
    )
    settings_src = old_dir / "settings.json"
    original_settings_bytes = (
        settings_src.read_bytes() if settings_src.exists() else None
    )
    old_games_dir = old_dir / "games"

    try:
        new_dir.mkdir(parents=True, exist_ok=True)

        for sub in DIRS_TO_MOVE:
            _move_item(old_dir / sub, new_dir / sub, moved_items)
        for fname in FILES_TO_MOVE:
            _move_item(old_dir / fname, new_dir / fname, moved_items)

        _recreate_content_symlink(content_target, old_dir, new_dir)
        _rewrite_game_dir(new_dir, old_games_dir)
        set_install_location(new_dir)
    except Exception as e:
        _rollback(moved_items)
        _restore_original_content_link(old_dir, original_content_link)
        _restore_original_settings(old_dir, original_settings_bytes)
        try:
            set_install_location(old_dir)
        except Exception:
            try:
                INSTALL_LOCATION_FILE.parent.mkdir(parents=True, exist_ok=True)
                INSTALL_LOCATION_FILE.write_text(str(old_dir), encoding="utf-8")
            except Exception:
                pass
        raise RelocationError(str(e)) from e
