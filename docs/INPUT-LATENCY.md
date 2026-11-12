# Input latency: mouse to screen (#202)

A player on 2.3.1 reported a camera that trails the mouse. This is what the
time from a mouse movement to the moved picture is made of, measured on the
real game, and what the launcher changes because of it.

## Results

Real game (Minecraft 1.26.60.5, a loaded Creative world standing still),
RTX 4060 with NVIDIA 550.163, headless gamescope at 1600x900 and 60 Hz.
40 injected camera motions per row; milliseconds after the motion, mean
[10th–90th percentile].

| Configuration | presented | shown |
|---|---|---|
| X11 client of Xwayland, vsync on (the default) | 73 [65–81] | **139** [131–147] |
| … `VKD3D_SWAPCHAIN_LATENCY_FRAMES=1` | 73 [66–80] | 139 [132–148] |
| … `VKD3D_SWAPCHAIN_LATENCY_FRAMES=2` | 71 [64–81] | 137 [130–149] |
| … *Keep the mouse inside the window* (Wine virtual desktop) | 72 [66–78] | 138 [132–145] |
| … paced by vkd3d-proton at 60 FPS (`BOL_FRAME_RATE=60`) | 61 [53–68] | 88 [81–94] |
| … paced at 59.82 FPS, just under the display (this change) | 60 [53–67] | **75** [57–92] |
| X11, vsync off, the launcher's 60 FPS cap | 61 [54–68] | 113 [95–125] |
| X11, vsync off, uncapped (~1050 FPS) | 18 [15–19] | 42 [35–49] |
| cage (wlroots) + Xwayland, vsync on | 72 [65–79] | 154 [146–160] |
| cage + winewayland (`BOL_INPUT=wayland`), vsync on | 61 [53–67] | **73** [65–78] |

*presented* is the game's `vkQueuePresentKHR` of the first frame that moved:
everything the game itself does (input, simulation, a frame or more of
rendering). *shown* is gamescope compositing that frame. The two rows in cage
run gamescope → cage → the game, since winewayland needs `wl_subcompositor`,
which gamescope does not offer; the winewayland row counts 20 motions, since
only motions to the right reached the game through that nesting.

Whether it holds: the same world, a second train of 40 motions 90 s after
the first (*shown*, ms). gamescope's output runs at 60.00 Hz, and RandR
reports 59.95 Hz for it.

| vsync on, paced… | first train | 90 s later |
|---|---|---|
| not at all | 137 [131–144] | 141 [133–147] |
| at 60 FPS, the display's own rate | 89 [81–94] | 80 [73–85] |
| at 59.82 FPS, by the launcher (this change) | 75 [57–92] | 74 [62–89] |
| at 60 FPS by the game's own Max Framerate | 70 [63–76] | 69 [57–79] |

The paced rows spread wider: a frame paced just under the refresh lands
anywhere from just before a refresh to just after one, as its phase slides.

A Direct3D 12 test application (`tools/inputlat/latd3d.c`: raw input, a
waitable swap chain, a colour flip per motion) under the same Wine, DXVK dxgi
and vkd3d-proton isolates the swap chain from the game:

| Test application, 60 Hz | presented | shown |
|---|---|---|
| vsync on, `SetMaximumFrameLatency(1)` | 23 | 89 |
| vsync on, `SetMaximumFrameLatency(3)` | 56 | 122 |
| vsync on, no waitable object | 56 | 124 |
| … `VKD3D_SWAPCHAIN_LATENCY_FRAMES=1` | 24 | 91 |
| vsync on, latency 1, `VKD3D_SWAPCHAIN_IMAGES=2` | 25 | 91 |
| vsync on, latency 1, vkd3d-proton limiter at 60 | 8 | 34 |
| vsync off, limiter at 60 | 23 | 57 |
| cage + Xwayland, vsync on (windowed) | 54 | 107 |
| cage + winewayland, vsync on (windowed) | 8 | 25 |
| 144 Hz, vsync on | 10 | 37 |
| 144 Hz, vsync on, limiter at 144 | 5 | 26 |

## What the numbers say

