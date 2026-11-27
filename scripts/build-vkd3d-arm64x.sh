#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Build the experimental ARM64EC engine's vkd3d-proton (#250, docs/ARM64EC.md):
# the x86 payload's exact source (v3.0.1 + restored NV-DGC + the occlusion and
# frame-limiter fixes) compiled as ARM64X, plus one ARM64X-only backport
# (hybrid_patchable command-queue vtable), so that the x64 game, running as
# ARM64EC under FEX, calls native ARM code for every D3D12 call instead of
# emulating the translation layer too.
#
# Run in Valve's steamrt4 arm64 SDK image (third_party/vkd3d-proton-arm64x/
# provenance.env), which holds llvm-mingw, meson, ninja and glslang. The x86
# payload's recipe, build-vkd3d-universal.sh, and its provenance directory stay
# untouched: both are part of the published x86 engine's bytes.
#
#   build-vkd3d-arm64x.sh WORK_ROOT
#
# Creates WORK_ROOT/vkd3d-build-output/vkd3d-proton-3.0.1-nv-dgc/aarch64/
# {d3d12,d3d12core}.dll. When OUTPUT-SHA256SUMS in the arm64x directory exists
# the DLLs must match it; until then the build prints their hashes, the first
# pass of the two-pass pin. It never publishes anything.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIVERSAL_DIR="$PROJECT_ROOT/third_party/vkd3d-proton-universal"
ARM_DIR="$PROJECT_ROOT/third_party/vkd3d-proton-arm64x"
UNIVERSAL_LOCK="$UNIVERSAL_DIR/provenance.env"
ARM_LOCK="$ARM_DIR/provenance.env"
CROSS_FILE="$ARM_DIR/build-arm64x.txt"
HYBRID_PATCH="$ARM_DIR/hybrid-patchable-command-queue.patch"
EXPECTED_HASHES="$ARM_DIR/OUTPUT-SHA256SUMS"
SUBMODULE_LOCK="$UNIVERSAL_DIR/submodules.lock"
REVERT_PATCH="$UNIVERSAL_DIR/restore-nv-dgc.patch"
OCCLUSION_PATCH="$UNIVERSAL_DIR/fix-occluded-frame-latency.patch"
LIMITER_PATCH="$UNIVERSAL_DIR/fix-frame-limiter-busy-wait.patch"
LICENSE_FILE="$UNIVERSAL_DIR/COPYING.LGPL-2.1"

die() {
  echo "!! $*" >&2
  exit 1
}

