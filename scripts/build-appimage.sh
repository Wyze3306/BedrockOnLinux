#!/usr/bin/env bash
# Portable AppImage with bundled CPython, PySide6-Essentials (Qt), login
# dependencies and CA store.
#
# An AppImage cannot declare dependencies, and the Qt platform-plugin runtime
# libraries stay host GUI dependencies as with standard AppImages -- but unlike
# the Tk GUI this replaced, a missing one is fatal and silent: Qt aborts the
# process natively with "could not load the Qt platform plugin xcb" before
# control returns to Python, so bol.gui's own error reporting never runs.
# The full list is pinned in tests/test_application_packaging.py
# (QT_HOST_LIBRARIES) and declared by the .deb and .rpm; the short version is
# libX11/libX11-xcb, libxkbcommon(-x11), libGL/libEGL, fontconfig, freetype
# and the xcb-util family (cursor, icccm, image, keysyms, render-util, util).
#
# Everything else Qt links that is *not* a GUI or driver library is bundled
# here instead -- libzstd first among them (issue #205) -- and the host
# dependency audit below fails the build if that list ever grows behind our
# back.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VER="$(grep -m1 '^VERSION = ' "$SRC/bol/config.py" | cut -d'"' -f2)"
OUT="$SRC/dist"
CACHE="${BOL_APPIMAGE_BUILD_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/bedrock-on-linux-build/appimage}"
mkdir -p "$CACHE" "$OUT"
GLIBC_CEILING="${BOL_APPIMAGE_GLIBC_CEILING:-2.31}"
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1782250551}"
export SOURCE_DATE_EPOCH

# Update information (issue #191). AppImageUpdate, AppImageLauncher, AM/AppMan
# and friends read this string out of the runtime's .upd_info section; it names
# the release to look at and the .zsync sidecar appimagetool writes next to the
# AppImage, so an update transfers only the blocks that actually changed
# instead of the whole ~200 MB bundle. The repository comes from bol/config.py,
# the same one the launcher's own updater asks, so a fork updates from its own
# releases. The channel picks the tag: a nightly must follow the rolling
# "nightly" prerelease rather than replace itself with the stable release.
# Setting BOL_APPIMAGE_UPDATE_INFO to the empty string builds an AppImage with
# no update information at all.
SELF_REPO="$(grep -m1 '^WINEGDK_PREBUILT_REPO = ' "$SRC/bol/config.py" | cut -d'"' -f2)"
[[ "$SELF_REPO" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] || {
  echo "!! could not read the release repository from bol/config.py" >&2
  exit 1
}
case "${BOL_RELEASE_CHANNEL:-release}" in
  nightly) UPDATE_TAG="nightly" ;;
  *)       UPDATE_TAG="latest" ;;
esac
UPDATE_INFO="${BOL_APPIMAGE_UPDATE_INFO-gh-releases-zsync|${SELF_REPO%/*}|${SELF_REPO#*/}|${UPDATE_TAG}|BedrockOnLinux-*-x86_64.AppImage.zsync}"

APPDIR="$OUT/BedrockOnLinux.AppDir"; rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/applications" \
         "$APPDIR/usr/share/icons/hicolor/256x256/apps" \
         "$APPDIR/usr/share/licenses/bedrock-on-linux"

PBS_TAG="${PBS_TAG:-20260610}"; PBS_PY="${PBS_PY:-3.12.13}"
PBS_ASSET="cpython-${PBS_PY}+${PBS_TAG}-x86_64-unknown-linux-gnu-install_only.tar.gz"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/${PBS_ASSET}"
PBS_TARBALL="$CACHE/$PBS_ASSET"
PBS_SHA256="c218f50baeb2c06a30c2f03db5986b2bad6ab7c8a52faad2d5a59bda0677b93a"

