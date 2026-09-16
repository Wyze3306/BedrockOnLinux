"""Validate and activate the universal EXT+NV DGC vkd3d build."""
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
import os
import shutil
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .config import (
    WINEGDK_SOURCE_COMMIT,
    WINEGDK_SOURCE_MANIFEST_SHA256,
)
from .engine_lock import managed_engine_lock
from .log import BolError
from .util import sha256_file


EXT_DGC = "ext-dgc"
NV_DGC = "nv-dgc"
VARIANTS = (EXT_DGC, NV_DGC)
ARCHES = ("x86_64-windows", "i386-windows")
DLL_NAMES = ("d3d12.dll", "d3d12core.dll")

DXVK_COMPONENT_VERSION = "3.0.1"
VKD3D_COMPAT_COMPONENT_VERSION = "3.0.1-bol-dgc"
VKD3D_EXT_DGC_COMPONENT_VERSION = VKD3D_COMPAT_COMPONENT_VERSION
VKD3D_NV_DGC_COMPONENT_VERSION = VKD3D_COMPAT_COMPONENT_VERSION
REQUIRED_COMPONENTS = {
    "dxvk": DXVK_COMPONENT_VERSION,
    "vkd3d_ext_dgc": VKD3D_EXT_DGC_COMPONENT_VERSION,
    "vkd3d_nv_dgc": VKD3D_NV_DGC_COMPONENT_VERSION,
}

