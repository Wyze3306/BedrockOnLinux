# ARM64EC engine: feasibility study (#250)

Status: study only. Nothing described here is built, pinned or published.
Written 2026-11-12 against engine `wow64-archs-native22` (WineGDK
`75637b674e1f`, Wine 11.1 base) and launcher `main` at `afdce7b`.

## Summary

It is feasible, and the work is mostly packaging rather than porting.

- **Wine:** upstream has supported ARM64EC fully since Wine 10.0. That
  includes the x86-64 emulation interface that FEX-Emu implements as
  `libarm64ecfex.dll`.
- **Proton:** Valve's Proton `master` already builds an ARM64 flavour the
  way this study proposes. Its Makefile runs Wine with
  `--enable-archs=arm64ec,aarch64,…`, builds DXVK and vkd3d-proton for
  arm64ec into `lib/wine/*/aarch64-windows`, builds FEX's ARM64EC and WoW64
  DLLs, and puts everything in `files/bin-arm64` and
  `share/default_pfx_arm64`. GE-Proton 11 publishes `-aarch64` archives.
- **Our proton script:** the one we ship, inherited from GDK-Proton, already
  switches to `bin-arm64` and `default_pfx_arm64` when
  `platform.machine() == "aarch64"`.
- **Patches:** none of our 13 WineGDK patches contains assembly or
  architecture conditionals. WineGDK's own `xgameruntime` is plain C with no
  unixlib.
- **CI runners:** GitHub's `ubuntu-24.04-arm` runners have been free and GA
  for public repositories since 2025-08-07. They are real 4 KiB-page ARM64
  machines, so CI can test far more than it can on x86 (see "Testing").
- **What stays emulated:** the game itself, plus every x64 PE it loads:
  Microsoft's `xgameruntime.dll.threading`, `libHttpClient.GDK.dll`,
  `XCurl.dll`, the OpenSSL XCurl set, GameInput, LeviLamina, OptiScaler and
  our helper `.exe`s. FEX translates these inside the ARM64EC process.
  Wine, vkd3d-proton, DXVK and Mesa (or the NVIDIA driver) become native
  aarch64. Today all of them, including the Vulkan driver taken from the FEX
  RootFS, run through FEX.
- **Asahi:** the microVM stays. Wine's ARM64 support needs 4 KiB pages
  whether or not it is ARM64EC, so the muvm relaunch from `afdce7b` stays.
  Only the emulator moves, from binfmt into the Wine process.

The main blockers are not in Wine:

