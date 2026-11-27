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
# BOL_WINEGDK_TARGET=aarch64 builds the experimental ARM64EC engine instead
# (#250, docs/ARM64EC.md): run as root in Valve's steamrt4 arm64 SDK image on an
# aarch64 host, which already holds the toolchain (native clang + llvm-mingw for
# arm64ec, aarch64, i386 and x86_64) and every -dev package, so nothing is
# installed. It builds Proton ARM64's architecture set, so no base Wine module
# survives the overlay, and its ceiling is the glibc of Steam Runtime 4. The
# default target, x86_64, is the engine above, unchanged.
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
readonly TARGET="${BOL_WINEGDK_TARGET:-x86_64}"
case "$TARGET" in
  x86_64)
    readonly GLIBC_CEILING="2.31" BUILD_SUITE="bullseye"
    readonly WINE_ARCHS="i386,x86_64" UNIX_ARCH="x86_64" ;;
  aarch64)
    readonly GLIBC_CEILING="2.41" BUILD_SUITE="steamrt4"
    readonly WINE_ARCHS="arm64ec,aarch64,i386,x86_64" UNIX_ARCH="aarch64" ;;
  *) echo "!! unknown BOL_WINEGDK_TARGET '$TARGET' (x86_64 or aarch64)" >&2; exit 1 ;;
esac
readonly XKBCOMMON_VERSION="1.9.2"
readonly XKBCOMMON_SHA256="8d68a8b45796f34f7cace357b9f89b8c92b158557274fef5889b03648b55fe59"
[ "$(uname -m)" = "$UNIX_ARCH" ] \
  || { echo "!! the $TARGET engine is built on a $UNIX_ARCH host, not $(uname -m)" >&2; exit 1; }
