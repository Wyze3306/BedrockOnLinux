# Vendored Linux UAPI headers

## `ntsync.h`

Kernel user-space API for the NT synchronization primitive driver
(`/dev/ntsync`), used by Wine's in-process synchronization backend
(`server/inproc_sync.c` and `dlls/ntdll/unix/sync.c`).

* Upstream path: `include/uapi/linux/ntsync.h`
* Upstream source: https://github.com/torvalds/linux
* SHA-256: `006437ee52a3e04f921df77081eb5c21c44c71f598b10ac534c6ef9e78296262`
* License: `GPL-2.0 WITH Linux-syscall-note` (the syscall note explicitly
  permits use by non-GPL user-space, which is how every libc ships it).

The file is byte-identical from the v6.14 release — the first kernel with the
complete ntsync API — through current mainline; it is a frozen ioctl ABI, so
vendoring one copy is safe and is verified by hash at build time.

### `HAVE_LINUX_NTSYNC_H` is not the thing to check

Kernels 6.10 to 6.13 shipped a *preview* UAPI with only two ioctls
(`NTSYNC_IOC_CREATE_SEM`, `NTSYNC_IOC_SEM_POST`). It is enough for
`configure` to define `HAVE_LINUX_NTSYNC_H`, but Wine gates the backend on
`NTSYNC_IOC_EVENT_READ`, so the build still compiles out to the stubs. Debian
13's `linux-libc-dev` 6.12 is exactly this case. Verified with two builds of
the same WineGDK source:

| header on the include path | `HAVE_LINUX_NTSYNC_H` | `/dev/ntsync` in `wineserver` |
| --- | --- | --- |
| this vendored v6.14 copy | 1 | present — backend compiled in |
| Debian 13 system 6.12 | 1 | **missing** — still stubbed |

That is why the build scripts replace an incomplete header instead of trusting
its presence, refuse a *complete* header that is not this one, and assert on
the built `wineserver` afterwards. It is also why building the engine yourself
on a distribution older than kernel 6.14 headers silently produced a stubbed
engine.

### Why it is vendored

The engine is built in a Debian 11 (Bullseye) container to hold the
`GLIBC_2.31` ABI ceiling. Bullseye's `linux-libc-dev` is kernel 5.10 and has
no `linux/ntsync.h`, so Wine's `configure` left `HAVE_LINUX_NTSYNC_H`
undefined and compiled the whole in-process synchronization backend out to
the stubs at the `#else` of `NTSYNC_IOC_EVENT_READ`. Every Win32 wait,
`SetEvent`, mutex and semaphore then fell back to a wineserver round-trip.
Because the wineserver serialises those requests, Minecraft's worker threads
queued behind one another and the game behaved as if it were single-threaded:
low server TPS, GPU starvation, and chunk-generation stalls (issues #63, #139,
#143, #148, #150).

Installing this header into the build container's `/usr/include/linux/` is
enough for `configure` to detect it. It changes nothing at runtime beyond
letting the engine use `/dev/ntsync` when the host kernel provides it; on a
kernel without ntsync the device simply fails to open and Wine falls back to
the wineserver path exactly as before.

Both engine build scripts verify this file's SHA-256 before installing it,
then assert after `make install` that the resulting `wineserver` really
contains the `/dev/ntsync` code path, so the feature can never be silently
dropped again.
