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
![NixOS](https://img.shields.io/badge/NixOS-5277C3?style=flat-square&logo=nixos&logoColor=white)

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
| Nix | NixOS, or any Linux with Nix installed | `nix run github:Wyze3306/BedrockOnLinux` |

### Nix / NixOS

Try it without installing anything:

```bash
nix run github:Wyze3306/BedrockOnLinux
```

Or add it as a flake input to install it declaratively, for example into
`environment.systemPackages`:

```nix
# flake.nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    bedrock-on-linux = {
      url = "github:Wyze3306/BedrockOnLinux";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, bedrock-on-linux, ... }: {
    nixosConfigurations.your-host = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        {
          environment.systemPackages = [
            bedrock-on-linux.packages.x86_64-linux.default
          ];
        }
        # ...your other modules
      ];
    };
  };
}
```

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

### Experimental OptiScaler / frame generation

This fork can install a tested OptiScaler v10 setup for Minecraft RTX under
Wine/vkd3d-proton, including the physical `nvngx.dll` workaround needed to
expose Bedrock's native DLSS input on the tested non-NVIDIA setup:

```bash
bedrock-on-linux optiscaler install
bedrock-on-linux optiscaler status
```

OptiScaler is downloaded from upstream and is not bundled with this project.
Frame generation remains opt-in in the OptiScaler overlay. See
[docs/OPTISCALER.md](docs/OPTISCALER.md) for the exact setup, management
commands, tested pipeline and compatibility notes.

## What you need

- A 64-bit Linux desktop, reasonably up to date.
- A graphics card and driver that support Vulkan: anything from the last few
  years, with the driver your distribution ships.
- A Microsoft account that owns Minecraft, since the game is downloaded under
  your own licence.
- Enough free disk space for the game and its runtime, a few gigabytes.

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