[[ $# -eq 1 ]] || die "usage: $0 WORK_ROOT"
for f in "$UNIVERSAL_LOCK" "$ARM_LOCK" "$CROSS_FILE" "$HYBRID_PATCH" "$SUBMODULE_LOCK" \
         "$REVERT_PATCH" "$OCCLUSION_PATCH" "$LIMITER_PATCH" "$LICENSE_FILE" \
         "$ARM_DIR/README.md"; do
  [[ -f "$f" ]] || die "incomplete vkd3d provenance bundle: ${f#"$PROJECT_ROOT"/}"
done

# shellcheck disable=SC1090
source "$UNIVERSAL_LOCK"
# shellcheck disable=SC1090
source "$ARM_LOCK"

[[ "$(uname -m)" == aarch64 ]] || die "build on an aarch64 host, not $(uname -m)"

WORK_ROOT="$(realpath -m -- "$1")"
[[ "$WORK_ROOT" != "/" ]] || die "WORK_ROOT must not be /"
if [[ -e "$WORK_ROOT" ]]; then
  [[ -d "$WORK_ROOT" ]] || die "WORK_ROOT is not a directory: $WORK_ROOT"
  [[ -z "$(find "$WORK_ROOT" -mindepth 1 -maxdepth 1 -print -quit)" ]] ||
    die "WORK_ROOT is not empty: $WORK_ROOT"
fi
SOURCE_DIR="$WORK_ROOT/$VKD3D_SOURCE_DIR_NAME"
BUILD_PARENT="$WORK_ROOT/$VKD3D_BUILD_PARENT_NAME"
OUTPUT_DIR="$BUILD_PARENT/$VKD3D_OUTPUT_DIR_NAME"
ARCH_DIR="$VKD3D_ARM64X_OUTPUT_DIR"

missing=()
for tool in git meson ninja python3 sha256sum cmp diff find install awk sed \
  grep realpath arm64ec-w64-mingw32-gcc arm64ec-w64-mingw32-g++ \
  arm64ec-w64-mingw32-ar arm64ec-w64-mingw32-strip arm64ec-w64-mingw32-widl; do
  command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done
command -v glslang >/dev/null 2>&1 || command -v glslangValidator >/dev/null 2>&1 \
  || missing+=("glslang")
[[ ${#missing[@]} -eq 0 ]] || die "the build image lacks: ${missing[*]}"

hash_file() {
  sha256sum "$1" | awk '{print $1}'
}
assert_equal() {
  [[ "$2" == "$3" ]] || die "$1 drift: got '$2', expected '$3'"
}
assert_equal "cross file" "$(hash_file "$CROSS_FILE")" "$VKD3D_ARM64X_CROSS_FILE_SHA256"
assert_equal "vendored hybrid_patchable backport" "$(hash_file "$HYBRID_PATCH")" \
  "$VKD3D_ARM64X_HYBRID_PATCH_SHA256"
assert_equal "vendored revert patch" "$(hash_file "$REVERT_PATCH")" "$VKD3D_REVERT_PATCH_SHA256"
assert_equal "vendored occlusion patch" "$(hash_file "$OCCLUSION_PATCH")" "$VKD3D_OCCLUSION_PATCH_SHA256"
assert_equal "vendored frame-limiter patch" "$(hash_file "$LIMITER_PATCH")" "$VKD3D_LIMITER_PATCH_SHA256"
assert_equal "submodule lock" "$(hash_file "$SUBMODULE_LOCK")" "$VKD3D_SUBMODULE_LOCK_SHA256"

mkdir -p "$WORK_ROOT"
mkdir "$BUILD_PARENT"
export LC_ALL=C LANG=C TZ=UTC PYTHONHASHSEED=0 ZERO_AR_DATE=1
# ccache comes first on the SDK's PATH; never read a cache from elsewhere.
export CCACHE_DISABLE=1

echo "== Cloning pinned vkd3d-proton source"
git clone --no-checkout "${BOL_VKD3D_GIT_URL:-$VKD3D_UPSTREAM_URL}" "$SOURCE_DIR"
git -C "$SOURCE_DIR" config core.autocrlf false
git -C "$SOURCE_DIR" config core.eol lf
git -C "$SOURCE_DIR" checkout --detach "$VKD3D_BASE_COMMIT"
git -C "$SOURCE_DIR" submodule sync --recursive
git -C "$SOURCE_DIR" submodule update --init --recursive
assert_equal "vkd3d base commit" "$(git -C "$SOURCE_DIR" rev-parse HEAD)" "$VKD3D_BASE_COMMIT"

ACTUAL_SUBMODULES="$WORK_ROOT/submodules.actual"
git -C "$SOURCE_DIR" submodule status --recursive | while IFS= read -r line; do
  [[ "${line:0:1}" == " " ]] || die "submodule is not at its recorded commit: $line"
  read -r commit path _ <<< "${line:1}"
  printf '%s %s\n' "$commit" "$path"
done > "$ACTUAL_SUBMODULES"
diff -u "$SUBMODULE_LOCK" "$ACTUAL_SUBMODULES" || die "submodule lock mismatch"

echo "== Restoring NV-DGC ($VKD3D_REMOVAL_COMMIT)"
git -C "$SOURCE_DIR" revert --no-commit "$VKD3D_REMOVAL_COMMIT"
GENERATED_PATCH="$WORK_ROOT/restore-nv-dgc.generated.patch"
git -C "$SOURCE_DIR" -c core.quotePath=true -c diff.mnemonicPrefix=false \
  -c diff.noprefix=false -c core.abbrev=7 \
  diff --cached --binary --no-ext-diff --no-textconv > "$GENERATED_PATCH"
cmp "$REVERT_PATCH" "$GENERATED_PATCH" || die \
  "vendored revert patch differs from git revert output"

echo "== Applying the occluded frame-latency and frame-limiter fixes"
git -C "$SOURCE_DIR" apply --index --whitespace=nowarn "$OCCLUSION_PATCH" \
  || die "occlusion patch does not apply"
git -C "$SOURCE_DIR" apply --index --whitespace=nowarn "$LIMITER_PATCH" \
  || die "frame-limiter patch does not apply"
echo "== Backporting hybrid_patchable on the command queue vtable (ARM64X only)"
git -C "$SOURCE_DIR" apply --index --whitespace=nowarn "$HYBRID_PATCH" \
  || die "hybrid_patchable backport does not apply"
install -m 0644 "$CROSS_FILE" "$SOURCE_DIR/build-arm64x.txt"

echo "== Toolchain"
arm64ec-w64-mingw32-gcc --version | sed -n 1p
echo "meson $(meson --version), ninja $(ninja --version)"

echo "== Building the ARM64X payload (SOURCE_DATE_EPOCH=$VKD3D_SOURCE_DATE_EPOCH_ARM64X)"
build_dir="$OUTPUT_DIR/build.arm64x"
(
  cd "$SOURCE_DIR"
  SOURCE_DATE_EPOCH="$VKD3D_SOURCE_DATE_EPOCH_ARM64X" meson setup \
    --cross-file build-arm64x.txt --buildtype release --prefix "$OUTPUT_DIR" \
    --strip --bindir "$ARCH_DIR" --libdir "$ARCH_DIR" "$build_dir"
)
SOURCE_DATE_EPOCH="$VKD3D_SOURCE_DATE_EPOCH_ARM64X" ninja -C "$build_dir" install
find "$OUTPUT_DIR/$ARCH_DIR" -maxdepth 1 -type f ! -name '*.dll' -delete
rm -rf "$build_dir"
# The linker's or strip's clock must not leak into the bytes.
python3 "$PROJECT_ROOT/scripts/pe-stamp.py" "$VKD3D_SOURCE_DATE_EPOCH_ARM64X" \
  "$OUTPUT_DIR/$ARCH_DIR"

echo "== Checking the DLLs are ARM64X with both DGC paths"
for dll in d3d12.dll d3d12core.dll; do
  path="$OUTPUT_DIR/$ARCH_DIR/$dll"
  [[ -f "$path" ]] || die "missing output: $ARCH_DIR/$dll"
  # ARM64X: an ARM64 header plus the hybrid (CHPE) metadata of its ARM64EC
  # half in the load configuration; plain ARM64 has no such pointer.
  python3 - "$path" <<'PEPY' || die "$dll is not ARM64X"
import struct, sys
d = open(sys.argv[1], "rb").read()
e = struct.unpack_from("<I", d, 0x3C)[0]
if struct.unpack_from("<H", d, e + 4)[0] != 0xAA64:
    sys.exit(1)
opt = e + 24
rva, _ = struct.unpack_from("<II", d, opt + 112 + 8 * 10)
nsec = struct.unpack_from("<H", d, e + 6)[0]
sect = opt + struct.unpack_from("<H", d, e + 20)[0]
for i in range(nsec):
    vsz, va, rsz, raw = struct.unpack_from("<IIII", d, sect + 40 * i + 8)
    if va <= rva < va + max(vsz, rsz):
        off = raw + rva - va
        ok = (struct.unpack_from("<I", d, off)[0] >= 0xD0
              and struct.unpack_from("<Q", d, off + 0xC8)[0] != 0)
        sys.exit(0 if ok else 1)
sys.exit(1)
PEPY
done
for dgc in VK_EXT_device_generated_commands VK_NV_device_generated_commands; do
  grep -qaF "$dgc" "$OUTPUT_DIR/$ARCH_DIR/d3d12core.dll" || die "no $dgc path"
done

echo "== Output hashes"
( cd "$OUTPUT_DIR" && sha256sum "$ARCH_DIR/d3d12.dll" "$ARCH_DIR/d3d12core.dll" )
if [[ -s "$EXPECTED_HASHES" ]]; then
  ( cd "$OUTPUT_DIR" && sha256sum -c "$EXPECTED_HASHES" ) \
    || die "the ARM64X DLLs do not reproduce OUTPUT-SHA256SUMS"
  echo "reproduced $EXPECTED_HASHES"
else
  echo "no OUTPUT-SHA256SUMS yet: commit the two lines above to pin them"
fi

PROVENANCE_OUT="$OUTPUT_DIR/provenance"
mkdir -p "$PROVENANCE_OUT"
install -m 0644 "$UNIVERSAL_LOCK" "$PROVENANCE_OUT/provenance.env"
install -m 0644 "$ARM_LOCK" "$PROVENANCE_OUT/provenance-arm64x.env"
install -m 0644 "$CROSS_FILE" "$SUBMODULE_LOCK" "$REVERT_PATCH" \
  "$OCCLUSION_PATCH" "$LIMITER_PATCH" "$HYBRID_PATCH" "$LICENSE_FILE" \
  "$PROVENANCE_OUT/"
install -m 0644 "$ARM_DIR/README.md" "$PROVENANCE_OUT/README.md"
install -m 0644 "$PROJECT_ROOT/scripts/build-vkd3d-arm64x.sh" "$PROVENANCE_OUT/"
[[ ! -s "$EXPECTED_HASHES" ]] || install -m 0644 "$EXPECTED_HASHES" "$PROVENANCE_OUT/"
{
  printf 'git: %s\n' "$(git --version)"
  printf 'meson: %s\n' "$(meson --version)"
  printf 'ninja: %s\n' "$(ninja --version)"
  printf 'compiler: %s\n' "$(arm64ec-w64-mingw32-gcc --version | sed -n 1p)"
  printf 'image: %s\n' "$VKD3D_ARM64X_SDK_IMAGE"
} > "$PROVENANCE_OUT/BUILD-TOOLS.txt"

echo "== ARM64X vkd3d payload ready: $OUTPUT_DIR/$ARCH_DIR"
