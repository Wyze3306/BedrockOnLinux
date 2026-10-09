#!/usr/bin/env bash
# Point apt at a fixed snapshot.debian.org timestamp so the build toolchain is
# frozen. This makes the containerized builds reproducible across TIME, not just
# run-to-run: without it, `apt-get install` pulls whatever the live mirror
# currently serves, so a Debian package update would silently change the output
# bytes and trip the fail-closed SHA asserts. Run as root inside the build
# container, before `apt-get update`.
#
# Usage: pin-apt-snapshot.sh <bullseye|trixie> [YYYYMMDDTHHMMSSZ]
set -Eeuo pipefail

SUITE="${1:?usage: pin-apt-snapshot.sh <suite> [snapshot]}"
# Pinned reproducibility constant, like SOURCE_DATE_EPOCH. Bump only alongside a
# deliberate re-baseline of the affected component hashes (see docs/BUILD.md).
# A second argument names another timestamp, for the one artifact that takes
# its packages from a later snapshot (the WebKitGTK runtime, XODUS_WEBVIEW_SNAPSHOT
# in bol/config.py); everything else keeps this one.
readonly SNAPSHOT="${2:-20260701T000000Z}"
[[ "$SNAPSHOT" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] \
  || { echo "!! not a snapshot.debian.org timestamp: $SNAPSHOT" >&2; exit 1; }
readonly BASE="http://snapshot.debian.org/archive/debian/${SNAPSHOT}"
readonly SEC="http://snapshot.debian.org/archive/debian-security/${SNAPSHOT}"

# Replace whatever the base image ships (sources.list and/or deb822 .sources).
rm -f /etc/apt/sources.list \
      /etc/apt/sources.list.d/*.list \
      /etc/apt/sources.list.d/*.sources 2>/dev/null || true

cat > /etc/apt/sources.list <<EOF
deb [check-valid-until=no] ${BASE}/ ${SUITE} main
deb [check-valid-until=no] ${BASE}/ ${SUITE}-updates main
deb [check-valid-until=no] ${SEC}/ ${SUITE}-security main
EOF

echo "== apt pinned to snapshot.debian.org ${SNAPSHOT} (${SUITE})"
