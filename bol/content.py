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
from .prefix import _mc_running, active_prefix

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
    """Users/Shared of the edition PLAY starts: Preview keeps its own."""
    from .perfcheck import game_data_folder
    rel = COM_MOJANG_REL.replace("/Minecraft Bedrock/",
                                 "/%s/" % game_data_folder(), 1)
    return (prefix or active_prefix()) / rel


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
    # Pack names often carry the game's colour codes (§c, §l): drop them
    # rather than turn each into an underscore.
    name = re.sub(r"§.?", "", name or "")
    name = re.sub(r"[^\w .()\-]+", "_", name.strip()) or "imported"
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


def _lang_value(pack_root: Path, key: str):
    """``key`` from the pack's own translations, English first.

    Many packs name themselves ``pack.name`` in their manifest and give the
    real name in texts/<language>.lang, the way the game expects.
    """
    texts = pack_root / "texts"
    candidates = [texts / "en_US.lang", texts / "en_GB.lang"]
    try:
        candidates += sorted(texts.glob("*.lang"))
    except OSError:
        pass
    for lang in candidates:
        try:
            lines = lang.read_text(encoding="utf-8-sig",
                                   errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            name, sep, value = line.partition("=")
            if sep and name.strip() == key:
                value = value.split("\t#", 1)[0].split("  #", 1)[0].strip()
                if value:
                    return value
    return None


def pack_text(pack_root: Path, value):
    """A manifest name or description, its translation key resolved."""
    value = str(value or "").strip()
    if re.fullmatch(r"[\w.]+\.(name|description)", value):
        return _lang_value(Path(pack_root), value) or ""
    return value


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
        nm = pack_text(pack_root, manifest.get("header", {}).get("name")) \
            or fallback_name
    except Exception:
        nm = fallback_name
    dest = _unique_path(_content_dir(sub, prefix) / sub
                        / _safe_component(str(nm)))
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(pack_root), str(dest))
    return sub, dest


def _extract(archive, dest: Path):
    """Unpack a zip into ``dest``, refusing entries that would leave it."""
    with zipfile.ZipFile(archive) as z:
        for m in z.infolist():
            p = PurePosixPath(m.filename)
            if p.is_absolute() or ".." in p.parts:
                raise ValueError(f"unsafe path in zip archive: {m.filename}")
        z.extractall(dest)


def _hoist_single_folder(dest: Path, marker: str):
    """Lift a world zipped inside one folder of its own up into ``dest``.

    Many .mcworld files are zipped from the folder above the world, so the
    archive holds ``My World/level.dat`` and the game, which looks for
    ``level.dat`` at the top, never lists it.
    """
    if (dest / marker).exists():
        return
    entries = [e for e in dest.iterdir() if e.name != "__MACOSX"]
    if len(entries) != 1 or not entries[0].is_dir() or \
            not (entries[0] / marker).exists():
        return
    inner = entries[0]
    for child in list(inner.iterdir()):
        shutil.move(str(child), str(dest / child.name))
    inner.rmdir()
    shutil.rmtree(dest / "__MACOSX", ignore_errors=True)


_NESTED_PACKS = (".mcpack", ".mcaddon", ".zip")
_NESTED_WORLDS = (".mcworld", ".mctemplate")


def _expand_nested(root: Path, depth=3):
    """Unpack the archives an add-on carries inside itself, in place.

    A .mcaddon is normally a zip of .mcpack files (one behaviour pack, one
    resource pack), not of the packs' folders, so its manifests are only
    found once those are unpacked too. Worlds bundled with an add-on are
    returned to be imported as worlds.
    """
    for _ in range(depth):
        nested = [f for f in root.rglob("*") if f.is_file()
                  and f.suffix.lower() in _NESTED_PACKS
                  and zipfile.is_zipfile(f)]
        if not nested:
            break
        for archive in nested:
            folder = _unique_path(archive.with_name(archive.stem))
            folder.mkdir(parents=True)
            _extract(archive, folder)
            archive.unlink()
    worlds = sorted(f for f in root.rglob("*") if f.is_file()
                    and f.suffix.lower() in _NESTED_WORLDS
                    and zipfile.is_zipfile(f))
    return worlds


_ARCHIVE_KINDS = (
    (b"Rar!\x1a\x07", "RAR"),
    (b"7z\xbc\xaf\x27\x1c", "7-Zip"),
    (b"\x1f\x8b", "gzip"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"BZh", "bzip2"),
)


