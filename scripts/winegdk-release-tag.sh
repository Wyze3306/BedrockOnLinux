#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Print the GitHub release tag that holds the WineGDK prefix bol/config.py
# describes. The prefix archive in it is "winegdk-prefix-<the tag without its
# winegdk- prefix>.tar.gz".
#
# The tag names what the bytes are made from: the source commit, the manifest
# of the vendored patches, and the build recipe once that changed too. The
# first recipe had no name, so the tags it produced keep theirs; "stripped"
# installs the modules without their debugging sections. A prefix built
# another way is another set of bytes and gets a release of its own, instead
# of replacing the one earlier engine revisions were assembled from.
set -euo pipefail

CONFIG="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/bol/config.py"

pin() {
  grep -m1 "^$1 = " "$CONFIG" | cut -d'"' -f2
}

commit="$(pin WINEGDK_SOURCE_COMMIT)"
manifest="$(pin WINEGDK_SOURCE_MANIFEST_SHA256)"
recipe="$(pin WINEGDK_PREFIX_RECIPE)"

[[ "$commit" =~ ^[0-9a-f]{40}$ ]] || {
  echo "!! invalid WINEGDK_SOURCE_COMMIT in $CONFIG" >&2
  exit 1
}
[[ "$manifest" =~ ^[0-9a-f]{64}$ ]] || {
  echo "!! invalid WINEGDK_SOURCE_MANIFEST_SHA256 in $CONFIG" >&2
  exit 1
}
[[ "$recipe" =~ ^[a-z0-9]{0,16}$ ]] || {
  echo "!! invalid WINEGDK_PREFIX_RECIPE in $CONFIG" >&2
  exit 1
}

echo "winegdk-${commit:0:12}-${manifest:0:12}${recipe:+-$recipe}"
