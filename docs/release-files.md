# The launcher's file store

Every file the launcher downloads by itself is an asset of one release,
tagged `files` and titled "Launcher files (not a launcher version)":

| File | Built by |
|---|---|
| `GDK-Proton-xuser-<rev>.tar.gz`, the game engine (x86-64 and ARM64EC) | `build-engine.yml` |
| `winegdk-prefix-<winegdk>-<patches>[-stripped][-aarch64].tar.gz`, the WineGDK the engine is assembled from | `build-winegdk.yml` |
| `vkd3d-universal-<version>-<lock>.tar.gz`, `vkd3d-arm64x-<version>-<lock>.tar.gz`, the vkd3d-proton payloads | `build-vkd3d.yml` |
| `xodus-cli-<rev>[-aarch64].tar.gz`, `xodus-src-<rev>[-aarch64].tar.gz`, the Minecraft downloader and its source | `build-xodus.yml` |
| `xodus-webview-<rev>[-aarch64].tar.xz`, the WebKitGTK runtime for the Microsoft sign-in | `build-xodus.yml` |
| `openssl-xcurl-set-<rev>.tar.gz`, the online sign-in set | `build-xcurl.yml` |

Each one has a `.sha256` sidecar beside it and a build provenance
attestation (`gh attestation verify <file> --repo Wyze3306/BedrockOnLinux`),
which GitHub finds by the file's digest, whatever release holds it.

## Why one release

Each build used to be a pre-release of its own (`engine-<rev>`,
`xodus-<rev>`, `winegdk-…`, `vkd3d-…`, `xcurl-<rev>`). By 3.0.0 there were
more than fifty: the first page of the Releases page held no launcher
version at all, and most of the newest thirty releases, which the
launcher's own pre-release lookup reads, were not launcher versions.

GitHub lists releases by the date of the commit their tag names, not by
when they were published. The `files` tag sits on the repository's first
commit (`6d37548`, 2026-05-16), older than every launcher version, so the
store is always the last release on the page however often it changes.
It is a pre-release and never "Latest".

## Rules

- **A name holds one set of bytes.** `scripts/release-files.sh publish`
  never replaces an asset. The same bytes under the same name are skipped
  (a re-run of a build is harmless); different bytes are an error, and the
  build gets a new revision (`XODUS_REV` `-p<n>`, `WINEGDK_BUILD_REV`
  `native<n>`, a new vkd3d lock) instead. A launcher already pins the old
  name by its SHA-256.
- **Only CI publishes.** The workflows above upload through
  `scripts/release-files.sh`, which creates the `files` release on first
  use if the repository has none. Nobody uploads to it by hand.
- **Names carry everything that tells two builds apart.** The vkd3d
  payloads, whose old names did not, are named after their lock
  (`scripts/vkd3d-release-tag.sh --asset`).

## Where the launcher looks

The store first, then the release the file was published under before the
store existed:

| File | Then |
|---|---|
| engine | `engine-<rev>` |
| xodus-cli, its WebKitGTK runtime | `xodus-<XODUS_REV>[-aarch64]` |
| OpenSSL XCurl set | `xcurl-<rev>` |

A pin the signed data feed (`docs/updates.md`) names comes with one
address of its own, which is tried alone. The SHA-256 decides either way.

The CI workflows read the same way (`release-files.sh fetch NAME DIR TAG
OLD_NAME`), so a branch whose pins predate the store still builds.

## The releases published before the store

They stay until nothing downloads from them any more, and are removed only
by a person, one at a time, never by a workflow:

- Launchers up to 3.0.0 download the engine from `engine-<rev>` by its tag,
  and xodus-cli, the WebKitGTK runtime and the XCurl set by looking their
  names up in the release list. A release that one of them pins goes once
  no supported launcher pins it.
- Other projects download from them by URL. These are never removed:

  | Release | Used by |
  |---|---|
  | `engine-wow64-archs-native20`, `xodus-64d39eb87a56-p5` | flarialmc/linux |
  | `engine-wow64-archs-native21`, `xodus-64d39eb87a56-p7`, `xcurl-504bb166e4e7` (and `xodus-webview-trixie-2.tar.xz` in p7) | mdevlopm/gomceix |

  Search GitHub for `Wyze3306/BedrockOnLinux/releases/download` before
  removing any other.

To move a branch whose pins predate the store onto it, run **Carry files
over to the file store** (`files-carry-over.yml`) on that branch, first with
*publish* off to see what it would copy. It copies each pinned file into the
store after checking it against its sidecar, its pin in `bol/config.py` and
its attestation, and deletes nothing.
