#!/usr/bin/env bash
# Build unreleased Linux candidate artifacts: .deb, AppImage, portable .pyz,
# and a Flatpak bundle.
#
# BOL_RELEASE_CHANNEL selects where the Flatpak's app payload comes from:
#   release (default) — the tracked Flathub manifest, i.e. the pinned tag. The
#                       checkout must be that tag; the payload audit compares
#                       the built tree against it.
#   nightly           — this working tree, like every other artifact here. The
#                       pinned tag is by definition behind the default branch
#                       between releases, so building it would ship a Flatpak
#                       that does not match the .deb/AppImage/.pyz next to it
#                       and would fail that same audit.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VER="$(grep -m1 '^VERSION = ' "$SRC/bol/config.py" | cut -d'"' -f2)"
REV="$(grep -m1 '^WINEGDK_BUILD_REV = ' "$SRC/bol/config.py" | cut -d'"' -f2)"
ENGINE_SHA="$(grep -m1 '^WINEGDK_ARCHIVE_SHA256 = ' "$SRC/bol/config.py" | cut -d'"' -f2)"
XCURL_REV="$(grep -m1 '^OPENSSL_XCURL_REV = ' "$SRC/bol/config.py" | cut -d'"' -f2)"
XCURL_SHA="$(grep -m1 '^OPENSSL_XCURL_ARCHIVE_SHA256 = ' "$SRC/bol/config.py" | cut -d'"' -f2)"
OUT="$SRC/dist"
mkdir -p "$OUT"
CHANNEL="${BOL_RELEASE_CHANNEL:-release}"
case "$CHANNEL" in
  release) FLATPAK_NOTE="" ;;
  nightly) FLATPAK_NOTE=" (working tree, not the pinned tag)" ;;
  *)
    echo "!! BOL_RELEASE_CHANNEL must be 'release' or 'nightly', got '$CHANNEL'" >&2
    exit 1
    ;;
esac
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1782250551}"
[[ "$SOURCE_DATE_EPOCH" =~ ^[0-9]+$ ]] || {
  echo "!! SOURCE_DATE_EPOCH must be a non-negative integer" >&2
  exit 1
}
export SOURCE_DATE_EPOCH

cleanup_build_trees() {
  # Build scratch only. The historical AppImage builder placed its cache in
  # dist/.cache; current builds use XDG_CACHE_HOME, so remove that legacy tree
  # as well. Do not touch config.txt, engine candidates, or XCurl assets.
  rm -rf "$OUT/appimagetool" "$OUT/BedrockOnLinux.AppDir" "$OUT/deb" \
         "$OUT/rpm" "$OUT/rpmbuild" \
         "$OUT/portable" "$OUT/pyz-stage" "$OUT/flatpak-build" "$OUT/.cache"
  rm -f "$OUT"/BedrockOnLinux-*-SHA256SUMS.tmp.* \
        "$OUT/appimage-build.log" "$OUT/flatpak-build.log"
}
trap cleanup_build_trees EXIT
cleanup_build_trees

