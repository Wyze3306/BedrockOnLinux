"""bol.feed — the signed data channel.

A launcher release used to be the only way to tell an install anything: a new
Minecraft build that moved the HBUI anchors, a rebuilt downloader, an outage
at Microsoft. The feed is one small JSON file, published apart from the
releases, that carries exactly that kind of fact:

* ``components`` -- replacement pins (revision + SHA-256) for the downloader,
  the online-login set and the sign-in web runtime, per update channel;
* ``launcher``   -- the newest launcher release per channel, with the SHA-256
  of every asset;
* ``minecraft``  -- extra HBUI anchors for builds that rebundled their UI;
* ``notices``    -- incident messages.

Trust comes from the signature and from nothing else. The file is an
envelope: the exact payload bytes, base64, and Ed25519 signatures over those
bytes. A launcher accepts a payload only when a key compiled into it
(PUBLIC_KEYS) signed it -- not because of where it came from, and not because
TLS said so; the hosts in URLS are mirrors of equal standing and none of them
is believed. Binaries stay on GitHub releases and are checked against the
SHA-256 the signed payload gives them.

Three more rules keep it harmless:

* Roll-back: every payload has a serial, and one lower than the highest
  serial this install has accepted is refused.
* A pin only ever *replaces* a named embedded revision (``replaces``). A
  launcher that ships a newer one than the feed knows about ignores the pin,
  so an old feed can never downgrade a new launcher.
* Nothing here is ever in PLAY's way. Launching reads the cache and never the
  network; a refresh is short, best effort, and silent when it fails; and
  without a cache, a key or a connection the launcher runs on what it
  shipped with. bol.netpolicy decides whether a refresh may happen at all.

The game engine is deliberately not a feed component: the launcher validates
an engine against an allow-list of file hashes compiled into bol.vkd3d, which
a pin alone cannot carry. A new engine still takes a launcher release.
"""
# SPDX-License-Identifier: MIT

import base64
import binascii
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

from . import ed25519
from .config import APP, DATA, VERSION, WINEGDK_PREBUILT_REPO

SCHEMA = 1
# Mirrors, tried in order. A branch of the project's own repository: no host
# the launcher did not already talk to, and nothing to run or pay for.
URLS = (
    "https://raw.githubusercontent.com/Wyze3306/BedrockOnLinux/data/"
    "v1/manifest.json",
)
# key id -> base64 of the 32-byte Ed25519 public key. More than one entry is
# how a key is rotated: ship the new key beside the old one, sign with both
# for a release cycle, then drop the old one. Empty means the feed is not in
# service: nothing is fetched and nothing cached is applied. The private half
# never enters this repository -- docs/updates.md says where it lives.
PUBLIC_KEYS = {}

COMPONENTS = ("xodus", "openssl_xcurl", "webview")
CHANNELS = ("stable", "prerelease")
MAX_BYTES = 1 << 20
REFRESH_AFTER = 6 * 3600          # a good answer is kept this long
RETRY_AFTER = 30 * 60             # a failed fetch is not repeated before this
TIMEOUT = 6

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REV = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_ASSET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
_VERSION = re.compile(r"\d+(\.\d+){1,3}\Z")
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_ASSET_URL_PREFIX = f"https://github.com/{WINEGDK_PREBUILT_REPO}/releases/"
LEVELS = ("info", "warning", "critical")

_lock = threading.Lock()
_memo = {}


class FeedError(Exception):
    """A feed that must not be used. The message says why."""


# --------------------------------------------------------------- envelope

def _b64(text, what):
    if not isinstance(text, str):
        raise FeedError(f"{what} is not text")
    try:
        return base64.b64decode(text.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeError, ValueError) as exc:
        raise FeedError(f"{what} is not base64") from exc


def public_keys():
    """{key id: 32 raw bytes} this launcher trusts.

    BOL_FEED_PUBKEY (``id:base64``) replaces the compiled-in set for one run,
    for a maintainer trying a staging feed. It is the local user's own
    environment: whoever can set it can already run anything as them.
    """
    override = os.environ.get("BOL_FEED_PUBKEY", "").strip()
    source = PUBLIC_KEYS
    if override and ":" in override:
        key_id, _, encoded = override.partition(":")
        source = {key_id: encoded}
    keys = {}
    for key_id, encoded in source.items():
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            continue
        if len(raw) == 32:
            keys[str(key_id)] = raw
    return keys


def in_service():
    return bool(public_keys())


