#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Assemble the experimental ARM64EC engine (#250, docs/ARM64EC.md):
#
#   package-engine-arm64.sh GE_BASE_ARCHIVE GDK_PROTON_BASE_ARCHIVE \
#     WINEGDK_PREFIX VKD3D_ARM64X_DIR VKD3D_UNIVERSAL_DIR OUT_DIR
#
# On GE-Proton11-14-aarch64, which already holds FEX (libarm64ecfex.dll,
# libwow64fex.dll), DXVK and Proton's ARM64 layout (files/bin-arm64,
# default_pfx_arm64, the SLR 4 arm64 tool manifest), it lays:
#
# - the WineGDK prefix built for arm64ec, aarch64, i386 and x86_64
#   (build-winegdk.yml arch=aarch64), so every Wine module is ours;
# - our vkd3d-proton: the ARM64X payload in aarch64-windows, the x86 payload
#   in x86_64-windows and i386-windows, each with the .bol-ext-dgc and
#   .bol-nv-dgc slots the launcher switches between;
# - the x64 libraries GDK-Proton ships for the GDK runtime and its native
#   XThreading DLL, from the same pinned GDK-Proton10-32 archive as the x86
#   engine: an ARM64EC process loads x64 code through FEX like the game;
# - GDK-Proton's one change to the proton script, re-applied for ARM64
#   (third_party/gdk-proton-arm64), so a prefix gets the GDK runtime.
#
# VKD3D_ARM64X_DIR and VKD3D_UNIVERSAL_DIR are the vkd3d-proton-3.0.1-nv-dgc
# directories build-vkd3d.yml packs (aarch64/, and x64/ + x86/). The archive is
# OUT_DIR/GDK-Proton-xuser-<WINEGDK_ARM64_BUILD_REV>.tar.gz, top directory
# GDK-Proton-xuser-arm64. Nothing is tagged, uploaded or released here.
set -Eeuo pipefail
umask 022
export LC_ALL=C LANG=C TZ=UTC

usage="usage: $0 GE_BASE_ARCHIVE GDK_PROTON_BASE_ARCHIVE WINEGDK_PREFIX VKD3D_ARM64X_DIR VKD3D_UNIVERSAL_DIR OUT_DIR"
GE_ARCHIVE="${1:?$usage}"
GDK_ARCHIVE="${2:?$usage}"
WINEGDK_PREFIX="${3:?$usage}"
VKD3D_ARM64X_DIR="${4:?$usage}"
VKD3D_UNIVERSAL_DIR="${5:?$usage}"
OUT="${6:?$usage}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

die() {
  echo "!! $*" >&2
  exit 1
}
pin() {
  grep -m1 "^$1 = " "$SRC/bol/config.py" | cut -d'"' -f2
}
sha() {
  sha256sum "$1" | cut -d' ' -f1
}
verify_sha256() {
  [[ "$(sha "$1")" == "$2" ]] || die "$3: SHA-256 $(sha "$1") is not the reviewed $2"
}

readonly GE_REPOSITORY="GloriousEggroll/proton-ge-custom"
readonly GE_RELEASE="GE-Proton11-14"
readonly GE_NAME="GE-Proton11-14-aarch64"
readonly GE_SHA256="f6353f4417bd8c1148d7db18bc383d8e2c59c403e367425fd8b9daaaecc2fad8"
readonly GE_PROTON_SCRIPT_SHA256="48ebe58d6b5483772a63f858bdfc0ef240555ed971a3b1db1743935c2ee7be3e"
readonly GDK_REPOSITORY="Weather-OS/GDK-Proton"
readonly GDK_RELEASE="release10-32"
readonly GDK_NAME="GDK-Proton10-32"
readonly GDK_SHA256="1e80f4e714f877f42101d5775bd38ca0a15a38d304e24af1f15c6deec4ebac2d"
readonly GDK_THREADING_SHA256="4c3e3faf5bcd86779e4a69677902dd8b36a16e1136a5901e616c36d8a02b68f6"
readonly PROTON_PATCH="$SRC/third_party/gdk-proton-arm64/0001-proton-install-the-GDK-runtime.patch"
readonly PROTON_PATCH_SHA256="aba6a4fd8c33c5990c57d3eb32c596b951ecb5f6cb5853f1986d4ba282b45f3e"
readonly EPOCH="1784308597"
readonly TOP="GDK-Proton-xuser-arm64"
# The x64 PE files GDK-Proton's proton script copies into system32, less the
# three WineGDK builtins (xgameruntime, windows.ui.core.textinput,
# windows.devices.enumeration), which come from our aarch64-windows build.
readonly -a GDK_X64_FILES=(
  libasprintf-0.dll libbrotlicommon.dll libbrotlidec.dll libcares-2.dll
  libcharset-1.dll libcrypto-3-x64.dll libiconv-2.dll libidn2-0.dll
  libintl-8.dll libnghttp2-14.dll libnghttp3-9.dll
  libngtcp2_crypto_gnutls-8.dll libngtcp2_crypto_ossl.dll
  libngtcp2_crypto_ossl-0.dll libngtcp2-16.dll libpsl-5.dll libssh2-1.dll
  libssl-3-x64.dll libunistring-5.dll libzstd.dll zlib1.dll
  xgameruntime.dll.threading
)
readonly -a ARM64X_MODULES=(
  ntdll combase user32 xgameruntime windows.storage
  windows.storage.applicationdata windows.ui.core.textinput
  windows.devices.enumeration
)

