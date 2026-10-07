"""bol.saves — the player's worlds, settings and servers across versions."""
# SPDX-License-Identifier: MIT

import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from .config import DATA, GAMES
from .log import BolError, info, ok, warn
from .util import load_settings, save_settings

# Everything the player made -- worlds, settings, the Servers tab, skins,
# packs, screenshots -- sits under one Users folder per edition, beside
# caches the game rebuilds by itself. Every build of an edition reads the
# same one: an account's folder is named after the account (the FNV-1 hash
# of its XUID), never after the build, so a new version opens exactly what
# the last one saved. Preview is the exception, and Mojang's on purpose: it
# keeps a folder of its own, so that a world it upgrades can never reach the
# stable game.
_ROAMING = "drive_c/users/steamuser/AppData/Roaming"
DATA_FOLDERS = {
    "release": "Minecraft Bedrock",
    "preview": "Minecraft Bedrock Preview",
}
EDITION_NAMES = {"release": "Minecraft", "preview": "Minecraft Preview"}
# The package identity a build's manifest carries, for a folder the launcher
# did not download and so cannot name the edition of by where it sits.
_IDENTITIES = {
    "Microsoft.MinecraftUWP": "release",
    "Microsoft.MinecraftWindowsBeta": "preview",
}

BACKUPS = DATA / "backups"
# A version change is rare, and worlds are big: five is a few changes back
# without the folder becoming the largest thing the launcher keeps.
KEEP = 5
_RECORD = "backup.json"
_PARTIAL = ".partial-"
# Room left on the drive after a copy, so that a backup never becomes the
# reason the game has nowhere to save the world it just opened.
_HEADROOM = 512 * 1024 * 1024
# Beside the Users folder they replace, so that taking it out of the way and
# putting the new one in are both renames.
_INCOMING = ".Users.bol-incoming"
_OUTGOING = ".Users.bol-outgoing"

# The build that last opened each edition's data, by edition id.
LAST_PLAYED = "last_played_builds"
# Whether Preview starts from a copy of the stable game's data: "copied" once
# it has, "separate" when the player chose to keep them apart.
PREVIEW_DATA = "preview_data"

_ACCOUNT_OPTIONS = "*/games/com.mojang/minecraftpe/options.txt"
_SIGNED_OUT_OPTIONS = "Shared/games/com.mojang/minecraftpe/options.txt"


def _prefix(prefix=None):
    if prefix is not None:
        return Path(prefix)
    from .prefix import active_prefix
    return active_prefix()


def users_dir(edition, prefix=None):
    """The folder holding everything the player made in ``edition``."""
    return _prefix(prefix) / _ROAMING / DATA_FOLDERS[edition] / "Users"


def _worlds(users):
    try:
        return [world for world in
                Path(users).glob("*/games/com.mojang/minecraftWorlds/*")
                if world.is_dir()]
    except OSError:
        return []


def _has_player_data(users):
    """Whether the game has saved anything worth keeping in ``users``.

    A world, or settings: the game writes its options file the first time
    it runs, so a folder without one has only ever held what this launcher
    seeded into it before that.
    """
    if _worlds(users):
        return True
    try:
        return any(path.is_file() for path in Path(users).glob(_ACCOUNT_OPTIONS))
    except OSError:
        return False


def _dir_size(path):
    from .games import _dir_size
    return _dir_size(path)


# ------------------------------------------------------------ which build

def _manifest_edition(game_dir):
    for name in ("appxmanifest.xml", "AppxManifest.xml"):
        try:
            text = (Path(game_dir) / name).read_text(errors="ignore")
        except OSError:
            continue
        match = re.search(r'<Identity[^>]*\bName="([^"]+)"', text)
        if match:
            return _IDENTITIES.get(match.group(1))
    return None


