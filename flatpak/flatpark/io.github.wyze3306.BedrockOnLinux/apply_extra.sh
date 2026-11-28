#!/bin/sh
set -eu

# Runs offline at install time. BedrockOnLinux's Linux download is an
# AppImage: an ELF stub with a SquashFS filesystem appended. It is never
# executed or mounted; the appended filesystem is unpacked with the
# appimage-tools stack fetched alongside it (appimage-offset finds where the
# SquashFS starts, unsquashfs unpacks from there).
#
# The result is the AppDir at /app/extra/bedrock-on-linux. The wrapper runs
# the vendor's own AppRun from it, which starts usr/bin/bedrock-on-linux with
# the bundled CPython in usr/python. The desktop file, icon and AppStream
# metainfo are installed by the manifest at build time, since extra-data only
# arrives on the user's machine.

# The apply_extra sandbox has no locale; bsdtar warns about it otherwise.
LC_ALL=C
export LC_ALL

extra_root="${EXTRA_ROOT:-/app/extra}"
cd "$extra_root"

for f in bedrock-on-linux.AppImage appimage-tools.tar.xz; do
    [ -f "$f" ] || { echo "missing extra-data: $f" >&2; exit 1; }
done
rm -rf bedrock-on-linux squashfs-root appimage-tools

# --no-same-owner: a system-wide install runs apply_extra as root with every
# capability dropped, so restoring recorded owners would fail.
bsdtar --no-same-owner -xf appimage-tools.tar.xz
tools="$extra_root/appimage-tools/bin"
[ -x "$tools/appimage-offset" ] && [ -x "$tools/unsquashfs" ] \
    || { echo "appimage-tools stack incomplete" >&2; exit 1; }

offset="$("$tools/appimage-offset" bedrock-on-linux.AppImage)"
case "$offset" in
    ''|*[!0-9]*) echo "appimage-offset did not return a number: '$offset'" >&2; exit 1 ;;
esac

# -no-xattrs: the sandbox cannot set them, and the app does not need them.
"$tools/unsquashfs" -q -no-xattrs -o "$offset" -d squashfs-root bedrock-on-linux.AppImage >/dev/null

# AppRun is a short script that runs usr/bin/bedrock-on-linux with the
# bundled interpreter; check both ends of it.
[ -x squashfs-root/AppRun ] \
    || { echo "AppRun missing after unpack" >&2; exit 1; }
[ -f squashfs-root/usr/bin/bedrock-on-linux ] \
    || { echo "usr/bin/bedrock-on-linux missing after unpack" >&2; exit 1; }
set -- squashfs-root/usr/python/bin/python3.*
[ "$#" -ge 1 ] && [ -x "$1" ] \
    || { echo "no bundled python3 in usr/python/bin" >&2; exit 1; }

mv squashfs-root bedrock-on-linux
rm -rf appimage-tools bedrock-on-linux.AppImage appimage-tools.tar.xz