BUILD_REV="$(pin WINEGDK_ARM64_BUILD_REV)"
[[ "$BUILD_REV" =~ ^arm64ec-native[0-9]+$ ]] \
  || die "WINEGDK_ARM64_BUILD_REV in bol/config.py must name the revision (arm64ec-native<n>), not '$BUILD_REV'"
WINEGDK_COMMIT="$(pin WINEGDK_SOURCE_COMMIT)"
[[ "$(uname -m)" == aarch64 ]] || echo "   (packaging on $(uname -m): the smoke tests need an aarch64 host)"
for tool in tar gzip sha256sum python3 patch cmp objdump; do
  command -v "$tool" >/dev/null || die "need $tool"
done

echo "== Verifying the inputs"
verify_sha256 "$GE_ARCHIVE" "$GE_SHA256" "$GE_NAME.tar.gz"
verify_sha256 "$GDK_ARCHIVE" "$GDK_SHA256" "$GDK_NAME.tar.gz"
verify_sha256 "$PROTON_PATCH" "$PROTON_PATCH_SHA256" "GDK proton patch"
BUILD_RECORD="$WINEGDK_PREFIX/.bol-winegdk-build.env"
PACKAGE_VERSIONS="$WINEGDK_PREFIX/.bol-winegdk-package-versions.tsv"
[[ -f "$BUILD_RECORD" && -f "$PACKAGE_VERSIONS" ]] || die "WINEGDK_PREFIX has no build provenance"
grep -qx "winegdk_commit=$WINEGDK_COMMIT" "$BUILD_RECORD" || die "the prefix is not WineGDK $WINEGDK_COMMIT"
grep -qx "source_manifest_sha256=$(pin WINEGDK_SOURCE_MANIFEST_SHA256)" "$BUILD_RECORD" \
  || die "the prefix was not built from the reviewed source manifest"
grep -qx "target=aarch64" "$BUILD_RECORD" || die "the prefix is not the aarch64 build"
( cd "$VKD3D_ARM64X_DIR" && sha256sum --strict -c "$SRC/third_party/vkd3d-proton-arm64x/OUTPUT-SHA256SUMS" ) \
  || die "the ARM64X vkd3d payload is not the pinned one"
( cd "$VKD3D_UNIVERSAL_DIR" && sha256sum --strict -c "$SRC/third_party/vkd3d-proton-universal/OUTPUT-SHA256SUMS" ) \
  || die "the x86 vkd3d payload is not the pinned one"

mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd -P)"
STAGE="$(mktemp -d "$OUT/.stage.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
ENGINE="$STAGE/$TOP"

echo "== Unpacking $GE_NAME"
tar -xzf "$GE_ARCHIVE" -C "$STAGE"
mv "$STAGE/$GE_NAME" "$ENGINE"
[[ -L "$ENGINE/files/bin" && "$(readlink "$ENGINE/files/bin")" == bin-arm64 ]] \
  || die "the base has no files/bin -> bin-arm64 layout"
verify_sha256 "$ENGINE/proton" "$GE_PROTON_SCRIPT_SHA256" "base proton script"

