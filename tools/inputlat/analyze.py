"""Pair injected motions with the first moved frame.

usage: analyze.py INJ_LOG LAYER_LOG [APP_LOG|-] [--pw PW_LOG] [--thr FRACTION] [--label L] [--json OUT]

For each injection at T0 (I line), the first layer frame (F line) whose
vkQueuePresentKHR came after T0 with at least THR of the sampled points
changed is the response. Reported per injection, in ms after T0:
  qp    the moved frame's vkQueuePresentKHR call
  gpu   the reader saw its copy finish (>= the frame's GPU completion)
  disp  vkWaitForPresentKHR for its present id returned (display), when known
  comp  the first frame gamescope composited (its PipeWire output) that moved
An injection is rejected when the frames before it were not still (the scene
moved on its own) or when nothing moved within 400 ms.
"""
import json
import statistics as st
import sys


def load(path, keys):
    rows = {k: [] for k in keys}
    with open(path) as f:
        for line in f:
            p = line.split()
            if p and p[0] in rows:
                rows[p[0]].append(p[1:])
    return rows


def pct(v, q):
    v = sorted(v)
    if not v:
        return float("nan")
    i = (len(v) - 1) * q
    lo = int(i)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (i - lo)


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    thr = 0.25
    label = ""
    out_json = None
    pw_path = None
    for i, a in enumerate(argv):
        if a == "--pw":
            pw_path = argv[i + 1]
        if a == "--thr":
            thr = float(argv[i + 1])
        if a == "--label":
            label = argv[i + 1]
        if a == "--json":
            out_json = argv[i + 1]
    args = [a for a in args if a not in (str(thr), label, out_json, pw_path)]
    comp = []
    if pw_path:
        for r in load(pw_path, ["C"])["C"]:
            comp.append((int(r[0]), int(r[1]) / int(r[2])))
        comp.sort()
    inj = [int(r[0]) for r in load(args[0], ["I"])["I"]]
    lay = load(args[1], ["F", "W", "A"])
    frames = []
    for r in lay["F"]:
        seq, pid, tqp, tqr, tgpu, ch, npts, w, h = r
        if int(ch) < 0:
            continue
        frames.append((int(tqp), int(pid), int(tgpu), int(ch) / int(npts), int(tqr)))
    frames.sort()
    waits = {}
    for r in lay["W"]:
        pid, t0, t1, res = r
        waits[int(pid)] = (int(t0), int(t1))
    app_e = []
    if len(args) > 2 and args[2] != "-":
        app = load(args[2], ["E"])
        app_e = sorted(int(r[0]) for r in app["E"])

    res = []
    rejected = {"noisy": 0, "none": 0}
    fi = 0
    for k, t0 in enumerate(inj):
        while fi < len(frames) and frames[fi][0] < t0:
            fi += 1
        # Still scene: the 3 frames before the injection barely changed.
        before = [f[3] for f in frames[max(0, fi - 3):fi]]
        if before and max(before) >= thr / 2:
            rejected["noisy"] += 1
            continue
        hit = None
        for f in frames[fi:]:
            if f[0] - t0 > 400e6:
                break
            if f[3] >= thr:
                hit = f
                break
        if not hit:
            rejected["none"] += 1
            continue
        tqp, pid, tgpu, frac, tqr = hit
        row = {"qp": (tqp - t0) / 1e6, "gpu": (tgpu - t0) / 1e6, "frac": frac}
        if pid and pid in waits:
            w0, w1 = waits[pid]
            row["disp"] = (w1 - t0) / 1e6
            row["disp_late"] = (w1 - w0) < 200e3  # returned at once: completion <= call
        if comp:
            import bisect
            j = bisect.bisect_left(comp, (t0, -1.0))
            c = next((c for c in comp[j:j + 40] if c[1] >= thr), None)
            if c and c[0] - t0 < 400e6:
                row["comp"] = (c[0] - t0) / 1e6
        if app_e:
            import bisect
            j = bisect.bisect_left(app_e, t0)
            if j < len(app_e) and app_e[j] - t0 < 200e6:
                row["app_event"] = (app_e[j] - t0) / 1e6
        res.append(row)

    def summ(key):
        v = [r[key] for r in res if key in r]
        if not v:
            return None
        return {"n": len(v), "mean": st.mean(v), "median": st.median(v), "p10": pct(v, .1),
                "p90": pct(v, .9), "min": min(v), "max": max(v),
                "sd": st.pstdev(v)}

    # Frame interval from presents and display cadence from waits.
    qps = [f[0] for f in frames]
    dq = [(b - a) / 1e6 for a, b in zip(qps, qps[1:]) if 0 < b - a < 200e6]
    lat_qp_disp = []
    for f in frames:
        if f[1] in waits:
            lat_qp_disp.append((waits[f[1]][1] - f[0]) / 1e6)
    summary = {"label": label, "injections": len(inj), "used": len(res), "rejected": rejected,
               "qp": summ("qp"), "gpu": summ("gpu"), "disp": summ("disp"), "comp": summ("comp"), "app_event": summ("app_event"),
               "frame_ms_median": st.median(dq) if dq else None,
               "present_to_display_ms_median": st.median(lat_qp_disp) if lat_qp_disp else None,
               "disp_late": sum(1 for r in res if r.get("disp_late"))}
    print(json.dumps(summary, indent=1))
    if out_json:
        with open(out_json, "w") as f:
            json.dump({"summary": summary, "rows": res}, f, indent=1)


if __name__ == "__main__":
    main(sys.argv[1:])
