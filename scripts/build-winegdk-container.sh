#!/usr/bin/env bash
# Build WineGDK inside a Debian 11 (Bullseye) CONTAINER, as an alternative to
# build-winegdk-bullseye.sh's debootstrap + unprivileged user-namespace chroot,
# whose `unshare --setgid` path does not work on GitHub hosted runners.
#
# Run as root inside a debian:bullseye container (glibc 2.31). Produces an
# install prefix carrying the provenance files package-engine.sh requires, and
# enforces the same glibc-2.31 ABI ceiling. Deterministic paths + SOURCE_DATE_EPOCH
# keep the build reproducible across CI runs.
#
# Usage: build-winegdk-container.sh WINEGDK_SOURCE_REPO OUT_PREFIX
set -Eeuo pipefail
umask 022
export LC_ALL=C LANG=C TZ=UTC

SOURCE_REPO="${1:?usage: build-winegdk-container.sh WINEGDK_SOURCE_REPO OUT_PREFIX}"
PREFIX="${2:?usage: build-winegdk-container.sh WINEGDK_SOURCE_REPO OUT_PREFIX}"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

EXPECTED_COMMIT="$(grep -m1 '^WINEGDK_SOURCE_COMMIT = ' \
  "$PROJECT_ROOT/bol/config.py" | cut -d'"' -f2)"
EXPECTED_SOURCE_MANIFEST_SHA256="$(
  grep -m1 '^WINEGDK_SOURCE_MANIFEST_SHA256 = ' \
    "$PROJECT_ROOT/bol/config.py" | cut -d'"' -f2
)"
readonly EXPECTED_SOURCE_DATE_EPOCH="1784308597"
readonly GLIBC_CEILING="2.31"
readonly VENDORED_FOLLOWUP_PATCH="$PROJECT_ROOT/third_party/winegdk-native5/0002-windows.storage-use-legacy-single-file-dialog.patch"
readonly VENDORED_FOLLOWUP_PATCH_SHA256="68b20aa95afbef46ad9a50d24cadfdd89267e1f4ad341bb25320443b8cac1cae"
readonly VENDORED_ACHIEVEMENTS_PATCH="$PROJECT_ROOT/third_party/winegdk-native5/0003-xgameruntime-use-windows-achievements-token.patch"
readonly VENDORED_ACHIEVEMENTS_PATCH_SHA256="244101f82f58328b94fce93d02ace47e1c0148cf67b7a32e4b9dd44225e81e00"
readonly VENDORED_CONTEXT_CALLBACK_PATCH="$PROJECT_ROOT/third_party/winegdk-native5/0004-combase-implement-context-callback.patch"
readonly VENDORED_CONTEXT_CALLBACK_PATCH_SHA256="33afb0b3bcd7678e828a955d639d3384b8b5c656219b05e9c53bb45dd7c34919"
readonly VENDORED_CLIENT_SURFACE_PATCH="$PROJECT_ROOT/third_party/winegdk-native5/0005-winex11-use-client-surface-origin.patch"
readonly VENDORED_CLIENT_SURFACE_PATCH_SHA256="464da914667bd9c683fb79bc7c2a4477546a73060c66f29809cfa93783cbc1c8"
readonly SOURCE_SHA256SUMS="$PROJECT_ROOT/third_party/winegdk-native5/SOURCE-SHA256SUMS"
readonly NTSYNC_UAPI_HEADER="$PROJECT_ROOT/third_party/linux-uapi/ntsync.h"
readonly NTSYNC_UAPI_HEADER_SHA256="006437ee52a3e04f921df77081eb5c21c44c71f598b10ac534c6ef9e78296262"
# Fixed build paths so Wine's embedded __FILE__ strings are stable run to run.
readonly SRC=/winegdk/source
readonly BUILD=/winegdk/build

[ "$(id -u)" = 0 ] || { echo "!! must run as root in a bullseye container" >&2; exit 1; }