- **Under Xwayland a vsync'd frame waits about four refreshes before it is
  shown.** The swap chain's present-wait (`vkWaitForPresentKHR`) returns
  0.3 ms after the present, when Xwayland passes the frame on, not when it is
  displayed. vkd3d-proton's frame-latency gating waits on exactly that, so
  neither the game's latency limit nor `VKD3D_SWAPCHAIN_LATENCY_FRAMES` ever
  sees the frames queued behind it: 66 ms between *presented* and *shown* in
  the game, 66 in the test application. `VKD3D_SWAPCHAIN_IMAGES=2` changes
  nothing (NVIDIA's minimum is 3), and MAILBOX hangs the swap chain on this
  driver under Xwayland.
- **Minecraft already asks for the lowest latency.** It calls
  `IDXGISwapChain2::SetMaximumFrameLatency(1)` and waits on the frame-latency
  object right after creating its swap chain (read from the decrypted
  1.26.60.5 image; *Enabling frame latency handles* in its log is that path).
  So `VKD3D_SWAPCHAIN_LATENCY_FRAMES` has nothing left to cut: 1, 2 and 3
  measure the same. It does cut a frame per step in an application that runs
  further ahead (test application with latency 3: 125 / 108 / 89 ms).
- **The virtual desktop is not the delay.** *Keep the mouse inside the
  window* measures the same as a plain window, in the game and in the test
  application.
- **Pacing the game at the display's rate empties the queue.** Unpaced, the
  game renders as fast as images come back: it waits 11.6 ms (median) in
  `vkAcquireNextImageKHR` for one, all three being held by Xwayland and
  gamescope, and the frame it then draws waits behind the other two. Paced
  by any limiter at the display's rate, that wait drops to 0.01 ms and the
  queue with it: 139 → 75 ms, still 74 ms 90 s later. The launcher's pace is
  just under the rate the display runs at, since there the display always
  takes frames faster than the game makes them and no queue can build up
  again; at the display's own rate the two clocks drift against each other,
  and vkd3d-proton's limiter at 60 FPS measured 89, then 80 ms. The game's
  own Max Framerate at 60 does as well as the pace (70, then 69 ms), but it
  waits by spinning a whole core in Wine's message loop, which is why the
  launcher swaps it for vkd3d-proton's.
- **winewayland halves it.** With true presentation feedback from the
  compositor the present-wait means *shown*, and the frame reaches the
  screen one refresh after the game presents it: 73 ms against 154 for the
  same game through cage's Xwayland.
- The rest is the game: about four frames from the motion to its own
  present at 60 FPS, 18 ms at 1050 FPS.

## What changed

Under Xwayland (an X11 game on a Wayland desktop, or in gamescope), with
vsync on and no limit of the player's under the refresh rate, the launcher now
paces Minecraft at the display's rate less 0.2 % (60 Hz → 59.88 FPS,
143.85 Hz → 143.56 FPS): 139 → 75 ms at 60 Hz in the table above. The
margin covers a reported rate a little off the real one (RandR says 59.95 Hz
for gamescope's 60.00). The cost:

- vkd3d-proton's limiter thread, which ends each wait spinning: about a fifth
  of a core at 60 FPS with the shipped vkd3d-proton (#202; the busy-wait patch
  on `wyze/silly-keller-7d9912` brings it to 4–6 %). This undoes, under
  Xwayland only, the saving 47b689a made for vsync'd games.
- One repeated frame every 8 s at 60 Hz (3.5 s at 144 Hz), where the pace
  falls a whole refresh behind the display.

*Limit frame rate to the display* in Settings ▸ Advanced turns it off. A plain
X11 server is left as it was: that queue was not measured there.

The pace needs its decimals, which `VKD3D_FRAME_RATE` cannot carry through
Proton: GE-Proton's protonfixes takes it out of the environment and passes
it on as DXVK's `dxgi.maxFrameRate`, a whole number, so `59.88` is no limit at
all (the reason the launcher's other caps are whole numbers, rounded up). Wine
also adds the prefix's `HKCU\Environment` to the environment of every process
it starts, behind Proton's back, and vkd3d-proton reads its own
`VKD3D_FRAME_RATE` there with decimals, ahead of the limit DXGI passes it. So
a `VKD3D_FRAME_RATE` that is not a whole number now goes into the prefix's
environment before the game starts, and every other launch removes it.

`VKD3D_SWAPCHAIN_LATENCY_FRAMES` stays at vkd3d-proton's default: the game's
own latency limit of 1 already bounds what it would.

## Not changed, for a decision

winewayland by default on Wayland desktops would halve the delay without the
limiter's cost. It was measured in cage nested in gamescope only, where its
full-screen popup window stalled after one frame in the test application and
only rightward motions reached the game. It needs a run on a real GNOME or KDE
session first.

## How it was measured

- `tools/inputlat/inputlat_layer.c` — a Vulkan layer. At each
  `vkQueuePresentKHR` it chains a copy of a 24×14 pixel grid in front of the
  present and logs, per frame, when it was presented and how many points
  changed; it also times `vkWaitForPresentKHR` and `vkAcquireNextImageKHR`.
  Loads in the game through pressure-vessel (it uses the host's glibc).
- `tools/inputlat/inj.c` — timestamped relative motions through XTest on the
  game's X server, or gamescope's EIS socket. Trains of 40 motions,
  alternating ±200 counts (±400 once night had fallen in the world and a
  smaller turn changed too few pixels), 450 ms + 0–17 ms apart so they do not
  lock to the refresh.
- `tools/inputlat/pwcap.py` — gamescope's own PipeWire output, compared on
  the same grid: the composited frame. It arrives 3.6 ms after gamescope
  presents (calibrated against winewayland's `wp_presentation` feedback),
  subtracted in *shown*.
- `tools/inputlat/analyze.py` pairs each motion with the first frame that
  moved, after a still frame; motions while the scene moved on its own are
  dropped.
- Checked first on known cases: `latapp.c` (X11 + Vulkan) logs its own
  presents and present-waits, and the probe picks exactly its flipped frame
  (present times within 1 µs, present-waits within 25 µs, `crosscheck.py`); an
  added 25 ms delay moves the result by 32.5 ms (two refreshes, 33.3).

Build and run:

```bash
gcc -std=gnu11 -O2 -fPIC -shared -fvisibility=hidden -o tools/inputlat/libVkLayer_inputlat.so tools/inputlat/inputlat_layer.c -lpthread
```

```bash
gcc -O2 -o tools/inputlat/inj tools/inputlat/inj.c $(pkg-config --cflags --libs libei-1.0 x11 xtst)
```

```bash
VK_ADD_LAYER_PATH=tools/inputlat VK_INSTANCE_LAYERS=VK_LAYER_BOL_inputlat INPUTLAT_LOG=/tmp/lat/layer.log <game command>
```

then feed `inj` on stdin (`t 40 450 200`) and run
`analyze.py inj.log layer.log.<pid> - --pw pw.log`.
