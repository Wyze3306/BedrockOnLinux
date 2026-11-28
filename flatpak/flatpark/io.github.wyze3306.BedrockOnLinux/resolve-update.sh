#!/usr/bin/env bash
# Update resolver for BedrockOnLinux: the newest vX.Y.Z GitHub release and its
# AppImage. Prints { "version", "releaseDate", "sources": [...] } on stdout;
# logs go to stderr. FlatPark downloads the URL and computes the sha256/size
# itself.
#
# The repository also publishes the launcher's engine and tool builds as
# releases of their own (engine-*, winegdk-*, xodus-*, ...). releases/latest
# is the app today; should one of those ever be marked latest, the newest
# non-prerelease vX.Y.Z release that carries the AppImage is taken instead.
set -euo pipefail

repo="Wyze3306/BedrockOnLinux"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing command: $1" >&2; exit 1; }; }
need curl; need jq

api() {
    curl -fsSL ${GITHUB_TOKEN:+-H "Authorization: Bearer $GITHUB_TOKEN"} \
        -H "Accept: application/vnd.github+json" "https://api.github.com/repos/$repo/$1"
}

# One release object in, the same object out when it is an app release with
# its AppImage, nothing otherwise.
pick='select(.draft == false and .prerelease == false
             and (.tag_name | test("^v[0-9]+\\.[0-9]+\\.[0-9]+$")))
      | . as $r
      | ($r.tag_name | sub("^v"; "")) as $v
      | select(any($r.assets[]; .name == "BedrockOnLinux-\($v)-x86_64.AppImage"))'

rel="$(api releases/latest | jq -c "$pick")"
if [ -z "$rel" ]; then
    echo "releases/latest is not an app release; walking the release list" >&2
    rel="$(api 'releases?per_page=50' | jq -c "[.[] | $pick] | first // empty")"
fi
[ -n "$rel" ] || { echo "no BedrockOnLinux release with an AppImage found" >&2; exit 1; }

version="$(jq -r '.tag_name | sub("^v"; "")' <<<"$rel")"
date="$(jq -r '.published_at' <<<"$rel" | cut -c1-10)"
name="BedrockOnLinux-$version-x86_64.AppImage"
url="$(jq -r --arg n "$name" '.assets[] | select(.name == $n) | .browser_download_url' <<<"$rel" | head -n1)"
page="$(jq -r '.html_url' <<<"$rel")"

[ -n "$version" ] && [ -n "$url" ] || { echo "failed to resolve the BedrockOnLinux release" >&2; exit 1; }
echo "resolved BedrockOnLinux $version ($date): $url" >&2

jq -n --arg v "$version" --arg d "$date" --arg u "$url" --arg p "$page" \
  '{version:$v, releaseDate:$d, releaseUrl:$p,
    sources:[{filename:"bedrock-on-linux.AppImage", arch:"x86_64", url:$u}]}'