readonly VENDORED_FOLLOWUP_PATCH="$PROJECT_ROOT/third_party/winegdk-native5/0002-windows.storage-use-legacy-single-file-dialog.patch"
readonly VENDORED_FOLLOWUP_PATCH_SHA256="68b20aa95afbef46ad9a50d24cadfdd89267e1f4ad341bb25320443b8cac1cae"
readonly VENDORED_ACHIEVEMENTS_PATCH="$PROJECT_ROOT/third_party/winegdk-native5/0003-xgameruntime-use-windows-achievements-token.patch"
readonly VENDORED_ACHIEVEMENTS_PATCH_SHA256="244101f82f58328b94fce93d02ace47e1c0148cf67b7a32e4b9dd44225e81e00"
readonly VENDORED_CONTEXT_CALLBACK_PATCH="$PROJECT_ROOT/third_party/winegdk-native5/0004-combase-implement-context-callback.patch"
readonly VENDORED_CONTEXT_CALLBACK_PATCH_SHA256="33afb0b3bcd7678e828a955d639d3384b8b5c656219b05e9c53bb45dd7c34919"
readonly VENDORED_CLIENT_SURFACE_PATCH="$PROJECT_ROOT/third_party/winegdk-native5/0005-winex11-use-client-surface-origin.patch"
readonly VENDORED_CLIENT_SURFACE_PATCH_SHA256="464da914667bd9c683fb79bc7c2a4477546a73060c66f29809cfa93783cbc1c8"
readonly VENDORED_XSTORE_PATCH="$PROJECT_ROOT/third_party/winegdk-native5/0006-xgameruntime-stop-faking-xstore-answers.patch"
readonly VENDORED_XSTORE_PATCH_SHA256="0ae38358531a81af9b20a997d73f07b383db68afcd14cf89107dd9442f101fd1"
readonly VENDORED_MAPPED_FD_PATCH="$PROJECT_ROOT/third_party/winegdk-native5/0007-ntdll-load-main-image-from-a-mapped-fd.patch"
readonly VENDORED_MAPPED_FD_PATCH_SHA256="0ebb25f67183b0bb052b9cbb76f6a0aca02be89fc7be448ff94702b178e9ffc6"
readonly VENDORED_PATH_MAP_PATCH="$PROJECT_ROOT/third_party/winegdk-native5/0008-ntdll-accept-a-path-in-the-image-map.patch"
readonly VENDORED_PATH_MAP_PATCH_SHA256="7510e707abf9869d40b855754d58c025e1bd98cec455bef3dbec79e9d14461a3"
readonly VENDORED_XSYSTEM_PATCH="$PROJECT_ROOT/third_party/winegdk-native5/0009-xgameruntime-support-IXSystemImpl2-5-and-stub-XSystemHandleTrack.patch"
readonly VENDORED_XSYSTEM_PATCH_SHA256="c0a8e35717368fdc31f5ba6374ecb64aacb84cbd1dc22958077cff9ab5e2c301"
readonly VENDORED_SAVE_PICKER_PATCH="$PROJECT_ROOT/third_party/winegdk-native5/0010-windows.storage-implement-the-file-save-picker.patch"
readonly VENDORED_SAVE_PICKER_PATCH_SHA256="6ec2431cd854c2ae9f47ca6936dff19b0309c82ad3f3bc3fdcd0a9361ecd511c"
readonly VENDORED_WAYLAND_SEAT_PATCH="$PROJECT_ROOT/third_party/winegdk-native5/0011-winewayland-bind-wl_seat-no-newer-than-the-pointer-listener.patch"
readonly VENDORED_WAYLAND_SEAT_PATCH_SHA256="ed550ec1c78d581fbd21d25da429b9b37fb137e19df7128056cb2ec5340a5c59"
readonly VENDORED_GAMING_SERVICES_PATCH="$PROJECT_ROOT/third_party/winegdk-native5/0012-xgameruntime-run-titles-that-target-a-newer-gaming-services.patch"
readonly VENDORED_GAMING_SERVICES_PATCH_SHA256="d43c0f2cf32b624bb12d6db890d6f9a3a45e72523837310d868bec78318f59ce"
readonly VENDORED_PRIVATE_USAGE_PATCH="$PROJECT_ROOT/third_party/winegdk-native5/0013-kernelbase-report-PrivateUsage-from-GetProcessMemoryInfo.patch"
readonly VENDORED_PRIVATE_USAGE_PATCH_SHA256="850b8f80a23e1b9a5b25ab2b647125c7affe6650fe8f9c632678bba9dacae35e"
readonly VENDORED_FSYNC_PATCH="$PROJECT_ROOT/third_party/winegdk-native5/0014-ntdll-server-fall-back-to-fsync-without-ntsync.patch"
readonly VENDORED_FSYNC_PATCH_SHA256="57a7cf41297df589e369f9e059a3e3d8925de785ed588a24134ffa26b5f7f81a"
readonly VENDORED_VM_COUNTERS_PATCH="$PROJECT_ROOT/third_party/winegdk-native5/0015-ntdll-reread-proc-self-status-through-a-kept-descriptor.patch"
readonly VENDORED_VM_COUNTERS_PATCH_SHA256="9fa03ed50b2a5bfb68a60f55aa4b1ce4b7d842010c6b3f2c176177528a722db6"
readonly VENDORED_TIME_ZONE_PATCH="$PROJECT_ROOT/third_party/winegdk-native5/0016-ntdll-check-the-time-zone-at-most-once-a-second.patch"
readonly VENDORED_TIME_ZONE_PATCH_SHA256="6054f33a5a95c5beaf12039dca8b303fdfa66c4e01fb95b75479659f0aebc2d9"
readonly VENDORED_GAME_EVENT_PATCH="$PROJECT_ROOT/third_party/winegdk-native5/0017-xgameruntime-record-the-title-s-XGameEventWrite-calls.patch"
readonly VENDORED_GAME_EVENT_PATCH_SHA256="c791f7bcab535204649c27ade0d328f669134f6044a1130362aeb5df964b28ad"
readonly VENDORED_INTL_PATCH="$PROJECT_ROOT/third_party/winegdk-native5/0018-jscript-add-the-Intl-object-Internet-Explorer-11-has.patch"
readonly VENDORED_INTL_PATCH_SHA256="154f48790e2a8eecd970438cda3caf3bfea5e0b72f5012f2dd0379d3e49de61a"
readonly VENDORED_CALL_FLAGS_PATCH="$PROJECT_ROOT/third_party/winegdk-native5/0019-jscript-call-not-get-when-METHOD-PROPERTYGET-carries-arguments.patch"
readonly VENDORED_CALL_FLAGS_PATCH_SHA256="8e5c60b1eb3e9494abab7ff61f3e4cc8eed1f1d73a31623d76d07f24a35086fb"
readonly VENDORED_SHELL_BROWSER_PATCH="$PROJECT_ROOT/third_party/winegdk-native5/0020-ieframe-serve-SID_SShellBrowser-from-the-WebBrowser-control.patch"
readonly VENDORED_SHELL_BROWSER_PATCH_SHA256="b53e95a2d11125744c5abd25ad60c53d32b35adae0c451b4ff85b47b94e4c215"
readonly VENDORED_PREAUTH_BYPASS_PATCH="$PROJECT_ROOT/third_party/winegdk-native5/0021-xgameruntime-use-preauth-without-oauth-refresh.patch"
readonly VENDORED_PREAUTH_BYPASS_PATCH_SHA256="8150133ce0f6bb7ac6d4c8076730539ee8a66d4e764a0990ae194f46731e9fe4"
readonly SOURCE_SHA256SUMS="$PROJECT_ROOT/third_party/winegdk-native5/SOURCE-SHA256SUMS"
readonly NTSYNC_UAPI_HEADER="$PROJECT_ROOT/third_party/linux-uapi/ntsync.h"
readonly NTSYNC_UAPI_HEADER_SHA256="006437ee52a3e04f921df77081eb5c21c44c71f598b10ac534c6ef9e78296262"
# Fixed build paths so Wine's embedded __FILE__ strings are stable run to run.
readonly SRC=/winegdk/source
readonly BUILD=/winegdk/build

