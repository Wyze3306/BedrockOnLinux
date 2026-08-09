<div align="center">

# 🟩 BedrockOnLinux

**Run Minecraft Bedrock for Windows (GDK edition) on Linux with native
Microsoft/Xbox identity, multiplayer, friends and Realms.**

[Latest release](https://github.com/Wyze3306/BedrockOnLinux/releases/latest) ·
[Report a bug](https://github.com/Wyze3306/BedrockOnLinux/issues) ·
[MIT license](LICENSE)

`Ubuntu` · `Debian` · `Linux Mint / LMDE` · `Fedora` · `Arch` · `openSUSE`

![BedrockOnLinux launcher](screenshot.png)

</div>

---

## What 2.0 provides

BedrockOnLinux installs the Minecraft version you select, prepares a managed
Wine prefix and runs the game through a reviewed WineGDK-based GDK-Proton
engine. No compiler or Windows installation is required on the player’s
machine.

- **Native Xbox identity:** XGame configuration, XUser, request signatures,
  gamertags, privileges and the XSAPI context are implemented by WineGDK.
- **Online play:** the Friends list, invitations, joining friends, public
  servers and Realms use that native identity. Realms receives a dedicated
  XSTS token for the Bedrock Realms audience instead of a generic Xbox token.
- **No Minecraft memory patcher:** the managed engine contains no code that
  scans or rewrites the running Minecraft process. Packaging rejects remnants
  of the former process-memory implementation. Static, fingerprinted game and
  Proton compatibility fixups are still applied before launch.
- **Native file chooser:** the pinned engine implements the WinAppSDK picker
  for both Windows architectures, and every stopped-prefix preparation repairs
  its activation registration. Minecraft's in-game world import and custom
  skin selection therefore open the desktop file dialog instead of failing in
  `RoGetActivationFactory`. Launcher-side `.mcskin` import remains available as
  an additional non-interactive installation path.
- **Graphics safety:** the launcher checks the existing display state and text
  kernel logs without opening Vulkan or OpenGL. A known unsafe session is
  blocked before Wine starts. The GUI offers an acknowledgement only for a
  verified previous-boot incident; it cannot dismiss a current driver fault or
  a running Wine/UMU session.
- **Verified updates:** engine archives, critical runtime files and dependency
  payloads are SHA-256 pinned. A rejected update does not replace a working
  engine.
- **Isolated account profiles:** one Linux user can create separate Xbox
  account, Wine-prefix, settings and world roots while sharing the large game,
  engine and runtime downloads.

The Microsoft sign-in flow runs locally between the launcher, Microsoft and
Xbox services. BedrockOnLinux does not use an account relay or multiplayer
proxy.

## Install

Download application files from the
[latest release](https://github.com/Wyze3306/BedrockOnLinux/releases/latest).
All currently supported builds target x86-64 Linux.

| Format | Best for | Start command |
|---|---|---|
| AppImage | Most glibc-based desktop distributions | `./BedrockOnLinux-*-x86_64.AppImage` |
| `.deb` | Debian, Ubuntu, Mint and LMDE | `sudo apt install ./bedrock-on-linux_*_amd64.deb` |
| Portable `.pyz` | A host with Python 3.9+ and Tk | `./bedrock-on-linux-*.pyz gui` |
| Flatpak bundle | Sandboxed local installation, when provided | `flatpak install --user ./BedrockOnLinux-*-x86_64.flatpak` |

### AppImage quick start

```bash
chmod +x BedrockOnLinux-2.1.1-x86_64.AppImage
./BedrockOnLinux-2.1.1-x86_64.AppImage
```

The first **PLAY** needs the matching engine archive:

```text
GDK-Proton-xuser-<engine-revision>.tar.gz
```

With an internet connection, the launcher downloads the exact archive from
the BedrockOnLinux release automatically. You can instead keep that engine
asset beside the AppImage or `.pyz`; a matching local sidecar is preferred and
verified before extraction. This is required when testing an unpublished
candidate and is useful for an offline first install.

An existing installation is upgraded in the same way on its next **PLAY**.
The launcher validates the new engine before an atomic replacement and keeps
the previous tree if download, disk space, extraction or verification fails.
The fix is therefore distributed to existing users as well as fresh installs.

If FUSE is unavailable, AppImage can extract itself at runtime:

```bash
APPIMAGE_EXTRACT_AND_RUN=1 ./BedrockOnLinux-2.1.1-x86_64.AppImage
```

The AppImage bundles Python, Tk, the GUI toolkit, `cryptography` and CA
certificates. It still uses the host graphics driver and common X11, Xft and
fontconfig libraries. The `.deb` declares its host dependencies; the portable
`.pyz` uses the host Python environment and can install its pinned Python GUI
and sign-in dependencies on first use.

Flatpak is built separately when a builder is available. See
[`flatpak/README.md`](flatpak/README.md) for local build and permission details;
the presence of a manifest does not imply that a particular release is already
published on Flathub. Flatpak data is kept in the application-private XDG
folder:

```text
~/.var/app/io.github.wyze3306.BedrockOnLinux/data/bedrock-on-linux/
```

All new writes stay in that private directory. For one upgrade cycle the
manifest retains read-only access to the exact legacy
`~/.local/share/bedrock-on-linux` folder, so the launcher can copy it
atomically before any command creates the new root. The explicit home-relative
path is required: Flatpak maps the special `xdg-data` alias onto the private
XDG destination, which would make the destination read-only. The copy
re-anchors the selected game path and internal `content` link; the host copy
remains as a recovery backup. Two populated trees are never merged
automatically.

If a local Flatpak permission override removed that transition access, close
the app and temporarily restore only the read-only legacy path:

```bash
flatpak kill io.github.wyze3306.BedrockOnLinux
flatpak override --user \
  --filesystem='~/.local/share/bedrock-on-linux:ro' \
  io.github.wyze3306.BedrockOnLinux
flatpak run io.github.wyze3306.BedrockOnLinux
```

Confirm the account, worlds and selected version in the private folder before
removing the local override. Never merge two non-empty data roots blindly.

## Requirements and limitations

- An **x86-64 glibc desktop**. The AppImage and managed engine are audited
  against a glibc 2.31 baseline. ARM and musl-only systems such as stock Alpine
  are not supported. A host i386 userspace is not required; the managed engine
  uses Wine's pure-WoW64 path.
- **X11 or XWayland for the launcher GUI.** The game normally uses X11/XWayland.
  Native Wine Wayland can be tried with `BOL_INPUT=wayland`, but remains an
  experimental game backend; it does not remove the launcher’s XWayland
  requirement.
- For the normal renderer, a working **Vulkan 1.3** driver exposing
  `VK_EXT_device_generated_commands`, or the older NVIDIA
  `VK_NV_device_generated_commands`. The managed vkd3d-proton 3.0.1 payload
  contains both implementations and chooses inside the game process.
- GPUs permanently limited to Vulkan 1.2 can try **Settings ▸ Advanced ▸
  Legacy compatibility renderer**. This is a last resort: it swaps the whole
  Direct3D stack — D3D9 through **D3D12** — to WineD3D, so both DXVK and
  vkd3d-proton are dropped. Minecraft renders exclusively through D3D12, so
  the option replaces the renderer the game actually uses; visual artifacts
  are common and ray tracing is unavailable. Performance is not guaranteed to
  improve.
- Enough free storage for the game, compressed engine and temporary
  extraction. A `No space left on device` error is non-destructive: free space
  and retry **PLAY**.
- A Microsoft account entitled to Minecraft. Friends, multiplayer and Realms
  also depend on the account’s privacy settings, any required Realms
  subscription or invitation, and the availability of Microsoft/Xbox/Minecraft
  services.

The launcher is an independent compatibility project and is not affiliated
with or supported by Mojang or Microsoft. Minecraft updates can change private
game interfaces; select a known-working version or attach diagnostics when a
new version regresses.

## Play

1. Open **BedrockOnLinux**.
2. Select **Sign in**, open the Microsoft device-code page shown by the
   launcher and enter its code.
3. Select a Minecraft version, then choose **▶ PLAY**.
4. Use Minecraft’s **Friends**, **Servers** and **Realms** tabs normally.

The first run downloads and prepares Minecraft, then downloads and verifies
the managed engine and its online/TLS compatibility payload. Later runs reuse
them. Account credentials are stored in the private BedrockOnLinux data
directory and seeded into the stopped Wine prefix before launch.

To refresh a same-tag archive replacement instead of reusing the local cache,
run `bedrock-on-linux setup --mc <version> --force`. This still cannot retrieve
an internal build the community archive has not published.

### Multiple local Xbox profiles

Create one isolated launcher root and desktop shortcut per player:

```bash
bedrock-on-linux profiles create "Alice"
bedrock-on-linux profiles create "Bob"
bedrock-on-linux profiles list
```

Each shortcut sets its own `BOL_HOME`, so the Microsoft account, Wine prefix,
pre-auth cache, settings and worlds stay separate. Game packages, Proton, UMU
and download caches are shared to avoid duplicate multi-gigabyte downloads.
Add the matching shortcut to each Steam user as a separate non-Steam game.
When created from an AppImage, the shortcut records the persistent AppImage
file rather than its temporary mounted executable. Because PLAY can repair a
shared runtime before starting, only one profile can run Minecraft at a time;
setup/update is also refused while that session owns the shared-assets lock.

Create profiles after choosing the final **Game files location**. Relocation is
refused while profiles exist because moving their shared base would break the
links. Host-visible desktop/Steam shortcuts cannot be installed from inside
the Flatpak sandbox; use the AppImage, `.deb` or native package for this
workflow.

### Achievements

The launcher prepares a dedicated user-only XSTS token for Minecraft's original
Windows Achievements request. XUser selects it only for the Achievements
service, preserving the packaged Windows title, SCID and platform while leaving
social, Marketplace, PlayFab, multiplayer and Realms authentication unchanged.
This loads the existing list; it does not unlock, emulate or force
achievements. Minecraft/Xbox policy, the world configuration, cheats, Creative
mode and some add-ons can disable or affect achievement eligibility.

Minecraft’s **Import World** and skin-selection actions use the WineGDK native
file picker. For direct launcher-side content installation while Minecraft is
closed, use:

```bash
bedrock-on-linux import world.mcworld addon.mcaddon pack.mcpack \
  template.mctemplate skin.mcskin
```

The launcher imports `.mcworld`, `.mcaddon`, `.mcpack`, `.mctemplate` and
`.mcskin` archives into the appropriate `com.mojang` directory. This is useful
for direct or scripted installation; it is not a replacement for Minecraft's
in-game custom-skin picker. Existing items are not overwritten; an available
folder name is selected.

### Compatible external client DLLs

While Minecraft is at its main menu, **Settings ▸ Tools ▸ Inject a client
DLL…** can load a compatible 64-bit Windows client DLL through the same Wine
prefix as the game. The client release must target the exact Minecraft line:
a repository’s “latest” DLL may still target an older game build. After
`LoadLibrary` succeeds, the launcher watches briefly for an immediate game exit
or a new Wine crash debugger and reports that as a failed injection instead of
a false success.

BedrockOnLinux does not ship or endorse client DLLs. Their Wine compatibility,
dependencies, safety, update compatibility and compliance with server rules
remain the user’s responsibility. The injector is supported by the native,
AppImage and `.deb` layouts, not by the Flatpak sandbox; a successful injection
cannot be promised for a specific third-party client.

## GPU safety

BedrockOnLinux deliberately does not open a Vulkan or OpenGL device to guess
whether the driver is healthy. Before launch it can detect, from already
available state, conditions such as:

- an X11 session with no hardware RandR provider;
- FBDEV or software-rendering fallback;
- a fatal graphics-driver event in the kernel journal;
- a Minecraft GPU session that did not return before a reboot or power loss.

The last case is recorded by a durable launch marker. A normal completed
shutdown retires it only after the Wine prefix is idle. Version 2.0 records a
separate durable wrapper-return phase, so a delayed normal Wine teardown no
longer becomes a permanent same-boot block. Older markers cannot distinguish
the userspace crash reported in issue
[#31](https://github.com/Wyze3306/BedrockOnLinux/issues/31) from a real driver
failure and therefore still require the explicit one-time acknowledgement
below.

If the desktop freezes or the machine requires a forced power-off, do not
blindly retry. Inspect why the session stopped and reboot. If the kernel log
contains a fatal GPU event, repair or update the host graphics driver first; a
marker alone does not prove a driver fault. When the launcher can prove that
the remaining incident belongs to the previous boot, **Settings ▸ Tools**
displays **Acknowledge previous GPU incident…** with a confirmation step. The
equivalent CLI flow is:

```bash
bedrock-on-linux doctor
bedrock-on-linux doctor --acknowledge-gpu-crash
```

`bedrock-on-linux` is on `PATH` only for the `.deb` and native packages. The
AppImage, portable `.pyz` and Flatpak are invoked through their own entry
point, so run `./BedrockOnLinux-*.AppImage doctor …`,
`./bedrock-on-linux-*.pyz doctor …` or
`flatpak run io.github.wyze3306.BedrockOnLinux doctor …` instead. Launcher
messages print the exact command for the installation in use.

Acknowledgement is revalidated while the launch lock is held and clears only
an old interrupted-session block or records a previous-boot driver incident.
It is refused when there is no eligible incident, Wine/UMU is still running,
the marker belongs to the current boot, its origin is unreadable, or the
current kernel journal contains a GPU fault. A current unsafe display state
remains blocked. The advanced override below is intended only for a confirmed
false positive and can re-expose a kernel hard lock:

```bash
BOL_ALLOW_UNSAFE_GPU=1 bedrock-on-linux play
```

## Diagnostics and recovery

Open **Settings ▸ Open logs folder**, or inspect:

```text
$XDG_DATA_HOME/bedrock-on-linux/logs/
```

`XDG_DATA_HOME` defaults to `~/.local/share`; `BOL_HOME` or a location selected
in Settings replaces that root. Flatpak uses the private path shown in the
installation section. If a native XDG-root change triggers the one-time copy,
the previous data and UMU trees are retained as recovery backups; remove them
manually only after verifying the new location.

The GUI’s **Details** panel contains the live launcher log. Useful commands:

```bash
bedrock-on-linux doctor                 # host dependencies and GPU safety
bedrock-on-linux doctor --network       # DNS/TLS, clock and VPN observations
bedrock-on-linux doctor --host 192.0.2.10 # plus route to one literal LAN IP
bedrock-on-linux repair                 # rebuild the managed Wine prefix
bedrock-on-linux versions               # available stable versions
bedrock-on-linux versions --beta        # include beta versions
bedrock-on-linux setup --mc <version>   # download and prepare one version
bedrock-on-linux profiles list          # isolated local Xbox profiles
bedrock-on-linux login                  # link a Microsoft account
bedrock-on-linux play                   # launch the selected version
bedrock-on-linux update                 # check for a launcher update
```

Network diagnostics are read-only. They resolve and establish
certificate-verified TLS connections to the Xbox, PlayFab and Minecraft
endpoints, report NTP/RTC and commonly named VPN or container-bridge
interfaces, and optionally show the kernel route to a validated literal host
IP. A route does not prove that the host accepts RakNet UDP 19132, so the
doctor deliberately does not claim that an unanswered UDP probe is an
open-port test. Logs additionally recognise `InitialConnection-13`,
`InitialConnection-25` and explicit client/host version-mismatch signatures
and provide host-side guidance.

A Friends-world “full” response can originate from the remote player-host
session. Once the client and host use the same internal build, the Windows host
owner should change the active network profile from **Public** to **Private**,
toggle **Multiplayer Game** off and back on for the world, then fully restart
both games. A guest without access to that remote host cannot apply the fix;
use a correctly configured Bedrock Dedicated Server when host ownership is not
available. BedrockOnLinux cannot change a remote host or repair a
Minecraft/Xbox service regression.

`repair` resets compatibility state, not the host graphics driver. Back up any
important game data before manual changes under the active data directory
(`$XDG_DATA_HOME/bedrock-on-linux` by default). When reporting a bug, include
the launcher version, engine revision, Minecraft version, distribution,
GPU/driver and the relevant log files; never publish account tokens or the
private authentication directory.

## Engine integrity and source provenance

The managed engine is not built on the user’s computer. Release maintainers
produce it from pinned inputs and the launcher accepts only the revision and
archive SHA-256 recorded in [`bol/config.py`](bol/config.py).

- WineGDK is built in an unprivileged Debian 11 (Bullseye) chroot. Every
  resulting ELF is rejected if it requires a glibc symbol newer than 2.31.
- The exact WineGDK source commit, reviewed patches and changed-file hashes are
  stored under [`third_party/`](third_party/).
- The universal vkd3d-proton build contains reviewed EXT-DGC and restored
  NV-DGC variants for x86-64 and i386. Its inputs and output hashes are
  documented in
  [`third_party/vkd3d-proton-universal/README.md`](third_party/vkd3d-proton-universal/README.md).
- `scripts/package-engine.sh` embeds licences, build records, source
  provenance and an `engine-manifest.json` that hashes critical runtime files.
  The completed archive is extracted and rechecked before it is accepted as a
  candidate.
- Engine installation uses a lock and transactional rename. An interrupted or
  invalid update cannot silently become the active managed engine.

The native Xbox and WinAppSDK work is built for both PE architectures. The
packager verifies native XGame/XUser markers, file-picker registration and the
absence of the former memory-patcher code before creating the archive.

## Build from source

Application builds require the matching engine and OpenSSL XCurl assets to
already be present in `dist/`; release scripts do not download or publish
unreviewed substitutes.

```bash
# Build the pinned WineGDK source in a clean Bullseye work directory.
scripts/build-winegdk-bullseye.sh /path/to/empty-workdir /path/to/WineGDK

# Build the reviewed universal vkd3d-proton payload when it is not available.
scripts/build-vkd3d-universal.sh /path/to/empty-vkd3d-workdir

# Package the engine from reviewed inputs and the WineGDK build prefix.
scripts/package-engine.sh \
  /path/to/staged/GDK-Proton-xuser \
  /path/to/vkd3d-proton-3.0.1-universal-dgc \
  "$(python3 -c 'from bol.config import WINEGDK_SOURCE_COMMIT; print(WINEGDK_SOURCE_COMMIT)')" \
  /path/to/GDK-Proton10-32.tar.gz \
  /path/to/empty-workdir/prefix

# Build and verify .deb, AppImage, portable .pyz and local/dev Flatpak candidates.
scripts/build-release.sh
scripts/verify-release-candidate.sh

# Force-install the exact local sidecar and run it; never publishes anything.
scripts/run-candidate.sh gui

# Optional standalone local/dev Flatpak rebuild. For publication, pin the
# release tag and commit in the manifest, then add --release.
scripts/build-flatpak.sh
```

`scripts/build-release.sh` only creates local candidate files and checksums in
`dist/`; its Flatpak is intentionally a working-tree development bundle. It
does not tag, push or upload a release. Bump the engine revision
whenever engine contents or packaging change, update the reviewed archive hash
in `bol/config.py`, run the complete test suite and smoke-test the exact
candidate before publication.

## Legal and license

BedrockOnLinux ships **no Minecraft game files**. Game packages are obtained
from the community-maintained
[`bubbles-wow/mcbe-gdk-unpack-archive`](https://github.com/bubbles-wow/mcbe-gdk-unpack-archive),
or from a local source selected by the user. You must own Minecraft and comply
with the terms that apply to it.

WineGDK, GDK-Proton, vkd3d-proton and bundled dependencies remain under their
respective licences; the engine includes their relevant notices and
provenance. BedrockOnLinux itself is MIT-licensed — see [`LICENSE`](LICENSE).