def _sniff(src: Path):
    """(kind, what to tell the player) for a file that is not a zip."""
    try:
        size = src.stat().st_size
        with open(src, "rb") as stream:
            head = stream.read(512)
    except OSError as exc:
        return "unreadable", f"{src.name} cannot be read ({exc})."
    if size == 0:
        return "empty", (f"{src.name} is empty: its download did not "
                         "finish. Download it again.")
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return "damaged-zip", (
            f"{src.name} is cut short: its download stopped before the end, "
            "or the file was damaged on the way. Download it again.")
    for magic, kind in _ARCHIVE_KINDS:
        if head.startswith(magic):
            return kind, None
    if head[257:262] == b"ustar":
        return "tar", None
    if head.startswith(b"MZ"):
        return "windows", (
            f"{src.name} is a Windows program or DLL, not Minecraft content. "
            "LeviLamina mods go on the Mods page; other client DLLs in "
            "Settings ▸ Tools ▸ Inject.")
    text = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if text.startswith((b"<!doctype", b"<html", b"<head", b"<?xml",
                        b"<meta", b"<script")):
        return "web-page", (
            f"{src.name} is a web page, not the content: the site sent "
            "its download page instead of the file (a link shortener or an "
            "advert page in between). Open the page in your browser, use its "
            "real download button, and import the file that gives.")
    return "unknown", (
        f"{src.name} is not a Minecraft file: .mcpack, .mcaddon, .mcworld "
        "and .mctemplate files are zip archives, and this one is not. If it "
        "came in another archive, extract it first, then import what it "
        "holds (or its folder).")


def _repair_zip(src: Path, work: Path):
    """A readable copy of a zip with something stuck after its end, or None.

    Python only looks for a zip's directory in the last 64 KiB, so a zip a
    download tool padded or appended to reads as "not a zip" although every
    byte of it is there.
    """
    import mmap  # noqa: PLC0415
    try:
        with open(src, "rb") as stream, \
                mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
            end = data.rfind(b"PK\x05\x06")
            if end < 0 or end + 22 > len(data):
                return None
            comment = int.from_bytes(data[end + 20:end + 22], "little")
            fixed = work / ("repaired" + src.suffix.lower())
            with open(fixed, "wb") as out:
                out.write(data[:min(len(data), end + 22 + comment)])
    except (OSError, ValueError):
        return None
    return fixed if zipfile.is_zipfile(fixed) else None


def _extract_with_tool(src: Path, kind: str, dest: Path):
    """Unpack a RAR/7-Zip/tar archive with whatever the system has.

    Returns False when no tool for it is installed. Links are removed
    afterwards: no world or pack needs one, and one could point outside.
    """
    import subprocess  # noqa: PLC0415
    tools = [("bsdtar", ["-xf", str(src), "-C", str(dest)])]
    if kind in ("RAR", "7-Zip"):
        for name in ("7z", "7zz", "7za"):
            tools.append((name, ["x", "-y", f"-o{dest}", str(src)]))
    if kind == "RAR":
        tools.append(("unrar", ["x", "-o+", "-y", str(src), str(dest) + "/"]))
    if kind in ("tar", "gzip", "xz", "bzip2"):
        tools.append(("tar", ["-xf", str(src), "-C", str(dest),
                              "--no-same-owner"]))
    for name, arguments in tools:
        exe = shutil.which(name)
        if not exe:
            continue
        result = subprocess.run([exe, *arguments], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, check=False)
        if result.returncode == 0:
            for path in sorted(dest.rglob("*"), reverse=True):
                if path.is_symlink():
                    path.unlink()
            return True
        die(f"{src.name} could not be unpacked ({kind}): "
            + (result.stderr.decode("utf-8", "replace").strip()
               .splitlines() or ["unknown error"])[-1])
    return False


