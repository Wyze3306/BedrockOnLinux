# Engine patches waiting for the next engine release

A patch in this folder is complete and reviewed, but no engine build applies
it yet. Moving one into `third_party/winegdk-native5/` changes the engine:
both builders must apply it, `SOURCE-SHA256SUMS` must pin the files it
touches, the packager must list it, and `WINEGDK_SOURCE_MANIFEST_SHA256` and
`WINEGDK_BUILD_REV` in `bol/config.py` move to the new engine. That is a
release decision, so it is taken when an engine is published, together with
whatever other patches are waiting, not by the change that wrote the patch.
`tests/test_winegdk_source_delta.py` keeps the two states apart: every patch
directly in `winegdk-native5/` must be wired everywhere, and nothing here is.

## 0017: record the title's XGameEventWrite calls (issue #152)

Applies on top of `0013` (checked against the exported `75637b67` tree with
`0002` to `0013` applied). It touches only `dlls/xgameruntime`, so it shares
no file with the ntdll and server patches `0014` to `0016` that the 3.0.0
branch carries, and applies after them unchanged. It serves the `XGameEvent` runtime class,
`{bbfbdcc7-bfe7-409b-a5ca-edf054960b4d}`, which `QueryApiImpl` refused with
`E_NOTIMPL`, so XSAPI dropped every in-game event Minecraft wrote, and with
them every achievement. Each event is appended as one JSON line to the file
named by `BOL_GAME_EVENT_LOG`; `bol/gameevents.py` sets that variable and
uploads the events to the Xbox Live event collector while the game runs.
Without the patch the launcher half is inert: the file stays empty.

Measured on 2026-11-12 with Minecraft 1.26.60.5, the shipped engine plus this
patch built locally, and the launcher of the same branch, in a survival world
without cheats:

* the game writes its events in batches, every few minutes and when it is
  paused, not one by one: `PlayerTravelled`, `BlockBroken`, `ItemAcquired`,
  `ItemCrafted`, `BlockPlaced`, `EndOfDay`, `AwardAchievement`, each with
  `"Cheevos": 1`;
* the launcher sent all 56 of the session's events and the collector refused
  none;
* Xbox Live graded them within a minute: the account's `BlockBrokenTotal` and
  `DistanceTravelled` stats grew by exactly what the forwarded events carried,
  and crafting a wooden hoe unlocked "Time to Farm!" and "MOAR Tools", one
  second after the batch that carried the `ItemCrafted` event was sent.

Wiring it in, when an engine is next released:

* `scripts/build-winegdk-bullseye.sh` and `scripts/build-winegdk-container.sh`:
  a `VENDORED_GAME_EVENT_PATCH` and its SHA-256, applied after `0016`;
* `scripts/package-engine.sh`: the file in both provenance lists;
* `SOURCE-SHA256SUMS`: the new hashes of `dlls/xgameruntime/main.c`,
  `private.h` and `Makefile.in`, plus
  `dlls/xgameruntime/GDKComponent/System/XGameEvent.c`; then the new hash of
  that file in both builders and in `WINEGDK_SOURCE_MANIFEST_SHA256`;
* `PINNED_SOURCE_FILES` and the patch list in
  `tests/test_winegdk_source_delta.py`.
