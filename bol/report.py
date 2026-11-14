"""bol.report — "Report a problem": what is collected, and how it leaves.

A bug report that says "it lags" cannot be answered. This gathers what the
answer needs -- the launcher and how it was installed, the pinned and the
installed components, the Minecraft build, the distribution, the session,
the graphics card and its driver, the settings that change behaviour and the
tail of the logs -- so one click sends all of it with the player's own words.

Three rules hold the design together:

* Nothing is personal. Every piece is passed through bol.scrub as it is
  collected, so what the preview shows is what is sent, and the finished text
  goes through it once more on the way out (Report._out). There is no other
  way out of this module.

* Nothing leaves without a click. collect() only reads this machine. send()
  and open_in_browser() are the two ways out, and both are called from a
  button or a confirmed command, never from a timer.

* The player sees where it goes. destination() names the host, and the
  window and the command print it before anything is sent.

How it leaves: the launcher cannot hold a GitHub credential, so either a
small relay files the issue on its behalf (relay/, RELAY_URL below), or --
always available, and the only way until a relay is deployed -- the browser
opens GitHub's own new-issue form with the summary filled in and the full
report saved to a file to attach.
"""
# SPDX-License-Identifier: MIT

import json
import os
import platform
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit

from . import scrub
from .config import (
    APP, DATA, LOGS, OPENSSL_XCURL_REV, PRETTY, SELF_REPO, UMU_VERSION,
    VERSION, WINEGDK_BUILD_REV, XODUS_DIR, XODUS_REV, XODUS_WEBVIEW_REV,
)

# The relay that files reports as issues, once one is deployed (docs/
# report-relay.md). Empty: the browser is the only way out. A host named here
# is listed by bol.netpolicy with every other host the launcher contacts, and
# tests/test_netpolicy.py then requires the README to name it too.
RELAY_URL = ""
# One run's override, for a packager's own relay or a test: a URL, or "off".
RELAY_ENV = "BOL_REPORT_RELAY"

ISSUE_REPO = SELF_REPO
ISSUES_URL = f"https://github.com/{ISSUE_REPO}/issues"
PROTOCOL = 1

MAX_TITLE = 120
MAX_COMMENT = 4000
# What a relay accepts in one request, and what the launcher never exceeds.
MAX_PAYLOAD = 400_000
# GitHub answers 414 a little past 8 kB of URL; stay well under.
MAX_URL = 6000

# The logs a report may carry, and how much of each: (name, title, bytes,
# included unless unticked). Anything else in the logs folder stays there --
# notably xcurl.log, which records the bodies of Xbox Live answers.
LOG_FILES = (
    ("minecraft.log", "Game log (minecraft.log)", 96_000, True),
    ("proton.log", "Engine log (proton.log)", 96_000, True),
    ("minecraft.log.prev", "Previous game log", 32_000, False),
    ("store-download.log", "Minecraft download log", 24_000, True),
    ("store-login.log", "Store sign-in log", 16_000, True),
    ("native-login.log", "Online sign-in log", 16_000, True),
    ("injector.log", "DLL injector log", 16_000, True),
    ("slr-locales.log", "Container locales log", 8_000, True),
    ("virtual-desktop.log", "Virtual desktop log", 8_000, True),
    ("fullscreen-nudge.log", "Fullscreen helper log", 8_000, True),
)
_HEAD_LINES = 40

# Launcher settings worth a line in a report. Anything not named here is not
# reported: a path, a URL or a name could be in it.
SETTINGS_REPORTED = (
    "proton_source", "renderer", "limit_frame_rate", "ray_tracing",
    "input_backend", "confine_cursor", "diagnostics", "close_on_launch",
    "auto_update_game", "xbl_presence", "injector_auto_enable",
    "injector_dll_type", "injector_delay", "controller_nav", "mc_edition",
    "mc_version", "network_updates", "update_channel", "warn_offline_play",
)
_SCALAR = (str, int, float, bool)


class ReportError(Exception):
    """The report could not be sent; the message is for the player."""


@dataclass
class Piece:
    """One part of a report, already scrubbed. `included` is the checkbox."""
    key: str
    title: str
    text: str
    kind: str = "info"            # "info": in the summary; "log": attached
    included: bool = True
    note: str = ""