[ "$(id -u)" = 0 ] || { echo "!! must run as root in a $BUILD_SUITE container" >&2; exit 1; }

readonly -a BUILD_PACKAGES=(
  build-essential ca-certificates bison flex gettext pkg-config python3-minimal
  gcc-mingw-w64-i686 gcc-mingw-w64-x86-64 libasound2-dev libdbus-1-dev
  libegl1-mesa-dev libfontconfig1-dev libfreetype6-dev libgl1-mesa-dev
  libgnutls28-dev libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev
  libkrb5-dev libpcap-dev libpulse-dev libsdl2-dev libudev-dev libunwind-dev
  libusb-1.0-0-dev libvulkan-dev libwayland-dev libx11-dev libxcomposite-dev
  libxcursor-dev libxext-dev libxfixes-dev libxi-dev libxinerama-dev
  libxkbcommon-dev libxkbregistry-dev libxrandr-dev libxrender-dev
  libxxf86vm-dev wayland-protocols
)

if [ "$TARGET" = x86_64 ]; then
  echo "== Installing build dependencies"
  export DEBIAN_FRONTEND=noninteractive
  bash "$PROJECT_ROOT/scripts/pin-apt-snapshot.sh" bullseye
  apt-get update -qq
  apt-get install -y --no-install-recommends git "${BUILD_PACKAGES[@]}" >/dev/null
else
  # The SDK image is pinned by digest and is the whole build input; installing
  # anything into it would make the prefix depend on the day of the build.
  echo "== Checking the steamrt4 SDK toolchain"
  for tool in git python3 make bison flex pkg-config readelf strip dpkg-query curl meson ninja \
              aarch64-w64-mingw32-clang arm64ec-w64-mingw32-clang \
              i686-w64-mingw32-clang x86_64-w64-mingw32-clang; do
    command -v "$tool" >/dev/null \
      || { echo "!! the SDK image has no $tool" >&2; exit 1; }
  done
  # ccache is first on the SDK's PATH; a cold cache only costs time, and the
  # build must not read one left by anything else.
  export CCACHE_DISABLE=1
  cc --version | head -n1
  aarch64-w64-mingw32-clang --version | head -n1
  # Wine configures the Wayland driver out without xkbregistry, and the SDK
  # has no libxkbregistry-dev. The base ships the 1.9.2 library at run time
  # (files/lib/aarch64-linux-gnu/libxkbregistry.so.0.9.2), so build that very
  # version to link against; nothing of it goes into the prefix.
  if ! pkg-config --exists xkbregistry; then
    echo "== Building xkbregistry $XKBCOMMON_VERSION to link the Wayland driver"
    xkb_tar="/winegdk/xkbcommon-$XKBCOMMON_VERSION.tar.gz"
    mkdir -p /winegdk
    curl -fsSL --retry 5 -o "$xkb_tar" \
      "https://github.com/xkbcommon/libxkbcommon/archive/refs/tags/xkbcommon-$XKBCOMMON_VERSION.tar.gz"
    [ "$(sha256sum "$xkb_tar" | cut -d' ' -f1)" = "$XKBCOMMON_SHA256" ] \
      || { echo "!! xkbcommon $XKBCOMMON_VERSION tarball hash mismatch" >&2; exit 1; }
    rm -rf /winegdk/xkb-src /winegdk/xkb-build /winegdk/xkb
    mkdir -p /winegdk/xkb-src
    tar -xzf "$xkb_tar" -C /winegdk/xkb-src --strip-components=1
    meson setup /winegdk/xkb-build /winegdk/xkb-src --prefix=/winegdk/xkb \
      --libdir=lib --buildtype=release -Denable-x11=false -Denable-wayland=false \
      -Denable-docs=false -Denable-tools=false -Denable-bash-completion=false \
      -Denable-xkbregistry=true >/dev/null
    ninja -C /winegdk/xkb-build install >/dev/null
    export PKG_CONFIG_PATH="/winegdk/xkb/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
    export LIBRARY_PATH="/winegdk/xkb/lib${LIBRARY_PATH:+:$LIBRARY_PATH}"
    pkg-config --exists xkbregistry || { echo "!! xkbregistry still not found" >&2; exit 1; }
  fi
