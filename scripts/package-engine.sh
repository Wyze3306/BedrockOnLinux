#!/usr/bin/env bash
# Build an unreleased hybrid WineGDK engine candidate without modifying the
# source engine tree.
#
# The candidate stores the same reviewed universal vkd3d-proton build in both
# manifest slots for both Windows architectures. That build contains EXT-DGC
# and restored NV-DGC; vkd3d selects against the real Vulkan device/features.
#
# Usage:
#   scripts/package-engine.sh ENGINE_DIR UNIVERSAL_DGC_BUILD_DIR \
#     WINEGDK_COMMIT GDK_PROTON_BASE_ARCHIVE [WINEGDK_PREFIX]
#
# ENGINE_DIR defaults to:
#   $BOL_HOME/proton/GDK-Proton-xuser
#
# UNIVERSAL_DGC_BUILD_DIR is the output of vkd3d-proton's package-release.sh and must
# contain x64/{d3d12,d3d12core}.dll and x86/{d3d12,d3d12core}.dll.  The build
# used for native5 through native12 is the reviewed r11/r12 v3.0.1 payload with removal
# commit 76c11d2 reverted; only WineGDK changes in these revisions.
# GDK_PROTON_BASE_ARCHIVE must be the reviewed Weather-OS release10-32
# archive. It supplies the native XThreading runtime which WineGDK delegates
# XAsync/XTaskQueue calls to; the WineGDK wrapper cannot replace that DLL.
# WINEGDK_PREFIX, when supplied, is the output of build-winegdk-bullseye.sh.
# It is overlaid only in the isolated staging snapshot, never into ENGINE_DIR.
#
# Output:
#   dist/GDK-Proton-xuser-<WINEGDK_BUILD_REV>.tar.gz
#   dist/GDK-Proton-xuser-<WINEGDK_BUILD_REV>.tar.gz.sha256
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$SRC/dist"
mkdir -p "$OUT"

REV="$(grep -m1 '^WINEGDK_BUILD_REV = ' "$SRC/bol/config.py" | cut -d'"' -f2)"
[[ -n "$REV" ]] || { echo "!! could not read WINEGDK_BUILD_REV" >&2; exit 1; }

BOL_HOME="${BOL_HOME:-$HOME/.local/share/bedrock-on-linux}"
ENGINE_DIR="${1:-$BOL_HOME/proton/GDK-Proton-xuser}"
UNIVERSAL_DGC_BUILD_DIR="${2:-${BOL_VKD3D_UNIVERSAL_BUILD:-}}"
WINEGDK_COMMIT="${3:-${BOL_WINEGDK_SOURCE_COMMIT:-}}"
GDK_PROTON_BASE_ARCHIVE="${4:-${BOL_GDK_PROTON_BASE_ARCHIVE:-}}"
WINEGDK_PREFIX="${5:-${BOL_WINEGDK_PREFIX:-}}"
PINNED_WINEGDK_COMMIT="$(grep -m1 '^WINEGDK_SOURCE_COMMIT = ' \
  "$SRC/bol/config.py" | cut -d'"' -f2)"
PINNED_WINEGDK_SOURCE_MANIFEST_SHA256="$(
  grep -m1 '^WINEGDK_SOURCE_MANIFEST_SHA256 = ' \
    "$SRC/bol/config.py" | cut -d'"' -f2
)"
GDK_PROTON_BASE_REPOSITORY="Weather-OS/GDK-Proton"
GDK_PROTON_BASE_RELEASE="release10-32"
GDK_PROTON_BASE_NAME="GDK-Proton10-32.tar.gz"
GDK_PROTON_BASE_SHA256="1e80f4e714f877f42101d5775bd38ca0a15a38d304e24af1f15c6deec4ebac2d"
GDK_PROTON_THREADING_MEMBER="GDK-Proton10-32/files/lib/wine/x86_64-windows/xgameruntime.dll.threading"
GDK_PROTON_THREADING_REL="files/lib/wine/x86_64-windows/xgameruntime.dll.threading"
GDK_PROTON_THREADING_SHA256="4c3e3faf5bcd86779e4a69677902dd8b36a16e1136a5901e616c36d8a02b68f6"

if [[ ! -f "$ENGINE_DIR/proton" || ! -d "$ENGINE_DIR/files" ]]; then
  echo "!! '$ENGINE_DIR' is not a built engine (need ./proton and ./files)." >&2
  exit 1
fi
if [[ -z "$UNIVERSAL_DGC_BUILD_DIR" \
      || ! -d "$UNIVERSAL_DGC_BUILD_DIR/x64" \
      || ! -d "$UNIVERSAL_DGC_BUILD_DIR/x86" ]]; then
  echo "!! pass the reviewed vkd3d-proton 3.0.1 universal DGC build directory." >&2
  echo "   expected x64/ and x86/ below: ${UNIVERSAL_DGC_BUILD_DIR:-<unset>}" >&2
  exit 1
fi
if [[ -z "$WINEGDK_COMMIT" || "$WINEGDK_COMMIT" != "$PINNED_WINEGDK_COMMIT" ]]; then
  echo "!! WineGDK source commit must be the reviewed pin:" >&2
  echo "   $PINNED_WINEGDK_COMMIT" >&2
  exit 1
fi
if [[ -z "$GDK_PROTON_BASE_ARCHIVE" || ! -f "$GDK_PROTON_BASE_ARCHIVE" \
      || -L "$GDK_PROTON_BASE_ARCHIVE" ]]; then
  echo "!! pass the reviewed $GDK_PROTON_BASE_NAME as the fourth argument." >&2
  exit 1
fi
if [[ -n "$WINEGDK_PREFIX" ]]; then
  if [[ ! -d "$WINEGDK_PREFIX" || -L "$WINEGDK_PREFIX" \
        || ! -x "$WINEGDK_PREFIX/bin/wine" \
        || ! -x "$WINEGDK_PREFIX/bin/wineserver" \
        || ! -f "$WINEGDK_PREFIX/lib/wine/x86_64-windows/xgameruntime.dll" \
        || ! -f "$WINEGDK_PREFIX/lib/wine/i386-windows/xgameruntime.dll" \
        || ! -f "$WINEGDK_PREFIX/.bol-winegdk-build.env" \
        || ! -f "$WINEGDK_PREFIX/.bol-winegdk-package-versions.tsv" ]]; then
    echo "!! the fifth argument is not a complete WineGDK build prefix:" >&2
    echo "   $WINEGDK_PREFIX" >&2
    exit 1
  fi
  if [[ -e "$WINEGDK_PREFIX/lib/wine/i386-unix" \
        || -L "$WINEGDK_PREFIX/lib/wine/i386-unix" ]]; then
    echo "!! the WineGDK prefix contains an unexpected i386 Unix runtime:" >&2
    echo "   $WINEGDK_PREFIX/lib/wine/i386-unix" >&2
    exit 1
  fi
  WINEGDK_PREFIX="$(cd "$WINEGDK_PREFIX" && pwd -P)"
fi

for tool in cmp cp find flock grep gzip install objdump python3 readelf readlink \
  sha256sum strings tar; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "!! required packaging tool is missing: $tool" >&2
    exit 1
  }
done

read -r gdk_proton_base_actual _ < <(sha256sum "$GDK_PROTON_BASE_ARCHIVE")
if [[ "$gdk_proton_base_actual" != "$GDK_PROTON_BASE_SHA256" ]]; then
  echo "!! GDK-Proton base archive SHA-256 mismatch:" >&2
  echo "   got $gdk_proton_base_actual, expected $GDK_PROTON_BASE_SHA256" >&2
  exit 1
