"""bol.betterrtx — BetterRTX presets, installed into a Minecraft build (#166).

BetterRTX replaces three of the game's own RenderDragon materials -- the ray
tracing stub and two post-processing passes -- under data/renderer/materials.
On Windows its installer copies them over the originals; a Store build here is
a plain folder, those files are not encrypted, and nothing checks them, so the
same three copies are all it takes. What the installer does not do, and what
this does first, is make sure the preset was compiled for this game:

A material is a RenderDragon.CompiledMaterialDefinition with a format version
in its header, and the game only loads its own. bedrock.graphics publishes
each preset with the version it was built against ("materialVersion": 25 for
the presets made for 1.26.40), and 1.26.52's own files say 26. Copying a 25
over a 26 is a ray tracing mode that no longer renders, so a preset is only
offered, and only installed, when its version is the build's.

The originals are kept beside the build, under .bedrock-on-linux-betterrtx/,
before anything is written, and "restore" puts them back. A game that is
running has the files open and is not written to.
"""
# SPDX-License-Identifier: MIT

import json
import shutil
import struct
import tempfile
import time
import zipfile
from pathlib import Path

from .log import BolError, info, ok
from .util import download, http_json, load_settings

API = "https://bedrock.graphics/api"

MATERIALS = {
    "stub": "RTXStub.material.bin",
    "tonemapping": "RTXPostFX.Tonemapping.material.bin",
    "bloom": "RTXPostFX.Bloom.material.bin",
}
MATERIALS_DIR = Path("data", "renderer", "materials")
STATE_DIR = ".bedrock-on-linux-betterrtx"
_STATE = "state.json"
_ORIGINALS = "original"

# u64 magic, then the definition's name as a u32-length string, then a u64
# format version.
_MAGIC = 0x0A11DA1A
_DEFINITION = b"RenderDragon.CompiledMaterialDefinition"


def material_version(data):
    """The format version in a compiled material's header, or None."""
    try:
        if struct.unpack_from("<Q", data, 0)[0] != _MAGIC:
            return None
        length = struct.unpack_from("<I", data, 8)[0]
        if data[12:12 + length] != _DEFINITION:
            return None
        return struct.unpack_from("<Q", data, 12 + length)[0]
    except struct.error:
        return None


def _game_dir(game_dir=None):
    if game_dir:
        return Path(game_dir)
    game = str(load_settings().get("game_dir") or "")
    if not game or not Path(game, "Minecraft.Windows.exe").is_file():
        raise BolError("No Minecraft version is installed yet — pick one "
                       "and press PLAY once first.")
    return Path(game)


def _state_dir(game):
    return Path(game) / STATE_DIR


