"""One-time migration from pre-XDG storage into the active XDG data root."""
# SPDX-License-Identifier: MIT

import fcntl
import json
import os
import shutil
import tempfile
from pathlib import Path

from .config import APP, DATA, DEFAULT_DATA, HOME, LEGACY_DATA, UMU_DIR
from .log import info, warn


_STAGING_INFIX = ".xdg-migration-"


def is_flatpak(environ=None, info_path=Path("/.flatpak-info")):
    source = os.environ if environ is None else environ
    return bool(source.get("FLATPAK_ID")) or Path(info_path).is_file()


def _directory_is_empty(path):
    try:
        return path.is_dir() and next(path.iterdir(), None) is None
    except OSError:
        return False


def _remove_staging_path(path):
    """Delete one staging path without ever following a directory symlink."""
    try:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.exists():
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def _discard_stale_staging(destination):
    """Drop staging trees an interrupted migration could not clean up.

    Activation renames the staging tree onto the destination, so anything left
    under this name is by construction an incomplete copy this module created
    and never published.  The caller holds the exclusive migration lock, so no
    concurrent copy can be using it.  Keeping such a leftover would otherwise
    make every later start fail — in the Flatpak sandbox the launcher usually
    receives the same PID, so the old PID-derived staging name collided on
    every run and the app could never open again.
    """
    destination = Path(destination)
    parent = destination.parent
    prefix = f".{destination.name}{_STAGING_INFIX}"
    try:
        leftovers = [
            entry for entry in parent.iterdir()
            if entry.name.startswith(prefix)
        ]
    except OSError:
        return
    for leftover in leftovers:
        warn(f"Discarding an interrupted migration copy: {leftover}")
        _remove_staging_path(leftover)


def _copy_tree_transactionally(source, destination, prepare=None):
    """Copy a directory without exposing a half-copied destination."""
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _discard_stale_staging(destination)
    # A unique staging name keeps two launchers (and a reused sandbox PID) from
    # ever colliding on the same path.
    staging = Path(tempfile.mkdtemp(
        prefix=f".{destination.name}{_STAGING_INFIX}",
        dir=destination.parent,
    ))
    activated = False
    try:
        shutil.copytree(source, staging, symlinks=True, dirs_exist_ok=True)
        if prepare is not None:
            prepare(staging)
        os.replace(staging, destination)
        activated = True
    finally:
        # Remove partial staging trees so ENOSPC remains recoverable.
        if not activated:
            _remove_staging_path(staging)