download_verified() {
  local url="$1" output="$2" expected="$3" label="$4" actual
  if [[ -f "$output" ]]; then
    read -r actual _ < <(sha256sum "$output")
    if [[ "$actual" == "$expected" ]]; then
      return
    fi
    echo "!! cached $label hash mismatch; downloading reviewed bytes again" >&2
    rm -f -- "$output"
  fi
  curl -fL --retry 3 -o "$output.part" "$url"
  read -r actual _ < <(sha256sum "$output.part")
  if [[ "$actual" != "$expected" ]]; then
    rm -f -- "$output.part"
    echo "!! $label SHA-256 mismatch: got $actual, expected $expected" >&2
    exit 1
  fi
  mv -f -- "$output.part" "$output"
}

download_verified "$PBS_URL" "$PBS_TARBALL" "$PBS_SHA256" \
  "python-build-standalone archive"
echo "== unpacking Python into the AppDir"
tar -C "$APPDIR/usr" -xzf "$PBS_TARBALL"
PYHOME="$APPDIR/usr/python"; PYBIN="$PYHOME/bin/python3.12"
PYLIB="$PYHOME/lib"; DYN="$PYLIB/python3.12/lib-dynload"
[[ -x "$PYBIN" ]] || { echo "!! bundled python missing" >&2; exit 1; }

# python-build-standalone bundles its own Tcl/Tk 9 + _tkinter for `tkinter`,
# which nothing here imports anymore (PySide6/Qt replaced it). Drop the
# extension module and the Tcl/Tk libraries behind it so they can never be
# picked up, and so the audit below has that much less to walk.
rm -f "$DYN"/_tkinter.*.so
rm -f "$PYLIB"/libtcl9*.so "$PYLIB"/libtcl9tk9*.so
rm -rf "$PYLIB"/tcl9* "$PYLIB"/tk9* 2>/dev/null || true

echo "== installing portable cryptography + certifi + PySide6-Essentials + python-xlib into the bundle"
# Hash-pinned, wheels only, no sdist builds: the closure + SHA-256s live in
# third_party/requirements-appimage.txt (--require-hashes rejects any mismatch).
"$PYBIN" -m pip install --no-cache-dir --no-compile \
    --no-deps --require-hashes --only-binary=:all: \
    -r "$SRC/third_party/requirements-appimage.txt" \
    >/dev/null

# PySide6 dev tools embed the build host's absolute Python path in their
# shebangs. They are not needed at runtime (the imports live in
# site-packages), so drop them before the relocatability audit below.
rm -f "$PYHOME"/bin/pyside6-* 2>/dev/null || true

# libQt6Core.so.6 links libzstd.so.1, and unlike the X11/GL stack above that
# is no host GUI library: a system with no zstd runtime at all -- NixOS via
# appimage-run, a minimal container -- fails the launcher's very first
# `import PySide6.QtCore` with "libzstd.so.1: cannot open shared object file"
# and never draws a window (issue #205). It is not on the AppImage excludelist
# either, so bundle it beside the wheel's own libicu, taken from the pinned
# Debian 11 snapshot the containerised builds already use: identical bytes on
# every build host, and GLIBC_2.14 at most -- well under the baseline below.
QT_LIB="$PYLIB/python3.12/site-packages/PySide6/Qt/lib"
[[ -d "$QT_LIB" ]] || {
  echo "!! the PySide6 wheel no longer keeps its Qt libraries in Qt/lib" >&2
  exit 1
}
ZSTD_DEB="$CACHE/libzstd1_1.4.8+dfsg-2.1_amd64.deb"
download_verified \
  "https://snapshot.debian.org/archive/debian/20260701T000000Z/pool/main/libz/libzstd/libzstd1_1.4.8%2Bdfsg-2.1_amd64.deb" \
  "$ZSTD_DEB" \
  "5dcadfbb743bfa1c1c773bff91c018f835e8e8c821d423d3836f3ab84773507b" \
  "Debian 11 libzstd1"
echo "== bundling libzstd.so.1, the one non-GUI library Qt asked the host for"
python3 - "$ZSTD_DEB" "$QT_LIB/libzstd.so.1" \
    "$APPDIR/usr/share/licenses/bedrock-on-linux/libzstd1.copyright" <<'PY'
