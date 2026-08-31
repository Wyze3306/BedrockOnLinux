# Changelog

## 2.2.5 — 2026-08-31

### Added

- **Settings ▸ Versions: see every Minecraft build you have downloaded, and
  remove the ones you are done with**
  ([#214](https://github.com/Wyze3306/BedrockOnLinux/issues/214)). Each build
  is downloaded into a folder of its own, which is what makes going back to
  one instant — and what made them pile up, because nothing ever removed one
  and nothing ever said they were still there. Three versions tried out is
  three copies of a 2.5 GB game, and the only way back was `rm -rf` on a path
  the launcher never showed. The new tab lists every build on disk with what
  it weighs, marks the one in use, the one that lost its package and the one
  installed before the move to the Microsoft Store, and removes any of them on
  request. Nothing you made is in those folders: worlds, settings,
  screenshots, skins and packs belong to the profile, so a build can be
  removed and downloaded again without touching a single world — which the tab
  says in as many words, because "delete this version" reads like something
  else entirely. A finished download now also names what the builds beside it
  are taking up, instead of leaving the total to be discovered on a full disk.
  On the command line: `bedrock-on-linux versions --installed` lists them and
  `bedrock-on-linux versions --remove <build>` removes one.

- **PLAY says so when nothing is signed in for online play** (#240). The
  launcher asks for two Microsoft sign-ins, and only one of them ever asks for
  itself: the account that downloads Minecraft is offered at PLAY, the moment
  it is needed. The account that plays online never was, so its absence
  surfaced only inside the game, as Realms, servers and friends quietly
  missing — with nothing in the launcher having mentioned a sign-in. Pressing
  PLAY without it now warns first, says what offline mode costs, and offers
  the sign-in on the spot; taking it resumes the launch on its own rather than
  asking you to press PLAY again. Offline is still a legitimate way to play,
  so it is a warning and not a refusal: *Play offline* carries straight on,
  the warning can be turned off from itself, and Settings ▸ Accounts puts it
  back. `bedrock-on-linux play` prints the same warning rather than launching
  in silence.

### Fixed

- **Your Xbox friends can see you again while you play**
  ([#238](https://github.com/Wyze3306/BedrockOnLinux/issues/238),
  [#243](https://github.com/Wyze3306/BedrockOnLinux/issues/243),
  [#244](https://github.com/Wyze3306/BedrockOnLinux/issues/244)). The
  dressing room said "Offline" under your own gamertag, the social tab said
  it too, and no friend could invite you or be joined. It was not the game
  misreading anything: asked straight out, Xbox Live agreed. Minutes after a
  full launch the presence service still answered `state: Offline`, last seen
  twelve days earlier — nothing had ever told it Minecraft was running. That
  write belongs to the Xbox services layer the engine cannot bring up under
  Wine, so it never happened, and every reader downstream of it was correctly
  reporting an account that really was offline. The launcher now sends the
  heartbeat itself, for exactly as long as the game runs, using the Xbox Live
  token it already holds for Friends and Social; Xbox names the title, so it
  can say only that Minecraft is being played, and by whom. Stopping the game
  takes it down again, and Xbox drops it by itself a few minutes later if the
  launcher is killed, so nothing can be left permanently in-game. It is on by
  default and Settings ▸ Accounts turns it off.

- **`bedrock-on-linux doctor --network` now answers "why can't I join my
  friend's world?"** (#243, #244). The reports were unanswerable: the account
  being invisible, the friends list not loading, and the friend simply not
  being in a joinable world all look identical from the outside, and every
  host in the report resolved and handshook perfectly through all three. The
  report now asks Xbox Live directly and prints what it says — how this
  account looks to other people, whether its friends list reads at all, and
  how many of those friends are in a multiplayer session at this moment. None
  of the three fail the report: an account nobody can see is a real problem
  but not a broken host, and an evening with nobody in a world is not a fault
  at all. `doctor` also gained an offline `xbox presence` line saying whether
  the next launch will publish anything.

- **Downloading Minecraft no longer fails on the downloader's own package
  cache** ([#241](https://github.com/Wyze3306/BedrockOnLinux/issues/241),
  [#242](https://github.com/Wyze3306/BedrockOnLinux/issues/242)). The
  download read back shorter than what had been written to it, gave up, and
  the attempt after it installed nothing at all without saying why — on a disk
  with 75 GiB free in one report and 700 GiB in the other, so the room the
  message ruled out was never the problem. The downloader streams the package
  through a cache file beside the game and reads that cache back through a
  second handle to work out the package layout, and it counted the bytes the
  runtime had *accepted* rather than the bytes that had reached the file. A
  write is accepted the moment it is queued on a background thread, so a read
  sent to the same pool could overtake it, find the file short and call the
  package corrupt. Retrying could not order those two operations — it only
  bought another go at the same coin toss, and the go that lost it while
  listing the package files rather than while reading its header exited
  reporting success with no game installed. The fix has been carried in this
  repository since [#217](https://github.com/Wyze3306/BedrockOnLinux/issues/217)
  and reached nobody: a patched downloader is published under a revision of
  its own, and the launcher still asked for the unpatched one. It now asks for
  the fixed build, and continuous integration refuses a patch that is not
  named by the pin, so this cannot happen again quietly.

- **The launcher no longer forgets which build you play, and downloads
  another one**
  ([#214](https://github.com/Wyze3306/BedrockOnLinux/issues/214),
  [#220](https://github.com/Wyze3306/BedrockOnLinux/issues/220),
  [#247](https://github.com/Wyze3306/BedrockOnLinux/issues/247),
  [#248](https://github.com/Wyze3306/BedrockOnLinux/issues/248)).
  The version picker shows a shortened label — `26.44` for build 1.26.44.3 —
  and that label was what got saved as the selection. Nothing could read it
  back: it names no build the installer can find, so setup reported the choice
  as no longer listed and installed the newest build instead. Picking 26.45
  and landing in 26.44 is the same fault seen from the front, and so is a
  launcher that starts every session by downloading something. Playing an
  older version, closing the launcher and opening it again therefore landed
  on the newest one with nothing having said so, and the next PLAY downloaded
  2.5 GB of a game that was never asked for. The selection is now the build itself,
  labels written by earlier releases are still understood, and the picker
  comes back to what you last played.

- **A Microsoft sign-in stuck on "Please wait" no longer takes the launcher
  with it**
  ([#214](https://github.com/Wyze3306/BedrockOnLinux/issues/214)). The account
  that downloads Minecraft is linked through Microsoft's own page, in a
  webview `xodus-cli` owns. When that page stops making progress it prints
  nothing and the process never exits — and the launcher waited on it for
  ever, with `capture_output` holding every line it might have printed until
  an exit that was not coming. What the player saw was worse than the stall:
  PLAY still ran the whole setup, got as far as the download, found no account
  and stopped — three times over in the report — and every later *Sign in* was
  a button that did nothing at all, because the flag saying a sign-in was in
  flight had no way left to be cleared. The page is not stuck by accident. A
  token exchange that faults hands back a second address to open, and the
  downloader opened it with no headers at all — none of the nine telling
  `login.live.com` it is talking to Windows' own token broker rather than to
  a browser. Without them Microsoft serves the ordinary consumer interrupt,
  which is the "Please wait" and the greyed *OK*: a page that never reaches
  the one address the sign-in is waiting to see, so it waits for ever. The
  second leg of the sign-in is opened like the first now
  (`third_party/xodus/patches/`, on its way upstream), and the launcher ships
  the downloader built with it. The rest of this stands whatever the page
  does: the sign-in is something the launcher holds rather than waits on. Its window can be closed from the
  account menu or from Settings, which signals the whole webview process
  group; PLAY says the window is open instead of starting a download that
  cannot work; a sign-in that has been up a long time says so; and a second
  one is never silently swallowed. Whatever the sign-in prints is streamed as
  it arrives and kept in `logs/store-login.log`, so a stall that reaches
  nobody's terminal still leaves something to read. The page's own cache and
  storage — never the tokens, which live in the keyring — are cleared before
  each sign-in and pinned inside the launcher's Xodus home, so a flow
  abandoned half-way is not resumed into by the next one, and nothing of it
  lands in `~/.local/share` again. Closing the window is reported as the
  ordinary change of mind it is, rather than as a failure of the launcher.

- **A download that fails now leaves something to read**
  ([#242](https://github.com/Wyze3306/BedrockOnLinux/issues/242)). The same
  install was reported three times over and every line anyone had was the
  launcher's own: *the Minecraft download installed no game and printed no
  reason for it*. Nothing was ever written down — the launcher kept the last
  forty lines the downloader printed for the error message and dropped the
  rest — and what it kept was mostly not output at all. The downloader draws
  its progress bars by redrawing one line, padded to the width the terminal
  reports, which for the launcher's terminal was none: a single redraw of
  three bars arrived here as 16 MiB of padding around 438 characters of bar.
  The sentence saying what actually went wrong could arrive in the middle of
  one of those frames, written from another thread. Every attempt now goes to
  `logs/store-download.log` the way a sign-in already goes to
  `store-login.log`, the frames are recognised and taken out so what is left
  is the message, and a downloader that has already exited is read to the end
  — a panic is written exactly there.

- **Closing the launcher while it was busy in the background could end the
  process on a crash instead of an exit.** Settings ▸ Versions adds up what
  each installed build weighs, and both changelogs are fetched over the
  network; all three run on threads that belong to the window. Closing it
  while one was still going destroyed that thread mid-run, which Qt answers
  by aborting the process — no data lost, the launcher was on its way out,
  but it left a crash where a clean exit belonged. The work is now let go of
  rather than killed or waited for: the window closes at once, and the thread
  finishes on its own. Waiting would have been the other answer and the wrong
  one, since two of the three are network reads.

- **The Microsoft sign-in window no longer dies where the desktop has
  accessibility running**
  ([#236](https://github.com/Wyze3306/BedrockOnLinux/issues/236)). Signing in
  to download Minecraft went blank part-way through — reported on Ubuntu with
  KDE, and recognised straight away as something other WebKitGTK windows do on
  Arch and NixOS too — leaving nothing to go on but a `WebKitWebProcess`
  coredump in the system journal, and a launcher that could only say the
  sign-in had not completed. That page is drawn by a process of its own, and
  WebKitGTK publishes it on the accessibility bus; its AT-SPI text interface
  then maps an attribute run back onto UTF-8 offsets by indexing a table with
  the end of that run, which the code above it allows to point past the end of
  the text it belongs to. A single `GetAttributeRun` landing on such a run
  reads off the end of the array and aborts the process on the spot, taking
  the sign-in with it. Nobody asks for that: the caller is whichever
  accessibility client happens to walk the window, and the fault is still
  present in WebKit's own tree, so there is no version to update to. The
  launcher now keeps that window off the accessibility bus, the same way it
  already keeps it off the DMABUF renderer, and for every `xodus-cli` call
  rather than the sign-in alone. Nothing else about the window changes — it is
  simply no longer readable by assistive technology, which is a real loss
  where it is needed: `BOL_WEBVIEW_A11Y=1` asks for the bridge back, a
  `WEBKIT_A11Y_BUS_ADDRESS` you set yourself still wins, and the game is never
  started with either.

## 2.2.4 — 2026-08-25

### Fixed

- **The in-game sign-in was back, and it has never worked** (#227, #228,
  #229). Two patches keep Minecraft's Play screen usable under Wine. One takes
  down the *You need a Microsoft account* notice that blocks the Servers and
  Realms tabs — it is there because the Xbox social layer never finishes
  starting under Wine, not because the account is missing. The other removes
  the sign-in link inside that notice, which reaches an interactive sign-in
  the engine does not implement and can only ever answer *Failed to log in.
  Error Code: Llama*; the account is linked from the launcher instead. Both
  were anchored on names Minecraft's interface invents afresh every time it is
  built. It was rebuilt, the names changed, and both patches quietly stopped
  matching anything — so setup kept reporting success on an installation where
  the Play screen still said the account was missing and offered a button that
  could only fail. They are anchored on the account fields and on the sign-in
  address now, neither of which a rebuild renames, and a build that moves past
  even those is reported rather than passed over: setup says so, and `doctor`
  gained a `sign-in ui` line telling you whether the installed build still
  carries both.

- **The Minecraft download died on the licence Microsoft issued for it.**
  *The Minecraft download failed: called `Result::unwrap()` on an `Err` value:
  Custom("unknown variant `Trial`, expected one of `Device`, `User`, `Full`,
  `KeyHolder`")* is the downloader refusing the answer to its own licence
  request. Microsoft describes the kind of entitlement a licence was granted
  for — owned outright, tied to a device, time limited — and the downloader
  knew four of those words, so the fifth one ended a download that had already
  fetched the package and asked for the key to open it. Nothing anywhere reads
  that word: the content key travels beside it, in the block the download
  actually needs. It is a licence the account holds, refused over the label on
  the envelope. The downloader now keeps a description it does not recognise
  instead of throwing the licence away, and a licence it genuinely cannot read
  ends the download with a sentence rather than a panic
  (`third_party/xodus/patches/`, on its way upstream). Until that build is
  published the launcher recognises the panic, says what happened in words
  about a licence, and stops asking other mirrors for a package whose licence
  was never theirs to give.

- **The Minecraft download was losing a race against its own cache** (#217,
  #200, #215). *The Minecraft download failed: ok: Header(Io(Custom { kind:
  UnexpectedEof, error: "cache ended before cached_len" }))* reads like a
  corrupt package, and it is nothing of the kind. The downloader streams the
  package through a cache file in the game folder and reads that cache back to
  find out what the package holds — writing through one handle, reading
  through a second — but it counts the bytes it *handed to* the write, not the
  bytes the write put there. Both run on the same pool of threads, so the read
  can overtake the write, find the file short of what the counter claims, and
  take that for a broken download. Measured against the shipped downloader,
  the cache claimed as much as 17 KiB more than the file held, on an idle disk
  with room to spare: nothing to do with the disk, the network or the account,
  which is why it stops some machines every time and others never. The race is
  fixed in the downloader itself now — `third_party/xodus/patches/`, on its
  way upstream — and until that build is published the launcher runs the
  download again when it sees the short read, which is what a race responds
  to. A destination that really has run out of room is told apart by the
  arithmetic and still refused outright.

- **The Minecraft download counted past a hundred percent and kept going**
  (#216). "Downloading Minecraft…  24346886100%" is one number short of a
  whole story: the byte counts crossed into Qt through a signal that carries
  a C++ `int`, and the package is 2.3 GiB. The total wrapped negative, the
  guard meant to keep it positive turned it into 1, and the status line read
  the download out as its own byte count times a hundred — above a bar that
  stayed empty the whole way down, since a progress bar ignores a value
  outside its range rather than clamping it. Both the signal and the bar now
  hold the figures they are given, so the percentage is the download's and
  the bar fills once across it.

- **A build that lost its encrypted package had no way back.** The game
  executable Microsoft ships is ciphertext, decrypted at every launch out of
  the package file beside it, and a folder without that file still holds an
  executable and a manifest — so it counted as installed. The download was
  skipped as unnecessary, every launch died on the missing package, and the
  message about it pointed at "Install / Update", which is a command-line
  verb the launcher window has no tab for. A build that cannot be decrypted
  is now no longer a build that is installed: PLAY downloads it again, and
  the message says so.

- **A Minecraft download that had nowhere to go said so in Rust** (#200).
  Everything the download needs lands in the same folder — the 2.3 GiB package
  it streams through, and the build decrypted out of it — so a disk with a few
  hundred MiB left could not finish it, and neither of the two ways it failed
  named the disk. Refused mid-package, it came back as *The Minecraft download
  failed: ok: Header(Io(Custom { kind: UnexpectedEof, error: "cache ended
  before cached_len" }))*; refused a moment later, as *The Minecraft download
  reported success but installed no game*, which is the downloader printing
  what it measured and exiting successfully anyway. The room is now checked
  before the download starts — the package's own size, asked of the same
  mirror it comes from — and a first install that cannot fit is refused with
  both figures and the folder it needs them in. An update is left alone,
  because only part of a build is fetched and that part cannot be predicted.

- **Failures the downloader printed were replaced with a sentence that said
  nothing.** It exits successfully on most of the paths that end a download
  early, and only its non-zero exits were being read — so an account that does
  not own Minecraft, a Store session that had expired, and a disk with no room
  left all arrived as *reported success but installed no game*, with the
  reason it had just printed thrown away. All of them are now named, whatever
  it exited with, and anything else keeps the line it printed.

- **A package cache left by an attempt that never finished quietly installed
  nothing.** The downloader resumes from it and can conclude there is nothing
  to fetch, leaving a folder with no game in it — and nothing can be resumed
  from that file anyway, because it starts a fresh one every run. It is now
  dropped before the download rather than only after a failure.

- **The AppImage never opened on a system without a zstd runtime** (#205).
  Qt links libzstd.so.1 from libQt6Core — plain compression, none of the
  graphics stack an AppImage genuinely has to borrow from the host — and the
  bundle was still asking for it, so a NixOS session running it through
  appimage-run got a traceback where the launcher should have been:
  *ImportError: libzstd.so.1: cannot open shared object file*. It now ships
  inside the AppImage, beside Qt's own libraries, taken from the same pinned
  Debian 11 package the rest of the build already trusts. The build also
  walks everything the launcher's window loads and refuses to package an
  AppImage that asks the host for anything but the documented X11/OpenGL
  libraries, so the next Qt update cannot reintroduce this quietly.

- **A missing system library now has a name instead of a traceback.** The
  launcher imported the whole Qt stack before it had even read its arguments,
  so one library the host was missing took every command down with it —
  including `doctor`, which exists to explain exactly this, and which called
  the toolkit *OK (GUI)* because the files were all there. Qt is now imported
  when the window is asked for: the failure names the library to install,
  `bedrock-on-linux play` and `bedrock-on-linux doctor` keep working without
  it, and `doctor` reports a toolkit that is installed but cannot load. On a
  portable .pyz or a bare checkout this also restores the first-launch
  install of the GUI toolkit, which the old import order made unreachable.

## 2.2.3 — 2026-08-23

### Added

- **The controller can drive the launcher again**. Navigating the window with
  a gamepad was lost in the PySide6 rewrite: the ring it moved was built on Tk
  internals and was removed with the toolkit, leaving Steam Game Mode and every
  other couch setup back at a window that needs a mouse to get past. It is
  back, written against Qt. A highlight follows the d-pad or the left stick, A
  activates, B goes back, the shoulder buttons change tab, Start plays and the
  right stick scrolls — across the dock, the version picker, the profile and
  account menus, every Settings tab and any dialog that opens over the window.
  Opening Settings or What's New takes the highlight with it, and a list, a tab
  bar or a text field is handed the key press so it navigates the way Qt
  already knows how to. Reading controllers never changed: the same
  `/dev/input` reader that survived the rewrite feeds it, so there is still no
  new dependency and hot-plug still needs no restart. Off with *Settings ▸
  General ▸ Controller*, or `BOL_CONTROLLER=0` for one session.

- **Discord now shows what you are playing.** Start Minecraft and your friends
  see *Playing BedrockOnLinux* on Discord, with the build you are on, how long
  you have been playing, and buttons that lead to the project and to its
  Discord server. That is how a launcher like this one is found in the first
  place — somebody notices a friend playing Bedrock on Linux and asks how — and
  until now a play session said nothing about where the game had come from.
  Nothing has to be installed for it: Discord's local socket is spoken to
  directly, with no library, no account and no network call of the launcher's
  own, and the only things it is ever told are the edition and the version —
  never the account, the worlds, or the server being played on. It ends with
  the game, and **Settings ▸ General ▸ Discord** turns it off. Inside the
  Flatpak, Discord has to be running before the launcher is, because the
  sandbox binds its socket at startup.

### Fixed

- **A fresh install could not fetch the game engine**, on 2.2.0, 2.2.1 and
  2.2.2 alike. The engine archive those releases are pinned against was rebuilt
  in place — same revision name, same file name, same URL — to carry a Wayland
  driver of our own build. Different bytes at an address three published
  releases had already shipped trusting: the launcher checks what it downloads
  against a SHA-256 it carries, found bytes it had never been told about, and
  refused the engine with *Prebuilt engine rejected (engine archive SHA-256
  mismatch)* — on a first install, with nothing there to fall back on. The
  engine now lives under a revision of its own, `wow64-archs-native17`, at its
  own URL, and the archives the older releases point at are back to being left
  alone. A revision names one set of bytes; a rebuild gets a new one rather
  than overwriting a published archive.

- **The version picker now takes the version you highlighted.** Arrowing down
  the list and pressing Enter picked the first row that had survived the filter
  instead of the highlighted one, so keyboard and controller users could ask
  for one build and install another.

## 2.2.2 — 2026-08-22

### Added

- **Both Microsoft sign-ins now live in one menu, top right.** The launcher
  needs two, and until now it never said so: the one for playing online sat at
  the top of the window, the one that downloads and updates Minecraft was
  buried in *Settings ▸ Accounts*, and nothing anywhere explained that they
  were different sessions at all. So a player who had signed in once — and
  been told "Signed in" — could reach ▶ PLAY and be handed "you have to sign
  in", about an account they had never been offered, with nothing in the
  window to click. The pill in the corner now opens a small menu holding both,
  each with what it is for, where it stands, and the one thing you can do
  about it; the dot on the pill only goes green when both are in, and its
  tooltip names them separately. The menu also says once why there are two.
  *Settings ▸ Accounts* still works and stays in step.

- **PLAY offers the download sign-in instead of failing.** Reaching the
  download with no account for it was reported as "Minecraft could not
  start" — true, and useless: nothing was broken and nothing had been
  downloaded, the game simply had no account to be fetched with. It now asks
  for that account, and resumes the launch on its own once it is there rather
  than making you press PLAY again. Signing in to play online also offers the
  download's sign-in straight afterwards, while you are still in the middle of
  signing in, instead of letting you find out at PLAY.

- **A server you can actually join, in the Servers tab.** Bedrock opens that
  tab on the featured servers it sells, and a fresh install has nothing of its
  own underneath, so joining anything meant finding an address elsewhere and
  typing it in by hand — the game offers no way to import one. Linesia
  SkyFaction (`play.linesia.net`, port 19132) is now written into the list the
  game reads at startup, for every signed-in account. The list belongs to you:
  seeding only ever adds, never reorders or removes, and deleting the entry in
  game keeps it deleted.

### Changed

- **The launcher window is now built on Qt instead of Tk**. Everything is
  where it was — the hero screen, the dock with the version picker, the
  Stable/Preview toggle and ▶ PLAY, the sliding Settings and What's New
  panes, the collapsible activity log — but none of it is drawn by
  CustomTkinter any more. What that buys is mostly invisible and mostly
  overdue: dialogs that centre themselves on the right monitor without the
  launcher doing the arithmetic, DPI scaling handled by the toolkit rather
  than by a manual 100/150/200% picker (which is why that setting is gone),
  real toggle switches instead of checkboxes with rounded borders, and a
  window that draws natively under Wayland instead of always going through
  XWayland. Background work — setup, launching, sign-in, imports, DLL
  injection, relocation, self-update — moved onto Qt threads reporting back
  through signals, replacing the hand-rolled polling the Tk build needed to
  avoid drawing from the wrong thread.

- **The launcher now needs Qt's runtime libraries.** The `.deb`, `.rpm`,
  AppImage and Flatpak all bundle the toolkit itself, so there is nothing new
  to install by hand; the distribution packages simply depend on the X and
  OpenGL libraries Qt loads at startup, in place of `python3-tk`. A portable
  `.pyz` or a git checkout downloads it on first launch as it did for
  CustomTkinter. `doctor` reports the toolkit under its new name.

### Removed

- **Controller navigation of the launcher window is gone for now**, the one
  thing from 2.2.1 that did not survive the move to Qt. It was written
  directly against Tk's widget tree — walking children, drawing the highlight
  on a Canvas, binding through `tkinter.Misc.bind` — and none of that has an
  equivalent to port to. Reading the pad itself is untouched and `doctor`
  still reports which controllers it can see, so nothing about playing with a
  controller changes; it is only the launcher window that needs a mouse again,
  until a Qt-native replacement lands. Steam Game Mode is unaffected in the
  usual case, since the window steps aside on its own there.

- The **UI scale** setting (100/150/200%). Qt scales for the display's DPI on
  its own, and a second scale factor on top of that fought it.

- The **QR code** on the Xbox sign-in dialog. The code and the button that
  opens the sign-in page are unchanged.

### Fixed

- **Closing the launcher window left it running.** The process stayed resident
  with no window, every time, and a second launch added another one. The Qt
  rewrite needs the event loop to outlive the window so that "close the
  launcher when Minecraft starts" can hand the game off — but that makes
  quitting an explicit act, and nothing was doing it.

- **The `.deb` and `.rpm` could install and then not start at all.** Qt's xcb
  plugin loads thirteen X libraries the packages never declared, plus libEGL,
  and a missing one is not a Python error you can read: Qt aborts from C++
  before the launcher's own reporting runs, so it vanished with nothing in the
  terminal and nothing in the logs. Both packages now declare the whole set.

- **Switching between Stable and Preview only worked if you restarted first**
  (#201). The catalogue was fetched for whichever edition was selected at
  startup, so the other one came back empty: the version stayed on the build
  from the wrong edition, the picker refused to open, and ▶ PLAY quietly
  launched what was already selected. The full catalogue is fetched every
  session now.

- **Minecraft was audible but invisible in Steam Deck Game Mode** (#199).
  Gamescope only presents a window it can attribute to the application Steam
  launched, and from the Flatpak neither route to that attribution reaches the
  game — it starts through the portal, outside the process tree Steam
  launched, and the property gamescope reads is set by Proton's fork of Wine
  rather than the one this engine is built from. The launcher stamps it
  itself now.

- **The Microsoft Store sign-in did not survive closing the Flatpak** (#198),
  and losing it was worse than a sign-out: with no device credentials on file
  every command provisioned a *new* Store device, an account may hold ten, and
  once they were gone the licensing service refused the game outright with
  "Device group is full" — after which Minecraft could be neither downloaded
  nor started until devices were removed by hand on account.microsoft.com. The
  keyring now lives in the launcher's own storage, which persists in every
  packaging; one left behind by an earlier release is taken along.

- **A game directory missing its Store package took the launcher down with a
  Rust panic** quoting a file and line number, and nothing about which file was
  missing or that re-downloading is what brings it back. The directory looks
  complete — every DLL, the manifest, a full-size executable — because on a
  Store build the executable is ciphertext. It is named and explained now.

- **Injecting a client DLL always reported "process not found".** The game is
  started through the GDK loader, and Wine leaves the process name empty for
  it, so the snapshot never matched. A nameless process is now identified
  through the image path in its own PEB.

- **The Flatpak had no icon** — not in the window, not in the title bar, not on
  the hero screen. Its icon is installed under `/app` and the candidate list no
  longer looked there.

- **`doctor` offered to install a package that does not exist.** On Debian and
  Ubuntu there is no `python3-pyside6` — it is split per Qt module — and it did
  not need naming at all: the launcher installs the toolkit itself on first
  run, exactly as it did for the previous one. It also stopped reporting a
  perfectly working portable install as an unready system.

- **The activity log mangled its own text.** Every download line arrived as
  `-&gt; downloading …`, and any line holding a path with `&` or an error in
  angle brackets came out the same way — on exactly the text people are asked
  to paste into bug reports. The view also stopped following the newest line,
  and the status line above it, once painted red by a failure, stayed red for
  the rest of the session.

- **Two sign-ins could be started at once.** Clicking *Sign in* twice handed
  out two device codes for one sign-in, with the launcher waiting on the one
  you were not shown. "Sign out?" could also be left armed on screen
  indefinitely, where the next click answered it. Signing in also warms the
  Xbox token chain again, so the first launch afterwards no longer pays for the
  whole round trip.

- **One unreachable edition emptied the whole version picker** — a Preview
  outage took Stable down with it. And triggering the same background job twice
  (two clicks on *Import content*, a quick Stable/Preview toggle) destroyed the
  running thread underneath itself, which Qt turns into an abort.

- Smaller things in the window: the log's **Clear** and **Copy** buttons were
  clipped, three *Settings ▸ Tools* rows had lost their tooltips, and the
  version picker's filter field did not take focus, so typing into a freshly
  opened picker did nothing.


## 2.2.1 — 2026-08-21

### Added

- **The launcher can be driven with a controller**. On a Steam Deck, in Game
  Mode, or on any couch setup, the launcher window was the one screen with no
  way past it: it deliberately still opens before the game, and reaching ▶ PLAY
  needed a mouse. A highlight now follows the d-pad or the left stick, A
  activates, B goes back, the shoulder buttons change tab, Start plays and the
  right stick scrolls — through the version picker, the profile menu, sign-in,
  every Settings tab and any dialog that opens over the window. Buttons are
  read by position rather than by letter, so an Xbox pad and a PlayStation one
  behave the same. The highlight appears only once a controller is used and
  goes away again as soon as the mouse moves, so nothing changes for anyone who
  never picks one up, and a short reminder of the main buttons sits above the
  dock while a pad is connected. Controllers are read straight from
  `/dev/input` — no new dependency, hot-plug included, and nothing to install,
  since udev already grants the logged-in user access to joysticks. Input is
  ignored while Minecraft is running, so a pad in hand during the game never
  reaches the launcher behind it. Off with *Settings ▸ General ▸ Controller*,
  or `BOL_CONTROLLER=0` for one session; `doctor` reports which controllers it
  can see, and distinguishes "none connected" from one that is connected but
  not readable.

- **The launcher can close itself when the game starts**. *Settings ▸ General
  ▸ Startup ▸ Close the launcher when Minecraft starts*, off by default. With
  it on, the window goes as soon as Minecraft is running instead of waiting
  around behind it — no second entry in the taskbar, nothing to alt-tab past.
  Only the window closes: the process behind it stays, invisibly, until the
  game exits, because what it does afterwards is not optional. It arms a GPU
  safety marker before starting the game and clears it by watching the wrapper
  return — a marker nobody watches back blocks the next launch until a reboot
  — and it repairs and patches Minecraft's settings file once the game has
  stopped writing to it. When that is done it exits on its own, with nothing
  left behind. Leaving the switch off keeps what the window is worth during a
  session: KILL, the activity log, and the launcher reporting how the game
  ended. In Steam Game Mode the window still steps aside on its own, switch or
  no switch, and comes back when the game closes.

- **The AppImage updates itself in place, one changed block at a time**
  ([#191](https://github.com/Wyze3306/BedrockOnLinux/issues/191)). It now
  carries standard AppImage update information, and every release publishes the
  matching `BedrockOnLinux-*-x86_64.AppImage.zsync` beside it, so
  AppImageUpdate, AppImageLauncher, AM/AppMan and anything else that reads the
  standard can upgrade an existing file by fetching only the blocks that
  actually differ instead of the whole bundle. A stable AppImage follows the
  newest release, a nightly one the rolling `nightly` prerelease it came from,
  and the repository is read from the same place the launcher's own updater
  reads it, so a fork updates from its own releases. The sidecar describes one
  exact AppImage, so it is cleared, built, checksummed, attested and uploaded
  with it — and the build now fails outright if the update information is
  embedded without it, which `appimagetool` otherwise reduces to a warning.

- **`bedrock-on-linux doctor` now reports what the graphics stack granted the
  game** ([#153](https://github.com/Wyze3306/BedrockOnLinux/issues/153)).
  "Minecraft does not detect my ray tracing hardware" had no answer anywhere:
  the tier is decided inside the game process by vkd3d-proton, from the Vulkan
  features the driver exposes, and that decision was thrown away. Every launch
  now runs the graphics payload at its info level — a couple of dozen lines at
  device creation, nothing per frame — and doctor reads the outcome back out
  of the launch log: the DXR tier Minecraft's *Ray Traced* mode is gated on,
  whether the device also reached DirectX 12 Ultimate, and which half of the
  universal device-generated-commands payload the driver took. Nothing opens a
  GPU device to answer it, which keeps the check on the right side of the same
  rule the rest of the GPU reporting follows. It separates the cases that look
  identical from a screenshot and are not: a driver that exposed no ray
  tracing at all, a device that only reached tier 1.0 — which *Ray Traced*
  does not accept — the *Ray tracing* switch being off, and vkd3d-proton's own
  quirk dropping the tier on a Steam Deck. A launch that died before it ever
  created a device is reported as no answer rather than as a missing feature.
  For the complete feature set the same stack hands Minecraft, including the
  adapter it picked and whether an acceleration structure can actually be
  sized, `tools/dxr-probe.c` prints it from inside the prefix with the game
  not involved.

- **The version picker asks for the edition first**
  ([#190](https://github.com/Wyze3306/BedrockOnLinux/pull/190)). Minecraft and
  Minecraft Preview are two buttons above the list rather than a choice buried
  in it, the builds below follow whichever is selected, and the list is filled
  through the same beta setting the rest of the launcher honours, so a picker
  opened on Preview cannot offer a build it would refuse to install.
  Contributed by [@Hultwl](https://github.com/Hultwl).

### Fixed

- **The Microsoft sign-in window no longer dies on Wayland**
  ([#186](https://github.com/Wyze3306/BedrockOnLinux/issues/186)). Signing in to
  download Minecraft failed the moment the window should have appeared, with
  nothing to go on but `Gdk-Message: Error 71 (Protocol error) dispatching to
  Wayland display` — reported on Bazzite, Fedora and KDE Plasma, and fatal in
  the plainest sense: the download comes from your own Microsoft account, so a
  sign-in that cannot open is a game that cannot be installed. The window is
  WebKitGTK's, and by default WebKitGTK composites the page into a DMABUF
  buffer and hands that to the compositor. Where the handoff is refused, the
  connection is not degraded but torn down, taking the window with it. The
  launcher now runs the downloader with WebKitGTK's DMABUF renderer switched
  off, so the page is drawn through shared memory instead — the same
  `WEBKIT_DISABLE_DMABUF_RENDERER=1` that people were setting by hand, applied
  to every `xodus-cli` call rather than the sign-in alone, since the download
  and an encrypted game's launch load the same library. For one login page the
  accelerated path is worth nothing measurable, so this is not conditional on
  guessing at a compositor or a driver; a value you set yourself still wins,
  and the game is never started with it.

- **An encrypted game launch prepares the environment it runs in again**.
  `xodus-cli run` — the outermost process of a Store-installed Minecraft, since
  its executable stays encrypted on disk — was no longer being shown the
  environment the launch had assembled: the call that hands it over lost its
  `env=` to an unrelated indentation fix. The game itself was started with it
  regardless, so nothing changed on an ordinary desktop, but on a host with no
  WebKitGTK of its own the bundled runtime never reached the process that
  cannot start without it — SteamOS above all — and, until now, neither did the
  renderer setting above.

- **What the Achievements support covers, said plainly**
  ([#152](https://github.com/Wyze3306/BedrockOnLinux/issues/152)). The
  Achievements section promised a catalog and added that the launcher "does not
  unlock, emulate or force anything" — which reads as a policy rather than as a
  limit, and left a Flatpak player looking for what was broken on their side.
  Nothing was: new achievements never unlock, in any layout, and the Flatpak
  behaves exactly like the AppImage here. Minecraft does not write its own
  achievements. It reports what you did to Xbox as in-game events and the
  achievement engine awards them from those — asked directly instead, the
  achievements service refuses every id with *"None of the submitted
  achievements may be updated in this fashion"*, whichever token it is
  presented with, and the event path that would feed it is not implemented in
  the engine. The catalog is unaffected and still loads through its dedicated
  user-only token. `tools/achievements-probe.py` now shows the whole picture
  for your own account in one run, without launching the game: the full list
  through the Achievements token, the same list empty through the profile token
  every other Xbox Live call uses, and the refusal — reading the tokens the
  launcher already holds, printing none of them, and attempting its unlock on
  an achievement id that does not exist, so it cannot change anything.

- **A first run no longer fails with "wineboot timed out"**
  ([#144](https://github.com/Wyze3306/BedrockOnLinux/issues/144)). "Could not
  initialise the Wine prefix ... wineboot timed out after 300 seconds" was
  reported from CachyOS, Artix and Kubuntu alike, on the Flatpak, the AppImage
  and the packages, and it sent everyone hunting for missing Wine packages that
  were never the cause. The first launch has to fetch the Steam Linux Runtime,
  close to 900 MB unpacked and verified, and it does that inside the very call
  the launcher was timing: the budget meant for booting a prefix was being spent
  downloading a runtime, so only the first run failed and pressing Install again
  looked like a fix. That bootstrap now gets its own allowance, decided in
  advance from whether the runtime is already unpacked rather than measured
  afterwards, and the timeout message names the budget that actually ran out.

- **A failed Minecraft download can be retried**. Three attempts at the same
  build could all fail with `cache ended before cached_len`, or install nothing
  at all, alternating between the two forever: the downloader caches the
  encrypted package beside the game and re-opens it next time, and once that
  file was left short it stayed short, every later run either reading past its
  end or concluding there was nothing left to fetch. Deleting the version
  directory by hand was the only way out. A failed download now drops that
  cache, so the next attempt starts from a clean state, while a cache belonging
  to a playable install is kept, because an encrypted game reads from it at
  every launch.

- **The launcher scrolls under the mouse wheel everywhere**
  ([#187](https://github.com/Wyze3306/BedrockOnLinux/pull/187)). Wheel and
  touchpad events are normalised in one place and bound on the widgets that
  scroll, so the version list, the settings pages and the activity log all
  answer the wheel the same way instead of only some of them doing it.
  Contributed by [@Hultwl](https://github.com/Hultwl).

- **A Store-downloaded Minecraft starts on the Flatpak**
  ([#193](https://github.com/Wyze3306/BedrockOnLinux/issues/193)). PLAY went to
  a black screen and came straight back with `ShellExecuteEx failed: File not
  found`, on a Steam Deck where the game was installed, the account signed in
  and the executable sitting right there on disk. The Microsoft Store keeps
  that executable encrypted at rest, so the launcher decrypts it at launch and
  stages the plaintext on `/dev/shm` for Wine to map — but pressure-vessel
  builds the Steam Linux Runtime container as a *new* Flatpak app instance, and
  says so in the log itself: *"/dev/shm not shared between app instances"*.
  Wine was looking into a different `/dev/shm`, found nothing there, fell back
  to the ciphertext on disk and reported the game as missing. The decrypted
  image is now staged in the application's `$XDG_RUNTIME_DIR`, the tmpfs
  Flatpak binds into every instance of the same application: still RAM, still
  created 0600, still unlinked by the loader the instant it is opened — and now
  reachable from inside the container. Nothing changes outside the Flatpak, and
  images left behind by a launch that died are swept from both locations.

- **Imported worlds and templates land where a signed-in game reads them**
  ([#188](https://github.com/Wyze3306/BedrockOnLinux/issues/188)). A
  `.mctemplate` imported without a single error, and then the world creation
  screen went on offering nothing but Marketplace purchases. The import was
  real and the folder was right there — in the wrong `com.mojang`. A prefix
  holds one per account the player has signed in with, plus `Users/Shared`
  for playing signed out, and Minecraft splits its content between them:
  packs are shared across accounts, but worlds, world templates and skins
  belong to whoever is signed in and are only ever read from that account's
  own folder. Everything was being unpacked into `Users/Shared`, so packs
  arrived and worlds and templates quietly went nowhere. They now go to the
  folder of the account that played last — the one whose settings the game
  wrote most recently — while packs stay shared, and before the first launch,
  with no account folder to prefer, they still go to `Users/Shared`. Each
  import names the folder it wrote to, an import that finds earlier copies
  stranded in the shared folder says where they are instead of moving save
  data around, and *Open Minecraft folder* now opens the player's own content
  rather than the shared one.

- **Xbox Live sign-in survives a SISU refusal**
  ([#149](https://github.com/Wyze3306/BedrockOnLinux/issues/149)). The
  Microsoft login completed, and then every `sisu.xboxlive.com` call came back
  HTTP 403 — profile, PlayFab, multiplayer, Realms and licensing alike — on
  several accounts that Xbox and Minecraft were perfectly happy with
  everywhere else. Nothing was wrong with those accounts: the same audiences
  were still being issued by `xsts.auth.xboxlive.com` in the same run, with
  the same user token, which is why the Achievements token kept arriving while
  the rest of the chain came back empty. Each audience now falls back to XSTS
  when SISU refuses it, so the sign-in completes instead of stopping at a
  partial payload; the fallback costs nothing when SISU works, and it is not
  attempted when XSTS has already failed. The advice was misleading on top of
  it — an Xbox Live account rejection always names an `XErr`, so a bare 403 no
  longer reads as *"Xbox Live rejected this Microsoft account"* and no longer
  sends people to xbox.com to repair a profile that was never broken.

- **`BOL_INPUT=wayland` gets a Wayland driver that can load**
  ([#180](https://github.com/Wyze3306/BedrockOnLinux/issues/180)). Asking for
  a native Wayland window never started the game on any host: Wine loaded
  `winewayland.drv`, its Unix half failed on `undefined symbol:
  win32u_set_window_pixel_format`, and no window was created. The driver was
  not ours. Debian 11 ships the xkbregistry development files in their own
  package, the build container installed only `libxkbcommon-dev`, and Wine's
  configure dropped `winewayland.drv` without failing the build — and because
  the engine is a Proton base with our WineGDK build overlaid on top, that did
  not leave a gap in the result: the base's own Wine 10 driver stayed, next to
  our Wine 11 `win32u.so`, which no longer exports the entry points it
  imports. The build now installs `libxkbregistry-dev` and refuses to finish
  without a Wayland driver, and the packager refuses a candidate whose driver
  belongs to a different Wine build than the engine around it. Until an engine
  built that way is installed, asking for Wayland says so and plays on
  XWayland instead of failing to start; Doctor reports the driver's state on
  its own line.

- **A launcher that steps aside for the game says so**. In a session that
  shows one application window at a time — Steam Game Mode — the launcher
  unmaps its window while the game runs, or the game stays audible and never
  appears ([#130](https://github.com/Wyze3306/BedrockOnLinux/issues/130)); it
  comes back the moment the game closes. On screen that is indistinguishable
  from a launcher that crashed, and it gets reported as one. The activity log
  now records the step-aside and the reason for it, which is what separates
  that case from an actual failure in the next report. Starting the launcher
  has opened the launcher in every session since 2.2.0
  ([#179](https://github.com/Wyze3306/BedrockOnLinux/issues/179)); the version
  that started the game without its own window was 2.1.4.

- **The Minecraft download reports how far along it is**. Installing an
  edition left a working bar labelled *Installing Minecraft…* sweeping for the
  whole package — over two gigabytes with nothing to say whether any of it was
  moving. Xodus draws the progress it streams with indicatif, which stays
  silent unless `TERM` names a terminal it is willing to draw on, and a
  launcher started from a desktop entry, from Steam or in Game Mode inherits
  no `TERM` at all: the pty the launcher opens precisely to catch those bars
  carried, measured here, exactly zero lines while 334 MB arrived. The
  download now names a terminal for its own child when the session named
  none — a `TERM` the session did set is left alone — so the real byte counts
  come back, 25 updates in the first twelve seconds of a fresh install, and
  the status line spends them: *Downloading Minecraft… 37% (0.9 GiB of 2.3
  GiB)*. The size is there beside the percentage because on a download this
  large it is what tells you whether to wait for it.

- **SteamOS can install and start the game again**
  ([#184](https://github.com/Wyze3306/BedrockOnLinux/issues/184)). On a Steam
  Deck, linking the Microsoft account failed with `No such file or directory`.
  Behind it: `xodus-cli: error while loading shared libraries:
  libwebkit2gtk-4.1.so.0`. Xodus links its login webview into every subcommand,
  so that library has to load before the program starts — which makes it a
  requirement not only of the sign-in window, but of the download *and* of
  every launch, since a Store-installed executable stays encrypted on disk and
  `xodus-cli run` is what decrypts it. SteamOS ships no WebKitGTK, and its
  rootfs is read-only, so installing one means disabling that and losing it
  again at the next update: the AppImage was simply unusable there for anyone
  who did not already have the game. The launcher now carries that stack
  itself. When the host has WebKitGTK — every ordinary distribution, the
  `.deb`, the `.rpm`, the Flatpak's runtime — nothing changes and nothing is
  downloaded. When it does not, one ~80 MB archive is fetched, verified
  against a pinned SHA-256, and used for xodus-cli alone; the game is handed
  back its own environment one exec before it starts, so Wine and the Steam
  Linux Runtime keep their own libraries. Where even that is unavailable, the
  failure now names the missing library and the package that provides it on
  *this* distribution, instead of the loader's `No such file or directory`.

- **The main menu no longer runs the GPU at full load**
  ([#150](https://github.com/Wyze3306/BedrockOnLinux/issues/150)). A still
  image over a panorama was pinning cards at 87-90%, hot and loud, before the
  player had even chosen a world. Nothing in the stack paces Minecraft's
  render loop when the game itself does not: with vsync off and *Max
  Framerate* on Unlimited it simply draws as fast as the machine can, and the
  menu — the cheapest frame the game ever produces — is where that goes
  furthest. Measured here in that exact configuration: **~1800 FPS and 58-77%
  of an RTX 4060** to display a menu. The launcher now hands vkd3d-proton a
  frame rate limit at the refresh rate of the fastest display attached
  whenever the game has no limit of its own, which brought the same menu to
  **144 FPS and 3-16% of the card**. Only that unpaced case is capped: vsync,
  or any *Max Framerate* the player chose, is left exactly as it was, and
  nobody loses a frame their display could have shown. *Settings ▸ Advanced ▸
  Limit the frame rate to the display* turns it off, and `BOL_FRAME_RATE` in
  the custom-environment field names a rate instead: `0` never caps, a number
  always caps at it.

- **Naming what actually holds the frame rate down**
  ([#173](https://github.com/Wyze3306/BedrockOnLinux/issues/173)). Reports of
  the game refusing to reach 60 FPS while barely touching the GPU kept
  arriving with no log to explain them, on hardware from an RTX 3050 to a
  5090. The measurement finds Minecraft's own *Max Framerate* limiter: it
  waits for each frame's deadline by polling for window messages, and under
  Wine every one of those polls on an empty queue costs two user-mode
  callbacks and a `NtYieldExecution` — three system calls, roughly a hundred
  and fifty times per frame. In the main menu, same scene and same 60 FPS, the
  main thread costs **99% of a CPU core with 56 points of it in the kernel**
  when Minecraft holds the limit, against **10%** when the launcher's limiter
  holds it instead; whole-process CPU falls from 127% of a core to 50%. That
  thread is what builds every frame in Bedrock, so the wait comes straight out
  of the frame rate. The launcher cannot change the setting on the player's
  behalf — that file is theirs, and rewriting it is how #175 happened — so it
  now says so before each launch, with the exact replacement to use. With
  vsync on the game waits inside its present call instead and the main thread
  stays near 20%, so the notice is limited to the configuration that was
  measured.

- **Settings no longer reset themselves after a crash** (#175). Players lost
  their keyboard mappings, the tutorial flags and the whole *Video ▸ Mode*
  block at random, with nothing in any log to explain it. The settings that
  went were never random: Minecraft saves `options.txt` by truncating it and
  streaming the entire file back, so when the game dies part-way through that
  write — and on Wine it does, with the unhandled page fault the same sessions
  report — the file is left cut off, and every key past the cut reads as its
  default next time. Those four are simply what lives in the tail. The
  launcher now keeps a copy of the settings file before the game starts and
  puts it back when the game leaves a torn one behind, so an interrupted save
  costs that session's changes rather than a rebuilt control scheme. Only a
  file that cannot be a finished save is ever replaced, and only from a copy
  the launcher took itself: a settings file Minecraft wrote to the end is left
  alone however much it changed.

- **The launcher no longer rewrites Minecraft's settings file wholesale.**
  Disabling the repeated online-multiplayer warning reparsed `options.txt` and
  regenerated it from scratch — 14,728 of 15,458 bytes rewritten for a
  one-character change, every CRLF terminator converted to LF, and any line
  the launcher could not parse dropped on the floor. It also ran the instant
  the umu wrapper returned, which is not the instant Minecraft exits: the
  launcher could be rewriting the file while the game was still saving to it.
  It now edits the single line it owns, writes through a rename so the file is
  never observed half-written, and waits for the prefix to be idle first.

## 2.2.0 — 2026-08-19

### Added

- **A ray tracing switch, in Settings ▸ Advanced.** It is on by default and
  matches what already happened: the bundled vkd3d-proton reports the ray
  tracing tier on its own wherever the driver exposes the Vulkan ray tracing
  extensions, so Minecraft is offered DXR 1.1 and full DirectX 12 Ultimate
  without anyone asking for it. The switch makes that explicit in two
  directions — it drops a `VKD3D_CONFIG=nodxr` inherited from the session,
  which would otherwise remove the game's *Ray Traced* graphics mode with no
  trace anywhere, and it sets `nodxr` when you would rather keep the video
  memory. Minecraft's own conditions are unchanged: *Ray Traced* stays
  uneditable outside a ray-tracing-capable world, and *Vibrant Visuals*, being
  deferred rendering, is unaffected either way.

- **The launcher now names why the game is slow, before it starts.** "It lags"
  has always arrived as an engine or GPU report, because the ordinary causes
  leave nothing in any Wine, Proton or vkd3d log: a host with no memory left,
  where the kernel pages the game out and every chunk the player walks into
  comes back through a disk fault; a data directory too full for vkd3d to keep
  its shader cache, so pipelines are recompiled every session instead of
  reused; a render distance set past what Bedrock's main thread can feed, which
  leaves even a fast GPU idle, since that ring is built on one thread and the
  work grows with the square of the distance; and vsync left on while the game
  runs in a window on a desktop that composites every window, which stacks a
  second frame queue on the game's own. Each is now reported before launch and
  summarised by `doctor`, together with what to change. They are advisories —
  nothing is blocked and no setting of yours is touched — and
  `BOL_SKIP_PERF_CHECK=1` silences them. Detection costs two `/proc/meminfo`
  fields, one `statvfs` and a read of Minecraft's own options.txt: no Wine
  process is started and no GPU is opened, so it can run on every launch.

### Changed

- **A GPU safety block can be acknowledged from the dialog that reports it.**
  PLAY refuses the launch, and the failure dialog then told you to open
  Settings ▸ Tools and find the acknowledgement there. That is a long way
  round for the most common case by far: rebooting the machine while
  Minecraft is still open leaves an interrupted-launch marker behind, because
  the launcher is killed before it can clear its own marker, and nothing can
  tell that apart from a graphics driver that locked the machine up. The
  confirmation is now offered where the block is met. Nothing about the
  safety decision changes — the same eligibility is still re-evaluated under
  the launch lock before anything is written, a marker from the running boot
  is still refused, and every current graphics check still runs on the next
  PLAY.

- **Minecraft is now downloaded from the Microsoft Store with your own
  account.** Until now the launcher fetched a repackaged, DRM-stripped copy of
  the game from a third-party GitHub repository. That redistributed Minecraft,
  and it let anyone install it without owning it. The launcher now uses
  [Xodus](https://github.com/xodus-gaming/xodus), which signs in to your
  Microsoft account, asks Microsoft's licensing service for the title licence
  and streams the package straight from the Xbox CDN, decrypting it as it goes
  — the same path Windows takes.

  What this changes for you:

  - **You must own Minecraft on the account you link.** This is now true for
    installing, not only for playing online.
  - **There is a second sign-in.** The Store account authorises the download;
    the existing in-game account is unchanged. They can be the same account,
    but they are separate links, because the Store needs a device-bound
    session that the in-game sign-in cannot provide.
  - **The picker gained an edition, and kept its builds.** You choose
    *Minecraft* or *Minecraft Preview*, then a build. Microsoft's own service
    only ever offers the current build, so the list of older ones comes from
    [GdkLinks](https://github.com/MinecraftBedrockArchiver/GdkLinks), an index
    of where each build sits on Microsoft's CDN — it holds no game data, and
    the licence still comes from Microsoft for your account. Each build lives
    in its own folder, so going back to one you already have costs nothing.
  - **WebKitGTK is a new dependency** (`libwebkit2gtk-4.1-0`), for the Store
    sign-in window. The `.deb` and `.rpm` pull it in and `doctor` reports it.
    The Flatpak moved to the GNOME runtime to get it: `org.freedesktop.Platform`
    ships no WebKitGTK and no Flathub extension provides one, so the sign-in —
    and with it installing the game at all — could not run in the sandbox. Both
    runtimes share the same freedesktop base and the same GL extension point,
    and this manifest already built its own Tcl, Tk and Python, so nothing else
    about the Flatpak changes.

  A copy of the game you already have keeps working: point the launcher at it
  as before.

- Load the game executable from memory when the Store package keeps it
  encrypted. Store packages leave the segments they flag
  `KEEP_ENCRYPTED_ON_DISK` — the game executable on GDK titles — encrypted at
  rest, exactly as on Windows, so there is no executable on disk for Wine to
  open. The engine gained a loader that can map the main image from a
  descriptor or a path, and the launcher decrypts it into `/dev/shm`, which is
  RAM: the copy is private, the loader unlinks it the instant it opens it, and
  nothing reaches durable storage. It goes through a file rather than anonymous
  memory for one reason — the game runs inside the Steam Linux Runtime
  container, where a descriptor number from outside means nothing. The
  settings/pause stack-reserve fix now lands on that copy instead of the file
  on disk. Packages that ship a plaintext executable are detected and take the
  previous path unchanged.

  One consequence worth knowing: an encrypted package needs the licence at
  every launch, so starting the game requires the network and a valid Store
  session even for single-player. Nothing else about offline play changes.

### Fixed

- Stop the crash that hit the main menu as soon as an account was signed in.
  Offline play was fine, but signing in brought the game to the menu, let it
  start loading the account and then killed it with a page fault
  ([#171](https://github.com/Wyze3306/BedrockOnLinux/issues/171)). The engine's
  Xbox store component answered two of the queries the signed-in menu makes —
  the game licence and the list of associated products — as if a Microsoft
  Store were really behind it, handing back a hard-coded licence and an empty
  product catalogue. The game took those for real answers, marked its store
  data loaded and then read through catalogue entries that were never filled
  in. Both queries now report that no store service is available, which is a
  state the game already knows how to handle: it retries a few times and
  carries on with the store disabled, and the menu stays up. Signing out and
  back in, resetting the Wine prefix or reinstalling Minecraft were never real
  cures — the crash depended on timing, which is why it came and went.

- Open the launcher again. 2.1.4 made starting the launcher in a Gamescope
  session start Minecraft directly instead of showing its window, to get past
  Steam Deck Game Mode showing one window at a time. That took the launcher
  away from the people who wanted it: in Game Mode nothing appeared at all,
  and the session probe matched on the reporter's desktop session too, where
  the game started the moment the launcher was opened
  ([#127](https://github.com/Wyze3306/BedrockOnLinux/issues/127),
  [#130](https://github.com/Wyze3306/BedrockOnLinux/issues/130)). Starting the
  launcher now opens the launcher, in every session. The launcher-free launch
  stays available where it is asked for explicitly: `bedrock-on-linux play`,
  the **Play without the launcher** app-menu action, and the *Minecraft
  Bedrock* shortcut that `shortcut` writes. `BOL_FORCE_GUI` no longer has
  anything to override and can be dropped from Steam launch options.

- Reach the game from the launcher in Game Mode. Gamescope presents a single
  application window, so the launcher's own kept Minecraft's off screen — the
  game was audible but never appeared
  ([#130](https://github.com/Wyze3306/BedrockOnLinux/issues/130)). **▶ PLAY**
  now unmaps the launcher window as soon as the game process exists and maps
  it back when the game closes, so the launcher is usable there rather than
  bypassed. A failure
  brings the window back first, so its error dialog has a window to appear in.
  Desktop sessions show both windows and are unchanged.

## 2.1.4 — 2026-08-11

### Fixed

- Reach the game from Steam Deck Game Mode. Adding the launcher to Steam and
  starting it there left an interface whose buttons did nothing — no hover, no
  click — and when a launch did happen the game stayed audible but invisible,
  both only with the Flatpak
  ([#127](https://github.com/Wyze3306/BedrockOnLinux/issues/127),
  [#130](https://github.com/Wyze3306/BedrockOnLinux/issues/130)). Two causes,
  both specific to that session:
  - Gamescope's Xwayland legitimately reports zero RandR GPU providers, which
    the launcher already tolerates — but only once it recognises the session,
    and it recognised it from environment variables that a Flatpak sandbox
    does not forward. Game Mode therefore looked like an ordinary X11 session
    running on a software renderer, and the launcher refused to start with
    *could not verify any X11 hardware provider*. That is why the AppImage was
    unaffected and why connecting an external monitor, which makes a provider
    appear, was the only known way out. The session is now identified by
    Gamescope's own `GAMESCOPE_*` properties on the X root window, which no
    sandbox hides.
  - Game Mode shows one application window at a time, so the launcher's own
    window stands between Steam and the game. Starting the launcher there now
    starts the game directly instead, the same launcher-free path a *Minecraft
    Bedrock* shortcut uses. A first run still opens the window, since
    installing a version and signing in cannot be done without it, and
    `BOL_FORCE_GUI=1` in the shortcut's launch options opens it on demand.

- Keep playing after minimizing the game or leaving it on another virtual
  desktop. The window came back black and never repainted, the desktop
  eventually offered to force quit it, and it happened every single time;
  turning on the Legacy compatibility renderer was the only way out, at the
  cost of the anti-aliasing. An occluded `Present` returns before it queues
  anything, so the callback that releases the frame latency object Minecraft
  waits on never runs — and under Wine that is permanent rather than a paused
  frame, because the blocked render loop stops pumping messages, so the window
  manager can no longer deliver the restore that would clear the occlusion.
  The managed engine, now `wow64-archs-native14`, rebuilds vkd3d-proton with
  that object released on the occluded path, so the render loop survives being
  hidden and picks the window back up. Only `d3d12core.dll` changes; the
  Legacy compatibility renderer is no longer needed for this. The game now
  keeps rendering while minimized, which is what it already did under that
  renderer ([#50](https://github.com/Wyze3306/BedrockOnLinux/issues/50)).

### Build

- Make the reviewed vkd3d-proton payload reproducible again. `git diff
  --check` counted the blank line ending the restored upstream shader source
  as an error and aborted the build, and the generated revert patch is
  compared byte for byte against the vendored one while its `index` lines
  abbreviate blob hashes with `core.abbrev=auto`, whose length grows with the
  repository — upstream had since crossed that threshold, so the check failed
  on a correct revert. Repackaging a published engine to change only its vkd3d
  payload also now finds the WineGDK build records where such an engine keeps
  them, beside its embedded provenance.

### Internal

- Share the helpers six modules had each reimplemented: hashing a file,
  removing a path without following a directory symlink, testing existence
  including a dangling symlink, and reading an opt-in environment string. Two
  of the copies were byte-identical. Only one behaviour changed, and only for
  the better: removing a game directory now survives a concurrent delete, as
  the other two copies of that helper always did.

## 2.1.3 — 2026-08-09

### Added

- Start Minecraft without the launcher window. **Tools ▸ Create direct launch
  shortcut** and `bedrock-on-linux shortcut` write a *Minecraft Bedrock*
  desktop entry that runs the game directly, and print the equivalent command;
  `--profile NAME` scopes one to an isolated profile. Steam adds that entry as
  a non-Steam game, so it plays from the library and from Steam Deck Game Mode
  without the GUI in between. The launcher's own app-menu entry also gained a
  **Play without the launcher** action, which is how the Flatpak — where
  host-visible shortcuts cannot be written from the sandbox — offers the same
  thing, alongside `flatpak run io.github.wyze3306.BedrockOnLinux play`
  ([#158](https://github.com/Wyze3306/BedrockOnLinux/issues/158)).
- Say why a launcher-free launch stopped. A desktop or Steam shortcut discards
  the launcher's output, so a refused launch — no version installed, a busy
  prefix, a blocked graphics session — looked exactly like the shortcut doing
  nothing. Those failures are now raised as a desktop notification. Creating a
  shortcut also names the first-run steps it cannot perform itself, since it
  has nowhere to display the Microsoft device code or a version picker.

### Performance

- Build the engine with Wine's in-process synchronization (ntsync) backend,
  which had been silently compiled out. Wine 11 has no esync/fsync any more,
  so `/dev/ntsync` is its only fast synchronization path — but the engine is
  built in Debian 11 to hold the `GLIBC_2.31` ABI ceiling, and Bullseye's
  `linux-libc-dev` is kernel 5.10 and ships no `linux/ntsync.h`. `configure`
  therefore left `HAVE_LINUX_NTSYNC_H` undefined and reduced the whole backend
  to stubs, so every Win32 wait, `SetEvent`, mutex and semaphore became a
  wineserver round-trip. Because the wineserver serialises those requests,
  Minecraft's worker threads queued behind one another and the game behaved as
  if it were single-threaded: chunk generation stalled, the GPU was left idle
  while one core saturated, and server TPS collapsed. The reviewed upstream
  UAPI header is now vendored in `third_party/linux-uapi/` and installed into
  the build container, and both build scripts fail closed if the resulting
  `wineserver` does not carry the ntsync code path. Checking for the header is
  not enough: kernels 6.10 to 6.13 shipped a two-ioctl preview that satisfies
  `HAVE_LINUX_NTSYNC_H` while Wine's `NTSYNC_IOC_EVENT_READ` gate still
  compiles the backend out, which is why building the engine yourself on, say,
  Debian 13 produced a stubbed engine too
  ([#63](https://github.com/Wyze3306/BedrockOnLinux/issues/63),
  [#139](https://github.com/Wyze3306/BedrockOnLinux/issues/139),
  [#143](https://github.com/Wyze3306/BedrockOnLinux/issues/143),
  [#148](https://github.com/Wyze3306/BedrockOnLinux/issues/148),
  [#150](https://github.com/Wyze3306/BedrockOnLinux/issues/150)).
- Say so before the game starts when that fast path is still unusable, instead
  of leaving the stutter unexplained. PLAY and Doctor now report whether the
  installed engine carries the backend and whether the running kernel exposes
  `/dev/ntsync` (mainline Linux 6.14 and later, module `ntsync`), with the
  matching remedy. Silence the notice with `BOL_SKIP_NTSYNC_CHECK=1`.
- Describe the Legacy compatibility renderer accurately. Settings, the
  diagnostic hint and the README all implied it merely "bypasses DXVK", but
  `PROTON_USE_WINED3D=1` swaps the entire Direct3D stack — D3D9 through
  **D3D12** — to WineD3D, dropping DXVK *and* vkd3d-proton. Minecraft renders
  exclusively through D3D12, so the option replaces the renderer the game
  actually uses, which is why following that advice produced artifacts and
  "weird blocks" rather than a speed-up
  ([#63](https://github.com/Wyze3306/BedrockOnLinux/issues/63)).
- Ignore an inherited `PROTON_NO_NTSYNC`, so a stale global export can no
  longer serialise every worker thread behind the wineserver. The Advanced
  custom-environment field remains the supported way to turn the fast path off.

### Fixed

- Play without a working Xbox Live session. PLAY refused to start the game at
  all when no Microsoft account was linked (`No Microsoft account linked —
  click 'Sign in' first.`) or when the Xbox Live chain could not be completed,
  so a machine with no Internet connection could not reach even a single-player
  world: the offline token refresh failed, the pre-auth was reported as
  `Xbox Live rejected this Microsoft account`, and signing out replaced that
  error with the other one. Minecraft now starts in offline mode instead —
  single-player worlds and LAN play work, while Realms, servers, the
  Marketplace and Xbox friends stay unavailable until sign-in succeeds, which
  the launcher says before the game window appears. An unreachable Microsoft
  token endpoint is also reported as a connection failure rather than as a
  rejected account, and credentials the launcher could not validate are no
  longer handed to the engine, so it settles into offline mode instead of
  chasing a sign-in that cannot complete
  ([#160](https://github.com/Wyze3306/BedrockOnLinux/issues/160)).

### Packaging

- Ship an `.rpm`, so the Fedora-based gaming distributions install the launcher
  like any other application instead of falling back to the AppImage
  ([#156](https://github.com/Wyze3306/BedrockOnLinux/issues/156)). It carries
  the same hash-pinned GUI stack as the `.deb`, declares its dependencies with
  Fedora's package names, is built reproducibly from `SOURCE_DATE_EPOCH`, and
  goes through the same payload verification and build attestation as every
  other artifact. `scripts/build-release.sh` builds it beside the others, and
  `sudo dnf install ./bedrock-on-linux-*.x86_64.rpm` installs it.
- Publish nightly builds again. Every nightly since 2.1.2 failed: the candidate
  build always asked for the Flatpak's *released* manifest, so the bundle came
  from the pinned tag while the payload audit compared it against the branch
  being built. The first commit merged after a release therefore broke the
  build for good, and with the Flatpak a required format, no nightly release
  was cut at all. A nightly now builds its Flatpak from the checkout like every
  other artifact, keeping the AppStream metadata, and a stale pin during a real
  release build says so instead of only listing the files that differ.
- Document which component needs the Flatpak
  `--talk-name=org.freedesktop.Flatpak` permission and how to verify it. No
  launcher code calls the portal, so the permission read as unused; the caller
  is the bundled Steam Linux Runtime, whose `pressure-vessel` switches to a
  Flatpak sub-sandbox when it sees `/.flatpak-info` and requests it through
  `org.freedesktop.Flatpak.Development.Spawn`. Revoking the name leaves the
  GUI, downloads and sign-in working and breaks only the game launch, which is
  what made it look removable
  ([#157](https://github.com/Wyze3306/BedrockOnLinux/issues/157)).

## 2.1.2 — 2026-08-03

### Friends, Realms and Xbox Live

- Stop exporting `GNUTLS_SYSTEM_PRIORITY_FILE`, which restores Friends worlds
  over the internet. The variable was meant to force TLS 1.2 for Azure-fronted
  hosts, but inside the Flatpak it did the opposite: its mere presence made
  Wine's `secur32` abandon its own version-capped priority and negotiate
  TLS 1.3, which Wine 11.1 schannel does not support, so every in-game WinHTTP
  TLS connection died just after the handshake. The XSAPI realtime-activity
  WebSocket could then never connect, the session write went out without a
  connection id, and Minecraft reported the misleading `world is full` — or
  published a hosted world as LAN-only. Wine's own schannel priority already
  caps at TLS 1.2, so removing the workaround achieves what it intended
  ([#48](https://github.com/Wyze3306/BedrockOnLinux/issues/48),
  [#145](https://github.com/Wyze3306/BedrockOnLinux/issues/145),
  [#125](https://github.com/Wyze3306/BedrockOnLinux/issues/125),
  [#133](https://github.com/Wyze3306/BedrockOnLinux/issues/133)).
- Name an unsynchronized system clock when Xbox Live rejects the account. A
  drifted clock puts the signed XSTS request outside its validity window and is
  refused exactly like an unusable profile, so the generic advice sent people
  to xbox.com to fix an account that was fine
  ([#119](https://github.com/Wyze3306/BedrockOnLinux/issues/119)).
- Remove WineGDK's durable refresh token from the Wine prefix on sign-out, so
  no reusable credential survives in the prefix registry
  ([#120](https://github.com/Wyze3306/BedrockOnLinux/pull/120)).
- Restore the Microsoft sign-out button, which failed with a misleading
  "another setup, repair, or game session is already in progress" on every
  attempt: a re-introduced lock wrapper deadlocked against the one that now
  lives inside the sign-out itself.

### Fixed

- Recover automatically from an interrupted first-run data migration instead
  of refusing to start forever with `stale migration staging path exists`,
  which kept the Flatpak from opening at all on affected systems
  ([#124](https://github.com/Wyze3306/BedrockOnLinux/issues/124)).
- Retry the Wine prefix once with a reinstalled RNG component when `wineboot`
  aborts on the unresolved `advapi32.SystemFunction036` forward, instead of
  waiting out the 300-second timeout and reporting a broken prefix
  ([#138](https://github.com/Wyze3306/BedrockOnLinux/issues/138)).
- Print an acknowledgement command the running installation can actually run
  (`flatpak run …`, the AppImage or zipapp path) rather than a program name
  that only exists for the distribution packages
  ([#136](https://github.com/Wyze3306/BedrockOnLinux/issues/136)).
- Stop offering the GPU acknowledgement for an interrupted launch from the
  running boot, which is always refused until the machine is rebooted.
- Report a fatal in-game page fault as the game process crashing instead of
  answering `No known cause`
  ([#132](https://github.com/Wyze3306/BedrockOnLinux/issues/132)).
- Scroll the version picker with a mouse wheel or touchpad, including while
  the pointer is over a version row
  ([#112](https://github.com/Wyze3306/BedrockOnLinux/issues/112)).
- Place the version picker correctly at 150% and 200% UI scaling
  ([#117](https://github.com/Wyze3306/BedrockOnLinux/pull/117)).
- Stop re-downloading the changelog every time the Game/Launcher tabs are
  switched ([#131](https://github.com/Wyze3306/BedrockOnLinux/pull/131)).
- Seed the `cryptbase` RNG component before the managed `wineboot` rather than
  after it, so the first prefix creation cannot race the missing forward.
- Refresh managed-engine release metadata instead of reusing a stale cached
  response, which could pin setup to an engine asset that no longer matches
  ([#110](https://github.com/Wyze3306/BedrockOnLinux/pull/110)).
- Survive a malformed or truncated PE header when raising the game's stack
  reserve, instead of failing the launch with an unhandled `struct` error
  ([#140](https://github.com/Wyze3306/BedrockOnLinux/pull/140)).
- Build the QR code image on the main thread, removing a CustomTkinter
  threading hazard during device-code sign-in
  ([#140](https://github.com/Wyze3306/BedrockOnLinux/pull/140)).

### Security

- Reject archive members whose path escapes the destination directory when
  importing `.mcpack`/`.mcaddon`/`.mcworld`/`.mctemplate` content, closing a
  Zip Slip path-traversal write outside the content directory
  ([#140](https://github.com/Wyze3306/BedrockOnLinux/pull/140)).

### Packaging

- Build the Flatpak with `--release` so bundles keep their AppStream metadata
  ([#141](https://github.com/Wyze3306/BedrockOnLinux/pull/141)).
- Pin the Flatpak manifest to the released tag
  ([#142](https://github.com/Wyze3306/BedrockOnLinux/pull/142)).

## 2.1.1 — 2026-07-26

### Fixed

- Complete the in-game single-file picker on Minecraft's calling apartment,
  preventing the cross-apartment `RoFailFastWithErrorContext` crash triggered
  after choosing a custom skin.
- Keep the native picker owned by the game window so it remains visible in
  fullscreen and Gamescope sessions.
- Keep the WineX11 rendering surface at the client origin, removing the thin
  top and left borders that could appear when Minecraft entered fullscreen.
- Make the General, Advanced and Tools settings tabs scroll with an X11
  touchpad or mouse wheel even while the pointer is over a child control.
- Create and list isolated profiles at the shared installation root when the
  launcher is opened from a managed profile, while preserving direct
  `BOL_HOME` data-root overrides.
- Keep Flatpak's private XDG data directory writable while exposing only the
  pre-XDG data root read-only for automatic migration, fixing the
  `.shared-assets.lock` startup failure reported on Bazzite and CachyOS.
- Load Minecraft's Windows Achievements catalog with a dedicated user-only
  XSTS token while preserving the packaged Windows title, SCID and platform.
- Repair an invalid managed-prefix `user32.dll` atomically from the verified
  engine before Wine starts, and diagnose its `c0000020` load failure instead
  of incorrectly reporting a missing `ntdll` patch.
- Remove the inherited Wine 10 i386 Unix runtime and force the managed engine's
  pure-WoW64 path, avoiding host i386 dependencies on minimal distributions.
- Keep graphics caches across Minecraft version changes and prevent Advanced
  diagnostics from tracing hot GDK polling channels that can starve the game.
- Ship the fixes in the reproducible, attested `wow64-archs-native12` managed
  engine.

## 2.1.0 — 2026-07-25

### Startup, engine, and compatibility

- Repair or reject partial Wine prefixes instead of reporting an incomplete
  installation as successful, including timed-out `wineboot`, incomplete
  registry hives, missing WineGDK activation, and residual Wine processes.
- Provide the missing native `cryptbase.SystemFunction036` implementation,
  removing the RNG recursion that prevented Wine services and the game window
  from starting.
- Upgrade the managed engine to `wow64-archs-native6` and UMU to 1.4.3.
- Disable incompatible global Proton options such as
  `PROTON_ENABLE_WAYLAND=1` automatically while preserving overrides explicitly
  configured in Advanced Settings.
- Recover stale XWayland `DISPLAY` values from session sockets owned by the
  current user, including on Hyprland, and improve image restoration after
  switching virtual desktops.
- Use the primary RandR monitor rather than the combined virtual-desktop size
  and tolerate non-UTF-8 system output.
- Recognize nested Gamescope sessions, remove Wine decorations on Steam Deck,
  and improve fullscreen behavior in Steam Game Mode.
- Configure DualSense and DualSense Edge through Steam Input when a virtual
  controller is present, and SDL otherwise.
- Add an explicit opt-in WineD3D compatibility renderer for systems limited to
  Vulkan 1.2.
- Repair WinRT activation and open the native file chooser for world imports
  and skin-file selection. Applying a custom skin remained a known issue in
  2.1.0 and is fixed in 2.1.1.
- Add direct `.mcskin` package import while Minecraft is stopped.
- Detect Minecraft archives replaced under the same tag, verify their identity
  and digest, and activate or restore them transactionally.
- Harden 64-bit DLL injection with a timeout, process validation,
  immediate-crash detection, and accurate failure messages.

### Xbox accounts, profiles, and multiplayer

- Add isolated local Xbox profiles. Each profile keeps its own account, prefix,
  settings, and worlds while large game, engine, and runtime downloads are
  shared under a lock.
- Serialize preparation, repair, launch, and shared-resource access to prevent
  inconsistent concurrent installations or sessions.
- Make Xbox pre-authentication errors more precise and actionable without
  retaining or printing response secrets.
- Add `doctor --network` and `doctor --host <IP>` for read-only checks of DNS,
  TLS 1.2+, clock/RTC, routes, VPN/container interfaces, and
  Xbox/PlayFab/Minecraft endpoints.
- Recognize `InitialConnection-13`, `InitialConnection-25`, client/host build
  mismatches, and the misleading “world full” message, with guidance that does
  not require users to configure environment variables manually.

### Interface, storage, and recovery

- Redesign the interface with tabbed settings, version search/filtering,
  persistent version selection, Copy/Clear log actions, and consistent
  PLAY/STOP states.
- Integrate application and Minecraft changelogs into the GUI and add the
  `changelog` CLI command, with disk caching, Markdown rendering, links,
  filtering, and controlled refresh.
- Rework Microsoft sign-in with a locally generated QR code, synchronized
  theme, gamertag retrieval, and two-step sign-out.
- Replace generic Tk dialogs with themed, centered, resizable, scrollable
  dialogs suitable for high scaling and multi-monitor desktops.
- Add 100/150/200% UI scaling, fix application restart outside a zipapp, and
  preserve theme choices.
- Add persistent Gamescope arguments and custom environment-variable fields
  with safe parsing of quoted values.
- Add GUI-driven data relocation with free-space checks, locking, internal-link
  repair, rollback, and safe restart.
- Honor `XDG_DATA_HOME`. Flatpak now writes to its private storage and migrates
  the previous directory transactionally without merging two populated trees;
  the old tree remains available as a recovery backup.
- Expose acknowledgement of an earlier GPU incident in the GUI only when
  eligible, revalidate it under lock, and remove orphaned markers after a clean
  exit.

### Security, integrity, and publication

- Stop forwarding an ambient `GITHUB_TOKEN` to non-GitHub hosts.
- Harden downloads and extraction against path traversal, unsafe links,
  partial archives, and unverified replacements.
- Add a public, reproducible, attested pipeline for OpenSSL-XCurl,
  vkd3d-proton, WineGDK, the managed engine, and all four application formats.
- Pin source trees, containers, APT snapshots, Python dependencies,
  intermediate digests, and final artifacts; mismatches now fail closed.
- Separate read-only build jobs from attestation/publication jobs, add a
  rolling nightly release, and include a bill of materials in releases.
- Add CI checks for tests, Python compilation, ShellCheck, actionlint, Ruff,
  Zizmor, CodeQL, published-pin validation, and a native Wine RNG test.
- Separate `SHA256SUMS` for application artifacts from `inputs.sha256` for
  separately published engine and XCurl inputs.

### Validation

- Pass 441 automated tests and 29 subtests.
- Pass Ruff, high-confidence Vulture, actionlint, Zizmor, and AppStream
  validation.
- Rebuild the WineGDK prefix and native6 engine reproducibly.
- Load `wine-11.1` successfully inside the pinned Steam sniper runtime.
- Build `.deb`, `.pyz`, AppImage, and Flatpak artifacts with the expected
  version and metadata.

Known limitations:

- #48/#61: the launcher diagnoses known “world full” causes but cannot alter a
  remote Windows host.
- #15: Xodus is not included in this release.
- #55: Xbox achievement submission has not been validated end to end.
- #63: launcher-induced repeated shader compilation and debug-I/O stutter are
  fixed, but initial RX 9060 XT chunk generation still needs reporter
  validation.
- #90: Borion and other version-specific third-party DLLs are not guaranteed
  and may crash Minecraft.
- WineD3D is a degraded compatibility mode; performance and rendering are not
  guaranteed.
- Published builds target x86-64 glibc Linux. The graphical launcher requires
  X11/XWayland; ARM and musl-only systems are not supported.
