#!/usr/bin/env bash
# Measure Minecraft's frame pacing and Wine synchronization cost while you play.
#
# Launches the game through the normal launcher with the Mesa Vulkan overlay
# logging frame times, samples the wineserver's synchronization round-trips and
# the GPU alongside it, and prints a summary when you close the game.
#
# Nothing is installed and no setting is changed: the overlay is an explicit
# Vulkan layer enabled per-process through the environment, and everything is
# written below one report directory.
#
# Usage:
#   tools/measure-frames.sh [LABEL]
#
# Play normally once the game is up — load a world, fly around to force chunk
# generation, open the settings screens. The report separates the first minute
# (pipeline compilation warming up) from the rest, because those are two
# different problems: compilation stutter fades as caches fill, whereas the
# wineserver round-trip cost does not.
set -Eeuo pipefail

LABEL="${1:-run}"
LAYER_LIB=/usr/lib/x86_64-linux-gnu/libVkLayer_MESA_overlay.so
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
REPORT_DIR="${BOL_MEASURE_DIR:-$HOME/bol-measure}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$REPORT_DIR/$LABEL-$STAMP"
WARMUP_SECONDS=60

mkdir -p "$OUT"

if [ ! -f "$LAYER_LIB" ]; then
  echo "!! $LAYER_LIB is missing — install the Mesa Vulkan overlay layer" >&2
  echo "   (Debian/Ubuntu: mesa-vulkan-drivers, Arch: vulkan-mesa-layers)" >&2
  exit 1
fi

# The engine the launcher is configured to use, so the sampler can tell this
# run's wineserver apart from one left behind by an earlier session.
engine_root="$(cd "$PROJECT_ROOT" && python3 -c 'import sys; sys.path.insert(0, ".")
from bol.util import load_settings
print(load_settings().get("proton") or "")' 2>/dev/null || true)"

# State the synchronization verdict up front. Comparing a run whose engine
# lacks the ntsync backend against one that has it is the whole point, and a
# silently reverted engine setting makes the two runs indistinguishable
# afterwards -- so record it in the report directory too.
fast_sync="$(cd "$PROJECT_ROOT" && python3 -c 'import sys; sys.path.insert(0, ".")
from bol.ntsync import inproc_sync_summary
from bol.util import load_settings
print(inproc_sync_summary(load_settings().get("proton"), environ={}))' 2>/dev/null || echo "unknown")"

echo "== Report directory: $OUT"
echo "== Engine    : ${engine_root:-unknown}"
echo "== Fast sync : $fast_sync"
{ echo "engine=$engine_root"; echo "fast_sync=$fast_sync"; } >"$OUT/conditions.txt"
case "$fast_sync" in
  OK*) ;;
  *) echo "   NOTE: this run will NOT use ntsync — it measures the slow path." ;;
esac
echo "== Launching Minecraft with frame logging."
echo "   Play normally, then CLOSE THE GAME WINDOW to end the measurement."

(
  cd "$PROJECT_ROOT"
  VK_INSTANCE_LAYERS=VK_LAYER_MESA_overlay \
  VK_LOADER_LAYERS_ENABLE=VK_LAYER_MESA_overlay \
  VK_LAYER_MESA_OVERLAY_CONFIG="fps,frame_timing,output_file=$OUT/frames.csv" \
    python3 -m bol play
) >"$OUT/launcher.log" 2>&1 &
LAUNCHER_PID=$!

cleanup() { kill "$LAUNCHER_PID" 2>/dev/null || true; }
trap cleanup INT TERM

