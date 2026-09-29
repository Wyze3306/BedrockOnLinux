# Xodus — legitimate Minecraft Bedrock acquisition

Upstream: https://github.com/xodus-gaming/xodus (GPL-3.0)
Pinned commit: `64d39eb87a56c7d0d7e7fde0b233654ac5477b0f`

## Why this is here

BedrockOnLinux used to install Minecraft by downloading a pre-extracted,
DRM-stripped copy of the game from a third-party GitHub repository. That
redistributes the game, and it let anyone install it without owning it.

Xodus replaces that with the path Windows itself uses. It signs in to the
user's own Microsoft account, asks Microsoft's licensing service for the
title license (which carries the content key), then streams the MSIXVC
package from the official Xbox CDN and decrypts it on the fly. The result is
the same extracted game directory the launcher already knows how to drive —
but the account has to actually own Minecraft.

Only `xodus-cli` is used. `xodus-service`, and the separate `xodus-gaming/wine`
and `xodus-gaming/Proton` trees, are not: BedrockOnLinux keeps its own WineGDK
engine, which carries the native Xbox identity, XUser and online-login work
that Xodus's stack does not have.

## What the launcher calls

| Command | Used for |
|---|---|
| `xodus-cli login` | One-time Microsoft sign-in (`InlineLogin.srf` in an embedded webview). |
| `xodus-cli streaming <product-id> <dir>` | Download + decrypt the current store build into `<dir>`. Incremental: it compares local segment hashes and fetches only the delta. |
| `xodus-cli download --dry-run <product-id>` | Resolve the store build number without downloading. |
| `xodus-cli run <dir> <wine>` | Decrypt the `keep_encrypted` segments into anonymous memfds and hand them to Wine. |

Product ids, resolved through DisplayCatalog:

| Edition | Product id | ContentId |
|---|---|---|
| Minecraft for Windows | `9NBLGGH2JHXJ` | `7792d9ce-355a-493c-afbd-768f4a77c3b0` |
| Minecraft Preview for Windows | `9P5X4QVLC2XR` | `98bd2335-9b01-4e4c-bd05-ccc01614078b` |

The Xbox CDN only serves the *current* build of a product id — there is no
back catalogue. This is why the launcher offers Release and Preview rather
than a list of Bedrock versions.

## Build flags that matter

