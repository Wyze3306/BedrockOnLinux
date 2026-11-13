# Updates and the data feed

Two things reach an installed launcher after it was released:

- **launcher updates**, a new version of the launcher itself;
- **the data feed**, one small signed JSON file that changes what an installed
  launcher *knows* without changing its code.

This page is for maintainers: what each one is, how to publish a data fix in
one command, and where the signing key lives. What the player sees and agrees
to is in the README, *What it connects to*.

## What the feed is for

Before the feed, every fix needed a full launcher release. Most fixes are not
code:

| Fix | Before | With the feed |
|---|---|---|
| A new Minecraft build moved the HBUI anchors | release | add an anchor set |
| A rebuilt `xodus-cli` (one more patch) | release | add a pin |
| A rebuilt OpenSSL XCurl set or WebKitGTK runtime | release | add a pin |
| "Microsoft sign-in is down, it is not your install" | nothing | add a notice |
| A new launcher release | release | release, then record its digests |

The game engine is **not** in the feed. The launcher validates an engine
against an allow-list of file hashes compiled into `bol/vkd3d.py`, and a pin
cannot carry that list. A new engine still takes a launcher release, as
before.

## How a launcher trusts it

The file is an envelope: the payload bytes, base64, and Ed25519 signatures
over exactly those bytes.

```json
{"payload": "<base64 of the JSON payload>",
 "signatures": [{"keyid": "bol-2026", "sig": "<base64>"}]}
```

A launcher accepts a payload only when a key in `PUBLIC_KEYS` of
[`bol/feed.py`](../bol/feed.py) signed it. Where the file came from proves
nothing, and neither does TLS: the hosts in `URLS` are mirrors, and a mirror
that serves something else is just a failed refresh. On top of the signature:

- **Roll-back.** The payload has a `serial`. A launcher refuses one lower
  than the highest it has accepted, from the network or dropped into its
  cache.
- **A pin replaces a named revision.** Every component pin lists the embedded
  revisions it `replaces`. A launcher that ships something newer ignores it,
  so an old feed cannot downgrade a new launcher.
- **Binaries are still checked.** A pin is a revision and a SHA-256. The
  archive comes from this repository's GitHub releases and is installed only
  if it hashes to the signed value; when it does not, or cannot be fetched,
  the launcher installs the version it was released with.
- **Anchors say where, never what.** An HBUI rule gives patterns that locate
  the two insertion points. What is inserted is fixed in `bol/fixups.py`.
- **Never in PLAY's way.** Launching reads the cache. A refresh happens when
  the launcher starts (at most every six hours, six seconds, silent on
  failure), and only if the player chose *Automatic*. With no key, no cache
  or no network the launcher runs on what it shipped with.

Until a real key is in `PUBLIC_KEYS` the feed is switched off: nothing is
fetched and the launcher behaves as it did before.

## The signing key

The private key never enters this repository, a pull request, an issue or a
CI log. `scripts/data-feed.py` refuses to read or write one inside any git
work tree.

Create it once:

```bash
scripts/data-feed.py keygen
```

This writes `~/.config/bedrock-on-linux/feed-signing.key` (mode 600) and
prints the line to add to `PUBLIC_KEYS` in `bol/feed.py`. That line ships
with the next launcher release; launchers released before it never use the
feed.

- Keep the key on the maintainer's machine, and a copy offline (a USB stick
  or a password manager). `BOL_FEED_KEY` points the script at another path.
- It is deliberately not a GitHub Actions secret: a workflow that can sign
  the feed turns any compromise of the repository into a signed pin for every
  install. Signing takes one command on a machine that has the key.
- **Rotation.** Generate a second key with `keygen --id bol-2027 --out …`,
  add its public key to `PUBLIC_KEYS` beside the old one, release, and sign
  with both (`--key old --key new`) until old launchers are gone. Then drop
  the old public key.
- **If the key is lost**, nothing breaks: launchers keep their cached feed
  and their embedded pins. Generate a new key and ship it in a release.
- **If the key leaks**, remove its public key from `PUBLIC_KEYS` and release.
  What an attacker could do with it is bounded by the validation in
  `bol/feed.py`: point a component at another *release asset of this
  repository*, add anchor patterns, show notices.

## Publishing a data fix

Edit [`feed/manifest.json`](../feed/manifest.json), commit it like any
change, then:

```bash
scripts/data-feed.py publish --push
```

That validates the source exactly as a launcher would, gives it the next
serial, signs it, commits the signed file as `v1/manifest.json` on the `data`
branch and pushes that branch. Launchers on *Automatic* pick it up at their
next start, within six hours; `bedrock-on-linux update --data-only` fetches
it at once.

Without `--push` the commit stays local and the script prints the push
command. `scripts/data-feed.py sign --out FILE` and
`scripts/data-feed.py verify FILE` produce and check a signed file without
touching git.

The `data` branch holds only the signed file. Pushing to it starts no
workflow: `release.yml` runs on `main` for `bol/config.py` and
`third_party/xodus/**`, and a data fix touches neither.

### Trying it before publishing

```bash
scripts/data-feed.py sign --out /tmp/feed/manifest.json
BOL_FEED_URL=file:///tmp/feed/manifest.json \
  bedrock-on-linux update --data-only
```

`BOL_FEED_PUBKEY=id:base64` makes one run trust another key, for a staging
feed signed with a throwaway key.

## The source format

