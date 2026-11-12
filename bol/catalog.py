"""bol.catalog — add-ons, maps, texture packs and skins from MCPEDL/CurseForge.

MCPEDL and CurseForge's Minecraft Bedrock section are one catalogue now:
both belong to Overwolf, and every CurseForge Bedrock project is mirrored on
MCPEDL under the same slug, its files served from CurseForge's CDN. MCPEDL's
own site reads that catalogue from a public JSON API (api.mcpedl.com/api),
which needs no account and no key, so that is what this module reads too:

* ``search/advanced`` -- full-text search, by kind, sorted, 20 per page;
* ``route/slug/<slug>`` -- one project: description, pictures, supported
  Minecraft versions and its downloads.

CurseForge's own API (api.curseforge.com) wants a per-application key that
may not be shipped in an open-source program, and adds nothing the mirror
does not have, so a CurseForge link is resolved through its slug instead.

A download is fetched over HTTPS only, from MCPEDL's or CurseForge's hosts
only, and handed to ``bol.content.import_content`` -- the same unpacking
"Import content…" in Settings does, into the folders the game reads. What
was installed from here is remembered (``content-installs.json``) so the
page can say so and take it out again.

Projects whose author links their files out to another site (an ad-link
shortener, a Discord, MediaFire) cannot be installed from here; their links
are offered to open in the browser, through MCPEDL's own exit page.
"""
# SPDX-License-Identifier: MIT

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

from .config import APP, CACHE, DATA, VERSION
from .log import BolError

API = "https://api.mcpedl.com/api"
SITE = "https://mcpedl.com"
CURSEFORGE_SITE = "https://www.curseforge.com/minecraft-bedrock"

USER_AGENT = f"{APP}/{VERSION} (+https://github.com/Wyze3306/BedrockOnLinux)"

CATALOG_CACHE = CACHE / "catalog"
INSTALLS_FILE = DATA / "content-installs.json"

_LIST_TTL = 15 * 60
_DETAIL_TTL = 10 * 60
MAX_DOWNLOAD = 2 << 30          # the largest maps are a few hundred MB
MAX_IMAGE = 6 << 20

# What the page offers, in its order: key -> (label, MCPEDL type_id, the
# CurseForge URL segment). MCPEDL also lists seeds, servers and single skins,
# none of which is a file the game imports.
KINDS = {
    "addons": ("Add-ons", 1, "addons"),
    "maps": ("Maps", 2, "maps"),
    "textures": ("Texture packs", 3, "texture-packs"),
    "skins": ("Skin packs", 6, "skins"),
    "scripts": ("Scripts", 9, "scripts"),
}
_KIND_BY_TYPE = {type_id: key for key, (_l, type_id, _c) in KINDS.items()}

SORTS = {
    "popular": ("Popular this week", "popularity_week"),
    "popular_month": ("Popular this month", "popularity_month"),
    "popular_all": ("Most downloaded", "popularity_all"),
    "updated": ("Recently updated", "updated_at"),
    "new": ("New", "created_at"),
}

# Hosts a file may come from, redirects included: MCPEDL's edge and storage,
# and CurseForge's CDN behind them.
_ALLOWED_HOSTS = ("mcpedl.com", "forgecdn.net", "curseforge.com")
INSTALLABLE = (".mcaddon", ".mcpack", ".mcworld", ".mctemplate", ".zip")


class CatalogError(BolError):
    """A catalogue operation that cannot go ahead, worded for the player."""


# ---------------------------------------------------------------- network

