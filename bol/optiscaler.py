"""OptiScaler integration for BedrockOnLinux.

This module installs a reviewed OptiScaler v10 nightly payload and applies the
Linux/Wine compatibility setup needed for Minecraft RTX to expose its DLSS
input path on non-NVIDIA GPUs.

The critical Minecraft-specific workaround is deliberately local-only:
Minecraft's own ``nvngx_dlss.dll`` is copied to ``nvngx.dll`` in the selected
game directory. BedrockOnLinux never redistributes NVIDIA NGX binaries.
"""
# SPDX-License-Identifier: MIT

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import CACHE, CONTENT, DATA
from .log import BolError, info, ok, warn
from .util import download

# Tested on Minecraft 1.26.51.1 + WineGDK/vkd3d-proton on an RX 7600 XT.
# Keep this pinned: silently moving a proxy DLL that is loaded into the game is
# not an acceptable update strategy. Update the tag, asset and SHA together
# after testing a newer OptiScaler build.
OPTISCALER_TAG = "nightly-20260916"
OPTISCALER_VERSION = "v10.0.0-pre1_20260916"
OPTISCALER_ASSET = "OptiScaler_v10.0.0-pre1_20260916.7z"
OPTISCALER_URL = (
    "https://github.com/optiscaler/OptiScaler-nightly/releases/download/"
    f"{OPTISCALER_TAG}/{OPTISCALER_ASSET}"
)
OPTISCALER_SHA256 = "339b5282a410747b41e9069a3a346068539ec317cc5b7f27269c3f3996c6700a"

ROOT = DATA / "optiscaler"
PAYLOAD = ROOT / "payload"
STATE = ROOT / "state.json"
ARCHIVE = CACHE / OPTISCALER_ASSET
MARKER = ".bedrock-on-linux-optiscaler.json"

_DXVK_BEGIN = "# BEGIN BedrockOnLinux OptiScaler"
_DXVK_END = "# END BedrockOnLinux OptiScaler"
_DXVK_BLOCK = "\n".join((
    _DXVK_BEGIN,
    "# Minecraft RTX DLSS capability spoof for OptiScaler under Wine/VKD3D.",
    "dxgi.customVendorId = 10de",
    "dxgi.hideAmdGpu = True",
    "dxgi.hideNvidiaGpu = False",
    "dxgi.customDeviceId = 2684",
    'dxgi.customDeviceDesc = "NVIDIA GeForce RTX 4090"',
    _DXVK_END,
))

_SPOOF_VALUES = {
    "SpoofedVendorId": "0x10de",
    "SpoofedDeviceId": "0x2684",
    "SpoofedGPUName": "NVIDIA GeForce RTX 4090",
    "Dxgi": "true",
    "Registry": "true",
    "User32": "true",
}
_INPUT_VALUES = {"EnableDlssInputs": "true"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_state() -> dict:
    try:
        value = json.loads(STATE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_state(**changes) -> dict:
    ROOT.mkdir(parents=True, exist_ok=True)
    state = _read_state()
    state.update(changes)
    fd, name = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=ROOT)
    staged = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, STATE)
    finally:
        staged.unlink(missing_ok=True)
    return state


def _env_truth(value: str | None) -> bool | None:
    if value is None or not str(value).strip():
        return None
    return str(value).strip().lower() not in {"0", "no", "off", "false"}


def enabled(environ=None) -> bool:
    source = os.environ if environ is None else environ
    override = _env_truth(source.get("BOL_OPTISCALER"))
    if override is not None:
        return override
    return bool(_read_state().get("enabled", False))


def _merge_dll_override(current: str, wanted: str) -> str:
    """Append a Wine DLL override without discarding the launcher's own set."""
    parts = [item.strip() for item in str(current or "").split(";") if item.strip()]
    wanted_name = wanted.split("=", 1)[0].lower().removesuffix(".dll")
    kept = []
    for item in parts:
        name = item.split("=", 1)[0].strip().lower().removesuffix(".dll")
        if name != wanted_name:
            kept.append(item)
    kept.append(wanted)
    return ";".join(kept)


def apply_environment(environ=None) -> dict:
    """Apply the Wine/dxvk-nvapi environment required by the tested setup."""
    env = os.environ if environ is None else environ
    env["WINEDLLOVERRIDES"] = _merge_dll_override(
        env.get("WINEDLLOVERRIDES", ""), "dxgi.dll=n,b"
    )
    env.setdefault("DXVK_NVAPI_ALLOW_OTHER_DRIVERS", "1")
    env.setdefault("DXVK_NVAPI_GPU_ARCH", "AD100")
    return env