echo "== Overlaying the WineGDK prefix"
# One directory at a time: files/bin is a symlink to bin-arm64, which a
# whole-tree copy would replace with a directory.
cp -a --remove-destination "$WINEGDK_PREFIX/bin/." "$ENGINE/files/bin-arm64/"
cp -a --remove-destination "$WINEGDK_PREFIX/lib/." "$ENGINE/files/lib/"
cp -a --remove-destination "$WINEGDK_PREFIX/share/." "$ENGINE/files/share/"
rm -rf "$ENGINE/files/include" "$ENGINE/files/share/wine/mono"
find "$ENGINE/files/lib/wine" -type f -name '*.a' -delete
find "$ENGINE" -type f \( -name '*.pyc' -o -name '.DS_Store' \) -delete
for tool in wine wineserver; do
  cmp -s "$WINEGDK_PREFIX/bin/$tool" "$ENGINE/files/bin-arm64/$tool" \
    || die "files/bin-arm64/$tool is not WineGDK's"
done
grep -qa "/dev/ntsync" "$ENGINE/files/bin-arm64/wineserver" || die "wineserver has no ntsync path"
grep -qa "WINEFSYNC" "$ENGINE/files/bin-arm64/wineserver" || die "wineserver has no fsync fallback (0014)"

# Every module Proton's ARM64 Wine ships in these directories must now be
# WineGDK's, or two Wine ABIs would meet in one process (#180). Proton's own
# modules (lsteamclient, vrclient, wineopenxr, steam.exe...) are the
# exception: WineGDK has no such module, and they talk to Wine through the
# stable syscall ABI.
python3 - "$ENGINE/files/lib/wine" "$WINEGDK_PREFIX/lib/wine" <<'PY'
import sys
from pathlib import Path

engine, ours = Path(sys.argv[1]), Path(sys.argv[2])
for arch in ("aarch64-unix", "aarch64-windows", "i386-windows", "x86_64-windows"):
    built = {p.name for p in (ours / arch).iterdir()}
    if not built:
        raise SystemExit(f"!! the prefix built nothing for {arch}")
    kept = sorted(p.name for p in (engine / arch).iterdir() if p.name not in built)
    print(f"   {arch}: {len(built)} WineGDK modules, {len(kept)} base-only")
for name in ("winewayland.drv", "win32u.dll", "ntdll.dll"):
    a = (engine / "aarch64-windows" / name).read_bytes()
    if a != (ours / "aarch64-windows" / name).read_bytes():
        raise SystemExit(f"!! aarch64-windows/{name} is not WineGDK's")
PY

echo "== Checking the ARM64X system modules"
python3 - "$ENGINE/files/lib/wine/aarch64-windows" "${ARM64X_MODULES[@]}" <<'PY'
import struct
import sys
from pathlib import Path


def arm64x(data):
    e = struct.unpack_from("<I", data, 0x3C)[0]
    if struct.unpack_from("<H", data, e + 4)[0] != 0xAA64:
        return False
    opt = e + 24
    rva, _ = struct.unpack_from("<II", data, opt + 112 + 8 * 10)
    nsec = struct.unpack_from("<H", data, e + 6)[0]
    sect = opt + struct.unpack_from("<H", data, e + 20)[0]
    for i in range(nsec):
        vsz, va, rsz, raw = struct.unpack_from("<IIII", data, sect + 40 * i + 8)
        if va <= rva < va + max(vsz, rsz):
            off = raw + rva - va
            return (struct.unpack_from("<I", data, off)[0] >= 0xD0
                    and struct.unpack_from("<Q", data, off + 0xC8)[0] != 0)
    return False


root = Path(sys.argv[1])
for module in sys.argv[2:]:
    data = (root / f"{module}.dll").read_bytes()
    if not arm64x(data):
        raise SystemExit(f"!! aarch64-windows/{module}.dll is not ARM64X")
    if data[0x40:0x50] != b"Wine builtin DLL":
        raise SystemExit(f"!! aarch64-windows/{module}.dll lost its Wine builtin mark")
for fex in ("libarm64ecfex.dll", "libwow64fex.dll"):
    if not (root / fex).is_file():
        raise SystemExit(f"!! the base has no {fex}: x64 code could not run")
print(f"   {len(sys.argv) - 2} ARM64X modules, FEX present")
PY
xgdk="$ENGINE/files/lib/wine/aarch64-windows/xgameruntime.dll"
for marker in "native XGame identity loaded: TitleId" \
              "native Xbox app configuration ready" \
              "using user-only XSTS for Xbox Achievements." \
              "no store service available, failing the" \
              "requesting token: method=" \
              "unsupported GDK QueryApiImpl class" \
              "short write of an event record"; do
  grep -qaF "$marker" "$xgdk" || die "aarch64 xgameruntime.dll lacks \"$marker\""
done

echo "== Installing the GDK x64 libraries from $GDK_NAME"
GDK_EXTRACT="$STAGE/gdk"
mkdir -p "$GDK_EXTRACT"
members=()
for f in "${GDK_X64_FILES[@]}"; do
  members+=("$GDK_NAME/files/lib/wine/x86_64-windows/$f")