def _reanchor_copied_paths(source, copied_root, settings_bytes,
                           content_target, active_root=None):
    """Make absolute paths in the copied tree refer to its new root."""
    copied_root = Path(copied_root)
    active_root = Path(active_root or copied_root)
    settings = copied_root / "settings.json"
    if settings_bytes is not None:
        # Never rewrite through a copied settings symlink into external state.
        if settings.is_symlink():
            settings.unlink()
            settings.write_bytes(settings_bytes)
            os.chmod(settings, 0o600)
        try:
            values = json.loads(settings_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            warn(
                "Legacy settings.json is unreadable; it was preserved "
                "unchanged in the XDG data folder."
            )
        else:
            old_game_dir = values.get("game_dir")
            if old_game_dir:
                try:
                    relative = Path(old_game_dir).resolve().relative_to(
                        (source / "games").resolve()
                    )
                except (OSError, ValueError):
                    pass
                else:
                    values["game_dir"] = str(
                        (active_root / "games" / relative).resolve()
                    )
                    settings.write_text(
                        json.dumps(values, indent=2) + "\n",
                        encoding="utf-8",
                    )

    if content_target is None:
        return
    target = content_target
    try:
        target = active_root.resolve() / target.relative_to(source.resolve())
    except ValueError:
        pass
    content = copied_root / "content"
    if content.is_symlink() or content.exists():
        if content.is_dir() and not content.is_symlink():
            shutil.rmtree(content)
        else:
            content.unlink()
    content.symlink_to(target)


def migrate_legacy_flatpak_data(
        environ=None, info_path=Path("/.flatpak-info"),
        old_data=None, new_data=None, old_umu=None, new_umu=None):
    """Copy legacy data into the active XDG folder before first use.

    Returns ``True`` only when application data was migrated. It never merges
    two populated trees: an existing destination wins and the legacy source is
    left untouched for manual inspection. The old tree is retained as a
    recovery backup after both Flatpak and native XDG migrations.
    """
    source_env = os.environ if environ is None else environ
    if str(source_env.get("BOL_HOME", "")).strip():
        return False
    if new_data is None and DATA != DEFAULT_DATA:
        # A location chosen in Settings is not an XDG data root. Relocation
        # moved the user's data there and left the rest of the old root (the
        # engine, caches, logs) behind, so that is no legacy tree waiting to
        # be copied -- and the migration lock would have gone beside the
        # chosen folder, in a directory the user need not be able to write
        # to, refusing every start (#255).
        return False
    flatpak = is_flatpak(source_env, info_path)

    source = Path(old_data or LEGACY_DATA)
    destination = Path(new_data or DATA)
    runtime_source = Path(old_umu or (HOME / ".local" / "share" / "umu"))
    runtime_destination = Path(new_umu or UMU_DIR)
    if source == destination or not source.is_dir():
        return False
    source_resolved = source.resolve()
    destination_resolved = destination.resolve(strict=False)
    if source_resolved == destination_resolved:
        return False
    try:
        destination_resolved.relative_to(source_resolved)
    except ValueError:
        pass
    else:
        raise RuntimeError(
            "the XDG destination is inside the legacy data directory"
        )
    try:
        source_resolved.relative_to(destination_resolved)
    except ValueError:
        pass
    else:
        raise RuntimeError(
            "the legacy data directory is inside the XDG destination"
        )

    profiles = source / "profiles"
    try:
        has_profiles = profiles.is_dir() and any(
            entry.is_dir() and (entry / "profile.json").is_file()
            for entry in profiles.iterdir()
        )
    except OSError as exc:
        raise RuntimeError(
            f"cannot inspect legacy account profiles before XDG migration: "
            f"{exc}"
        ) from exc
    if has_profiles:
        # Absolute profile links would split locks and safety state across roots.
        raise RuntimeError(
            "automatic XDG migration is disabled because the legacy data "
            "root contains isolated account profiles; keep the previous XDG "
            "data location or move/recreate those profiles explicitly first"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    lock_path = destination.parent / f".{APP}-xdg-migration.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if destination.exists():
            if (destination / ".xdg-storage").is_file():
                return False
            if _directory_is_empty(destination):
                destination.rmdir()
            else:
                warn(
                    "Both legacy and XDG Flatpak data folders contain files; "
                    "the XDG folder is in use and the legacy folder was left "
                    f"untouched at {source}."
                )
                return False

        settings_path = source / "settings.json"
        settings_bytes = (
            settings_path.read_bytes() if settings_path.is_file() else None
        )
        old_content = source / "content"
        content_target = (
            old_content.resolve(strict=False)
            if old_content.is_symlink() else None
        )

        def prepare_migrated_tree(staging):
            _reanchor_copied_paths(
                source,
                staging,
                settings_bytes,
                content_target,
                active_root=destination,
            )
            (staging / ".xdg-storage").write_text(
                ("copied-read-only\n" if flatpak else "migrated\n"),
                encoding="utf-8",
            )

        try:
            _copy_tree_transactionally(
                source,
                destination,
                prepare=prepare_migrated_tree,
            )
        except Exception:
            if destination.exists():
                shutil.rmtree(destination)
            raise

        if runtime_source.is_dir() and not runtime_destination.exists():
            try:
                _copy_tree_transactionally(
                    runtime_source, runtime_destination,
                )
            except Exception as exc:
                # UMU is redownloadable; user data has already moved atomically.
                warn(f"Could not migrate the old UMU runtime ({exc}).")
        info(f"Migrated legacy data to the standard XDG folder: {destination}")
        return True