# native5 through native13 kept the reviewed r11/r12 universal graphics
# payload; native14 rebuilds it with the occluded frame-latency fix, which
# changes d3d12core.dll on both architectures and leaves d3d12.dll untouched.
# Hashing whatever an archive declares would only prove internal consistency,
# so pin the known DLL bytes as an independent trust anchor. Future engine
# revisions must update the revision pin deliberately.
# native15 and native16 differ from native14 only in Wine's ntdll (the
# mapped-fd main-image loader, then path support for it), native17 differs
# from native16 only in Wine's Wayland driver, and native18 from native17 only
# in xgameruntime's XSystem interface revisions, so the vkd3d payload — and
# therefore every variant hash below — is unchanged; only the revision this
# launcher accepts moves.
REQUIRED_VARIANT_HASHES_BUILD_REV = "wow64-archs-native18"
REQUIRED_ENGINE_GLIBC_MAX = "2.31"
REQUIRED_VKD3D_BASE_COMMIT = "3b10bd7a7ec6a7347e616cf8bea59333afec2255"
REQUIRED_VKD3D_REVERT = "76c11d2e2b90b0a46dc894508e67e2aaacc2c04d"
REQUIRED_GDK_PROTON_BASE_REPOSITORY = "Weather-OS/GDK-Proton"
REQUIRED_GDK_PROTON_BASE_RELEASE = "release10-32"
REQUIRED_GDK_PROTON_BASE_ARCHIVE = "GDK-Proton10-32.tar.gz"
REQUIRED_GDK_PROTON_BASE_ARCHIVE_SHA256 = (
    "1e80f4e714f877f42101d5775bd38ca0a15a38d304e24af1f15c6deec4ebac2d"
)
REQUIRED_WINEGDK_SOURCE_MANIFEST_PATH = (
    "files/share/bedrock-on-linux/licenses-and-provenance/winegdk/"
    "native5/SOURCE-SHA256SUMS"
)
XGAMERUNTIME_THREADING_PATH = (
    "files/lib/wine/x86_64-windows/xgameruntime.dll.threading"
)
XGAMERUNTIME_THREADING_SHA256 = (
    "4c3e3faf5bcd86779e4a69677902dd8b36a16e1136a5901e616c36d8a02b68f6"
)
REQUIRED_PINNED_CRITICAL_HASHES = {
    XGAMERUNTIME_THREADING_PATH: XGAMERUNTIME_THREADING_SHA256,
}
REQUIRED_CRITICAL_FILE_PATHS = (
    "proton",
    "files/bin/wine",
    "files/bin/wine-preloader",
    "files/bin/wine64",
    "files/bin/wine64-preloader",
    "files/bin/wineserver",
    "files/bin-wow64/wine",
    "files/bin-wow64/wine-preloader",
    "files/bin-wow64/wineserver",
    "files/bin-wow64/msidb",
    "files/share/wine/wine.inf",
    "files/lib/x86_64-linux-gnu/libunwind.so.8",
    "files/lib/wine/x86_64-windows/ntdll.dll",
    "files/lib/wine/x86_64-windows/combase.dll",
    "files/lib/wine/x86_64-windows/user32.dll",
    "files/lib/wine/x86_64-windows/xgameruntime.dll",
    "files/lib/wine/x86_64-windows/windows.storage.dll",
    "files/lib/wine/x86_64-windows/windows.storage.applicationdata.dll",
    XGAMERUNTIME_THREADING_PATH,
    "files/lib/wine/i386-windows/ntdll.dll",
    "files/lib/wine/i386-windows/combase.dll",
    "files/lib/wine/i386-windows/xgameruntime.dll",
    "files/lib/wine/i386-windows/windows.storage.dll",
    "files/lib/wine/i386-windows/windows.storage.applicationdata.dll",
    "files/lib/wine/x86_64-unix/ntdll.so",
)
REQUIRED_EXECUTABLE_FILE_PATHS = (
    "proton",
    "files/bin/wine",
    "files/bin/wine-preloader",
    "files/bin/wine64",
    "files/bin/wine64-preloader",
    "files/bin/wineserver",
    "files/bin-wow64/wine",
    "files/bin-wow64/wine-preloader",
    "files/bin-wow64/wineserver",
    "files/bin-wow64/msidb",
)
REQUIRED_RUNTIME_LINK_TARGETS = {
    "files/bin/wine-preloader": "wine",
    "files/bin/wine64": "wine",
    "files/bin/wine64-preloader": "wine",
    "files/bin-wow64/wine-preloader": "wine",
    "files/bin-wow64/msidb": "wine",
    "files/lib/x86_64-linux-gnu/libunwind.so.8": "libunwind.so.8.0.1",
}
REQUIRED_VARIANT_HASHES = {
    EXT_DGC: {
        ("files/lib/wine/vkd3d-proton/x86_64-windows/"
         "d3d12.dll.bol-ext-dgc"):
            "d6f4e30131e5f05f9167f93c2277652b7d2299ef43a4f623990ee4fd8a34cc4c",
        ("files/lib/wine/vkd3d-proton/x86_64-windows/"
         "d3d12core.dll.bol-ext-dgc"):
            "09cc495a96dd4861b1fd75c3ce1e6a5efc21c8bccc1f1ac8a4b134dd63f14b2b",
        ("files/lib/wine/vkd3d-proton/i386-windows/"
         "d3d12.dll.bol-ext-dgc"):
            "aac73e3555cb3ca893b3ef11ab033e42a0ffa3139e3ac4ae8ded1667cd321190",
        ("files/lib/wine/vkd3d-proton/i386-windows/"
         "d3d12core.dll.bol-ext-dgc"):
            "b0ef89b9c147b35d751154186cd5a4e249cb2cdbbbafe9f0a47757f6c3fd0fd7",
    },
    NV_DGC: {
        ("files/lib/wine/vkd3d-proton/x86_64-windows/"
         "d3d12.dll.bol-nv-dgc"):
            "d6f4e30131e5f05f9167f93c2277652b7d2299ef43a4f623990ee4fd8a34cc4c",
        ("files/lib/wine/vkd3d-proton/x86_64-windows/"
         "d3d12core.dll.bol-nv-dgc"):
            "09cc495a96dd4861b1fd75c3ce1e6a5efc21c8bccc1f1ac8a4b134dd63f14b2b",
        ("files/lib/wine/vkd3d-proton/i386-windows/"
         "d3d12.dll.bol-nv-dgc"):
            "aac73e3555cb3ca893b3ef11ab033e42a0ffa3139e3ac4ae8ded1667cd321190",
        ("files/lib/wine/vkd3d-proton/i386-windows/"
         "d3d12core.dll.bol-nv-dgc"):
            "b0ef89b9c147b35d751154186cd5a4e249cb2cdbbbafe9f0a47757f6c3fd0fd7",
    },
}