fi

ASSET="GDK-Proton-xuser-${REV}.tar.gz"
TARBALL="$OUT/$ASSET"
CHECKSUM="$TARBALL.sha256"
STAGE="$(mktemp -d "$OUT/.engine-stage.XXXXXX")"
STAGED_ENGINE="$STAGE/GDK-Proton-xuser"
PART="$TARBALL.part"

cleanup() {
  rm -rf "$STAGE"
  rm -f "$PART"
}
trap cleanup EXIT

echo "== Packaging unreleased hybrid engine candidate '$REV'"
echo "   source: $ENGINE_DIR"
echo "   universal DGC build: $UNIVERSAL_DGC_BUILD_DIR"
echo "   WineGDK source: $WINEGDK_COMMIT"
if [[ -n "$WINEGDK_PREFIX" ]]; then
  echo "   WineGDK prefix overlay: $WINEGDK_PREFIX"
fi
echo "   source size: $(du -sh "$ENGINE_DIR" | cut -f1)"

# The application uses this same stable parent lock when replacing the managed
# engine or switching its live vkd3d DLLs.  Hold it through staging and tar so
# a low-space hard-link snapshot cannot observe half of a concurrent update.
ENGINE_PARENT="$(cd "$ENGINE_DIR/.." && pwd -P)"
ENGINE_LOCK="$ENGINE_PARENT/.bol-engine.lock"
exec {ENGINE_LOCK_FD}>>"$ENGINE_LOCK"
flock "$ENGINE_LOCK_FD"
if [[ ! -f "$ENGINE_DIR/proton" || ! -d "$ENGINE_DIR/files" ]]; then
  echo "!! engine changed before its packaging lock was acquired: $ENGINE_DIR" >&2
  exit 1
fi

# Prefer an independent copy-on-write snapshot: it costs almost no additional
# disk on filesystems that support reflinks and cannot be mutated through the
# source inode.  On filesystems without reflinks, use hard links under the
# stable engine lock rather than doubling this multi-gigabyte tree.  A normal
# copy remains the cross-filesystem fallback.
mkdir -p "$STAGED_ENGINE"
if cp -a --reflink=always "$ENGINE_DIR/." "$STAGED_ENGINE/" 2>/dev/null; then
  echo "   staging: independent copy-on-write snapshot"
else
  rm -rf "$STAGED_ENGINE"
  mkdir -p "$STAGED_ENGINE"
  if cp -al "$ENGINE_DIR/." "$STAGED_ENGINE/" 2>/dev/null; then
    echo "   staging: hard-link snapshot protected by $ENGINE_LOCK"
  else
    rm -rf "$STAGED_ENGINE"
    mkdir -p "$STAGED_ENGINE"
    cp -a "$ENGINE_DIR/." "$STAGED_ENGINE/"
    echo "   staging: independent copy (links unavailable)"
  fi
fi

# A freshly built WineGDK prefix can be combined with a known-good Proton base
# without modifying that base. --remove-destination is essential for the
# low-space hard-link snapshot: replacing an existing inode prevents cp from
# truncating a file that is still linked to ENGINE_DIR.
if [[ -n "$WINEGDK_PREFIX" ]]; then
  cp -a --remove-destination "$WINEGDK_PREFIX/." "$STAGED_ENGINE/files/"
  echo "   staged reviewed WineGDK prefix overlay"
  # The public GE-Proton base ships real Wine-10 wine64 + *-preloader binaries in
  # files/bin; the WineGDK overlay only replaces `wine` (Wine 11 wow64 drops the
  # separate 64-bit loader and preloaders). Recreate the wow64 launch aliases as
  # symlinks to the staged WineGDK `wine`, so files/bin runs one coherent Wine
  # runtime and matches the reviewed runtime-link layout.
  for alias in wine64 wine-preloader wine64-preloader; do
    rm -f "$STAGED_ENGINE/files/bin/$alias"
    ln -s wine "$STAGED_ENGINE/files/bin/$alias"
  done
  echo "   reconciled files/bin wow64 launch aliases"
fi

# The combined i386+x86_64 WineGDK build uses the new pure-WoW64 layout:
# 32-bit PE DLLs are handled by the 64-bit Unix runtime.  The GE-Proton base
# still contains Wine 10's i386 Unix modules; retaining them would mix two
# Wine ABIs and require host i386 libraries on otherwise 64-bit-only systems.
# Keep this cleanup unconditional so an already-overlaid input cannot re-add it.
rm -rf "$STAGED_ENGINE/files/lib/wine/i386-unix"
[[ ! -e "$STAGED_ENGINE/files/lib/wine/i386-unix" \
    && ! -L "$STAGED_ENGINE/files/lib/wine/i386-unix" ]] || {
  echo "!! stale Wine 10 i386 Unix runtime survived staging" >&2
  exit 1
}
echo "   removed stale Wine 10 i386 Unix runtime"

# The overlay above only replaces the modules the WineGDK build produced, so a
# module Wine's configure dropped does not leave a hole here: the GE-Proton
# base's own module survives in its place, a Wine major version behind
# everything around it. That is how the base's Wine 10 winewayland.drv shipped
# beside our Wine 11 win32u.so, importing entry points Wine has since removed
# and failing to load on every host (issue #180). Ask the same question the
# launcher asks before it honours BOL_INPUT=wayland, so a candidate cannot
# disagree with the check that has to defend users from it.
PYTHONPATH="$SRC" python3 - "$STAGED_ENGINE" <<'PY'
import sys
from pathlib import Path

from bol.waylanddrv import engine_wayland_driver_problem

problem = engine_wayland_driver_problem(Path(sys.argv[1]))
if problem:
    raise SystemExit("!! " + problem)
print("   Wayland driver belongs to the staged Wine build")
PY

# Wine's installed headers are build-time material, not part of the Proton
# runtime. Besides adding about 75 MiB, generated headers embed the builder's
# absolute source path. Remove them from the locked snapshot so the candidate
# is relocatable and does not leak maintainer-specific paths. With a hard-link
# snapshot this only unlinks staging entries; ENGINE_DIR remains untouched.
rm -rf "$STAGED_ENGINE/files/include"

# GE-Proton release archives may carry maintainer bytecode caches (including
# embedded build paths). They are neither runtime inputs nor reproducible
# source, so never propagate them into a BedrockOnLinux engine candidate.
find "$STAGED_ENGINE" -type f \( -name '*.pyc' -o -name '.DS_Store' \) -delete
find "$STAGED_ENGINE" -depth -type d -name '__pycache__' -empty -delete

# GE-Proton's inherited bin-wow64 directory contains a Wine 10 wineserver,
# while the WineGDK overlay in files/bin is Wine 11.1. Proton selects
# bin-wow64 whenever wow64 mode is enabled, and mixing those servers produces
# the exact runtime-revision mismatch this candidate is meant to eliminate.
# Recreate the complete multicall directory from the staged WineGDK runtime;
# removing the staged directory only unlinks hard-link snapshot entries and
# never mutates ENGINE_DIR.
rm -rf "$STAGED_ENGINE/files/bin-wow64"
install -d -m755 "$STAGED_ENGINE/files/bin-wow64"
cp -a "$STAGED_ENGINE/files/bin/wine" \
  "$STAGED_ENGINE/files/bin-wow64/wine"
cp -a "$STAGED_ENGINE/files/bin/wineserver" \
  "$STAGED_ENGINE/files/bin-wow64/wineserver"
