"""bol.packcache — the server resource packs Minecraft keeps under Temp.

Every server that sends resource packs leaves them unpacked in
``%LocalAppData%\\Temp\\Minecraft Bedrock\\minecraftpe\\packcache``, one folder
per pack *version*. A server that updates its pack leaves the old version
behind, and on Windows nothing keeps that in check except the system's own
cleaning of temporary files. In a Wine prefix nothing cleans Temp at all, so
the cache only grows: measured on one player's prefix after a week, 4.8 GB in
300,000 files, 123 of its 177 folders older versions of packs it also held
in a newer one (28 versions of a single server's UI pack).

Before each launch, with the game closed, a version is removed when the same
pack is cached in a newer version and the old one has not been written to
for a week -- the age Windows' own cleanup gives temporary files. The newest
version of every pack is kept, so nothing a server sends today is lost; an
older version a server asks for again is downloaded again, as after a
Windows cleanup.
"""
# SPDX-License-Identifier: MIT

import json
import os
import shutil
import time
from pathlib import Path

from .saves import DATA_FOLDERS

_TEMP = "drive_c/users/steamuser/AppData/Local/Temp"
STALE_AFTER = 7 * 24 * 3600


def cache_dirs(prefix):
    """The pack cache folders of every edition in ``prefix``."""
    for folder in DATA_FOLDERS.values():
        root = Path(prefix) / _TEMP / folder / "minecraftpe" / "packcache"
        try:
            kinds = sorted(root.iterdir())
        except OSError:
            continue
        for kind in kinds:
            if kind.is_dir() and not kind.is_symlink():
                yield kind


def _identity(folder):
    """(uuid, version) from a cached pack's manifest, or None."""
    try:
        header = json.loads((folder / "manifest.json")
                            .read_text(encoding="utf-8-sig"))["header"]
        version = tuple(int(part) for part in header["version"])
        uuid = str(header["uuid"]).strip().lower()
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return (uuid, version) if uuid else None


def _last_written(folder):
    """When anything directly in ``folder`` was last written."""
    latest = folder.stat().st_mtime
    with os.scandir(folder) as entries:
        for entry in entries:
            try:
                latest = max(latest, entry.stat(follow_symlinks=False).st_mtime)
            except OSError:
                pass
    return latest


def _size(folder):
    total = 0
    for path in folder.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            pass
    return total


def stale_versions(cache, now=None):
    """The folders in ``cache`` that a newer version of their pack replaced
    more than STALE_AFTER ago. Unreadable folders are never among them."""
    now = time.time() if now is None else now
    packs = {}
    try:
        folders = list(cache.iterdir())
    except OSError:
        return []
    for folder in folders:
        if not folder.is_dir() or folder.is_symlink():
            continue
        identity = _identity(folder)
        if identity is not None:
            packs.setdefault(identity[0], []).append((identity[1], folder))
    stale = []
    for versions in packs.values():
        versions.sort(key=lambda item: item[0])
        for _version, folder in versions[:-1]:
            try:
                if now - _last_written(folder) > STALE_AFTER:
                    stale.append(folder)
            except OSError:
                pass
    return sorted(stale)


def prune(prefix, now=None):
    """Remove the stale pack versions of ``prefix``; (folders, bytes) freed.

    Only call it with the game closed: the game reads these folders.
    """
    removed = freed = 0
    for cache in cache_dirs(prefix):
        for folder in stale_versions(cache, now):
            size = _size(folder)
            shutil.rmtree(folder, ignore_errors=True)
            if not folder.exists():
                removed += 1
                freed += size
    return removed, freed
