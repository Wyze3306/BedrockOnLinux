#!/usr/bin/env bash
# Package the WebKitGTK stack xodus-cli needs as the xodus-webview-<rev>.tar.xz
# asset the launcher downloads on hosts that ship no WebKitGTK.
#
# xodus-cli links wry/tao unconditionally, so libwebkit2gtk-4.1.so.0 has to be
# loadable before its main() runs -- for the Microsoft sign-in window, for the
# game download, and for `xodus-cli run`, which starts every Store-installed
# game. Distributions package that library; immutable images (SteamOS, issue
# #184) do not and cannot be made to. This bundle is what those hosts use.
#
# Everything is taken from the packages installed in this container, which the
# workflow pins to a snapshot.debian.org timestamp, so the output bytes are a
# function of that pin -- the same property the engine and xodus-cli archives
# rely on. Prints the SHA-256 to pin back into bol/config.py.
set -Eeuo pipefail

WORK="${1:?usage: build-xodus-webview.sh WORKDIR [XODUS_CLI]}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLI="${2:-}"

for t in ldd patchelf tar xz sha256sum python3; do
  command -v "$t" >/dev/null || { echo "!! need $t" >&2; exit 1; }
done

REV="$(grep -m1 '^XODUS_WEBVIEW_REV = ' "$SRC/bol/config.py" | cut -d'"' -f2)"
[ -n "$REV" ] || { echo "!! XODUS_WEBVIEW_REV missing from bol/config.py" >&2; exit 1; }
EXEC_DIR="$(grep -m1 '^XODUS_WEBVIEW_EXEC_DIR = ' "$SRC/bol/config.py" | cut -d'"' -f2)"
[ -n "$EXEC_DIR" ] || { echo "!! XODUS_WEBVIEW_EXEC_DIR missing from bol/config.py" >&2; exit 1; }
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1784308597}"
# aarch64 bundles the same stack for the experimental ARM64EC engine (#250)
# under its own name; the helper literal names that machine's directory.
MULTIARCH="$(uname -m)-linux-gnu"
case "$MULTIARCH" in
  x86_64-linux-gnu) SUFFIX="" PIN_NAME=XODUS_WEBVIEW_SHA256 ;;
  aarch64-linux-gnu)
    SUFFIX="-aarch64" PIN_NAME=XODUS_WEBVIEW_ARM64_SHA256
    EXEC_DIR="$(grep -m1 '^XODUS_WEBVIEW_ARM64_EXEC_DIR = ' "$SRC/bol/config.py" | cut -d'"' -f2)"
    [ -n "$EXEC_DIR" ] || { echo "!! XODUS_WEBVIEW_ARM64_EXEC_DIR missing from bol/config.py" >&2; exit 1; } ;;
  *) echo "!! no WebKitGTK runtime for $MULTIARCH" >&2; exit 1 ;;
esac
export MULTIARCH

SET="$WORK/webview"
rm -rf "$SET"
mkdir -p "$SET"

echo "== collecting the WebKitGTK closure"
XODUS_CLI="$CLI" WEBVIEW_EXEC_DIR="$EXEC_DIR" python3 - "$SET" <<'PY'
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

out = Path(sys.argv[1])
exec_dir = Path(os.environ["WEBVIEW_EXEC_DIR"])

# What xodus-cli itself pulls in from this stack. Passing the built binary is
# better -- it is the definition of "what has to load" -- but the list keeps
# the script runnable on its own, and a mismatch would show up as a missing
# library at run time on every host, not only the ones using this bundle.
ROOTS = ["libwebkit2gtk-4.1.so.0", "libjavascriptcoregtk-4.1.so.0",
         "libgtk-3.so.0", "libgdk-3.so.0", "libsoup-3.0.so.0",
         "libcairo.so.2", "libgdk_pixbuf-2.0.so.0", "libgio-2.0.so.0",
         "libgobject-2.0.so.0", "libglib-2.0.so.0", "libdbus-1.so.3"]