readonly -a BUILD_PACKAGES=(
  build-essential ca-certificates bison flex gettext pkg-config python3-minimal
  gcc-mingw-w64-i686 gcc-mingw-w64-x86-64 libasound2-dev libdbus-1-dev
  libegl1-mesa-dev libfontconfig1-dev libfreetype6-dev libgl1-mesa-dev
  libgnutls28-dev libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev
  libkrb5-dev libpcap-dev libpulse-dev libsdl2-dev libudev-dev libunwind-dev
  libusb-1.0-0-dev libvulkan-dev libwayland-dev libx11-dev libxcomposite-dev
  libxcursor-dev libxext-dev libxfixes-dev libxi-dev libxinerama-dev
  libxkbcommon-dev libxrandr-dev libxrender-dev libxxf86vm-dev wayland-protocols
)

echo "== Installing build dependencies"
export DEBIAN_FRONTEND=noninteractive
bash "$PROJECT_ROOT/scripts/pin-apt-snapshot.sh" bullseye
apt-get update -qq
apt-get install -y --no-install-recommends git "${BUILD_PACKAGES[@]}" >/dev/null

echo "== Installing the vendored linux/ntsync.h UAPI header"
# Bullseye's linux-libc-dev is kernel 5.10 and predates ntsync, so configure
# would leave HAVE_LINUX_NTSYNC_H undefined and compile Wine's whole
# in-process synchronization backend out to stubs. Every Win32 wait would then
# be a wineserver round-trip, which serialises Minecraft's worker threads and
# makes the game behave as if it were single-threaded (issues #63/#139/#143/
# #148/#150). The header is a frozen ioctl ABI; see third_party/linux-uapi.
[ -f "$NTSYNC_UAPI_HEADER" ] \
  || { echo "!! missing vendored linux/ntsync.h" >&2; exit 1; }
[ "$(sha256sum "$NTSYNC_UAPI_HEADER" | cut -d' ' -f1)" = \
  "$NTSYNC_UAPI_HEADER_SHA256" ] \
  || { echo "!! vendored linux/ntsync.h hash mismatch" >&2; exit 1; }
if cmp -s "$NTSYNC_UAPI_HEADER" /usr/include/linux/ntsync.h 2>/dev/null; then
  : # already the reviewed header
elif [ -e /usr/include/linux/ntsync.h ] \
     && grep -q "NTSYNC_IOC_EVENT_READ" /usr/include/linux/ntsync.h; then
  # Complete but not ours: a build-input change that needs review, not a
  # silent build against an unreviewed ABI.
  echo "!! /usr/include/linux/ntsync.h differs from the vendored copy" >&2
  exit 1
else
  # Absent, or the 6.10-6.13 preview UAPI with only two ioctls. That preview
  # defines HAVE_LINUX_NTSYNC_H so configure looks satisfied, yet Wine gates
  # the backend on NTSYNC_IOC_EVENT_READ and still compiles it out.
  install -D -m 0644 "$NTSYNC_UAPI_HEADER" /usr/include/linux/ntsync.h
fi

echo "== Exporting pinned WineGDK source ($EXPECTED_COMMIT)"
git config --global --add safe.directory '*'
git -C "$SOURCE_REPO" cat-file -e "$EXPECTED_COMMIT^{commit}" \
  || { echo "!! source repo lacks $EXPECTED_COMMIT" >&2; exit 1; }
epoch="$(git -C "$SOURCE_REPO" show -s --format=%ct "$EXPECTED_COMMIT")"
[ "$epoch" = "$EXPECTED_SOURCE_DATE_EPOCH" ] \
  || { echo "!! source timestamp $epoch != $EXPECTED_SOURCE_DATE_EPOCH" >&2; exit 1; }
rm -rf "$SRC" "$BUILD"; mkdir -p "$SRC" "$BUILD" "$PREFIX"
git -C "$SOURCE_REPO" archive --format=tar "$EXPECTED_COMMIT" | tar -x -C "$SRC"

echo "== Applying reviewed file-picker follow-up"
[ -f "$VENDORED_FOLLOWUP_PATCH" ] \
  || { echo "!! missing WineGDK file-picker follow-up patch" >&2; exit 1; }
[ "$(sha256sum "$VENDORED_FOLLOWUP_PATCH" | cut -d' ' -f1)" = \
  "$VENDORED_FOLLOWUP_PATCH_SHA256" ] \
  || { echo "!! WineGDK file-picker follow-up patch hash mismatch" >&2; exit 1; }
git -C "$SRC" apply --check "$VENDORED_FOLLOWUP_PATCH" \
  || { echo "!! WineGDK file-picker follow-up patch does not apply" >&2; exit 1; }
