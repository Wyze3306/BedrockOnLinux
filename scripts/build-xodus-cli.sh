#!/usr/bin/env bash
# Build xodus-cli from the pinned upstream commit and package it as the
# xodus-cli-<rev>.tar.gz asset the launcher downloads at runtime.
#
# Xodus (https://github.com/xodus-gaming/xodus, GPL-3.0) is what replaced the
# third-party archive repository the launcher used to pull the game from: it
# signs in to the user's own Microsoft account, obtains the title license, and
# streams the MSIXVC package from the official Xbox CDN. See
# third_party/xodus/README.md.
#
# The commit is read from bol/config.py (XODUS_SOURCE_COMMIT) so the pin lives
# in exactly one place. Prints the resulting XODUS_REV + SHA-256 to pin back
# into bol/config.py.
set -Eeuo pipefail

WORK="${1:?usage: build-xodus-cli.sh WORKDIR}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
XODUS_REMOTE="${XODUS_REMOTE:-https://github.com/xodus-gaming/xodus}"

for t in git cargo cargo-zigbuild zig objdump tar gzip sha256sum protoc cmake; do
  command -v "$t" >/dev/null || { echo "!! need $t" >&2; exit 1; }
done
# wry/tao link the login webview unconditionally, and the xodus crate pulls in
# openssl-sys through dbus-secret-service-keyring-store even when the file
# keyring is selected. Fail here rather than 200 crates into the build.
for m in webkit2gtk-4.1 openssl; do
  pkg-config --exists "$m" || { echo "!! need the $m development package" >&2; exit 1; }
done

COMMIT="$(grep -m1 '^XODUS_SOURCE_COMMIT = ' "$SRC/bol/config.py" | cut -d'"' -f2)"
[ -n "$COMMIT" ] || { echo "!! XODUS_SOURCE_COMMIT missing from bol/config.py" >&2; exit 1; }

# xodus-cli is linked against the glibc the game engine is built for, so the
# C library is not what stops it where the engine runs: Debian 12, Ubuntu
# 22.04 and the distributions built on it, Fedora 39 (issue #264, where it
# asked for glibc 2.39 because it was linked against the container's). The
# container cannot simply be that old -- WebKitGTK 4.1, which xodus-cli still
# links, is packaged for nothing older than Ubuntu 22.04 -- so cargo-zigbuild
# links it instead: zig supplies the C library of the version named in the
# target, and every glibc symbol xodus-cli imports is that version's.
# Everything else still comes from the container.
GLIBC_FLOOR="$(grep -m1 '^REQUIRED_ENGINE_GLIBC_MAX = ' "$SRC/bol/vkd3d.py" | cut -d'"' -f2)"
[ -n "$GLIBC_FLOOR" ] || { echo "!! REQUIRED_ENGINE_GLIBC_MAX missing from bol/vkd3d.py" >&2; exit 1; }
TARGET="x86_64-unknown-linux-gnu"
# The patches are part of what the binary is, so the rev names them too: a
# revision has to name exactly one set of bytes, or a rebuild silently
# replaces a published asset with a different binary under the same name.
PATCHES="$SRC/third_party/xodus/patches"
PATCH_LEVEL="$(find "$PATCHES" -maxdepth 1 -name '*.patch' 2>/dev/null | wc -l)"
REV="${COMMIT:0:12}"
[ "$PATCH_LEVEL" -eq 0 ] || REV="$REV-p$PATCH_LEVEL"

echo "== Fetching xodus $COMMIT"
TREE="$WORK/xodus"
rm -rf "$TREE"
mkdir -p "$TREE"
git init -q "$TREE"
git -C "$TREE" remote add origin "$XODUS_REMOTE"
git -C "$TREE" fetch -q --depth 1 origin "$COMMIT"
git -C "$TREE" checkout -q FETCH_HEAD

# key-chain-file is not optional: without it xodus stores tokens through a
# D-Bus secret service, which does not exist in a Steam Deck Game Mode session
# or inside a Flatpak sandbox, and every command needing device credentials
# fails. With it the tokens live in $HOME/.xodus-keyring.ron, where $HOME is
# the directory bol.xodus.home() hands the binary rather than the user's own.
#
# --remap-path-prefix keeps the build directory out of the binary, and
# CARGO_INCREMENTAL=0 + -C strip=symbols drop the two other common sources of
# run-to-run drift, so the artifact has a stable SHA-256 to pin.
#
# --allow-shlib-undefined: the container's GNOME libraries are linked against
# its own, newer glibc, and the linker refuses an executable whose libraries
# import symbols the target glibc lacks. Those imports are the libraries'
# own, resolved at run time against the glibc they were built for -- the
# host's, or the bundled runtime's -- so the check is waived; what xodus-cli
# itself imports is held to the floor below.
export CARGO_INCREMENTAL=0
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1784308597}"
export RUSTFLAGS="-C strip=symbols -C link-arg=-Wl,--allow-shlib-undefined --remap-path-prefix=$TREE=/xodus --remap-path-prefix=$HOME/.cargo=/cargo"