`--features xodus/key-chain-file` is **required**, not optional. Without it
Xodus stores its tokens through a D-Bus secret service, which does not exist
in a Steam Deck Game Mode session or inside a Flatpak sandbox, and every
command that needs device credentials fails outright. With it, tokens go to
`$HOME/.xodus-keyring.ron` — and `$HOME` is the launcher's own directory
rather than the user's: see `bol.xodus.home()`. In the Flatpak the real home
is a tmpfs the sandbox discards, and a discarded keyring is not a sign-out but
a *new* provisioned Microsoft device, ten of which exhaust the account's Store
download pool (issue #198).

`xodus-cli` links `wry`/`tao` unconditionally for the login webview, so
`libwebkit2gtk-4.1` is a build *and* runtime dependency. The `xodus` crate
runs `prost-build`, so `protoc` is needed at build time. It also depends on
`dbus-secret-service-keyring-store` with `crypto-openssl` unconditionally on
Linux — `key-chain-file` stops that store from being *used*, but it is still
compiled, so `libssl-dev` is required to build even though the file keyring
never touches it.

## The patches carried here

`third_party/xodus/patches/` holds what BedrockOnLinux fixes in Xodus itself.
`scripts/build-xodus-cli.sh` applies them with `git am` after checking the
pinned commit out, runs the tests they bring, and archives the *patched* tree
as the GPL source tarball. Anything in there is meant to go upstream: each one
is a self-contained commit with the test that fails without it.

| Patch | What it fixes |
|---|---|
| `0001-msixvc-only-count-cache-bytes-that-are-on-disk.patch` | The download aborting on its own package cache (issue #217). `PrefixCacheFile` counts the bytes tokio *accepted*, and tokio reports a file write accepted as soon as it has queued it on a blocking thread, so a read through the second handle can outrun the write and find the file short — `Header(Io(Custom { kind: UnexpectedEof, error: "cache ended before cached_len" }))`, which kills the whole download. Measured over a 4 MiB body on an idle disk, the cache claimed up to 17 KiB more than the file held. |
| `0002-licensing-accept-a-license-type-the-enum-does-not-name.patch` | The download dying on the licence Microsoft issued for it. `LicenseInfo/@Type` names the kind of entitlement a licence was granted for, `LicenseType` names four of them, and Microsoft also issues `Trial` — `called Result::unwrap() on an Err value: Custom("unknown variant \`Trial\`, expected one of \`Device\`, \`User\`, \`Full\`, \`KeyHolder\`")`, in the middle of the download. Nothing reads the type: the content key travels in the `SPLicenseBlock` beside it. Same account, same content id and same market answered `Trial` twice seconds apart here and a known type seven times twenty minutes later. |
| `0003-licensing-report-a-license-that-cannot-be-read.patch` | Reading the licence out of a licences response — first key, base64, XML — was an index and two unwraps inside a running download, so anything unexpected from the licensing service was a panic in a library rather than a line the caller could print. It now ends the download the way a refused entitlement does. |
| `0004-login-host-the-second-leg-of-a-sign-in-like-the-first.patch` | The sign-in window that loads for ever (issue #214). A token exchange that faults hands back an `inline_auth_url`, and `finalize_request` opened it with an **empty** `HeaderMap` — none of the nine `cxh-*`/`hostApp` headers that tell login.live.com it is talking to the token broker. Without them Microsoft serves the ordinary consumer interrupt ("Please wait", a disabled *OK*) instead of the CloudExperienceHost one, so the page never reaches `ppsecure/post.srf` — the only URL `create_session` fires `Finish` on — `ServerData` is never read back, and `xodus-cli login` sits there printing nothing and never exiting. |
| `0005-licensing-ask-for-a-sign-in-instead-of-unwrapping-a-missing-token.patch` | The download dying on a keyring that holds some of the account and not the rest (issue #260). A license request reads four entries — this device's STS token and license, the account (`user-DA`) and its Passport STS token — and unwrapped every lookup, so a keyring missing one of them ended the download on `called Result::unwrap() on an Err value: NotFound`. Reproduced by deleting `user-DA` from a copy of a working keyring: the unpatched binary panics at `license.rs:17`, the patched one prints "not logged in: the signed-in Microsoft account is missing from the keyring (entry not found); sign in again", which the launcher already reads as a sign-in to offer. |

A patched binary is not the upstream commit's binary, so the rev names the
patches too: `<commit12>-p<n>`, where `n` is how many patch files there are.
Publishing one takes two passes, like every other pinned artifact — run
`.github/workflows/build-xodus.yml`, read the SHA-256 it reports, pin
`XODUS_REV` and `XODUS_ARCHIVE_SHA256` in `bol/config.py`, then run it again
with `publish` on. Until that pin lands, the launcher keeps downloading the
unpatched asset it names today — which is exactly what happened to the first
four patches above: they sat on main for a week while every download still raced
its own package cache (issues #241 and #242), because nothing tied the pin
back to the directory. `ci.yml` now checks that `XODUS_REV` names the patch
set on the branch, so a patch added here fails the build until it is built and
pinned.

## The WebKitGTK runtime beside it

`mod webview` is not behind a feature, so `libwebkit2gtk-4.1.so.0` has to load
before `main()` — for `login`, but equally for `streaming` and for `run`, which
starts every Store-installed game. A host without WebKitGTK therefore cannot
sign in, download or play, and immutable images (SteamOS) cannot install one:
issue #184.

`scripts/build-xodus-webview.sh` packages that stack from the same pinned
snapshot as the binary, and `bol/webview.py` uses it only where the host has
none. Two things are worth knowing before touching either:

- The helper processes (`WebKitWebProcess`, `WebKitNetworkProcess`) are spawned
  from a directory compiled into the library. `WEBKIT_EXEC_PATH` overrides it
  in developer builds only, and Debian's is not one, so the launcher rewrites
  that literal in place — which is why `XODUS_WEBVIEW_EXEC_DIR` is a pin, and
  why the replacement must be shorter than it.
- Nothing else is patched. The bundle is stock Debian packages with a `$ORIGIN`
  RUNPATH, listed in the `PACKAGES` file it carries, so it can be rebuilt and
  compared byte for byte.

## License

Xodus is GPL-3.0; BedrockOnLinux is MIT. The launcher never links Xodus code —
it executes `xodus-cli` as a separate program, which is mere aggregation. The
published `xodus-cli-<rev>.tar.gz` carries `LICENSE.GPL-3.0` and a
`SOURCE-COMMIT` file, and `.github/workflows/build-xodus.yml` publishes the
matching source tarball alongside it so the GPL source requirement is met by
the same release that ships the binary.
