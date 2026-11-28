#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# The one release that holds every file the launcher downloads by itself:
# the game engines, xodus-cli and its WebKitGTK runtime, the OpenSSL XCurl
# set, and the WineGDK prefixes and vkd3d-proton payloads the engines are
# assembled from (docs/release-files.md).
#
# Each build used to get a release of its own (engine-<rev>, xodus-<rev>,
# winegdk-..., vkd3d-..., xcurl-...), which buried the launcher's versions
# under dozens of them on the Releases page. They are now assets of this one
# release. Its tag sits on the repository's first commit, and GitHub orders
# releases by the date of the commit their tag names, so it is listed last.
# The releases published before stay where they are: launchers up to 3.0.0
# and other projects download from them by name, and `fetch` falls back to
# them.
#
# A name in the store is one set of bytes. `publish` never replaces an
# asset: the same bytes under the same name are already there and skipped,
# different bytes are an error, and the build gets a new revision instead.
#
#   release-files.sh asset-sha NAME
#       The SHA-256 of asset NAME in the store; nothing when it has none.
#   release-files.sh fetch NAME DIR [TAG [OLD_NAME]]
#       Download NAME and NAME.sha256 into DIR, checked against each other.
#       When the store has no NAME, take OLD_NAME (default NAME) and its
#       sidecar from release TAG instead, saved as NAME. Prints "store" or
#       "TAG" for where it came from.
#   release-files.sh publish FILE...
#       Upload each FILE, and FILE.sha256 when there is one, creating the
#       store release first if this repository has none.
#
# GH_TOKEN and GITHUB_REPOSITORY come from the environment.
set -euo pipefail

STORE_TAG="files"
# The repository's root commit (6d37548, 2026-05-16): older than every
# release, so the store sorts after all of them.
ROOT_COMMIT="6d375486ee69507601024f04ff8318b475f0c0e8"
STORE_TITLE="Launcher files (not a launcher version)"

REPO="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is not set}"

die() {
  echo "::error::$*" >&2
  exit 1
}

sha_of() {
  sha256sum "$1" | cut -d' ' -f1
}

# The store's release id, or nothing when the repository has none yet. On an
# error gh prints the response body, not the filtered field: keep neither.
store_id() {
  local id
  if id="$(gh api "repos/$REPO/releases/tags/$STORE_TAG" --jq .id 2>/dev/null)"; then
    echo "$id"
  fi
}

asset_sha() {
  local name="$1" id digest tmp
  id="$(store_id)"
  [ -n "$id" ] || return 0
  # GitHub keeps the SHA-256 of every asset it was handed.
  digest="$(gh api --paginate "repos/$REPO/releases/$id/assets?per_page=100" \
    --jq ".[] | select(.name == \"$name\") | (.digest // \"none\")")"
  case "$digest" in
    "") return 0 ;;
    sha256:*) echo "${digest#sha256:}" ;;
    *)
      # No digest on record: hash the bytes themselves.
      tmp="$(mktemp -d)"
      gh release download "$STORE_TAG" -R "$REPO" -p "$name" -D "$tmp" >&2
      sha_of "$tmp/$name"
      rm -rf "$tmp"
      ;;
  esac
}

fetch() {
  local name="$1" dir="$2" tag="${3:-}" old="${4:-$1}" tmp from got want
  mkdir -p "$dir"
  tmp="$(mktemp -d)"
  # stdout carries the answer, so gh's own messages go to stderr.
  if gh release download "$STORE_TAG" -R "$REPO" -p "$name" -p "$name.sha256" \
      -D "$tmp" >&2 && [ -f "$tmp/$name" ]; then
    from="store"
  elif [ -n "$tag" ] && gh release download "$tag" -R "$REPO" -p "$old" \
      -p "$old.sha256" -D "$tmp" >&2 && [ -f "$tmp/$old" ]; then
    from="$tag"
    [ "$old" = "$name" ] || {
      mv "$tmp/$old" "$tmp/$name"
      [ ! -f "$tmp/$old.sha256" ] || mv "$tmp/$old.sha256" "$tmp/$name.sha256"
    }
  else
    rm -rf "$tmp"
    die "$name is neither in the $STORE_TAG release${tag:+ nor in $tag} of $REPO."
  fi
  [ -f "$tmp/$name.sha256" ] || die "$name has no published .sha256 in $from."
  want="$(cut -d' ' -f1 "$tmp/$name.sha256")"
  got="$(sha_of "$tmp/$name")"
  [ "$got" = "$want" ] || die "$name from $from is $got, its .sha256 says $want."
  # The sidecar names the file it was written for; name it after this one so
  # `sha256sum -c` works on it wherever it came from.
  echo "$got  $name" > "$tmp/$name.sha256"
  mv -f "$tmp/$name" "$tmp/$name.sha256" "$dir/"
  rm -rf "$tmp"
  echo "$from"
}

ensure_store() {
  [ -z "$(store_id)" ] || return 0
  local notes
  notes="Every file the launcher downloads by itself: the game engines, the
Minecraft downloader (xodus-cli) and its WebKitGTK runtime, the online
sign-in set, and the WineGDK prefixes and vkd3d-proton payloads the engines
are assembled from.

This is not a launcher version. To install BedrockOnLinux, take the latest
release above.

Each file is published once and never replaced: a rebuilt file gets a new
name. Verify any of them with:
gh attestation verify <file> --repo $REPO"
  # Two jobs of one run may both find it missing; the loser's create fails
  # and the release the winner made is what counts.
  gh release create "$STORE_TAG" -R "$REPO" --target "$ROOT_COMMIT" \
    --title "$STORE_TITLE" --notes "$notes" --prerelease --latest=false \
    || [ -n "$(store_id)" ] || die "could not create the $STORE_TAG release."
}

publish_one() {
  local file="$1" name have want
  name="$(basename "$file")"
  want="$(sha_of "$file")"
  have="$(asset_sha "$name")"
  if [ -z "$have" ]; then
    # No --clobber: GitHub itself refuses a name that is already taken.
    gh release upload "$STORE_TAG" "$file" -R "$REPO" >&2
    echo "published $name ($want)"
  elif [ "$have" = "$want" ]; then
    echo "$name is already published with these bytes."
  else
    die "$name is already published as $have; this build is $want." \
      "A name holds one set of bytes: give the new build a new revision."
  fi
}

publish() {
  [ "$#" -gt 0 ] || die "publish: no file given."
  local file
  for file in "$@"; do
    [ -f "$file" ] || die "publish: $file does not exist."
  done
  ensure_store
  for file in "$@"; do
    publish_one "$file"
    [ ! -f "$file.sha256" ] || publish_one "$file.sha256"
  done
}

command="${1:-}"
shift || true
case "$command" in
  asset-sha) [ "$#" -eq 1 ] || die "usage: $0 asset-sha NAME"; asset_sha "$1" ;;
  fetch) [ "$#" -ge 2 ] && [ "$#" -le 4 ] || die "usage: $0 fetch NAME DIR [TAG [OLD_NAME]]"; fetch "$@" ;;
  publish) publish "$@" ;;
  *) die "usage: $0 asset-sha|fetch|publish ..." ;;
esac
