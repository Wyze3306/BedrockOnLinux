#!/usr/bin/env python3
"""Rank where a Wine game thread spends its CPU time, from a perf recording.

perf resolves Wine's unix side (ntdll.so, win32u.so, winex11.so, ...) and the
kernel by itself, but not the PE side: Wine's own .dll files carry DWARF that
perf does not read, and the game's code is decrypted into anonymous memory at
launch. This tool fills that gap and groups the time the way the question
"what does Wine cost this thread?" needs it.

    snapshot PID DIR      while the game runs: copy /proc/PID/maps and, for
                          every 64-bit PE image, its runtime base and its
                          .pdata (function starts) read from live memory
    symfs DIR             build a --symfs tree that maps the pressure-vessel
                          container's paths (/run/host, /usr) back to the host
    report TXT DIR        rank a `perf script` dump using the snapshot

Recording (perf_event_paranoid <= 1), for the game's main thread TID:

    tools/wine-perf-report.py snapshot PID snap
    perf record -e cycles -F 999 --call-graph dwarf,16384 -t TID -o main.data -- sleep 15
    tools/wine-perf-report.py symfs symfs
    perf script --symfs=symfs --kallsyms=/proc/kallsyms -i main.data \\
        -F tid,time,period,event,ip,sym,symoff,dso --no-inline > main.txt
    tools/wine-perf-report.py report main.txt snap [--top N] [--json OUT]

DWARF unwinding stops at __wine_syscall_dispatcher, where Wine switches from
the PE stack to the unix one, so each sample of the unix side is charged to the
Win32 entry point it serves (NtUserPeekMessage, NtQueryInformationProcess...),
kernel time included. PE frames are leaves only: inlined helpers are reported
as outer<inner>.
"""
import bisect
import collections
import glob
import json
import os
import re
import struct
import subprocess
import sys

GAME_EXE = "minecraft.windows.exe"
VKD3D = {"d3d12core.dll", "d3d12.dll", "dxgi.dll"}
DISPATCHERS = {"__wine_syscall_dispatcher", "__wine_unix_call_dispatcher"}
HDR = re.compile(r"^\s*(\d+)\s+([\d.]+):\s+(\d+)\s+(\S+):?\s*$")
FRAME = re.compile(r"^\s+([0-9a-f]+)\s+(.*?)\s+\((.*)\)\s*$")


# ---------------------------------------------------------------- snapshot

def snapshot(pid, out):
    os.makedirs(out, exist_ok=True)
    maps = open(f"/proc/{pid}/maps").read()
    with open(os.path.join(out, "maps"), "w") as f:
        f.write(maps)
    images = []
    with open(f"/proc/{pid}/mem", "rb") as mem:
        for line in maps.splitlines():
            parts = line.split(None, 5)
            if len(parts) < 6 or int(parts[2], 16) != 0:
                continue
            path = parts[5].strip()
            if not path.lower().endswith((".dll", ".exe", ".drv", ".sys")):
                continue
            start = int(parts[0].split("-")[0], 16)
            try:
                mem.seek(start)
                hdr = mem.read(4096)
            except OSError:
                continue
            if hdr[:2] != b"MZ":
                continue
            pe = struct.unpack_from("<I", hdr, 0x3C)[0]
            opt = pe + 24
            if hdr[pe:pe + 4] != b"PE\0\0" or struct.unpack_from("<H", hdr, opt)[0] != 0x20B:
                continue
            size = struct.unpack_from("<I", hdr, opt + 56)[0]
            exc_rva, exc_size = struct.unpack_from("<II", hdr, opt + 112 + 3 * 8)
            img = {"path": path, "base": start, "size": size, "pdata": None}
            if exc_rva and exc_size:
                try:
                    mem.seek(start + exc_rva)
                    raw = mem.read(exc_size)
                    fn = os.path.join(out, f"pdata-{os.path.basename(path)}.bin")
                    with open(fn, "wb") as f:
                        f.write(raw)
                    img["pdata"] = fn
                except OSError:
                    pass
            images.append(img)
    with open(os.path.join(out, "images.json"), "w") as f:
        json.dump(images, f, indent=1)
    print(f"{len(images)} PE images")