fi

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

echo "== Applying the XStore honest-failure fix"
[ -f "$VENDORED_XSTORE_PATCH" ] \
  || { echo "!! missing XStore patch" >&2; exit 1; }
[ "$(sha256sum "$VENDORED_XSTORE_PATCH" | cut -d' ' -f1)" = \
  "$VENDORED_XSTORE_PATCH_SHA256" ] \
  || { echo "!! XStore patch hash mismatch" >&2; exit 1; }
git -C "$SRC" apply --check "$VENDORED_XSTORE_PATCH" \
  || { echo "!! XStore patch does not apply" >&2; exit 1; }
git -C "$SRC" apply "$VENDORED_XSTORE_PATCH"

echo "== Applying the ntdll mapped-fd main-image loader"
[ -f "$VENDORED_MAPPED_FD_PATCH" ] \
  || { echo "!! missing ntdll mapped-fd patch" >&2; exit 1; }
[ "$(sha256sum "$VENDORED_MAPPED_FD_PATCH" | cut -d' ' -f1)" = \
  "$VENDORED_MAPPED_FD_PATCH_SHA256" ] \
  || { echo "!! ntdll mapped-fd patch hash mismatch" >&2; exit 1; }
git -C "$SRC" apply --check "$VENDORED_MAPPED_FD_PATCH" \
  || { echo "!! ntdll mapped-fd patch does not apply" >&2; exit 1; }
git -C "$SRC" apply "$VENDORED_MAPPED_FD_PATCH"

echo "== Applying the ntdll image-map path support"
[ -f "$VENDORED_PATH_MAP_PATCH" ] \
  || { echo "!! missing ntdll image-map path patch" >&2; exit 1; }
[ "$(sha256sum "$VENDORED_PATH_MAP_PATCH" | cut -d' ' -f1)" = \
  "$VENDORED_PATH_MAP_PATCH_SHA256" ] \
  || { echo "!! ntdll image-map path patch hash mismatch" >&2; exit 1; }
git -C "$SRC" apply --check "$VENDORED_PATH_MAP_PATCH" \
  || { echo "!! ntdll image-map path patch does not apply" >&2; exit 1; }
git -C "$SRC" apply "$VENDORED_PATH_MAP_PATCH"

echo "== Applying the XSystem interface revisions"
[ -f "$VENDORED_XSYSTEM_PATCH" ] \
  || { echo "!! missing XSystem interface patch" >&2; exit 1; }
[ "$(sha256sum "$VENDORED_XSYSTEM_PATCH" | cut -d' ' -f1)" = \
  "$VENDORED_XSYSTEM_PATCH_SHA256" ] \
  || { echo "!! XSystem interface patch hash mismatch" >&2; exit 1; }
git -C "$SRC" apply --check "$VENDORED_XSYSTEM_PATCH" \
  || { echo "!! XSystem interface patch does not apply" >&2; exit 1; }
git -C "$SRC" apply "$VENDORED_XSYSTEM_PATCH"

echo "== Applying the Windows.Storage file save picker"
[ -f "$VENDORED_SAVE_PICKER_PATCH" ] \
  || { echo "!! missing file save picker patch" >&2; exit 1; }