def _set_ini_section_values(text: str, section: str, values: dict[str, str]) -> str:
    """Change only named keys in one INI section, preserving OptiScaler comments."""
    lines = text.splitlines(keepends=True)
    header_re = re.compile(r"^\s*\[([^]]+)\]\s*(?:[;#].*)?(?:\r?\n)?$")
    key_re = re.compile(r"^(\s*)([^=;#\s]+)(\s*=).*(\r?\n)?$")
    in_section = False
    found_section = False
    seen = set()
    insert_at = len(lines)

    for idx, line in enumerate(lines):
        header = header_re.match(line)
        if header:
            name = header.group(1).strip().lower()
            if in_section:
                insert_at = idx
                break
            in_section = name == section.lower()
            found_section = found_section or in_section
            continue
        if not in_section:
            continue
        match = key_re.match(line)
        if not match:
            continue
        key = match.group(2)
        if key in values:
            newline = match.group(4) or ("\n" if line.endswith("\n") else "")
            lines[idx] = f"{match.group(1)}{key}{match.group(3)}{values[key]}{newline}"
            seen.add(key)
    else:
        if in_section:
            insert_at = len(lines)

    missing = [key for key in values if key not in seen]
    if not found_section:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += "\n"
        lines.extend([f"\n[{section}]\n"] + [f"{k}={values[k]}\n" for k in missing])
    elif missing:
        lines[insert_at:insert_at] = [f"{k}={values[k]}\n" for k in missing]
    return "".join(lines)