ln -s wine "$STAGED_ENGINE/files/bin-wow64/wine-preloader"
ln -s wine "$STAGED_ENGINE/files/bin-wow64/msidb"
chmod 755 "$STAGED_ENGINE/files/bin-wow64/wine" \
  "$STAGED_ENGINE/files/bin-wow64/wineserver"
cmp -s "$STAGED_ENGINE/files/bin/wine" \
  "$STAGED_ENGINE/files/bin-wow64/wine" || {
  echo "!! staged bin-wow64 wine differs from WineGDK runtime" >&2; exit 1; }
cmp -s "$STAGED_ENGINE/files/bin/wineserver" \
  "$STAGED_ENGINE/files/bin-wow64/wineserver" || {
  echo "!! staged bin-wow64 wineserver differs from WineGDK runtime" >&2; exit 1; }
[[ "$(readlink "$STAGED_ENGINE/files/bin-wow64/wine-preloader")" == wine \
   && "$(readlink "$STAGED_ENGINE/files/bin-wow64/msidb")" == wine ]] || {
  echo "!! staged bin-wow64 launch aliases are inconsistent" >&2; exit 1; }
echo "   coherent WineGDK bin-wow64 runtime verified"

VKD3D_ROOT="files/lib/wine/vkd3d-proton"
ARCHES=(x86_64-windows i386-windows)
DLLS=(d3d12.dll d3d12core.dll)
EXT_MARKER="VK_EXT_device_generated_commands"
NV_MARKER="VK_NV_device_generated_commands"
# DXVK is swapped to stock v3.0.1 by scripts/apply-dxvk-301.sh before packaging
# (the public GDK-Proton base ships 2.7.1; upstream's engine used 3.0.1). This
# validates the swapped-in version.
DXVK_VERSION="3.0.1"
VKD3D_BASE_VERSION="3.0.1"
VKD3D_EXT_VERSION="3.0.1-bol-dgc"
VKD3D_NV_VERSION="3.0.1-bol-dgc"
VKD3D_NV_SOURCE="3b10bd7a7ec6a7347e616cf8bea59333afec2255"
VKD3D_NV_REVERT="76c11d2e2b90b0a46dc894508e67e2aaacc2c04d"
WINEGDK_SOURCE_DATE_EPOCH="1784308597"
PROVENANCE_SOURCE="$SRC/third_party/vkd3d-proton-universal"
WINEGDK_R12_PROVENANCE_SOURCE="$SRC/third_party/winegdk-r12"
WINEGDK_NATIVE_PROVENANCE_SOURCE="$SRC/third_party/winegdk-native5"
PROVENANCE_BASE_REL="files/share/bedrock-on-linux/licenses-and-provenance"
VKD3D_PROVENANCE_REL="$PROVENANCE_BASE_REL/vkd3d-proton-universal"
WINEGDK_PROVENANCE_REL="$PROVENANCE_BASE_REL/winegdk"
GDK_PROTON_PROVENANCE_REL="$PROVENANCE_BASE_REL/gdk-proton-base"
if [[ -n "$WINEGDK_PREFIX" ]]; then
  WINEGDK_BUILD_RECORD="$WINEGDK_PREFIX/.bol-winegdk-build.env"
  WINEGDK_PACKAGE_VERSIONS="$WINEGDK_PREFIX/.bol-winegdk-package-versions.tsv"
else
  WINEGDK_BUILD_RECORD="$ENGINE_DIR/files/.bol-winegdk-build.env"
  WINEGDK_PACKAGE_VERSIONS="$ENGINE_DIR/files/.bol-winegdk-package-versions.tsv"
  # A published engine keeps those records beside its embedded provenance
  # rather than at the root of files/, which is where a freshly built prefix
  # leaves them. Repackaging one to change only the vkd3d payload must find
  # them there; both copies are checked against the pinned WineGDK commit and
  # source manifest below either way.
  if [[ ! -f "$WINEGDK_BUILD_RECORD" ]]; then
    WINEGDK_BUILD_RECORD="$ENGINE_DIR/$WINEGDK_PROVENANCE_REL/.bol-winegdk-build.env"
    WINEGDK_PACKAGE_VERSIONS="$ENGINE_DIR/$WINEGDK_PROVENANCE_REL/.bol-winegdk-package-versions.tsv"
  fi
fi
PROVENANCE_FILES=(
  COPYING.LGPL-2.1
  provenance.env
  submodules.lock
  OUTPUT-SHA256SUMS
  restore-nv-dgc.patch
  fix-occluded-frame-latency.patch
)
WINEGDK_R12_PROVENANCE_FILES=(
  README.md
  SOURCE-SHA256SUMS
  online-patches-after-user-ready.patch
)
WINEGDK_NATIVE_PROVENANCE_FILES=(
  README.md
  SOURCE-SHA256SUMS
  0001-winegdk-native5-Xbox-and-file-picker-runtime.patch
  0002-windows.storage-use-legacy-single-file-dialog.patch
  0003-xgameruntime-use-windows-achievements-token.patch
  0004-combase-implement-context-callback.patch
  0005-winex11-use-client-surface-origin.patch
  0006-xgameruntime-stop-faking-xstore-answers.patch
  0007-ntdll-load-main-image-from-a-mapped-fd.patch
  0008-ntdll-accept-a-path-in-the-image-map.patch
  0009-xgameruntime-support-IXSystemImpl2-5-and-stub-XSystemHandleTrack.patch
)

verify_sha256() {
  local file="$1" expected="$2" label="$3" actual
  [[ -f "$file" && ! -L "$file" ]] || {
    echo "!! required $label is missing or is not a regular file: $file" >&2
    exit 1
  }
  read -r actual _ < <(sha256sum "$file")
  if [[ "$actual" != "$expected" ]]; then
    echo "!! SHA-256 mismatch for $label: got $actual, expected $expected" >&2
    exit 1
  fi
}

verify_reviewed_provenance() {
  local root="$1"
  verify_sha256 "$root/COPYING.LGPL-2.1" \
    "dc626520dcd53a22f727af3ee42c770e56c97a64fe3adb063799d8ab032fe551" \
    "vkd3d-proton LGPL notice"
  verify_sha256 "$root/provenance.env" \
    "f46ed5078a90958968ff40c2c9b5caf1c79fb5adaa34ac2a1f5c1c47f5e36e58" \
    "vkd3d-proton provenance lock"
  verify_sha256 "$root/submodules.lock" \
    "19ae5e28d0828ebf911fe99569e4de56328ea39e1a076a11506a78d98c76f3aa" \
    "vkd3d-proton submodule lock"
  verify_sha256 "$root/OUTPUT-SHA256SUMS" \
    "09d22ca93ae4b0a2f99294a43cec6a89a2bd92b96dffb14f2fcc5cf66685b3df" \
    "vkd3d-proton output hash lock"
  verify_sha256 "$root/restore-nv-dgc.patch" \
    "91878d389dc0e315f770fa6c7fffea8f78f410a04796c38e2a6410ff0b9b4a33" \
    "vkd3d-proton NV-DGC restoration patch"
  verify_sha256 "$root/fix-occluded-frame-latency.patch" \
    "aac04d2bf777345272dec90f48b715d3eaebac7b2890f2b1ece4aa8daffc3465" \
    "vkd3d-proton occluded frame-latency fix"
}