[ "$(sha256sum "$VENDORED_SAVE_PICKER_PATCH" | cut -d' ' -f1)" = \
  "$VENDORED_SAVE_PICKER_PATCH_SHA256" ] \
  || { echo "!! file save picker patch hash mismatch" >&2; exit 1; }
git -C "$SRC" apply --check "$VENDORED_SAVE_PICKER_PATCH" \
  || { echo "!! file save picker patch does not apply" >&2; exit 1; }
git -C "$SRC" apply "$VENDORED_SAVE_PICKER_PATCH"

echo "== Applying the Wayland seat version fix"
[ -f "$VENDORED_WAYLAND_SEAT_PATCH" ] \
  || { echo "!! missing Wayland seat version patch" >&2; exit 1; }
[ "$(sha256sum "$VENDORED_WAYLAND_SEAT_PATCH" | cut -d' ' -f1)" = \
  "$VENDORED_WAYLAND_SEAT_PATCH_SHA256" ] \
  || { echo "!! Wayland seat version patch hash mismatch" >&2; exit 1; }
git -C "$SRC" apply --check "$VENDORED_WAYLAND_SEAT_PATCH" \
  || { echo "!! Wayland seat version patch does not apply" >&2; exit 1; }
git -C "$SRC" apply "$VENDORED_WAYLAND_SEAT_PATCH"

echo "== Applying the Gaming Services version fix"
[ -f "$VENDORED_GAMING_SERVICES_PATCH" ] \
  || { echo "!! missing Gaming Services version patch" >&2; exit 1; }
[ "$(sha256sum "$VENDORED_GAMING_SERVICES_PATCH" | cut -d' ' -f1)" = \
  "$VENDORED_GAMING_SERVICES_PATCH_SHA256" ] \
  || { echo "!! Gaming Services version patch hash mismatch" >&2; exit 1; }
git -C "$SRC" apply --check "$VENDORED_GAMING_SERVICES_PATCH" \
  || { echo "!! Gaming Services version patch does not apply" >&2; exit 1; }
git -C "$SRC" apply "$VENDORED_GAMING_SERVICES_PATCH"

echo "== Applying the GetProcessMemoryInfo PrivateUsage fix"
[ -f "$VENDORED_PRIVATE_USAGE_PATCH" ] \
  || { echo "!! missing PrivateUsage patch" >&2; exit 1; }
[ "$(sha256sum "$VENDORED_PRIVATE_USAGE_PATCH" | cut -d' ' -f1)" = \
  "$VENDORED_PRIVATE_USAGE_PATCH_SHA256" ] \
  || { echo "!! PrivateUsage patch hash mismatch" >&2; exit 1; }
git -C "$SRC" apply --check "$VENDORED_PRIVATE_USAGE_PATCH" \
  || { echo "!! PrivateUsage patch does not apply" >&2; exit 1; }
git -C "$SRC" apply "$VENDORED_PRIVATE_USAGE_PATCH"

echo "== Applying the fsync fallback for kernels without ntsync"
[ -f "$VENDORED_FSYNC_PATCH" ] \
  || { echo "!! missing fsync fallback patch" >&2; exit 1; }
[ "$(sha256sum "$VENDORED_FSYNC_PATCH" | cut -d' ' -f1)" = \
  "$VENDORED_FSYNC_PATCH_SHA256" ] \
  || { echo "!! fsync fallback patch hash mismatch" >&2; exit 1; }
git -C "$SRC" apply --check "$VENDORED_FSYNC_PATCH" \
  || { echo "!! fsync fallback patch does not apply" >&2; exit 1; }
git -C "$SRC" apply "$VENDORED_FSYNC_PATCH"
echo "== Applying the per-frame process memory counters fix"
[ -f "$VENDORED_VM_COUNTERS_PATCH" ] \
  || { echo "!! missing process memory counters patch" >&2; exit 1; }
[ "$(sha256sum "$VENDORED_VM_COUNTERS_PATCH" | cut -d' ' -f1)" = \
  "$VENDORED_VM_COUNTERS_PATCH_SHA256" ] \
  || { echo "!! process memory counters patch hash mismatch" >&2; exit 1; }
git -C "$SRC" apply --check "$VENDORED_VM_COUNTERS_PATCH" \
  || { echo "!! process memory counters patch does not apply" >&2; exit 1; }
git -C "$SRC" apply "$VENDORED_VM_COUNTERS_PATCH"

echo "== Applying the per-frame time zone check fix"
[ -f "$VENDORED_TIME_ZONE_PATCH" ] \
  || { echo "!! missing time zone check patch" >&2; exit 1; }
