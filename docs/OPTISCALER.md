# OptiScaler integration (experimental)

This fork can install and load a tested OptiScaler v10 build for Minecraft Bedrock RTX under Wine/vkd3d-proton. The integration was developed from a working Linux setup where Minecraft's native DLSS input is intercepted by OptiScaler and can feed another upscaler and OptiFG frame generation.

The tested path was:

```text
Minecraft RTX native DLSS input
  -> OptiScaler
  -> XeSS / FSR output
  -> OptiFG
  -> XeFG / FSR frame generation
```

On the tested RX 7600 XT setup, XeFG produced about 98 displayed FPS from about 49 base FPS.

## Install

OptiScaler is **not bundled** with BedrockOnLinux. The command below downloads a pinned, tested upstream build, verifies its SHA-256, installs it into BedrockOnLinux's data directory and enables it for the active Minecraft build:

```bash
bedrock-on-linux optiscaler install
```

The current pin is OptiScaler `v10.0.0-pre1_20260916` (`nightly-20260916`).

The `.7z` archive needs either `7zz`, `7z` or `7za` on `PATH`, or the optional Python package `py7zr`.

You can also install from an archive or already-extracted OptiScaler directory:

```bash
bedrock-on-linux optiscaler install --source /path/to/OptiScaler.7z
bedrock-on-linux optiscaler install --source /path/to/extracted/OptiScaler
```

## What the integration changes

When enabled, BedrockOnLinux prepares the selected game directory before launch:

- installs OptiScaler as the game-local `dxgi.dll` proxy;
- adds `dxgi.dll=n,b` to `WINEDLLOVERRIDES` without discarding BedrockOnLinux's other DLL overrides;
- enables `DXVK_NVAPI_ALLOW_OTHER_DRIVERS=1` and reports an Ada-class NVIDIA architecture to dxvk-nvapi;
- configures OptiScaler's DXGI, registry and User32 spoofing as an RTX 4090;
- writes a small managed block to `dxvk.conf` that exposes the spoofed NVIDIA adapter to Minecraft;
- enables OptiScaler's DLSS input hook;
- copies the **installed Minecraft build's own** `nvngx_dlss.dll` to a physical `nvngx.dll` beside the executable.

That last file is important on Bedrock under Wine: without a physical `nvngx.dll`, the in-game Upscaling setting can remain enabled but greyed out even though OptiScaler is loaded. BedrockOnLinux does not download or redistribute NVIDIA NGX binaries; it only copies the file already shipped with the user's installed Minecraft build.

Existing OptiScaler overlay choices are preserved when the launcher re-syncs the integration. BedrockOnLinux only enforces the capability-spoof and DLSS-input settings needed to expose the path.

## Use

Launch Minecraft normally after installing OptiScaler. Press **Insert** to open the OptiScaler overlay.

OptiScaler does not turn frame generation on automatically. Choose the upscaler and frame-generation outputs you want, then explicitly enable **Active** under frame generation. **Page Up** opens the OptiScaler performance overlay; with 2x frame generation working it reports roughly twice the displayed FPS as base FPS, for example `98 / 49`.

The exact best output depends on the GPU and driver. The tested setup successfully used `DLSS -> XeSS 2.0.2` with `OptiFG -> XeFG` on AMD RDNA3.

## Manage

```bash
bedrock-on-linux optiscaler status
bedrock-on-linux optiscaler enable
bedrock-on-linux optiscaler disable
bedrock-on-linux optiscaler uninstall
```

`disable` removes only files it can verify were managed by this integration and removes its block from `dxvk.conf`. It deliberately leaves `OptiScaler.ini` and the backend directory in place so custom overlay settings are not destroyed. `uninstall` also removes the cached OptiScaler payload.

If you switch to a different installed Minecraft build while the launcher is already open, run `bedrock-on-linux optiscaler enable` once (or restart BedrockOnLinux) to sync the proxy into the newly selected build.

## Safety and compatibility

This is experimental middleware injection. Keep a way to disable it if a Minecraft or OptiScaler update changes the rendering path. The integration refuses to overwrite an unknown existing `dxgi.dll` or `nvngx.dll` rather than silently breaking another mod.

OptiScaler itself warns against use with games protected by anti-cheat. Check OptiScaler's upstream documentation and licence before using or redistributing its binaries.