git -C "$SRC" apply "$VENDORED_FOLLOWUP_PATCH"

echo "== Applying reviewed Achievements token follow-up"
[ -f "$VENDORED_ACHIEVEMENTS_PATCH" ] \
  || { echo "!! missing WineGDK Achievements patch" >&2; exit 1; }
[ "$(sha256sum "$VENDORED_ACHIEVEMENTS_PATCH" | cut -d' ' -f1)" = \
  "$VENDORED_ACHIEVEMENTS_PATCH_SHA256" ] \
  || { echo "!! WineGDK Achievements patch hash mismatch" >&2; exit 1; }
git -C "$SRC" apply --check "$VENDORED_ACHIEVEMENTS_PATCH" \
  || { echo "!! WineGDK Achievements patch does not apply" >&2; exit 1; }
git -C "$SRC" apply "$VENDORED_ACHIEVEMENTS_PATCH"

echo "== Applying reviewed COM context-callback backport"
[ -f "$VENDORED_CONTEXT_CALLBACK_PATCH" ] \
  || { echo "!! missing WineGDK context-callback patch" >&2; exit 1; }
[ "$(sha256sum "$VENDORED_CONTEXT_CALLBACK_PATCH" | cut -d' ' -f1)" = \
  "$VENDORED_CONTEXT_CALLBACK_PATCH_SHA256" ] \
  || { echo "!! WineGDK context-callback patch hash mismatch" >&2; exit 1; }
git -C "$SRC" apply --check "$VENDORED_CONTEXT_CALLBACK_PATCH" \
  || { echo "!! WineGDK context-callback patch does not apply" >&2; exit 1; }
git -C "$SRC" apply "$VENDORED_CONTEXT_CALLBACK_PATCH"

echo "== Applying X11 client-surface geometry backport"
[ -f "$VENDORED_CLIENT_SURFACE_PATCH" ] \
  || { echo "!! missing X11 client-surface patch" >&2; exit 1; }
[ "$(sha256sum "$VENDORED_CLIENT_SURFACE_PATCH" | cut -d' ' -f1)" = \
  "$VENDORED_CLIENT_SURFACE_PATCH_SHA256" ] \
  || { echo "!! X11 client-surface patch hash mismatch" >&2; exit 1; }
git -C "$SRC" apply --check "$VENDORED_CLIENT_SURFACE_PATCH" \
  || { echo "!! X11 client-surface patch does not apply" >&2; exit 1; }
git -C "$SRC" apply "$VENDORED_CLIENT_SURFACE_PATCH"

echo "== Verifying reviewed source hashes"
[ -f "$SOURCE_SHA256SUMS" ] || { echo "!! missing SOURCE-SHA256SUMS" >&2; exit 1; }
[ "$(sha256sum "$SOURCE_SHA256SUMS" | cut -d' ' -f1)" = \
  "$EXPECTED_SOURCE_MANIFEST_SHA256" ] \
  || { echo "!! SOURCE-SHA256SUMS does not match the config pin" >&2; exit 1; }
( cd "$SRC" && sha256sum --strict -c "$SOURCE_SHA256SUMS" >/dev/null ) \
  || { echo "!! exported source does not match the reviewed native delta" >&2; exit 1; }

echo "== Configuring + building WineGDK (i386 + x86_64)"
( cd "$BUILD"
  SOURCE_DATE_EPOCH="$EXPECTED_SOURCE_DATE_EPOCH" \
    "$SRC/configure" --enable-archs=i386,x86_64 --disable-tests --prefix="$PREFIX"
  SOURCE_DATE_EPOCH="$EXPECTED_SOURCE_DATE_EPOCH" make -j"$(nproc)"
  make install )
# Wine's installed headers embed the builder's absolute source path and are not
# runtime material; drop them so the prefix is relocatable + reproducible.
rm -rf "$PREFIX/include"

# Fail closed if the in-process synchronization backend was compiled out. The
# "/dev/ntsync" literal only survives when NTSYNC_IOC_EVENT_READ was defined,
# so this is a direct check that the ntsync path is really in the artifact.
echo "== Verifying in-process synchronization (ntsync) is compiled in"
ntsync_servers=0
for server in "$PREFIX"/bin/wineserver "$PREFIX"/bin-wow64/wineserver; do
  [ -f "$server" ] || continue
  ntsync_servers=$((ntsync_servers + 1))
  grep -qa "/dev/ntsync" "$server" \
    || { echo "!! $server has no ntsync path — in-process sync compiled out" >&2; exit 1; }
