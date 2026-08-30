#!/usr/bin/env bash
# Build BedrockOnLinux.app, the macOS application bundle.
# Usage: scripts/build-macos-app.sh   -> dist/BedrockOnLinux-<ver>-macos.zip
#
# Runs on macOS *and* on Linux. Nothing in the bundle is compiled here: the
# launcher is Python, and the Qt and cryptography wheels are downloaded
# prebuilt. pip can resolve wheels for a platform it is not running on, so a
# Linux box asks for the macOS universal2 ones by tag and gets exactly the
# files a Mac would have got -- which is what makes this cross-buildable at
# all. The differences are only in the tools: sips/iconutil/ditto on a Mac,
# Pillow and zip(1) elsewhere.
#
# It deliberately does not bundle a Windows runtime. Game Porting Toolkit and
# CrossOver are separate installs with their own licences, and bol.winemac
# finds whichever one the player has.
#
# Codesigning is left to the caller, and is possible only on a Mac. An
# unsigned bundle runs after the usual right-click-Open (or `xattr -dr
# com.apple.quarantine`); to ship one, set CODESIGN_IDENTITY and it is signed
# with hardened runtime.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VER="$(grep -m1 '^VERSION = ' "$SRC/bol/config.py" | cut -d'"' -f2)"
OUT="$SRC/dist"
APP="$OUT/BedrockOnLinux.app"
BUNDLE_ID="io.github.wyze3306.BedrockOnLinux"
PYTHON="${PYTHON:-python3}"

NATIVE=0
[[ "$(uname -s)" == "Darwin" ]] && NATIVE=1
[[ -f "$SRC/data/icon.png" ]] || { echo "data/icon.png missing" >&2; exit 1; }
if (( NATIVE )); then
  echo "Building on macOS."
else
  echo "Cross-building a macOS bundle on $(uname -s)."
  for tool in zip; do
    command -v "$tool" >/dev/null || {
      echo "cross-building needs '$tool'" >&2
      exit 1
    }
  done
  "$PYTHON" -c "import PIL" 2>/dev/null || {
    echo "cross-building needs Pillow for the icon (pip install Pillow)" >&2
    exit 1
  }
fi

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cp -r "$SRC/bol" "$APP/Contents/Resources/bol"
install -m755 "$SRC/bedrock-on-linux" "$APP/Contents/Resources/bedrock-on-linux"
cp "$SRC/data/icon.png" "$APP/Contents/Resources/icon.png"
find "$APP/Contents/Resources" -name __pycache__ -type d -prune -exec rm -rf {} +

# The Qt toolkit, beside bol/ so it is on sys.path with nothing to install.
# python-xlib is deliberately absent: it talks to an X server, and bol.deps
# drops it from the macOS dependency set for the same reason.
#
# Off a Mac the wheels have to be asked for by tag. cp39-abi3 is not a guess:
# every binary wheel here is abi3, which is also what lets the bundle run on
# macOS's own Python 3.9 as well as on a newer Homebrew one. universal2 covers
# Intel and Apple Silicon in one file, and --abi none admits the pure-Python
# ones (packaging) that carry no ABI tag at all.
PIP_ARGS=(--quiet --no-cache-dir --no-compile --no-deps --only-binary=:all:)
if (( ! NATIVE )); then
  PIP_ARGS+=(
    --python-version 3.9 --implementation cp --abi abi3 --abi none
    --platform macosx_12_0_universal2
    --platform macosx_11_0_universal2
    --platform macosx_10_12_universal2
  )
fi
"$PYTHON" -m pip install "${PIP_ARGS[@]}" --target "$APP/Contents/Resources" \
  "PySide6-Essentials==6.9.3" "shiboken6==6.9.3" "packaging==26.2" \
  "cryptography==43.0.3"

# Prove they really are Mach-O. A silent fall back to the host's own wheels is
# the one way this can produce a bundle that looks right and cannot start, and
# a Linux .so inside a .app fails much later, on the player's Mac.
"$PYTHON" - "$APP/Contents/Resources" <<'CHECK'
import pathlib
import sys

# Mach-O, either a single architecture or a universal ("fat") wrapper.
MAGIC = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe",
         b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}
root = pathlib.Path(sys.argv[1])
libraries = sorted(root.rglob("*.so")) + sorted(root.rglob("*.dylib"))
if not libraries:
    sys.exit("no compiled extension was installed into the bundle")