verify_winegdk_source_provenance() {
  local r12_root="$1" native_root="$2"
  verify_sha256 "$r12_root/README.md" \
    "754aca14f0622ba8e40f5572488e8b9a6408b5c1619d0a5d67800f7fddb0017b" \
    "WineGDK r12 source-delta README"
  verify_sha256 "$r12_root/SOURCE-SHA256SUMS" \
    "2c70a6922132746a3de6cc282ac7cd96ac4a1c8bbd3c326f86696569e43a2da1" \
    "WineGDK r12 source hash lock"
  verify_sha256 "$r12_root/online-patches-after-user-ready.patch" \
    "ee0543f11737a11f5edec389967bb41482c7f5eda3807c24d171dd6bf6301274" \
    "WineGDK r12 source delta"
  verify_sha256 "$native_root/README.md" \
    "ab92ce75b331a83affc8e2758cc2ce3106eb5e1a25f7e02111a8a91cc205dd40" \
    "WineGDK native5 source-delta README"
  verify_sha256 "$native_root/SOURCE-SHA256SUMS" \
    "027969bfa0e28aa7bec32a4a85ee2afe64ff274062479aeaf9e46a527a67979b" \
    "WineGDK native5 source hash lock"
  verify_sha256 \
    "$native_root/0001-winegdk-native5-Xbox-and-file-picker-runtime.patch" \
    "d5630af845064b50780665e8ba335d4484a4c0b68eb396f195f840f0d2689a8b" \
    "WineGDK native5 source delta"
  verify_sha256 \
    "$native_root/0002-windows.storage-use-legacy-single-file-dialog.patch" \
    "68b20aa95afbef46ad9a50d24cadfdd89267e1f4ad341bb25320443b8cac1cae" \
    "WineGDK single-file dialog fallback"
  verify_sha256 \
    "$native_root/0003-xgameruntime-use-windows-achievements-token.patch" \
    "244101f82f58328b94fce93d02ace47e1c0148cf67b7a32e4b9dd44225e81e00" \
    "WineGDK Windows Achievements token fix"
  verify_sha256 \
    "$native_root/0004-combase-implement-context-callback.patch" \
    "33afb0b3bcd7678e828a955d639d3384b8b5c656219b05e9c53bb45dd7c34919" \
    "WineGDK COM context-callback backport"
  verify_sha256 \
    "$native_root/0005-winex11-use-client-surface-origin.patch" \
    "464da914667bd9c683fb79bc7c2a4477546a73060c66f29809cfa93783cbc1c8" \
    "WineGDK X11 client-surface geometry backport"
  verify_sha256 \
    "$native_root/0006-xgameruntime-stop-faking-xstore-answers.patch" \
    "0ae38358531a81af9b20a997d73f07b383db68afcd14cf89107dd9442f101fd1" \
    "WineGDK XStore honest-failure fix"
  verify_sha256 \
    "$native_root/0007-ntdll-load-main-image-from-a-mapped-fd.patch" \
    "0ebb25f67183b0bb052b9cbb76f6a0aca02be89fc7be448ff94702b178e9ffc6" \
    "WineGDK ntdll mapped-fd main-image loader"
  verify_sha256 \
    "$native_root/0008-ntdll-accept-a-path-in-the-image-map.patch" \
    "7510e707abf9869d40b855754d58c025e1bd98cec455bef3dbec79e9d14461a3" \
    "WineGDK ntdll image-map path support"
  verify_sha256 \
    "$native_root/0009-xgameruntime-support-IXSystemImpl2-5-and-stub-XSystemHandleTrack.patch" \
    "c0a8e35717368fdc31f5ba6374ecb64aacb84cbd1dc22958077cff9ab5e2c301" \
    "WineGDK XSystem interface revisions"
}

has_marker() {
  local file="$1" marker="$2"
  # Do not use grep -q here: under pipefail its early exit can make strings(1)
  # fail with SIGPIPE and turn a successful match into a false negative.
  LC_ALL=C strings -a "$file" | grep -F -- "$marker" >/dev/null
}

has_text() {
  local file="$1" expected="$2"
  LC_ALL=C strings -a "$file" | grep -F -- "$expected" >/dev/null
}

has_file_picker_registration() {
  python3 - "$1" <<'PY'
import re
import sys
from pathlib import Path

data = Path(sys.argv[1]).read_bytes()
registration = re.compile(
    rb"ForceRemove Microsoft[.]Windows[.]Storage[.]Pickers[.]FileOpenPicker"
    rb"\s*[{]\s*val 'DllPath' = s '%MODULE%'\s*[}]"
)
raise SystemExit(0 if registration.search(data) else 1)
PY
}

version_file_has() {
  local file="$1" expected="$2" component="$3"
  if [[ ! -f "$file" ]] || ! grep -F -- "$expected" "$file" >/dev/null; then
    echo "!! $component must be version $expected (not confirmed by $file)" >&2
    exit 1
  fi
}

# Restore WineGDK's native XThreading delegate from the exact reviewed
# GDK-Proton base. The wrapper xgameruntime.dll loads this different binary
# and forwards XAsync/XTaskQueue calls to it, so copying the wrapper itself or
# merely making Proton's copy optional would produce a subtly broken runtime.
GDK_PROTON_EXTRACT="$STAGE/gdk-proton-base-extract"
mkdir -p "$GDK_PROTON_EXTRACT"
tar -xzf "$GDK_PROTON_BASE_ARCHIVE" -C "$GDK_PROTON_EXTRACT" -- \
  "$GDK_PROTON_THREADING_MEMBER"
GDK_PROTON_THREADING_SOURCE="$GDK_PROTON_EXTRACT/$GDK_PROTON_THREADING_MEMBER"
verify_sha256 "$GDK_PROTON_THREADING_SOURCE" \
  "$GDK_PROTON_THREADING_SHA256" "GDK-Proton native XThreading runtime"
GDK_PROTON_THREADING_DEST="$STAGED_ENGINE/$GDK_PROTON_THREADING_REL"
install -D -m755 "$GDK_PROTON_THREADING_SOURCE" \
  "$GDK_PROTON_THREADING_DEST"
verify_sha256 "$GDK_PROTON_THREADING_DEST" \
  "$GDK_PROTON_THREADING_SHA256" "staged native XThreading runtime"
rm -rf "$GDK_PROTON_EXTRACT"
has_text "$STAGED_ENGINE/proton" "xgameruntime.dll.threading" || {
  echo "!! Proton no longer wires the required native XThreading runtime" >&2
  exit 1
}
echo "   native GDK-Proton XThreading runtime verified"

version_file_has "$ENGINE_DIR/files/lib/wine/dxvk/version" \
  "$DXVK_VERSION" "DXVK"

# Refuse missing/tampered corresponding-source records before accepting the
# binary payload. OUTPUT-SHA256SUMS is itself pinned above, then checks all four
# reviewed universal DLLs relative to the supplied build directory.
verify_reviewed_provenance "$PROVENANCE_SOURCE"
verify_winegdk_source_provenance "$WINEGDK_R12_PROVENANCE_SOURCE" \
  "$WINEGDK_NATIVE_PROVENANCE_SOURCE"
(
  cd "$UNIVERSAL_DGC_BUILD_DIR"
  sha256sum --strict -c "$PROVENANCE_SOURCE/OUTPUT-SHA256SUMS"
)

# The Wine files must come from the reproducible Bullseye build helper, not
# merely from a caller who supplied the expected commit as an argument.
[[ -f "$WINEGDK_BUILD_RECORD" && ! -L "$WINEGDK_BUILD_RECORD" ]] || {
  echo "!! WineGDK build provenance is missing: $WINEGDK_BUILD_RECORD" >&2
  echo "   Build the prefix with scripts/build-winegdk-bullseye.sh." >&2
  exit 1
}
[[ -f "$WINEGDK_PACKAGE_VERSIONS" && ! -L "$WINEGDK_PACKAGE_VERSIONS" ]] || {
  echo "!! WineGDK package-version record is missing: $WINEGDK_PACKAGE_VERSIONS" >&2
  exit 1
}
python3 - "$WINEGDK_BUILD_RECORD" "$WINEGDK_PACKAGE_VERSIONS" \
  "$PINNED_WINEGDK_COMMIT" "$WINEGDK_SOURCE_DATE_EPOCH" \
  "$PINNED_WINEGDK_SOURCE_MANIFEST_SHA256" <<'PY'