def _patch_ini(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    text = _set_ini_section_values(text, "Spoofing", _SPOOF_VALUES)
    text = _set_ini_section_values(text, "Inputs", _INPUT_VALUES)
    path.write_text(text, encoding="utf-8")


def _write_dxvk_conf(game_dir: Path) -> None:
    path = game_dir / "dxvk.conf"
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError as exc:
        raise BolError(f"Could not read {path}: {exc}") from exc
    block_re = re.compile(
        rf"(?ms)^\s*{re.escape(_DXVK_BEGIN)}\n.*?^\s*{re.escape(_DXVK_END)}\s*\n?"
    )
    cleaned = block_re.sub("", text).rstrip()
    new_text = (cleaned + "\n\n" if cleaned else "") + _DXVK_BLOCK + "\n"
    try:
        path.write_text(new_text, encoding="utf-8")
    except OSError as exc:
        raise BolError(f"Could not write {path}: {exc}") from exc


def _remove_dxvk_conf_block(game_dir: Path) -> None:
    path = game_dir / "dxvk.conf"
    if not path.exists():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    block_re = re.compile(
        rf"(?ms)^\s*{re.escape(_DXVK_BEGIN)}\n.*?^\s*{re.escape(_DXVK_END)}\s*\n?"
    )
    cleaned = block_re.sub("", text).strip()
    try:
        if cleaned:
            path.write_text(cleaned + "\n", encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _payload_root(extracted: Path) -> Path:
    """Return the directory containing OptiScaler.dll after extraction."""
    direct = extracted / "OptiScaler.dll"
    if direct.is_file():
        return extracted
    matches = list(extracted.rglob("OptiScaler.dll"))
    if len(matches) != 1:
        raise BolError(
            "The OptiScaler archive did not contain exactly one OptiScaler.dll."
        )
    return matches[0].parent


def _validate_payload(path: Path) -> None:
    required = (
        path / "OptiScaler.dll",
        path / "OptiScaler.ini",
        path / "OptiScaler" / "amd_fidelityfx_framegeneration_dx12.dll",
    )
    missing = [item.name for item in required if not item.is_file()]
    if missing:
        raise BolError(
            "OptiScaler payload is incomplete; missing: " + ", ".join(missing)
        )


def _extract_archive(archive: Path, destination: Path) -> None:
    sevenzip = next((shutil.which(name) for name in ("7zz", "7z", "7za")
                     if shutil.which(name)), None)
    if sevenzip:
        result = subprocess.run(
            [sevenzip, "x", "-y", f"-o{destination}", str(archive)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if result.returncode != 0:
            raise BolError(
                "Could not extract OptiScaler with 7-Zip:\n" + result.stdout[-2000:]
            )
        return
    try:
        import py7zr  # type: ignore
    except ImportError as exc:
        raise BolError(
            "Installing OptiScaler needs 7-Zip (7zz/7z/7za) or the optional "
            "Python package py7zr. Install one of them and retry."
        ) from exc
    with py7zr.SevenZipFile(archive, mode="r") as bundle:
        bundle.extractall(path=destination)


def _install_payload_from(source: Path) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="optiscaler-", dir=ROOT) as tmp:
        staged = Path(tmp)
        if source.is_dir():
            root = _payload_root(source)
            shutil.copytree(root, staged / "payload", dirs_exist_ok=True)
        else:
            extracted = staged / "extract"
            extracted.mkdir()
            _extract_archive(source, extracted)
            root = _payload_root(extracted)
            shutil.copytree(root, staged / "payload", dirs_exist_ok=True)
        candidate = staged / "payload"
        _validate_payload(candidate)
        _patch_ini(candidate / "OptiScaler.ini")
        previous = ROOT / "payload.old"
        if previous.exists():
            shutil.rmtree(previous)
        if PAYLOAD.exists():
            PAYLOAD.replace(previous)
        candidate.replace(PAYLOAD)
        shutil.rmtree(previous, ignore_errors=True)


def sync_to_game(game_dir=None) -> Path:
    """Install the prepared proxy into the active Minecraft build."""
    _validate_payload(PAYLOAD)
    game = Path(game_dir or CONTENT).resolve()
    if not (game / "Minecraft.Windows.exe").is_file():
        raise BolError("No active Minecraft build is installed yet.")

    marker_path = game / MARKER
    try:
        marker_before = json.loads(marker_path.read_text(encoding="utf-8"))
        if not isinstance(marker_before, dict):
            marker_before = {}
    except (OSError, ValueError, TypeError):
        marker_before = {}

    proxy = game / "dxgi.dll"
    payload_proxy_hash = _sha256(PAYLOAD / "OptiScaler.dll")
    if proxy.is_file():
        current_proxy_hash = _sha256(proxy)
        managed_proxy_hash = marker_before.get("proxy_sha256")
        if current_proxy_hash not in {payload_proxy_hash, managed_proxy_hash}:
            raise BolError(
                f"{proxy.name} already exists and is not the managed OptiScaler proxy. "
                "Remove/rename the existing DXGI proxy before enabling the integration."
            )

    # v10 keeps backends under OptiScaler/ and the proxy/config at the game root.
    shutil.copy2(PAYLOAD / "OptiScaler.dll", proxy)
    game_ini = game / "OptiScaler.ini"
    if not game_ini.is_file():
        shutil.copy2(PAYLOAD / "OptiScaler.ini", game_ini)
    # Preserve choices made in the overlay (upscaler/FG/HUDFix), but make sure
    # the capability-spoof settings that make Minecraft expose DLSS stay set.
    _patch_ini(game_ini)
    shutil.copytree(PAYLOAD / "OptiScaler", game / "OptiScaler", dirs_exist_ok=True)

    # Minecraft-specific discovery gate: on Wine the in-game Upscaling switch
    # stays enabled-but-grey until this physical nvngx.dll exists. Copy from the
    # game's own NGX binary; never download or redistribute it ourselves.
    dlss = game / "nvngx_dlss.dll"
    if not dlss.is_file():
        raise BolError(
            f"{dlss.name} is missing from this Minecraft build; OptiScaler "
            "cannot unlock the game's DLSS input path."
        )
    ngx = game / "nvngx.dll"
    dlss_hash = _sha256(dlss)
    if ngx.is_file():
        current_ngx_hash = _sha256(ngx)
        managed_ngx_hash = marker_before.get("ngx_sha256")
        if current_ngx_hash not in {dlss_hash, managed_ngx_hash}:
            raise BolError(
                "nvngx.dll already exists and is not the managed Minecraft NGX copy; "
                "refusing to overwrite an unmanaged NGX proxy."
            )
    shutil.copy2(dlss, ngx)
    _write_dxvk_conf(game)

    marker = {
        "managed_by": "BedrockOnLinux",
        "optiscaler": OPTISCALER_VERSION,
        "proxy_sha256": _sha256(game / "dxgi.dll"),
        "ngx_sha256": _sha256(game / "nvngx.dll"),
    }
    marker_path.write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
    return game


def _safe_unlink_if_hash(path: Path, expected: str | None) -> None:
    if not expected or not path.is_file():
        return
    try:
        if _sha256(path) == expected:
            path.unlink()
    except OSError:
        return


def detach_from_game(game_dir=None) -> None:
    game = Path(game_dir or CONTENT).resolve()
    marker_path = game / MARKER
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        marker = {}
    _safe_unlink_if_hash(game / "dxgi.dll", marker.get("proxy_sha256"))
    _safe_unlink_if_hash(game / "nvngx.dll", marker.get("ngx_sha256"))
    _remove_dxvk_conf_block(game)
    marker_path.unlink(missing_ok=True)


def install(source=None) -> Path:
    """Install the pinned payload (or a user-supplied archive/directory) and enable it."""
    if source is None:
        CACHE.mkdir(parents=True, exist_ok=True)
        if not ARCHIVE.is_file() or _sha256(ARCHIVE) != OPTISCALER_SHA256:
            ARCHIVE.unlink(missing_ok=True)
            info(f"Downloading tested OptiScaler {OPTISCALER_VERSION} …")
            download(OPTISCALER_URL, ARCHIVE, label="OptiScaler")
        actual = _sha256(ARCHIVE)
        if actual != OPTISCALER_SHA256:
            ARCHIVE.unlink(missing_ok=True)
            raise BolError(
                "OptiScaler download failed its SHA-256 check "
                f"({actual}); expected {OPTISCALER_SHA256}."
            )
        source_path = ARCHIVE
    else:
        source_path = Path(source).expanduser().resolve()
        if not source_path.exists():
            raise BolError(f"OptiScaler source not found: {source_path}")

    _install_payload_from(source_path)
    _write_state(enabled=True, version=OPTISCALER_VERSION)
    apply_environment()
    game = sync_to_game()
    ok(f"OptiScaler {OPTISCALER_VERSION} is enabled for {game.name}.")
    info("Open OptiScaler with Insert. Frame-generation output stays off until "
         "you explicitly enable Active in the OptiScaler overlay.")
    return game


def enable() -> Path:
    if not PAYLOAD.is_dir():
        return install()
    _write_state(enabled=True, version=OPTISCALER_VERSION)
    apply_environment()
    game = sync_to_game()
    ok(f"OptiScaler enabled for {game.name}.")
    return game


def disable() -> None:
    _write_state(enabled=False)
    try:
        detach_from_game()
    except Exception as exc:
        warn(f"OptiScaler was disabled, but its game-directory cleanup failed ({exc}).")
    ok("OptiScaler disabled.")


def uninstall() -> None:
    disable()
    shutil.rmtree(PAYLOAD, ignore_errors=True)
    ARCHIVE.unlink(missing_ok=True)
    try:
        STATE.unlink(missing_ok=True)
        ROOT.rmdir()
    except OSError:
        pass
    ok("OptiScaler payload removed.")


def status() -> dict:
    game = Path(CONTENT).resolve()
    state = _read_state()
    result = {
        "enabled": enabled(),
        "payload": PAYLOAD.is_dir(),
        "version": state.get("version"),
        "game": str(game),
        "attached": (game / MARKER).is_file(),
        "nvngx": (game / "nvngx.dll").is_file(),
        "proxy": (game / "dxgi.dll").is_file(),
    }
    print(f"OptiScaler enabled: {'yes' if result['enabled'] else 'no'}")
    print(f"Payload installed: {'yes' if result['payload'] else 'no'}"
          + (f" ({result['version']})" if result['version'] else ""))
    print(f"Active game: {result['game']}")
    print(f"Proxy attached: {'yes' if result['attached'] else 'no'}")
    print(f"Physical nvngx.dll: {'yes' if result['nvngx'] else 'no'}")
    return result


def bootstrap(environ=None) -> None:
    """Cheap startup hook used by both launcher entry points.

    No download happens here. Once the user has installed OptiScaler, every
    launcher start reapplies the tested environment and syncs the payload to
    the currently selected Minecraft build, so switching game versions only
    needs a launcher restart.
    """
    if not enabled(environ):
        return
    apply_environment(os.environ if environ is None else environ)
    if not PAYLOAD.is_dir():
        warn("OptiScaler is enabled but its payload is missing; run "
             "'bedrock-on-linux optiscaler install'.")
        return
    try:
        if (Path(CONTENT).resolve() / "Minecraft.Windows.exe").is_file():
            sync_to_game()
    except Exception as exc:
        warn(f"OptiScaler could not be prepared for this Minecraft build ({exc}).")


def cli_main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="bedrock-on-linux optiscaler",
        description="Install and manage the tested OptiScaler RTX/FG integration.",
    )
    sub = parser.add_subparsers(dest="command")
    install_parser = sub.add_parser(
        "install", help="download the tested OptiScaler build and enable it"
    )
    install_parser.add_argument(
        "--source", metavar="PATH",
        help="use an already extracted OptiScaler directory or .7z archive",
    )
    sub.add_parser("enable", help="enable and re-sync the installed payload")
    sub.add_parser("disable", help="disable the proxy for the active game")
    sub.add_parser("uninstall", help="disable and remove the cached payload")
    sub.add_parser("status", help="show OptiScaler integration status")

    args = parser.parse_args(argv)
    try:
        if args.command == "install":
            install(args.source)
        elif args.command == "enable":
            enable()
        elif args.command == "disable":
            disable()
        elif args.command == "uninstall":
            uninstall()
        elif args.command == "status":
            status()
        else:
            parser.print_help()
    except BolError as exc:
        parser.error(str(exc))
    return 0
