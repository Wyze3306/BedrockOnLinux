"""bol.games — Minecraft edition listing, installation and selection."""
# SPDX-License-Identifier: MIT

import json
import os
import re
import shutil
import time
from pathlib import Path

from . import xodus
from .config import CACHE, CONTENT, GAMES
from .log import BolError, die, info, ok, warn
from .util import load_settings, save_settings


_INSTALL_METADATA = ".bedrock-on-linux-install.json"


def prune_legacy_game_archives(cache=None):
    """Delete the Minecraft archives launchers before 2.1 kept in the cache.

    Until Minecraft came from Microsoft through Xodus, every version was
    downloaded as a zip of well over a gigabyte -- "Microsoft.MinecraftUWP_
    1.26.4403.0_x64__8wekyb3d8bbwe.zip" -- and kept in the cache once it was
    extracted. Nothing has read one since, and nothing removed them either,
    so an install that followed a few versions carried gigabytes of them
    (#289). Returns the bytes freed.
    """
    folder = Path(cache or CACHE)
    try:
        candidates = sorted(folder.iterdir())
    except OSError:
        return 0
    removed = freed = 0
    for path in candidates:
        name = path.name.lower()
        if "minecraft" not in name or not name.endswith((".zip", ".zip.part")):
            continue
        try:
            if path.is_symlink() or not path.is_file():
                continue
            size = path.stat().st_size
            path.unlink()
        except OSError as exc:
            warn(f"Could not remove {path.name}, a Minecraft archive an "
                 f"earlier version of the launcher downloaded: {exc}")
            continue
        removed += 1
        freed += size
    if removed:
        info(f"Removed {removed} Minecraft archive(s) an earlier version of "
             f"the launcher downloaded ({freed / 1024 ** 3:.1f} GiB).")
    return freed


def list_editions(include_beta=True):
    """The Minecraft editions available for installation."""
    return [entry for entry in xodus.list_editions()
            if include_beta or not entry["beta"]]


def version_dir(edition_id, version):
    return GAMES / edition_id / version


def list_versions(edition_id, ignore_cache=False):
    """Installable builds for an edition, newest first.

    Each entry gains ``installed``: whether that exact build is already on
    disk, which is what lets switching back to a build you already have cost
    nothing. ``ignore_cache`` bypasses the 12-hour cache on the build index,
    for when a build known to be out is not showing up yet.
    """
    out = []
    for entry in xodus.version_catalogue(edition_id, ignore_cache=ignore_cache):
        entry = dict(entry)
        entry["installed"] = _game_root(
            version_dir(edition_id, entry["version"])) is not None
        out.append(entry)
    return out


def _game_root(dest):
    """Folder of a complete installed build (exe + appxmanifest), else None
    (a bare exe with no manifest means a truncated install → reinstall).

    The shape is decided in bol.xodus, which is what writes the directory and
    has to tell a finished download from one that installed nothing. What is
    added here is the other way a folder can look complete and not be one: a
    Store build whose encrypted package went missing cannot be decrypted, so
    it is not something to launch — it is something to download again, and
    saying so is what gives PLAY a way to repair it (issue #216)."""
    root = xodus.game_root(dest)
    if root is None or xodus.lost_package_cache(root):
        return None
    return root


def _install_record(dest):
    try:
        return json.loads(
            (Path(dest) / _INSTALL_METADATA).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}