import hashlib
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
package_versions_path = Path(sys.argv[2])
expected_commit = sys.argv[3]
expected_epoch = sys.argv[4]
expected_source_manifest = sys.argv[5]
try:
    lines = path.read_text(encoding="utf-8").splitlines()
except (OSError, UnicodeError) as exc:
    raise SystemExit(f"unreadable WineGDK build provenance: {exc}") from exc

values = {}
for line in lines:
    if not line or line.startswith("#"):
        continue
    if "=" not in line:
        raise SystemExit(f"invalid WineGDK provenance line: {line!r}")
    key, value = line.split("=", 1)
    if not re.fullmatch(r"[a-z][a-z0-9_]*", key) or key in values:
        raise SystemExit(f"invalid/duplicate WineGDK provenance key: {key!r}")
    values[key] = value

required = {
    "schema": "1",
    "winegdk_commit": expected_commit,
    "source_manifest_sha256": expected_source_manifest,
    "source_date_epoch": expected_epoch,
    "debian_suite": "bullseye",
    "glibc_ceiling": "2.31",
}
if not re.fullmatch(r"[0-9a-f]{64}", expected_source_manifest):
    raise SystemExit("invalid configured WineGDK source-manifest SHA-256")
if set(values) != set(required) | {"package_versions_sha256"}:
    raise SystemExit(
        "WineGDK build provenance has an unexpected field set: "
        + ", ".join(sorted(values)))
for key, expected in required.items():
    if values.get(key) != expected:
        raise SystemExit(
            f"WineGDK build provenance mismatch for {key}: "
            f"got {values.get(key)!r}, expected {expected!r}")
if not re.fullmatch(r"[0-9a-f]{64}",
                    values.get("package_versions_sha256", "")):
    raise SystemExit("invalid WineGDK package_versions_sha256")
try:
    package_versions_hash = hashlib.sha256(
        package_versions_path.read_bytes()).hexdigest()
except OSError as exc:
    raise SystemExit(f"unreadable WineGDK package-version record: {exc}") \
        from exc
if package_versions_hash != values["package_versions_sha256"]:
    raise SystemExit(
        "WineGDK package-version record SHA-256 mismatch: "
        f"got {package_versions_hash}, "
        f"expected {values['package_versions_sha256']}")
print("   WineGDK Bullseye build provenance verified")
PY

# Carry the licence, exact source transformation and build locks inside the
# engine itself. SHA256SUMS gives recipients a direct integrity check, while
# the engine manifest below also records every copied file hash.
VKD3D_PROVENANCE_DEST="$STAGED_ENGINE/$VKD3D_PROVENANCE_REL"
WINEGDK_PROVENANCE_DEST="$STAGED_ENGINE/$WINEGDK_PROVENANCE_REL"
GDK_PROTON_PROVENANCE_DEST="$STAGED_ENGINE/$GDK_PROTON_PROVENANCE_REL"
rm -rf "${STAGED_ENGINE:?}/$PROVENANCE_BASE_REL"
mkdir -p "$VKD3D_PROVENANCE_DEST" "$WINEGDK_PROVENANCE_DEST" \
  "$GDK_PROTON_PROVENANCE_DEST" "$WINEGDK_PROVENANCE_DEST/r12" \
  "$WINEGDK_PROVENANCE_DEST/native5"
for provenance_file in "${PROVENANCE_FILES[@]}"; do
  cp -a "$PROVENANCE_SOURCE/$provenance_file" \
    "$VKD3D_PROVENANCE_DEST/$provenance_file"
done
cp -a "$WINEGDK_BUILD_RECORD" \
  "$WINEGDK_PROVENANCE_DEST/.bol-winegdk-build.env"
cp -a "$WINEGDK_PACKAGE_VERSIONS" \
  "$WINEGDK_PROVENANCE_DEST/.bol-winegdk-package-versions.tsv"
# WineGDK/Wine is LGPL-2.1 as well. Keep an explicit licence copy beside its
# own build records instead of relying on the vkd3d provenance directory.
cp -a "$PROVENANCE_SOURCE/COPYING.LGPL-2.1" \
  "$WINEGDK_PROVENANCE_DEST/COPYING.LGPL-2.1"
for provenance_file in "${WINEGDK_R12_PROVENANCE_FILES[@]}"; do
  cp -a "$WINEGDK_R12_PROVENANCE_SOURCE/$provenance_file" \
    "$WINEGDK_PROVENANCE_DEST/r12/$provenance_file"
done
for provenance_file in "${WINEGDK_NATIVE_PROVENANCE_FILES[@]}"; do
  cp -a "$WINEGDK_NATIVE_PROVENANCE_SOURCE/$provenance_file" \
    "$WINEGDK_PROVENANCE_DEST/native5/$provenance_file"
done
verify_winegdk_source_provenance "$WINEGDK_PROVENANCE_DEST/r12" \
  "$WINEGDK_PROVENANCE_DEST/native5"
cat >"$GDK_PROTON_PROVENANCE_DEST/provenance.env" <<EOF
schema=1
repository=$GDK_PROTON_BASE_REPOSITORY
release=$GDK_PROTON_BASE_RELEASE
archive=$GDK_PROTON_BASE_NAME
archive_sha256=$GDK_PROTON_BASE_SHA256
threading_dll_path=$GDK_PROTON_THREADING_REL
threading_dll_sha256=$GDK_PROTON_THREADING_SHA256
EOF
chmod 0644 "$GDK_PROTON_PROVENANCE_DEST/provenance.env"
# Keep a single canonical copy in the explicit distribution-provenance tree.
# These unlink staging entries only; the locked ENGINE_DIR remains untouched.
rm -f "$STAGED_ENGINE/files/.bol-winegdk-build.env" \
  "$STAGED_ENGINE/files/.bol-winegdk-package-versions.tsv"
(
  cd "$VKD3D_PROVENANCE_DEST"
  sha256sum "${PROVENANCE_FILES[@]}" > SHA256SUMS
  sha256sum --strict -c SHA256SUMS >/dev/null
)
verify_reviewed_provenance "$VKD3D_PROVENANCE_DEST"
echo "   embedded licence and build provenance verified"

copy_compat_pair() {
  local build_arch="$1" arch="$2" dest_suffix="$3" dll source dest
  for dll in "${DLLS[@]}"; do
    source="$UNIVERSAL_DGC_BUILD_DIR/$build_arch/$dll"
    dest="$STAGED_ENGINE/$VKD3D_ROOT/$arch/$dll$dest_suffix"
    [[ -f "$source" ]] || {
      echo "!! missing reviewed universal DGC DLL: $source" >&2
      exit 1
    }
    rm -f "$dest"
    cp -a "$source" "$dest"
  done
}