[ "$(sha256sum "$VENDORED_TIME_ZONE_PATCH" | cut -d' ' -f1)" = \
  "$VENDORED_TIME_ZONE_PATCH_SHA256" ] \
  || { echo "!! time zone check patch hash mismatch" >&2; exit 1; }
git -C "$SRC" apply --check "$VENDORED_TIME_ZONE_PATCH" \
  || { echo "!! time zone check patch does not apply" >&2; exit 1; }
git -C "$SRC" apply "$VENDORED_TIME_ZONE_PATCH"

echo "== Applying the XGameEvent runtime class"
[ -f "$VENDORED_GAME_EVENT_PATCH" ] \
  || { echo "!! missing XGameEvent patch" >&2; exit 1; }
[ "$(sha256sum "$VENDORED_GAME_EVENT_PATCH" | cut -d' ' -f1)" = \
  "$VENDORED_GAME_EVENT_PATCH_SHA256" ] \
  || { echo "!! XGameEvent patch hash mismatch" >&2; exit 1; }
git -C "$SRC" apply --check "$VENDORED_GAME_EVENT_PATCH" \
  || { echo "!! XGameEvent patch does not apply" >&2; exit 1; }
git -C "$SRC" apply "$VENDORED_GAME_EVENT_PATCH"

echo "== Applying the jscript Intl object"
[ -f "$VENDORED_INTL_PATCH" ] \
  || { echo "!! missing jscript Intl patch" >&2; exit 1; }
[ "$(sha256sum "$VENDORED_INTL_PATCH" | cut -d' ' -f1)" = \
  "$VENDORED_INTL_PATCH_SHA256" ] \
  || { echo "!! jscript Intl patch hash mismatch" >&2; exit 1; }
git -C "$SRC" apply --check "$VENDORED_INTL_PATCH" \
  || { echo "!! jscript Intl patch does not apply" >&2; exit 1; }
git -C "$SRC" apply "$VENDORED_INTL_PATCH"

echo "== Applying the jscript call flags fix"
[ -f "$VENDORED_CALL_FLAGS_PATCH" ] \
  || { echo "!! missing jscript call flags patch" >&2; exit 1; }
[ "$(sha256sum "$VENDORED_CALL_FLAGS_PATCH" | cut -d' ' -f1)" = \
  "$VENDORED_CALL_FLAGS_PATCH_SHA256" ] \
  || { echo "!! jscript call flags patch hash mismatch" >&2; exit 1; }
git -C "$SRC" apply --check "$VENDORED_CALL_FLAGS_PATCH" \
  || { echo "!! jscript call flags patch does not apply" >&2; exit 1; }
git -C "$SRC" apply "$VENDORED_CALL_FLAGS_PATCH"

echo "== Applying the ieframe shell browser window fix"
[ -f "$VENDORED_SHELL_BROWSER_PATCH" ] \
  || { echo "!! missing ieframe shell browser patch" >&2; exit 1; }
[ "$(sha256sum "$VENDORED_SHELL_BROWSER_PATCH" | cut -d' ' -f1)" = \
  "$VENDORED_SHELL_BROWSER_PATCH_SHA256" ] \
  || { echo "!! ieframe shell browser patch hash mismatch" >&2; exit 1; }
git -C "$SRC" apply --check "$VENDORED_SHELL_BROWSER_PATCH" \
  || { echo "!! ieframe shell browser patch does not apply" >&2; exit 1; }
git -C "$SRC" apply "$VENDORED_SHELL_BROWSER_PATCH"

echo "== Applying the xgameruntime preauth bypass fix"
[ -f "$VENDORED_PREAUTH_BYPASS_PATCH" ] \
  || { echo "!! missing xgameruntime preauth bypass patch" >&2; exit 1; }
[ "$(sha256sum "$VENDORED_PREAUTH_BYPASS_PATCH" | cut -d' ' -f1)" = \
  "$VENDORED_PREAUTH_BYPASS_PATCH_SHA256" ] \
  || { echo "!! xgameruntime preauth bypass patch hash mismatch" >&2; exit 1; }
git -C "$SRC" apply --check "$VENDORED_PREAUTH_BYPASS_PATCH" \
  || { echo "!! xgameruntime preauth bypass patch does not apply" >&2; exit 1; }