# Left to the host on purpose: the loader/libc family, because a bundled glibc
# cannot mix with the host's loader, and everything that talks to the machine's
# own graphics stack or display server, because those must be the host's.
HOST = re.compile(
    r"^(ld-linux.*|libc|libm|libpthread|libdl|librt|libresolv|libutil|libnsl|"
    r"libanl|libthread_db|libmvec|"
    r"libGL|libGLX|libGLdispatch|libGLESv2|libOpenGL|libEGL|libgbm|libdrm|"
    r"libva|libva-drm|libva-x11|libvdpau|libnvidia.*|"
    r"libX11|libX11-xcb|libxcb.*|libXext|libXfixes|libXrender|libXi|libXcursor|"
    r"libXdamage|libXcomposite|libXrandr|libXinerama|libXtst|libXau|libXdmcp|"
    r"libwayland-.*|libxkbcommon.*)\.so.*$")

# The formats a sign-in window can meet. The rest (AVIF, HEIF, JPEG-XL) drag in
# a video-codec closure several times the size of everything else here.
LOADERS = ("png", "jpeg", "gif", "ico", "bmp", "xpm", "pnm", "tga", "icns")

libdir = Path("/usr/lib") / os.environ["MULTIARCH"]
lib_out = out / "lib"
helper_out = out / "libexec" / exec_dir.name
injected_out = helper_out / "injected-bundle"
gio_out = out / "gio-modules"
pixbuf_out = out / "pixbuf-loaders"
for path in (lib_out, injected_out, gio_out, pixbuf_out, out / "schemas"):
    path.mkdir(parents=True, exist_ok=True)


def resolve(name):
    if os.path.isabs(name):
        return name if os.path.exists(name) else None
    candidate = libdir / name
    return str(candidate) if candidate.exists() else None


def closure(paths):
    found = {}
    for path in paths:
        result = subprocess.run(["ldd", str(path)], text=True,
                                capture_output=True)
        if result.returncode:
            raise SystemExit(f"!! ldd failed for {path}: {result.stderr.strip()}")
        for line in result.stdout.splitlines():
            match = re.match(r"\s*(\S+) => (/\S+)", line)
            if match:
                found.setdefault(match.group(1), match.group(2))
            elif "not found" in line:
                raise SystemExit(f"!! unresolved dependency of {path}: {line.strip()}")
    return found


helpers = [exec_dir / name for name in
           ("WebKitWebProcess", "WebKitNetworkProcess", "WebKitGPUProcess")]
helpers = [path for path in helpers if path.exists()]
if not helpers:
    raise SystemExit(f"!! no WebKit helper processes in {exec_dir}")
injected = exec_dir / "injected-bundle" / "libwebkit2gtkinjectedbundle.so"
gio_modules = sorted(Path(libdir / "gio" / "modules").glob("*.so"))
if not any("gnutls" in module.name or "openssl" in module.name
           for module in gio_modules):
    raise SystemExit("!! no GIO TLS backend installed (glib-networking); the "
                     "sign-in page could not be loaded over HTTPS")
loaders = [path for path in
           sorted((libdir / "gdk-pixbuf-2.0" / "2.10.0" / "loaders").glob("*.so"))
           if any(f"-{name}." in path.name or path.name.endswith(f"-{name}.so")
                  for name in LOADERS)]

roots = []
binary = os.environ.get("XODUS_CLI") or ""
if binary:
    roots.append(binary)
for name in ROOTS:
    path = resolve(name)
    if not path:
        raise SystemExit(f"!! {name} is not installed in this container")
    roots.append(path)
roots += [str(path) for path in helpers + gio_modules + loaders]
if injected.exists():
    roots.append(str(injected))