1. **Container:** umu-launcher cannot set up an arm64 Steam Linux Runtime
   yet. [umu#716](https://github.com/Open-Wine-Components/umu-launcher/issues/716)
   (`KeyError: 'steamrt4-arm64'`) is open, and Valve is phasing out
   `SteamLinuxRuntime_sniper-arm64` in favour of SLR 4
   ([umu#673](https://github.com/Open-Wine-Components/umu-launcher/issues/673)).
2. **vkd3d-proton:** our pinned base `3b10bd7` (2026-05-05) predates
   upstream's arm64ec cross file (`c1e0926`, 2026-09-16, renamed
   `build-arm64x.txt` in `d5f57ce`) and `hybrid_patchable` support
   (`20c0ab2`, 2026-08-19). The DGC payload has to be rebased or backported.
3. **Testing:** no machine here has an ARM GPU, so nobody can run Minecraft
   on it before players do.

## 1. What would change in the engine build

Today the engine is three tiers:

```
build-winegdk.yml  (ubuntu-24.04, debian:bullseye, gcc-mingw i686+x86_64)
build-vkd3d.yml    (ubuntu-24.04, debian:trixie, gcc-mingw)
build-engine.yml   GDK-Proton10-32 base (x86_64 only) + DXVK 3.0.1 release
                   tarball + the two above -> GDK-Proton-xuser-<rev>.tar.gz
```

An ARM build needs a parallel set, with the x86 pipeline left unchanged.

### 1.1 WineGDK prefix (`build-winegdk-arm64.yml`)

- **Runner:** `runs-on: ubuntu-24.04-arm` (4 vCPU, 16 GB RAM, 14 GB SSD for
  public repositories). The x86 job keeps its multi-GB tree on the large
  `/mnt` volume. Check first that the ARM image has a comparable scratch
  volume. Otherwise free space by deleting preinstalled toolchains, or
  cross-compile from the x86 runner (aarch64-linux-gnu gcc for the Unix side
  plus llvm-mingw for the PE side). Running the container under qemu-user
  would be far too slow.
- **Toolchain:** ARM64EC PE code needs LLVM (clang with the `arm64ec`
  target). GCC mingw cannot produce it, and bullseye's clang 11 is far too
  old. Proton uses Valve's `steamrt4/sdk/arm64-llvm` image. For us, a pinned
  llvm-mingw release checked by SHA-256 is the natural fit, for example
  `llvm-mingw-20261104-ucrt-ubuntu-22.04-aarch64.tar.xz`.
- **glibc ceiling:** that llvm-mingw host build needs glibc 2.35, so it
  cannot run in a bullseye (2.31) container. Use `debian:bookworm` (arm64,
  pinned by digest and snapshot) and raise the ARM ceiling to
  `GLIBC_2.36`. That excludes no extra users: xodus-cli already needs 2.39
  (#264), and SLR 4 is Debian 13 based. The x86 ceiling stays 2.31.
  Building llvm-mingw from source inside bullseye would also work, but it
  adds a slow tier of its own.
- **configure:** `--enable-archs=arm64ec,aarch64,i386`. `i386` is not
  needed by Minecraft, but the packager checks `i386-windows/*`, the proton
  script fills `syswow64`, and Proton ships it with FEX's
  `libwow64fex.dll`. Keep it for parity unless the build budget forces a
  cut.
- **Hard-coded x86_64 paths in `build-winegdk-container.sh`:**
  - `lib/wine/x86_64-unix/winewayland.so` and
    `x86_64-windows/winewayland.drv` become the `aarch64-unix` and
    `aarch64-windows` equivalents.
  - The `x86_64-unix/ntdll.so` presence check moves to `aarch64-unix`.
  - The libunwind bundle path becomes `/usr/lib/aarch64-linux-gnu` and
    `lib/aarch64-linux-gnu`, which Proton already puts on
    `LD_LIBRARY_PATH`.
  - The ntsync header and check carry over unchanged: the ioctl ABI is the
    same on arm64.
- **Patches and checks unchanged:** the 13 patches, their SHA-256 checks and
  `SOURCE-SHA256SUMS` apply to the same tree. The source is
  architecture-independent.
- **New pins:** a separate `WINEGDK_ARM64_PREFIX_SHA256`. Never reuse the
  x86 name: one rev names one set of bytes.

### 1.2 FEX (`build-fex.yml`, new tier)

FEX GitHub releases carry no assets (FEX-2611.1, 2026-11-11, has none), so
the DLLs must be built in CI. Follow Proton's recipe:

- `cmake` with `Data/CMake/toolchain_mingw.cmake`.
- `MINGW_TRIPLE=arm64ec-w64-mingw32` gives `libarm64ecfex.dll`, and
  `aarch64-w64-mingw32` gives `libwow64fex.dll`.
- Flags: `-DTUNE_CPU=none -DENABLE_LTO=False -DBUILD_TESTING=False`.
- Pin the FEX tag and its submodules. Pack deterministically, attest, and
  give it its own SHA-256 pin.

FEX's optional Unixlib (since FEX-2607) needs a modern host clang and
libstdc++. Leave it out of a first version.

### 1.3 vkd3d-proton and DXVK for arm64ec

- **vkd3d-proton:** build an arm64x payload (`-marm64x`, cross file as
  upstream's `build-arm64x.txt`) into `vkd3d-proton/aarch64-windows`, with
  the same ext-dgc and nv-dgc slot pairs. There are two ways to get there:
  - Backport `c1e0926`, `d5f57ce`, `20c0ab2` and the older ARM64EC fixes
    (`b9a16bb`, "don't use rdtsc on ARM64EC") onto `3b10bd7` plus the
    `76c11d2` revert.
  - Rebase the DGC payload onto current upstream. That changes the x86
    payload as well, so it only makes sense together with an x86 vkd3d
    bump.

  `hybrid_patchable` matters as soon as x64 code hooks the D3D12 vtables:
  OptiScaler, overlays and LeviLamina mods.
- **DXVK:** the engine takes DXVK 3.0.1 from the release tarball, which has
  only x64 and x32. On ARM, DXVK must be built from the `v3.0.1` tag with
  an arm64ec cross file into `dxvk/aarch64-windows`, as Proton does. 3.0.1
  already has ARM64EC conditionals (`util_bit.h`). Hot-patch support came
  later, in 3.1. Proton picks DXVK's `dxgi` for D3D12 titles, so this is
  needed even though Minecraft renders with D3D12.
- **dxvk-nvapi:** optional, only for NVIDIA aarch64 machines (DGX
  Spark/GB10, Jetson Thor) and DLSS through OptiScaler.

### 1.4 Base and packaging (`build-engine-arm64` job)

`package-engine.sh` overlays our prefix onto the GDK-Proton10-32 base. That
base is x86_64 only, and Weather-OS publishes no aarch64 build. Options:

- **A (recommended):** pin `GE-Proton11-<n>-aarch64.tar.gz`, for which
  GE-Proton publishes a sha512sum, as the ARM base. Port the GDK-Proton
  changes onto its proton script. In ours, the GDK DLL list (lines
  ~1210–1250 of the shipped `proton`) copies `xgameruntime.dll`,
  `xgameruntime.dll.threading`, libcurl, OpenSSL and the rest from a
  hard-coded `wine/x86_64-windows/`. On ARM:
  - `xgameruntime.dll`, `windows.ui.core.textinput.dll` and
    `windows.devices.enumeration.dll` are Wine builtins, so they must come
    from `aarch64-windows` (ARM64EC/ARM64X).
  - `xgameruntime.dll.threading` is a genuine MSVC x64 DLL (4.5 MB,
    machine `0x8664`, no Wine builtin marker), and so are the curl and
    OpenSSL DLLs. They stay x64 and are copied as they are.

  The packager then gets an aarch64 branch:
  - It also checks that the overlay is complete. The #180 lesson applies
    twice as hard here, since any module we fail to build silently survives
    from a different Wine.
  - It drops the base's own FEX in favour of ours, or pins it.
  - It verifies that `default_pfx_arm64` registers `libarm64ecfex.dll`
    under `HKLM\Software\Microsoft\Wow64\amd64`.
- **B:** build the whole Proton tree from source with `TARGET_ARCH=arm64`.
  That is reproducible and has no third-party base, but it is a project in
  itself (gstreamer, ffmpeg, vosk and others) and duplicates what GE-Proton
  already ships.

Smoke test: `wine --version` inside the arm64 runtime, then the tests under
"Testing" below.

The output is a second archive,
`GDK-Proton-xuser-<rev>-aarch64.tar.gz`, with its own release asset,
attestation and pin. A fat archive holding both architectures would work
with the proton script, but it doubles every x86 download.

## 2. Architecture-sensitive WineGDK patches

None of the patches is architecture-specific in source. The ARM64EC-specific
risk is at runtime, where ARM64EC code meets x64 code:

| Patch | Area | ARM64EC risk |
|---|---|---|
| 0007, 0008 ntdll loader (mapped fd / path) | Unix side, C | **Medium.** It maps the x64 main image through `virtual_map_module(…, machine)` exactly like `open_dll_file`, so ARM64EC's handling of x64 images applies. It has only ever run where native machine = AMD64. The `STATUS_IMAGE_MACHINE_TYPE_MISMATCH` fallback must be re-checked with native machine = ARM64. It gates every launch, so test it first (CI can, see below). |
| 0001, 0003, 0006, 0009, 0012 xgameruntime | PE, C | **Medium.** ARM64EC entry and exit thunks are generated from the C prototype. On x64 a slightly wrong prototype of a reverse-engineered GDK interface (`provider.idl`, `xgame.idl`) is harmless. Under ARM64EC, float arguments, struct-by-value and variadics marshal through the thunk, and a wrong signature corrupts arguments. 0006 also calls the native x64 threading sidecar through `QueryApiImpl` and caches its `IXThreadingImpl`, so every XAsync dispatch crosses x64↔EC. That works by design, but it sits on a hot path and stays emulated. |
| 0004 combase IContextCallback | PE, C | Low. Cross-apartment calls go through rpcrt4/NDR, which upstream already supports on ARM64EC. Re-test the picker and the WinRT continuations. |
| 0002, 0010 windows.storage pickers | PE, C | Low. Pure COM and WinRT. Same prototype caveat as above. |
| 0005 winex11, 0011 winewayland | Unix side, C | Low. 0011 exists because bullseye's libwayland has no `axis_value120`. A bookworm build has it (libwayland 1.21), so keep the patch but re-check its reasoning: it still binds no newer than the listener handles. |
| 0013 kernelbase PrivateUsage | PE, C | None. |

The historical x86 byte patches on the game image ("Patch 3", "Patch 7")
were deleted by native5/0001 and are not a concern.

## 3. What the launcher would pick per architecture

| Concern | Today | aarch64 with the ARM64EC engine |
|---|---|---|
| Engine pins (`bol/config.py`) | `WINEGDK_BUILD_REV`, `WINEGDK_ARCHIVE_SHA256`, `WINEGDK_PREFIX_SHA256` | A second set keyed on `hostarch.machine()`, for example `WINEGDK_ARM64_BUILD_REV`. `_verify_engine_archive()` and the engine lock choose the set. Engine dir `GDK-Proton-xuser` stays per machine. |
| Engine file checks (`bol/vkd3d.py`) | `ARCHES = (x86_64-windows, i386-windows)`, `REQUIRED_CRITICAL_FILE_PATHS` and variant hashes under `x86_64-windows` | `aarch64-windows` + `i386-windows`, with their own variant hash table. The ext-dgc and nv-dgc slot swap works per arch dir. |
| `bol/proton.py: patch_proton` | x86-64 byte patches on `x86_64-windows/combase.dll` (`RoOriginateErrorW` → `xor eax,eax; ret`) and the ntdll raise funnel | **Gated (done).** With `relax=True` it "stubbed anyway" over any prologue, and x86 bytes written into ARM64 code is a guaranteed crash. It now checks `bol.pe.code_machine()` first and skips ARM64EC, ARM64 and ARM64X DLLs, or a Proton that has only `aarch64-windows`. Still to do: move the `RoOriginateErrorW` behaviour into a WineGDK source patch, which is better for x86 too, since an ARM engine would otherwise run without it. The ntdll side already recognises the source-level `NtQueryWnfStateData`. |
| Game-side patches (`bol/fixups.py`) | libHttpClient `ja` NOP, HBUI JS, PE stack reserve | Unchanged: they patch x64 game files, which FEX runs as patched. |
| Container (`proton_umu_cmd`) | umu 1.4.3 + SLR 3 (sniper), toolmanifest appid 1628350 | SLR 4 arm64 (Proton ARM64's appid 4185400) once umu supports it (umu#716). Until then, a direct `proton run` outside the container. That needs the CA bundle the multiplayer fix depends on, so `SSL_CERT_FILE` or the XCurl CA shim must be set explicitly. |
| `bol/hostarch.py` | aarch64 needs an x86-64 binfmt emulator for everything | The engine needs **no** binfmt entry. FEX lives in the prefix. xodus-cli and the WebKitGTK runtime still do until they get aarch64 builds, so `problem()` keeps requiring binfmt for them. The muvm relaunch on 16K/64K pages stays. Add an override (`BOL_ENGINE_ARCH=x86_64`) that keeps today's all-FEX path as a fallback. |
| xodus-cli, WebKitGTK runtime | x86_64 builds | Phase 2: `build-xodus.yml` on an arm runner. It is Rust, and the trixie snapshot has arm64 packages. The webview closure script's `x86_64-linux-gnu` paths need parametrising. Not on the frame-time path, so it can wait. |
| GPU checks (`gpus.py`, `dgc.py`, `gpu_safety.py`) | NVIDIA/AMD/Intel | Add Asahi (Honeykrisp), Adreno (turnip), Mali (panvk) and NVIDIA aarch64. Most have no `VK_EXT_device_generated_commands`, so the ext/nv-dgc choice falls back. The `force_raw_va_cbv` requirement must be re-measured per driver. |
| `doctor` | cpu line | Report the engine architecture and whether FEX runs in-process or through binfmt. |

## 4. Size of the work

These are rough estimates for one person, plus CI wait. Each Wine build is
an hour or more on 4 vCPU.

| Step | Estimate |
|---|---|
| `build-winegdk-arm64.yml` (bookworm arm64, llvm-mingw pin, path checks, ceiling 2.36) | 2–3 days |
| `build-fex.yml` (pinned tag, reproducible DLLs) | 1 day |
| vkd3d-proton arm64x (backport onto `3b10bd7`, both DGC slots, OUTPUT-SHA256SUMS) | 2–4 days, the largest unknown |
| DXVK 3.0.1 arm64ec from source | 1 day |
| ARM base (GE-Proton11 aarch64) + proton-script port + `package-engine.sh` aarch64 branch + completeness check | 3–4 days |
| ARM smoke tests on the arm runner | 1–2 days |
| Launcher: per-arch pins, vkd3d paths, `patch_proton` gating + `RoOriginateErrorW` source patch, container path, hostarch, doctor, tests | 3–4 days |
| Phase 2: xodus-cli + webview aarch64 | 2–3 days |

That is about **3 to 4 weeks** before a first candidate, not counting
fixing what players report on real hardware. After that, every engine bump
(`nativeNN`) needs two CI builds and two pins, which doubles the release
gate permanently.

## 5. Testing without ARM hardware

The `ubuntu-24.04-arm` runner is an ARM64 machine with 4 KiB pages, so CI
can go well beyond `wine --version`:

1. `wineboot` a prefix and run an x64 console test program through
   `libarm64ecfex.dll`. This proves the emulation interface is wired.
2. Start that program through `WINE_DLL_FILE_MAP` from a memfd. This
   exercises patches 0007 and 0008 with native machine = ARM64.
3. From x64 code, call `QueryApiImpl` for XThreading and XUser and run an
   XAsync round trip. This tests the x64↔EC thunks across xgameruntime and
   the native sidecar.
4. Create a D3D12 device and draw a frame with vkd3d-proton on Mesa
   lavapipe (aarch64). This proves the arm64x graphics stack.

What CI cannot cover: a real GPU driver, the signed-in game, and
performance. Those need players on ARM hardware (the #250 reporter, Asahi
users) running a candidate engine that is clearly marked experimental.

## 6. Risks

- **No ARM GPU here.** The first real run is a player's. Ship it behind an
  opt-in, and keep the all-FEX x86 engine as the default fallback until
  reports come in.
- **Container story in flux.** umu has no arm64 SLR 4 support yet, and
  sniper-arm64 is being retired. Running outside the container brings back
  host-library and CA-bundle problems the runtime currently hides.
- **Prototype-exact thunks.** WineGDK's reverse-engineered GDK interfaces
  have only ever run on x64, where mismatched signatures are forgiven.
  ARM64EC will expose any that are wrong, with symptoms like garbage
  arguments rather than clean failures.
- **Still emulated where it hurts.** The game, Microsoft's threading
  sidecar, libHttpClient, XCurl and OpenSSL are x64. The gain is the
  graphics stack, the driver's shader compiler and Wine's core, not the
  game's own CPU time. Memory-ordering (TSO) emulation costs extra on CPUs
  without hardware TSO, and FEX's TSO settings will need tuning per title.
- **vkd3d rebase.** The DGC payload is custom. Porting it to arm64x, or
  rebasing it, may need several reproducibility rounds.
- **Overlay mismatches (#180).** Overlaying Wine 11.1 onto GE-Proton 11's
  Wine needs a strict completeness check. Otherwise base modules leak in
  silently.
- **Runner limits.** 14 GB disk and 4 vCPU per job. Builds may need
  splitting or disk cleanup. The 6 h job limit should hold.
- **Who benefits.** Asahi still runs inside muvm, which keeps its overhead.
  4 KiB-page ARM64 hosts gain the most: Snapdragon X laptops, RK3588 boards,
  NVIDIA GB10 and Ampere workstations. The Raspberry Pi 5's V3D lacks
  features vkd3d-proton needs.
- **Maintenance.** Two engines, two pin sets, two smoke matrices, for every
  release, permanently.

## Sources

- Wine 10.0 release notes (ARM64EC parity, x86-64 emulation interface,
  4 KiB page requirement):
  https://list.winehq.org/mailman3/hyperkitty/list/wine-announce@winehq.org/2025/1
- FEX ARM64EC notes: https://wiki.fex-emu.com/index.php/Development:ARM64EC
- Proton ARM64 build recipe: `Makefile.in` and `toolmanifest_arm64.vdf` in
  https://github.com/ValveSoftware/Proton (master, 2026-11-11)
- vkd3d-proton `build-arm64x.txt`: https://github.com/HansKristian-Work/vkd3d-proton
- GitHub arm64 runners GA for public repositories:
  https://github.blog/changelog/2025-08-07-arm64-hosted-runners-for-public-repositories-are-now-generally-available/
- umu-launcher arm64 issues #716, #673, #361:
  https://github.com/Open-Wine-Components/umu-launcher/issues
- llvm-mingw releases: https://github.com/mstorsjo/llvm-mingw/releases
