#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Print the GitHub release tag that holds the universal vkd3d-proton payload
# described by third_party/vkd3d-proton-universal.
#
# The tag is named after the output hash lock, the way the WineGDK prefix
# release is named after its source commit and manifest: a rebuilt payload gets
# a release of its own instead of replacing the bytes that earlier engine
# revisions were assembled from, and reusing a release by name can never pick
# up a payload the committed OUTPUT-SHA256SUMS does not describe.
set -euo pipefail

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

echo "vkd3d-3.0.1-nv-dgc-${VKD3D_OUTPUT_HASHES_SHA256:0:12}"