# A candidate directory must describe this invocation, not accumulate releases.
# These patterns are deliberately limited to application artifacts; engine and
# OpenSSL archives have independent build/review cycles and must be preserved.
shopt -s nullglob
old_application_artifacts=(
  "$OUT"/bedrock-on-linux_*.deb
  "$OUT"/bedrock-on-linux-*.rpm
  "$OUT"/bedrock-on-linux-*.pyz
  "$OUT"/BedrockOnLinux-x86_64.AppImage
  "$OUT"/BedrockOnLinux-*-x86_64.AppImage
  "$OUT"/BedrockOnLinux-*-x86_64.AppImage.zsync
  "$OUT"/BedrockOnLinux-*-x86_64.flatpak
  "$OUT"/BedrockOnLinux-*-SHA256SUMS
  "$OUT"/*portable.tar.gz
)
if (( ${#old_application_artifacts[@]} )); then
  rm -f -- "${old_application_artifacts[@]}"
fi

ENGINE_ASSET="GDK-Proton-xuser-${REV}.tar.gz"
ENGINE="$OUT/$ENGINE_ASSET"
[[ "$ENGINE_SHA" =~ ^[0-9a-f]{64}$ ]] || {
  echo "!! bol/config.py has no valid engine SHA-256 pin" >&2
  exit 1
}
[[ -s "$ENGINE" ]] || {
  echo "!! exact engine candidate is missing: $ENGINE" >&2
  exit 1
}
ACTUAL_ENGINE_SHA="$(sha256sum -- "$ENGINE" | cut -d' ' -f1)"
[[ "$ACTUAL_ENGINE_SHA" == "$ENGINE_SHA" ]] || {
  echo "!! $ENGINE_ASSET hash does not match bol/config.py" >&2
  echo "   expected: $ENGINE_SHA" >&2
  echo "   actual:   $ACTUAL_ENGINE_SHA" >&2
  exit 1
}
[[ -f "$ENGINE.sha256" ]] || {
  echo "!! engine checksum sidecar is missing: $ENGINE.sha256" >&2
  exit 1
}
(
  cd "$OUT"
  sha256sum -c -- "$ENGINE_ASSET.sha256" >/dev/null
) || { echo "!! engine checksum sidecar is invalid" >&2; exit 1; }

XCURL_ASSET="openssl-xcurl-set-${XCURL_REV}.tar.gz"
XCURL="$OUT/$XCURL_ASSET"
[[ "$XCURL_SHA" =~ ^[0-9a-f]{64}$ ]] || {
  echo "!! bol/config.py has no valid OpenSSL XCurl SHA-256 pin" >&2
  exit 1
}
[[ -s "$XCURL" ]] || {
  echo "!! exact OpenSSL XCurl candidate is missing: $XCURL" >&2
  exit 1
}
ACTUAL_XCURL_SHA="$(sha256sum -- "$XCURL" | cut -d' ' -f1)"
[[ "$ACTUAL_XCURL_SHA" == "$XCURL_SHA" ]] || {
  echo "!! $XCURL_ASSET hash does not match bol/config.py" >&2
  echo "   expected: $XCURL_SHA" >&2
  echo "   actual:   $ACTUAL_XCURL_SHA" >&2
  exit 1
}

declare -a built_artifacts=("$ENGINE" "$XCURL")
declare -a verified_artifacts=()
declare -a required_failures=()

echo "== BedrockOnLinux $VER — building UNRELEASED candidate artifacts =="
echo "  ✓ dist/$ENGINE_ASSET (pinned engine input)"
echo "  ✓ dist/$XCURL_ASSET (pinned online-login input)"

DEB="$OUT/bedrock-on-linux_${VER}_amd64.deb"
if command -v dpkg-deb >/dev/null; then
  rm -f -- "$DEB"
  if bash "$SRC/scripts/build-deb.sh" >/dev/null && [[ -s "$DEB" ]]; then
    echo "  ✓ dist/$(basename "$DEB")"
    built_artifacts+=("$DEB")
    verified_artifacts+=("$DEB")
  else
    rm -f -- "$DEB"
    echo "  !! .deb build failed — run scripts/build-deb.sh to see why"
    required_failures+=(".deb")
  fi
else
  echo "  !! .deb build unavailable (dpkg-deb absent)"
  required_failures+=(".deb")
fi

RPM="$OUT/bedrock-on-linux-${VER}-1.x86_64.rpm"
if command -v rpmbuild >/dev/null; then
  rm -f -- "$RPM"
  if bash "$SRC/scripts/build-rpm.sh" >/dev/null && [[ -s "$RPM" ]]; then
    echo "  ✓ dist/$(basename "$RPM")"
    built_artifacts+=("$RPM")
    verified_artifacts+=("$RPM")
  else
    rm -f -- "$RPM"
    echo "  !! .rpm build failed — run scripts/build-rpm.sh to see why"
    required_failures+=(".rpm")
  fi
else
  echo "  !! .rpm build unavailable (rpmbuild absent)"
  required_failures+=(".rpm")
fi

# The zipapp needs host Python 3; GUI and login dependencies (PySide6-Essentials,
# cryptography, …) can be installed on first use by bol/deps.py.
STAGE="$OUT/pyz-stage"
PYZ="$OUT/bedrock-on-linux-${VER}.pyz"
rm -rf "$STAGE"
rm -f -- "$PYZ"
mkdir -p "$STAGE"
cp -r "$SRC/bol" "$STAGE/bol"
install -m644 "$SRC/LICENSE" "$STAGE/LICENSE"
install -Dm644 "$SRC/data/icon.png" "$STAGE/data/icon.png"
find "$STAGE/bol" -name __pycache__ -type d -exec rm -rf {} +
cat > "$STAGE/__main__.py" <<'PYEOF'
from bol.__main__ import main
main()
PYEOF
# zipapp uses the freshly copied/generated mtimes, so two otherwise identical
# builds receive different central-directory timestamps. Write the same
# uncompressed zipapp layout in sorted order with one SOURCE_DATE_EPOCH instead.
python3 - "$STAGE" "$PYZ" "$SOURCE_DATE_EPOCH" <<'PY'
import stat
import sys
import time
import zipfile
from pathlib import Path

stage = Path(sys.argv[1])
output = Path(sys.argv[2])
epoch = max(int(sys.argv[3]), 315532800)  # ZIP timestamps start in 1980.
date_time = time.gmtime(epoch)[:6]

with output.open("wb") as stream:
    stream.write(b"#!/usr/bin/env python3\n")
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as zf:
        for source in sorted(stage.rglob("*"),
                             key=lambda path: path.relative_to(stage).as_posix()):
            if source.is_dir():
                continue
            if source.is_symlink() or not source.is_file():
                raise SystemExit(f"unsafe zipapp entry: {source}")
            relative = source.relative_to(stage).as_posix()
            info = zipfile.ZipInfo(relative, date_time=date_time)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            zf.writestr(info, source.read_bytes())
PY
chmod 755 "$PYZ"
rm -rf "$STAGE"
echo "  ✓ dist/$(basename "$PYZ")"
built_artifacts+=("$PYZ")
verified_artifacts+=("$PYZ")

APPIMAGE="$OUT/BedrockOnLinux-${VER}-x86_64.AppImage"
# The .zsync sidecar carries the block checksums AppImageUpdate and friends use
# to transfer only what changed (issue #191); it describes exactly these bytes,
# so it is built, published and checksummed with the AppImage, never separately.
APPIMAGE_ZSYNC="$APPIMAGE.zsync"
# build-appimage.sh removes its output only at the packaging stage.  Remove it
# before starting too, otherwise an early dependency/network failure could make
# an old file look like the successful result of this invocation.
rm -f -- "$APPIMAGE" "$APPIMAGE_ZSYNC"
APPIMAGE_LOG="$OUT/appimage-build.log"
if bash "$SRC/scripts/build-appimage.sh" >"$APPIMAGE_LOG" 2>&1 \
    && [[ -s "$APPIMAGE" ]]; then
  echo "  ✓ dist/$(basename "$APPIMAGE")"
  built_artifacts+=("$APPIMAGE")
  verified_artifacts+=("$APPIMAGE")
  if [[ -s "$APPIMAGE_ZSYNC" ]]; then
    echo "  ✓ dist/$(basename "$APPIMAGE_ZSYNC") (AppImage delta updates)"
    built_artifacts+=("$APPIMAGE_ZSYNC")
  fi
else
  rm -f -- "$APPIMAGE" "$APPIMAGE_ZSYNC"
  echo "  !! AppImage build failed — last 40 lines of scripts/build-appimage.sh:"
  tail -n 40 "$APPIMAGE_LOG" | sed 's/^/     /'
  required_failures+=("AppImage")
fi

FLATPAK="$OUT/BedrockOnLinux-${VER}-x86_64.flatpak"
rm -f -- "$FLATPAK"
if command -v flatpak-builder >/dev/null \
    || { command -v flatpak >/dev/null \
         && flatpak info org.flatpak.Builder >/dev/null 2>&1; }; then
  FLATPAK_LOG="$OUT/flatpak-build.log"
  declare -a FLATPAK_ARGS=()
  if [[ "$CHANNEL" == "release" ]]; then
    FLATPAK_ARGS=(--release)
  fi
  if bash "$SRC/scripts/build-flatpak.sh" "${FLATPAK_ARGS[@]}" \
      >"$FLATPAK_LOG" 2>&1 && [[ -s "$FLATPAK" ]]; then
    echo "  ✓ dist/$(basename "$FLATPAK")$FLATPAK_NOTE"
    built_artifacts+=("$FLATPAK")
  else
    rm -f -- "$FLATPAK"
    echo "  !! Flatpak build failed — last 40 lines of scripts/build-flatpak.sh:"
    tail -n 40 "$FLATPAK_LOG" | sed 's/^/     /'
    if [[ "$CHANNEL" == "release" ]] \
        && grep -q "payload differs from checkout" "$FLATPAK_LOG"; then
      echo "     ^ the checkout has moved past the tag the Flatpak manifest" \
           "pins. Re-pin flatpak/*.yml to this release, or build this" \
           "candidate with BOL_RELEASE_CHANNEL=nightly."
    fi
    required_failures+=("Flatpak")
  fi
else
  echo "  – Flatpak skipped (no host or org.flatpak.Builder builder) — install it with: flatpak install flathub org.flatpak.Builder"
fi

if (( ${#required_failures[@]} )) \
    && [[ "${BOL_ALLOW_PARTIAL_ARTIFACTS:-0}" != 1 ]]; then
  echo "!! incomplete candidate: required formats failed: ${required_failures[*]}" >&2
  echo "   Set BOL_ALLOW_PARTIAL_ARTIFACTS=1 only for targeted packaging tests." >&2
  exit 1
fi

# Read VERSION and WINEGDK_BUILD_REV back from every supported artifact.  A
# stale/mixed payload is a hard failure and is never checksummed or presented.
echo "== Verifying embedded candidate metadata =="
"$SRC/scripts/verify-release-candidate.sh" "${verified_artifacts[@]}"

# The application checksum file lists only the app artifacts rebuilt by this
# invocation. The engine and XCurl archives ship from their own separately
# attested releases and are not attached here, so their hashes go in a sidecar
# inputs file instead of this one. The temp+rename sequence prevents an
# interrupted hash pass leaving a plausible but incomplete checksum file.
CHECKSUM="$OUT/BedrockOnLinux-${VER}-SHA256SUMS"
CHECKSUM_TMP="$CHECKSUM.tmp.$$"
INPUTS_SUMS="$OUT/BedrockOnLinux-${VER}-inputs.sha256"
INPUTS_TMP="$INPUTS_SUMS.tmp.$$"
rm -f -- "$CHECKSUM" "$CHECKSUM_TMP" "$INPUTS_SUMS" "$INPUTS_TMP"
(
  cd "$OUT"
  for artifact in "${built_artifacts[@]}"; do
    base="$(basename "$artifact")"
    if [ "$base" = "$ENGINE_ASSET" ] || [ "$base" = "$XCURL_ASSET" ]; then
      continue
    fi
    sha256sum -- "$base"
  done
) > "$CHECKSUM_TMP"
mv -f -- "$CHECKSUM_TMP" "$CHECKSUM"
echo "  ✓ dist/$(basename "$CHECKSUM") (application artifacts only)"
(
  cd "$OUT"
  sha256sum -- "$ENGINE_ASSET" "$XCURL_ASSET"
) > "$INPUTS_TMP"
mv -f -- "$INPUTS_TMP" "$INPUTS_SUMS"
echo "  ✓ dist/$(basename "$INPUTS_SUMS") (pinned engine + online-login inputs)"

echo
echo "Unreleased candidate artifacts in $OUT:"
for artifact in "${built_artifacts[@]}" "$CHECKSUM" "$INPUTS_SUMS"; do
  ls -1sh "$artifact"
done
echo
echo "Nothing was tagged, pushed, uploaded, or released."
echo "Keep $ENGINE_ASSET beside the AppImage/.pyz while testing this local candidate."
echo "Smoke-test these files locally before any separate publication step."