@dataclass
class Report:
    scrubber: scrub.Scrubber
    pieces: list = field(default_factory=list)
    title: str = ""
    comment: str = ""

    # ------------------------------------------------------- the only exit
    def _out(self, text, prose=False):
        """Everything that leaves goes through here, whatever built it."""
        return self.scrubber.text(text, prose=prose)

    def add(self, key, title, raw, kind="info", included=True, note=""):
        text = self._out(raw).strip()
        if text:
            self.pieces.append(Piece(key, title, text, kind, included, note))

    def set_message(self, title, comment):
        """The player's own words, cleaned like the rest."""
        title = " ".join(str(title or "").split())
        self.title = self._out(title, prose=True)[:MAX_TITLE].strip()
        self.comment = self._out(str(comment or "").strip(),
                                 prose=True)[:MAX_COMMENT].strip()

    def included(self, kind=None):
        return [piece for piece in self.pieces if piece.included
                and (kind is None or piece.kind == kind)]

    def piece(self, key):
        return next((p for p in self.pieces if p.key == key), None)

    # ------------------------------------------------------------ renders
    def summary(self):
        """The system information, as one block of plain text."""
        blocks = [f"== {piece.title} ==\n{piece.text}"
                  for piece in self.included("info")]
        return self._out("\n\n".join(blocks))

    def attachments(self):
        """[(name, text)] for the logs, trimmed to what one request holds."""
        budget = MAX_PAYLOAD - len(self.summary()) - len(self.comment) - 2000
        out = []
        for piece in self.included("log"):
            text = self._out(piece.text)
            if len(text) > budget:
                text = _tail(text, max(budget, 0))
            if not text:
                continue
            budget -= len(text)
            out.append((piece.title, text))
        return out

    def full_text(self):
        """The whole report as one text file, for the browser route."""
        parts = [f"{PRETTY} problem report", "",
                 "Title: " + (self.title or "(none)"), "",
                 "What happened:", self.comment or "(no comment)", "",
                 self.summary()]
        for name, text in self.attachments():
            parts += ["", "=" * 72, name, "=" * 72, text]
        return self._out("\n".join(parts), prose=True) + "\n"

    def payload(self):
        """What the relay receives."""
        return {
            "v": PROTOCOL,
            "launcher": VERSION,
            "title": self._out(self.title, prose=True),
            "comment": self._out(self.comment, prose=True),
            "summary": self.summary(),
            "attachments": [{"name": name, "text": text}
                            for name, text in self.attachments()],
        }

    def removed(self):
        """'12 names, 3 tokens, 5 lines removed' -- what the pass took out."""
        stats = self.scrubber.stats
        return ", ".join(f"{count} {kind}" for kind, count
                         in sorted(stats.items()) if count)


# ====================================================================
# Who this machine belongs to: what the scrubber is told to look for
# ====================================================================

def _read(path, limit=200_000):
    try:
        with open(path, "rb") as handle:
            return handle.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def local_identity(data=None, prefix=None, environ=None):
    """[(value, placeholder)] of everything personal this machine can name.

    Reads names only -- never to report them, only to remove them.
    """
    environ = os.environ if environ is None else environ
    data = Path(DATA if data is None else data)
    known = []

    def add(value, placeholder):
        value = str(value or "").strip()
        if value:
            known.append((value, placeholder))

    home = Path.home()
    add(home, "~")
    try:
        add(home.resolve(), "~")
    except OSError:
        pass
    # The launcher's own folder, wherever it was moved to: a relocated one
    # is named after a disk or a folder of the player's.
    add(data, "<data>")
    try:
        add(data.resolve(), "<data>")
    except OSError:
        pass

    for name in ("USER", "LOGNAME", "SUDO_USER"):
        add(environ.get(name), "<user>")
    try:
        import pwd
        entry = pwd.getpwuid(os.getuid())
        add(entry.pw_name, "<user>")
        full_name = entry.pw_gecos.split(",")[0].strip()
        add(full_name, "<user>")
        for word in full_name.split():
            if len(word) >= 4:
                add(word, "<user>")
    except (ImportError, KeyError, OSError):
        pass

    hosts = {platform.node(), environ.get("HOSTNAME", ""),
             _read("/etc/hostname").strip()}
    for host in hosts:
        add(host, "<host>")
        add(host.split(".")[0], "<host>")
    add(_read("/etc/machine-id").strip(), "<machine-id>")

    # The Xbox account: the gamertag and the XUID by name, and every other
    # string of the token cache as something to redact wherever it shows up.
    try:
        account = json.loads(_read(data / "winegdk-preauth" / "device.json",
                                   2_000_000))
    except ValueError:
        account = {}
    if isinstance(account, dict):
        for key, value in account.items():
            if not isinstance(value, str) or len(value) < 6:
                continue
            if "gamertag" in key:
                add(value, "<gamertag>")
            elif "xuid" in key:
                add(value, "<xuid>")
            elif not key.endswith(("_expiry", "_rp", "obtained", "age_group")):
                add(value, "<redacted>")
    add(_read(data / "winegdk-preauth" / "device-id.txt").strip(), "<device>")

    try:
        from .profiles import current_profile_info, list_profiles
        profiles = list_profiles() + [current_profile_info()]
    except Exception:  # noqa: BLE001 - a broken profile must not stop a report
        profiles = []
    for profile in profiles:
        if profile.get("slug") == "default":
            continue
        add(profile.get("name"), "<profile>")
        add(profile.get("slug"), "<profile>")

    for name in _world_names(prefix):
        add(name, "<world>")
    for name in _server_names(prefix):
        add(name, "<server>")
    return known