libraries = closure(roots)
kept = 0
for name, path in sorted(libraries.items()):
    if HOST.match(name):
        continue
    target = lib_out / name
    if not target.exists():
        shutil.copy2(os.path.realpath(path), target)
        target.chmod(0o644)
        kept += 1

sources = {}
for source, destination, rpath in (
        [(path, helper_out / path.name, "$ORIGIN/../../lib")
         for path in helpers]
        + ([(injected, injected_out / injected.name, "$ORIGIN/../../../lib")]
           if injected.exists() else [])
        + [(path, gio_out / path.name, "$ORIGIN/../lib")
           for path in gio_modules]
        + [(path, pixbuf_out / path.name, "$ORIGIN/../lib")
           for path in loaders]):
    shutil.copy2(os.path.realpath(source), destination)
    destination.chmod(0o755 if destination.parent == helper_out else 0o644)
    sources[destination] = rpath

# Every bundled object finds the rest of the bundle relative to itself, so the
# launcher only has to put lib/ on LD_LIBRARY_PATH -- and nothing it unpacks
# depends on where it unpacked it.
for path in sorted(lib_out.iterdir()):
    subprocess.run(["patchelf", "--set-rpath", "$ORIGIN", str(path)], check=True)
for path, rpath in sorted(sources.items()):
    subprocess.run(["patchelf", "--set-rpath", rpath, str(path)], check=True)

# Which Debian packages these files came from, so a bundle can be traced back
# to a snapshot without unpacking it -- and so an apt snapshot bump that moves
# WebKitGTK is visible as a diff rather than only as a changed SHA-256.
used = sorted({os.path.realpath(path) for path in libraries.values()}
              | {os.path.realpath(path) for path in roots})
owners = subprocess.run(["dpkg", "-S"] + used, text=True, capture_output=True)
packages = sorted({line.split(":", 1)[0].split(", ")[0]
                   for line in owners.stdout.splitlines() if ":" in line})
manifest = subprocess.run(
    ["dpkg-query", "-W", "-f", "${binary:Package} ${Version}\n"] + packages,
    text=True, capture_output=True)
(out / "PACKAGES").write_text(manifest.stdout, encoding="utf-8")

print(f"   {kept} libraries, {len(helpers)} helper processes, "
      f"{len(gio_modules)} GIO modules, {len(loaders)} pixbuf loaders, "
      f"from {len(packages)} packages")
PY

echo "== generating the pixbuf loader cache"
QUERY="/usr/lib/$MULTIARCH/gdk-pixbuf-2.0/gdk-pixbuf-query-loaders"
[ -x "$QUERY" ] || QUERY="$(command -v gdk-pixbuf-query-loaders || true)"
[ -n "$QUERY" ] || { echo "!! need gdk-pixbuf-query-loaders" >&2; exit 1; }
# The cache stores absolute module paths, which only exist once the launcher
# has unpacked the bundle; it substitutes this placeholder for that directory.
GDK_PIXBUF_MODULEDIR="$SET/pixbuf-loaders" "$QUERY" \
  | sed "s|$SET|@BOL_WEBVIEW@|g" > "$SET/pixbuf-loaders/loaders.cache"
grep -q '@BOL_WEBVIEW@' "$SET/pixbuf-loaders/loaders.cache" \
  || { echo "!! the loader cache came out without any module paths" >&2; exit 1; }

if [ -f /usr/share/glib-2.0/schemas/gschemas.compiled ]; then
  cp /usr/share/glib-2.0/schemas/gschemas.compiled "$SET/schemas/"
elif command -v glib-compile-schemas >/dev/null; then
  glib-compile-schemas --targetdir "$SET/schemas" /usr/share/glib-2.0/schemas
else
  echo "!! no compiled GSettings schemas; GTK would fall back to defaults" >&2
  exit 1
fi

echo "== checking the bundle is self-contained"
python3 - "$SET" <<'PY'
import os
import re
import subprocess
import sys
from pathlib import Path

