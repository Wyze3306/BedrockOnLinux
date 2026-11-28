#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Print the GitHub release tag that held the universal vkd3d-proton payload
# described by third_party/vkd3d-proton-universal, before the launcher's file
# store existed.
#
# The tag is named after the output hash lock, the way the WineGDK prefix is
# named after its source commit and manifest: a rebuilt payload gets a name of
# its own instead of replacing the bytes that earlier engine revisions were
# assembled from, and reusing a payload by name can never pick up one the
# committed OUTPUT-SHA256SUMS does not describe.
#
# "vkd3d-release-tag.sh aarch64" names the experimental ARM64EC engine's ARM64X
# payload instead (third_party/vkd3d-proton-arm64x), after its own output lock.
#
# With --asset first it prints the payload's file name in the launcher's file
# store instead (scripts/release-files.sh), which carries the same lock: the
# releases of their own used one file name for every payload.
set -euo pipefail

asset=false
if [[ "${1:-}" == --asset ]]; then
  asset=true
  shift
fi

name() {
  if ! $asset; then
    echo "$1"
  elif [[ "$1" == vkd3d-arm64x-* ]]; then
    echo "$1.tar.gz"
  else
    echo "vkd3d-universal-${1#vkd3d-}.tar.gz"
  fi
}

if [[ "${1:-}" == aarch64 ]]; then
  ARM_HASHES="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/third_party/vkd3d-proton-arm64x/OUTPUT-SHA256SUMS"
  [[ -s "$ARM_HASHES" ]] || {
    echo "!! no ARM64X OUTPUT-SHA256SUMS yet: the payload is not pinned" >&2
    exit 1
  }
  read -r arm_lock _ < <(sha256sum "$ARM_HASHES")
  name "vkd3d-arm64x-3.0.1-nv-dgc-${arm_lock:0:12}"
  exit 0
fi

PROVENANCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/third_party/vkd3d-proton-universal"
LOCK_FILE="$PROVENANCE_DIR/provenance.env"
EXPECTED_HASHES="$PROVENANCE_DIR/OUTPUT-SHA256SUMS"

# shellcheck disable=SC1090
source "$LOCK_FILE"

[[ "${VKD3D_OUTPUT_HASHES_SHA256:-}" =~ ^[0-9a-f]{64}$ ]] || {
  echo "!! invalid VKD3D_OUTPUT_HASHES_SHA256 in $LOCK_FILE" >&2
  exit 1
}
read -r actual _ < <(sha256sum "$EXPECTED_HASHES")
[[ "$actual" == "$VKD3D_OUTPUT_HASHES_SHA256" ]] || {
  echo "!! OUTPUT-SHA256SUMS does not match its lock: got $actual," \
    "expected $VKD3D_OUTPUT_HASHES_SHA256" >&2
  exit 1
}

name "vkd3d-3.0.1-nv-dgc-${VKD3D_OUTPUT_HASHES_SHA256:0:12}"