def open_envelope(raw, keys=None):
    """The validated payload of signed envelope bytes `raw`.

    Raises FeedError unless a trusted key signed the exact payload bytes and
    the payload is well formed.
    """
    keys = public_keys() if keys is None else keys
    if not keys:
        raise FeedError("this launcher trusts no feed key")
    if len(raw) > MAX_BYTES:
        raise FeedError("the feed is larger than a feed can be")
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise FeedError("the feed is not JSON") from exc
    if not isinstance(envelope, dict):
        raise FeedError("the feed is not an envelope")
    payload = _b64(envelope.get("payload"), "payload")
    signatures = envelope.get("signatures")
    if not isinstance(signatures, list):
        raise FeedError("the feed carries no signatures")
    signed = False
    for entry in signatures[:8]:
        if not isinstance(entry, dict):
            continue
        key = keys.get(entry.get("keyid"))
        if key is None:
            continue
        try:
            signature = _b64(entry.get("sig"), "signature")
        except FeedError:
            continue
        if ed25519.verify(key, payload, signature):
            signed = True
            break
    if not signed:
        raise FeedError("no trusted key signed this feed")
    try:
        manifest = json.loads(payload.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise FeedError("the signed payload is not JSON") from exc
    return validate(manifest)


# ------------------------------------------------------------- validation

def _version_tuple(text):
    return tuple(int(part) for part in text.split("."))


def _need(condition, message):
    if not condition:
        raise FeedError(message)


def _text(value, limit, what, required=True):
    if value is None and not required:
        return ""
    _need(isinstance(value, str) and len(value) <= limit
          and (value or not required), f"{what} is not valid text")
    return value


def _opt_version(entry, key, what):
    value = entry.get(key)
    if value is None:
        return None
    _need(isinstance(value, str) and _VERSION.match(value),
          f"{what}.{key} is not a version")
    return value


def _validate_pin(component, pin):
    what = f"components.{component}"
    _need(isinstance(pin, dict), f"{what} entry is not an object")
    _need(isinstance(pin.get("rev"), str) and _REV.match(pin["rev"]),
          f"{what}.rev is not a revision")
    _need(isinstance(pin.get("sha256"), str)
          and _SHA256.match(pin["sha256"]), f"{what}.sha256 is not a SHA-256")
    replaces = pin.get("replaces")
    _need(isinstance(replaces, list) and replaces
          and all(isinstance(r, str) and _REV.match(r) for r in replaces),
          f"{what}.replaces must name the embedded revisions it replaces")
    _need(pin["rev"] not in replaces, f"{what} replaces itself")
    url = pin.get("url")
    if url is not None:
        _need(isinstance(url, str) and url.startswith(_ASSET_URL_PREFIX)
              and len(url) < 400 and "\n" not in url,
              f"{what}.url is not one of the project's release assets")
    _opt_version(pin, "min_launcher", what)
    _opt_version(pin, "max_launcher", what)
    return pin


def _validate_launcher(channel, release):
    what = f"launcher.{channel}"
    _need(isinstance(release, dict), f"{what} is not an object")
    _need(isinstance(release.get("version"), str)
          and _VERSION.match(release["version"]), f"{what}.version is invalid")
    tag = release.get("tag")
    _need(isinstance(tag, str) and _REV.match(tag), f"{what}.tag is invalid")
    assets = release.get("assets")
    _need(isinstance(assets, dict) and assets, f"{what}.assets is empty")
    for name, asset in assets.items():
        _need(isinstance(name, str) and _ASSET.match(name),
              f"{what} asset name {name!r} is invalid")
        _need(isinstance(asset, dict) and isinstance(asset.get("sha256"), str)
              and _SHA256.match(asset["sha256"]),
              f"{what} asset {name} has no SHA-256")
        size = asset.get("size", 0)
        _need(isinstance(size, int) and not isinstance(size, bool)
              and size >= 0, f"{what} asset {name} has an invalid size")
    _text(release.get("notes"), 20000, f"{what}.notes", required=False)
    return release


def _validate_patterns(value, what, groups):
    _need(isinstance(value, list) and len(value) <= 16,
          f"{what} is not a short list")
    for pattern in value:
        _need(isinstance(pattern, str) and 0 < len(pattern) <= 600,
              f"{what} holds an invalid pattern")
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise FeedError(f"{what} holds a pattern that does not compile") \
                from exc
        _need(compiled.groups == groups,
              f"{what} patterns need exactly {groups} group(s)")


def _validate_hbui(rules):
    _need(isinstance(rules, list) and len(rules) <= 16,
          "minecraft.hbui is not a short list")
    for rule in rules:
        _need(isinstance(rule, dict), "minecraft.hbui entry is not an object")
        _need(isinstance(rule.get("id"), str) and _ID.match(rule["id"]),
              "minecraft.hbui entry has no id")
        gate = rule.get("gate")
        if gate is not None:
            _need(isinstance(gate, dict), "hbui gate is not an object")
            _validate_patterns([gate.get("function")], "hbui gate.function", 2)
            _validate_patterns([gate.get("selector")], "hbui gate.selector", 0)
            marks = gate.get("marks")
            _need(isinstance(marks, list) and 0 < len(marks) <= 16
                  and all(isinstance(m, str) and 0 < len(m) <= 200
                          for m in marks), "hbui gate.marks is invalid")
        link = rule.get("link")
        if link is not None:
            _need(isinstance(link, dict), "hbui link is not an object")
            _validate_patterns([link.get("anchor")], "hbui link.anchor", 2)
            _validate_patterns([link.get("done")], "hbui link.done", 0)
        _need(gate is not None or link is not None,
              "minecraft.hbui entry patches nothing")


def _validate_notice(notice):
    _need(isinstance(notice, dict), "notice is not an object")
    _need(isinstance(notice.get("id"), str) and _ID.match(notice["id"]),
          "notice has no id")
    _need(notice.get("level", "info") in LEVELS, "notice level is unknown")
    _text(notice.get("title"), 120, "notice.title")
    _text(notice.get("text"), 2000, "notice.text", required=False)
    url = notice.get("url")
    if url is not None:
        _need(isinstance(url, str) and url.startswith("https://")
              and len(url) < 400 and not re.search(r"\s", url),
              "notice.url is not an https link")
    _opt_version(notice, "min_launcher", "notice")
    _opt_version(notice, "max_launcher", "notice")
    until = notice.get("until")
    _need(until is None or (isinstance(until, int)
                            and not isinstance(until, bool)),
          "notice.until is not a timestamp")
    for key, allowed in (("channels", CHANNELS),):
        value = notice.get(key)
        _need(value is None or (isinstance(value, list)
                                and all(v in allowed for v in value)),
              f"notice.{key} is invalid")
    minecraft = notice.get("minecraft")
    _need(minecraft is None or (
        isinstance(minecraft, list) and len(minecraft) <= 32
        and all(isinstance(v, str) and re.match(r"[0-9.]{1,24}\Z", v)
                for v in minecraft)), "notice.minecraft is invalid")


def validate(manifest):
    """`manifest` itself when it is a well-formed schema-1 payload.

    Strict on purpose: the launcher acts on these values, so one the code
    below does not recognise is a reason to refuse the whole file and keep
    the previous one. Unknown *keys* are ignored, which is what lets a later
    launcher read more out of the same file.
    """
    _need(isinstance(manifest, dict), "payload is not an object")
    _need(manifest.get("schema") == SCHEMA
          and not isinstance(manifest.get("schema"), bool),
          f"unsupported feed schema {manifest.get('schema')!r}")
    serial = manifest.get("serial")
    _need(isinstance(serial, int) and not isinstance(serial, bool)
          and serial > 0, "payload has no serial")
    expires = manifest.get("expires")
    _need(expires is None or (isinstance(expires, int)
                              and not isinstance(expires, bool)),
          "expires is not a timestamp")
    components = manifest.get("components", {})
    _need(isinstance(components, dict), "components is not an object")
    for channel, table in components.items():
        _need(channel in CHANNELS, f"unknown channel {channel!r}")
        _need(isinstance(table, dict), "components channel is not an object")
        for component, pins in table.items():
            _need(component in COMPONENTS,
                  f"unknown component {component!r}")
            _need(isinstance(pins, list) and len(pins) <= 16,
                  f"components.{component} is not a short list")
            for pin in pins:
                _validate_pin(component, pin)
    launcher = manifest.get("launcher", {})
    _need(isinstance(launcher, dict), "launcher is not an object")
    for channel, release in launcher.items():
        _need(channel in CHANNELS, f"unknown channel {channel!r}")
        _validate_launcher(channel, release)
    minecraft = manifest.get("minecraft", {})
    _need(isinstance(minecraft, dict), "minecraft is not an object")
    if "hbui" in minecraft:
        _validate_hbui(minecraft["hbui"])
    notices = manifest.get("notices", [])
    _need(isinstance(notices, list) and len(notices) <= 32,
          "notices is not a short list")
    for notice in notices:
        _validate_notice(notice)
    return manifest


# ------------------------------------------------------------------ cache

def _dir():
    # Beside the settings, not in cache/: clearing the cache must not reset
    # the roll-back floor.
    return DATA / "feed"


def _state_path():
    return _dir() / "state.json"


def _cached_path():
    return _dir() / "manifest.signed.json"


def _read_state():
    try:
        state = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(path.name + f".{os.getpid()}.tmp")
    staged.write_bytes(data)
    os.replace(staged, path)


def _write_state(state):
    try:
        _write(_state_path(), json.dumps(state, indent=1).encode("utf-8"))
    except OSError:
        pass


def _floor(state):
    serial = state.get("serial")
    return serial if isinstance(serial, int) and not isinstance(serial, bool) \
        else 0


def forget():
    """Drop the in-memory copy; the next current() reads the disk again."""
    with _lock:
        _memo.clear()


def cached():
    """The cached payload if it still verifies, whatever the update mode."""
    with _lock:
        if "manifest" in _memo:
            return _memo["manifest"]
        manifest = None
        if in_service():
            try:
                manifest = open_envelope(_cached_path().read_bytes())
                # A cache swapped for an older signed file is still a
                # roll-back, whoever swapped it.
                if manifest["serial"] < _floor(_read_state()):
                    manifest = None
            except (OSError, FeedError):
                manifest = None
        _memo["manifest"] = manifest
        return manifest


def current(settings=None):
    """The payload in force, or None: embedded values apply then."""
    from . import netpolicy
    if not netpolicy.feed_allowed(settings):
        return None
    return cached()


# ---------------------------------------------------------------- refresh

def _urls():
    override = os.environ.get("BOL_FEED_URL", "").strip()
    return (override,) if override else URLS


def _fetch(url, timeout):
    # Nothing but the launcher's name: no version, no install id, no cookie.
    request = urllib.request.Request(url, headers={"User-Agent": APP})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise FeedError("the feed is larger than a feed can be")
    return data


def accept(raw, now=None):
    """Verify envelope bytes and make them the cached feed; the payload.

    FeedError when the bytes are unsigned, malformed, expired or older than
    what this install already accepted. The cache is untouched then.
    """
    now = int(time.time() if now is None else now)
    manifest = open_envelope(raw)
    state = _read_state()
    floor = _floor(state)
    if manifest["serial"] < floor:
        raise FeedError(
            f"roll-back refused: serial {manifest['serial']} is older than "
            f"{floor}, which this install already accepted")
    expires = manifest.get("expires")
    if expires is not None and expires < now:
        raise FeedError("this feed has expired")
    if manifest["serial"] > floor or not _cached_path().is_file():
        _write(_cached_path(), raw)
    state.update({"serial": manifest["serial"], "fetched": now})
    state.pop("failed", None)
    _write_state(state)
    forget()
    return manifest


def refresh(timeout=TIMEOUT, fetch=None, now=None):
    """Fetch and accept the feed now. Returns (state, detail); never raises.

    state: "updated" (a newer payload was accepted), "current" (the mirror
    has what is cached), "off" (no key, so no feed), "error". This is the one
    function here that touches the network; callers ask bol.netpolicy first.
    """
    if not in_service():
        return "off", "the data feed is not in service in this build"
    fetch = fetch or _fetch
    now = int(time.time() if now is None else now)
    before = _floor(_read_state())
    problems = []
    for url in _urls():
        try:
            manifest = accept(fetch(url, timeout), now=now)
        except FeedError as exc:
            problems.append(str(exc))
        except (OSError, ValueError, urllib.error.URLError) as exc:
            problems.append(f"unreachable ({exc})")
        except Exception as exc:        # a mirror must never crash a launch
            problems.append(f"failed ({exc})")
        else:
            if manifest["serial"] > before:
                return "updated", f"data {manifest['serial']}"
            return "current", f"data {manifest['serial']}"
    state = _read_state()
    state["failed"] = now
    _write_state(state)
    return "error", "; ".join(problems) or "no mirror"


def due(now=None):
    """Whether a background refresh is worth a request yet."""
    if not in_service():
        return False
    now = time.time() if now is None else now
    state = _read_state()
    fetched, failed = state.get("fetched"), state.get("failed")
    if isinstance(failed, (int, float)) and 0 <= now - failed < RETRY_AFTER:
        return False
    return not (isinstance(fetched, (int, float))
                and 0 <= now - fetched < REFRESH_AFTER)


def refresh_if_due(timeout=TIMEOUT, settings=None):
    """The background refresh: only in "auto", only when due, never raises."""
    from . import netpolicy
    try:
        if netpolicy.background_allowed(settings) and due():
            return refresh(timeout=timeout)
    except Exception:
        pass
    return "skipped", ""


def status():
    """What Settings and `network` show about the feed."""
    state = _read_state()
    manifest = cached()
    return {
        "in_service": in_service(),
        "serial": manifest["serial"] if manifest else None,
        "fetched": state.get("fetched"),
        "failed": state.get("failed"),
    }


# -------------------------------------------------------------- accessors

def _launcher_in_range(entry):
    mine = _version_tuple(VERSION) if _VERSION.match(VERSION) else (0,)
    low, high = entry.get("min_launcher"), entry.get("max_launcher")
    if low and mine < _version_tuple(low):
        return False
    if high and mine > _version_tuple(high):
        return False
    return True


def component_pin(component, embedded_rev, settings=None, channel=None):
    """The feed's replacement for an embedded component revision, or None.

    Returned only when a pin names `embedded_rev` in ``replaces`` and this
    launcher version is in its range. Chains are followed (p14 -> p15 -> p16)
    so one manifest serves every launcher still in the field.
    """
    manifest = current(settings)
    if not manifest:
        return None
    from . import netpolicy
    channel = channel or netpolicy.channel(settings)
    tables = manifest.get("components", {})
    pins = list(tables.get("stable", {}).get(component, []))
    if channel == "prerelease":
        # Its own pins first, then everything stable has.
        pins = list(tables.get("prerelease", {}).get(component, [])) + pins
    found, rev, seen = None, embedded_rev, set()
    while rev not in seen:
        seen.add(rev)
        step = next((p for p in pins if rev in p["replaces"]
                     and _launcher_in_range(p)), None)
        if step is None:
            break
        found, rev = step, step["rev"]
    return found


def resolve(component, embedded_rev, embedded_sha256, settings=None):
    """(rev, sha256, url) to install: the feed's pin, else the embedded one.

    `url` is None when the asset has to be looked up in the releases.
    """
    try:
        pin = component_pin(component, embedded_rev, settings)
    except Exception:
        pin = None
    if pin:
        return pin["rev"], pin["sha256"], pin.get("url")
    return embedded_rev, embedded_sha256, None


def launcher_release(channel="stable", settings=None):
    """The feed's launcher release for `channel`, shaped like a GitHub one.

    The prerelease channel answers with whichever of the two is newer, so a
    pre-release tester moves on to the stable release that follows it.
    """
    manifest = current(settings)
    if not manifest:
        return None
    table = manifest.get("launcher", {})
    picks = [table[c] for c in (("stable", "prerelease")
                                if channel == "prerelease" else ("stable",))
             if c in table]
    if not picks:
        return None
    release = max(picks, key=lambda r: _version_tuple(r["version"]))
    tag = release["tag"]
    base = f"https://github.com/{WINEGDK_PREBUILT_REPO}/releases"
    return {
        "version": release["version"], "tag": tag,
        "url": f"{base}/tag/{tag}",
        "notes": release.get("notes", ""),
        "source": "feed",
        "assets": [{"name": name, "size": asset.get("size", 0),
                    "sha256": asset["sha256"],
                    "browser_download_url": f"{base}/download/{tag}/{name}"}
                   for name, asset in sorted(release["assets"].items())],
    }


def hbui_rules(settings=None):
    """Extra HBUI anchor sets, tried after the launcher's own."""
    manifest = current(settings)
    if not manifest:
        return []
    return list(manifest.get("minecraft", {}).get("hbui", []))


def notices(minecraft=None, settings=None, now=None):
    """Incident notices that apply to this launcher, most severe first.

    A notice tied to Minecraft versions (prefix match) is returned only when
    `minecraft` is one of them.
    """
    manifest = current(settings)
    if not manifest:
        return []
    from . import netpolicy
    channel = netpolicy.channel(settings)
    now = time.time() if now is None else now
    out = []
    for notice in manifest.get("notices", []):
        if notice.get("until") is not None and notice["until"] < now:
            continue
        if notice.get("channels") and channel not in notice["channels"]:
            continue
        if not _launcher_in_range(notice):
            continue
        builds = notice.get("minecraft")
        if builds and not (minecraft and any(
                str(minecraft) == b or str(minecraft).startswith(
                    b.rstrip(".") + ".") for b in builds)):
            continue
        out.append(notice)
    out.sort(key=lambda n: -LEVELS.index(n.get("level", "info")))
    return out