def make_symfs(out):
    """pressure-vessel shows the host's libraries under /run/host and the
    Steam Runtime's under /usr; Wine itself keeps its host path."""
    runtimes = sorted(glob.glob(os.path.expanduser(
        "~/.local/share/bedrock-on-linux/umu/steamrt3/sniper_platform_*/files")))
    os.makedirs(os.path.join(out, "run"), exist_ok=True)
    links = {"run/host": "/", "home": "/home", "tmp": "/tmp"}
    if runtimes:
        links["usr"] = runtimes[-1]
    for name, target in links.items():
        path = os.path.join(out, name)
        if os.path.islink(path):
            os.unlink(path)
        os.symlink(target, path)
    print("symfs:", ", ".join(f"/{k} -> {v}" for k, v in links.items()))


# ------------------------------------------------------------------ report

class Symbolizer:
    def __init__(self, snap):
        self.images = sorted(json.load(open(os.path.join(snap, "images.json"))),
                             key=lambda m: m["base"])
        self.starts = [m["base"] for m in self.images]
        self.debug, self.pdata, self.exports, self.bases = {}, {}, {}, {}
        self.pending = collections.defaultdict(set)
        self.resolved = {}

    def image_of(self, ip):
        k = bisect.bisect_right(self.starts, ip) - 1
        if k >= 0 and ip < self.images[k]["base"] + self.images[k]["size"]:
            return self.images[k]
        return None

    def file_image_base(self, path):
        # The loader rewrites the in-memory ImageBase when it relocates.
        if path not in self.bases:
            with open(path, "rb") as f:
                hdr = f.read(4096)
            pe = struct.unpack_from("<I", hdr, 0x3C)[0]
            self.bases[path] = struct.unpack_from("<Q", hdr, pe + 48)[0]
        return self.bases[path]

    def has_debug(self, path):
        if path not in self.debug:
            try:
                out = subprocess.run(["objdump", "-h", path], capture_output=True,
                                     text=True).stdout
            except OSError:
                out = ""
            self.debug[path] = ".debug_info" in out
        return self.debug[path]

    def function_starts(self, img):
        if img["path"] not in self.pdata:
            starts = []
            if img.get("pdata"):
                raw = open(img["pdata"], "rb").read()
                for j in range(0, len(raw) - 11, 12):
                    b, e, _ = struct.unpack_from("<III", raw, j)
                    if b:
                        starts.append((b, e))
            self.pdata[img["path"]] = sorted(starts)
        return self.pdata[img["path"]]

    def export_table(self, path):
        if path in self.exports:
            return self.exports[path]
        res = []
        try:
            data = open(path, "rb").read()
            pe = struct.unpack_from("<I", data, 0x3C)[0]
            opt = pe + 24
            nsec = struct.unpack_from("<H", data, pe + 6)[0]
            optsize = struct.unpack_from("<H", data, pe + 20)[0]
            secs = []
            for s in range(nsec):
                o = opt + optsize + s * 40
                vsz, va = struct.unpack_from("<II", data, o + 8)
                secs.append((va, vsz, struct.unpack_from("<I", data, o + 20)[0]))

            def off(rva):
                for va, vsz, rp in secs:
                    if va <= rva < va + max(vsz, 1):
                        return rva - va + rp
                raise ValueError(rva)
            erva = struct.unpack_from("<I", data, opt + 112)[0]
            if erva:
                eo = off(erva)
                nfun, nnames, afun, anames, aord = struct.unpack_from("<IIIII", data, eo + 20)
                funcs = [struct.unpack_from("<I", data, off(afun) + 4 * k)[0]
                         for k in range(nfun)]
                for k in range(nnames):
                    no = off(struct.unpack_from("<I", data, off(anames) + 4 * k)[0])
                    ordinal = struct.unpack_from("<H", data, off(aord) + 2 * k)[0]
                    res.append((funcs[ordinal],
                                data[no:data.index(b"\0", no)].decode(errors="replace")))
        except (OSError, ValueError, struct.error):
            pass
        self.exports[path] = sorted(res)
        return self.exports[path]

    def nearest_export(self, path, rva):
        ex = self.export_table(path)
        k = bisect.bisect_right(ex, (rva, "\xff")) - 1
        return f"{ex[k][1]}+0x{rva - ex[k][0]:x}?" if k >= 0 else f"rva_{rva:x}"

    def queue(self, ip):
        """(module, name or deferred addr2line key) for a PE address."""
        img = self.image_of(ip)
        if not img:
            return None, None
        path, mod = img["path"], os.path.basename(img["path"])
        rva = ip - img["base"]
        if mod.lower() != GAME_EXE and self.has_debug(path):
            va = self.file_image_base(path) + rva
            self.pending[path].add(va)
            return mod, (path, va)
        starts = self.function_starts(img)
        k = bisect.bisect_right(starts, (rva, 1 << 40)) - 1
        if k >= 0 and starts[k][0] <= rva < starts[k][1]:
            return mod, f"sub_{starts[k][0]:x}"
        if mod.lower() in VKD3D:
            return mod, f"rva_{rva & ~0xff:x}"
        return mod, self.nearest_export(path, rva)

    def run_addr2line(self):
        for path, vas in self.pending.items():
            vas = sorted(vas)
            out = subprocess.run(["addr2line", "-a", "-f", "-i", "-e", path],
                                 input="\n".join(hex(v) for v in vas),
                                 capture_output=True, text=True).stdout.splitlines()
            chains, cur = {}, None
            k = 0
            while k < len(out):
                if out[k].startswith("0x"):
                    cur = int(out[k], 16)
                    chains[cur] = []
                    k += 1
                else:
                    chains[cur].append(out[k])
                    k += 2  # function, then file:line
            for va in vas:
                names = [n for n in chains.get(va, []) if n != "??"]
                if not names:
                    name = self.nearest_export(path, va - self.file_image_base(path))
                elif len(names) == 1:
                    name = names[0]
                else:
                    name = f"{names[-1]}<{names[0]}>"
                self.resolved[(path, va)] = name

    def name(self, deferred):
        return self.resolved[deferred] if isinstance(deferred, tuple) else deferred


