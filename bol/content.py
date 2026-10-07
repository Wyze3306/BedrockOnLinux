"""bol.content — import of .mcpack/.mcworld/.mcaddon/.mcskin content."""
# SPDX-License-Identifier: MIT

import json
import os
import re
import shutil
import zipfile
from pathlib import Path, PurePosixPath

from .log import die, ok, warn
from .perfcheck import find_options_file
from .prefix import _mc_running, active_prefix, in_wine_user

# Minecraft Bedrock normally imports these by "opening" the file, which has no
# handler under Wine — so worlds/packs can't be imported in-game. We unpack
# them straight into the game's com.mojang folders instead.
COM_MOJANG_REL = ("drive_c/users/steamuser/AppData/Roaming/Minecraft Bedrock/"
                  "Users/Shared/games/com.mojang")

# A prefix holds one com.mojang per account the player has signed in with,
# plus the Users/Shared one above for playing signed out. Packs are shared
# between accounts, but worlds, world templates and skins belong to whoever
# is signed in, and the game only ever reads those from that account's own
# folder. Unpacking them into Users/Shared therefore imports them somewhere a
# signed-in game never looks, which is why an imported .mctemplate never
# reached the template list (#188).
_PER_ACCOUNT_SUBS = frozenset({
    "custom_skins", "minecraftWorlds", "world_templates",
})


def _mojang_dir(prefix=None):
    root = prefix or active_prefix()
    return root / in_wine_user(COM_MOJANG_REL, root)


def _active_mojang_dir(prefix=None):
    """The com.mojang folder the game itself used last, or None.

    Minecraft keeps its settings beside the content of the account it is
    signed in as, so the most recently written options.txt marks the profile
    whose worlds and templates the player is actually shown. None means the
    game has never run here, which is the normal state before the first
    launch and leaves nothing to prefer over the shared folder.
    """
    options = find_options_file(prefix or active_prefix())
    if options is None:
        return None
    base = options.parent.parent
    return base if base.name == "com.mojang" else None


def _content_dir(sub, prefix=None):
    """The com.mojang folder the game reads *sub* from."""
    if sub in _PER_ACCOUNT_SUBS:
        active = _active_mojang_dir(prefix)
        if active is not None:
            return active
    return _mojang_dir(prefix)


def game_content_dir(prefix=None):
    """The com.mojang folder to show the player.

    Their own account's folder, since that is where the worlds, templates and
    screenshots they are looking for live; the shared one only until the game
    has run once.
    """
    return _active_mojang_dir(prefix) or _mojang_dir(prefix)


def _report_shared_leftovers(sub, prefix=None):
    """Name what an earlier import left in the folder the game skips.

    Everything used to be unpacked into Users/Shared, so an install that
    imported worlds or templates before this fix still has them there,
    invisible to a signed-in game. Say where they are rather than moving
    them: the same folder holds the real saves of anyone who plays signed
    out, and those are not ours to relocate.
    """
    shared = _mojang_dir(prefix)
    if _content_dir(sub, prefix) == shared:
        return
    try:
        stale = sorted(p.name for p in (shared / sub).iterdir() if p.is_dir())
    except OSError:
        return
    if stale:
        warn(f"{shared / sub} also holds {', '.join(stale)}. The game only "
             "reads that folder when nobody is signed in — import those "
             "files again if they never showed up in-game.")


def _safe_component(name):
    """A filesystem-safe folder name derived from a pack/world name."""
    name = re.sub(r"[^\w .()\-]+", "_", (name or "").strip()) or "imported"
    return name[:96]


def _unique_path(p: Path):
    if not p.exists():
        return p
    for i in range(2, 1000):
        cand = p.with_name(f"{p.name} ({i})")
        if not cand.exists():
            return cand
    return p.with_name(f"{p.name} ({os.getpid()})")


def _pack_subfolder(manifest):
    """The com.mojang subfolder a pack belongs in, from its manifest modules."""
    types = set()
    try:
        for m in manifest.get("modules", []):
            t = (m.get("type") or "").lower()
            if t:
                types.add(t)
    except Exception:
        pass
    if types & {"skin_pack", "skins"}:
        return "skin_packs"
    if types & {"data", "script", "client_data"}:
        return "behavior_packs"
    if types & {"world_template"}:
        return "world_templates"
    return "resource_packs"


def _install_pack_tree(pack_root: Path, prefix, fallback_name: str):
    """Move one extracted pack (dir containing manifest.json) into com.mojang."""
    manifest = {}
    mf = pack_root / "manifest.json"
    if mf.exists():
        try:
            manifest = json.loads(mf.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            manifest = {}
    sub = _pack_subfolder(manifest)
    try:
        nm = manifest.get("header", {}).get("name") or fallback_name
    except Exception:
        nm = fallback_name
    dest = _unique_path(_content_dir(sub, prefix) / sub
                        / _safe_component(str(nm)))
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(pack_root), str(dest))
    return sub, dest


def import_content(src, prefix=None):
    """Import a .mcpack/.mcaddon/.mcworld/.mctemplate/.mcskin into the game.

    Returns a list of human-readable result strings.
    """
    src = Path(src).expanduser()
    if not src.is_file():
        die(f"File not found: {src}")
    if not zipfile.is_zipfile(src):
        die(f"Not a Minecraft content file (not a zip): {src.name}")
    ext = src.suffix.lower()
    stem = src.stem
    results = []

    if ext in (".mcworld", ".mctemplate"):
        sub = "minecraftWorlds" if ext == ".mcworld" else "world_templates"
        dest = _unique_path(_content_dir(sub, prefix) / sub
                            / _safe_component(stem))
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(src) as z:
            for m in z.infolist():
                p = PurePosixPath(m.filename)
                if p.is_absolute() or ".." in p.parts:
                    raise ValueError(f"unsafe path in zip archive: {m.filename}")
            z.extractall(dest)
        kind = "world" if ext == ".mcworld" else "world template"
        results.append(f"{kind}: {dest.name}")
        ok(f"Imported {kind} → {dest}")
        _report_shared_leftovers(sub, prefix)
        return results

    tmp = _mojang_dir(prefix) / ".bol-import-tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(src) as z:
            for m in z.infolist():
                p = PurePosixPath(m.filename)
                if p.is_absolute() or ".." in p.parts:
                    raise ValueError(f"unsafe path in zip archive: {m.filename}")
            z.extractall(tmp)
        manifests = sorted(tmp.rglob("manifest.json"),
                           key=lambda p: len(p.parts))
        claimed, roots = [], []
        for mf in manifests:
            r = mf.parent
            if any(str(r).startswith(str(c) + os.sep) for c in claimed):
                continue
            claimed.append(r)
            roots.append(r)
        if not roots:
            die(f"No manifest.json in {src.name} — not a valid pack/addon.")
        for r in roots:
            sub, dest = _install_pack_tree(r, prefix, stem)
            label = sub.rstrip("s").replace("_", " ")
            results.append(f"{label}: {dest.name}")
            ok(f"Imported {label} → {dest}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return results


def cmd_import(paths):
    if not paths:
        die("Usage: bedrock-on-linux import "
            "<file.mcpack|.mcworld|.mcaddon|.mctemplate|.mcskin> …")
    if _mc_running():
        warn("Minecraft appears to be running — close it before importing so "
             "the game picks up new content on next launch.")
    total = []
    for p in paths:
        total += import_content(p)
    ok(f"Done — imported {len(total)} item(s)")