def _is_levilamina_mod(manifest_path: Path):
    """A LeviLamina mod's manifest: name, entry and type, no pack header."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig",
                                                      errors="ignore"))
    except (OSError, ValueError):
        return False
    return isinstance(manifest, dict) and "header" not in manifest and \
        "entry" in manifest


def _shallowest(root: Path, name: str):
    found = [p for p in root.rglob(name) if p.is_file()
             and "__MACOSX" not in p.parts]
    return min(found, key=lambda p: len(p.parts)) if found else None


def _is_template(world: Path):
    try:
        manifest = json.loads((world / "manifest.json").read_text(
            encoding="utf-8", errors="ignore"))
    except (OSError, ValueError):
        return False
    return isinstance(manifest, dict) and \
        _pack_subfolder(manifest) == "world_templates"


def _install_world(root: Path, name: str, template: bool, prefix,
                   destinations):
    sub = "world_templates" if template else "minecraftWorlds"
    dest = _unique_path(_content_dir(sub, prefix) / sub
                        / _safe_component(name))
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(root), str(dest))
    shutil.rmtree(dest / "__MACOSX", ignore_errors=True)
    kind = "world template" if template else "world"
    if destinations is not None:
        destinations.append(dest)
    ok(f"Imported {kind} → {dest}")
    _report_shared_leftovers(sub, prefix)
    return f"{kind}: {dest.name}"


def _import_unpacked(tmp: Path, name: str, ext: str, prefix, destinations):
    """Import what was unpacked into ``tmp`` (ours to move things out of)."""
    level = (tmp / "level.dat") if (tmp / "level.dat").is_file() else None
    if level is None and ext in (".mcworld", ".mctemplate"):
        # Zipped from the folder above the world, or deeper still.
        level = _shallowest(tmp, "level.dat")
    if level is None:
        _hoist_single_folder(tmp, "level.dat")
        if (tmp / "level.dat").is_file():
            level = tmp / "level.dat"
    if level is not None:
        world = level.parent
        template = ext == ".mctemplate" or (ext != ".mcworld" and
                                            _is_template(world))
        return [_install_world(world, name, template, prefix, destinations)]
    if ext in (".mcworld", ".mctemplate"):
        die(f"{name}{ext} has no level.dat: it is not a world.")

    results = []
    worlds = _expand_nested(tmp)
    for world in worlds:
        results += import_content(world, prefix, destinations)
    # World folders packed as they are (a .zip of a map, a folder of maps).
    world_dirs = []
    for level in sorted(tmp.rglob("level.dat"), key=lambda p: len(p.parts)):
        folder = level.parent
        if "__MACOSX" in folder.parts or any(
                folder == d or d in folder.parents for d in world_dirs):
            continue
        world_dirs.append(folder)
    manifests = sorted(tmp.rglob("manifest.json"), key=lambda p: len(p.parts))
    claimed, roots, mods = [], [], []
    for mf in manifests:
        r = mf.parent
        if "__MACOSX" in r.parts:
            continue
        # A world's own packs travel with the world.
        if any(r == d or d in r.parents for d in world_dirs):
            continue
        if any(str(r).startswith(str(c) + os.sep) for c in claimed):
            continue
        claimed.append(r)
        if _is_levilamina_mod(mf):
            mods.append(r)
            continue
        roots.append(r)
    if not roots and not worlds and not world_dirs:
        if mods:
            die(f"{name}{ext} is a LeviLamina mod, not an add-on: install it "
                "on the Mods page (Installed ▸ Import mod…).")
        if any(tmp.rglob("*.jar")):
            die(f"{name}{ext} holds Java Edition mods (.jar files). They only "
                "run on Minecraft: Java Edition, not on Bedrock.")
        die(f"{name}{ext} holds no Minecraft pack or world: no manifest.json "
            "and no level.dat. It may be a mod for another program (Java "
            "Edition, LeviLamina: see the Mods page) or need extracting "
            "first.")
    for folder in world_dirs:
        label = folder.name if folder != tmp else name
        results.append(_install_world(folder, label, _is_template(folder),
                                      prefix, destinations))
    for r in roots:
        sub, dest = _install_pack_tree(r, prefix, name)
        if destinations is not None:
            destinations.append(dest)
        label = sub.rstrip("s").replace("_", " ")
        results.append(f"{label}: {dest.name}")
        ok(f"Imported {label} → {dest}")
    return results


def import_content(src, prefix=None, destinations=None):
    """Import a .mcpack/.mcaddon/.mcworld/.mctemplate/.mcskin into the game.

    Also takes a .zip of any of those, an unpacked pack or world folder, and
    archives the system can unpack (RAR, 7-Zip, tar). Returns a list of
    human-readable result strings. A list passed as ``destinations``
    receives the folder each item was unpacked into, for a caller that wants
    to take them out again (bol.catalog).
    """
    import tempfile  # noqa: PLC0415
    src = Path(src).expanduser()
    if not src.exists():
        die(f"File not found: {src}")
    from .uwp import is_package_file  # noqa: PLC0415 - rarely needed
    if src.is_file() and is_package_file(src):
        # A build of the game, not content for it: a UWP package the player
        # kept (#387), installed beside the builds downloaded here.
        from .gamesetup import import_uwp_package  # noqa: PLC0415 - cycle
        dest = import_uwp_package(src)
        return [f"Minecraft build: Minecraft for Windows (UWP) {dest.name}"]
    ext = "" if src.is_dir() else src.suffix.lower()
    name = src.name if src.is_dir() else src.stem
    base = _mojang_dir(prefix)
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".bol-import-", dir=base))
    try:
        tmp = work / "content"
        if src.is_dir():
            shutil.copytree(src, tmp, symlinks=False)
        elif zipfile.is_zipfile(src):
            tmp.mkdir()
            _extract(src, tmp)
        else:
            kind, problem = _sniff(src)
            repaired = _repair_zip(src, work) if kind == "damaged-zip" \
                else None
            if repaired is not None:
                warn(f"{src.name} had extra data after its end; imported "
                     "what the archive holds.")
                tmp.mkdir()
                _extract(repaired, tmp)
            elif problem is None:
                tmp.mkdir()
                if not _extract_with_tool(src, kind, tmp):
                    die(f"{src.name} is a {kind} archive, not a Minecraft "
                        "file. Extract it with your archive manager, then "
                        "import what it holds (or its folder).")
            else:
                die(problem)
        return _import_unpacked(tmp, name, ext, prefix, destinations)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def cmd_import(paths):
    if not paths:
        die("Usage: bedrock-on-linux import "
            "<file.mcpack|.mcworld|.mcaddon|.mctemplate|.mcskin|.zip|folder"
            "|.appx> …")
    if _mc_running():
        warn("Minecraft appears to be running — close it before importing so "
             "the game picks up new content on next launch.")
    total = []
    for p in paths:
        total += import_content(p)
    ok(f"Done — imported {len(total)} item(s)")