def category(mod, wine_pe):
    m = mod.lower()
    if m in ("[kernel]", "[kernel.kallsyms]") or m.startswith("[nvidia") or m.endswith(".ko.xz"):
        return "kernel"
    if m == GAME_EXE or m.startswith("perf-"):
        return "game"
    if m in VKD3D:
        return "vkd3d-proton"
    if m.startswith(("libnvidia", "libvulkan", "libglx_nvidia")):
        return "nvidia-vulkan"
    if m.endswith(".so") and not m.startswith("lib"):
        return "wine-unix"
    if m.startswith(("libc.so", "libm.so", "ld-linux", "libpthread")):
        return "libc"
    if m.startswith(("libx11", "libxcb", "libxext", "libxi", "libxrandr")):
        return "x11-libs"
    if m.endswith((".dll", ".drv", ".exe")):
        return "wine-pe" if m in wine_pe else "game-dll"
    return "other"


def parse(path):
    samples, cur = [], None
    for line in open(path, errors="replace"):
        m = None if line.startswith("\t") else HDR.match(line)
        if m:
            if cur:
                samples.append(cur)
            cur = {"tid": int(m.group(1)), "period": int(m.group(3)), "frames": []}
            continue
        m = FRAME.match(line)
        if m and cur is not None:
            cur["frames"].append([int(m.group(1), 16), m.group(2), m.group(3)])
    if cur:
        samples.append(cur)
    return samples