for arch in "${ARCHES[@]}"; do
  rel_dir="$VKD3D_ROOT/$arch"

  if [[ "$arch" == "x86_64-windows" ]]; then
    build_arch=x64
  else
    build_arch=x86
  fi
  copy_compat_pair "$build_arch" "$arch" ".bol-ext-dgc"
  copy_compat_pair "$build_arch" "$arch" ".bol-nv-dgc"

  staged_core="$STAGED_ENGINE/$rel_dir/d3d12core.dll"
  has_marker "$staged_core.bol-ext-dgc" "$EXT_MARKER" || {
    echo "!! staged EXT-DGC verification failed for $arch" >&2
    exit 1
  }
  has_marker "$staged_core.bol-ext-dgc" "$NV_MARKER" || {
    echo "!! staged universal EXT slot lost its NV fallback ($arch)" >&2
    exit 1
  }
  has_text "$staged_core.bol-ext-dgc" "$VKD3D_BASE_VERSION" || {
    echo "!! staged universal DLL is not based on vkd3d-proton $VKD3D_BASE_VERSION ($arch)" >&2
    exit 1
  }
  has_marker "$staged_core.bol-nv-dgc" "$NV_MARKER" || {
    echo "!! staged NV-DGC verification failed for $arch" >&2
    exit 1
  }
  has_marker "$staged_core.bol-nv-dgc" "$EXT_MARKER" || {
    echo "!! reviewed NV-DGC build lost its 3.0.1 EXT path ($arch)" >&2
    exit 1
  }
  has_text "$staged_core.bol-nv-dgc" "$VKD3D_BASE_VERSION" || {
    echo "!! staged NV-DGC DLL is not based on vkd3d-proton 3.0.1 ($arch)" >&2
    exit 1
  }

  # Default the staged engine to the universal build as well. This remains safe
  # even if launch-time probing or activation is interrupted.
  for dll in "${DLLS[@]}"; do
    rm -f "$STAGED_ENGINE/$rel_dir/$dll"
    cp -a "$STAGED_ENGINE/$rel_dir/$dll.bol-ext-dgc" \
      "$STAGED_ENGINE/$rel_dir/$dll"
    rm -f "$STAGED_ENGINE/$rel_dir/$dll.bol-pre301"
  done
  echo "   verified $arch: EXT-DGC + NV-DGC"
done

# The native engine must expose XGame identity and the completed XUser request
# path, while containing no remnants or imports from the old Minecraft memory
# patcher. These checks catch an accidental r12 overlay before provenance is
# asserted.
for arch in "${ARCHES[@]}"; do
  xgdk="$STAGED_ENGINE/files/lib/wine/$arch/xgameruntime.dll"
  [[ -f "$xgdk" ]] || {
    echo "!! WineGDK xgameruntime.dll missing for $arch" >&2; exit 1; }
  has_text "$xgdk" "native XGame identity loaded: TitleId" || {
    echo "!! WineGDK native XGame identity missing for $arch" >&2; exit 1; }
  has_text "$xgdk" "native Xbox app configuration ready" || {
    echo "!! WineGDK native Xbox app context missing for $arch" >&2; exit 1; }
  has_text "$xgdk" \
    "using user-only XSTS for Xbox Achievements." || {
    echo "!! WineGDK Xbox Achievements token fix missing for $arch" >&2
    exit 1
  }
  has_text "$xgdk" "no store service available, failing the" || {
    echo "!! WineGDK XStore honest-failure fix missing for $arch" >&2; exit 1; }
  has_text "$xgdk" "requesting token: method=" || {
    echo "!! WineGDK native XUser request path missing for $arch" >&2; exit 1; }
  has_text "$xgdk" "unsupported GDK QueryApiImpl class" || {
    echo "!! WineGDK native interface diagnostics missing for $arch" >&2; exit 1; }
  storage="$STAGED_ENGINE/files/lib/wine/$arch/windows.storage.dll"
  [[ -f "$storage" ]] || {
    echo "!! WineGDK windows.storage.dll missing for $arch" >&2; exit 1; }
  storage_file="$STAGED_ENGINE/files/lib/wine/$arch/windows.storage.applicationdata.dll"
  [[ -f "$storage_file" ]] || {
    echo "!! WineGDK StorageFile runtime missing for $arch" >&2; exit 1; }
  has_text "$storage" "Microsoft.Windows.Storage.Pickers.FileOpenPicker" || {
    echo "!! WineGDK native FileOpenPicker class missing for $arch" >&2; exit 1; }
  has_text "$storage" "PickSingleFileAsync" || {
    echo "!! WineGDK native single-file picker method missing for $arch" >&2; exit 1; }
  has_text "$storage" "PickMultipleFilesAsync" || {
    echo "!! WineGDK native multi-file picker method missing for $arch" >&2; exit 1; }
  has_file_picker_registration "$storage" || {
    echo "!! WineGDK native FileOpenPicker registration missing for $arch" >&2
    exit 1
  }
  if has_text "$xgdk" "online patches skipped:" ||
     has_text "$xgdk" "committed XblInitialize gate"; then
    echo "!! legacy Minecraft memory-patch code remains in $arch" >&2
    exit 1
  fi
  # Do not use grep -q here: with pipefail, an early grep exit can make
  # objdump receive SIGPIPE and turn a positive match into status 141.
  if objdump -p "$xgdk" | grep -E \
       'K32GetModuleInformation|GetModuleInformation|VirtualProtect' \
       >/dev/null; then
    echo "!! Minecraft memory-patch imports remain in $arch" >&2
    exit 1
  fi
done

# Hash the exact bytes that users will run. _wire_winegdk() applies these
# compatibility patches after installation, so doing it only at runtime would
# immediately invalidate critical_files (notably combase.dll) and cause an
# endless reinstall cycle. Patch the locked staging snapshot, never ENGINE_DIR,
# and remove patch backups before either the manifest or archive is created.
PYTHONPATH="$SRC" python3 - "$STAGED_ENGINE" <<'PY'
import os
import shutil
import sys
from pathlib import Path

from bol.proton import patch_proton

stage = Path(sys.argv[1])
# The low-space fallback above is a hard-link snapshot. Detach every file the
# patcher may write before invoking it, otherwise an in-place write through a
# staged hard link would also mutate ENGINE_DIR despite the packaging lock.
for relative in (
    "files/lib/wine/x86_64-windows/combase.dll",
    "files/lib/wine/x86_64-windows/ntdll.dll",
):
    path = stage / relative
    detached = path.with_name(path.name + ".bol-detached")
    shutil.copy2(path, detached, follow_symlinks=False)
    os.replace(detached, path)
patch_proton(stage, strict=False)
for backup in stage.rglob("*.bol-orig"):
    if backup.is_file() or backup.is_symlink():
        backup.unlink()
    else:
        raise SystemExit(f"unexpected patch-backup entry: {backup}")
if any(stage.rglob("*.bol-orig")):
    raise SystemExit("could not remove every staged *.bol-orig backup")
print("   staged Proton compatibility patches verified")
PY

