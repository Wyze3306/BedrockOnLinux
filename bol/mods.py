"""bol.mods — LeviLamina mods for the client: catalogue, lip packages, imports.

LeviLamina (github.com/LiteLDev/LeviLamina) is the mod loader of Bedrock
Edition; its mods are lip packages listed by levimc.org. This module is the
launcher's own lip client, for one environment only -- side "client",
platform "win-x64" -- and one workspace per Minecraft build: the build's
folder, where lip itself would install them.

What lip does on Windows and this does not:

* It runs the packages' scripts. They are cmd.exe lines, and the only ones
  the client packages carry are PeEditor's, which add PreLoader.dll to the
  import table of Minecraft.Windows.exe. A Store build keeps that executable
  encrypted on disk, so there is no import table to edit: the launcher puts
  src/levi-loader.c in front of the game instead (installed as
  vcruntime140_1.dll, see ``prepare_launch``), and PeEditor is not installed.
* It reads packages through the Go module proxy and git. Here a version list
  is the repository's tags (git's smart HTTP, no API quota) and a package
  file is read at its tag from raw.githubusercontent.com.

The state of a workspace lives in ``<build>/.bol-mods.json``: what was
installed, at which version, and which files belong to it, so removing a
package removes exactly those files.
"""
# SPDX-License-Identifier: MIT

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import struct
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

from .config import APP, CACHE
from .log import BolError, info, warn

CATALOGUE_URL = "https://levimc.org/api/v4/mods/"
SITE_URL = "https://levimc.org/mods/"
VERSION_DB_URL = ("https://raw.githubusercontent.com/LiteLDev/"
                  "levilamina-client-version-db/refs/heads/main/v2/"
                  "version-db.json")
LEVILAMINA = "github.com/LiteLDev/LeviLamina"
SIDE = "client"
PLATFORM = "win-x64"

# Packages that exist for what this launcher does another way. A dependency
# on one of them is satisfied without installing it.
SKIPPED = {
    # Edits the import table of the game executable; levi-loader replaces it.
    "github.com/LiteLDev/PeEditor",
    # The dedicated server, a dependency of server variants only.
    "github.com/LiteLDev/bds",
}

STATE_NAME = ".bol-mods.json"
LOADER_NAME = "vcruntime140_1.dll"
LOADER_LOG = PurePosixPath("mods", "bol-loader.log")
LOADER_OVERRIDE = "vcruntime140_1=n,b"
DISABLED_SUFFIX = ".close"   # LeviLauncher's spelling: manifest.json.close
BACKUP_SUFFIX = ".bol-mods-backup"

MOD_CACHE = CACHE / "mods"
_CATALOGUE_TTL = 3600
_VERSION_DB_TTL = 6 * 3600
_TAGS_TTL = 3600

_RESERVED_TOP = {".lip", STATE_NAME.lower()}
_PROTECTED = {"minecraft.windows.exe", "appxmanifest.xml",
              "microsoftgame.config", LOADER_NAME}


class ModError(BolError):
    """A mod operation that cannot go ahead, with a message for the player."""


# ---------------------------------------------------------------- network

def _get(url, timeout=30):
    if not url.startswith("https://"):
        raise ModError(f"Refusing a non-HTTPS download: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": APP})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _get_json(url, timeout=30):
    return json.loads(_get(url, timeout).decode("utf-8-sig"))


def _cached(name, ttl, fetch, refresh=False):
    """``fetch()``'s JSON-able result, cached for ``ttl`` seconds.

    A failed fetch falls back on what the cache holds, however old: the
    catalogue going down should not empty the Mods page.
    """
    path = MOD_CACHE / name
    if not refresh:
        try:
            if time.time() - path.stat().st_mtime < ttl:
                return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    try:
        value = fetch()
    except Exception:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise
    try:
        MOD_CACHE.mkdir(parents=True, exist_ok=True)
        fd, staged = tempfile.mkstemp(dir=MOD_CACHE, prefix=".cache-")
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
        os.replace(staged, path)
    except OSError:
        pass
    return value


def _slug(text):
    return hashlib.sha256(text.encode()).hexdigest()[:24]


# ---------------------------------------------------------------- versions

_SEMVER = re.compile(
    r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?"
    r"(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")


def parse_version(text):
    """A comparable key for a package version, or None.

    SemVer, plus the fourth number some tags carry: ``(numbers, pre)`` where
    a version without a pre-release sorts after the same numbers with one.
    """
    match = _SEMVER.match(str(text).strip())
    if not match:
        return None
    numbers = tuple(int(part or 0) for part in match.group(1, 2, 3, 4))
    pre = match.group(5)
    if pre is None:
        return numbers, (1,)
    parts = []
    for item in pre.split("."):
        parts.append((0, int(item), "") if item.isdigit() else (1, 0, item))
    return numbers, (0, tuple(parts))


def is_prerelease(text):
    key = parse_version(text)
    return key is not None and key[1][0] == 0


def _compare(left, right):
    a, b = parse_version(left), parse_version(right)
    if a is None or b is None:
        return (str(left) > str(right)) - (str(left) < str(right))
    return (a > b) - (a < b)


def sort_versions(versions):
    """Newest first; unparsable ones last."""
    good = [v for v in versions if parse_version(v) is not None]
    bad = [v for v in versions if parse_version(v) is None]
    return sorted(set(good), key=parse_version, reverse=True) + sorted(bad)


def _satisfies_one(version, comparator):
    comparator = comparator.strip()
    if comparator in ("", "*", "x", "X", "latest"):
        return True
    op = ""
    for candidate in (">=", "<=", "==", "!=", ">", "<", "=", "^", "~"):
        if comparator.startswith(candidate):
            op, comparator = candidate, comparator[len(candidate):].strip()
            break
    comparator = comparator.lstrip("v")
    if re.search(r"(^|\.)[*xX](\.|$)", comparator):
        fixed = []
        for part in comparator.split("."):
            if part in ("*", "x", "X"):
                break
            fixed.append(part)
        key = parse_version(version)
        if key is None:
            return False
        prefix = tuple(int(p) for p in fixed if p.isdigit())
        return key[0][:len(prefix)] == prefix and op in ("", "=", "==")
    target = parse_version(comparator)
    key = parse_version(version)
    if target is None or key is None:
        return str(version).lstrip("v") == comparator
    given = len([p for p in comparator.split("-")[0].split(".") if p])
    if op in ("", "=", "=="):
        if given >= 3 or "-" in comparator:
            return key == target
        return key[0][:given] == target[0][:given]
    if op == "!=":
        return key != target
    if op == ">":
        return key > target
    if op == ">=":
        return key >= target
    if op == "<":
        return key < target
    if op == "<=":
        return key <= target
    if op == "^":
        major = target[0][0]
        if major == 0:
            return key >= target and key[0][:2] == target[0][:2]
        return key >= target and key[0][0] == major
    if op == "~":
        return key >= target and key[0][:2] == target[0][:2]
    return False


def satisfies(version, version_range):
    """Whether ``version`` is in ``version_range``: space-separated
    comparators that must all hold, alternatives split by ``||``; wildcards
    (``1.4.*``), exact versions, ``^``/``~`` and the usual comparisons.

    A pre-release is only in a range that names a pre-release, or that is
    exactly it -- ``26.60.*`` is not a promise to install a release
    candidate.
    """
    version = str(version).strip().lstrip("v")
    for alternative in str(version_range or "*").split("||"):
        comparators = alternative.replace(",", " ").split()
        # "1.0.0 - 2.0.0" is a hyphen range.
        if len(comparators) == 3 and comparators[1] == "-":
            comparators = [">=" + comparators[0], "<=" + comparators[2]]
        if not comparators:
            comparators = ["*"]
        if is_prerelease(version) and not any(
                "-" in c.lstrip("<>=^~v").split("+")[0] for c in comparators):
            continue
        if all(_satisfies_one(version, c) for c in comparators):
            return True
    return False