def report(src, snap, top=40, json_out=None, tids=None):
    sym = Symbolizer(snap)
    samples = [s for s in parse(src) if not tids or s["tid"] in tids]
    for s in samples:
        for f in s["frames"]:
            ip, name, dso = f
            if ip >= 0xffff800000000000:
                f[2] = "[kernel]"
            elif (name.startswith("[unknown]") or "/perf-" in dso or dso in ("[unknown]", "//anon")
                  or dso.lower().endswith((".dll", ".exe", ".drv"))):
                mod, deferred = sym.queue(ip)
                if mod:
                    f[1], f[2] = deferred, mod
            f[2] = os.path.basename(f[2])
    sym.run_addr2line()
    for s in samples:
        for f in s["frames"]:
            f[1] = re.sub(r"\+0x[0-9a-f]+$", "", sym.name(f[1]))
    wine_pe = {os.path.basename(i["path"]).lower() for i in sym.images
               if ("/lib/wine/" in i["path"] or "/drive_c/windows/" in i["path"])
               and os.path.basename(i["path"]).lower() not in VKD3D}

    total = sum(s["period"] for s in samples) or 1
    leaf_cat, owner_cat, leaf_sym = (collections.Counter() for _ in range(3))
    entries, kernel_by, libc_by, game_fn = (collections.Counter() for _ in range(4))
    for s in samples:
        w, fr = s["period"], s["frames"]
        if not fr:
            leaf_cat["unknown"] += w
            owner_cat["unknown"] += w
            continue
        cats = [category(f[2], wine_pe) for f in fr]
        leaf_cat[cats[0]] += w
        leaf_sym[(cats[0], fr[0][2], fr[0][1])] += w
        owner = next((k for k, c in enumerate(cats) if c not in ("kernel", "libc")), None)
        owner_cat[cats[owner] if owner is not None else cats[0]] += w
        if cats[0] in ("kernel", "libc"):
            user = next((f for f, c in zip(fr, cats) if c != "kernel"), None)
            caller = fr[owner][1] if owner is not None else "?"
            (kernel_by if cats[0] == "kernel" else libc_by)[
                ((user[1] if user else "(kernel thread)"), caller)] += w
        if cats[0] == "game":
            game_fn[fr[0][1]] += w
        for k, f in enumerate(fr):
            if f[1] in DISPATCHERS and k:
                entries[(fr[k - 1][2], fr[k - 1][1])] += w
                break

    def pct(x):
        return 100.0 * x / total

    def table(title, counter, n, fmt):
        print(f"\n== {title}")
        for key, w in counter.most_common(n):
            print(f"  {pct(w):6.2f}%  {fmt(key)}")

    print(f"{len(samples)} samples, threads {sorted({s['tid'] for s in samples})[:8]}")
    table("self time by category (leaf frame)", leaf_cat, 20, str)
    table("time by owner (kernel and libc charged to their caller)", owner_cat, 20, str)
    table(f"top {top} leaf symbols", leaf_sym, top, lambda k: f"{k[0]:13s} {k[1]:24s} {k[2]}")
    table("Wine unix entry points, inclusive of libc and kernel below them", entries, 30,
          lambda k: f"{k[0]:16s} {k[1]}")
    table("kernel time by (user-side wrapper, calling function)", kernel_by, 25,
          lambda k: f"{k[0]:32s} <- {k[1]}")
    table("libc time by (function, caller)", libc_by, 15, lambda k: f"{k[0]:32s} <- {k[1]}")
    table("hottest game functions (self, by .pdata start)", game_fn, 15, str)
    if json_out:
        with open(json_out, "w") as f:
            json.dump({"samples": len(samples),
                       "self_cat": {k: pct(v) for k, v in leaf_cat.items()},
                       "owner_cat": {k: pct(v) for k, v in owner_cat.items()},
                       "entries": {f"{a}!{b}": pct(v) for (a, b), v in entries.most_common(80)},
                       "top": [[c, m, n, pct(w)] for (c, m, n), w in leaf_sym.most_common(300)],
                       "kernel": {f"{a} <- {b}": pct(v) for (a, b), v in kernel_by.most_common(80)}},
                      f, indent=1)


def main(argv):
    if len(argv) >= 3 and argv[0] == "snapshot":
        snapshot(int(argv[1]), argv[2])
    elif len(argv) >= 2 and argv[0] == "symfs":
        make_symfs(argv[1])
    elif len(argv) >= 3 and argv[0] == "report":
        opts = dict(zip(argv[3::2], argv[4::2]))
        report(argv[1], argv[2], top=int(opts.get("--top", 40)), json_out=opts.get("--json"),
               tids={int(t) for t in opts["--tid"].split(",")} if "--tid" in opts else None)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