# Sample the synchronization and GPU side while the game runs. The wineserver
# is single-threaded, so its voluntary context switches count how many Win32
# waits had to round-trip through it — the cost that disappears with ntsync.
: >"$OUT/samples.tsv"
printf 'elapsed_s\tsync_roundtrips_per_s\tgpu_util_pct\tgame_cpu_pct\n' >>"$OUT/samples.tsv"
started="$(date +%s)"
prev_sync=""; prev_cpu=""
while kill -0 "$LAUNCHER_PID" 2>/dev/null; do
  sleep 5
  # Match on the executable name, not the command line: -f also catches this
  # script and any shell whose arguments mention wineserver.
  server="$(pgrep -x wineserver 2>/dev/null | tail -1 || true)"
  # pgrep -f also matches the umu-run wrapper, srt-bwrap and pv-adverb, whose
  # command lines contain the exe path but which sit idle. Select on comm --
  # and note that Wine reports the game as "MINECRAFT MAIN", not the file
  # name, so the comparison has to be case-insensitive.
  game=""
  for candidate in $(pgrep -f 'Minecraft.Window[s].exe' 2>/dev/null || true); do
    comm="$(cat "/proc/$candidate/comm" 2>/dev/null || true)"
    case "${comm,,}" in
      minecraft*) game="$candidate" ;;
    esac
  done
  [ -n "$server" ] && [ -n "$game" ] || continue

  # Report what the launch REALLY used, once. The configured setting is not
  # evidence: the launcher re-selects the managed engine whenever it does not
  # consider the configured one user-supplied, so a run can silently use a
  # different engine than the one requested. The wineserver binary's own path,
  # and its open handles on /dev/ntsync, are the facts.
  if [ -z "${actual_engine:-}" ]; then
    actual_engine="$(readlink -f "/proc/$server/exe" 2>/dev/null || true)"
    ntsync_handles="$(ls -l "/proc/$server/fd" 2>/dev/null | grep -ci ntsync || true)"
    {
      echo "actual_wineserver=$actual_engine"
      echo "wineserver_ntsync_handles=${ntsync_handles:-0}"
    } >>"$OUT/conditions.txt"
    echo "== Running engine : ${actual_engine%/files/bin*}"
    if [ "${ntsync_handles:-0}" -gt 0 ] 2>/dev/null; then
      echo "== ntsync         : ACTIVE (${ntsync_handles} handles)"
    else
      echo "== ntsync         : NOT IN USE — this run measures the slow path"
    fi
  fi
  sync="$(awk '/^voluntary_ctxt_switches/{print $2}' "/proc/$server/status" 2>/dev/null || true)"
  cpu="$(awk '{print $14+$15}' "/proc/$game/stat" 2>/dev/null || true)"
  gpu="$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null | head -1)"
  [ -n "$gpu" ] || gpu="-"
  if [ -n "$prev_sync" ] && [ -n "$sync" ] && [ -n "$cpu" ] && [ -n "$prev_cpu" ]; then
    printf '%s\t%s\t%s\t%s\n' \
      "$(( $(date +%s) - started ))" \
      "$(( (sync - prev_sync) / 5 ))" \
      "$gpu" \
      "$(( (cpu - prev_cpu) * 100 / 500 ))" >>"$OUT/samples.tsv"
  fi
  prev_sync="$sync"; prev_cpu="$cpu"
done
trap - INT TERM

echo
echo "== Game closed. Building the report."
WARMUP_SECONDS="$WARMUP_SECONDS" OUT="$OUT" python3 - <<'PY'
import csv, os, statistics

out = os.environ["OUT"]
warmup = float(os.environ["WARMUP_SECONDS"])
frames_path = os.path.join(out, "frames.csv")

fps = []
if os.path.exists(frames_path):
    with open(frames_path) as handle:
        for row in csv.reader(handle):
            if len(row) < 4:
                continue
            try:
                value, timing = float(row[2]), float(row[3])
            except ValueError:
                continue
            if value > 0:
                fps.append((value, timing))

if not fps:
    raise SystemExit("No frame samples were captured — did the game render?")

# The overlay logs one row per interval; frame_timing is that interval in µs.
elapsed, phases = 0.0, {"warm-up (pipeline compilation)": [], "steady play": []}
for value, timing in fps:
    phases["warm-up (pipeline compilation)" if elapsed < warmup
           else "steady play"].append(value)
    elapsed += timing / 1_000_000.0

print(f"\nFrame rate over {elapsed/60:.1f} min of play\n" + "-" * 62)
for phase, values in phases.items():
    if not values:
        continue
    ordered = sorted(values)
    stutter = 100.0 * sum(1 for v in values if v < 30) / len(values)
    print(f"{phase:32s} n={len(values):4d}  median={statistics.median(values):5.1f}"
          f"  1%low={ordered[max(0, len(ordered)//100)]:5.1f}"
          f"  under30fps={stutter:4.1f}%")

samples_path = os.path.join(out, "samples.tsv")
if os.path.exists(samples_path):
    sync, gpu = [], []
    with open(samples_path) as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            try:
                sync.append(int(row["sync_roundtrips_per_s"]))
            except (ValueError, KeyError, TypeError):
                pass
            try:
                gpu.append(float(row["gpu_util_pct"]))
            except (ValueError, KeyError, TypeError):
                pass
    if sync:
        print(f"\nWineserver sync round-trips : median {statistics.median(sync):.0f}/s"
              f"  peak {max(sync)}/s")
        print("  Every one of these is a Win32 wait that could not use ntsync.")
    if gpu:
        print(f"GPU utilisation             : median {statistics.median(gpu):.0f}%"
              f"  peak {max(gpu):.0f}%")
        print("  A low peak here means the GPU is not the limit.")

print(f"\nRaw data: {out}")
PY
