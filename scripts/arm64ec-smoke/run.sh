#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Smoke test for the experimental ARM64EC engine (#250), run by
# build-engine.yml in Valve's steamrt4 arm64 SDK image on an ARM runner: the
# Steam Runtime 4 userland the engine targets, plus llvm-mingw for the probe.
#
#   run.sh ENGINE_DIR
#
# 1. WineGDK's aarch64 loader starts and says which Wine it is.
# 2. wineboot creates a prefix with no display.
# 3. An x86-64 program runs through FEX, sees an ARM64 machine, and loads our
#    ARM64X xgameruntime.dll and vkd3d d3d12.dll from emulated code.
set -Eeuo pipefail
ENGINE="${1:?usage: run.sh ENGINE_DIR}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
export WINEPREFIX="$WORK/pfx" WINEDEBUG="-all" DISPLAY=""
export WINEDLLOVERRIDES="mscoree=d;mshtml=d;winemenubuilder.exe=d"
export LD_LIBRARY_PATH="$ENGINE/files/lib/aarch64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
WINE="$ENGINE/files/bin-arm64/wine"

echo "== wine --version"
version="$("$WINE" --version)"
echo "$version"
[[ "$version" == wine-11.* ]] || { echo "!! not WineGDK's Wine 11" >&2; exit 1; }

echo "== wineboot"
timeout 600 "$WINE" wineboot -i
timeout 120 "$ENGINE/files/bin-arm64/wineserver" -w
sys32="$WINEPREFIX/drive_c/windows/system32"
[[ -f "$sys32/kernel32.dll" ]] || { echo "!! wineboot made no prefix" >&2; exit 1; }

echo "== registering FEX as the x86-64 emulator"
# What Proton's default_pfx_arm64 carries; a bare wineboot does not.
cp "$ENGINE/files/lib/wine/aarch64-windows/libarm64ecfex.dll" \
   "$ENGINE/files/lib/wine/aarch64-windows/libwow64fex.dll" "$sys32/"
"$WINE" reg add 'HKLM\Software\Microsoft\Wow64\amd64' /ve /d libarm64ecfex.dll /f
"$WINE" reg add 'HKLM\Software\Microsoft\Wow64\x86' /ve /d libwow64fex.dll /f
for dll in d3d12.dll d3d12core.dll; do
  cp "$ENGINE/files/lib/wine/vkd3d-proton/aarch64-windows/$dll" "$sys32/"
done
cp "$ENGINE/files/lib/wine/aarch64-windows/xgameruntime.dll" "$sys32/"
cp "$ENGINE/files/lib/wine/x86_64-windows/xgameruntime.dll.threading" "$sys32/"
timeout 120 "$ENGINE/files/bin-arm64/wineserver" -w

echo "== x86-64 probe through FEX"
x86_64-w64-mingw32-clang -O2 -o "$WORK/probe.exe" "$HERE/probe.c"
python3 - "$WORK/probe.exe" <<'PY'
import struct, sys
d = open(sys.argv[1], "rb").read()
e = struct.unpack_from("<I", d, 0x3C)[0]
assert struct.unpack_from("<H", d, e + 4)[0] == 0x8664, "probe is not x86-64"
PY
out="$(timeout 600 "$WINE" "$WORK/probe.exe" 2>&1 | tr -d '\r')" || true
echo "$out"
grep -qx "probe: OK" <<< "$out" || { echo "!! the x86-64 probe failed" >&2; exit 1; }
timeout 120 "$ENGINE/files/bin-arm64/wineserver" -k || true
echo "== ARM64EC engine smoke test passed"