def _build_tuple(text):
    return tuple(int(p) for p in re.findall(r"\d+", str(text)))


def same_build(left, right):
    """1.26.60.05 and 1.26.60.5 are the same build."""
    return _build_tuple(left) == _build_tuple(right) and bool(_build_tuple(left))


# ---------------------------------------------------------------- TOML

def parse_toml(text):
    """lip.toml as a dict: tomllib when Python has it (3.11+), else a reader
    for the part of TOML package files use (tables, arrays of tables, quoted
    and dotted keys, strings, numbers, booleans, arrays, inline tables)."""
    try:
        import tomllib  # noqa: PLC0415 - optional on Python < 3.11
    except ImportError:
        return _MiniToml(text).parse()
    return tomllib.loads(text)


class _MiniToml:
    def __init__(self, text):
        self.s = text
        self.i = 0

    def error(self, what):
        line = self.s.count("\n", 0, self.i) + 1
        raise ModError(f"lip.toml line {line}: {what}")

    def ws(self, newlines=False):
        while self.i < len(self.s):
            c = self.s[self.i]
            if c in " \t\r" or (newlines and c == "\n"):
                self.i += 1
            elif c == "#":
                while self.i < len(self.s) and self.s[self.i] != "\n":
                    self.i += 1
            else:
                break

    def parse(self):
        root = {}
        table = root
        while True:
            self.ws(True)
            if self.i >= len(self.s):
                return root
            if self.s.startswith("[[", self.i):
                self.i += 2
                keys = self.keys("]]")
                parent = self.descend(root, keys[:-1])
                items = parent.setdefault(keys[-1], [])
                if not isinstance(items, list):
                    self.error("array of tables over a value")
                table = {}
                items.append(table)
            elif self.s[self.i] == "[":
                self.i += 1
                keys = self.keys("]")
                table = self.descend(root, keys)
            else:
                keys = self.keys("=")
                self.ws()
                value = self.value()
                target = self.descend(table, keys[:-1])
                target[keys[-1]] = value
            self.ws()
            if self.i < len(self.s) and self.s[self.i] not in "\n":
                self.error("expected the end of the line")

    def descend(self, table, keys):
        for key in keys:
            nxt = table.setdefault(key, {})
            if isinstance(nxt, list):
                nxt = nxt[-1]
            if not isinstance(nxt, dict):
                self.error(f"{key} is not a table")
            table = nxt
        return table

    def keys(self, end):
        keys = []
        while True:
            self.ws()
            keys.append(self.key())
            self.ws()
            if self.s.startswith(end, self.i):
                self.i += len(end)
                return keys
            if self.s[self.i:self.i + 1] == ".":
                self.i += 1
                continue
            self.error(f"expected '{end}'")

    def key(self):
        c = self.s[self.i:self.i + 1]
        if c in ('"', "'"):
            return self.string()
        match = re.compile(r"[A-Za-z0-9_-]+").match(self.s, self.i)
        if not match:
            self.error("expected a key")
        self.i = match.end()
        return match.group(0)

    def string(self):
        quote = self.s[self.i]
        if self.s.startswith(quote * 3, self.i):
            self.i += 3
            end = self.s.find(quote * 3, self.i)
            if end < 0:
                self.error("unterminated string")
            raw = self.s[self.i:end]
            self.i = end + 3
            if raw.startswith("\n"):
                raw = raw[1:]
            return raw if quote == "'" else self.unescape(raw)
        self.i += 1
        out = []
        while self.i < len(self.s):
            c = self.s[self.i]
            if c == quote:
                self.i += 1
                text = "".join(out)
                return text if quote == "'" else self.unescape(text)
            if c == "\\" and quote == '"':
                out.append(self.s[self.i:self.i + 2])
                self.i += 2
                continue
            if c == "\n":
                break
            out.append(c)
            self.i += 1
        self.error("unterminated string")

    def unescape(self, text):
        def repl(match):
            item = match.group(1)
            simple = {"n": "\n", "t": "\t", "r": "\r", '"': '"',
                      "\\": "\\", "b": "\b", "f": "\f"}
            if item in simple:
                return simple[item]
            return chr(int(item[1:], 16))
        return re.sub(r'\\(u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|.)', repl, text)

    def value(self):
        c = self.s[self.i:self.i + 1]
        if c in ('"', "'"):
            return self.string()
        if c == "[":
            self.i += 1
            items = []
            while True:
                self.ws(True)
                if self.s[self.i:self.i + 1] == "]":
                    self.i += 1
                    return items
                items.append(self.value())
                self.ws(True)
                if self.s[self.i:self.i + 1] == ",":
                    self.i += 1
        if c == "{":
            self.i += 1
            table = {}
            while True:
                self.ws()
                if self.s[self.i:self.i + 1] == "}":
                    self.i += 1
                    return table
                keys = self.keys("=")
                self.ws()
                self.descend(table, keys[:-1])[keys[-1]] = self.value()
                self.ws()
                if self.s[self.i:self.i + 1] == ",":
                    self.i += 1
        match = re.compile(r"[^\s,\]}#]+").match(self.s, self.i)
        if not match:
            self.error("expected a value")
        self.i = match.end()
        token = match.group(0)
        if token in ("true", "false"):
            return token == "true"
        try:
            return int(token.replace("_", ""), 0)
        except ValueError:
            pass
        try:
            return float(token.replace("_", ""))
        except ValueError:
            self.error(f"cannot read the value {token!r}")


# ---------------------------------------------------------------- packages

def split_key(key):
    """``id#variant@ref`` -> (id, variant or None)."""
    key = key.split("@", 1)[0]
    if "#" in key:
        package, variant = key.split("#", 1)
        return package.strip(), variant.strip()
    return key.strip(), None


def _github(package_id):
    parts = package_id.split("/")
    if len(parts) < 3 or parts[0].lower() != "github.com":
        raise ModError(f"{package_id} is not hosted on GitHub, which is the "
                       "only place this launcher reads lip packages from.")
    return parts[1], parts[2]


def package_tags(package_id, refresh=False):
    """The versions a package was tagged with, newest first."""
    owner, repo = _github(package_id)

    def fetch():
        url = (f"https://github.com/{owner}/{repo}.git/info/refs"
               "?service=git-upload-pack")
        data = _get(url)
        tags = set()
        for match in re.finditer(rb"refs/tags/([^\s^{}\x00]+)", data):
            tag = match.group(1).decode("utf-8", "replace")
            if tag.startswith("v") and parse_version(tag) is not None:
                tags.add(tag[1:])
        return sort_versions(tags)

    return _cached(f"tags-{_slug(package_id)}.json", _TAGS_TTL, fetch,
                   refresh)


def _normalise_assets(assets, format3):
    out = []
    for asset in assets or []:
        kind = str(asset.get("type", "")).lower()
        places = []
        raw_places = asset.get("placements" if format3 else "place") or []
        for place in raw_places:
            src = str(place.get("src", ""))
            dest = str(place.get("dest", ""))
            if format3 and str(place.get("type")) == "dir":
                if src and not src.endswith("/"):
                    src += "/"
                if dest and not dest.endswith("/"):
                    dest += "/"
            places.append({"src": src, "dest": dest})
        out.append({"type": kind, "urls": list(asset.get("urls") or []),
                    "place": places})
    return out


