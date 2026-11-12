# Universal vkd3d-proton payload for engines r11, r12 and native5 through native12

BedrockOnLinux engine revisions `wow64-archs-r11`, `wow64-archs-r12` and
`wow64-archs-native5` through `wow64-archs-native12` use a reviewed vkd3d-proton 3.0.1
build containing both Vulkan device-generated-command implementations.
vkd3d-proton itself selects `VK_EXT_device_generated_commands` when the
selected device fully supports it and falls back to
`VK_NV_device_generated_commands` on older NVIDIA drivers.

This directory is the auditable provenance and rebuild bundle for that binary
payload. It does not contain game files and none of its scripts publish or
release anything. It is not, by itself, a complete corresponding-source
archive: the recipe checks out the exact recursive source tree described below.

## Exact source transformation

- Upstream: <https://github.com/HansKristian-Work/vkd3d-proton>
- Tag: `v3.0.1`
- Base commit: `3b10bd7a7ec6a7347e616cf8bea59333afec2255`
- Restored commit: `76c11d2e2b90b0a46dc894508e67e2aaacc2c04d`
- Operation: `git revert --no-commit 76c11d2e2b90b0a46dc894508e67e2aaacc2c04d`
- Resulting binary patch SHA-256:
  `91878d389dc0e315f770fa6c7fffea8f78f410a04796c38e2a6410ff0b9b4a33`

The complete revert is vendored as `restore-nv-dgc.patch`. The build script
performs the Git revert itself and requires its generated patch to be identical
to that file. `submodules.lock` similarly fixes every recursive submodule.

Two vendored fixes are then applied, in this order, each hash-checked against
`provenance.env` first:

1. `fix-occluded-frame-latency.patch` (#50, since native14);
2. `fix-frame-limiter-busy-wait.patch` (#202, not in a published engine yet).

## Occluded frame latency (#50)

`fix-occluded-frame-latency.patch` fixes the game freezing for good when its
window is minimized or left behind on another virtual desktop
([#50](https://github.com/Wyze3306/BedrockOnLinux/issues/50)). It has been
part of the payload since engine `wow64-archs-native14`.

### What it fixes

`dxgi_vk_swap_chain_Present()` returns `DXGI_STATUS_OCCLUDED` before it
enqueues anything, so the present callback that releases the frame latency
waitable object never runs. Minecraft creates a waitable swapchain — its
graphics log says `Enabling frame latency handles` — and waits on that object
before every frame, so the first occluded present ends its render loop.

Under Wine that is not a paused frame but a permanent hang, because the
condition that produced the occlusion can no longer clear:

1. The window manager iconifies the window, so Wine sets `WS_MINIMIZE` and
   the Win32 client rect becomes empty.
2. `adjust_surface_capabilities()` in `dlls/win32u/vulkan.c` derives the
   Vulkan surface extents from that client rect, so `maxImageExtent` is 0 and
   vkd3d-proton considers the swapchain occluded.
3. The render loop blocks on the waitable object, so the window stops pumping
   messages, and Wine never processes the un-iconify. The client rect stays
   empty and step 2 stays true for ever.

Releasing the waitable object on the occluded path — exactly what the
present-wait worker does for a retired present — keeps the loop running, so
the window keeps pumping messages and picks the window state back up.

The legacy WineD3D renderer is unaffected because it does not go through this
swapchain, which is why turning it on has been the workaround in that issue.
Upstream `master` still has the same early return, so this is worth sending
upstream rather than carrying for ever.

### Reproducing and validating

Reproduced and diagnosed on X11 (NVIDIA, Cinnamon/Muffin), where minimizing
the game window or switching virtual desktop freezes it every time:

```bash
# with the game running, iconify its window, restore it, then observe
python3 -c 'import ctypes;x=ctypes.cdll.LoadLibrary("libX11.so.6");
x.XOpenDisplay.restype=ctypes.c_void_p;d=ctypes.c_void_p(x.XOpenDisplay(None));
x.XIconifyWindow(d,ctypes.c_ulong(WINDOW_ID),x.XDefaultScreen(d));x.XFlush(d)'
wmctrl -i -a WINDOW_ID
```

Before the fix the window comes back black, the process settles at 0% CPU and
never repaints, and the desktop offers to force quit it. A Win32 probe run
against the same prefix reports the window still minimized after the restore
(`IsIconic` 1, `showCmd` `SW_SHOWMINIMIZED`, client rect `0x0`, window rect
`-32000,-32000 160x31`), a cross-process `ShowWindow(SW_RESTORE)` blocks
because the window is not pumping messages, and `WINEDEBUG=+x11drv` shows
`window_update_client_state minimizing win` with no restore ever following it.
After the fix the same sequence must return to a rendering window.

Expect the game to keep rendering while it is minimized, as it already does
under the legacy renderer; if that costs too much on battery, throttle the
occluded path with `dxgi_vk_swap_chain_platform_sleep_for_ns()` rather than by
withholding the release again.

### Adopting it

Adopted in native14: the recipe applies it after the revert verification,
`provenance.env`, `scripts/package-engine.sh` and `OUTPUT-SHA256SUMS` pin it,
and `bol/vkd3d.py` pins the resulting `d3d12core.dll` bytes.

## Frame limiter busy-wait (#202)

`fix-frame-limiter-busy-wait.patch` keeps the busy-wait that ends every frame
of vkd3d-proton's frame rate limiter short under Wine
([#202](https://github.com/Wyze3306/BedrockOnLinux/issues/202)). The recipe
applies it after the occlusion fix and `OUTPUT-SHA256SUMS` describes the
payload built with it: `d3d12core.dll` changes on both architectures and
`d3d12.dll` reproduces byte for byte. No published engine contains it yet;
see *Shipping it* below.

### What it fixes

When a frame rate cap is set (`VKD3D_FRAME_RATE`, or DXGI's
`SetTargetFrameRate`), `dxgi_vk_swap_chain_delay_next_frame()` in
`libs/vkd3d/swapchain.c` sleeps with `NtDelayExecution` until a threshold
before each deadline, then spins with `vkd3d_pause()` for the rest. Upstream
sets the threshold to the timer interval plus a sixth of the wait, and under
Wine the timer interval is the 1 ms that `NtQueryTimerResolution` always
reports (the log line `Timer interval is 1.0 ms`). The present-wait thread,
`vkd3d-swapchain`, therefore spun for 1 ms plus a sixth of every frame: 22-23%
of a core at 60 FPS and 25-30% at 141 FPS in the real game (RTX 4060,
Minecraft 1.26.60.5), more than the game's own main thread in the menu. The
launcher relies on this limiter whenever the player's cap is below the
display's refresh rate, for `BOL_FRAME_RATE`, and for the default "limit to the
display" cap with vsync off.

Wine needs far less. A non-alertable `NtDelayExecution` is a `select()` with a
microsecond timeout (`dlls/ntdll/unix/sync.c`), so it oversleeps by the host's
timer slack, not by a timer interval. Measured with `tools/limiter-sleep-probe.c`
under the engine's Wine 11.14 on Linux 6.16 (Ryzen 5 5600, 12 threads), for waits from
0.1 to 15 ms:

| load | median oversleep | p99 | worst |
|---|---|---|---|
| idle | 56-60 µs | 63-126 µs | 0.28 ms, plus one 5 ms scheduling hiccup |
| 12 busy threads | 55-57 µs | 57-68 µs | 0.31 ms |

The same probe replays the limiter's tail. Upstream's threshold spins for 22%
of the thread's time at 60 FPS and 30% at 141 FPS (the in-game figures), and
under full CPU contention it is also the less punctual one, because the
scheduler preempts a thread that spins that long: 3.5 ms late at the 90th
percentile at 60 FPS. A fixed 0.5 ms tail spins for 2.6% and 6.2% and released
every frame on time at the 90th percentile in both runs.

So under Wine, when `NtDelayExecution` is available, the patch uses a fixed
0.5 ms tail, comfortably above the worst oversleep measured outside scheduling
hiccups. Windows and native Linux builds keep the upstream threshold. With
`VKD3D_DEBUG=info` the log says `Using a fixed 0.5 ms busy-wait tail for the
frame rate limiter.`

### Measured in the game

Real launcher and game (Minecraft 1.26.60.5, RTX 4060, driver 550) in a
headless gamescope at 1600x900 and 144 Hz, vsync on, main menu, cap set with
`BOL_FRAME_RATE`. The patched `d3d12core.dll` was bind-mounted over the
installed engine in a private mount namespace, and its bytes were checked in
the game's own mappings. CPU is per thread over 10-15 s windows, in % of one
core; frame times are the intervals between the present-wait thread's releases
of the frame latency object (`NTSYNC_IOC_SEM_POST`) over 15 s.

| cap | build | `vkd3d-swapchain` | whole game | frame time median / p99 / max |
|---|---|---|---|---|
| 60 FPS | upstream threshold | 20.0-21.7% | 53-60% | 16.67 / 17.15 / 173.6 ms |
| 60 FPS | fixed 0.5 ms tail | 3.8-5.9% | 35-37% | 16.67 / 16.68 / 16.77 ms |

### Shipping it

Shipping it is a new engine revision, so it waits for the maintainer's go-ahead:

1. Publish the payload: `gh workflow run build-vkd3d.yml --ref <branch> -f
   publish=true`, or let `release.yml` do it, since it builds a vkd3d release
   that does not exist yet. The release is named by
   `scripts/vkd3d-release-tag.sh` after the hash of `OUTPUT-SHA256SUMS`
   (`vkd3d-3.0.1-nv-dgc-32d230b6254d`), so the `vkd3d-3.0.1-nv-dgc` release
   keeps the native14-native22 payload those engines were assembled from.
2. Bump `WINEGDK_BUILD_REV` in `bol/config.py` and
   `REQUIRED_VARIANT_HASHES_BUILD_REV` in `bol/vkd3d.py` together, and set the
   `d3d12core.dll` hashes of both variants in `REQUIRED_VARIANT_HASHES` to
   `fb087ac16045927ffe2003ef210925b53d701add127927957fc0e8647c89b8aa`
   (x86_64) and `ec67019917132762cda430bd0f12df2d352937bb98345b336f5a2f93bab9ddb0`
   (i386). The `d3d12.dll` hashes stay.
3. Build the engine in two passes as usual: the first run of
   `build-engine.yml` prints the archive SHA-256 to pin as
   `WINEGDK_ARCHIVE_SHA256`, the second publishes it. The WineGDK prefix is
   reused unchanged.

## Rebuild

The reviewed toolchain was Debian trixie with these exact components:

- GCC/MinGW `14.2.0-19+27+b1` (`14-win32` targets), for x86-64 and i686;
- MinGW binutils `2.44-3+12+b1`;
- `mingw-w64-tools` `12.0.0-5`;
- Meson `1.7.0-1`;
- Ninja Python package `1.13.0`;
- `glslang-tools` `15.1.0+1.4.309.0-1`;
- `spirv-tools` `2025.1~rc1-1`;
- Python `3.13.5` and Git `2.47.3`.

Put those tools on `PATH` (and Meson's Debian modules on `PYTHONPATH` when
using locally extracted `.deb` files), then choose a new empty work root:

```bash
scripts/build-vkd3d-universal.sh /tmp/bol-vkd3d-r11
```

The result is written to:

```text
/tmp/bol-vkd3d-r11/vkd3d-build-output/vkd3d-proton-3.0.1-nv-dgc
```

The historical directory names and per-architecture `SOURCE_DATE_EPOCH`
values are deliberate binary inputs: stripped vkd3d-proton DLLs retain
relative `__FILE__` strings, and GNU's PE linker records a timestamp. The
script refuses toolchain drift and finally verifies all four DLLs against
`OUTPUT-SHA256SUMS`. Never update those hashes merely to make an unexplained
rebuild pass.

After independent review, the verified directory can be passed to the engine
packager:

```bash
scripts/package-engine.sh /path/to/GDK-Proton-xuser \
  /tmp/bol-vkd3d-r11/vkd3d-build-output/vkd3d-proton-3.0.1-nv-dgc \
  75637b674e1f191e65753663c4c0c32bea05ba6e \
  /path/to/GDK-Proton10-32.tar.gz \
  /path/to/winegdk-native5-work/prefix
```

## Licence and distribution

vkd3d-proton source files are distributed under the GNU Lesser General Public
License 2.1 or later; the upstream LGPL 2.1 text is included as
`COPYING.LGPL-2.1`. Its recursively checked-out dependencies retain the
licences found in their pinned source trees. BedrockOnLinux's build recipe is
MIT-licensed with the main project.

When distributing the modified DLLs, retain this provenance, the revert patch
and all licence notices, and make the complete pinned recursive source checkout
available under the applicable licences so recipients can rebuild and modify
the corresponding source.

`scripts/package-engine.sh` verifies the fixed SHA-256 of every file in this
directory that forms the distribution bundle, verifies the four built DLLs
against `OUTPUT-SHA256SUMS`, and embeds the bundle in the engine at:

```text
files/share/bedrock-on-linux/licenses-and-provenance/vkd3d-proton-universal/
```

The embedded `SHA256SUMS` covers the licence, provenance lock, recursive
submodule lock, output hash lock and restoration patch. The packager extracts
those records back out of the completed compressed candidate and rechecks them
before it publishes the local archive path.
