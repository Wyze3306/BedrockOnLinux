# WineGDK native Xbox and WinAppSDK file-picker delta

This engine targets WineGDK commit
`75637b674e1f191e65753663c4c0c32bea05ba6e`, on top of the reviewed r12
commit `432f414b251cc6d668404825a1d0f05eca807a70`.

The cumulative delta retains the native GDK identity, XUser, Xbox context and
Realms implementation developed in the earlier native delta. It also implements the
WinAppSDK 1.8 `Microsoft.Windows.Storage.Pickers.FileOpenPicker` runtime class
inside Wine's `windows.storage.dll` for both PE architectures.

The picker implementation provides the exact WinRT ABI used by Minecraft,
including the `Microsoft.UI.WindowId` factory, validated file-type filters,
single- and multiple-file selection, immutable result vectors, cancellation,
and `PickFileResult.Path`.
It delegates the desktop UI to Wine's `IFileOpenDialog`; closing the chooser is
reported as a successful null result instead of raising an exception. Async
results and completion delegates have explicit ownership rules to avoid the
use-after-free and reference-cycle hazards found in earlier prototypes.
The follow-up `0002` patch routes single-file selection through
`GetOpenFileNameW`. Wine's Common Item Dialog can destroy itself before
initialization inside Minecraft; the legacy dialog avoids that failing path
without changing the WinRT async result ABI. The modal chooser runs on the
calling apartment and returns an already-completed operation, so C++/WinRT
installs and invokes its completion handler without a cross-apartment
thread-pool callback. Multiple-file selection remains asynchronous on
`IFileOpenDialog`, with its private STA kept alive through completion.
Minecraft then converts the returned path with
`Windows.Storage.StorageFile.GetFileFromPathAsync`. The same patch implements
that missing static operation and the `IStorageItem` name and path properties
used by the game, without adding unused stream support.

The former Minecraft process-memory patcher remains removed. Online state
comes from XGame, XUser and XSAPI, while world and skin imports use the native
WinRT picker rather than a package-identity shim.

The `0003` patch keeps Minecraft's Windows Achievements URL and title ID intact.
For the exact `achievements.xboxlive.com` host it selects a separately minted
user-only XSTS token and its matching user hash, with a safe in-Wine user-only
fallback. Cached and live user hashes are accepted only as non-zero decimal
`uint64` values. The in-memory fallback cache is synchronized, refreshed on
demand and replaced after a forced refresh, while Android SISU tokens remain in
use for the services that require title binding, including PlayFab, multiplayer,
Realms and licensing.

The `0004` patch backports Wine's complete `IContextCallback` implementation
and its required COM interfaces for other WinRT continuations that cross
apartments.

The `0005` patch keeps the X11 client surface in client coordinates instead
of offsetting it by the Win32 visible-window frame. This prevents fullscreen
render surfaces from being placed inside their X11 parent, which otherwise
exposes the parent background along the top and left edges. It is a narrow
semantic backport of the client-surface geometry behavior introduced by
upstream Wine commit `b868cd31d6b`.

The `0006` patch stops the XStore composite from fabricating store answers.
Nothing implements the Microsoft Store behind it, yet its game-license query
completed with a hard-coded license and its associated-products query completed
with a zeroed result, which reaches the caller as `S_OK` plus a null
product-query handle. A signed-in Minecraft reads that as a store that answered,
marks its offer repository loaded and then walks the containers the enumeration
was supposed to fill, faulting on the first null one while the main menu is
still assembling (issue #171). Both queries now report
`E_GAMESTORE_NETWORK_ERROR`, which needs no task queue and leaves the caller's
async block untouched; the title retries a few times and continues with the
store disabled. The same patch teaches `XAsync` that a task queue this DLL does
not own may still belong to the native GDK threading sidecar, where
`QueryApiImpl` sends `CLSID_XThreadingImpl`: such handles are duplicated,
driven and closed through the implementation that owns them instead of being
silently replaced by a thread-pool queue of our own.

The `0007` patch lets the loader map the main image straight from a file
descriptor. Minecraft is no longer installed from a repackaged copy of the
game: it is downloaded from the Microsoft Store with the user's own account,
and the package keeps the segments it flags `KEEP_ENCRYPTED_ON_DISK` — the game
executable on GDK titles — encrypted at rest, exactly as on Windows. What sits
on disk is not a loadable PE, so there is nothing for `open_dll_file` to open.
The launcher decrypts the executable into anonymous memory at launch and passes
the descriptor through `WINE_DLL_FILE_MAP`, whose `<fd>:<nt-name>` entries
`open_main_image` now consults before touching the filesystem; the plaintext
never reaches a file. Without a match, or without the variable, the loader takes
its usual path unchanged. This is a port of `xodus-gaming/wine` commit
`183d5d90b62a`, using `RtlUnicodeToUTF8N` in place of the `locale_private.h`
helpers this Wine base predates, and carrying only the loader change: the
upstream commit's standalone C launcher is replaced by the launcher's own
Python implementation in `bol/launch.py`.

The `0008` patch lets a `WINE_DLL_FILE_MAP` entry name a file instead of a
descriptor, chosen by a leading `/`. `0007` alone is enough on a bare Wine, but
not through a container: Minecraft runs inside the Steam Linux Runtime, where a
descriptor number means nothing, and Wine died on `sendmsg: Bad file
descriptor` before drawing a frame. The loader now opens such an entry, unlinks
it immediately, and maps the descriptor it is left holding — so the decrypted
image has no name from the first instant and is reachable only through that
process, exactly as an anonymous mapping would be. The launcher stages it on
`/dev/shm`, which is RAM, so nothing reaches durable storage either. Descriptor
entries are unchanged, and a map with no matching entry still falls through to
the ordinary on-disk path.

The `0009` patch lets `XSystem` answer every interface revision the GDK
libraries Minecraft ships ask it for. The GDK serves one XSystem object behind
several interface IDs, and each library queries the one its headers were built
with: `libHttpClient.GDK.dll` asks for `1861cf2e-e18b-4834-a9f5-b4a4e6efb4cf`,
`PlayFabMultiplayerGDK.dll` for `6fd71f09-7513-49f0-89bc-bfaf5df6f852`, and
`Microsoft.Xbox.Services.GDK.C.Thunks.dll`, new with Minecraft 1.26.50, for
`67ce4bfc-b1d1-4ac7-bc3a-cb9219a97a85`. `QueryInterface` accepted only the
`dadc2895-34b0-4ef5-a83e-45114d629b80` revision `provider.idl` declares, so all
three were refused with `E_NOINTERFACE`. The first two carry on without it; the
Xbox Live services thunks do not, and on 1.26.50 a signed-in player got a game
that was half offline (issue #266). Measured on 1.26.51.1: Social showed no
friends, the Realms tab spun forever with its buttons disabled, and featured
servers kept a disabled Play button; with this patch Social counts the friends
online, Realms offers its invitations and featured servers can be joined. Each of
the three libraries calls one method through the interface it obtains, vtable
slot 4 — `XSystemGetXboxLiveSandboxId` with the size, buffer and used-size
arguments the flattened vtable already implements — so the one vtable answers
them all. The patch also makes `XSystemHandleTrack`, which registers a
handle-lifetime debugging callback, report success instead of `E_NOTIMPL`; no
callback is ever delivered.

`SOURCE-SHA256SUMS` pins every source file changed by the cumulative r12 to
native5 delta and its follow-ups. The Bullseye builder applies the reviewed r12
and native patches when the target commit is unavailable, always applies `0002`
through `0009`, then verifies the complete resulting source tree.
