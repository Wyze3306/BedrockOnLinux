"""Capture gamescope's own PipeWire output stream and log when its picture moves.

usage: pwcap.py GAMESCOPE_LOG OUT_LOG SECONDS   (node id read from gamescope's log)

Each composited frame gamescope hands to PipeWire is timed on arrival
(CLOCK_MONOTONIC) and compared with the previous one on the same 24x14 grid
the Vulkan probe uses:  C <t_ns> <changed> <points> <pts_ns>
"""
import json
import os
import subprocess
import sys
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

import numpy as np  # noqa: E402

GX, GY = 24, 14
gs_log, out_path, seconds = sys.argv[1], sys.argv[2], float(sys.argv[3])


def find_node():
    import re
    for _ in range(100):
        m = re.search(r"stream available on node ID: (\d+)", open(gs_log, errors="replace").read())
        if m:
            nid = int(m.group(1))
            dump = json.loads(subprocess.run(["pw-dump", str(nid)], capture_output=True, text=True,
                                             timeout=10).stdout or "[]")
            for obj in dump:
                if obj.get("id") == nid:
                    return nid, ((obj.get("info") or {}).get("props") or {}).get("object.serial")
        time.sleep(0.2)
    return None, None


node, serial = find_node()
if node is None:
    print("no gamescope node", file=sys.stderr)
    sys.exit(1)
print("node", node, "serial", serial, file=sys.stderr)
Gst.init(None)
pipe = Gst.parse_launch(
    "pipewiresrc target-object=%s always-copy=true %s ! video/x-raw,format=BGRx ! "
    "appsink name=sink emit-signals=true sync=false max-buffers=1 drop=false" % (serial, os.environ.get("PWSRC_OPTS", "")))
sink = pipe.get_by_name("sink")
log = open(out_path, "w", buffering=1)
state = {"prev": None, "n": 0}


def on_sample(s):
    t = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
    sample = s.emit("pull-sample")
    buf = sample.get_buffer()
    caps = sample.get_caps().get_structure(0)
    w, h = caps.get_value("width"), caps.get_value("height")
    ok, info = buf.map(Gst.MapFlags.READ)
    if not ok:
        return Gst.FlowReturn.OK
    try:
        stride = len(info.data) // h
        a = np.frombuffer(info.data, dtype=np.uint8).reshape(h, stride)
        ys = ((2 * np.arange(GY) + 1) * h) // (2 * GY)
        xs = ((2 * np.arange(GX) + 1) * w) // (2 * GX)
        cols = (xs[:, None] * 4 + np.arange(3)[None, :]).reshape(-1)
        px = a[ys][:, cols].astype(np.int16).reshape(GY, GX, 3)
    finally:
        buf.unmap(info)
    prev = state["prev"]
    changed = GX * GY if prev is None else int((np.abs(px - prev).sum(axis=2) > 24).sum())
    state["prev"] = px
    state["n"] += 1
    log.write("C %d %d %d %d\n" % (t, changed, GX * GY, buf.pts if buf.pts != Gst.CLOCK_TIME_NONE else -1))
    return Gst.FlowReturn.OK


sink.connect("new-sample", on_sample)
pipe.set_state(Gst.State.PLAYING)
loop = GLib.MainLoop()
GLib.timeout_add(int(seconds * 1000), loop.quit)
loop.run()
pipe.set_state(Gst.State.NULL)
log.close()
print("frames", state["n"], file=sys.stderr)