def _from_tooth(tooth):
    """A tooth.json (format 3) read as lip v0.40 reads it: each label's
    variants for this platform merged into one entry of our side."""
    variants = {}
    order = []
    for variant in tooth.get("variants") or []:
        label = variant.get("label") or ""
        pattern = variant.get("platform") or ""
        if pattern and not fnmatch.fnmatchcase(PLATFORM, pattern):
            continue
        if label not in variants:
            order.append(label)
            variants[label] = {"side": SIDE, "platform": PLATFORM, "deps": {},
                               "assets": [], "preserve": [], "purge": [],
                               "scripts": False}
        entry = variants[label]
        entry["deps"].update(variant.get("dependencies") or {})
        entry["assets"].extend(_normalise_assets(variant.get("assets"), True))
        entry["preserve"].extend(variant.get("preserve_files") or [])
        entry["purge"].extend(variant.get("remove_files") or [])
        if any((variant.get("scripts") or {}).values()):
            entry["scripts"] = True
    return {"id": tooth.get("tooth") or "", "version": tooth.get("version"),
            "info": {"name": (tooth.get("info") or {}).get("name"),
                     "description": (tooth.get("info") or {}).get(
                         "description"),
                     "logo": (tooth.get("info") or {}).get("avatar_url")},
            "variants": {label: [variants[label]] for label in order},
            "format": 3}


def _from_lip_toml(document):
    variants = {}
    for label, entries in (document.get("variants") or {}).items():
        out = []
        for entry in entries or []:
            out.append({
                "side": entry.get("side", ""),
                "platform": entry.get("platform", ""),
                "deps": dict(entry.get("deps") or {}),
                "assets": _normalise_assets(entry.get("assets"), False),
                "preserve": list(entry.get("preserve") or []),
                "purge": list(entry.get("purge") or []),
                "scripts": any((entry.get("hooks") or {}).values()),
            })
        variants[label] = out
    return {"id": document.get("id") or "", "version": document.get("version"),
            "info": document.get("info") or {}, "variants": variants,
            "format": document.get("format", 4)}