def _write_install_record(dest, edition, version, url):
    record = {
        "schema": 2,
        "edition": edition["id"],
        "product": edition["product"],
        "version": version,
        "source_url": url,
        "xodus_rev": xodus.XODUS_REV,
        "installed": int(time.time()),
    }
    target = Path(dest) / _INSTALL_METADATA
    staged = target.with_name("." + target.name + ".tmp")
    try:
        staged.write_text(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8")
        staged.replace(target)
    finally:
        staged.unlink(missing_ok=True)


def _configured_legacy_root():
    """A complete install configured before the move to the Store, or None.

    Anything under GAMES/<edition>/ belongs to the new layout and is handled
    by the ordinary path; this is only about the copy an upgrade inherits.
    """
    configured = (load_settings().get("game_dir") or "").strip()
    if not configured:
        return None
    path = Path(configured)
    try:
        # Legacy installs also live under GAMES, as GAMES/<version-tag>/, so
        # what marks the new layout is the edition id, not the parent.
        owner = path.resolve().relative_to(GAMES.resolve()).parts[0]
    except (ValueError, IndexError, OSError):
        owner = None
    if owner and xodus.edition(owner):
        return None
    return _game_root(path)


def install_game(edition, version=None, progress=None, force=False):
    """Install one build of one edition through Xodus.

    Each build lives in its own folder, so going back to a build already on
    disk costs nothing and the delta cache Xodus keeps beside it stays valid.
    ``xodus-cli streaming`` is itself incremental and atomic -- it compares
    local segment hashes against the package, fetches only what changed and
    commits with a rename -- so there is deliberately no staging dance here.
    """
    catalogue = list_versions(edition["id"])
    if not catalogue:
        raise BolError(
            f"No {edition['name']} build is listed. Check the network "
            "connection and try again.")
    wanted = str(version or "").strip()
    entry = next((c for c in catalogue if c["version"] == wanted), None)
    if entry is None:
        if wanted:
            warn(f"{edition['name']} {wanted} is no longer listed; using "
                 f"{catalogue[0]['version']} instead.")
        entry = catalogue[0]

    dest = version_dir(edition["id"], entry["version"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    root = _game_root(dest)
    if root and not force:
        info(f"{edition['name']} {entry['version']} already installed")
        return root

    # A build installed before the move to the Store lives outside
    # GAMES/<edition>/<version>/ and cannot be reached by this path. It is
    # still a complete, working game and it is the one the player has, so keep
    # it as the fallback rather than stranding them with nothing to launch.
    fallback = None if root else _configured_legacy_root()

    info(f"{'Reinstalling' if root else 'Installing'} {edition['name']} "
         f"{entry['version']} — this downloads it from Microsoft with your "
         "own account …")
    # Every mirror the index lists, not just the first: they carry the same
    # package, so a truncated body from one is retryable on the next.
    url = entry["urls"][0]
    try:
        xodus.install(entry["urls"], dest, progress)
    except xodus.NotSignedIn:
        # Actionable, and only the caller can act: never fold this into the
        # fallback below, or the launcher quietly keeps starting the old build
        # instead of offering the sign-in that would install this one.
        raise
    except BolError as exc:
        if fallback is None:
            raise
        warn(f"Could not download {edition['name']} {entry['version']} "
             f"({exc}) — starting the copy already installed. It predates the "
             "switch to the Microsoft Store, so it stays on its own build "
             "until the download works.")
        return fallback
    root = _game_root(dest)
    if not root:
        die(f"Minecraft.Windows.exe missing after installing "
            f"{edition['name']} {entry['version']}.")
    _write_install_record(dest, edition, entry["version"], url)
    ok(f"{edition['name']} {entry['version']} installed")
    _mention_other_builds(edition["id"], entry["version"])
    return root


def _human_size(size):
    value = float(size or 0)
    for unit in ("B", "KiB", "MiB"):
        if value < 1024:
            return f"{value:.0f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def _mention_other_builds(edition_id, version):
    """Say what the builds this download did not replace are taking up.

    Each build has a folder of its own, so a download never removes the one
    it follows. That is deliberate -- it is what makes going back instant --
    and it was completely invisible: nothing said the old build was still
    there, so a few version changes quietly became 10 GiB and the launcher
    read as "it keeps downloading Minecraft over and over" (issue #214).
    Saying it here turns that into a number and a place to act on it.
    """
    try:
        others = [build for build in installed_builds()
                  if build["managed"] and not (build["edition"] == edition_id
                                               and build["version"] == version)]
    except OSError:
        return
    if not others:
        return
    total = sum(build["size"] or 0 for build in others)
    info(f"{len(others)} other Minecraft build"
         f"{'s are' if len(others) != 1 else ' is'} still installed, taking "
         f"{_human_size(total)}. Remove the ones you are finished with in "
         "Settings ▸ Versions — worlds and settings are kept.")


def use_game_dir(folder):
    folder = Path(folder).expanduser().resolve()
    if not (folder / "Minecraft.Windows.exe").exists():
        cands = list(folder.rglob("Minecraft.Windows.exe"))
        if not cands:
            die(f"Minecraft.Windows.exe not found in {folder} (nor in "
                f"its subfolders). Choose an installed edition folder, "
                f"or use '① Minecraft edition'.")
        best = max(cands, key=lambda e: _vt(mc_version_str(e.parent) or "0"))
        folder = best.parent
        info(f"Minecraft found: {folder} "
             f"(version {mc_version_str(folder) or '?'})")
    if CONTENT.is_symlink() or CONTENT.exists():
        CONTENT.unlink() if CONTENT.is_symlink() else shutil.rmtree(CONTENT)
    CONTENT.symlink_to(folder)
    s = load_settings()
    s["game_dir"] = str(folder)
    # Remember what was selected so the picker and auto-select default to what
    # you last played. games/<edition>/<version>/ names both outright; a folder
    # from outside the managed tree names neither, and keeping the previous
    # choice there would silently reinstall over an imported copy.
    try:
        parts = folder.relative_to(GAMES.resolve()).parts
    except ValueError:
        parts = ()
    if len(parts) >= 2 and xodus.edition(parts[0]):
        s["mc_edition"] = parts[0]
        s["mc_version"] = parts[1]
    else:
        # An imported copy still reports its build, for display and bug reports.
        version = mc_version_str(folder)
        if version:
            s["mc_version"] = version
    save_settings(s)
    return folder


# ------------------------------------------------------------ installed builds

# Every build is downloaded into its own folder, which is what makes going
# back to one already on disk instant -- and what makes them pile up: three
# builds tried out is three copies of a 2.5 GiB game, and until this section
# existed nothing but `rm -rf` ever removed one (issue #214).
#
# Nothing the player made is in there. Worlds, settings, screenshots, skins
# and packs live in the Wine prefix, under the account that made them (see
# bol.content), and the prefix belongs to the profile rather than to any one
# build -- so removing a build removes the game and none of what was played
# with it. That sentence belongs wherever the launcher offers the removal:
# "delete this version" reads like "delete my worlds" to anyone who has not
# been told otherwise.


def _dir_size(path):
    """Bytes the tree under ``path`` holds, unreadable parts skipped."""
    total = 0
    stack = [str(path)]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return total


def _managed_parts(path):
    """``path`` as its parts under GAMES, or None when it is outside it.

    Resolved on both sides, so a symlinked data directory -- which is what
    "Storage ▸ Browse…" leaves behind -- is still recognised as the managed
    tree rather than treated as somewhere the launcher must not touch.
    """
    try:
        return Path(path).resolve().relative_to(GAMES.resolve()).parts
    except (OSError, ValueError):
        return None


def _selected_root():
    """The build folder the launcher would start next, or None."""
    configured = (load_settings().get("game_dir") or "").strip()
    if not configured:
        return None
    try:
        return Path(configured).expanduser().resolve()
    except OSError:
        return None


def _holds(folder, path):
    """Whether ``path`` is ``folder`` or something inside it."""
    if path is None:
        return False
    try:
        path.relative_to(Path(folder).resolve())
    except (OSError, ValueError):
        return False
    return True


def _build_entry(folder, edition_entry, version, selected, with_size=True):
    root = xodus.game_root(folder)
    record = _install_record(folder)
    managed = _managed_parts(folder) is not None
    return {
        "edition": edition_entry["id"] if edition_entry else None,
        "name": edition_entry["name"] if edition_entry else "Minecraft",
        "version": version or mc_version_str(root or folder) or "unknown",
        "path": Path(folder),
        "size": _dir_size(folder) if with_size else None,
        # Complete *and* decryptable: a Store build whose package went missing
        # still has an exe and a manifest and cannot be started (#216), and
        # the only thing to do with it is download it again.
        "playable": _game_root(folder) is not None,
        "in_use": _holds(folder, selected),
        "installed_at": record.get("installed"),
        # Only what the launcher itself downloaded is the launcher's to
        # delete; a folder the player imported stays theirs.
        "managed": managed,
        # In the tree the launcher owns, but not under an edition: an install
        # from before the move to the Store. It is a real build and the one
        # some players still have, so it is listed and removable like the
        # rest -- it just cannot be downloaded again from here.
        "legacy": managed and edition_entry is None,
    }


def installed_builds(with_size=True):
    """Every Minecraft build on disk, newest first.

    Covers the three shapes a build can have here: the managed layout
    (``games/<edition>/<version>/``), the one an install from before the move
    to the Store left behind (``games/<version>/``), and a copy the player
    pointed the launcher at from somewhere else -- listed so the one in use is
    always shown, and marked unmanaged so nothing offers to delete it.

    ``with_size=False`` skips walking each build, for callers that only need
    to know what is there.
    """
    selected = _selected_root()
    editions = {entry["id"]: entry for entry in list_editions(True)}
    out, seen = [], set()
    try:
        top = sorted(GAMES.iterdir())
    except OSError:
        top = []
    for entry in top:
        if not entry.is_dir() or entry.is_symlink():
            continue
        edition_entry = editions.get(entry.name)
        if edition_entry is not None:
            try:
                builds = sorted(entry.iterdir())
            except OSError:
                continue
            for build in builds:
                if not build.is_dir() or xodus.game_root(build) is None:
                    continue
                out.append(_build_entry(build, edition_entry, build.name,
                                        selected, with_size))
                seen.add(build.resolve())
            continue
        # The pre-Store layout: games/<version>/, with no edition above it.
        if xodus.game_root(entry) is not None:
            out.append(_build_entry(entry, None, entry.name, selected,
                                    with_size))
            seen.add(entry.resolve())
    if selected is not None and _game_root(selected) is not None and not any(
            _holds(build["path"], selected) for build in out):
        out.append(_build_entry(selected, None, None, selected, with_size))
    out.sort(key=lambda build: xodus.version_key(build["version"]),
             reverse=True)
    return out


def remove_build(path):
    """Delete one downloaded build and return the bytes that frees.

    Worlds, settings and screenshots are not in there -- see the note at the
    top of this section -- so this takes the download and nothing else. What
    it does have to take with it is the *selection*: the launcher starts
    whatever ``game_dir`` names, and a setting left pointing at a folder that
    is gone turns the next PLAY into a launch failure instead of the download
    it should be.
    """
    from .prefix import _mc_running

    folder = Path(path).expanduser()
    try:
        folder = folder.resolve()
    except OSError as exc:
        raise BolError(f"Could not remove {path}: {exc}") from exc
    parts = _managed_parts(folder)
    # One or two components: games/<version>/ or games/<edition>/<version>/.
    # Anything else is GAMES itself, a folder deeper inside a build, or --
    # the one that would really hurt -- games/<edition>/, which holds every
    # build of that edition and has exactly the depth of the pre-Store
    # layout. A delete aimed at any of those is a bug, not a request.
    if (not parts or len(parts) > 2
            or (len(parts) == 1 and xodus.edition(parts[0]))):
        raise BolError(
            f"{folder} is not a Minecraft build this launcher downloaded, so "
            "it will not be removed. Delete it yourself if you are sure.")
    if not folder.is_dir():
        raise BolError(f"There is no build in {folder} to remove.")
    # And it has to look like a build: a game inside it, the record the
    # installer wrote, or the encrypted package a download leaves. Whatever
    # else has found its way in there is not this function's to delete.
    if not (xodus.game_root(folder) or xodus.has_package_cache(folder)
            or (folder / _INSTALL_METADATA).exists()):
        raise BolError(
            f"{folder} holds no Minecraft build, so it will not be removed.")
    if _mc_running():
        raise BolError(
            "Minecraft is running. Close the game, then remove the build.")

    selected = _selected_root()
    freed = _dir_size(folder)
    shutil.rmtree(folder)
    if _holds(folder, selected):
        # The launcher runs the game through this symlink, so it goes with
        # the folder it points into.
        try:
            if CONTENT.is_symlink():
                CONTENT.unlink()
        except OSError:
            pass
        settings = load_settings()
        settings.pop("game_dir", None)
        save_settings(settings)
    return freed


def mc_version_str(game_dir: Path):
    for nm in ("appxmanifest.xml", "AppxManifest.xml"):
        man = game_dir / nm
        if man.exists():
            m = re.search(r'Identity[^>]*Version="(\d+)\.(\d+)\.(\d+)\.\d+"',
                          man.read_text(errors="ignore"))
            if m:
                p = m.group(3)
                # Bedrock packs "<minor><patch>" into the Appx 3rd field, e.g.
                # 2004 -> "20.4", 3005 -> "30.5", 0301 -> "3.1" — split it back
                # so the result matches Mojang's version numbers (e.g. 1.26.20.4).
                if len(p) >= 3:
                    return f"{m.group(1)}.{m.group(2)}.{int(p[:2])}.{int(p[2:])}"
                return f"{m.group(1)}.{m.group(2)}.{int(p)}"
    return None


def _vt(v):
    try:
        return tuple(int(x) for x in v.split("."))
    except Exception:
        return (0,)


def _auto_selection(s):
    """The (edition, version) to install when the caller named neither.

    An unset or no-longer-listed version falls through to the newest build,
    which is what install_game() does with it.
    """
    want = (s.get("mc_edition") or "").strip()
    chosen = xodus.edition(want) if want else None
    if chosen is None:
        editions = list_editions(include_beta=False) or list_editions(True)
        if not editions:
            die("No Minecraft edition is available.")
        chosen = editions[0]
    return chosen, (s.get("mc_version") or "").strip() or None