git -C "$SRC" apply "$VENDORED_PREAUTH_BYPASS_PATCH"

echo "== Verifying reviewed source hashes"
[ -f "$SOURCE_SHA256SUMS" ] || { echo "!! missing SOURCE-SHA256SUMS" >&2; exit 1; }
[ "$(sha256sum "$SOURCE_SHA256SUMS" | cut -d' ' -f1)" = \
  "$EXPECTED_SOURCE_MANIFEST_SHA256" ] \
  || { echo "!! SOURCE-SHA256SUMS does not match the config pin" >&2; exit 1; }
( cd "$SRC" && sha256sum --strict -c "$SOURCE_SHA256SUMS" >/dev/null ) \
  || { echo "!! exported source does not match the reviewed native delta" >&2; exit 1; }

echo "== Configuring + building WineGDK ($WINE_ARCHS)"
( cd "$BUILD"
  SOURCE_DATE_EPOCH="$EXPECTED_SOURCE_DATE_EPOCH" \
    "$SRC/configure" --enable-archs="$WINE_ARCHS" --disable-tests --prefix="$PREFIX"
  SOURCE_DATE_EPOCH="$EXPECTED_SOURCE_DATE_EPOCH" make -j"$(nproc)"
  # Stripped, the way Wine itself installs a release: its install rule strips
  # each module and writes the builtin mark back onto the PE ones. The
  # debugging sections were two thirds of the engine: 2.5 GB installed,
  # 863 MB to download, and again in every prefix.
  SOURCE_DATE_EPOCH="$EXPECTED_SOURCE_DATE_EPOCH" \
    make install INSTALL_PROGRAM_FLAGS=-s )
# strip rewrites each PE file with the time of the run (binutils 2.35 does
# not read SOURCE_DATE_EPOCH there); put the link time back.
python3 "$PROJECT_ROOT/scripts/pe-stamp.py" "$EXPECTED_SOURCE_DATE_EPOCH" \
  "$PREFIX/lib/wine"
if [ "$TARGET" = aarch64 ]; then
  # clang writes the time of the build into each COFF object it assembles for
  # the *-windows targets; the ones inside the installed lib*.a archives were
  # the only bytes two ARM64EC builds did not share.
  python3 "$PROJECT_ROOT/scripts/coff-stamp.py" "$EXPECTED_SOURCE_DATE_EPOCH" \
    "$PREFIX/lib/wine"
fi
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
  # 0014: kernels without ntsync fall back to futex_waitv. The server only
  # reads WINEFSYNC when that backend is compiled in.
  grep -qa "WINEFSYNC" "$server" \
    || { echo "!! $server has no fsync fallback — 0014 not built" >&2; exit 1; }
done
[ "$ntsync_servers" -gt 0 ] \
  || { echo "!! no wineserver below $PREFIX to verify" >&2; exit 1; }

# Fail closed if the Wayland driver was configured out. configure disables it
# without stopping the build when any of wayland-client, wayland-scanner,
# xkbcommon, xkbregistry or <linux/input.h> is missing, and the packager then
# overlays this prefix onto the GE-Proton base: every module we did not build
# survives from that base, so a missing winewayland.drv does not leave a hole,
# it leaves Proton's Wine 10 one next to our Wine 11 win32u.so. The two ABIs do
# not match, the driver fails to load on its win32u imports, and BOL_INPUT=
# wayland can never start the game (issue #180).
echo "== Verifying the Wayland driver was built"
for module in "lib/wine/$UNIX_ARCH-unix/winewayland.so" \
              "lib/wine/$UNIX_ARCH-windows/winewayland.drv"; do
  [ -f "$PREFIX/$module" ] \
    || { echo "!! $module was not built — Wayland driver configured out" >&2; exit 1; }
done