def _game_folders(prefix):
    if prefix is None:
        try:
            from .prefix import active_prefix
            prefix = active_prefix()
        except Exception:  # noqa: BLE001
            return []
    try:
        from .perfcheck import find_game_data_dirs
        return [Path(folder).parent for folder in find_game_data_dirs(prefix)]
    except Exception:  # noqa: BLE001
        return []


def _world_names(prefix=None):
    names = set()
    for folder in _game_folders(prefix):
        for kind in ("minecraftWorlds", "world_templates"):
            try:
                worlds = list((folder / kind).iterdir())[:500]
            except OSError:
                continue
            for world in worlds:
                name = _read(world / "levelname.txt", 400).strip()
                if name:
                    names.add(name)
    return names


def _server_names(prefix=None):
    """Names and addresses of the servers the player added in the game."""
    names = set()
    for folder in _game_folders(prefix):
        text = _read(folder / "minecraftpe" / "external_servers.txt")
        for line in text.splitlines():
            # "<id>:<name>:<address>:<port>:<timestamp>"
            parts = line.split(":")
            if len(parts) >= 4:
                names.update(part.strip() for part in parts[1:-2])
    return names


# ====================================================================
# What is collected
# ====================================================================

INSTALL_KINDS = {
    "appimage": "AppImage", "file": "portable .pyz", "deb": ".deb package",
    "rpm": ".rpm package", "flatpak": "Flatpak", "nix": "Nix",
    "git": "git checkout", "system": "distribution package or pip",
}


def _lines(pairs):
    width = max((len(label) for label, _ in pairs), default=0)
    return "\n".join(f"{label:<{width}} : {value}" for label, value in pairs
                     if value not in (None, ""))