def package_file(package_id, version):
    """The package file of ``package_id`` at ``version``, normalised."""
    owner, repo = _github(package_id)

    def fetch():
        base = f"https://raw.githubusercontent.com/{owner}/{repo}/v{version}/"
        try:
            text = _get(base + "lip.toml").decode("utf-8-sig")
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
        else:
            return _from_lip_toml(parse_toml(text))
        try:
            tooth = json.loads(_get(base + "tooth.json").decode("utf-8-sig"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise ModError(f"{package_id} {version} has neither a "
                               "lip.toml nor a tooth.json.") from None
            raise
        return _from_tooth(tooth)

    return _cached(f"pkg-{_slug(package_id + '@' + version)}.json",
                   30 * 24 * 3600, fetch)


def client_entry(document, variant):
    for entry in (document.get("variants") or {}).get(variant) or []:
        if entry.get("side") == SIDE and entry.get("platform") == PLATFORM:
            return entry
    return None


def pick_variant(document, wanted=None):
    """The variant of ``document`` to install on the client: the one asked
    for, else "client" (the label client builds use), else the default."""
    for label in ([wanted] if wanted is not None else ["client", ""]):
        if client_entry(document, label) is not None:
            return label
    return None


# ---------------------------------------------------------------- catalogue

def catalogue(refresh=False):
    """The mods levimc.org lists that run on the client, in its order."""
    def fetch():
        return _get_json(CATALOGUE_URL + "index.json")
    document = _cached("catalogue.json", _CATALOGUE_TTL, fetch, refresh)
    out = []
    for mod in document.get("mods") or []:
        if SIDE in (mod.get("side") or []) and PLATFORM in (
                mod.get("platform") or []):
            out.append(mod)
    return out


def catalogue_stats(refresh=False):
    try:
        document = json.loads((MOD_CACHE / "catalogue.json").read_text())
    except (OSError, ValueError):
        return {}
    return document.get("stats") or {}


def detail(package_id, refresh=False):
    """levimc.org's page of a package: versions, README, links; or None when
    it does not list the package."""
    def fetch():
        try:
            return _get_json(f"{CATALOGUE_URL}{package_id}/index.json")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
    return _cached(f"detail-{_slug(package_id)}.json", _CATALOGUE_TTL, fetch,
                   refresh)


def readme(document):
    """The README of a detail document, as Markdown (English when there is
    one)."""
    readmes = (document or {}).get("readmes") or []
    if isinstance(readmes, dict):
        readmes = [{"lang": k, "content": v} for k, v in readmes.items()]
    best = None
    for item in readmes:
        if not isinstance(item, dict):
            continue
        text = item.get("content") or item.get("markdown") or item.get("text")
        if not text:
            continue
        lang = str(item.get("lang") or item.get("language") or "").lower()
        if best is None or lang.startswith("en") or lang in ("", "default"):
            best = text
            if lang.startswith("en"):
                break
    return best or ""


def logo(url):
    """The bytes of a logo, cached on disk; None when it cannot be had."""
    if not url or not str(url).startswith("https://"):
        return None
    path = MOD_CACHE / "logos" / _slug(url)
    try:
        return path.read_bytes()
    except OSError:
        pass
    try:
        data = _get(url, timeout=15)
    except Exception:
        return None
    if len(data) > 4 << 20:
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError:
        pass
    return data


def levilamina_logo_url():
    return "https://docs.levimc.org/LeviLamina/logo.svg"


def version_db(refresh=False):
    """Minecraft client build -> the LeviLamina versions made for it."""
    document = _cached("levilamina-version-db.json", _VERSION_DB_TTL,
                       lambda: _get_json(VERSION_DB_URL), refresh)
    return document.get("versions") or {}


def levilamina_versions(game_version, refresh=False):
    """The LeviLamina versions that run on ``game_version``, newest first."""
    if not game_version:
        return []
    for build, versions in version_db(refresh).items():
        if same_build(build, game_version):
            return sort_versions(versions)
    return []


def supported_builds(refresh=False):
    """The Minecraft builds some LeviLamina runs on, newest first."""
    builds = list(version_db(refresh))
    return sorted(builds, key=_build_tuple, reverse=True)


def nearest_supported(game_version, refresh=False):
    """The newest build LeviLamina supports that is not newer than
    ``game_version`` and of the same 1.x line -- what "try anyway" uses for a
    build it was not made for (a hotfix such as 1.26.63 after 1.26.60) --
    or None."""
    wanted = _build_tuple(game_version)
    if len(wanted) < 2:
        return None
    for build in supported_builds(refresh):
        parts = _build_tuple(build)
        if parts[:2] == wanted[:2] and parts <= wanted:
            return build
    return None


def effective_build(game_dir, refresh=False):
    """(the build LeviLamina is picked for, whether that is a forced try).

    A supported build is itself. An unsupported one is itself too, unless
    the player switched "try anyway" on for it: then it borrows the nearest
    supported build's LeviLamina, which may well not start."""
    version = game_version(game_dir)
    if levilamina_versions(version, refresh):
        return version, False
    if load_state(game_dir).get("force_unsupported"):
        nearest = nearest_supported(version, refresh)
        if nearest:
            return nearest, True
    return version, False


def set_force_unsupported(game_dir, on):
    state = load_state(game_dir)
    state["force_unsupported"] = bool(on)
    save_state(game_dir, state)
    if not on and not levilamina_versions(game_version(game_dir)):
        remove_loader(game_dir)


def mc_line(game_version):
    """1.26.60.5 -> "1.26.60", the form gameVersions in the catalogue use."""
    parts = _build_tuple(game_version)
    return ".".join(str(p) for p in parts[:3])


def catalogue_compatible(mod, game_version):
    """Whether the catalogue lists ``mod`` for this game's line."""
    line = mc_line(game_version)
    return any(_build_tuple(v)[:3] == _build_tuple(line)[:3]
               for v in mod.get("gameVersions") or [])


# ---------------------------------------------------------------- workspace

def game_version(game_dir):
    from .games import mc_version_str  # noqa: PLC0415 - avoids a cycle
    try:
        return mc_version_str(Path(game_dir))
    except OSError:
        return None


def load_state(game_dir):
    try:
        state = json.loads((Path(game_dir) / STATE_NAME).read_text(
            encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    state.setdefault("format", 1)
    state.setdefault("enabled", True)
    state.setdefault("packages", {})
    return state


def save_state(game_dir, state):
    path = Path(game_dir) / STATE_NAME
    fd, staged = tempfile.mkstemp(dir=path.parent, prefix=".bol-mods-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, indent=2, sort_keys=True)
        os.replace(staged, path)
    finally:
        if os.path.exists(staged):
            os.unlink(staged)


def _safe_target(game_dir, relative):
    """The absolute path of a workspace-relative path, or ModError."""
    relative = str(relative).replace("\\", "/")
    parts = [p for p in relative.split("/") if p not in ("", ".")]
    if (not parts or relative.startswith("/") or ".." in parts
            or ":" in relative or parts[0].lower() in _RESERVED_TOP):
        raise ModError(f"A package tries to write outside its place: "
                       f"{relative!r}.")
    if len(parts) == 1 and parts[0].lower() in _PROTECTED:
        raise ModError(f"A package tries to replace the game's "
                       f"{parts[0]}; refusing.")
    target = Path(game_dir).joinpath(*parts)
    root = Path(game_dir).resolve()
    try:
        target.resolve().relative_to(root)
    except ValueError:
        raise ModError(f"A package path leaves the game folder: "
                       f"{relative!r}.") from None
    return target, "/".join(parts)


# ---------------------------------------------------------------- resolve

class _Resolver:
    """Picks a version of every package a request needs.

    Greedy with a fixpoint: every package gets the newest version that
    satisfies every range on it, then the ranges are collected again from
    the versions picked, until nothing changes. LeviLamina is further held to
    the versions made for this Minecraft build.
    """

    def __init__(self, game_dir, state, log=None):
        self.game_dir = Path(game_dir)
        self.state = state
        self.game, _forced = effective_build(game_dir)
        self.log = log or (lambda message: None)
        self._versions = {}

    def versions(self, package_id):
        if package_id not in self._versions:
            found = set()
            document = None
            try:
                document = detail(package_id)
            except Exception:
                document = None
            for item in (document or {}).get("versions") or []:
                if item.get("version"):
                    found.add(str(item["version"]))
            if not found:
                found.update(package_tags(package_id))
            if package_id == LEVILAMINA:
                allowed = levilamina_versions(self.game)
                found = {v for v in found if v in allowed} or set(allowed)
            self._versions[package_id] = sort_versions(found)
        return self._versions[package_id]

    def choose(self, package_id, ranges):
        candidates = self.versions(package_id)
        if not candidates:
            if package_id == LEVILAMINA:
                raise ModError(
                    f"LeviLamina has no build for Minecraft {self.game}. "
                    "Pick one of the builds it supports: "
                    + ", ".join(supported_builds()[:4]) + ".")
            raise ModError(f"No version of {package_id} could be found.")
        for version in candidates:
            if all(satisfies(version, r) for r, _ in ranges):
                return version
        # A pre-release is acceptable when it is all there is.
        for version in candidates:
            if all(_loose(version, r) for r, _ in ranges):
                return version
        needs = "; ".join(f"{who} needs {r}" for r, who in ranges)
        if package_id == LEVILAMINA:
            raise ModError(
                f"No LeviLamina for Minecraft {self.game} satisfies what "
                f"the mods need ({needs}). Those mods are made for another "
                "Minecraft version.")
        raise ModError(f"No version of {package_id} satisfies {needs}.")

    def best_for_game(self, package_id):
        """(version, variant) of a catalogue package that runs on this
        build: the newest version -- a release before a pre-release -- with
        a client variant whose LeviLamina range has a LeviLamina for this
        Minecraft. None when the catalogue has nothing to say."""
        try:
            document = detail(package_id)
        except Exception:  # noqa: BLE001 - fall back on the tags
            return None
        allowed = levilamina_versions(self.game)
        found = []
        for item in (document or {}).get("versions") or []:
            version = str(item.get("version") or "")
            variants = item.get("variants") or {}
            labels = ["client", ""] + [k for k in variants
                                       if k not in ("client", "")]
            for label in labels:
                for entry in variants.get(label) or []:
                    if entry.get("side") != SIDE or \
                            entry.get("platform") != PLATFORM:
                        continue
                    ranges = [r for k, r in (entry.get("deps") or {}).items()
                              if split_key(k)[0] == LEVILAMINA]
                    # A mod that names no LeviLamina says nothing about
                    # which Minecraft it is for: not a match.
                    if ranges and all(any(_loose(v, r) for v in allowed)
                                      for r in ranges):
                        found.append((version, label))
                        break
                else:
                    continue
                break
        if not found:
            return None
        stable = [f for f in found if not is_prerelease(f[0])]
        pool = stable or found
        return max(pool, key=lambda f: parse_version(f[0]) or ((0,), (0,)))

    def resolve(self, roots, pins=None):
        """The packages to install, dependencies first.

        ``roots``: [(id, variant or None, range)] the player asked for.
        ``pins``: {id: version} kept while every range allows it.

        A package is a variant of a package file, ``id#variant`` (lip's
        identity), and several variants of one id can be installed at once:
        LegacyScriptEngine#client is nothing but dependencies on its other
        client variants. They share the package's version.
        """
        pins = pins or {}
        nodes = {}
        for _round in range(24):
            ranges = {}
            wanted = []
            for package_id, variant, version_range in roots:
                if package_id == LEVILAMINA:
                    variant = "client"
                ranges.setdefault(package_id, []).append(
                    (str(version_range), "you"))
                wanted.append((package_id, variant, True))
            for node in nodes.values():
                for key, version_range in node["entry"]["deps"].items():
                    dep, dep_variant = split_key(key)
                    if dep in SKIPPED:
                        continue
                    if dep == LEVILAMINA:
                        dep_variant = "client"
                    version_range = _expand(str(version_range), node["id"],
                                            node["version"])
                    ranges.setdefault(dep, []).append(
                        (version_range, node["identity"]))
                    wanted.append((dep, dep_variant, False))
            versions = {}
            for package_id, package_ranges in ranges.items():
                pin = pins.get(package_id)
                if pin and all(_loose(pin, r) for r, _ in package_ranges):
                    versions[package_id] = pin
                else:
                    versions[package_id] = self.choose(package_id,
                                                       package_ranges)
            new = {}
            # Named variants first, so that "any variant" can reuse one.
            wanted.sort(key=lambda item: item[1] is None)
            for package_id, variant, explicit in wanted:
                version = versions[package_id]
                document = package_file(package_id, version)
                if variant is None:
                    # A dependency on "any variant" is met by one already
                    # picked; what the player asked for gets its own.
                    same = [n for n in new.values() if n["id"] == package_id]
                    if same and not explicit:
                        continue
                    variant = pick_variant(document)
                    if variant is None:
                        raise ModError(
                            f"{package_id} {version} has nothing to install "
                            "on the Windows client.")
                entry = client_entry(document, variant)
                if entry is None:
                    raise ModError(
                        f"{package_id} {version} has no client variant "
                        f"{variant!r} for Windows.")
                key = identity(package_id, variant)
                if key in new:
                    new[key]["explicit"] |= explicit
                    continue
                new[key] = {
                    "id": package_id, "identity": key, "version": version,
                    "variant": variant, "document": document, "entry": entry,
                    "explicit": explicit,
                }
            if {k: v["version"] for k, v in new.items()} == \
                    {k: v["version"] for k, v in nodes.items()}:
                return self._order(new)
            nodes = new
        raise ModError("The mods' dependencies could not be settled.")

    def _order(self, nodes):
        """Dependencies before what needs them."""
        out, seen = [], set()
        by_id = {}
        for key, node in nodes.items():
            by_id.setdefault(node["id"], []).append(key)

        def targets(key):
            dep, variant = split_key(key)
            if dep == LEVILAMINA:
                variant = "client"
            if variant is not None:
                wanted = identity(dep, variant)
                return [wanted] if wanted in nodes else []
            return by_id.get(dep, [])[:1]

        def visit(key, depth=0):
            if key in seen or depth > 64:
                return
            seen.add(key)
            for dep_key in nodes[key]["entry"]["deps"]:
                for target in targets(dep_key):
                    visit(target, depth + 1)
            out.append(nodes[key])

        for key in sorted(nodes):
            visit(key)
        return out


def identity(package_id, variant):
    """lip's name for one variant of a package."""
    return f"{package_id}#{variant}" if variant else package_id


def _loose(version, version_range):
    """``satisfies`` without the pre-release rule."""
    plain = re.sub(r"-.*$", "", str(version))
    return satisfies(version, version_range) or (
        is_prerelease(version) and satisfies(plain, version_range))


# ---------------------------------------------------------------- install

def _expand(template, package_id, version):
    return (template.replace("{{tooth}}", package_id)
            .replace("{{id}}", package_id)
            .replace("{{version}}", version))


def _download_asset(urls, label, progress=None):
    """The first of ``urls`` that downloads, as a file in the cache."""
    from .util import download  # noqa: PLC0415 - heavy import, GUI-free path
    last = None
    for url in urls:
        if not url.startswith("https://"):
            url = "https://" + url.split("://", 1)[-1]
        target = MOD_CACHE / "assets" / (_slug(url) + "-" +
                                         (Path(url).name or "asset"))
        if target.is_file() and target.stat().st_size:
            return target, url
        try:
            download(url, target, label=label, progress=progress, attempts=3)
            return target, url
        except SystemExit as exc:
            last = exc
        except Exception as exc:  # noqa: BLE001 - next mirror
            last = exc
    raise ModError(f"{label}: no download worked ({last}).")


class _Archive:
    """The files of an asset, by '/'-separated path."""

    def __init__(self, kind, path, url):
        self.kind = kind
        self.files = {}
        if kind in ("zip", "mcmod", "self"):
            with zipfile.ZipFile(path) as archive:
                # Archives made on Windows separate with "\" and list their
                # folders as "name\" entries: normalise before telling a
                # folder from a file, or the folder gets written as an empty
                # file that the files inside it then cannot get past.
                names = [(n.replace("\\", "/"), n)
                         for n in archive.namelist()]
                names = [(k, n) for k, n in names if not k.endswith("/")]
                strip = ""
                if kind == "self" and names:
                    # GitHub's tag archive puts everything under repo-tag/.
                    strip = names[0][0].split("/", 1)[0] + "/"
                    if not all(k.startswith(strip) for k, _ in names):
                        strip = ""
                for key, name in names:
                    key = key[len(strip):]
                    if key:
                        self.files[key] = (archive, name)
                self._zip = path
        elif kind in ("tar", "tgz"):
            mode = "r:gz" if kind == "tgz" else "r:*"
            with tarfile.open(path, mode) as archive:
                for member in archive.getmembers():
                    if member.isfile():
                        self.files[member.name.lstrip("./")] = (None,
                                                                member.name)
            self._tar = (path, mode)
        elif kind == "uncompressed":
            self.files[Path(url).name] = (None, None)
            self._raw = path
        else:
            raise ModError(f"Unknown asset type {kind!r}.")

    def read(self, key):
        archive, name = self.files[key]
        if self.kind in ("zip", "mcmod", "self"):
            with zipfile.ZipFile(self._zip) as z:
                return z.read(name)
        if self.kind in ("tar", "tgz"):
            path, mode = self._tar
            with tarfile.open(path, mode) as archive:
                return archive.extractfile(name).read()
        return Path(self._raw).read_bytes()


def _writes_of(package, archives):
    """(target, archive, key) for every file a package places."""
    entry = package["entry"]
    label = f"{package['id']} {package['version']}"
    writes = []
    for index, asset in enumerate(entry["assets"]):
        archive = archives[index]
        files = sorted(archive.files)
        if asset["type"] == "self":
            files = [f for f in files if f not in ("lip.toml", "tooth.json")]
        if asset["type"] == "mcmod":
            if "manifest.json" not in archive.files:
                raise ModError(f"{label}: its mod archive has no "
                               "manifest.json at its root.")
            try:
                name = json.loads(archive.read("manifest.json").decode(
                    "utf-8-sig"))["name"]
            except (ValueError, KeyError, TypeError):
                raise ModError(f"{label}: its manifest.json has no "
                               "mod name.") from None
            if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", str(name)):
                raise ModError(f"{label}: invalid mod name {name!r}.")
            for key in files:
                writes.append((f"mods/{name}/{key}", archive, key))
            continue
        if asset["type"] == "uncompressed":
            only = files[0]
            for place in asset["place"] or [{"src": only, "dest": only}]:
                dest = place["dest"]
                if dest.endswith("/") or not dest:
                    dest += only
                writes.append((dest, archive, only))
            continue
        for place in asset["place"]:
            src, dest = place["src"], place["dest"]
            if any(c in src for c in "*?["):
                fixed = re.split(r"[*?\[]", src, maxsplit=1)[0]
                fixed = fixed[:fixed.rfind("/") + 1]
                matched = [f for f in files if fnmatch.fnmatchcase(f, src)]
                for key in matched:
                    writes.append((dest + key[len(fixed):], archive, key))
            elif src == "" or src.endswith("/"):
                matched = [f for f in files if f.startswith(src)]
                if not matched:
                    raise ModError(f"{label}: its archive has nothing "
                                   f"under {src!r}.")
                for key in matched:
                    writes.append((dest + key[len(src):], archive, key))
            else:
                if src not in archive.files:
                    raise ModError(f"{label}: its archive has no {src!r}.")
                writes.append((dest, archive, src))
    return writes


def _matches(patterns, relative):
    for pattern in patterns or []:
        pattern = pattern.replace("\\", "/")
        if pattern.endswith("/"):
            if relative.lower().startswith(pattern.lower()):
                return True
        elif fnmatch.fnmatch(relative.lower(), pattern.lower()):
            return True
    return False


def _owner_of(state, relative):
    for package_id, record in state["packages"].items():
        if relative.lower() in (f.lower() for f in record.get("files", [])):
            return package_id
    return None


def _install_package(game_dir, state, package, progress=None, log=None):
    log = log or (lambda message: None)
    entry = package["entry"]
    label = f"{package['identity']} {package['version']}"
    key = package["identity"]
    previous = state["packages"].get(key)
    if previous and previous.get("version") == package["version"]:
        previous["explicit"] = previous.get("explicit") or package["explicit"]
        return False
    if entry.get("scripts"):
        log(f"{label}: its install scripts are Windows commands and are not "
            "run here.")
    archives = []
    for index, asset in enumerate(entry["assets"]):
        if asset["type"] == "self":
            owner, repo = _github(package["id"])
            urls = [f"https://codeload.github.com/{owner}/{repo}/zip/"
                    f"refs/tags/v{package['version']}"]
        else:
            urls = [_expand(u, package["id"], package["version"])
                    for u in asset["urls"]]
        if not urls:
            raise ModError(f"{label}: an asset has no download address.")
        log(f"Downloading {label} ({index + 1}/{len(entry['assets'])})")
        path, url = _download_asset(urls, label, progress)
        try:
            archives.append(_Archive(asset["type"], path, url))
        except (zipfile.BadZipFile, tarfile.TarError) as exc:
            path.unlink(missing_ok=True)
            raise ModError(f"{label}: the download is not a valid archive "
                           f"({exc}).") from None
    writes = _writes_of(package, archives)
    planned = []
    for relative, archive, member in writes:
        target, relative = _safe_target(game_dir, relative)
        owner = _owner_of(state, relative)
        if owner and owner != key:
            raise ModError(f"{label} would overwrite {relative}, which "
                           f"belongs to {owner}.")
        planned.append((target, relative, archive, member))
    # An upgrade: drop the files the old version had and the new one lacks.
    if previous:
        keep = {rel.lower() for _, rel, _, _ in planned}
        for old in previous.get("files", []):
            if old.lower() not in keep and not _matches(
                    entry.get("preserve"), old):
                _remove_file(game_dir, old)
    backups = list((previous or {}).get("backups", []))
    files = []
    created = []
    mode = 0o666 & ~_umask()
    try:
        for target, relative, archive, member in planned:
            if target.exists() and _matches(entry.get("preserve"), relative):
                files.append(relative)
                continue
            if (target.exists() and not previous
                    and _owner_of(state, relative) is None
                    and not relative.lower().startswith("mods/")):
                backup = target.with_name(target.name + BACKUP_SUFFIX)
                if not backup.exists():
                    shutil.copy2(target, backup)
                    backups.append(relative)
            existed = target.exists()
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, staged = tempfile.mkstemp(dir=target.parent, prefix=".bol-")
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(archive.read(member))
                # mkstemp makes the file private (0600); a game file is not.
                os.chmod(staged, mode)
                os.replace(staged, target)
            finally:
                if os.path.exists(staged):
                    os.unlink(staged)
            if not existed:
                created.append(relative)
            files.append(relative)
    except BaseException:
        # Leave nothing half-installed: no record says whose those files
        # are, and the next attempt would trip over them.
        for relative in reversed(created):
            _remove_file(game_dir, relative)
        raise
    state["packages"][key] = {
        "id": package["id"],
        "version": package["version"],
        "variant": package["variant"],
        "explicit": bool(package["explicit"] or
                         (previous or {}).get("explicit")),
        "files": sorted(set(files)),
        "backups": sorted(set(backups)),
        "deps": entry["deps"],
        "preserve": entry.get("preserve") or [],
        "purge": entry.get("purge") or [],
        "name": (package["document"].get("info") or {}).get("name")
        or package["id"].rsplit("/", 1)[-1],
        "installed": int(time.time()),
    }
    save_state(game_dir, state)
    return True


def _umask():
    current = os.umask(0)
    os.umask(current)
    return current


def _remove_file(game_dir, relative):
    try:
        target, _ = _safe_target(game_dir, relative)
    except ModError:
        return
    try:
        target.unlink()
    except FileNotFoundError:
        pass
    except IsADirectoryError:
        return
    backup = target.with_name(target.name + BACKUP_SUFFIX)
    if backup.exists():
        os.replace(backup, target)
    parent = target.parent
    root = Path(game_dir)
    while parent != root and parent.is_dir():
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def _record_id(key, record):
    return record.get("id") or split_key(key)[0]


def _pins(state):
    return {_record_id(k, r): r["version"]
            for k, r in state["packages"].items()}


def _roots(state):
    return [(_record_id(k, r), r.get("variant"), "*")
            for k, r in state["packages"].items() if r.get("explicit")]


def levilamina_key(state):
    for key, record in state["packages"].items():
        if _record_id(key, record) == LEVILAMINA:
            return key
    return None


def _check_build(game_dir):
    game_dir = Path(game_dir)
    if not (game_dir / "Minecraft.Windows.exe").exists():
        raise ModError(f"{game_dir} is not a Minecraft build.")
    version, _forced = effective_build(game_dir)
    if not levilamina_versions(version):
        raise ModError(
            f"LeviLamina does not support Minecraft {version or '(unknown)'}. "
            "It is made for one build at a time; it supports "
            + ", ".join(supported_builds()[:4]) + ".")
    return version


def _refuse_while_running(game_dir):
    """Changing the files of the build Minecraft is running from is not
    something to do under it; any other build is fine."""
    from .config import CONTENT  # noqa: PLC0415
    from .prefix import _mc_running  # noqa: PLC0415 - heavy, launch-time only
    try:
        running_build = CONTENT.resolve() == Path(game_dir).resolve()
    except OSError:
        running_build = True
    if running_build and _mc_running():
        raise ModError("Close Minecraft before changing its mods.")


def install(game_dir, package_ids, progress=None, log=None, update=False):
    """Install packages (and what they need) into a build; LeviLamina comes
    with the first. ``update`` moves everything to the newest versions that
    still fit. Returns the identities of what was installed or changed."""
    log = log or info
    _refuse_while_running(game_dir)
    _check_build(game_dir)
    state = load_state(game_dir)
    resolver = _Resolver(game_dir, state, log)
    roots = {(pid, variant): rng for pid, variant, rng in _roots(state)}
    requested = [split_key(p) for p in package_ids]
    if update:
        requested += [(pid, variant) for pid, variant in roots
                      if pid != LEVILAMINA]
    for package_id, variant in requested:
        for old in [k for k in roots if k[0] == package_id and
                    (variant is None or k[1] == variant)]:
            roots.pop(old)
        best = None
        if package_id != LEVILAMINA:
            best = resolver.best_for_game(package_id)
        if best and (variant is None or variant == best[1]):
            roots[(package_id, best[1])] = best[0]
        else:
            roots[(package_id, variant)] = "*"
    roots = {k: v for k, v in roots.items() if k[0] != LEVILAMINA}
    roots[(LEVILAMINA, "client")] = "*"
    pins = {} if update else _pins(state)
    for package_id, _variant in requested:
        pins.pop(package_id, None)
    plan = resolver.resolve([(pid, variant, rng)
                             for (pid, variant), rng in roots.items()], pins)
    changed = []
    total = len(plan)
    for index, package in enumerate(plan):
        log(f"[{index + 1}/{total}] {package['identity']} "
            f"{package['version']}")
        if _install_package(game_dir, state, package, progress, log):
            changed.append(package["identity"])
    for package in plan:
        state["packages"][package["identity"]]["explicit"] = (
            package["explicit"] or
            state["packages"][package["identity"]].get("explicit", False))
    _autoremove(game_dir, state, {p["identity"] for p in plan})
    save_state(game_dir, state)
    if state.get("enabled", True):
        install_loader(game_dir)
    return changed


def update_all(game_dir, progress=None, log=None):
    return install(game_dir, [], progress=progress, log=log, update=True)


def _autoremove(game_dir, state, needed):
    for key in list(state["packages"]):
        if key not in needed and not state["packages"][key].get("explicit"):
            _uninstall_files(game_dir, state, key)


def _uninstall_files(game_dir, state, key):
    record = state["packages"].pop(key, None)
    if not record:
        return
    for relative in record.get("files", []):
        if _matches(record.get("preserve"), relative):
            continue
        _remove_file(game_dir, relative)
    for pattern in record.get("purge", []):
        for match in Path(game_dir).glob(pattern.replace("\\", "/")):
            if match.is_file():
                rel = match.relative_to(game_dir).as_posix()
                if _owner_of(state, rel) is None:
                    _remove_file(game_dir, rel)
    save_state(game_dir, state)


def _needed(state):
    """Every package an explicitly installed one needs, transitively."""
    keys = set(state["packages"])
    needed = set()
    frontier = [k for k, r in state["packages"].items() if r.get("explicit")]
    while frontier:
        current = frontier.pop()
        if current in needed:
            continue
        needed.add(current)
        for dep_key in state["packages"].get(current, {}).get("deps", {}):
            dep, variant = split_key(dep_key)
            if dep == LEVILAMINA:
                variant = "client"
            if variant is not None:
                candidates = [identity(dep, variant)]
            else:
                candidates = [k for k in keys if split_key(k)[0] == dep][:1]
            frontier.extend(c for c in candidates if c in keys)
    return needed


def uninstall(game_dir, key, log=None):
    """Remove a package (by identity or id), and the dependencies nothing
    else needs. Removing LeviLamina removes every catalogue mod."""
    _refuse_while_running(game_dir)
    state = load_state(game_dir)
    if key not in state["packages"]:
        matches = [k for k, r in state["packages"].items()
                   if _record_id(k, r) == key]
        if not matches:
            raise ModError(f"{key} is not installed.")
        key = matches[0]
    if _record_id(key, state["packages"][key]) == LEVILAMINA:
        for other in list(state["packages"]):
            _uninstall_files(game_dir, state, other)
        remove_loader(game_dir)
        save_state(game_dir, state)
        return
    package_id = _record_id(key, state["packages"][key])
    for other in [k for k, r in state["packages"].items()
                  if _record_id(k, r) == package_id]:
        state["packages"][other]["explicit"] = False
    _autoremove(game_dir, state, _needed(state))
    save_state(game_dir, state)


# ---------------------------------------------------------------- mods folder

def _manifest_of(folder):
    for name in ("manifest.json", "manifest.json" + DISABLED_SUFFIX):
        path = folder / name
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                data = {}
            return (data if isinstance(data, dict) else {}), \
                name == "manifest.json"
    return None, False


def installed(game_dir):
    """What is in a build's mods folder, with where each came from."""
    game_dir = Path(game_dir)
    state = load_state(game_dir)
    owners, manifests = {}, {}
    for key, record in state["packages"].items():
        for relative in record.get("files", []):
            parts = relative.split("/")
            if len(parts) >= 3 and parts[0].lower() == "mods":
                owners.setdefault(parts[1].lower(), key)
                if len(parts) == 3 and parts[2].lower() == "manifest.json":
                    manifests[parts[1].lower()] = key
    owners.update(manifests)
    out = []
    mods = game_dir / "mods"
    try:
        folders = sorted(p for p in mods.iterdir() if p.is_dir())
    except OSError:
        folders = []
    for folder in folders:
        manifest, enabled = _manifest_of(folder)
        if manifest is None:
            continue
        key = owners.get(folder.name.lower())
        record = state["packages"].get(key) if key else None
        package_id = _record_id(key, record) if record else None
        out.append({
            "folder": folder.name,
            "path": folder,
            "name": manifest.get("name") or folder.name,
            "version": (record or {}).get("version")
            or manifest.get("version") or "",
            "type": manifest.get("type") or "",
            "enabled": enabled,
            "package": key,
            "id": package_id,
            "explicit": bool((record or {}).get("explicit")),
            "source": "catalogue" if key else "imported",
            "blocked": (state.get("blocked") or {}).get(folder.name),
            "core": package_id == LEVILAMINA,
        })
    return out


def _entry_dll(folder, manifest):
    entry = str((manifest or {}).get("entry") or "").replace("\\", "/")
    if not entry.lower().endswith(".dll"):
        return None
    path = folder / entry
    return path if path.is_file() else None


def load_problems(game_dir):
    """{mod folder: why it cannot load} for the mods that are switched on.

    A mod calls into LeviLamina (and sometimes into another mod) by name.
    On Windows a name that is not there stops that one DLL from loading and
    LeviLamina says so; under Wine the missing name becomes a stub that
    kills the whole game the first time it is called -- during loading, so
    Minecraft runs with no window and nothing says why. That happens when a
    mod was built for another LeviLamina than its "26.60.*" claims (MelonRind
    0.1.1 against 26.60.6). Checking the names here, before the game starts,
    is what LeviLamina would have done.
    """
    from .pe import PE  # noqa: PLC0415
    game_dir = Path(game_dir)
    mods_dir = game_dir / "mods"
    enabled = {}
    for item in installed(game_dir):
        if not item["enabled"] or item["core"]:
            continue
        manifest, _on = _manifest_of(item["path"])
        dll = _entry_dll(item["path"], manifest)
        if dll is not None:
            enabled[item["folder"]] = dll
    providers = {}
    for path in (mods_dir / "LeviLamina" / "LeviLamina.dll",
                 game_dir / "PreLoader.dll"):
        if path.is_file():
            try:
                providers[path.name.lower()] = PE(path).export_names()
            except (OSError, ValueError, IndexError, struct.error):
                pass
    if "levilamina.dll" not in providers:
        return {}
    imports = {}
    for folder, dll in enabled.items():
        try:
            image = PE(dll)
            imports[folder] = image.imports()
            providers[dll.name.lower()] = image.export_names()
        except (OSError, ValueError, IndexError, struct.error) as exc:
            imports[folder] = None
            providers.pop(dll.name.lower(), None)
            enabled[folder] = exc
    owner = {dll.name.lower(): folder for folder, dll in enabled.items()
             if isinstance(dll, Path)}
    problems = {}
    for folder, value in enabled.items():
        if not isinstance(value, Path):
            problems[folder] = f"its {Path(str(value)).name or 'DLL'} " \
                               f"cannot be read ({value})"
    # A mod that cannot load takes down the mods that call into it.
    changed = True
    while changed:
        changed = False
        for folder, table in imports.items():
            if folder in problems or not table:
                continue
            for dll_name, names in table.items():
                exports = providers.get(dll_name)
                if exports is None:
                    continue
                provider = owner.get(dll_name)
                if provider in problems:
                    problems[folder] = f"it needs {provider}, which cannot " \
                                       "load"
                    changed = True
                    break
                missing = sorted(n for n in names if n not in exports)
                if missing:
                    what = "LeviLamina" if dll_name == "levilamina.dll" else \
                        (owner.get(dll_name) or dll_name)
                    problems[folder] = (
                        f"it was built for another version of {what}: "
                        f"{len(missing)} function(s) it calls are missing")
                    changed = True
                    break
    return problems


def packages(game_dir):
    """The lip packages installed in a build, by id."""
    return load_state(game_dir)["packages"]


def set_enabled(game_dir, folder, enabled):
    """Turn one mod on or off (renames its manifest, as LeviLauncher does)."""
    path = _mod_folder(game_dir, folder)
    if enabled:
        state = load_state(game_dir)
        if (state.get("blocked") or {}).pop(folder, None) is not None:
            save_state(game_dir, state)
    on, off = path / "manifest.json", path / ("manifest.json" +
                                              DISABLED_SUFFIX)
    if enabled and off.exists():
        os.replace(off, on)
    elif not enabled and on.exists():
        os.replace(on, off)


def _mod_folder(game_dir, folder):
    if not folder or "/" in folder or "\\" in folder or folder in (".", ".."):
        raise ModError(f"Invalid mod folder {folder!r}.")
    path = Path(game_dir) / "mods" / folder
    if not path.is_dir():
        raise ModError(f"No mod folder {folder}.")
    return path


def remove_folder(game_dir, folder):
    """Delete an imported mod. A catalogue mod is removed with uninstall()."""
    state = load_state(game_dir)
    path = _mod_folder(game_dir, folder)
    for package_id, record in state["packages"].items():
        if any(f.lower().startswith(f"mods/{folder.lower()}/")
               for f in record.get("files", [])):
            return uninstall(game_dir, package_id)
    shutil.rmtree(path)


def _valid_mod_name(name):
    return bool(re.match(r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$", str(name or ""))) \
        and str(name).strip() not in (".", "..")


def import_mod(game_dir, source, overwrite=False):
    """Add a mod from a .zip, a folder or a .dll. Returns its folder name.

    A mod is a folder with a LeviLamina manifest.json (name, entry, type);
    an archive may hold it at its root or one folder down. A lone DLL gets
    a manifest written for it, as a native mod.
    """
    _refuse_while_running(game_dir)
    source = Path(source)
    mods = Path(game_dir) / "mods"
    mods.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".bol-import-", dir=mods))
    try:
        if source.is_dir():
            shutil.copytree(source, staging / "mod")
            root = staging / "mod"
        elif source.suffix.lower() == ".dll":
            root = staging / "mod"
            root.mkdir()
            shutil.copy2(source, root / source.name)
            (root / "manifest.json").write_text(json.dumps({
                "name": source.stem, "entry": source.name,
                "version": "0.0.0", "type": "preload-native"}, indent=2))
        elif zipfile.is_zipfile(source):
            root = _unpack_mod_zip(source, staging)
        else:
            raise ModError(f"{source.name} is not a .zip, a .dll or a "
                           "folder.")
        manifest_path = root / "manifest.json"
        if not manifest_path.is_file():
            raise ModError(f"{source.name} has no manifest.json: it is not "
                           "a LeviLamina mod.")
        try:
            manifest = json.loads(manifest_path.read_text(
                encoding="utf-8-sig"))
        except ValueError:
            raise ModError(f"The manifest.json of {source.name} is not "
                           "valid JSON.") from None
        name = manifest.get("name") if isinstance(manifest, dict) else None
        entry = manifest.get("entry") if isinstance(manifest, dict) else None
        if not _valid_mod_name(name) or not entry:
            raise ModError(f"The manifest.json of {source.name} needs a "
                           "\"name\" and an \"entry\".")
        if not (root / str(entry).replace("\\", "/")).exists():
            raise ModError(f"{source.name} does not contain its entry "
                           f"{entry}.")
        target = mods / name
        if target.exists():
            if not overwrite:
                raise ModError(f"A mod named {name} is already installed.")
            state = load_state(game_dir)
            if any(f.lower().startswith(f"mods/{name.lower()}/")
                   for r in state["packages"].values()
                   for f in r.get("files", [])):
                raise ModError(f"{name} was installed from the catalogue; "
                               "update or remove it there.")
            shutil.rmtree(target)
        os.replace(root, target)
        return name
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _unpack_mod_zip(source, staging):
    with zipfile.ZipFile(source) as archive:
        names = [n.replace("\\", "/") for n in archive.namelist()]
        manifests = sorted((n for n in names
                            if PurePosixPath(n).name.lower() == "manifest.json"
                            and len(PurePosixPath(n).parts) <= 2),
                           key=lambda n: len(PurePosixPath(n).parts))
        if not manifests:
            raise ModError(f"{source.name} has no manifest.json: it is not "
                           "a LeviLamina mod.")
        prefix = str(PurePosixPath(manifests[0]).parent)
        prefix = "" if prefix == "." else prefix + "/"
        root = staging / "mod"
        root.mkdir()
        for info_item, name in zip(archive.infolist(), names):
            if not name.startswith(prefix) or name.endswith("/"):
                continue
            relative = name[len(prefix):]
            parts = PurePosixPath(relative).parts
            if not parts or ".." in parts or relative.startswith("/") or \
                    ":" in relative:
                raise ModError(f"{source.name} holds an unsafe path: "
                               f"{name}.")
            target = root.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info_item) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
        return root


# ---------------------------------------------------------------- loader

def loader_bytes():
    import pkgutil  # noqa: PLC0415
    blob = pkgutil.get_data("bol", "levi-loader.dll")
    if not blob:
        raise ModError("The mod loader (levi-loader.dll) is missing from "
                       "this install.")
    return blob


def has_levilamina(game_dir):
    game_dir = Path(game_dir)
    return (levilamina_key(load_state(game_dir)) is not None
            and (game_dir / "PreLoader.dll").is_file())


def _is_our_loader(path):
    try:
        data = path.read_bytes()
    except OSError:
        return False
    return b"bol levi-loader" in data


def install_loader(game_dir):
    target = Path(game_dir) / LOADER_NAME
    blob = loader_bytes()
    try:
        if target.read_bytes() == blob:
            return target
    except OSError:
        pass
    if target.exists() and not _is_our_loader(target):
        raise ModError(f"{target} exists and is not the launcher's; "
                       "refusing to replace it.")
    fd, staged = tempfile.mkstemp(dir=target.parent, prefix=".bol-loader-")
    with os.fdopen(fd, "wb") as stream:
        stream.write(blob)
    os.replace(staged, target)
    return target


def remove_loader(game_dir):
    target = Path(game_dir) / LOADER_NAME
    if target.exists() and _is_our_loader(target):
        target.unlink()


def mods_enabled(game_dir):
    return bool(load_state(game_dir).get("enabled", True))


def set_mods_enabled(game_dir, enabled):
    state = load_state(game_dir)
    state["enabled"] = bool(enabled)
    save_state(game_dir, state)
    if not enabled:
        remove_loader(game_dir)
    elif has_levilamina(game_dir):
        install_loader(game_dir)


def loader_log(game_dir):
    try:
        return (Path(game_dir) / LOADER_LOG).read_text(errors="replace")
    except OSError:
        return ""


def prepare_launch(game_dir, env):
    """Put the loader in front of the game when this build has LeviLamina
    and mods are on; take it away otherwise. Returns whether mods load."""
    game_dir = Path(game_dir)
    try:
        active = has_levilamina(game_dir) and mods_enabled(game_dir)
        if not active:
            remove_loader(game_dir)
            return False
        state = load_state(game_dir)
        record = state["packages"][levilamina_key(state)]
        try:
            version, forced = effective_build(game_dir)
            allowed = levilamina_versions(version)
        except Exception:  # noqa: BLE001 - offline: trust what is installed
            version, forced = game_version(game_dir), False
            allowed = [record["version"]]
        if forced:
            warn(f"LeviLamina {record['version']} was made for Minecraft "
                 f"{version}, not {game_version(game_dir)}: trying anyway, as "
                 "asked. If the game closes or freezes at start, turn off "
                 "\"Try anyway\" on the Mods page.")
        if not allowed:
            warn(f"LeviLamina does not support Minecraft "
                 f"{game_version(game_dir)}; starting without mods. Pick a "
                 "supported build, or switch on \"Try anyway\" on the Mods "
                 "page.")
            remove_loader(game_dir)
            return False
        if record["version"] not in allowed:
            warn(f"LeviLamina {record['version']} is not made for Minecraft "
                 f"{version}; starting without mods. Update it from the "
                 "Mods page.")
            remove_loader(game_dir)
            return False
        for folder, reason in load_problems(game_dir).items():
            set_enabled(game_dir, folder, False)
            state = load_state(game_dir)
            state.setdefault("blocked", {})[folder] = reason
            save_state(game_dir, state)
            warn(f"The mod {folder} was switched off: {reason}, so it would "
                 "stop Minecraft while loading. Update it, or ask its author "
                 "for a build for this LeviLamina.")
        install_loader(game_dir)
        log_path = game_dir / LOADER_LOG
        try:
            log_path.unlink()
        except OSError:
            pass
        current = env.get("WINEDLLOVERRIDES", "")
        if LOADER_OVERRIDE not in current:
            env["WINEDLLOVERRIDES"] = (
                f"{current};{LOADER_OVERRIDE}" if current else LOADER_OVERRIDE)
        enabled = [m["name"] for m in installed(game_dir)
                   if m["enabled"] and not m["core"]]
        info(f"LeviLamina {record['version']} will load"
             + (f" with {len(enabled)} mod(s): " + ", ".join(enabled)
                if enabled else "."))
        return True
    except Exception as exc:  # noqa: BLE001 - never block PLAY on mods
        warn(f"The mods could not be prepared ({exc}); starting without "
             "them.")
        try:
            remove_loader(game_dir)
        except OSError:
            pass
        return False


def describe(game_dir):
    """A summary of a build's mod setup for the page header."""
    game_dir = Path(game_dir)
    version = game_version(game_dir)
    state = load_state(game_dir)
    key = levilamina_key(state)
    record = state["packages"].get(key) if key else None
    forced, nearest = False, None
    try:
        target, forced = effective_build(game_dir)
        allowed = levilamina_versions(target)
        if not allowed or forced:
            nearest = nearest_supported(version)
        db_error = None
    except Exception as exc:  # noqa: BLE001
        target, allowed, db_error = version, [], str(exc)
    return {
        "game_dir": game_dir,
        "game_version": version,
        # The build mods are picked for: this one, or under "try anyway"
        # the nearest one LeviLamina supports.
        "target_version": target,
        "forced": forced,
        "nearest": nearest,
        "supported": bool(allowed),
        "levilamina_versions": allowed,
        "levilamina": record["version"] if record else None,
        "levilamina_latest": allowed[0] if allowed else None,
        "enabled": bool(state.get("enabled", True)),
        "error": db_error,
    }