def _host_allowed(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(
        host == h or host.endswith("." + h) for h in _ALLOWED_HOSTS)


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to an HTTPS URL on an allowed host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _host_allowed(newurl):
            raise CatalogError(
                f"The download moved to {urllib.parse.urlsplit(newurl).hostname},"
                " which is not MCPEDL or CurseForge; open it in the browser "
                "instead.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_HttpsOnlyRedirects)


def _open(url, timeout=30):
    if not _host_allowed(url):
        raise CatalogError(f"Refusing a download from {url}")
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    return _OPENER.open(request, timeout=timeout)


def _get_json(path, params=None, timeout=30):
    url = f"{API}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with _open(url, timeout) as response:
            return json.loads(response.read().decode("utf-8-sig"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise CatalogError("MCPEDL does not know that project.") from exc
        raise CatalogError(f"MCPEDL answered HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CatalogError(
            "MCPEDL cannot be reached right now. Check the connection and "
            "try again.") from exc
    except ValueError as exc:
        raise CatalogError("MCPEDL sent an answer this launcher cannot "
                           "read.") from exc


def _cache_key(*parts):
    return hashlib.sha256("\0".join(map(str, parts)).encode()).hexdigest()[:24]


def _cached(name, ttl, fetch, refresh=False):
    """``fetch()`` cached for ``ttl`` seconds; a stale copy when offline."""
    path = CATALOG_CACHE / f"{name}.json"
    if not refresh:
        try:
            if time.time() - path.stat().st_mtime < ttl:
                return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    try:
        value = fetch()
    except CatalogError as failure:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise failure from None
    try:
        CATALOG_CACHE.mkdir(parents=True, exist_ok=True)
        fd, staged = tempfile.mkstemp(dir=CATALOG_CACHE, prefix=".cache-")
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
        os.replace(staged, path)
    except OSError:
        pass
    return value


# ---------------------------------------------------------------- listing

def _number(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _rating(value):
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return None


def _item(raw):
    """One search result, as the page shows it."""
    slug = str(raw.get("slug") or "")
    kind = _KIND_BY_TYPE.get(_number(raw.get("type_id")), "addons")
    tags = [str(t.get("name")) for t in raw.get("cf_tags") or []
            if isinstance(t, dict) and t.get("name")]
    return {
        "slug": slug,
        "title": html.unescape(str(raw.get("title") or slug)).strip(),
        "summary": html.unescape(str(raw.get("summary") or
                                     raw.get("introduction") or "")).strip(),
        "image": raw.get("image") or "",
        "author": str(raw.get("display_name") or raw.get("user_nicename")
                      or ""),
        "downloads": _number(raw.get("downloadCount")),
        "rating": _rating(raw.get("average_rating")),
        "kind": kind,
        "tags": list(dict.fromkeys(tags)),
        "updated": str(raw.get("updated_at") or raw.get("sort_date") or ""),
        "url": f"{SITE}/{slug}/",
    }


def search(query="", kind="addons", sort="popular", page=1, refresh=False):
    """A page of the catalogue: ``{"items": [...], "page", "pages", "total"}``.

    An empty ``query`` lists the whole kind in ``sort`` order; a query is
    ranked by relevance, as MCPEDL's own search box does.
    """
    if kind not in KINDS:
        raise CatalogError(f"Unknown content kind: {kind}")
    params = {"type_id": KINDS[kind][1], "page": max(1, int(page))}
    query = " ".join(str(query or "").split())
    if query:
        params["q"] = query[:120]
    else:
        params["sort"] = SORTS.get(sort, SORTS["popular"])[1]

    def fetch():
        data = _get_json("search/advanced", params)
        meta = data.get("meta") or {}
        return {
            "items": [_item(r) for r in data.get("results") or []
                      if isinstance(r, dict) and r.get("slug")],
            "page": _number(meta.get("page")) or params["page"],
            "pages": _number(meta.get("page_count")) or 1,
            "total": _number(meta.get("total")) or 0,
        }

    return _cached("search-" + _cache_key(sorted(params.items())), _LIST_TTL,
                   fetch, refresh)


def featured(kinds=("addons", "maps", "textures"), per_kind=6,
             refresh=False):
    """This week's most popular projects of each kind, for the home page."""
    out = {}
    for kind in kinds:
        try:
            out[kind] = search("", kind, "popular", 1, refresh)["items"][
                :per_kind]
        except CatalogError:
            out[kind] = []
    return out


# ---------------------------------------------------------------- details

def _leaving_target(link):
    """The address behind MCPEDL's exit page (/leaving/?url=…), or the link."""
    parts = urllib.parse.urlsplit(link)
    if parts.path.rstrip("/").endswith("/leaving") or \
            parts.path.startswith("/leaving"):
        target = urllib.parse.parse_qs(parts.query).get("url")
        if target:
            return target[0]
    return link


_SIZE = re.compile(r"\s*\(([\d.,]+\s*[KMG]?B)\)\s*$", re.I)


def _files(downloads):
    """Split a project's downloads into files to install and links to open.

    MCPEDL lists a file once per way it serves it (web, app, "VIP"), all of
    them the same file: keep the first of each name.
    """
    files, links, seen = [], [], set()
    for entry in downloads or []:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("display_name") or entry.get("name") or "")
        name = str(entry.get("name") or label)
        url = str(entry.get("file") or "")
        if not url:
            continue
        if url.startswith("/"):
            url = SITE + url
        size = ""
        match = _SIZE.search(name) or _SIZE.search(label)
        if match:
            size = match.group(1)
        name = _SIZE.sub("", name).strip()
        label = _SIZE.sub("", label).strip() or name
        if _number(entry.get("type")) in (3, 4) or not _host_allowed(url):
            target = _leaving_target(url)
            label = label or urllib.parse.urlsplit(target).hostname or "Link"
            if (label, target) not in seen:
                seen.add((label, target))
                links.append({"name": label, "url": url,
                              "host": urllib.parse.urlsplit(target).hostname
                              or ""})
            continue
        filename = name if name.lower().endswith(INSTALLABLE) else \
            urllib.parse.unquote(Path(urllib.parse.urlsplit(url).path).name)
        if not filename.lower().endswith(INSTALLABLE):
            filename = name or filename
        if filename.lower() in seen:
            continue
        seen.add(filename.lower())
        files.append({
            "name": filename, "label": label, "url": url, "size": size,
            "date": str(entry.get("fileDate") or ""),
            "installable": filename.lower().endswith(INSTALLABLE),
        })
    return files, links


def _text(markup):
    """Readable plain text from a project's HTML description."""
    markup = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", str(markup or ""))
    markup = re.sub(r"(?i)<br\s*/?>|</p>|</h\d>|</li>", "\n", markup)
    markup = re.sub(r"(?i)<li[^>]*>", "• ", markup)
    text = html.unescape(re.sub(r"<[^>]+>", "", markup))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(
        line.strip() for line in text.splitlines())).strip()


def details(slug, refresh=False):
    """Everything the details view shows about one project.

    Not cached for long: the download links MCPEDL hands out for its own
    files are signed, and expire.
    """
    slug = str(slug or "").strip("/ ")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,200}", slug):
        raise CatalogError("That is not an MCPEDL project name.")

    def fetch():
        data = _get_json(f"route/slug/{urllib.parse.quote(slug)}")
        raw = data.get("data") if isinstance(data, dict) else None
        if not isinstance(raw, dict) or not raw.get("slug"):
            raise CatalogError("MCPEDL does not know that project.")
        return raw

    raw = _cached("detail-" + _cache_key(slug), _DETAIL_TTL, fetch, refresh)
    item = _item(raw)
    files, links = _files(raw.get("downloads"))
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    images = [u for u in raw.get("submission_images") or []
              if isinstance(u, str) and u.startswith("https://")]
    versions = [str(v.get("name")) for v in raw.get("minecraft_versions") or []
                if isinstance(v, dict) and v.get("name")]
    item.update({
        "author": str(user.get("display_name") or user.get("username")
                      or item["author"]),
        "summary": item["summary"] or str(raw.get("short_description") or ""),
        "downloads": _number(raw.get("download_count")) or item["downloads"],
        "description": _text(raw.get("description")),
        "installation": _text(raw.get("installation")),
        "images": ([item["image"]] if item["image"] else []) + images,
        "versions": sorted(set(versions), key=_version_key, reverse=True),
        "files": files,
        "links": links,
        "curseforge": str(raw.get("cf_link") or ""),
        "source": str(raw.get("source") or "mcpedl"),
        "updated": str(raw.get("update_date") or raw.get("updated_at")
                       or item["updated"]),
    })
    return item


def _version_key(text):
    return tuple(int(p) for p in re.findall(r"\d+", str(text))[:4])


def supports(item, game_version):
    """Whether a project lists the player's Minecraft version.

    MCPEDL tags versions as 1.21.70 while builds are 1.21.70.3: the first
    three numbers decide. True when the project names no version at all,
    None when there is no build to compare with.
    """
    if not game_version:
        return None
    versions = item.get("versions") or []
    if not versions:
        return True
    want = _version_key(game_version)[:3]
    return any(_version_key(v)[:3] == want for v in versions)


def resolve(text):
    """The MCPEDL slug a pasted link or name points at, or None.

    Takes an mcpedl.com project link, a curseforge.com/minecraft-bedrock
    one (same slug), or a bare slug.
    """
    text = str(text or "").strip()
    if not text:
        return None
    if "://" not in text and "/" not in text:
        return text if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", text) \
            else None
    parts = urllib.parse.urlsplit(text if "://" in text else "https://" + text)
    host = (parts.hostname or "").lower()
    segments = [s for s in parts.path.split("/") if s]
    if host.endswith("mcpedl.com") and segments:
        return segments[-1] if segments[0] != "leaving" else None
    if host.endswith("curseforge.com") and len(segments) >= 3 and \
            segments[0] == "minecraft-bedrock":
        return segments[2]
    return None


# ---------------------------------------------------------------- pictures

def image(url):
    """The bytes of a picture, cached on disk; None when it cannot be had."""
    if not url or not str(url).startswith("https://"):
        return None
    path = CATALOG_CACHE / "images" / _cache_key(url)
    try:
        return path.read_bytes()
    except OSError:
        pass
    try:
        request = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=15) as response:
            data = response.read(MAX_IMAGE + 1)
    except Exception:  # noqa: BLE001 - a missing picture is not an error
        return None
    if len(data) > MAX_IMAGE:
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError:
        pass
    return data


# ---------------------------------------------------------------- install

def _safe_filename(name):
    name = re.sub(r"[^\w.()+ -]+", "_", Path(str(name)).name).strip(" .")
    return name[:120] or "download"


def download(file, progress=None, cancelled=None):
    """Fetch one of ``details()["files"]`` into the cache; its path."""
    url = file["url"]
    target_dir = CATALOG_CACHE / "downloads"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / _safe_filename(file.get("name") or "download")
    fd, staged = tempfile.mkstemp(dir=target_dir, prefix=".part-")
    try:
        try:
            with _open(url, timeout=60) as response, \
                    os.fdopen(fd, "wb") as out:
                fd = None
                total = _number(response.headers.get("Content-Length")) or 0
                if total > MAX_DOWNLOAD:
                    raise CatalogError("That file is too large to install "
                                       "from here.")
                got = 0
                while True:
                    if cancelled is not None and cancelled():
                        raise CatalogError("Download cancelled.")
                    chunk = response.read(1 << 16)
                    if not chunk:
                        break
                    got += len(chunk)
                    if got > MAX_DOWNLOAD:
                        raise CatalogError("That file is too large to "
                                           "install from here.")
                    out.write(chunk)
                    if progress is not None:
                        progress(got, total)
        except urllib.error.HTTPError as exc:
            raise CatalogError(
                f"The download failed (HTTP {exc.code}). The link may have "
                "expired: open the project again and retry.") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise CatalogError("The download stopped: the connection "
                               "dropped.") from exc
        if not zipfile.is_zipfile(staged):
            raise CatalogError(f"{file.get('name')} is not a Minecraft "
                               "content file.")
        os.replace(staged, target)
        return target
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(staged)
        except OSError:
            pass


def _content_suffix(path: Path):
    """The extension ``import_content`` needs for a downloaded file.

    Plenty of projects ship a plain .zip: a world when it holds a level.dat
    at its top, packs when it holds manifests.
    """
    suffix = path.suffix.lower()
    if suffix != ".zip":
        return suffix
    with zipfile.ZipFile(path) as archive:
        names = [PurePosixPath(n) for n in archive.namelist()]
    if any(n.name == "level.dat" and len(n.parts) <= 2 for n in names):
        return ".mcworld"
    if any(n.name == "manifest.json" or n.suffix.lower() in (
            ".mcpack", ".mcaddon", ".mcworld", ".mctemplate")
           for n in names):
        return ".mcaddon"
    raise CatalogError(f"{path.name} holds no world or pack the game can "
                       "use.")


def install(item, file, prefix=None, progress=None, cancelled=None):
    """Download ``file`` of project ``item`` and import it into the game.

    Returns the import's result lines. The folders it created are recorded
    so ``uninstall`` can take them out again.
    """
    from . import content  # noqa: PLC0415 - imports the prefix machinery

    if not file.get("installable", True):
        raise CatalogError(f"{file.get('name')} is not a file the game "
                           "imports.")
    path = download(file, progress, cancelled)
    suffix = _content_suffix(path)
    staged = path
    if suffix != path.suffix.lower():
        staged = path.with_suffix(suffix)
        shutil.move(str(path), str(staged))
    destinations = []
    try:
        results = content.import_content(staged, prefix=prefix,
                                          destinations=destinations)
    finally:
        try:
            staged.unlink()
        except OSError:
            pass
    record = {
        "slug": item.get("slug"), "title": item.get("title"),
        "kind": item.get("kind"), "image": item.get("image"),
        "file": file.get("name"), "installed": time.time(),
        "paths": [str(p) for p in destinations],
        "results": list(results),
    }
    installs = [r for r in load_installs()
                if not (r.get("slug") == record["slug"]
                        and r.get("file") == record["file"])]
    installs.append(record)
    _save_installs(installs)
    return results


def load_installs():
    """What was installed from the catalogue, oldest first."""
    try:
        data = json.loads(INSTALLS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [r for r in data if isinstance(r, dict)] \
        if isinstance(data, list) else []


def _save_installs(records):
    INSTALLS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, staged = tempfile.mkstemp(dir=INSTALLS_FILE.parent,
                                  prefix=".content-installs-")
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(records, stream, indent=1)
    os.replace(staged, INSTALLS_FILE)


def installed_slugs():
    """Slugs with at least one install whose folders are still there."""
    return {r.get("slug") for r in load_installs()
            if any(Path(p).exists() for p in r.get("paths") or [])}


# ---------------------------------------------------------------- library

TRASH = DATA / "content-trash"
_TRASH_DAYS = 30

# com.mojang subfolder -> the library's kind for what lives there.
LIBRARY_KINDS = {
    "behavior_packs": "Behaviour pack",
    "resource_packs": "Resource pack",
    "skin_packs": "Skin pack",
    "minecraftWorlds": "World",
    "world_templates": "World template",
}


def _strip_codes(text):
    return re.sub(r"§.?", "", str(text or "")).strip()


def _describe_folder(sub, folder: Path):
    """One library entry, or None for a folder that is not an item."""
    if sub == "minecraftWorlds":
        if not (folder / "level.dat").exists():
            return None
        try:
            name = (folder / "levelname.txt").read_text(
                encoding="utf-8", errors="replace").strip()
        except OSError:
            name = ""
        icon = folder / "world_icon.jpeg"
        description = ""
    else:
        try:
            manifest = json.loads((folder / "manifest.json").read_text(
                encoding="utf-8-sig", errors="replace"))
            header = manifest.get("header") or {}
        except (OSError, ValueError, AttributeError):
            return None
        name = header.get("name") if isinstance(header, dict) else ""
        description = header.get("description") if isinstance(
            header, dict) else ""
        icon = folder / "pack_icon.png"
        if not icon.exists() and (folder / "world_icon.jpeg").exists():
            icon = folder / "world_icon.jpeg"
        if name == "pack.name" or str(name).startswith("pack."):
            name = ""
    try:
        modified = folder.stat().st_mtime
    except OSError:
        modified = 0
    return {
        "name": _strip_codes(name) or folder.name,
        "description": _strip_codes(description),
        "kind": LIBRARY_KINDS[sub], "sub": sub, "path": str(folder),
        "icon": str(icon) if icon.exists() else "",
        "modified": modified,
    }


def library(prefix=None):
    """Every pack and world the game will find, newest first.

    Packs are read from the shared folder, worlds and templates from the
    signed-in account's own (bol.content, #188), so this is what the game
    lists, not only what came from the catalogue.
    """
    from . import content  # noqa: PLC0415 - imports the prefix machinery

    by_path = {}
    for record in load_installs():
        for raw in record.get("paths") or []:
            by_path[str(raw)] = record
    seen, out = set(), []
    for sub in LIBRARY_KINDS:
        try:
            base = content._content_dir(sub, prefix) / sub
            folders = sorted(base.iterdir())
        except OSError:
            continue
        for folder in folders:
            if not folder.is_dir() or folder.name.startswith("."):
                continue
            key = str(folder.resolve(strict=False))
            if key in seen:
                continue
            seen.add(key)
            entry = _describe_folder(sub, folder)
            if entry is None:
                continue
            record = by_path.get(str(folder))
            entry["slug"] = record.get("slug") if record else None
            out.append(entry)
    out.sort(key=lambda e: e["modified"], reverse=True)
    return out


def _inside_com_mojang(path: Path) -> bool:
    """Whether ``path`` is an item folder: com.mojang/<kind>/<item>."""
    parts = path.parts
    return (len(parts) >= 3 and parts[-3] == "com.mojang"
            and parts[-2] in LIBRARY_KINDS)


def _prune_trash(now=None):
    now = time.time() if now is None else now
    try:
        entries = list(TRASH.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if now - entry.stat().st_mtime > _TRASH_DAYS * 86400:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            pass


def remove(path):
    """Take one item out of the game, into the launcher's trash.

    Never deleted at once: a world is someone's save, so it is kept for
    thirty days in ``content-trash`` before it goes for good.
    """
    path = Path(path)
    if not _inside_com_mojang(path) or not path.is_dir():
        raise CatalogError(f"{path} is not a pack or world folder.")
    _prune_trash()
    TRASH.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = TRASH / f"{stamp}-{path.parent.name}-{path.name}"
    n = 2
    while target.exists():
        target = target.with_name(f"{stamp}-{path.parent.name}-{path.name}"
                                  f"-{n}")
        n += 1
    shutil.move(str(path), str(target))
    # Utime marks when it went in, for the thirty days.
    try:
        os.utime(target)
    except OSError:
        pass
    keep = []
    for record in load_installs():
        paths = [p for p in record.get("paths") or [] if Path(p) != path]
        if paths:
            keep.append(dict(record, paths=paths))
    _save_installs(keep)
    return target


def uninstall(slug):
    """Remove every folder installed for ``slug``; how many went."""
    removed = 0
    for record in load_installs():
        if record.get("slug") != slug:
            continue
        for raw in record.get("paths") or []:
            if Path(raw).is_dir() and _inside_com_mojang(Path(raw)):
                remove(raw)
                removed += 1
    _save_installs([r for r in load_installs() if r.get("slug") != slug])
    return removed