@dataclass(frozen=True)
class ValidatedEngineManifest:
    """A schema-checked manifest whose file hashes have all been verified."""

    path: Path
    build_rev: str
    components: Mapping[str, str]
    critical_files: Mapping[str, str]
    variants: Mapping[str, Mapping[str, str]]


def _normalise_variant(value: str, source: str = "variant") -> str:
    variant = (value or "").strip().lower()
    if variant not in VARIANTS:
        raise BolError("Invalid %s '%s'; expected '%s' or '%s'." %
                       (source, value, EXT_DGC, NV_DGC))
    return variant


def _variant_relative_paths(variant: str) -> Tuple[str, ...]:
    variant = _normalise_variant(variant)
    return tuple(
        "files/lib/wine/vkd3d-proton/%s/%s.bol-%s" %
        (arch, dll, variant)
        for arch in ARCHES for dll in DLL_NAMES
    )


def _nv_variant_missing(detail: str) -> BolError:
    return BolError(
        "This GPU requires the 'nv-dgc' vkd3d variant because it exposes "
        "VK_NV_device_generated_commands without "
        "VK_EXT_device_generated_commands, but the installed engine has no "
        "complete, verified nv-dgc variant (%s). Reinstall or update the game "
        "engine." % detail)


def _safe_manifest_file(root: Path, relative: str, variant: str,
                        required_variant: Optional[str]) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise BolError("Invalid path in engine manifest for %s: %r" %
                       (variant, relative))
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != relative:
        raise BolError("Unsafe path in engine manifest for %s: %s" %
                       (variant, relative))
    candidate = root.joinpath(*pure.parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve())
    except (OSError, ValueError) as exc:
        if variant == NV_DGC and required_variant == NV_DGC:
            raise _nv_variant_missing("missing file %s" % relative) from exc
        raise BolError("Engine manifest file is missing or escapes the engine: "
                       "%s" % relative) from exc
    if not resolved.is_file():
        if variant == NV_DGC and required_variant == NV_DGC:
            raise _nv_variant_missing("missing file %s" % relative)
        raise BolError("Engine manifest entry is not a file: %s" % relative)
    return resolved


def _valid_sha256(value) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _validated_distribution_files(
        root: Path, section: Mapping[str, object],
        label: str) -> Dict[str, str]:
    """Validate one provenance distribution-file map.

    Content hashing is performed through the mandatory ``critical_files``
    entry below, after both declarations have been checked for agreement.
    """

    raw_files = section.get("distribution_files")
    if not isinstance(raw_files, dict) or not raw_files:
        raise BolError(
            "Invalid engine manifest: %s.distribution_files must be a "
            "non-empty object." % label)

    validated: Dict[str, str] = {}
    for relative, expected_hash in raw_files.items():
        if not _valid_sha256(expected_hash):
            raise BolError(
                "Invalid SHA-256 in engine manifest for %s: %s" %
                (label, relative))
        expected_hash = expected_hash.lower()
        _safe_manifest_file(root, relative,
                            label + ".distribution_files", None)
        validated[relative] = expected_hash
    return validated