# ntdll.so links libunwind (configure autodetects libunwind-dev), but the
# pressure-vessel/sniper runtime ships no libunwind, so bundle the exact bullseye
# libunwind alongside the engine's other x86_64-linux-gnu libs. Proton's wrapper
# puts files/lib/x86_64-linux-gnu on LD_LIBRARY_PATH, so ntdll resolves it there.
# This matches the upstream engine layout that REQUIRED_*_PATHS pin.
echo "== Bundling libunwind for the Steam Runtime"
if [ "$TARGET" = x86_64 ] \
   || readelf -d "$PREFIX/lib/wine/$UNIX_ARCH-unix/ntdll.so" | grep -q 'libunwind'; then
  libunwind_real="$(readlink -f "/usr/lib/$UNIX_ARCH-linux-gnu/libunwind.so.8")"
  [ -f "$libunwind_real" ] \
    || { echo "!! libunwind.so.8 missing from the build container" >&2; exit 1; }
  mkdir -p "$PREFIX/lib/$UNIX_ARCH-linux-gnu"
  cp -a "$libunwind_real" "$PREFIX/lib/$UNIX_ARCH-linux-gnu/$(basename "$libunwind_real")"
  ln -sf "$(basename "$libunwind_real")" "$PREFIX/lib/$UNIX_ARCH-linux-gnu/libunwind.so.8"
else
  echo "ntdll.so does not link libunwind"
fi

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
[ -n "$(find "$PREFIX" -type f -path "*/$UNIX_ARCH-unix/ntdll.so" -print -quit)" ] \
  || { echo "!! prefix has no $UNIX_ARCH-unix/ntdll.so" >&2; exit 1; }
[ ! -e "$PREFIX/lib/wine/i386-unix" ] \
  && [ ! -L "$PREFIX/lib/wine/i386-unix" ] \
  || { echo "!! prefix unexpectedly contains an i386 Unix runtime" >&2; exit 1; }
[ "$failures" = 0 ] || { echo "!! $failures ELF file(s) exceed GLIBC_$GLIBC_CEILING" >&2; exit 1; }
if [ "$TARGET" = aarch64 ]; then
  # With arm64ec in the set, aarch64-windows holds ARM64X images: one file
  # whose ARM64 and ARM64EC halves serve native and emulated callers alike.
  # A plain ARM64 ntdll there means configure dropped arm64ec, and x64 code
  # could not load at all.
  echo "== Verifying the ARM64X system modules"
  for module in ntdll combase user32 xgameruntime; do
    dll="$PREFIX/lib/wine/aarch64-windows/$module.dll"
    [ -f "$dll" ] || { echo "!! $module.dll was not built for aarch64" >&2; exit 1; }
    python3 - "$dll" <<'PEPY' || { echo "!! $module.dll is not ARM64X" >&2; exit 1; }
import struct, sys
d = open(sys.argv[1], "rb").read()
e = struct.unpack_from("<I", d, 0x3C)[0]
opt = e + 24
machine = struct.unpack_from("<H", d, e + 4)[0]
rva, size = struct.unpack_from("<II", d, opt + 112 + 8 * 10)
nsec = struct.unpack_from("<H", d, e + 6)[0]
sect = opt + struct.unpack_from("<H", d, e + 20)[0]
off = None
for i in range(nsec):
    vsz, va, rsz, raw = struct.unpack_from("<IIII", d, sect + 40 * i + 8)
    if va <= rva < va + max(vsz, rsz):
        off = raw + rva - va
ok = (machine == 0xAA64 and off is not None
      and struct.unpack_from("<I", d, off)[0] >= 0xD0
      and struct.unpack_from("<Q", d, off + 0xC8)[0] != 0)
sys.exit(0 if ok else 1)
PEPY
  done
  [ -f "$PREFIX/lib/wine/x86_64-windows/ntdll.dll" ] \
    || { echo "!! x86_64-windows was not built" >&2; exit 1; }
fi

echo "== Writing provenance"
dpkg-query -W -f='${binary:Package}\t${Version}\n' > "$PREFIX/.bol-winegdk-package-versions.tsv"
pv_sha="$(sha256sum "$PREFIX/.bol-winegdk-package-versions.tsv" | cut -d' ' -f1)"
cat > "$PREFIX/.bol-winegdk-build.env" <<EOF
schema=1
winegdk_commit=$EXPECTED_COMMIT
source_manifest_sha256=$EXPECTED_SOURCE_MANIFEST_SHA256
source_date_epoch=$EXPECTED_SOURCE_DATE_EPOCH
debian_suite=$BUILD_SUITE
glibc_ceiling=$GLIBC_CEILING
package_versions_sha256=$pv_sha
EOF
if [ "$TARGET" != x86_64 ]; then
  # Appended, so the x86_64 file keeps the exact schema-1 bytes.
  cat >> "$PREFIX/.bol-winegdk-build.env" <<EOF
target=$TARGET
wine_archs=$WINE_ARCHS
EOF
fi

echo "== WineGDK prefix ready: $PREFIX"
