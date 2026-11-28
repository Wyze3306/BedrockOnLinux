# FlatPark package

`io.github.wyze3306.BedrockOnLinux/` is the package for
[FlatPark](https://github.com/flatpark/flatpark), laid out the way its
`registry/` folder expects. It is not used by this repository's own builds.

It installs the released AppImage as Flatpak extra-data: `apply_extra.sh`
unpacks it at install time with FlatPark's appimage-tools (no FUSE), on the
GNOME runtime for WebKitGTK. The permissions are the ones in
`flatpak/io.github.wyze3306.BedrockOnLinux.yml`, which FlatPark compares
against this file on every update sweep (`upstream:` in `flatpark.yml`).

The package was built and run in a sandbox against 2.3.1: the window opens,
`versions` reaches the network, settings survive a restart and an update, and
the Microsoft sign-in page loads in the runtime's WebKitGTK. Signing in,
downloading the game and PLAY were not run.

## Submitting it with 3.0.0

1. Publish v3.0.0 as a full release (not a pre-release) with its AppImage.
   `resolve-update.sh` skips pre-releases.
2. Point the AppImage source in `io.github.wyze3306.BedrockOnLinux.yml` at
   v3.0.0, with its `sha256` and `size`, and add the 3.0.0 release to the
   package metainfo.
3. Build and install it with FlatPark's `scripts/build-app.sh`, then sign in,
   download the game and press PLAY in the installed app.
4. Copy this folder to `registry/` in a fork of flatpark/flatpark and open the
   pull request.
5. Once the pull request has a number, set `catalog.upstream_approved: true`
   in `flatpark.yml` and, in the same pull request, add an "Approved by
   construction" row citing it to FlatPark's
   `docs/reference/upstream-approvals.md`.