def _read_state(game):
    try:
        state = json.loads((_state_dir(game) / _STATE).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def game_material_version(game_dir=None):
    """The material format the build loads, from its own original stub."""
    game = _game_dir(game_dir)
    for candidate in (_state_dir(game) / _ORIGINALS / MATERIALS["stub"],
                      game / MATERIALS_DIR / MATERIALS["stub"]):
        try:
            data = candidate.read_bytes()
        except OSError:
            continue
        version = material_version(data[:256])
        if version is not None:
            return version
    return None


def list_presets(fetch=None):
    """The presets bedrock.graphics publishes, as plain dicts.

    Entries without the three https links are dropped. Older ones announce no
    material version; theirs is read from the downloaded files instead,
    before anything is written.
    """
    try:
        payload = (fetch or http_json)(API)
    except Exception as exc:
        raise BolError(f"Could not reach bedrock.graphics for the BetterRTX "
                       f"presets ({exc}).") from exc
    presets = []
    for entry in payload if isinstance(payload, list) else []:
        if not isinstance(entry, dict):
            continue
        urls = {key: str(entry.get(key) or "") for key in MATERIALS}
        if not all(url.startswith("https://") for url in urls.values()):
            continue
        try:
            version = int(entry.get("materialVersion"))
        except (TypeError, ValueError):
            version = None
        presets.append({
            "uuid": str(entry.get("uuid") or entry.get("slug") or ""),
            "name": str(entry.get("name") or entry.get("uuid") or "preset"),
            "game_version": str(entry.get("gameVersion") or "").lstrip("v"),
            "material_version": version,
            "urls": urls,
        })
    return [preset for preset in presets if preset["uuid"]]


def _require_closed():
    from .prefix import _mc_running
    if _mc_running():
        raise BolError("Close Minecraft first: the game keeps its shader "
                       "files open while it runs.")


def _check_versions(files, game_version, label):
    for name, data in files.items():
        version = material_version(data[:256])
        if version is None:
            raise BolError(f"{label}: {name} is not a compiled Minecraft "
                           "material.")
        if game_version is not None and version != game_version:
            raise BolError(
                f"{label} is made for another Minecraft version: its "
                f"materials are format {version}, and this Minecraft loads "
                f"format {game_version}. Installing it would leave the Ray "
                "Traced mode without shaders. Pick a preset made for this "
                "version, or play a Minecraft version it was made for.")


def _install(files, game, label, uuid=None):
    """Write ``files`` (name -> bytes) over the build's materials."""
    _require_closed()
    materials = game / MATERIALS_DIR
    state_dir = _state_dir(game)
    originals = state_dir / _ORIGINALS
    if _read_state(game) is None:
        # What is in place now is the game's own: keep it before anything
        # else is written, so "restore" always has the real files to give.
        tmp = Path(tempfile.mkdtemp(prefix=".original-", dir=game))
        try:
            for name in MATERIALS.values():
                source = materials / name
                if source.is_file():
                    shutil.copy2(source, tmp / name)
            shutil.rmtree(originals, ignore_errors=True)
            state_dir.mkdir(exist_ok=True)
            tmp.replace(originals)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    for name, data in files.items():
        target = materials / name
        part = target.with_name(name + ".bol-part")
        part.write_bytes(data)
        part.replace(target)
    (state_dir / _STATE).write_text(json.dumps(
        {"name": label, "uuid": uuid, "files": sorted(files),
         "installed": int(time.time())}, indent=2) + "\n", encoding="utf-8")
    ok(f"BetterRTX: {label} installed. Use the Ray Traced graphics mode in a "
       "ray tracing world to see it.")


def install_preset(preset, game_dir=None, downloader=None):
    """Download one bedrock.graphics preset and install it."""
    game = _game_dir(game_dir)
    game_version = game_material_version(game)
    announced = preset["material_version"]
    if None not in (game_version, announced) and announced != game_version:
        raise BolError(
            f"{preset['name']} is made for Minecraft "
            f"{preset['game_version'] or 'another version'} (materials format "
            f"{preset['material_version']}); this Minecraft loads format "
            f"{game_version}. Installing it would leave the Ray Traced mode "
            "without shaders.")
    fetch = downloader or download
    files = {}
    with tempfile.TemporaryDirectory() as tmp:
        for key, name in MATERIALS.items():
            dest = Path(tmp) / name
            fetch(preset["urls"][key], dest, label=name)
            files[name] = dest.read_bytes()
    _check_versions(files, game_version, preset["name"])
    _install(files, game, preset["name"], uuid=preset["uuid"])
    return game


def install_rtpack(path, game_dir=None):
    """Install a .rtpack: a zip holding some of the three materials."""
    pack = Path(path).expanduser()
    game = _game_dir(game_dir)
    files = {}
    try:
        with zipfile.ZipFile(pack) as archive:
            for member in archive.infolist():
                name = Path(member.filename).name
                if name in MATERIALS.values() and not member.is_dir():
                    files[name] = archive.read(member)
    except (OSError, zipfile.BadZipFile) as exc:
        raise BolError(f"{pack.name} is not a BetterRTX pack: {exc}") from exc
    if not files:
        raise BolError(f"{pack.name} holds none of the materials BetterRTX "
                       "replaces.")
    _check_versions(files, game_material_version(game), pack.stem)
    _install(files, game, pack.stem)
    return game


def restore(game_dir=None):
    """Put the build's own materials back; False when nothing was changed."""
    game = _game_dir(game_dir)
    state = _read_state(game)
    if state is None:
        return False
    _require_closed()
    originals = _state_dir(game) / _ORIGINALS
    materials = game / MATERIALS_DIR
    for name in MATERIALS.values():
        source = originals / name
        if source.is_file():
            part = materials / (name + ".bol-part")
            shutil.copy2(source, part)
            part.replace(materials / name)
    shutil.rmtree(_state_dir(game), ignore_errors=True)
    ok("BetterRTX removed: Minecraft's own ray tracing shaders are back.")
    return True


def status(game_dir=None):
    """What is installed in the build, and which material format it loads."""
    game = _game_dir(game_dir)
    state = _read_state(game) or {}
    return {"installed": state.get("name"), "uuid": state.get("uuid"),
            "material_version": game_material_version(game)}


def cli(action, target=None):
    """`bedrock-on-linux betterrtx …`."""
    if action == "status":
        current = status()
        info(f"Materials format of this Minecraft: "
             f"{current['material_version'] or 'unknown'}")
        info(f"BetterRTX: {current['installed'] or 'not installed'}")
    elif action == "list":
        version = game_material_version()
        presets = list_presets()
        usable = [p for p in presets if p["material_version"] == version]
        for preset in presets:
            mark = "*" if preset in usable else " "
            made_for = (f"Minecraft {preset['game_version']}, format "
                        f"{preset['material_version']}"
                        if preset["material_version"] is not None
                        else "format checked on install")
            print(f" {mark} {preset['uuid']:<40} {preset['name']} "
                  f"({made_for})")
        if not usable:
            info(f"No preset is made for this Minecraft (materials format "
                 f"{version}) yet; * marks the ones that are.")
    elif action == "install":
        if target and Path(target).suffix.lower() in (".rtpack", ".zip"):
            install_rtpack(target)
            return
        preset = next((p for p in list_presets() if p["uuid"] == target), None)
        if preset is None:
            raise BolError(f"No BetterRTX preset is called {target!r}; "
                           "`bedrock-on-linux betterrtx list` shows them.")
        install_preset(preset)
    elif action == "restore":
        if not restore():
            info("BetterRTX is not installed in this Minecraft version.")
