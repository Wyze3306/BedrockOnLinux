#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Rebuild the exact universal EXT+NV DGC vkd3d-proton payload reviewed for r11.
# This script only creates files below the caller-supplied work root.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROVENANCE_DIR="$PROJECT_ROOT/third_party/vkd3d-proton-universal"
LOCK_FILE="$PROVENANCE_DIR/provenance.env"
SUBMODULE_LOCK="$PROVENANCE_DIR/submodules.lock"
EXPECTED_HASHES="$PROVENANCE_DIR/OUTPUT-SHA256SUMS"
REVERT_PATCH="$PROVENANCE_DIR/restore-nv-dgc.patch"
OCCLUSION_PATCH="$PROVENANCE_DIR/fix-occluded-frame-latency.patch"
LIMITER_PATCH="$PROVENANCE_DIR/fix-frame-limiter-busy-wait.patch"
LICENSE_FILE="$PROVENANCE_DIR/COPYING.LGPL-2.1"
README_FILE="$PROVENANCE_DIR/README.md"
BUILD_RECIPE="$PROJECT_ROOT/scripts/build-vkd3d-universal.sh"

die() {
  echo "!! $*" >&2
  exit 1
}

usage() {
  cat <<EOF
Usage: $0 WORK_ROOT

Creates the canonical, byte-reproducible build layout below WORK_ROOT:
  WORK_ROOT/vkd3d-proton-r9
  WORK_ROOT/vkd3d-build-output/vkd3d-proton-3.0.1-nv-dgc

WORK_ROOT must be absent or empty. The build downloads the pinned
upstream Git repository and its pinned submodules, but never publishes anything.
See third_party/vkd3d-proton-universal/README.md for the locked toolchain.
EOF
}

