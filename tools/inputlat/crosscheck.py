"""Known case: does the probe pick the frame the app itself flipped?

usage: crosscheck.py INJ_LOG LAYER_LOG APP_LOG [APP_ERR]
For each injection: the app's first flipped frame after it (Q ... flipped=1),
the probe's first moved frame, and the gap between their present times.
With APP_ERR (present-wait D lines), the app's own display time too.
"""
import bisect
import statistics as st
import sys

inj = [int(l.split()[1]) for l in open(sys.argv[1]) if l.startswith("I ")]
F = []
W = {}
for l in open(sys.argv[2]):
    p = l.split()
    if p[0] == "F" and int(p[6]) >= 0:
        F.append((int(p[3]), int(p[6]) / int(p[7]), int(p[2])))
    if p[0] == "W":
        W[int(p[1])] = int(p[3])
F.sort()
Q = []
for l in open(sys.argv[3]):
    p = l.split()
    if p[0] == "Q":
        Q.append((int(p[2]), int(p[4]), int(p[1])))
Q.sort()
D = {}
if len(sys.argv) > 4:
    for l in open(sys.argv[4]):
        p = l.split()
        if p and p[0] == "D":
            D[int(p[1])] = int(p[2])
fq = [f[0] for f in F]
qq = [q[0] for q in Q]
gaps, same, disp_gap, app_disp = [], 0, [], []
for t0 in inj:
    i = bisect.bisect_left(qq, t0)
    app = next((q for q in Q[i:] if q[1] == 1), None)
    j = bisect.bisect_left(fq, t0)
    hit = next((f for f in F[j:] if f[1] >= 0.25), None)
    if not app or not hit:
        print("missing", t0)
        continue
    gaps.append((hit[0] - app[0]) / 1e6)
    same += abs(hit[0] - app[0]) < 1e6
    pid = app[2] + 1
    if pid in D:
        app_disp.append((D[pid] - t0) / 1e6)
    if hit[2] in W and pid in D:
        disp_gap.append((W[hit[2]] - D[pid]) / 1e6)
print("injections %d, probe frame == app's flipped frame: %d" % (len(inj), same))
print("probe qp - app qp (ms): median %.4f, max |gap| %.4f" % (st.median(gaps), max(abs(g) for g in gaps)))
if app_disp:
    print("app's own present-wait display latency: median %.2f ms (n=%d)" % (st.median(app_disp), len(app_disp)))
if disp_gap:
    print("probe W - app D (ms): median %.4f, max %.4f" % (st.median(disp_gap), max(abs(g) for g in disp_gap)))