done
tar -xzf "$GDK_ARCHIVE" -C "$GDK_EXTRACT" -- "${members[@]}"
GDK_SUMS="$STAGE/gdk-x64-SHA256SUMS"
: > "$GDK_SUMS"
for f in "${GDK_X64_FILES[@]}"; do
  source="$GDK_EXTRACT/$GDK_NAME/files/lib/wine/x86_64-windows/$f"
  python3 - "$source" <<'PY' || die "$f is not an x64 PE file"
import struct, sys
d = open(sys.argv[1], "rb").read()
e = struct.unpack_from("<I", d, 0x3C)[0]
sys.exit(0 if d[:2] == b"MZ" and struct.unpack_from("<H", d, e + 4)[0] == 0x8664 else 1)
PY
  install -D -m 0755 "$source" "$ENGINE/files/lib/wine/x86_64-windows/$f"
  printf '%s  files/lib/wine/x86_64-windows/%s\n' "$(sha "$source")" "$f" >> "$GDK_SUMS"
done
verify_sha256 "$ENGINE/files/lib/wine/x86_64-windows/xgameruntime.dll.threading" \
  "$GDK_THREADING_SHA256" "native XThreading runtime"

echo "== Applying the GDK change to the proton script"
( cd "$ENGINE" && patch -p1 --no-backup-if-mismatch -s < "$PROTON_PATCH" )
python3 -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' "$ENGINE/proton"
grep -q '"xgameruntime.dll.threading"' "$ENGINE/proton" || die "the proton patch did not land"

echo "== Installing vkd3d-proton (ARM64X + x86)"
VKD3D_ROOT="$ENGINE/files/lib/wine/vkd3d-proton"
for pair in aarch64-windows:"$VKD3D_ARM64X_DIR/aarch64" \
            x86_64-windows:"$VKD3D_UNIVERSAL_DIR/x64" \
            i386-windows:"$VKD3D_UNIVERSAL_DIR/x86"; do
  arch="${pair%%:*}" from="${pair#*:}"
  mkdir -p "$VKD3D_ROOT/$arch"
  for dll in d3d12.dll d3d12core.dll; do
    for slot in "" .bol-ext-dgc .bol-nv-dgc; do
      rm -f "$VKD3D_ROOT/$arch/$dll$slot"
      install -m 0644 "$from/$dll" "$VKD3D_ROOT/$arch/$dll$slot"
    done
  done
  for dgc in VK_EXT_device_generated_commands VK_NV_device_generated_commands; do
    grep -qaF "$dgc" "$VKD3D_ROOT/$arch/d3d12core.dll" || die "$arch vkd3d has no $dgc"
  done
done
# Proton reads its vkd3d version from here for the log; say which it is.
printf 'vkd3d-proton 3.0.1-bol-dgc (ARM64X: %s)\n' \
  "$(sha "$SRC/third_party/vkd3d-proton-arm64x/OUTPUT-SHA256SUMS" | cut -c1-12)" \
  > "$VKD3D_ROOT/version"

echo "== Writing provenance"
PROV_REL="files/share/bedrock-on-linux/licenses-and-provenance"
PROV="$ENGINE/$PROV_REL"
mkdir -p "$PROV/winegdk/native5" "$PROV/vkd3d-proton-arm64x" \
  "$PROV/vkd3d-proton-universal" "$PROV/gdk-proton-base" "$PROV/ge-proton-base" \
  "$PROV/gdk-proton-arm64"