# Refuse a host-built engine that would import a newer glibc than the oldest
# supported runtime (Debian Bullseye / Steam Runtime sniper: glibc 2.31).
python3 - "$STAGED_ENGINE" <<'PY'
import re
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])
limit = (2, 31)
worst = ((0, 0), None)
violations = []
for path in root.rglob("*"):
    if not path.is_file():
        continue
    try:
        with path.open("rb") as stream:
            if stream.read(4) != b"\x7fELF":
                continue
    except OSError:
        continue
    result = subprocess.run(
        ["readelf", "--version-info", "--wide", str(path)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise SystemExit(f"readelf failed for {path}: {result.stderr.strip()}")
    versions = [tuple(map(int, value)) for value in
                re.findall(r"GLIBC_(\d+)\.(\d+)", result.stdout)]
    required = max(versions, default=(0, 0))
    if required > worst[0]:
        worst = (required, path)
    if required > limit:
        violations.append((required, path.relative_to(root)))
if violations:
    detail = "\n".join(
        f"  GLIBC_{major}.{minor}: {path}"
        for (major, minor), path in sorted(violations, reverse=True)[:40]
    )
    raise SystemExit(
        "engine contains ELF binaries newer than GLIBC_2.31:\n" + detail)
print(f"   ABI verified: maximum GLIBC_{worst[0][0]}.{worst[0][1]} "
      f"({worst[1]})")
PY

rm -f "$STAGED_ENGINE/engine-manifest.json"
python3 - "$STAGED_ENGINE" "$REV" "$DXVK_VERSION" \
  "$VKD3D_EXT_VERSION" "$VKD3D_NV_VERSION" "$SRC" \
  "$VKD3D_NV_SOURCE" "$VKD3D_NV_REVERT" "$WINEGDK_COMMIT" \
  "$VKD3D_PROVENANCE_REL" "$WINEGDK_PROVENANCE_REL" \
  "$GDK_PROTON_PROVENANCE_REL" "$GDK_PROTON_BASE_REPOSITORY" \
  "$GDK_PROTON_BASE_RELEASE" "$GDK_PROTON_BASE_NAME" \
  "$GDK_PROTON_BASE_SHA256" "$GDK_PROTON_THREADING_SHA256" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
build_rev = sys.argv[2]
dxvk_version, vkd3d_ext_version, vkd3d_nv_version = sys.argv[3:6]
source_root = Path(sys.argv[6])
vkd3d_nv_source, vkd3d_nv_revert, winegdk_commit = sys.argv[7:10]
vkd3d_provenance_root = Path(sys.argv[10])
winegdk_provenance_root = Path(sys.argv[11])
gdk_proton_provenance_root = Path(sys.argv[12])
gdk_repository, gdk_release, gdk_archive = sys.argv[13:16]
gdk_archive_sha256, gdk_threading_sha256 = sys.argv[16:18]
sys.path.insert(0, str(source_root))

try:
    from bol.vkd3d import (
        REQUIRED_CRITICAL_FILE_PATHS,
        REQUIRED_EXECUTABLE_FILE_PATHS,
        REQUIRED_PINNED_CRITICAL_HASHES,
        REQUIRED_RUNTIME_LINK_TARGETS,
        REQUIRED_VARIANT_HASHES,
        REQUIRED_VARIANT_HASHES_BUILD_REV,
    )
except (ImportError, AttributeError) as exc:
    raise SystemExit(
        "runtime vkd3d hash pins are unavailable; refusing to package"
    ) from exc
base = Path("files/lib/wine/vkd3d-proton")
arches = ("x86_64-windows", "i386-windows")
dlls = ("d3d12.dll", "d3d12core.dll")

variants = {}
for variant, suffix in (("ext-dgc", ".bol-ext-dgc"),
                        ("nv-dgc", ".bol-nv-dgc")):
    files = {}
    for arch in arches:
        for dll in dlls:
            rel = base / arch / (dll + suffix)
            path = root / rel
            if not path.is_file():
                raise SystemExit(f"missing staged variant file: {rel}")
            files[rel.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    variants[variant] = {"files": files}

provenance_files = {}
for relative_root, names in (
    (vkd3d_provenance_root, (
        "COPYING.LGPL-2.1",
        "provenance.env",
        "submodules.lock",
        "OUTPUT-SHA256SUMS",
        "restore-nv-dgc.patch",
        "SHA256SUMS",
    )),
    (winegdk_provenance_root, (
        "COPYING.LGPL-2.1",
        ".bol-winegdk-build.env",
        ".bol-winegdk-package-versions.tsv",
        "r12/README.md",
        "r12/SOURCE-SHA256SUMS",
        "r12/online-patches-after-user-ready.patch",
        "native5/README.md",
        "native5/SOURCE-SHA256SUMS",
        "native5/0001-winegdk-native5-Xbox-and-file-picker-runtime.patch",
        "native5/0002-windows.storage-use-legacy-single-file-dialog.patch",
        "native5/0003-xgameruntime-use-windows-achievements-token.patch",
        "native5/0004-combase-implement-context-callback.patch",
        "native5/0005-winex11-use-client-surface-origin.patch",
        "native5/0006-xgameruntime-stop-faking-xstore-answers.patch",
        "native5/0007-ntdll-load-main-image-from-a-mapped-fd.patch",
        "native5/0008-ntdll-accept-a-path-in-the-image-map.patch",
        "native5/0009-xgameruntime-support-IXSystemImpl2-5-and-stub-XSystemHandleTrack.patch",
    )),
    (gdk_proton_provenance_root, ("provenance.env",)),
):
    for name in names:
        relative = relative_root / name
        path = root / relative
        if not path.is_file() or path.is_symlink():
            raise SystemExit(f"missing staged provenance file: {relative}")
        provenance_files[relative.as_posix()] = hashlib.sha256(
            path.read_bytes()).hexdigest()

vkd3d_provenance_prefix = vkd3d_provenance_root.as_posix() + "/"
winegdk_provenance_prefix = winegdk_provenance_root.as_posix() + "/"
gdk_proton_provenance_prefix = gdk_proton_provenance_root.as_posix() + "/"
vkd3d_provenance_files = {
    path: digest for path, digest in provenance_files.items()
    if path.startswith(vkd3d_provenance_prefix)
}
winegdk_provenance_files = {
    path: digest for path, digest in provenance_files.items()
    if path.startswith(winegdk_provenance_prefix)
}
gdk_proton_provenance_files = {
    path: digest for path, digest in provenance_files.items()
    if path.startswith(gdk_proton_provenance_prefix)
}

critical_relative_paths = tuple(dict.fromkeys((
    *REQUIRED_CRITICAL_FILE_PATHS,
    "files/lib/x86_64-linux-gnu/libunwind.so.8.0.1",
    *sorted(provenance_files),
)))
critical_files = {}
for relative in critical_relative_paths:
    path = root / relative
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve())
    except (OSError, ValueError) as exc:
        raise SystemExit(
            f"missing/unsafe critical engine file: {relative}: {exc}") from exc
    if not resolved.is_file():
        raise SystemExit(f"non-file critical engine entry: {relative}")
    critical_files[relative] = hashlib.sha256(resolved.read_bytes()).hexdigest()

for relative in REQUIRED_EXECUTABLE_FILE_PATHS:
    path = root / relative
    if not path.stat().st_mode & 0o100:
        raise SystemExit(f"critical engine file is not executable: {relative}")

for relative, expected_hash in REQUIRED_PINNED_CRITICAL_HASHES.items():
    actual_hash = critical_files.get(relative)
    if actual_hash != expected_hash:
        raise SystemExit(
            f"critical engine hash pin mismatch for {relative}: "
            f"got {actual_hash}, expected {expected_hash}")

for relative, expected_target in REQUIRED_RUNTIME_LINK_TARGETS.items():
    path = root / relative
    if not path.is_symlink() or path.readlink().as_posix() != expected_target:
        raise SystemExit(
            f"critical runtime link mismatch: {relative} must target "
            f"{expected_target!r}")

manifest = {
    "schema": 1,
    "build_rev": build_rev,
    "critical_files": critical_files,
    "components": {
        "dxvk": dxvk_version,
        "vkd3d_ext_dgc": vkd3d_ext_version,
        "vkd3d_nv_dgc": vkd3d_nv_version,
    },
    "provenance": {
        "winegdk": {
            "commit": winegdk_commit,
            "distribution_files": winegdk_provenance_files,
        },
        "vkd3d_universal": {
            "base_commit": vkd3d_nv_source,
            "restored_by_reverting": vkd3d_nv_revert,
            "distribution_files": vkd3d_provenance_files,
        },
        "gdk_proton_base": {
            "repository": gdk_repository,
            "release": gdk_release,
            "archive": gdk_archive,
            "archive_sha256": gdk_archive_sha256,
            "threading_dll_sha256": gdk_threading_sha256,
            "distribution_files": gdk_proton_provenance_files,
        },
        "build": {
            "glibc_max": "2.31",
        },
    },
    "variants": variants,
}

# Refuse an unknown build revision or byte mismatch against the runtime's
# authoritative allow-list before creating the candidate archive.
if build_rev != REQUIRED_VARIANT_HASHES_BUILD_REV:
    raise SystemExit(
        f"no runtime DLL hash pin for build rev {build_rev!r}; "
        f"expected {REQUIRED_VARIANT_HASHES_BUILD_REV!r}"
    )
actual_hashes = {
    variant: dict(payload["files"])
    for variant, payload in variants.items()
}
required_hashes = {
    variant: dict(files)
    for variant, files in REQUIRED_VARIANT_HASHES.items()
}
if actual_hashes != required_hashes:
    all_variants = sorted(set(actual_hashes) | set(required_hashes))
    detail = "mapping differs"
    for variant in all_variants:
        actual_files = actual_hashes.get(variant, {})
        required_files = required_hashes.get(variant, {})
        for relative in sorted(set(actual_files) | set(required_files)):
            if actual_files.get(relative) != required_files.get(relative):
                detail = (
                    f"{variant}:{relative}: got {actual_files.get(relative)!r}, "
                    f"expected {required_files.get(relative)!r}"
                )
                break
        if detail != "mapping differs":
            break
    raise SystemExit(f"DLL hash pin mismatch ({detail})")

(root / "engine-manifest.json").write_text(
    json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
chmod 0644 "$STAGED_ENGINE/engine-manifest.json"

# Default to the pinned WineGDK commit timestamp used by the native5
# candidate. Maintainers can still override it deliberately, but the documented
# packaging command now reproduces the same tar metadata without hidden env.
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$WINEGDK_SOURCE_DATE_EPOCH}"
[[ "$SOURCE_DATE_EPOCH" =~ ^[0-9]+$ ]] || {
  echo "!! SOURCE_DATE_EPOCH must be a non-negative integer" >&2
  exit 1
}
rm -f "$PART" "$CHECKSUM"
# GNU tar + gzip metadata are normalised so rebuilding identical engine bytes
# yields the same archive hash on every maintainer machine. Hard links are
# stored as independent regular files, which also lets the old-Python extractor
# reject link entries without losing engine content.
LC_ALL=C tar --sort=name --format=gnu --hard-dereference \
  --mtime="@${SOURCE_DATE_EPOCH}" --owner=0 --group=0 --numeric-owner \
  -C "$STAGE" -cf - GDK-Proton-xuser | gzip -n -6 > "$PART"

# Prove that the compressed candidate, not only its staging tree, contains the
# exact reviewed records. GNU tar scans the gzip stream once and extracts only
# these tiny files, so this check does not duplicate the engine on disk.
ARCHIVE_CHECK="$STAGE/archive-provenance-check"
mkdir -p "$ARCHIVE_CHECK"
ARCHIVE_MEMBERS=()
for provenance_file in "${PROVENANCE_FILES[@]}" SHA256SUMS; do
  ARCHIVE_MEMBERS+=(
    "GDK-Proton-xuser/$VKD3D_PROVENANCE_REL/$provenance_file")
done
ARCHIVE_MEMBERS+=(
  "GDK-Proton-xuser/$WINEGDK_PROVENANCE_REL/COPYING.LGPL-2.1"
  "GDK-Proton-xuser/$WINEGDK_PROVENANCE_REL/.bol-winegdk-build.env"
  "GDK-Proton-xuser/$WINEGDK_PROVENANCE_REL/.bol-winegdk-package-versions.tsv"
  "GDK-Proton-xuser/$GDK_PROTON_PROVENANCE_REL/provenance.env"
  "GDK-Proton-xuser/$GDK_PROTON_THREADING_REL")
for provenance_file in "${WINEGDK_R12_PROVENANCE_FILES[@]}"; do
  ARCHIVE_MEMBERS+=(
    "GDK-Proton-xuser/$WINEGDK_PROVENANCE_REL/r12/$provenance_file")
done
for provenance_file in "${WINEGDK_NATIVE_PROVENANCE_FILES[@]}"; do
  ARCHIVE_MEMBERS+=(
    "GDK-Proton-xuser/$WINEGDK_PROVENANCE_REL/native5/$provenance_file")
done
tar -xzf "$PART" -C "$ARCHIVE_CHECK" -- "${ARCHIVE_MEMBERS[@]}"
EXTRACTED_VKD3D_PROVENANCE="$ARCHIVE_CHECK/GDK-Proton-xuser/$VKD3D_PROVENANCE_REL"
EXTRACTED_WINEGDK_PROVENANCE="$ARCHIVE_CHECK/GDK-Proton-xuser/$WINEGDK_PROVENANCE_REL"
EXTRACTED_GDK_PROTON_PROVENANCE="$ARCHIVE_CHECK/GDK-Proton-xuser/$GDK_PROTON_PROVENANCE_REL"
verify_reviewed_provenance "$EXTRACTED_VKD3D_PROVENANCE"
(
  cd "$EXTRACTED_VKD3D_PROVENANCE"
  sha256sum --strict -c SHA256SUMS >/dev/null
)
read -r winegdk_record_sha _ < <(sha256sum "$WINEGDK_BUILD_RECORD")
verify_sha256 "$EXTRACTED_WINEGDK_PROVENANCE/.bol-winegdk-build.env" \
  "$winegdk_record_sha" \
  "archived WineGDK build provenance"
read -r winegdk_packages_sha _ < <(sha256sum "$WINEGDK_PACKAGE_VERSIONS")
verify_sha256 \
  "$EXTRACTED_WINEGDK_PROVENANCE/.bol-winegdk-package-versions.tsv" \
  "$winegdk_packages_sha" "archived WineGDK package-version record"
verify_sha256 "$EXTRACTED_WINEGDK_PROVENANCE/COPYING.LGPL-2.1" \
  "dc626520dcd53a22f727af3ee42c770e56c97a64fe3adb063799d8ab032fe551" \
  "archived WineGDK LGPL notice"
verify_winegdk_source_provenance "$EXTRACTED_WINEGDK_PROVENANCE/r12" \
  "$EXTRACTED_WINEGDK_PROVENANCE/native5"
read -r gdk_proton_record_sha _ < <(
  sha256sum "$GDK_PROTON_PROVENANCE_DEST/provenance.env")
verify_sha256 "$EXTRACTED_GDK_PROTON_PROVENANCE/provenance.env" \
  "$gdk_proton_record_sha" "archived GDK-Proton base provenance"
verify_sha256 "$ARCHIVE_CHECK/GDK-Proton-xuser/$GDK_PROTON_THREADING_REL" \
  "$GDK_PROTON_THREADING_SHA256" \
  "archived native XThreading runtime"
echo "   archived licence and build provenance verified"

mv -f "$PART" "$TARBALL"
(
  cd "$OUT"
  sha256sum "$ASSET" > "$ASSET.sha256"
)

SZ_BYTES="$(stat -c%s "$TARBALL")"
echo "   candidate: $TARBALL ($(du -h "$TARBALL" | cut -f1))"
echo "   checksum:  $CHECKSUM"
if (( SZ_BYTES > 2147483648 )); then
  echo "!! WARNING: candidate archive is larger than 2 GiB." >&2
fi
echo "== Candidate created locally; nothing was tagged, uploaded, or released."
