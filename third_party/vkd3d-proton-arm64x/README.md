# vkd3d-proton as ARM64X, for the experimental ARM64EC engine (#250)

The experimental `arm64ec-native*` engines (`docs/ARM64EC.md`) run Minecraft,
an x64 program, as ARM64EC: FEX translates only the game's own code, and every
call it makes into Windows or D3D12 lands in native ARM code. This directory is
the provenance of that engine's D3D12 layer.

The source is the x86 payload's, unchanged (`../vkd3d-proton-universal`):
vkd3d-proton v3.0.1 (`3b10bd7a7ec6`), NV-DGC restored by reverting
`76c11d2e2b90`, then the occluded frame-latency (#50) and frame-limiter (#202)
fixes. `scripts/build-vkd3d-arm64x.sh` checks each of them against the x86
lock before it builds.

What differs is the toolchain and one file:

- `build-arm64x.txt` is upstream's ARM64X cross file (`d5f57cebf2`,
  2026-09-22). It came after v3.0.1, which needs nothing else for ARM64X:
  the rdtsc guard for ARM64EC has been upstream since 2024.
- The build runs in Valve's steamrt4 arm64 SDK image, pinned by digest in
  `provenance.env`, on an aarch64 host. llvm-mingw's `-marm64x` links one DLL
  holding an ARM64 and an ARM64EC half, so the same file serves the emulated
  game and native ARM64 callers.
- Upstream's later `hybrid_patchable` change (`20c0ab2095`), for titles that
  hook D3D12 vtables, is not taken: Minecraft does not.

`OUTPUT-SHA256SUMS` pins the two DLLs once a CI build has produced them; the
release is named after that file's hash (`scripts/vkd3d-release-tag.sh
aarch64`). Nothing in this directory publishes anything.