import io
import lzma
import sys
import tarfile
from pathlib import Path

package, library, licence = (Path(value) for value in sys.argv[1:4])
raw = package.read_bytes()
if raw[:8] != b"!<arch>\n":
    raise SystemExit(f"{package} is not a Debian package")
# A .deb is an ar archive: 60-byte member headers, member data padded to an
# even length. Read it here rather than shelling out, so the build needs
# neither dpkg nor xz-utils on the host that runs it.
members, offset = {}, 8
while offset + 60 <= len(raw):
    header = raw[offset:offset + 60]
    name = header[:16].decode("ascii", "replace").strip().rstrip("/")
    start = offset + 60
    size = int(header[48:58])
    members[name] = raw[start:start + size]
    offset = start + size + size % 2
if "data.tar.xz" not in members:
    raise SystemExit(f"{package} carries no data.tar.xz")
wanted = {
    "./usr/lib/x86_64-linux-gnu/libzstd.so.1.4.8": library,
    "./usr/share/doc/libzstd1/copyright": licence,
}
with tarfile.open(fileobj=io.BytesIO(lzma.decompress(members["data.tar.xz"])),
                  mode="r:") as payload:
    for name, destination in wanted.items():
        try:
            member = payload.getmember(name)
        except KeyError:
            raise SystemExit(f"{package.name} no longer contains {name}")
        source = payload.extractfile(member)
        if source is None:
            raise SystemExit(f"{name} is not a regular file in {package.name}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read())
        destination.chmod(0o755 if destination == library else 0o644)
print(f"  bundled {library.name} + its licence from {package.name}")
PY

PY3="$PYLIB/python3.12"
rm -rf "$PY3/test" "$PY3/idlelib" "$PY3/turtledemo" "$PY3/tkinter/test" \
       "$PY3/lib2to3" "$PY3/ensurepip" "$PYHOME/share" "$PYHOME/include" 2>/dev/null || true
rm -f "$DYN"/_crypt.*.so
find "$PYHOME" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$PYHOME" -name '*.pyc' -delete 2>/dev/null || true

# AppImage is the cross-distribution artifact, so host-built Tcl/Tk must not
# silently import the build machine's newer glibc.  This caught the old cache
# requiring GLIBC_2.38, which made an otherwise valid AppImage fail on Ubuntu
# 22.04, Mint 21, Debian 12 and the Steam Runtime. Audit every bundled ELF,
# including binary Python wheels, against the Debian 11/sniper baseline.
command -v readelf >/dev/null 2>&1 \
  || { echo "!! readelf is required for the AppImage ABI audit" >&2; exit 1; }
python3 - "$APPDIR" "$GLIBC_CEILING" <<'PY'
import os
import re
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])
ceiling = tuple(map(int, sys.argv[2].split(".")))
if len(ceiling) != 2:
    raise SystemExit("invalid BOL_APPIMAGE_GLIBC_CEILING (expected MAJOR.MINOR)")