def _safely(function, *args, **kwargs):
    """A collector's answer, or why it had none. Never raises: one broken
    probe must not cost the report the others."""
    try:
        return function(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - reported in place of the value
        return f"(unavailable: {type(exc).__name__}: {exc})"


def _os_release(environ=None):
    """The distribution's name. Inside Flatpak /etc/os-release describes the
    runtime; the host's is mounted at /run/host."""
    for path in ("/run/host/os-release", "/run/host/etc/os-release",
                 "/etc/os-release", "/usr/lib/os-release"):
        fields = dict(re.findall(r'^([A-Z_]+)="?([^"\n]*)"?$', _read(path),
                                 re.M))
        if fields:
            name = fields.get("PRETTY_NAME") or fields.get("NAME") or "?"
            extra = [fields[k] for k in ("VARIANT_ID", "BUILD_ID")
                     if fields.get(k)]
            return name + (f" ({', '.join(extra)})" if extra else "")
    return "unknown"


def session_summary(environ=None):
    environ = os.environ if environ is None else environ
    desktop = (environ.get("XDG_CURRENT_DESKTOP")
               or environ.get("DESKTOP_SESSION") or "unknown")
    kind = environ.get("XDG_SESSION_TYPE") or (
        "wayland" if environ.get("WAYLAND_DISPLAY") else
        "x11" if environ.get("DISPLAY") else "none")
    extras = []
    if environ.get("GAMESCOPE_WAYLAND_DISPLAY") or "gamescope" in desktop.lower():
        extras.append("inside Gamescope")
    if environ.get("SteamDeck") == "1" or environ.get("SteamOS") == "1":
        extras.append("SteamOS")
    if environ.get("WAYLAND_DISPLAY") and environ.get("DISPLAY"):
        extras.append("XWayland available")
    if environ.get("SSH_CONNECTION"):
        extras.append("over SSH")
    return desktop, kind + (" (" + ", ".join(extras) + ")" if extras else "")


def _cpu():
    info = _read("/proc/cpuinfo")
    model = re.search(r"^(?:model name|Model|cpu model)\s*:\s*(.+)$", info, re.M)
    return (f"{model.group(1).strip() if model else platform.machine()}"
            f", {os.cpu_count() or '?'} threads")


def _memory():
    from . import perfcheck
    total = perfcheck.total_memory_mib()
    available = perfcheck.available_memory_mib()
    swap = perfcheck.swap_used_mib()
    return (f"{total} MiB total, {available} MiB available, "
            f"{swap} MiB of swap in use")


def _collect_launcher():
    from . import netpolicy, update
    return _lines([
        ("launcher", f"{PRETTY} version {VERSION}"),
        ("install", _safely(lambda: INSTALL_KINDS.get(
            update.update_kind(), update.update_kind()))),
        ("python", platform.python_version()),
        ("updates", f"{netpolicy.mode() or 'not chosen'}, "
                    f"{netpolicy.channel()} channel"),
    ])


def _collect_system(environ=None):
    from . import hostarch
    desktop, session = session_summary(environ)
    return _lines([
        ("distribution", _os_release()),
        ("kernel", f"Linux {platform.release()}"),
        ("cpu", _safely(_cpu)),
        ("architecture", _safely(hostarch.summary)),
        ("memory", _safely(_memory)),
        ("desktop", desktop),
        ("session", session),
        ("locale", (os.environ.get("LC_ALL") or os.environ.get("LANG")
                    or "unset")),
    ])


def nvidia_driver_version(proc="/proc/driver/nvidia/version",
                          sysfs="/sys/module/nvidia/version"):
    version = _read(sysfs).strip()
    if version:
        return version
    match = re.search(r"Kernel Module(?: for \S+)?\s+(\d+(?:\.\d+)+)",
                      _read(proc))
    return match.group(1) if match else None


def mesa_version(lib_dirs=None, runner=None):
    """Mesa's version without loading it: asking Vulkan or OpenGL is exactly
    what a report about a broken driver cannot afford (see bol.gpus).

    Mesa 24.2 and later name the version in a library file; older ones are
    asked of the package manager.
    """
    lib_dirs = lib_dirs if lib_dirs is not None else (
        "/usr/lib/x86_64-linux-gnu", "/usr/lib64", "/usr/lib",
        "/usr/lib/aarch64-linux-gnu", "/run/host/usr/lib/x86_64-linux-gnu",
        "/run/host/usr/lib64", "/run/host/usr/lib",
        "/usr/lib/x86_64-linux-gnu/GL/default/lib", "/run/opengl-driver/lib")
    for folder in lib_dirs:
        try:
            names = sorted(Path(folder).glob("libgallium-*.so"))
        except OSError:
            continue
        for name in names:
            match = re.match(r"libgallium-(\d[\w.~+-]*)\.so$", name.name)
            if match:
                return match.group(1)
    runner = runner or (lambda argv: subprocess.run(
        argv, capture_output=True, text=True, timeout=5).stdout)
    for argv, pattern in (
            (["dpkg-query", "-W", "-f=${Version}", "libgl1-mesa-dri"],
             r"(\d+\.\d+[\w.~+-]*)"),
            (["rpm", "-q", "--qf", "%{VERSION}", "mesa-dri-drivers"],
             r"^(\d+\.\d+[\w.]*)$"),
            (["pacman", "-Q", "mesa"], r"mesa \d*:?(\d+\.\d+[\w.]*)")):
        if not shutil.which(argv[0]):
            continue
        try:
            match = re.search(pattern, runner(argv) or "")
        except (OSError, subprocess.SubprocessError):
            continue
        if match:
            return match.group(1)
    return None


_GRAPHICS_LOG_LINES = re.compile(
    r"vkd3d-proton - (?:applicationVersion|build)|Found device|"
    r"Device supports|DXR|Vulkan 1\.\d|driver version|Driver:|llvmpipe|"
    r"VKD3D_CONFIG|feature level|DXVK: v|Presenter|vk_khr|Skipping:", re.I)


def _collect_graphics(settings, logs_dir=None):
    from . import gpus, raytracing
    cards = gpus.list_gpus()
    chosen = gpus.chosen_gpu(settings, cards)
    rows = []
    for card in cards:
        marks = [mark for mark, on in (
            ("boot display", card.boot_vga), ("integrated", card.integrated),
            ("chosen in Settings", chosen is not None and card == chosen))
            if on]
        rows.append(("gpu", f"{card.name} [{card.vendor}:{card.device}], "
                            f"kernel driver {card.driver or 'none'}"
                            + (f" ({', '.join(marks)})" if marks else "")))
    if not rows:
        rows.append(("gpu", "none found in sysfs"))
    if chosen is None:
        rows.append(("gpu choice", "left to the system"))
    rows.append(("nvidia driver", _safely(nvidia_driver_version)))
    rows.append(("mesa version", _safely(mesa_version)))
    caps = _safely(raytracing.graphics_capabilities, logs_dir)
    if isinstance(caps, dict) and caps.get("observed"):
        rows.append(("last launch", f"DXR tier {caps.get('tier') or 'none'}, "
                                    f"VKD3D_CONFIG {caps.get('config') or 'unset'}"))
    else:
        rows.append(("last launch", "no graphics log yet"))
    head = (raytracing._graphics_log_head(logs_dir) or b"").decode(
        "utf-8", "replace")
    seen, facts = set(), []
    for line in head.splitlines():
        if _GRAPHICS_LOG_LINES.search(line):
            # "814.9:0118:011c:info:vkd3d-proton:function: the message"
            parts = line.split(":", 6)
            fact = (parts[-1] if len(parts) == 7 and line[:1].isdigit()
                    else line).strip()
            if fact and fact not in seen and len(facts) < 30:
                seen.add(fact)
                facts.append("    " + fact[:240])
    text = _lines(rows)
    if facts:
        text += "\nfrom the last launch log:\n" + "\n".join(facts)
    return text


def installed_engine(engine=None):
    """(revision, components) the installed engine's manifest declares."""
    if engine is None:
        from .proton import proton_path
        engine = proton_path()
    if not engine:
        return None, {}
    try:
        manifest = json.loads(_read(Path(engine) / "engine-manifest.json"))
    except ValueError:
        return None, {}
    if not isinstance(manifest, dict):
        return None, {}
    components = manifest.get("components")
    return manifest.get("build_rev"), (
        components if isinstance(components, dict) else {})


def _collect_components(settings):
    from . import vkd3d, xodus
    rev, components = installed_engine()
    engine_name = "none installed"
    if settings.get("proton"):
        engine_name = Path(settings["proton"]).name
    xodus_installed = _read(XODUS_DIR / ".rev").strip() or "not installed"
    rows = [
        ("engine pinned", WINEGDK_BUILD_REV),
        ("engine installed", f"{rev or 'no manifest'} ({engine_name}, "
                             f"source {settings.get('proton_source') or '?'})"),
        ("graphics payload", ", ".join(
            f"{name} {version}" for name, version in sorted(components.items())
            if isinstance(version, _SCALAR)) or "unknown"),
        ("vkd3d pinned for", vkd3d.REQUIRED_VARIANT_HASHES_BUILD_REV),
        ("xodus pinned", XODUS_REV),
        ("xodus in force", _safely(xodus.effective_rev)),
        ("xodus installed", xodus_installed),
        ("webview runtime", XODUS_WEBVIEW_REV),
        ("openssl xcurl", OPENSSL_XCURL_REV),
        ("umu-launcher", UMU_VERSION),
    ]
    return _lines(rows)


def _collect_game(settings):
    from . import games
    edition = games.selected_edition(settings)
    game_dir = (settings.get("game_dir") or "").strip()
    version = None
    if game_dir:
        folder = Path(game_dir)
        version = (games.mc_version_str(folder) if games.is_bedrock(edition)
                   else games.build_version(folder))
    rows = [
        ("edition", f"{edition.get('name', '?')} ({edition.get('id', '?')})"),
        ("installed build", f"Minecraft version {version}" if version
         else "none selected" if not game_dir else "version unreadable"),
        ("chosen version", settings.get("mc_version") or "newest"),
    ]
    builds = _safely(games.installed_builds, with_size=False)
    if isinstance(builds, list):
        rows.append(("builds on disk", "; ".join(
            f"{build['edition'] or 'legacy'} version {build['version']}"
            + ("" if build["playable"] else " (not playable)")
            + (" (in use)" if build["in_use"] else "")
            for build in builds[:12]) or "none"))
    else:
        rows.append(("builds on disk", builds))
    return _lines(rows)


def _collect_game_options():
    """The few in-game video settings a performance report turns on."""
    from . import perfcheck
    from .prefix import active_prefix
    path = perfcheck.find_options_file(active_prefix())
    if not path:
        return ""
    options = perfcheck.read_game_options(path)
    limit = perfcheck._option_int(options, "gfx_max_framerate")
    return _lines([
        ("render distance", f"{perfcheck.render_distance_chunks(options)} chunks"),
        ("vsync", "on" if perfcheck.vsync_is_on(options) else "off"),
        ("max framerate", {None: "not set", 0: "unlimited"}.get(
            limit, f"{limit} FPS")),
    ])


def _collect_settings(settings):
    from .util import _custom_env_pairs
    rows = []
    for key in SETTINGS_REPORTED:
        value = settings.get(key)
        if isinstance(value, _SCALAR) and value != "":
            rows.append((key, value))
    rows.append(("gpu", "a specific card" if settings.get("gpu")
                 not in (None, "", "auto") else "auto"))
    rows.append(("gamescope", "on" if settings.get("gamescope") else "off"))
    if settings.get("gamescope"):
        rows.append(("gamescope options", settings.get("gamescope")))
    rows.append(("custom engine", "yes" if settings.get("proton_dir")
                 or settings.get("proton_url") else "no"))
    text = _lines(rows)
    kept, hidden = scrub.filter_env(
        _custom_env_pairs(settings.get("custom_env") or "", quiet=True))
    env = [f"    {name}={value}" for name, value in kept]
    if hidden:
        env.append(f"    ({hidden} other variable(s) not reported: not on "
                   "the list of known tuning variables)")
    return text + "\ncustom environment:\n" + (
        "\n".join(env) if env else "    (none)")


def _collect_checks(settings):
    """What `doctor` prints, as a report reads it: the summaries only."""
    from . import discord, hidraw, perfcheck, presence, raytracing, webview
    from .gpu_safety import graphics_safety_problem
    from .ntsync import inproc_sync_problem, inproc_sync_summary
    from .prefix import active_prefix
    from .proton import proton_path
    from .util import custom_env_map
    from .waylanddrv import wayland_driver_summary

    engine = proton_path()
    custom = custom_env_map(settings.get("custom_env") or "")
    prefix = active_prefix()
    rows = [
        ("fast sync (ntsync/fsync)",
         _safely(inproc_sync_summary, engine, environ=custom)),
        ("fast sync problem",
         _safely(inproc_sync_problem, engine, environ=custom)),
        ("graphics safety", _safely(
            lambda: graphics_safety_problem() or "OK (no unsafe state found)")),
        ("wayland driver", _safely(wayland_driver_summary, engine)),
        ("hidraw", _safely(hidraw.summary)),
        ("ray tracing", _safely(raytracing.ray_tracing_summary)),
        ("ray tracing problem", _safely(raytracing.ray_tracing_problem)),
        ("performance", _safely(perfcheck.performance_summary, prefix, DATA)),
        ("sign-in webview", _safely(lambda: webview.status()[0])),
        ("discord", _safely(discord.presence_summary, settings)),
        ("xbox presence", _safely(presence.presence_summary, settings)),
    ]
    text = _lines(rows)
    problems = _safely(perfcheck.performance_problems, prefix, DATA)
    if isinstance(problems, list) and problems:
        text += "\n" + "\n".join("performance: " + p for p in problems)
    return text


def _collect_diagnosis():
    """The launcher's own reading of the last launch's logs."""
    from .gamesetup import diagnose
    hits = list(dict.fromkeys(diagnose(quiet=True) or []))
    return "\n".join("- " + str(hit) for hit in hits) or "no known cause"


# ------------------------------------------------------------------ logs
_TIMESTAMP = re.compile(r"^[\d.:\- T]+|\b[0-9a-f]{4}:")


def _collapse_repeats(lines):
    """Fold runs of the same message -- Wine prints a fixme a thousand times
    -- into one line and a count, comparing without clocks and thread IDs."""
    out, previous, count = [], None, 0

    def flush():
        if count > 1:
            out.append(f"    [repeated {count - 1} more time(s)]")

    for line in lines:
        key = _TIMESTAMP.sub("", line)
        if key == previous and key.strip():
            count += 1
            continue
        flush()
        out.append(line)
        previous, count = key, 1
    flush()
    return out


def _tail(text, limit):
    """The end of `text` within `limit` characters, cut at a line."""
    if len(text) <= limit:
        return text
    if limit <= 0:
        return ""
    cut = text[-limit:]
    return "[earlier part cut to fit]\n" + cut.split("\n", 1)[-1]


def read_log(path, limit):
    """The useful part of a log: its head, where the versions and the device
    are named, and its tail, where it went wrong, within `limit` characters.

    Reads at most a few megabytes from each end, so a diagnostics launch
    that wrote hundreds of them costs nothing more.
    """
    edge = 2_000_000
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            if size <= edge * 2:
                raw, skipped = handle.read(), False
            else:
                head = handle.read(64_000)
                handle.seek(-edge, os.SEEK_END)
                raw, skipped = head + b"\n" + handle.read(), True
    except OSError:
        return ""
    lines = _collapse_repeats(raw.decode("utf-8", "replace").splitlines())
    text = "\n".join(lines)
    if len(text) <= limit and not skipped:
        return text
    head = "\n".join(lines[:_HEAD_LINES])
    if len(head) > limit // 3:
        head = head[:limit // 3]
        head = head.rsplit("\n", 1)[0] if "\n" in head else head
    kept = _tail(text[len(head):], limit - len(head) - 80).removeprefix(
        "[earlier part cut to fit]\n").lstrip("\n")
    omitted = text.count("\n") - head.count("\n") - kept.count("\n") - 1
    return (head + f"\n[... {max(omitted, 0)} line(s) omitted, the file is "
            f"{size} bytes ...]\n" + kept)


def collect(activity_log=None, settings=None, logs_dir=None, identity=None,
            environ=None):
    """Read this machine and return a Report, every piece already scrubbed.

    Reads only. Starts nothing that touches the graphics driver, opens no
    connection, and writes nothing.
    """
    if settings is None:
        from .util import load_settings
        settings = load_settings()
    if identity is None:
        identity = _safely(local_identity, environ=environ)
        if not isinstance(identity, list):
            identity = [(str(Path.home()), "~")]
    report = Report(scrub.Scrubber(identity))
    logs_dir = Path(LOGS if logs_dir is None else logs_dir)

    for key, title, collector, args in (
            ("launcher", "Launcher", _collect_launcher, ()),
            ("system", "System", _collect_system, (environ,)),
            ("graphics", "Graphics", _collect_graphics, (settings, logs_dir)),
            ("components", "Components", _collect_components, (settings,)),
            ("game", "Minecraft", _collect_game, (settings,)),
            ("options", "In-game video settings", _collect_game_options, ()),
            ("settings", "Launcher settings", _collect_settings, (settings,)),
            ("checks", "System check", _collect_checks, (settings,)),
            ("diagnosis", "Launcher's reading of the last launch",
             _collect_diagnosis, ())):
        report.add(key, title, _safely(collector, *args))

    if activity_log and str(activity_log).strip():
        report.add("activity", "Launcher activity (this session)",
                   _tail(str(activity_log), 48_000), kind="log")
    for name, title, limit, included in LOG_FILES:
        text = read_log(logs_dir / name, limit)
        if text.strip():
            report.add("log:" + name, title, text, kind="log",
                       included=included)
    return report


# ====================================================================
# The ways out
# ====================================================================

def relay_url(environ=None):
    """The relay to send to, or None when the browser is the only way."""
    environ = os.environ if environ is None else environ
    url = environ.get(RELAY_ENV, "").strip()
    if url.lower() == "off":
        return None
    url = url or RELAY_URL
    if not url:
        return None
    parts = urlsplit(url)
    local = parts.hostname in ("127.0.0.1", "localhost", "::1")
    # Never in clear over a network, whoever configured it.
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        return None
    return url


def relay_host(environ=None):
    url = relay_url(environ)
    return urlsplit(url).hostname if url else None


def destination(environ=None):
    """One sentence for the window and the command: where a report goes."""
    host = relay_host(environ)
    if host:
        return (f"Sent to {host}, the {PRETTY} report relay, which files it "
                f"as a public issue on github.com/{ISSUE_REPO}. Your IP "
                "address reaches the relay like any web request and is not "
                "kept.")
    return (f"Nothing is sent by the launcher: your browser opens "
            f"github.com/{ISSUE_REPO} with the report filled in, and you "
            "submit it there with your GitHub account. The issue is public.")


def send(report, url=None, timeout=30, opener=None):
    """POST the report to the relay; the URL of the issue it filed.

    Raises ReportError with a sentence for the player. Called from a click.
    """
    url = url or relay_url()
    if not url:
        raise ReportError("No report relay is configured.")
    if not report.title:
        raise ReportError("Give the report a title first.")
    body = json.dumps(report.payload()).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "User-Agent": f"{PRETTY}/{VERSION}",
    })
    try:
        with (opener or urllib.request.urlopen)(request, timeout=timeout) as r:
            answer = json.loads(r.read(100_000).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = str(json.loads(exc.read(10_000).decode("utf-8"))
                         .get("error") or "")
        except Exception:  # noqa: BLE001 - any answer that is not ours
            pass
        if exc.code == 429:
            raise ReportError(detail or "Too many reports were sent from "
                              "this connection. Try again later, or use the "
                              "browser instead.") from exc
        raise ReportError(detail or f"The report relay answered {exc.code}."
                          ) from exc
    except (OSError, ValueError) as exc:
        raise ReportError(f"The report relay could not be reached ({exc}).") \
            from exc
    issue = str(answer.get("url") or "") if isinstance(answer, dict) else ""
    # The link is shown and opened: only ever one into the project's issues.
    if not re.fullmatch(re.escape(ISSUES_URL) + r"/\d+", issue):
        raise ReportError("The report relay gave an unexpected answer.")
    return issue


def reports_dir():
    return DATA / "reports"


def save(report, directory=None, now=None):
    """Write the full report to a file the player can attach; its path."""
    directory = Path(directory) if directory else reports_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    path = directory / f"{APP}-report-{stamp}.txt"
    path.write_text(report.full_text(), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def issue_url(report, attached=None):
    """GitHub's new-issue form, filled in with as much as a URL holds.

    A URL cannot carry the logs, so the body says which file to attach and
    the summary is cut to what fits.
    """
    title = report.title or "Problem report"
    intro = [report.comment or "(describe the problem here)", ""]
    if attached:
        intro += [f"**Full report:** drag `{Path(attached).name}` into this "
                  "box to attach it (it is in the folder the launcher "
                  "opened).", ""]
    intro = "\n".join(intro)
    summary = report.summary()
    base = f"{ISSUES_URL}/new?title={quote(title)}&body="
    while True:
        body = f"{intro}```text\n{summary}\n```\n"
        url = base + quote(report._out(body, prose=True))
        if len(url) <= MAX_URL or not summary:
            return url
        # Cut whole lines off the end until it fits.
        summary = summary[:max(len(summary) - (len(url) - MAX_URL) // 2 - 80,
                               0)].rsplit("\n", 1)[0]
        if summary:
            summary += "\n[cut: see the attached report]"


def open_url(url, runner=None):
    """Open `url` in the player's browser; whether something took it."""
    if runner is not None:
        return bool(runner(url))
    opener = shutil.which("xdg-open")
    try:
        if opener:
            subprocess.Popen([opener, url], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True
        import webbrowser
        return bool(webbrowser.open(url))
    except OSError:
        return False


def open_in_browser(report, directory=None, runner=None):
    """The serverless way: save the full report, open the filled-in form.

    Returns (path, url, opened). Nothing is sent from here -- the player
    submits the form in their browser, under their own GitHub account.
    """
    path = save(report, directory)
    url = issue_url(report, attached=path)
    return path, url, open_url(url, runner)