def launched_build(settings=None):
    """The (edition, version) PLAY starts, or (None, None).

    A build the launcher downloaded is named by its folder,
    games/<edition>/<version>/. Anything else is named by its own manifest,
    since the setting it was picked with can belong to another build.
    """
    s = load_settings() if settings is None else settings
    game_dir = (s.get("game_dir") or "").strip()
    if not game_dir:
        return None, None
    try:
        parts = Path(game_dir).resolve().relative_to(GAMES.resolve()).parts
    except (OSError, ValueError):
        parts = ()
    if len(parts) == 2 and parts[0] in DATA_FOLDERS:
        return parts[0], parts[1]
    from .games import mc_version_str
    edition = _manifest_edition(game_dir) or s.get("mc_edition")
    version = mc_version_str(Path(game_dir)) or s.get("mc_version")
    if edition not in DATA_FOLDERS or not version:
        return None, None
    return edition, str(version)


# ------------------------------------------------------------ backups

def _record(folder):
    try:
        record = json.loads((Path(folder) / _RECORD).read_text("utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("edition") not in DATA_FOLDERS:
        return None
    return record


def list_backups():
    """Every backup, newest first, each with its folder as ``path``."""
    out = []
    try:
        entries = list(BACKUPS.iterdir())
    except OSError:
        return out
    for folder in entries:
        if folder.name.startswith(".") or folder.is_symlink() \
                or not (folder / "Users").is_dir():
            continue
        record = _record(folder)
        if record is None:
            continue
        out.append(dict(record, path=folder))
    out.sort(key=lambda item: (item.get("created") or 0, item["path"].name),
             reverse=True)
    return out


def _prune(protect=()):
    """Keep the newest KEEP backups, and drop what a failed copy left."""
    try:
        for leftover in BACKUPS.glob(_PARTIAL + "*"):
            shutil.rmtree(leftover, ignore_errors=True)
    except OSError:
        pass
    keep = {Path(path).resolve() for path in protect}
    for backup in list_backups()[KEEP:]:
        if backup["path"].resolve() in keep:
            continue
        shutil.rmtree(backup["path"], ignore_errors=True)


def back_up(edition, reason, version=None, previous=None, prefix=None,
            protect=()):
    """Copy ``edition``'s data into a new backup; return its folder.

    Returns None when there is nothing to keep. Raises BolError when there
    is something and it could not be copied, so that nothing which is about
    to overwrite that data goes ahead on the strength of a backup that does
    not exist.
    """
    source = users_dir(edition, prefix)
    if not _has_player_data(source):
        return None
    size = _dir_size(source)
    try:
        BACKUPS.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(BACKUPS).free
    except OSError as exc:
        raise BolError(f"Could not create {BACKUPS}: {exc}") from exc
    if free < size + _HEADROOM:
        raise BolError(
            f"Not enough free space to back up your "
            f"{EDITION_NAMES[edition]} worlds and settings "
            f"({size / 1024 ** 2:.0f} MB, {free / 1024 ** 2:.0f} MB free "
            f"on the drive holding {BACKUPS}).")
    # To the microsecond: it is what orders the backups, and two can be
    # made within the same second.
    created = time.time()
    staged = Path(tempfile.mkdtemp(prefix=_PARTIAL, dir=BACKUPS))
    try:
        shutil.copytree(source, staged / "Users", symlinks=True)
        record = {
            "schema": 1,
            "edition": edition,
            "reason": reason,
            "version": version,
            "previous": previous,
            "created": created,
            "size": size,
            "worlds": len(_worlds(staged / "Users")),
        }
        (staged / _RECORD).write_text(
            json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
        name = time.strftime("%Y%m%d-%H%M%S", time.localtime(created))
        final = BACKUPS / f"{name}-{edition}"
        for index in range(2, 100):
            if not final.exists():
                break
            final = BACKUPS / f"{name}-{edition}-{index}"
        os.rename(staged, final)
    except OSError as exc:
        shutil.rmtree(staged, ignore_errors=True)
        raise BolError(
            f"Could not back up your {EDITION_NAMES[edition]} worlds and "
            f"settings: {exc}") from exc
    _prune(protect=tuple(protect) + (final,))
    return final


def _recover_interrupted_swap(target):
    """Finish, or undo, a replacement the launcher was stopped in."""
    outgoing = target.with_name(_OUTGOING)
    incoming = target.with_name(_INCOMING)
    if outgoing.exists() and not target.exists():
        # Stopped between the two renames: what went out is the real data.
        os.rename(outgoing, target)
    shutil.rmtree(outgoing, ignore_errors=True)
    shutil.rmtree(incoming, ignore_errors=True)


def _replace_users(source, edition, prefix=None):
    """Make a copy of the Users tree ``source`` what ``edition`` reads.

    The copy is complete before anything is moved, and the swap is two
    renames inside one folder, so a failure at any point leaves the player
    with one whole set of data: the old one or the new one.
    """
    target = users_dir(edition, prefix)
    target.parent.mkdir(parents=True, exist_ok=True)
    _recover_interrupted_swap(target)
    incoming = target.with_name(_INCOMING)
    outgoing = target.with_name(_OUTGOING)
    shutil.copytree(source, incoming, symlinks=True)
    if target.exists():
        os.rename(target, outgoing)
    try:
        os.rename(incoming, target)
    except OSError:
        if outgoing.exists():
            os.rename(outgoing, target)
        raise
    shutil.rmtree(outgoing, ignore_errors=True)


def _require_game_closed(prefix=None, action="change your saved data"):
    from .prefix import require_prefix_idle
    require_prefix_idle(_prefix(prefix), action)


def restore_backup(path, prefix=None):
    """Put the data of one backup back; return the backup made of the
    data it replaced, or None when there was none."""
    folder = Path(path)
    record = _record(folder)
    if record is None or not (folder / "Users").is_dir():
        raise BolError(f"{folder} is not a backup this launcher made.")
    edition = record["edition"]
    _require_game_closed(prefix, "restore a backup")
    from .prefix import prefix_operation_lock
    with prefix_operation_lock("restore a backup"):
        # What is there now is the player's too, and restoring is a choice
        # they may want back.
        kept = back_up(edition, "before-restore", prefix=prefix,
                       protect=(folder,))
        try:
            _replace_users(folder / "Users", edition, prefix)
        except OSError as exc:
            raise BolError(
                f"Could not restore the backup: {exc}. Your data was left "
                "as it was.") from exc
    ok(f"{EDITION_NAMES[edition]} worlds and settings restored from "
       f"{_describe(record)}.")
    return kept


def _describe(record):
    when = time.strftime("%Y-%m-%d %H:%M",
                         time.localtime(record.get("created") or 0))
    if record.get("reason") == "version-change" and record.get("version"):
        return f"the backup made before {record['version']} ({when})"
    return f"the backup of {when}"


# ------------------------------------------------------------ preview

def preview_copy_pending(settings=None, prefix=None):
    """Whether Preview would start empty while the stable game has data.

    Asked once: after a copy, or after the player chose to keep Preview
    apart, Preview's data is its own and nothing writes over it.
    """
    s = load_settings() if settings is None else settings
    if s.get(PREVIEW_DATA):
        return False
    return (not _worlds(users_dir("preview", prefix))
            and _has_player_data(users_dir("release", prefix)))


def keep_preview_separate():
    s = load_settings()
    s[PREVIEW_DATA] = "separate"
    save_settings(s)


def copy_to_preview(prefix=None):
    """Give Preview a copy of the stable game's worlds, settings and servers.

    A copy, never a link: a world Preview opens is upgraded to a version the
    stable game does not have yet, which is the reason Mojang keeps the two
    apart. Whatever Preview already had is backed up first.
    """
    source = users_dir("release", prefix)
    if not _has_player_data(source):
        raise BolError("Minecraft has no worlds or settings to copy yet.")
    back_up("preview", "before-preview-copy", prefix=prefix)
    worlds = len(_worlds(source))
    info(f"Copying your Minecraft worlds, settings and servers into "
         f"Minecraft Preview ({_dir_size(source) / 1024 ** 2:.0f} MB) …")
    try:
        _replace_users(source, "preview", prefix)
    except OSError as exc:
        raise BolError(
            f"Could not copy your Minecraft data into Preview: {exc}") from exc
    s = load_settings()
    s[PREVIEW_DATA] = "copied"
    save_settings(s)
    ok(f"Minecraft Preview starts with a copy of your {worlds} world"
       f"{'' if worlds == 1 else 's'}, your settings and your servers. The "
       "originals stay with Minecraft, untouched by Preview.")


def copy_to_preview_when_idle(prefix=None):
    """copy_to_preview(), for a request made outside a launch."""
    _require_game_closed(prefix, "copy your worlds into Preview")
    from .prefix import prefix_operation_lock
    with prefix_operation_lock("copy your worlds into Preview"):
        copy_to_preview(prefix)


# ------------------------------------------------------------ at PLAY

def before_launch(settings=None, prefix=None):
    """Make sure what the build about to start opens can be had back.

    Runs with the prefix idle and the launch lock held. Preview is first
    given a copy of the stable game's data if the player has not said
    otherwise; then, if this build is not the one that last opened this
    edition's data, that data is backed up -- a world a newer version opens
    is upgraded for good, and one an older version opens may not load.
    Returns (edition, version), or (None, None) for a build it cannot name.
    """
    s = load_settings() if settings is None else settings
    edition, version = launched_build(s)
    if edition is None:
        return None, None
    copied = False
    if edition == "preview" and not s.get(PREVIEW_DATA):
        if preview_copy_pending(s, prefix):
            try:
                copy_to_preview(prefix)
                copied = True
            except BolError as exc:
                warn(f"{exc} Minecraft Preview starts with its own data.")
        elif _has_player_data(users_dir("preview", prefix)):
            # Preview already has a life of its own: never copy over it.
            keep_preview_separate()
    s = load_settings()
    last = s.get(LAST_PLAYED)
    last = dict(last) if isinstance(last, dict) else {}
    previous = last.get(edition)
    if previous != version:
        name = EDITION_NAMES[edition]
        try:
            # A copy made a moment ago has its original right beside it.
            kept = None if copied else back_up(
                edition, "version-change", version=version,
                previous=previous, prefix=prefix)
        except BolError as exc:
            warn(f"{exc} {name} {version} starts without a backup of them.")
        else:
            if kept is not None:
                ok(f"Your {name} worlds, settings and servers were backed up "
                   f"before {version} opens them — restore them from "
                   "Settings ▸ Versions if this version does not suit them.")
        last[edition] = version
        s = load_settings()
        s[LAST_PLAYED] = last
        save_settings(s)
    return edition, version


SIGNED_OUT_NOTICE = (
    "Minecraft started without your Microsoft account this time, so it "
    "opened the signed-out profile, which has none of your worlds, settings "
    "or servers. Nothing was lost: they are still in your account's folder, "
    "and the game shows them again as soon as it signs in. If this only "
    "happens with this version of Minecraft, go back to the one you played "
    "before until the launcher supports it."
)


def ran_signed_out(edition, since, prefix=None):
    """Whether the session that started at ``since`` played signed out
    while an account's data was there to be missed.

    The game keeps its settings beside the profile it plays as, and writes
    them while it runs. A signed-in session never writes the signed-out
    profile's file, so that one being written while no account's file was
    is the session having run without the account.
    """
    if edition not in DATA_FOLDERS:
        return False
    users = users_dir(edition, prefix)

    def written(path):
        try:
            return path.stat().st_mtime >= since
        except OSError:
            return False

    if not written(users / _SIGNED_OUT_OPTIONS):
        return False
    try:
        accounts = [path for path in users.glob(_ACCOUNT_OPTIONS)
                    if path.relative_to(users).parts[0] != "Shared"]
    except OSError:
        return False
    return bool(accounts) and not any(written(path) for path in accounts)