[[ $# -eq 1 ]] || { usage >&2; exit 2; }
[[ -f "$LOCK_FILE" && -f "$SUBMODULE_LOCK" && \
   -f "$EXPECTED_HASHES" && -f "$REVERT_PATCH" && \
   -f "$OCCLUSION_PATCH" && -f "$LIMITER_PATCH" && \
   -f "$LICENSE_FILE" && -f "$README_FILE" && \
   -f "$BUILD_RECIPE" ]] || die "incomplete vkd3d provenance bundle"

# shellcheck disable=SC1090
source "$LOCK_FILE"

WORK_ROOT="$(realpath -m -- "$1")"
[[ "$WORK_ROOT" != "/" ]] || die "WORK_ROOT must not be /"
SOURCE_DIR="$WORK_ROOT/$VKD3D_SOURCE_DIR_NAME"
BUILD_PARENT="$WORK_ROOT/$VKD3D_BUILD_PARENT_NAME"
OUTPUT_DIR="$BUILD_PARENT/$VKD3D_OUTPUT_DIR_NAME"

if [[ -e "$WORK_ROOT" ]]; then
  [[ -d "$WORK_ROOT" ]] || die "WORK_ROOT is not a directory: $WORK_ROOT"
  [[ -z "$(find "$WORK_ROOT" -mindepth 1 -maxdepth 1 -print -quit)" ]] ||
    die "WORK_ROOT is not empty: $WORK_ROOT"
fi

require_tool() {
  command -v "$1" >/dev/null 2>&1 || die "required tool is missing: $1"
}

for tool in git meson ninja python3 sha256sum cmp diff strings find install \
  awk sed grep readlink realpath mkdir \
  x86_64-w64-mingw32-gcc x86_64-w64-mingw32-g++ \
  x86_64-w64-mingw32-ar x86_64-w64-mingw32-strip \
  x86_64-w64-mingw32-ld x86_64-w64-mingw32-widl \
  i686-w64-mingw32-gcc i686-w64-mingw32-g++ i686-w64-mingw32-ar \
  i686-w64-mingw32-strip i686-w64-mingw32-ld i686-w64-mingw32-widl; do
  require_tool "$tool"
done

if command -v glslang >/dev/null 2>&1; then
  GLSLANG=glslang
elif command -v glslangValidator >/dev/null 2>&1; then
  GLSLANG=glslangValidator
else
  die "required tool is missing: glslang or glslangValidator"
fi

# vkd3d checks these host names before consulting the architecture-specific
# fallback pinned by build-win{32,64}.txt. Refuse an ambiguous Meson input.
for unprefixed_widl in widl widl-stable; do
  if command -v "$unprefixed_widl" >/dev/null 2>&1; then
    die "unexpected unprefixed IDL compiler on PATH: $unprefixed_widl"
  fi
done

assert_equal() {
  local label="$1" actual="$2" expected="$3"
  [[ "$actual" == "$expected" ]] || die \
    "$label drift: got '$actual', expected '$expected'"
}

assert_prefix() {
  local label="$1" actual="$2" expected="$3"
  [[ "$actual" == "$expected"* ]] || die \
    "$label drift: got '$actual', expected prefix '$expected'"
}

hash_file() {
  sha256sum "$1" | awk '{print $1}'
}

assert_equal "vendored revert patch" "$(hash_file "$REVERT_PATCH")" \
  "$VKD3D_REVERT_PATCH_SHA256"
assert_equal "vendored occlusion patch" "$(hash_file "$OCCLUSION_PATCH")" \
  "$VKD3D_OCCLUSION_PATCH_SHA256"
assert_equal "vendored frame-limiter patch" "$(hash_file "$LIMITER_PATCH")" \
  "$VKD3D_LIMITER_PATCH_SHA256"
assert_equal "upstream licence" "$(hash_file "$LICENSE_FILE")" \
  "$VKD3D_LICENSE_SHA256"
assert_equal "submodule lock" "$(hash_file "$SUBMODULE_LOCK")" \
  "$VKD3D_SUBMODULE_LOCK_SHA256"
assert_equal "output hash manifest" "$(hash_file "$EXPECTED_HASHES")" \
  "$VKD3D_OUTPUT_HASHES_SHA256"

first_line() {
  "$@" 2>&1 | sed -n '1p'
}

assert_equal "Meson" "$(meson --version)" "$VKD3D_MESON_VERSION"
assert_prefix "Ninja" "$(ninja --version)" "$VKD3D_NINJA_VERSION_PREFIX"
assert_equal "Python" "$(python3 --version)" "$VKD3D_PYTHON_VERSION"
assert_equal "Git" "$(git --version)" "$VKD3D_GIT_VERSION"
assert_equal "glslang" "$(first_line "$GLSLANG" --version)" \
  "$VKD3D_GLSLANG_VERSION"
assert_equal "x64 GCC" "$(first_line x86_64-w64-mingw32-gcc --version)" \
  "$VKD3D_X64_GCC_VERSION"
assert_equal "x86 GCC" "$(first_line i686-w64-mingw32-gcc --version)" \
  "$VKD3D_X86_GCC_VERSION"
assert_equal "x64 G++" "$(first_line x86_64-w64-mingw32-g++ --version)" \
  "$VKD3D_X64_GXX_VERSION"
assert_equal "x86 G++" "$(first_line i686-w64-mingw32-g++ --version)" \
  "$VKD3D_X86_GXX_VERSION"
for tool in x86_64-w64-mingw32-ld i686-w64-mingw32-ld \
            x86_64-w64-mingw32-ar i686-w64-mingw32-ar \
            x86_64-w64-mingw32-strip i686-w64-mingw32-strip; do
  first="$(first_line "$tool" --version)"
  [[ "$first" == *" $VKD3D_BINUTILS_VERSION" ]] || die \
    "$tool drift: got '$first', expected Binutils $VKD3D_BINUTILS_VERSION"
done

hash_tool() {
  sha256sum "$(readlink -f -- "$(command -v "$1")")" | awk '{print $1}'
}
assert_equal "x64 widl" "$(hash_tool x86_64-w64-mingw32-widl)" \
  "$VKD3D_X64_WIDL_SHA256"
assert_equal "x86 widl" "$(hash_tool i686-w64-mingw32-widl)" \
  "$VKD3D_X86_WIDL_SHA256"

mkdir -p "$WORK_ROOT"
mkdir "$BUILD_PARENT"

export LC_ALL=C
export LANG=C
export TZ=UTC
export PYTHONHASHSEED=0
export ZERO_AR_DATE=1

GIT_URL="${BOL_VKD3D_GIT_URL:-$VKD3D_UPSTREAM_URL}"
echo "== Cloning pinned vkd3d-proton source"
git clone --no-checkout "$GIT_URL" "$SOURCE_DIR"
git -C "$SOURCE_DIR" config core.autocrlf false
git -C "$SOURCE_DIR" config core.eol lf
git -C "$SOURCE_DIR" checkout --detach "$VKD3D_BASE_COMMIT"
git -C "$SOURCE_DIR" submodule sync --recursive
git -C "$SOURCE_DIR" submodule update --init --recursive

assert_equal "vkd3d base commit" \
  "$(git -C "$SOURCE_DIR" rev-parse HEAD)" "$VKD3D_BASE_COMMIT"
assert_equal "vkd3d tag" \
  "$(git -C "$SOURCE_DIR" rev-parse "$VKD3D_TAG^{commit}")" \
  "$VKD3D_BASE_COMMIT"
git -C "$SOURCE_DIR" cat-file -e "$VKD3D_REMOVAL_COMMIT^{commit}" || die \
  "removal commit is absent: $VKD3D_REMOVAL_COMMIT"
git -C "$SOURCE_DIR" merge-base --is-ancestor \
  "$VKD3D_REMOVAL_COMMIT" "$VKD3D_BASE_COMMIT" || die \
  "removal commit is not an ancestor of the pinned base"

ACTUAL_SUBMODULES="$WORK_ROOT/submodules.actual"
git -C "$SOURCE_DIR" submodule status --recursive | while IFS= read -r line; do
  [[ "${line:0:1}" == " " ]] || die "submodule is not at its recorded commit: $line"
  read -r commit path _ <<< "${line:1}"
  printf '%s %s\n' "$commit" "$path"
done > "$ACTUAL_SUBMODULES"
diff -u "$SUBMODULE_LOCK" "$ACTUAL_SUBMODULES" || die \
  "submodule lock mismatch"

echo "== Restoring the exact NV-DGC implementation removed by $VKD3D_REMOVAL_COMMIT"
git -C "$SOURCE_DIR" revert --no-commit "$VKD3D_REMOVAL_COMMIT"
# The restored shader source ends on a blank line upstream, which is not a sign
# of a corrupted revert; keep every other whitespace rule armed.
git -C "$SOURCE_DIR" -c core.whitespace=-blank-at-eof diff --cached --check
GENERATED_PATCH="$WORK_ROOT/restore-nv-dgc.generated.patch"
git -C "$SOURCE_DIR" \
  -c core.quotePath=true \
  -c diff.mnemonicPrefix=false \
  -c diff.noprefix=false \
  -c core.abbrev=7 \
  diff --cached --binary --no-ext-diff --no-textconv > "$GENERATED_PATCH"
assert_equal "revert patch" \
  "$(sha256sum "$GENERATED_PATCH" | awk '{print $1}')" \
  "$VKD3D_REVERT_PATCH_SHA256"
cmp "$REVERT_PATCH" "$GENERATED_PATCH" || die \
  "vendored revert patch differs from git revert output"

echo "== Applying the occluded frame-latency fix"
git -C "$SOURCE_DIR" apply --index --whitespace=nowarn "$OCCLUSION_PATCH" || die \
  "vendored occlusion patch does not apply to the pinned source"

echo "== Applying the frame-limiter busy-wait fix"
git -C "$SOURCE_DIR" apply --index --whitespace=nowarn "$LIMITER_PATCH" || die \
  "vendored frame-limiter patch does not apply to the pinned source"

build_arch() {
  local arch="$1" cross_file="$2" bindir="$3" epoch="$4"
  local build_dir="$OUTPUT_DIR/build.$arch"
  echo "== Building Windows $bindir payload (SOURCE_DATE_EPOCH=$epoch)"
  (
    cd "$SOURCE_DIR"
    SOURCE_DATE_EPOCH="$epoch" meson setup \
      --cross-file "$cross_file" \
      --buildtype release \
      --prefix "$OUTPUT_DIR" \
      --strip \
      --bindir "$bindir" \
      --libdir "$bindir" \
      "$build_dir"
  )
  SOURCE_DATE_EPOCH="$epoch" ninja -C "$build_dir" install
  find "$OUTPUT_DIR/$bindir" -maxdepth 1 -type f ! -name '*.dll' -delete
}

build_arch 64 build-win64.txt x64 "$VKD3D_SOURCE_DATE_EPOCH_X64"
build_arch 86 build-win32.txt x86 "$VKD3D_SOURCE_DATE_EPOCH_X86"
install -m 0755 "$SOURCE_DIR/setup_vkd3d_proton.sh" "$OUTPUT_DIR/"

for arch in x64 x86; do
  for dll in d3d12.dll d3d12core.dll; do
    [[ -f "$OUTPUT_DIR/$arch/$dll" ]] || die "missing output: $arch/$dll"
  done
  strings -a "$OUTPUT_DIR/$arch/d3d12core.dll" | \
    grep -F 'VK_EXT_device_generated_commands' >/dev/null || die \
    "$arch build has no EXT-DGC path"
  strings -a "$OUTPUT_DIR/$arch/d3d12core.dll" | \
    grep -F 'VK_NV_device_generated_commands' >/dev/null || die \
    "$arch build has no NV-DGC path"
done

echo "== Verifying reviewed r11 DLL hashes"
(
  cd "$OUTPUT_DIR"
  sha256sum -c "$EXPECTED_HASHES"
)

PROVENANCE_OUT="$OUTPUT_DIR/provenance"
mkdir -p "$PROVENANCE_OUT"
install -m 0644 "$LOCK_FILE" "$SUBMODULE_LOCK" "$EXPECTED_HASHES" \
  "$REVERT_PATCH" "$OCCLUSION_PATCH" "$LIMITER_PATCH" "$LICENSE_FILE" \
  "$README_FILE" \
  "$BUILD_RECIPE" "$PROVENANCE_OUT/"
{
  printf 'git: %s\n' "$(git --version)"
  printf 'python: %s\n' "$(python3 --version)"
  printf 'meson: %s\n' "$(meson --version)"
  printf 'ninja: %s\n' "$(ninja --version)"
  printf 'glslang: %s\n' "$(first_line "$GLSLANG" --version)"
  printf 'x64 gcc: %s\n' "$(first_line x86_64-w64-mingw32-gcc --version)"
  printf 'x86 gcc: %s\n' "$(first_line i686-w64-mingw32-gcc --version)"
  printf 'binutils: %s\n' "$(first_line x86_64-w64-mingw32-ld --version)"
} > "$PROVENANCE_OUT/BUILD-TOOLS.txt"

echo "== Universal vkd3d payload reproduced and verified"
echo "   output: $OUTPUT_DIR"
echo "   next: scripts/package-engine.sh ENGINE_DIR '$OUTPUT_DIR' WINEGDK_COMMIT"
