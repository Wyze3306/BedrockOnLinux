<div align="center">

# 🟩 BedrockOnLinux

**Minecraft Bedrock for Windows, running on Linux, with real Xbox sign-in,
Friends, servers and Realms.**

[![Download](https://img.shields.io/github/v/release/Wyze3306/BedrockOnLinux?style=for-the-badge&logo=github&logoColor=white&label=Download&color=2ea043)](https://github.com/Wyze3306/BedrockOnLinux/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/Wyze3306/BedrockOnLinux/total?style=for-the-badge&logo=github&logoColor=white&label=Downloads&color=444d56)](https://github.com/Wyze3306/BedrockOnLinux/releases)
[![Website](https://img.shields.io/badge/Website-0b7285?style=for-the-badge&logo=googlechrome&logoColor=white)](https://wyze3306.github.io/BedrockOnLinux/)
[![Discord](https://img.shields.io/badge/Discord-5865F2?style=for-the-badge&logo=discord&logoColor=white)](https://discord.gg/5YJq54Yhbu)
[![License](https://img.shields.io/badge/License-MIT-6e7781?style=for-the-badge&logo=opensourceinitiative&logoColor=white)](LICENSE)

![Ubuntu](https://img.shields.io/badge/Ubuntu-E95420?style=flat-square&logo=ubuntu&logoColor=white)
![Debian](https://img.shields.io/badge/Debian-A81D33?style=flat-square&logo=debian&logoColor=white)
![Linux Mint](https://img.shields.io/badge/Mint%20%2F%20LMDE-87CF3E?style=flat-square&logo=linuxmint&logoColor=white)
![Fedora](https://img.shields.io/badge/Fedora-51A2DA?style=flat-square&logo=fedora&logoColor=white)
![Arch](https://img.shields.io/badge/Arch-1793D1?style=flat-square&logo=archlinux&logoColor=white)
![openSUSE](https://img.shields.io/badge/openSUSE-73BA25?style=flat-square&logo=opensuse&logoColor=white)
![Steam Deck](https://img.shields.io/badge/Steam%20Deck-1A9FFF?style=flat-square&logo=steamdeck&logoColor=white)

![BedrockOnLinux launcher](screenshot.png)

</div>

## What it is

BedrockOnLinux installs and runs the Windows version of Minecraft Bedrock on
Linux. It downloads the game from the Microsoft Store with your own account,
sets everything up for you, and starts it. No Windows, no second machine,
nothing to compile.

You sign in to Microsoft from inside Minecraft, exactly as on Windows, so
Friends, invitations, public servers, Realms and the Marketplace work like they
should. Nothing goes through a third party.

You can also play without an account: single-player worlds and LAN games work
offline, only the online features are out of reach. Achievements show up in the
game, but they don't unlock yet.

## Install

Download the file you want from the
[latest release](https://github.com/Wyze3306/BedrockOnLinux/releases/latest).

| Format | Best for | How to start it |
|---|---|---|
| AppImage | Most Linux desktops | `./BedrockOnLinux-*-x86_64.AppImage` |
| `.deb` | Debian, Ubuntu, Mint, LMDE | `sudo apt install ./bedrock-on-linux_*_amd64.deb` |
| `.rpm` | Fedora, Nobara | `sudo dnf install ./bedrock-on-linux-*.x86_64.rpm` |
| Flatpak | Atomic systems such as Bazzite | `flatpak install --user ./BedrockOnLinux-*-x86_64.flatpak` |

If you are not sure, take the AppImage: it works nearly everywhere and needs
nothing installed.

## Play

1. Open **BedrockOnLinux** and sign in with the Microsoft account that owns
   Minecraft. It is asked for twice, once to download the game from the
   Microsoft Store, once to play online, because the Store needs a session
   of its own. Use the same account both times; the launcher offers the
   second sign-in right after the first, and again if a download needs it.
2. Pick **Minecraft** or **Minecraft Preview**, choose a version, and hit
   **PLAY**.
3. Play, including the **Friends**, **Servers** and **Realms** tabs.

The first launch downloads the game and everything it needs, so give it a
while; after that it starts straight away. You can install an older version
too, which is handy when a server hasn't updated yet.

The launcher can be used with a controller — the d-pad or left stick moves the
highlight, **A** selects, **B** goes back, the shoulder buttons change tab and
**Start** plays — and **Tools ▸ Create direct launch shortcut** adds a
*Minecraft Bedrock* entry to your app menu or to Steam. That is the one to use
on a Steam Deck.

## What you need

- A 64-bit Linux desktop, reasonably up to date.
- A graphics card and driver that support Vulkan: anything from the last few
  years, with the driver your distribution ships.
- A Microsoft account that owns Minecraft, since the game is downloaded under
  your own licence.
- Enough free disk space for the game and its runtime, a few gigabytes.

## macOS

There is a `macos` branch of this launcher, and it is honest about what it can
do. The launcher itself runs natively on a Mac: the same window, the same
settings, the same `doctor`, with its data in
`~/Library/Application Support/bedrock-on-linux`. What changes is underneath.

The Windows runtime is a **native macOS Wine**, not GDK-Proton — the launcher
finds Apple's [Game Porting Toolkit][gptk], CrossOver, Whisky or a plain Wine,
in that order, and uses the best one you have. It installs none of them: they
are separate products with their own licences. Point it at a specific build
with `BOL_WINE=/path/to/wine` if you want a different one.

[gptk]: https://developer.apple.com/games/game-porting-toolkit/

Two things do **not** work on macOS, and neither is fixable from this
repository alone:

- **Downloading Minecraft.** The Microsoft Store download and the decryption
  the game needs at every launch both live in `xodus-cli`, which is built for
  Linux and links WebKitGTK. So on a Mac you bring your own **decrypted**
  Minecraft for Windows folder and point the launcher at it in Settings.
- **Signing in to Xbox Live.** The in-game sign-in is the WineGDK XUser fork
  compiled into GDK-Proton, and there is no macOS build of it. The game runs
  **offline and on the LAN**: single-player worlds and LAN play, no Realms, no
  servers, no Marketplace, no Friends.

What that leaves working is a real thing — a Mac running Bedrock's own Windows
build, on a prefix the launcher prepares, with the GameInput controller stack,
the CA bundle, the stack-reserve fix and the UI patches all applied exactly as
on Linux, because every one of those operates on Windows files.

Build the application bundle with:

```bash
scripts/build-macos-app.sh
```

It runs on a Mac and, just as well, on Linux: nothing in the bundle is
compiled, and pip resolves the macOS universal2 wheels by tag, so a Linux box
produces the same `BedrockOnLinux.app` — unsigned, and with the icon written by
`scripts/png2icns.py` in place of `iconutil`. Cross-building needs `zip` and
Pillow; the script checks that every bundled binary really is Mach-O before it
packages anything.

Run `bedrock-on-linux doctor` there first: it names the Windows runtime it
found, and says which of the checks above simply do not apply.

## If something goes wrong

Start with the built-in check, which looks at your system and tells you what is
wrong:

```bash
bedrock-on-linux doctor
```

If the game itself misbehaves after an update or a crash, `bedrock-on-linux
repair` rebuilds the Windows environment without touching your worlds. Logs are
one click away in **Settings**, and the same commands are available from the
AppImage or Flatpak through their own entry point.

Still stuck? Ask on [Discord](https://discord.gg/5YJq54Yhbu) or
[open an issue](https://github.com/Wyze3306/BedrockOnLinux/issues) with your
launcher version, your distribution, your GPU and the log, but never your
account details.

## Building

Everything is built from source by a public, reproducible pipeline, and each
release is signed. The details are in [`docs/BUILD.md`](docs/BUILD.md).

## Legal

BedrockOnLinux ships **no Minecraft game files**. The game is downloaded from
Microsoft's own servers, under your own account's licence, by
[Xodus](https://github.com/xodus-gaming/xodus), so you have to own Minecraft,
and the terms that come with it still apply.

BedrockOnLinux is MIT licensed, see [`LICENSE`](LICENSE); the components it
bundles keep their own licences. This is an independent project, not affiliated
with or supported by Mojang or Microsoft.