wrong = [str(p.relative_to(root)) for p in libraries
         if p.open("rb").read(4) not in MAGIC]
if wrong:
    sys.exit("not macOS binaries: " + ", ".join(wrong[:5]))
print(f"{len(libraries)} macOS binaries bundled")
CHECK
rm -rf "$APP/Contents/Resources/bin"
find "$APP/Contents/Resources" -name __pycache__ -type d -prune -exec rm -rf {} +

# The icon. On a Mac, iconutil wants a full .iconset and sips does the
# resizing; elsewhere scripts/png2icns.py writes the same container directly.
if (( NATIVE )); then
  ICONSET="$OUT/icon.iconset"
  rm -rf "$ICONSET"
  mkdir -p "$ICONSET"
  for size in 16 32 64 128 256 512; do
    sips -z "$size" "$size" "$SRC/data/icon.png" \
      --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
    double=$((size * 2))
    sips -z "$double" "$double" "$SRC/data/icon.png" \
      --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
  done
  iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/icon.icns"
  rm -rf "$ICONSET"
else
  "$PYTHON" "$SRC/scripts/png2icns.py" "$SRC/data/icon.png" \
    "$APP/Contents/Resources/icon.icns"
fi

# The bundle's executable. A shell stub rather than a compiled launcher: it
# keeps the bundle buildable with nothing but the system Python.
#
# It runs the repository's own entry script out of Resources, and that detail
# matters twice. The script finds bol/ beside itself, so nothing depends on
# PYTHONPATH; and it makes sys.argv[0] a path inside the bundle, which is how
# bol.update recognises that it is running from one -- an app updates by being
# replaced whole, not by having a single file inside it overwritten. A
# `python3 -c` invocation would leave argv[0] as "-c" and lose both.
#
# Which Python runs it. The Qt and cryptography wheels bundled above are
# abi3, so any Python 3.9 or newer loads them -- but /usr/bin/python3 is only a
# shim on a Mac that has never installed the Command Line Tools, and touching
# it there pops the Xcode installer instead of opening the launcher. So a
# Homebrew Python is preferred when one is present, and BOL_PYTHON overrides
# the lot.
cat > "$APP/Contents/MacOS/BedrockOnLinux" <<'LAUNCHER'
#!/bin/sh
# Generated by scripts/build-macos-app.sh — do not edit in the bundle.
here="$(cd "$(dirname "$0")" && pwd)"
resources="$(cd "$here/../Resources" && pwd)"
for candidate in \
  "$BOL_PYTHON" \
  /opt/homebrew/bin/python3 \
  /usr/local/bin/python3 \
  /usr/bin/python3
do
  [ -n "$candidate" ] && [ -x "$candidate" ] || continue
  exec "$candidate" "$resources/bedrock-on-linux" "$@"
done
echo "BedrockOnLinux: no Python 3 was found to run the launcher with." >&2
echo "Install one with:  brew install python3" >&2
exit 1
LAUNCHER
chmod 755 "$APP/Contents/MacOS/BedrockOnLinux"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>BedrockOnLinux</string>
  <key>CFBundleDisplayName</key><string>BedrockOnLinux</string>
  <key>CFBundleIdentifier</key><string>${BUNDLE_ID}</string>
  <key>CFBundleVersion</key><string>${VER}</string>
  <key>CFBundleShortVersionString</key><string>${VER}</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>BedrockOnLinux</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <!-- The launcher window is the application; it never runs as an agent. -->
  <key>LSUIElement</key><false/>
</dict>
</plist>
PLIST

if [[ -n "${CODESIGN_IDENTITY:-}" ]]; then
  (( NATIVE )) || { echo "codesigning needs a Mac" >&2; exit 1; }
  codesign --force --deep --options runtime --timestamp \
    --sign "$CODESIGN_IDENTITY" "$APP"
  codesign --verify --strict --verbose=2 "$APP"
fi

ZIP="$OUT/BedrockOnLinux-${VER}-macos.zip"
rm -f "$ZIP"
if (( NATIVE )); then
  # ditto, not zip(1): it is the one that preserves the resource forks and the
  # signature a bundle needs to survive being unpacked on another Mac.
  ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"
else
  # A cross-built bundle carries no signature and no resource forks, so a
  # plain zip loses nothing -- except the executable bit, which -X keeps.
  ( cd "$OUT" && zip -qXr "$ZIP" "$(basename "$APP")" )
fi

echo "Built $APP"
echo "Built $ZIP"