install -m 0644 "$BUILD_RECORD" "$PACKAGE_VERSIONS" "$PROV/winegdk/"
install -m 0644 "$SRC/third_party/vkd3d-proton-universal/COPYING.LGPL-2.1" "$PROV/winegdk/"
install -m 0644 "$SRC"/third_party/winegdk-native5/*.patch \
  "$SRC/third_party/winegdk-native5/README.md" \
  "$SRC/third_party/winegdk-native5/SOURCE-SHA256SUMS" "$PROV/winegdk/native5/"
cp -a "$VKD3D_ARM64X_DIR/provenance/." "$PROV/vkd3d-proton-arm64x/"
cp -a "$VKD3D_UNIVERSAL_DIR/provenance/." "$PROV/vkd3d-proton-universal/"
install -m 0644 "$PROTON_PATCH" "$PROV/gdk-proton-arm64/"
install -m 0644 "$GDK_SUMS" "$PROV/gdk-proton-base/x64-SHA256SUMS"
cat > "$PROV/gdk-proton-base/provenance.env" <<EOF
repository=$GDK_REPOSITORY
release=$GDK_RELEASE
archive=$GDK_NAME.tar.gz
archive_sha256=$GDK_SHA256
threading_dll_sha256=$GDK_THREADING_SHA256
EOF
cat > "$PROV/ge-proton-base/provenance.env" <<EOF
repository=$GE_REPOSITORY
release=$GE_RELEASE
archive=$GE_NAME.tar.gz
archive_sha256=$GE_SHA256
proton_script_sha256=$GE_PROTON_SCRIPT_SHA256
EOF
find "$PROV" -type f -exec chmod 0644 {} +

echo "== Writing engine-manifest.json"
python3 - "$ENGINE" "$BUILD_REV" "$WINEGDK_COMMIT" "$PROV_REL" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

root, build_rev, winegdk_commit, prov_rel = Path(sys.argv[1]), *sys.argv[2:5]


def digest(relative):
    path = root / relative
    resolved = path.resolve(strict=True)
    resolved.relative_to(root.resolve())
    if not resolved.is_file():
        raise SystemExit(f"!! non-file critical engine entry: {relative}")
    return hashlib.sha256(resolved.read_bytes()).hexdigest()


critical = [
    "proton",
    "toolmanifest.vdf",
    "files/bin-arm64/wine",
    "files/bin-arm64/wineserver",
    "files/share/wine/wine.inf",
    "files/lib/wine/aarch64-unix/ntdll.so",
    "files/lib/wine/aarch64-windows/libarm64ecfex.dll",
    "files/lib/wine/aarch64-windows/libwow64fex.dll",
    "files/lib/wine/x86_64-windows/xgameruntime.dll.threading",
]
critical += [f"files/lib/wine/aarch64-windows/{m}.dll" for m in (
    "ntdll", "combase", "user32", "xgameruntime", "windows.storage",
    "windows.storage.applicationdata")]
critical += [f"files/lib/wine/i386-windows/{m}.dll" for m in (
    "ntdll", "combase", "xgameruntime")]
critical += sorted(p.relative_to(root).as_posix()
                   for p in (root / prov_rel).rglob("*") if p.is_file())

variants = {}
for variant, suffix in (("ext-dgc", ".bol-ext-dgc"), ("nv-dgc", ".bol-nv-dgc")):
    files = {}
    for arch in ("aarch64-windows", "x86_64-windows", "i386-windows"):
        for dll in ("d3d12.dll", "d3d12core.dll"):
            rel = f"files/lib/wine/vkd3d-proton/{arch}/{dll}{suffix}"
            files[rel] = digest(rel)
    variants[variant] = {"files": files}

manifest = {
    "schema": 1,
    "arch": "aarch64",
    "build_rev": build_rev,
    "critical_files": {relative: digest(relative) for relative in critical},
    "components": {
        "base": "GE-Proton11-14-aarch64",
        "vkd3d_ext_dgc": "3.0.1-bol-dgc",
        "vkd3d_nv_dgc": "3.0.1-bol-dgc",
        "emulator": "FEX (libarm64ecfex.dll, from the base)",
    },
    "provenance": {
        "winegdk": {"commit": winegdk_commit,
                    "archs": "arm64ec,aarch64,i386,x86_64"},
        "build": {"glibc_max": "2.41", "runtime": "steamrt4 (SLR 4 arm64)"},
    },
    "variants": variants,
}
(root / "engine-manifest.json").write_text(
    json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(f"   {len(manifest['critical_files'])} critical files, 2 vkd3d variants")
PY
chmod 0644 "$ENGINE/engine-manifest.json"

echo "== Packing"
ASSET="GDK-Proton-xuser-$BUILD_REV.tar.gz"
tar --sort=name --format=gnu --hard-dereference \
  --mtime="@$EPOCH" --owner=0 --group=0 --numeric-owner \
  -C "$STAGE" -cf - "$TOP" | gzip -n -6 > "$OUT/$ASSET.part"
mv -f "$OUT/$ASSET.part" "$OUT/$ASSET"
( cd "$OUT" && sha256sum "$ASSET" > "$ASSET.sha256" )
echo "   $OUT/$ASSET ($(du -h "$OUT/$ASSET" | cut -f1))"
echo "   WINEGDK_ARM64_ARCHIVE_SHA256 = \"$(sha "$OUT/$ASSET")\""
echo "== Candidate created; nothing was tagged, uploaded or released."