def validate_engine_manifest(
        engine_root: Path, build_rev: str,
        required_variant: Optional[str] = None,
        enforce_pins: bool = False) -> ValidatedEngineManifest:
    """Validate schema 1 and every variant payload hash.

    Manifest file keys are safe POSIX paths relative to ``engine_root`` and
    values are hexadecimal SHA-256 digests. ``critical_files`` authenticates
    the launcher, Wine runtime, patched DLLs and embedded provenance, while
    both variants describe the four universal vkd3d DLL slots used by Proton.
    """

    root = Path(engine_root)
    if enforce_pins and build_rev != REQUIRED_VARIANT_HASHES_BUILD_REV:
        raise BolError(
            "No compiled engine hash allow-list exists for build revision "
            "'%s'; this launcher only accepts '%s'." %
            (build_rev, REQUIRED_VARIANT_HASHES_BUILD_REV))
    if required_variant is not None:
        required_variant = _normalise_variant(required_variant)
    path = root / "engine-manifest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        if required_variant == NV_DGC:
            raise _nv_variant_missing("engine-manifest.json is missing") from exc
        raise BolError("Game engine manifest is missing: %s" % path) from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BolError("Game engine manifest is unreadable: %s" % exc) from exc

    if not isinstance(payload, dict):
        raise BolError("Invalid engine manifest: root must be an object.")
    schema = payload.get("schema")
    if isinstance(schema, bool) or schema != 1:
        raise BolError("Unsupported engine manifest schema %r; expected 1." %
                       schema)
    manifest_rev = payload.get("build_rev")
    if not isinstance(manifest_rev, str) or not manifest_rev:
        raise BolError("Invalid engine manifest: build_rev is missing.")
    if manifest_rev != build_rev:
        raise BolError("Game engine revision mismatch: manifest has '%s', "
                       "launcher expects '%s'." % (manifest_rev, build_rev))
    raw_variants = payload.get("variants")
    if not isinstance(raw_variants, dict):
        raise BolError("Invalid engine manifest: variants must be an object.")
    components = payload.get("components")
    if not isinstance(components, dict):
        raise BolError("Invalid engine manifest: components must be an object.")
    for component, expected_version in REQUIRED_COMPONENTS.items():
        actual_version = components.get(component)
        if actual_version != expected_version:
            raise BolError(
                "Game engine component mismatch for %s: manifest has %r, "
                "expected '%s'." %
                (component, actual_version, expected_version))

    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        raise BolError("Invalid engine manifest: provenance must be an object.")
    winegdk = provenance.get("winegdk")
    if not isinstance(winegdk, dict) \
            or winegdk.get("commit") != WINEGDK_SOURCE_COMMIT:
        actual = winegdk.get("commit") if isinstance(winegdk, dict) else None
        raise BolError(
            "Game engine WineGDK source mismatch: manifest has %r, expected "
            "'%s'." % (actual, WINEGDK_SOURCE_COMMIT))
    universal = provenance.get("vkd3d_universal")
    if not isinstance(universal, dict) \
            or universal.get("base_commit") != REQUIRED_VKD3D_BASE_COMMIT \
            or universal.get("restored_by_reverting") != REQUIRED_VKD3D_REVERT:
        raise BolError("Game engine vkd3d source provenance does not match the "
                       "reviewed universal DGC build.")
    gdk_proton_base = provenance.get("gdk_proton_base")
    required_gdk_proton_fields = {
        "repository": REQUIRED_GDK_PROTON_BASE_REPOSITORY,
        "release": REQUIRED_GDK_PROTON_BASE_RELEASE,
        "archive": REQUIRED_GDK_PROTON_BASE_ARCHIVE,
        "archive_sha256": REQUIRED_GDK_PROTON_BASE_ARCHIVE_SHA256,
        "threading_dll_sha256": XGAMERUNTIME_THREADING_SHA256,
    }
    if not isinstance(gdk_proton_base, dict) or any(
            gdk_proton_base.get(key) != expected
            for key, expected in required_gdk_proton_fields.items()):
        raise BolError(
            "Game engine GDK-Proton base provenance does not match the "
            "reviewed release10-32 archive.")
    build = provenance.get("build")
    if not isinstance(build, dict) \
            or build.get("glibc_max") != REQUIRED_ENGINE_GLIBC_MAX:
        actual = build.get("glibc_max") if isinstance(build, dict) else None
        raise BolError(
            "Game engine ABI mismatch: manifest glibc_max is %r, expected "
            "'%s'." % (actual, REQUIRED_ENGINE_GLIBC_MAX))

    winegdk_distribution = _validated_distribution_files(
        root, winegdk, "provenance.winegdk")
    actual_source_manifest = winegdk_distribution.get(
        REQUIRED_WINEGDK_SOURCE_MANIFEST_PATH)
    if actual_source_manifest != WINEGDK_SOURCE_MANIFEST_SHA256:
        raise BolError(
            "Game engine WineGDK source-manifest mismatch: manifest has %r, "
            "expected '%s'." %
            (actual_source_manifest, WINEGDK_SOURCE_MANIFEST_SHA256))
    vkd3d_distribution = _validated_distribution_files(
        root, universal, "provenance.vkd3d_universal")
    gdk_proton_distribution = _validated_distribution_files(
        root, gdk_proton_base, "provenance.gdk_proton_base")

    raw_critical_files = payload.get("critical_files")
    if not isinstance(raw_critical_files, dict) or not raw_critical_files:
        raise BolError(
            "Invalid engine manifest: critical_files must be a non-empty "
            "object.")
    required_critical_files = set(REQUIRED_CRITICAL_FILE_PATHS)
    required_critical_files.update(winegdk_distribution)
    required_critical_files.update(vkd3d_distribution)
    required_critical_files.update(gdk_proton_distribution)
    missing_critical_files = required_critical_files.difference(
        raw_critical_files)
    if missing_critical_files:
        raise BolError(
            "Invalid engine manifest: critical_files does not list required "
            "file %s." % sorted(missing_critical_files)[0])

    critical_files: Dict[str, str] = {}
    for relative, expected_hash in raw_critical_files.items():
        if not _valid_sha256(expected_hash):
            raise BolError(
                "Invalid SHA-256 in engine manifest for critical_files: %s" %
                relative)
        expected_hash = expected_hash.lower()
        source = _safe_manifest_file(root, relative, "critical_files", None)
        actual_hash = sha256_file(source)
        if actual_hash != expected_hash:
            raise BolError(
                "Engine manifest validation failed: SHA-256 mismatch for "
                "critical file %s (expected %s, got %s)." %
                (relative, expected_hash, actual_hash))
        critical_files[relative] = expected_hash

    if enforce_pins or manifest_rev == REQUIRED_VARIANT_HASHES_BUILD_REV:
        for relative, pinned_hash in REQUIRED_PINNED_CRITICAL_HASHES.items():
            declared_hash = critical_files.get(relative)
            if declared_hash != pinned_hash:
                raise BolError(
                    "Pinned engine critical-file SHA-256 mismatch for %s: "
                    "manifest has %r, expected %s." %
                    (relative, declared_hash, pinned_hash))

    for relative in REQUIRED_EXECUTABLE_FILE_PATHS:
        candidate = root.joinpath(*PurePosixPath(relative).parts)
        try:
            mode = candidate.stat().st_mode
        except OSError as exc:
            raise BolError(
                "Required engine executable is unavailable: %s" % relative) \
                from exc
        if not (mode & stat.S_IXUSR):
            raise BolError(
                "Required engine file is not executable: %s" % relative)

    for relative, expected_target in REQUIRED_RUNTIME_LINK_TARGETS.items():
        candidate = root.joinpath(*PurePosixPath(relative).parts)
        if not candidate.is_symlink():
            raise BolError(
                "Required engine runtime link is missing: %s" % relative)
        try:
            actual_target = os.readlink(candidate)
        except OSError as exc:
            raise BolError(
                "Required engine runtime link is unreadable: %s" % relative) \
                from exc
        if actual_target != expected_target:
            raise BolError(
                "Required engine runtime link %s targets %r, expected %r." %
                (relative, actual_target, expected_target))

    for provenance_files in (winegdk_distribution, vkd3d_distribution,
                             gdk_proton_distribution):
        for relative, provenance_hash in provenance_files.items():
            if critical_files[relative] != provenance_hash:
                raise BolError(
                    "Game engine provenance SHA-256 mismatch for %s: "
                    "critical_files and distribution_files disagree." %
                    relative)

    validated: Dict[str, Dict[str, str]] = {}
    for variant in VARIANTS:
        raw_variant = raw_variants.get(variant)
        if not isinstance(raw_variant, dict):
            if variant == NV_DGC and required_variant == NV_DGC:
                raise _nv_variant_missing("manifest entry variants.nv-dgc "
                                          "is missing")
            raise BolError("Invalid engine manifest: variants.%s is missing." %
                           variant)
        raw_files = raw_variant.get("files")
        if not isinstance(raw_files, dict) or not raw_files:
            if variant == NV_DGC and required_variant == NV_DGC:
                raise _nv_variant_missing("variants.nv-dgc.files is empty")
            raise BolError("Invalid engine manifest: variants.%s.files must be "
                           "a non-empty object." % variant)
        expected = set(_variant_relative_paths(variant))
        missing = expected.difference(raw_files)
        if missing:
            detail = "manifest does not list %s" % sorted(missing)[0]
            if variant == NV_DGC and required_variant == NV_DGC:
                raise _nv_variant_missing(detail)
            raise BolError("Invalid engine manifest for %s: %s." %
                           (variant, detail))

        if enforce_pins or manifest_rev == REQUIRED_VARIANT_HASHES_BUILD_REV:
            pinned_files = REQUIRED_VARIANT_HASHES[variant]
            # Equality on the four required paths prevents a repackaged or
            # locally substituted DLL set from authenticating itself merely by
            # updating the hashes stored inside the same archive.
            if set(raw_files) != set(pinned_files):
                raise BolError(
                    "Pinned engine file list mismatch for %s: manifest paths do "
                    "not equal the reviewed payload." % variant)
            for relative, pinned_hash in pinned_files.items():
                declared_hash = raw_files.get(relative)
                if not isinstance(declared_hash, str) \
                        or declared_hash.lower() != pinned_hash:
                    raise BolError(
                        "Pinned engine SHA-256 mismatch for %s: manifest has %r, "
                        "expected %s." %
                        (relative, declared_hash, pinned_hash))

        files: Dict[str, str] = {}
        for relative, expected_hash in raw_files.items():
            if not _valid_sha256(expected_hash):
                raise BolError("Invalid SHA-256 in engine manifest for %s: %s" %
                               (variant, relative))
            expected_hash = expected_hash.lower()
            source = _safe_manifest_file(root, relative, variant,
                                         required_variant)
            actual_hash = sha256_file(source)
            if actual_hash != expected_hash:
                detail = "SHA-256 mismatch for %s" % relative
                if variant == NV_DGC and required_variant == NV_DGC:
                    raise _nv_variant_missing(detail)
                raise BolError("Engine manifest validation failed: %s "
                               "(expected %s, got %s)." %
                               (detail, expected_hash, actual_hash))
            files[relative] = expected_hash
        validated[variant] = files

    return ValidatedEngineManifest(path=path, build_rev=manifest_rev,
                                   components=dict(components),
                                   critical_files=critical_files,
                                   variants=validated)