done
[ "$ntsync_servers" -gt 0 ] \
  || { echo "!! no wineserver below $PREFIX to verify" >&2; exit 1; }

# ntdll.so links libunwind (configure autodetects libunwind-dev), but the
# pressure-vessel/sniper runtime ships no libunwind, so bundle the exact bullseye
# libunwind alongside the engine's other x86_64-linux-gnu libs. Proton's wrapper
# puts files/lib/x86_64-linux-gnu on LD_LIBRARY_PATH, so ntdll resolves it there.
# This matches the upstream engine layout that REQUIRED_*_PATHS pin.
echo "== Bundling libunwind for the sniper runtime"
libunwind_real="$(readlink -f /usr/lib/x86_64-linux-gnu/libunwind.so.8)"
[ -f "$libunwind_real" ] \
  || { echo "!! libunwind.so.8 missing from the build container" >&2; exit 1; }
mkdir -p "$PREFIX/lib/x86_64-linux-gnu"
cp -a "$libunwind_real" "$PREFIX/lib/x86_64-linux-gnu/$(basename "$libunwind_real")"
ln -sf "$(basename "$libunwind_real")" "$PREFIX/lib/x86_64-linux-gnu/libunwind.so.8"

echo "== Enforcing GLIBC_$GLIBC_CEILING ABI ceiling"
version_is_greater() {  # $1 > $2 ?
  local greatest
  greatest="$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -n1)"
  [ "$1" != "$2" ] && [ "$greatest" = "$1" ]
}
elf=0 failures=0
while IFS= read -r -d '' f; do
  readelf -h -- "$f" >/dev/null 2>&1 || continue
  elf=$((elf + 1))
  # `|| true`: a file with no GLIBC version symbols makes grep exit 1, which
  # would otherwise trip set -e via pipefail.
  vers="$(readelf --version-info -- "$f" 2>/dev/null \
    | grep -oE 'GLIBC_[0-9]+([.][0-9]+)*' || true)"
  max="$(printf '%s\n' "$vers" | sed 's/GLIBC_//' | sort -Vu | tail -n1)"
  if [ -n "$max" ] && version_is_greater "$max" "$GLIBC_CEILING"; then
    echo "ABI violation: ${f#"$PREFIX"/} needs GLIBC_$max" >&2
    failures=$((failures + 1))
  fi
done < <(find "$PREFIX" -type f -print0)
[ "$elf" -gt 0 ] || { echo "!! no ELF file in prefix" >&2; exit 1; }
[ -n "$(find "$PREFIX" -type f -name wineserver -print -quit)" ] \
  || { echo "!! prefix has no wineserver" >&2; exit 1; }
[ -n "$(find "$PREFIX" -type f -path '*/x86_64-unix/ntdll.so' -print -quit)" ] \
  || { echo "!! prefix has no x86_64-unix/ntdll.so" >&2; exit 1; }
[ ! -e "$PREFIX/lib/wine/i386-unix" ] \
  && [ ! -L "$PREFIX/lib/wine/i386-unix" ] \
  || { echo "!! prefix unexpectedly contains an i386 Unix runtime" >&2; exit 1; }
[ "$failures" = 0 ] || { echo "!! $failures ELF file(s) exceed GLIBC_$GLIBC_CEILING" >&2; exit 1; }

echo "== Writing provenance"
dpkg-query -W -f='${binary:Package}\t${Version}\n' > "$PREFIX/.bol-winegdk-package-versions.tsv"
pv_sha="$(sha256sum "$PREFIX/.bol-winegdk-package-versions.tsv" | cut -d' ' -f1)"
cat > "$PREFIX/.bol-winegdk-build.env" <<EOF
schema=1
winegdk_commit=$EXPECTED_COMMIT
source_manifest_sha256=$EXPECTED_SOURCE_MANIFEST_SHA256
source_date_epoch=$EXPECTED_SOURCE_DATE_EPOCH
debian_suite=bullseye
glibc_ceiling=$GLIBC_CEILING
package_versions_sha256=$pv_sha
EOF

echo "== WineGDK prefix ready: $PREFIX"