# zig's clang searches its own libc headers and the -I paths it is handed, not
# Debian's multiarch include directory, and libssl-dev keeps its two generated
# headers (opensslconf.h, configuration.h) there. openssl-sys preprocesses
# <openssl/opensslv.h> to learn the version, so without them it stops on
# "openssl/opensslconf.h file not found". Give it one directory holding all of
# them. The headers only steer that version probe; the library linked is the
# container's libssl.so either way.
if [ -z "${OPENSSL_INCLUDE_DIR:-}" ]; then
  MULTIARCH="$(gcc -print-multiarch)"
  SSL_INCLUDE="$WORK/openssl-include"
  rm -rf "$SSL_INCLUDE"
  mkdir -p "$SSL_INCLUDE/openssl"
  cp /usr/include/openssl/*.h "/usr/include/$MULTIARCH/openssl/"*.h "$SSL_INCLUDE/openssl/"
  export OPENSSL_INCLUDE_DIR="$SSL_INCLUDE"
  export OPENSSL_LIB_DIR="/usr/lib/$MULTIARCH"
fi

if [ "$PATCH_LEVEL" -gt 0 ]; then
  echo "== Applying $PATCH_LEVEL patch(es) from third_party/xodus/patches"
  # -3 is deliberately absent: a patch that no longer applies cleanly to the
  # pinned commit is a patch that has to be re-read, not merged blind.
  git -C "$TREE" -c user.email=build@bedrock-on-linux -c user.name=BedrockOnLinux \
    am --keep-non-patch "$PATCHES"/*.patch
  # Each patch carries the test that fails without it, and this is the only
  # place they ever run: the crate is not vendored into this repository. Every
  # crate the patches touch, or a patch lands with its test never run --
  # xodus-cli is in the list for 0004, whose tests are the only thing that
  # checks both legs of the sign-in are still hosted the same way. They are
  # built the way the binary is, so they test the code that ships.
  cargo-zigbuild test --release --manifest-path "$TREE/Cargo.toml" \
    -p msixvc -p xodus -p xodus-cli --locked --target "$TARGET.$GLIBC_FLOOR"
fi

echo "== Building xodus-cli for glibc $GLIBC_FLOOR"
cargo zigbuild --release --manifest-path "$TREE/Cargo.toml" \
  -p xodus-cli --features xodus/key-chain-file --locked \
  --target "$TARGET.$GLIBC_FLOOR"

BIN="$TREE/target/$TARGET/release/xodus-cli"
[ -x "$BIN" ] || { echo "!! xodus-cli was not produced" >&2; exit 1; }

# The point of the target above, checked rather than assumed: a dependency
# can still reach for a newer symbol (a C library built with the container's
# headers, say), and the loader would refuse the binary on every host older
# than that, exactly as in #264.
NEWEST="$(objdump -T "$BIN" | grep -oE 'GLIBC_[0-9]+(\.[0-9]+)+' \
  | sed 's/^GLIBC_//' | sort -uV | tail -n1)"
[ -n "$NEWEST" ] || { echo "!! no glibc symbol versions found in xodus-cli" >&2; exit 1; }
if [ "$(printf '%s\n%s\n' "$NEWEST" "$GLIBC_FLOOR" | sort -V | tail -n1)" != "$GLIBC_FLOOR" ]; then
  echo "!! xodus-cli imports glibc $NEWEST symbols, above the $GLIBC_FLOOR floor:" >&2
  objdump -T "$BIN" | grep -E "GLIBC_$NEWEST( |\)|$)" >&2 || true
  exit 1
fi
echo "   glibc symbols up to $NEWEST (floor $GLIBC_FLOOR)"

echo "== Packaging"
SET="$WORK/set"
rm -rf "$SET"
mkdir -p "$SET"
cp "$BIN" "$SET/xodus-cli"
cp "$TREE/LICENSE" "$SET/LICENSE.GPL-3.0"
printf '%s\n' "$COMMIT" > "$SET/SOURCE-COMMIT"
if [ "$PATCH_LEVEL" -gt 0 ]; then
  for patch in "$PATCHES"/*.patch; do basename "$patch"; done > "$SET/PATCHES"
fi

mkdir -p "$SRC/dist"
OUT="$SRC/dist/xodus-cli-$REV.tar.gz"
tar --sort=name --format=gnu --hard-dereference \
  --mtime="@$SOURCE_DATE_EPOCH" --owner=0 --group=0 --numeric-owner \
  -C "$SET" -cf - . | gzip -n -6 > "$OUT"

# GPL-3.0 requires the source to be available from the same place as the
# binary, so the workflow publishes this tarball beside it -- archived from
# HEAD rather than FETCH_HEAD, so a patched build ships the source it was
# actually built from.
SRC_OUT="$SRC/dist/xodus-src-$REV.tar.gz"
git -C "$TREE" archive --format=tar --prefix="xodus-$REV/" HEAD \
  | gzip -n -6 > "$SRC_OUT"

SHA="$(sha256sum "$OUT" | cut -d' ' -f1)"
echo "$SHA  $(basename "$OUT")" > "$OUT.sha256"
sha256sum "$SRC_OUT" | sed "s|$SRC/dist/||" > "$SRC_OUT.sha256"

echo
echo "   XODUS_SOURCE_COMMIT = \"$COMMIT\""
echo "   XODUS_REV = \"$REV\""
echo "   XODUS_ARCHIVE_SHA256 = \"$SHA\""
echo "   $OUT"
echo "   $SRC_OUT"