out = Path(sys.argv[1])
# Exactly how bol.webview runs xodus-cli. It matters: a host library that the
# bundle keeps on purpose (libGL and friends) pulls in dependencies of its own,
# and whichever object asks for a soname first decides where it comes from.
environment = dict(os.environ, LD_LIBRARY_PATH=str(out / "lib"))
allowed = re.compile(
    r"^/(usr/)?lib(32|64|/x86_64-linux-gnu|/aarch64-linux-gnu)?/(ld-linux|lib(c|m|dl|rt|pthread|"
    r"resolv|util|nsl|anl|thread_db|mvec|GL|GLX|GLdispatch|GLESv2|OpenGL|EGL|"
    r"gbm|drm|va|va-drm|va-x11|vdpau|X11|X11-xcb|xcb.*|Xext|Xfixes|Xrender|Xi|"
    r"Xcursor|Xdamage|Xcomposite|Xrandr|Xinerama|Xtst|Xau|Xdmcp|wayland-.*|"
    r"xkbcommon.*)\.so)")
strays = []
checked = 0
for path in sorted(out.rglob("*")):
    if not path.is_file():
        continue
    with path.open("rb") as handle:
        if handle.read(4) != b"\x7fELF":
            continue
    checked += 1
    result = subprocess.run(["ldd", str(path)], text=True, capture_output=True,
                            env=environment)
    for line in result.stdout.splitlines():
        match = re.match(r"\s*(\S+) => (/\S+)", line)
        if not match:
            if "not found" in line:
                strays.append(f"{path.name}: {line.strip()}")
            continue
        resolved = match.group(2)
        if not resolved.startswith(str(out)) and not allowed.match(resolved):
            strays.append(f"{path.name} -> {resolved}")
if strays:
    raise SystemExit("!! the bundle reaches outside itself:\n  "
                     + "\n  ".join(sorted(set(strays))[:40]))
print(f"   {checked} ELF files, every dependency inside the bundle or in the "
      "host baseline")
PY

echo "== checking the relocatable helper path"
python3 - "$SET" "$EXEC_DIR" <<'PY'
import sys
from pathlib import Path

library = Path(sys.argv[1]) / "lib" / "libwebkit2gtk-4.1.so.0"
literal = sys.argv[2].encode() + b"\x00"
count = library.read_bytes().count(literal)
if count != 1:
    raise SystemExit(
        f"!! expected exactly one {sys.argv[2]!r} literal in "
        f"libwebkit2gtk-4.1.so.0, found {count}; bol.webview rewrites that "
        "string to relocate the helper processes")
print("   helper directory literal is where bol.webview expects it")
PY

if [ -n "$CLI" ]; then
  echo "== checking xodus-cli starts against the bundle"
  # The container has WebKitGTK of its own, so this does not prove the bundle
  # would work on a host without one -- but it does prove the binary loads when
  # the bundled libraries are the ones preferred, which is how the launcher
  # runs it, and it catches a bundle that is internally inconsistent.
  LD_LIBRARY_PATH="$SET/lib" "$CLI" --version >/dev/null \
    || { echo "!! xodus-cli does not start against the bundle" >&2; exit 1; }
fi

echo "== packaging"
mkdir -p "$SRC/dist"
OUT="$SRC/dist/xodus-webview-$REV$SUFFIX.tar.xz"
tar --sort=name --format=gnu --hard-dereference \
  --mtime="@$SOURCE_DATE_EPOCH" --owner=0 --group=0 --numeric-owner \
  -C "$SET" -cf - . | xz -9 -T1 > "$OUT"

SHA="$(sha256sum "$OUT" | cut -d' ' -f1)"
echo "$SHA  $(basename "$OUT")" > "$OUT.sha256"

echo
echo "   XODUS_WEBVIEW_REV = \"$REV\""
echo "   $PIN_NAME = \"$SHA\""
echo "   $OUT ($(du -h "$OUT" | cut -f1))"