@contextmanager
def _engine_lock(root: Path):
    try:
        # The engine root is renamed during updates; its parent is stable.
        with managed_engine_lock(Path(root).parent):
            yield
    except OSError as exc:
        raise BolError("Could not lock the managed game engine: %s" % exc) \
            from exc


def _copy_file_synced(source: Path, destination: Path):
    mode = stat.S_IMODE(source.stat().st_mode)
    with source.open("rb") as src, destination.open("xb") as dst:
        shutil.copyfileobj(src, dst, length=1 << 20)
        dst.flush()
        os.fsync(dst.fileno())
    os.chmod(destination, mode)


def _fsync_directories(paths: Iterable[Path]):
    for directory in set(paths):
        fd = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            fd = os.open(str(directory), flags)
            os.fsync(fd)
        except OSError:
            # Some network/FUSE filesystems do not support directory fsync.
            pass
        finally:
            if fd is not None:
                os.close(fd)


def _target_for_variant_source(relative: str, variant: str) -> str:
    suffix = ".bol-" + variant
    if not relative.endswith(suffix):
        raise BolError("Invalid %s variant path: %s" % (variant, relative))
    return relative[:-len(suffix)]


def _transactional_activate(
        operations: Sequence[Tuple[Path, Path, str]],
        all_targets: Sequence[Tuple[Path, str]], variant: str):
    transaction = "%d-%s" % (os.getpid(), uuid.uuid4().hex)
    stages: Dict[Path, Path] = {}
    backups: Dict[Path, Optional[Path]] = {}
    replaced: List[Path] = []
    preserve_backups = set()
    directories = [target.parent for _, target, _ in operations]
    try:
        # Prepare and verify every byte before changing the first live DLL.
        for source, target, expected_hash in operations:
            stage_path = target.with_name(
                target.name + ".bol-stage-" + transaction)
            _copy_file_synced(source, stage_path)
            if sha256_file(stage_path) != expected_hash:
                raise OSError("staged SHA-256 mismatch for %s" % target)
            stages[target] = stage_path

            if target.exists() or target.is_symlink():
                backup_path = target.with_name(
                    target.name + ".bol-rollback-" + transaction)
                _copy_file_synced(target, backup_path)
                backups[target] = backup_path
            else:
                backups[target] = None

        for _, target, _ in operations:
            os.replace(stages[target], target)
            replaced.append(target)
        _fsync_directories(directories)

        for target, expected_hash in all_targets:
            if not target.is_file() or sha256_file(target) != expected_hash:
                raise OSError("post-activation SHA-256 mismatch for %s" % target)
    except Exception as exc:
        rollback_errors = []
        for target in reversed(replaced):
            backup = backups.get(target)
            try:
                if backup is None:
                    target.unlink(missing_ok=True)
                elif backup.exists():
                    os.replace(backup, target)
            except OSError as rollback_exc:
                rollback_errors.append("%s: %s" % (target, rollback_exc))
                if backup is not None and backup.exists():
                    preserve_backups.add(backup)
        _fsync_directories(directories)
        detail = "vkd3d %s activation failed: %s" % (variant, exc)
        if rollback_errors:
            detail += "; rollback errors: " + "; ".join(rollback_errors)
        raise BolError(detail) from exc
    finally:
        for temporary in list(stages.values()) + [
                value for value in backups.values() if value is not None]:
            if temporary in preserve_backups:
                continue
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def activate_vkd3d_variant(
        engine_root: Path, build_rev: str, variant: str,
        enforce_pins: bool = True) -> bool:
    """Transactionally activate a verified variant; return whether it changed.

    Each live DLL is replaced with ``os.replace`` from the same directory, and
    any mid-transaction failure restores every DLL already changed.  Calls are
    serialized with an engine-local lock and matching targets make this a no-op.
    """

    root = Path(engine_root)
    variant = _normalise_variant(variant)
    with _engine_lock(root):
        manifest = validate_engine_manifest(
            root, build_rev, required_variant=variant,
            enforce_pins=enforce_pins)
        variant_files = manifest.variants[variant]
        operations = []
        all_targets = []
        for relative in _variant_relative_paths(variant):
            expected_hash = variant_files[relative]
            source = root.joinpath(*PurePosixPath(relative).parts)
            target_relative = _target_for_variant_source(relative, variant)
            target = root.joinpath(*PurePosixPath(target_relative).parts)
            all_targets.append((target, expected_hash))
            try:
                current_hash = sha256_file(target) if target.is_file() else None
            except OSError:
                current_hash = None
            if current_hash != expected_hash:
                operations.append((source, target, expected_hash))

        if not operations:
            return False
        _transactional_activate(operations, all_targets, variant)
        return True


def prepare_universal_vkd3d(
        engine_root: Path, build_rev: str,
        environ: Optional[Mapping[str, str]] = None,
        enforce_pins: bool = True) -> Tuple[str, bool]:
    """Validate/activate the universal payload without opening Vulkan.

    The reviewed r12 EXT and NV slots are byte-identical: the DLL contains both
    implementations and vkd3d-proton selects the usable backend after the game
    opens its real device.  A launcher-side Vulkan probe is therefore not only
    redundant, it is unsafe on a broken kernel driver because the diagnostic
    process becomes an additional GPU client before Minecraft.

    ``BOL_VKD3D_VARIANT`` remains useful as an activation/manifest diagnostic,
    but it selects identical bytes and never triggers hardware enumeration.
    """

    env = os.environ if environ is None else environ
    override = env.get("BOL_VKD3D_VARIANT", "")
    variant = (_normalise_variant(override, "BOL_VKD3D_VARIANT")
               if override.strip() else EXT_DGC)
    changed = activate_vkd3d_variant(
        engine_root, build_rev, variant, enforce_pins=enforce_pins)
    return variant, changed