```json
{
  "schema": 1,
  "components": {
    "stable": {
      "xodus": [
        {"rev": "64d39eb87a56-p15",
         "sha256": "<sha256 of xodus-cli-64d39eb87a56-p15.tar.gz>",
         "replaces": ["64d39eb87a56-p14"],
         "url": "https://github.com/Wyze3306/BedrockOnLinux/releases/download/xodus-64d39eb87a56-p15/xodus-cli-64d39eb87a56-p15.tar.gz",
         "min_launcher": "3.0.0"}
      ]
    },
    "prerelease": {}
  },
  "launcher": {
    "stable": {"version": "3.0.1", "tag": "v3.0.1",
               "assets": {"BedrockOnLinux-3.0.1-x86_64.AppImage":
                          {"sha256": "…"}}}
  },
  "minecraft": {
    "hbui": [
      {"id": "mc-1-27",
       "gate": {"function": "(function\\s+\\w+\\(\\)\\{)(return …\\()",
                "selector": "\\(?\\([\\w$,\\s]*\\)=>\\{",
                "marks": ["isSignedInPlatformNetwork", "\"not-signed-in\""]},
       "link": {"anchor": "(…)(return \\w+\\.createElement\\(\\w+,)",
                "done": "…return null;"}}
    ]
  },
  "notices": [
    {"id": "xbl-outage-2026-11", "level": "warning",
     "title": "Xbox sign-in is failing for everyone",
     "text": "Microsoft is working on it. Your install is fine.",
     "url": "https://support.xbox.com/xbox-live-status",
     "until": 1794700000}
  ]
}
```

- `serial` and `issued` are added when signing; they are not in the source.
- **components**: `xodus`, `openssl_xcurl`, `webview`. `rev` and `sha256` are
  what `bol/config.py` would hold. `replaces` is required. `url` is optional
  (without it the launcher looks the asset up by name in the releases) and
  must be a release asset of this repository. `min_launcher` /
  `max_launcher` bound the launcher versions the pin is for: use them when a
  build needs a launcher change to work. The `prerelease` table adds pins for
  players on the pre-release channel; they also get everything in `stable`.
  The archive must exist first: build it with the usual workflow
  (`build-xodus.yml`, `build-xcurl.yml`), which publishes it under its own
  tag and prints the SHA-256.
- **launcher**: the newest release per channel with the SHA-256 of every
  asset. Optional: without it the launcher asks GitHub for the latest
  release and uses that release's checksum list, as before. With it the
  digests are signed. After publishing a release:

  ```bash
  scripts/data-feed.py release v3.0.1            # or --channel prerelease
  scripts/data-feed.py publish --push
  ```

- **minecraft.hbui**: extra anchor sets, tried after the ones in
  `bol/fixups.py`. `gate.function` needs two groups (the function head, the
  start of its `return`), `link.anchor` two (the end of the hook, the start
  of the `return`). Test a rule against the build's
  `data/gui/dist/hbui/index-*.js` before publishing; `bedrock-on-linux
  doctor` reports whether the installed build is patched.
- **notices**: `level` is `info`, `warning` or `critical`. Optional filters:
  `until` (Unix time), `min_launcher` / `max_launcher`, `channels`,
  `minecraft` (version prefixes, such as `"1.26.70"`). A notice is shown once
  per `id` and can be dismissed.

A value the launcher does not recognise makes it refuse the whole file and
keep the previous one, so the script refuses to sign it. Unknown *keys* are
ignored, which is how a later launcher can read more out of the same file.

## Launcher updates

`bol/update.py` has one path per way the launcher is installed:

| Install | Update |
|---|---|
| AppImage | replaces its own file; uses `appimageupdatetool` for a delta download (the `.zsync` beside each release) when the host has it |
| `.pyz` | replaces its own file |
| `.deb` | downloads the package, hands it to `apt` through `pkexec` |
| `.rpm` | downloads the package, hands it to `dnf` / `zypper` / `rpm` through `pkexec` |
| Flatpak | tells the player the `flatpak install` command, keeping `--user` |
| Nix | tells the player to update the flake input |
| git checkout, distribution package | left to git or the package manager |

For every kind that installs something:

1. The file is checked against a SHA-256: the feed's signed digest when the
   feed names the release, else the release's `SHA256SUMS`. A release with
   neither is refused.
2. The new launcher is started once with `--version` before it replaces
   anything. One that cannot start on this machine is discarded.
3. AppImage and `.pyz`: the previous file is kept beside the new one
   (`.<name>.previous`). If the new launcher is started and twice never gets
   as far as its window or a command, the third start puts the previous file
   back and continues as that version (`bol/rollback.py`).
4. `.deb` and `.rpm`: after the package manager returns, the installed
   launcher is started once. The package manager keeps no previous version,
   so a failure is reported at once with the link to the previous package
   instead of being discovered at the next start.

### Channels

*Settings ▸ Network ▸ Release channel*, or `bedrock-on-linux update
--channel prerelease`. Stable follows the release GitHub marks Latest.
Pre-release also offers versioned pre-releases (never the rolling `nightly`)
and moves on to the stable release that follows.

## Hosting

The feed is served from the `data` branch of this repository through
`raw.githubusercontent.com`, a host the launcher already used for the build
index. A second mirror can be added to `URLS` in `bol/feed.py` at any time:
because of the signature it needs no trust, only availability. A server of
our own was considered and left out. It would add a host every player has to
be told about and a machine to keep running, and it would improve nothing
the signature does not already guarantee.