worst = ((0, 0), None)
violations = []
rpath_violations = []
forbidden_dependencies = []
sonames = {}
dependencies = {}
count = 0
for path in root.rglob("*"):
    if not path.is_file():
        continue
    try:
        with path.open("rb") as stream:
            if stream.read(4) != b"\x7fELF":
                continue
    except OSError:
        continue
    count += 1
    result = subprocess.run(
        ["readelf", "--version-info", "--wide", str(path)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise SystemExit(f"readelf failed for {path}: {result.stderr.strip()}")
    versions = [tuple(map(int, value)) for value in
                re.findall(r"GLIBC_(\d+)\.(\d+)", result.stdout)]
    required = max(versions, default=(0, 0))
    if required > worst[0]:
        worst = (required, path.relative_to(root))
    if required > ceiling:
        violations.append((required, path.relative_to(root)))
    dynamic = subprocess.run(
        ["readelf", "--dynamic", "--wide", str(path)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False,
    )
    if dynamic.returncode:
        raise SystemExit(f"readelf failed for {path}: {dynamic.stderr.strip()}")
    for value in re.findall(
            r"\((?:RPATH|RUNPATH)\).*?\[([^]]*)\]", dynamic.stdout):
        absolute = [entry for entry in value.split(":")
                    if entry and os.path.isabs(entry)]
        if absolute:
            rpath_violations.append((path.relative_to(root), absolute))
    dependencies[path] = re.findall(
        r"\(NEEDED\).*?\[([^]]+)\]", dynamic.stdout)
    soname = re.search(r"\(SONAME\).*?\[([^]]+)\]", dynamic.stdout)
    if soname:
        sonames.setdefault(soname.group(1), path)
    for dependency in dependencies[path]:
        if dependency in {"libcrypt.so.1", "libXss.so.1"}:
            forbidden_dependencies.append((path.relative_to(root), dependency))
if not count:
    raise SystemExit("AppImage staging tree contains no ELF files")
if violations:
    details = "\n".join(
        f"  GLIBC_{version[0]}.{version[1]}: {path}"
        for version, path in sorted(violations, reverse=True)[:40]
    )
    raise SystemExit(
        "AppImage contains host-built ELF files newer than GLIBC_%d.%d:\n%s\n"
        "Check the pinned wheel versions in "
        "third_party/requirements-appimage.txt against this ceiling."
        % (*ceiling, details)
    )
if rpath_violations:
    details = "\n".join(
        f"  {path}: {':'.join(entries)}"
        for path, entries in rpath_violations[:40]
    )
    raise SystemExit(
        "AppImage contains absolute RPATH/RUNPATH entries; bundled ELF "
        "paths must be relative to $ORIGIN:\n" + details
    )
if forbidden_dependencies:
    details = "\n".join(
        f"  {path}: {dependency}"
        for path, dependency in forbidden_dependencies
    )
    raise SystemExit(
        "AppImage retains avoidable legacy host dependencies:\n" + details
    )
print("  AppImage ABI OK: %d ELF files, maximum GLIBC_%d.%d (%s)" %
      (count, worst[0][0], worst[0][1], worst[1]))

# Libraries the host is expected to own. Nearly all are on the AppImage
# excludelist -- the C/C++ runtime, the X11/GL stack, zlib, fontconfig and
# freetype -- and glib, gthread and D-Bus stay host libraries too, because a
# bundled copy would shadow the desktop's own GTK/D-Bus stack for the whole
# process. Anything Qt needs that is not in here has to be bundled instead.
# Keep in step with QT_HOST_LIBRARIES in tests/test_application_packaging.py
# and with the .deb/.rpm dependency lists.
host_libraries = {
    "ld-linux-x86-64.so.2", "libc.so.6", "libdl.so.2", "libm.so.6",
    "libpthread.so.0", "librt.so.1", "libgcc_s.so.1", "libstdc++.so.6",
    "libz.so.1", "libglib-2.0.so.0", "libgthread-2.0.so.0", "libdbus-1.so.3",
    "libfontconfig.so.1", "libfreetype.so.6", "libGL.so.1", "libEGL.so.1",
    "libX11.so.6", "libX11-xcb.so.1", "libxkbcommon.so.0",
    "libxkbcommon-x11.so.0", "libxcb.so.1", "libxcb-cursor.so.0",
    "libxcb-icccm.so.4", "libxcb-image.so.0", "libxcb-keysyms.so.1",
    "libxcb-randr.so.0", "libxcb-render.so.0", "libxcb-render-util.so.0",
    "libxcb-shape.so.0", "libxcb-shm.so.0", "libxcb-sync.so.1",
    "libxcb-util.so.1", "libxcb-xfixes.so.0", "libxcb-xkb.so.1",
}
# What bol.gui imports, and what Qt loads behind it to put a window on the
# screen. Plugins Qt only tries opportunistically -- image formats, SQL
# drivers, the GTK platform theme -- are deliberately not roots: Qt carries on
# without them, and several link libraries (libpq, libgtk-3) this app never
# wants to require.
site_packages = root / "usr/python/lib/python3.12/site-packages"
import_path = [site_packages / "PySide6/QtCore.abi3.so",
               site_packages / "PySide6/QtGui.abi3.so",
               site_packages / "PySide6/QtWidgets.abi3.so",
               site_packages / "PySide6/Qt/plugins/platforms/libqxcb.so"]
absent = [str(path.relative_to(root)) for path in import_path
          if not path.is_file()]
if absent:
    raise SystemExit(
        "the pinned PySide6 wheel no longer provides the files the launcher "
        "imports:\n  " + "\n  ".join(absent))
from_host = {}
reached = set()
queue = list(import_path)
while queue:
    path = queue.pop()
    if path in reached:
        continue
    reached.add(path)
    for dependency in dependencies.get(path, ()):
        if dependency in sonames:
            queue.append(sonames[dependency])
        elif dependency in host_libraries:
            from_host.setdefault(dependency, [])
        else:
            from_host.setdefault(dependency, []).append(
                str(path.relative_to(root)))
undeclared = {name: users for name, users in from_host.items() if users}
if undeclared:
    details = "\n".join(
        f"  {name}  (needed by {', '.join(sorted(set(users)))})"
        for name, users in sorted(undeclared.items()))
    raise SystemExit(
        "the launcher's Qt import path needs libraries that are neither "
        "bundled here nor declared host dependencies:\n" + details + "\n"
        "An AppImage cannot ask for these, so a host without one gets an "
        "ImportError instead of a window (issue #205). Bundle it from a "
        "pinned Debian 11 package, the way libzstd.so.1 is bundled above -- "
        "or, if the host really must own it, add it to host_libraries here, "
        "to QT_HOST_LIBRARIES in tests/test_application_packaging.py and to "
        "the .deb/.rpm dependency lists.")
print("  AppImage host dependencies OK: the import path resolves %d bundled "
      "objects and asks the host for %d libraries"
      % (len(reached), len(from_host)))
PY

[[ -f "$SRC/data/icon.png" ]] || { echo "data/icon.png missing" >&2; exit 1; }
[[ -f "$SRC/LICENSE" ]] || { echo "LICENSE missing" >&2; exit 1; }
install -m755 "$SRC/bedrock-on-linux" "$APPDIR/usr/bin/bedrock-on-linux"
cp -r "$SRC/bol" "$APPDIR/usr/bin/bol"
find "$APPDIR/usr/bin/bol" -name __pycache__ -type d -exec rm -rf {} +
mkdir -p "$APPDIR/usr/bin/data"
cp "$SRC/data/icon.png" "$APPDIR/usr/bin/data/icon.png"
cp "$SRC/data/icon.png" "$APPDIR/bedrock-on-linux.png"
cp "$SRC/data/icon.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/bedrock-on-linux.png"
# Normalise the launcher entry without touching the Play action's argument.
sed '0,/^Exec=/s|^Exec=.*|Exec=bedrock-on-linux gui|' \
   "$SRC/data/bedrock-on-linux.desktop" > "$APPDIR/bedrock-on-linux.desktop"
cp "$APPDIR/bedrock-on-linux.desktop" \
   "$APPDIR/usr/share/applications/bedrock-on-linux.desktop"
install -m644 "$SRC/LICENSE" \
  "$APPDIR/usr/share/licenses/bedrock-on-linux/LICENSE"

cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
PY="$HERE/usr/python/bin/python3.12"
CERT="$HERE/usr/python/lib/python3.12/site-packages/certifi/cacert.pem"
if [ -f "$CERT" ]; then
  export SSL_CERT_FILE="$CERT"
  export REQUESTS_CA_BUNDLE="$CERT"
fi
unset PYTHONHOME PYTHONPATH        # self-contained; libs found via rpath
exec "$PY" "$HERE/usr/bin/bedrock-on-linux" "$@"
EOF
chmod 755 "$APPDIR/AppRun"
/bin/sh -n "$APPDIR/AppRun" \
  || { echo "!! AppRun is not compatible with /bin/sh" >&2; exit 1; }

echo "== verifying the bundle: PySide6/Qt + cryptography + HTTPS, all self-contained"
env -i SSL_CERT_FILE="$PYLIB/python3.12/site-packages/certifi/cacert.pem" \
   SSL_CERT_DIR=/nonexistent \
   ${DISPLAY:+DISPLAY="$DISPLAY"} ${XAUTHORITY:+XAUTHORITY="$XAUTHORITY"} \
   "$PYBIN" - <<'PY'
import os
import ssl
import time
import urllib.error
import urllib.request

import cryptography
import shiboken6
import Xlib
from importlib.metadata import version
from PySide6 import __version__ as pyside6_version
from PySide6.QtCore import QLibraryInfo
expected = {
    "cryptography": "43.0.3",
    "certifi": "2026.6.17",
    "cffi": "2.0.0",
    "pycparser": "3.0",
    "shiboken6": "6.9.3",
    "pyside6_essentials": "6.9.3",
    "python-xlib": "0.33",
    "six": "1.17.0",
}
actual = {package: version(package) for package in expected}
assert actual == expected, (actual, expected)
assert pyside6_version == "6.9.3", pyside6_version
# The bundled Qt plugins (platforms/libqxcb.so, etc.) ship inside the
# PySide6 wheel itself; PySide6 points Qt's plugin search path at its own
# package directory on import, so this is what the app will actually find
# at runtime rather than anything on the host.
plugins_path = QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath)
assert os.path.isdir(plugins_path), f"Qt plugins path missing: {plugins_path}"
def verify_https():
    # The point of this check is that the bundled OpenSSL + certifi CA can
    # complete a real TLS handshake. A certificate/TLS failure is a genuine
    # bundling defect and must fail the build; a network/HTTP error (offline
    # runner, sandbox, or the host being briefly unreachable) is environmental
    # and must not, since the crypto stack already imported and loaded its CA.
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen("https://api.github.com", timeout=20) as response:
                response.read(16)
            return "HTTPS via bundled CA"
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            # Only a certificate-verification failure proves the bundled CA is
            # broken. Other ssl.SSLError subtypes (SSLEOFError, reset mid-
            # handshake, etc.) are transport hiccups -> retry, then skip.
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise SystemExit(f"bundled TLS/CA verification failed: {reason}")
            last = exc
        except ssl.SSLCertVerificationError as exc:
            raise SystemExit(f"bundled TLS/CA verification failed: {exc}")
        except (ssl.SSLError, OSError) as exc:
            last = exc
        time.sleep(2 * (attempt + 1))
    print(f"  (warning: could not live-test HTTPS against api.github.com ({last});"
          " bundled crypto imported OK, skipping the online check)")
    return "HTTPS online check skipped (network unavailable)"

https_status = verify_https()
msg = (f"  bundle OK: PySide6 {pyside6_version} | cryptography {cryptography.__version__}"
       f" | {https_status}")
if os.environ.get("DISPLAY"):
    # A real QApplication construction proves the bundled xcb platform
    # plugin actually loads against the host's X11/xcb libraries — the one
    # part of this bundle a headless build box cannot exercise, so it only
    # runs when DISPLAY is present (same convention the old Tk check used).
    from PySide6.QtWidgets import QApplication
    app = QApplication(["bedrock-on-linux-appimage-verify"])
    app.quit()
    msg += " | QApplication constructed (xcb platform plugin OK)"
print(msg)
PY

# The isolated import check above intentionally exercises many modules and
# recreates bytecode with the temporary AppDir path in co_filename. Never ship
# those host-specific paths; the runtime can recreate bytecode in its user
# cache if needed.
find "$PYHOME" -name '__pycache__' -type d -prune -exec rm -rf {} + \
  2>/dev/null || true
find "$PYHOME" -name '*.pyc' -delete 2>/dev/null || true
find "$APPDIR" -type f -name '.DS_Store' -delete 2>/dev/null || true

# Refuse maintainer-specific source/cache paths anywhere in the final staging
# tree. This catches Tcl's compiled-in configure prefix and Python bytecode in
# addition to the ELF RUNPATH audit above.
python3 - "$APPDIR" "$SRC" "$CACHE" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
forbidden = [value.encode() for value in sys.argv[2:] if value]
leaks = []
for path in root.rglob("*"):
    if not path.is_file():
        continue
    try:
        data = path.read_bytes()
    except OSError:
        continue
    if any(value in data for value in forbidden):
        leaks.append(path.relative_to(root))
if leaks:
    raise SystemExit(
        "AppImage staging tree embeds maintainer-local paths:\n  " +
        "\n  ".join(map(str, leaks[:40])))
print("  AppImage staging paths are relocatable")
PY

TOOL="$CACHE/appimagetool"
# The 1.9.1 release, not "continuous": upstream rebuilds that tag on every push
# to its main branch, which swapped the bytes behind the URL on 2026-10-04 and
# took the release build down the next day, as the runtime's did below. 1.9.1
# is commit 8c8c91f, the same source as the continuous build 295 pinned before
# it, bundling the same mksquashfs 4.6.1, zsyncmake 0.6.2 and
# desktop-file-validate 0.28. Upstream publishes no checksums or attestations;
# this is the digest GitHub recorded when the 1.9.1 tag's own build (run
# 19938734639) uploaded the asset.
download_verified \
  "https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage" \
  "$TOOL" \
  "ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0" \
  "appimagetool 1.9.1"
chmod 755 "$TOOL"
RUNTIME="$CACHE/runtime-x86_64"
# A dated release, not "continuous": upstream republishes that tag whenever
# it rebuilds, which swaps the bytes behind the URL and fails the hash check
# below -- it took the release build down on 2026-09-29.
download_verified \
  "https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64" \
  "$RUNTIME" \
  "2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d" \
  "AppImage type-2 x86_64 runtime 20251108"
# readelf translates its field labels, so read the header in the C locale:
# on a French or German build host "Class:" is "Classe:" and this check would
# reject a perfectly good runtime.
runtime_header="$(LC_ALL=C readelf -h "$RUNTIME")"
[[ "$runtime_header" == *"Class:"*"ELF64"* \
   && "$runtime_header" == *"Machine:"*"X86-64"* ]] \
  || { echo "!! AppImage runtime is not ELF64 x86-64" >&2; exit 1; }
runtime_dynamic="$(LC_ALL=C readelf -d "$RUNTIME")"
[[ "$runtime_dynamic" != *"(NEEDED)"* ]] \
  || { echo "!! AppImage runtime is not statically linked" >&2; exit 1; }
APPIMG="$OUT/BedrockOnLinux-${VER}-x86_64.AppImage"
ZSYNC="$APPIMG.zsync"
rm -f "$APPIMG" "$ZSYNC"
declare -a UPDATE_ARGS=()
if [[ -n "$UPDATE_INFO" ]]; then
  UPDATE_ARGS=(-u "$UPDATE_INFO")
fi
# appimagetool writes the .zsync sidecar into the *working directory*, named
# after the destination's basename -- not beside the destination itself. Build
# from dist/ so the sidecar lands next to the AppImage instead of wherever the
# maintainer (or build-release.sh) happened to be standing.
(
  cd "$OUT"
  ARCH=x86_64 "$TOOL" --appimage-extract-and-run "${UPDATE_ARGS[@]}" \
    --runtime-file "$RUNTIME" "$APPDIR" "${APPIMG##*/}"
)
chmod 755 "$APPIMG"

if [[ -n "$UPDATE_INFO" ]]; then
  # appimagetool only *warns* when zsyncmake is missing, so without this the
  # build would happily ship an AppImage advertising a delta file that was
  # never written, and every updater would fail on it.
  [[ -s "$ZSYNC" ]] || {
    echo "!! update information was embedded but no $ZSYNC was written" >&2
    echo "   (appimagetool found no zsyncmake — install the zsync package)" >&2
    exit 1
  }
  chmod 644 "$ZSYNC"
  python3 - "$APPIMG" "$ZSYNC" "$UPDATE_INFO" "$SOURCE_DATE_EPOCH" <<'PY'
import email.utils
import fnmatch
import re
import subprocess
import sys
from pathlib import Path

image, sidecar = Path(sys.argv[1]), Path(sys.argv[2])
expected, epoch = sys.argv[3], int(sys.argv[4])

# The updaters read the string back out of the runtime's .upd_info section, so
# check it there rather than trusting the command line we passed.
headers = subprocess.run(
    ["readelf", "--section-headers", "--wide", str(image)],
    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
if headers.returncode:
    raise SystemExit(f"readelf failed for {image}: {headers.stderr.strip()}")
section = re.search(
    r"\.upd_info\s+\S+\s+[0-9a-f]+\s+([0-9a-f]+)\s+([0-9a-f]+)", headers.stdout)
if not section:
    raise SystemExit("the AppImage runtime carries no .upd_info section")
offset, size = (int(value, 16) for value in section.groups())
with image.open("rb") as stream:
    stream.seek(offset)
    embedded = stream.read(size).split(b"\0", 1)[0].decode("utf-8", "replace")
if embedded != expected:
    raise SystemExit(f"embedded update information is {embedded!r}, "
                     f"expected {expected!r}")

fields = expected.split("|")
if fields[0] == "gh-releases-zsync" and len(fields) == 5:
    # The release asset has to be the file the embedded pattern looks for,
    # otherwise every updater reports "no matching asset" on a release that
    # does carry the update.
    if not fnmatch.fnmatch(sidecar.name, fields[4]):
        raise SystemExit(f"{sidecar.name} does not match the pattern the "
                         f"AppImage advertises ({fields[4]})")

raw = sidecar.read_bytes()
end = raw.find(b"\n\n")          # text headers, blank line, binary checksums
if end < 0:
    raise SystemExit(f"{sidecar.name} has no zsync header block")
zsync = {}
for line in raw[:end].decode("utf-8", "replace").splitlines():
    key, _, value = line.partition(": ")
    zsync[key] = value
for key in ("Filename", "URL"):
    # URL is relative, so it resolves next to the .zsync -- i.e. the AppImage
    # asset of the same release.
    if zsync.get(key) != image.name:
        raise SystemExit(f"{sidecar.name} {key} is {zsync.get(key)!r}, "
                         f"expected {image.name!r}")
if zsync.get("Length") != str(image.stat().st_size):
    raise SystemExit(f"{sidecar.name} describes {zsync.get('Length')} bytes, "
                     f"but the AppImage is {image.stat().st_size}")

# zsyncmake stamps the input file's mtime, which is the one value here that is
# not derived from the bytes. Pin it to SOURCE_DATE_EPOCH like every other
# timestamp in this build, so rebuilding the same source reproduces the sidecar
# too. RFC-822 dates are fixed width, so the header block keeps its length.
stamp = email.utils.formatdate(epoch).replace("-0000", "+0000")
sidecar.write_bytes(
    re.sub(rb"(?m)^MTime: .*$", ("MTime: " + stamp).encode(), raw[:end],
           count=1) + raw[end:])
print(f"  update information: {embedded}")
print(f"  delta updates: {sidecar.name} "
      f"({zsync['Blocksize']}-byte blocks, MTime {stamp})")
PY
fi

rm -rf "$APPDIR"
echo "OK -> $APPIMG ($(du -h "$APPIMG" | cut -f1))"
if [[ -s "$ZSYNC" ]]; then
  echo "OK -> $ZSYNC ($(du -h "$ZSYNC" | cut -f1))"
fi
