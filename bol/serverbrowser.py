"""BedrockOnLinux's own Servers tab: one page of tiles, every server.

The game's Servers tab lists the player's own servers next to Microsoft's
featured ones, one name per line. With the ``server_browser`` setting on
(the default) it becomes a page built like a game store, for the mouse and
the controller alike:

* a spotlight at the top describing the tile that has focus (artwork,
  players online, categories, description, address) with Play and Add to
  my servers;
* a search bar (name, address, category or any word of a description) and
  a language choice;
* the player's own servers (the game's ``external_servers.txt``);
* BedrockOnLinux's partners, each in a tile the width of the page;
* the featured experiences, in big tiles: the servers of the game's own
  featured layout (The Hive, CubeCraft,
  MegaSMP...: the second Servers tab some accounts get, read for its lists
  alone), or the game's featured servers where there is no layout, with the
  banner and player count their details give;
* rows of tiles: the most played servers in the game's language and
  overall, one row per category, then every server of the community list
  github.com/Wyze3306/bedrock-servers-list, each with the logo and cover
  that list stores for it.

A tile opens the server's own page (big picture, description, activities,
its other pictures; Play, Add to my servers, Back) at
``/play/servers#<card key>``: a history entry of its own, so the game's
Back leaves it, which the game's tab does not read (its ``:id`` it would
load, and a catalogue key there crashes the game). The spotlight's Play, and the
page's, join it in one press. A server of the community list is
first added to the player's own list, since the game only joins servers it
knows, then joined the way the game's own details pane does: straight away
when the player has turned the safety warning off, through the game's
warning screen otherwise.

The player counts come from the RakNet status ping every Bedrock server
answers, sent to the whole list in the background at each launch: the page
shows the last answers, and the bundle's first line takes the new ones when
they arrive, usually before the game reaches its menu.

How it gets there: bol/hbui/serverbrowser.js holds the React components,
written against placeholders. ``_discover`` reads, from anchors the Servers
tab cannot drop without being rewritten, the minified name each build gives
the components, hooks and engine facets they use, and the components are
pasted into the bundle's own scope next to the vanilla tab. The vanilla tab
then draws the page instead of its own two panes (and the game keeps that
tab rather than its second one), but only while the
``__bolServerBrowser`` global on the bundle's first line says so: switching
the setting off rewrites that line alone, and the game is back to its own
tab without the bundle being restored.

All or nothing, like the partner patch it rides on: a build whose Servers tab
no longer matches keeps its vanilla tab, and the launcher says so.
"""
# SPDX-License-Identifier: MIT

import json
import pkgutil
import re
import shutil
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .config import APP, CACHE, VERSION
from .log import warn

SETTING = "server_browser"
LIST_URL = ("https://raw.githubusercontent.com/Wyze3306/bedrock-servers-list/"
            "main/servers.json")
LIST_CACHE = CACHE / "server-list"
_LIST_FILE = "servers.json"
_LIST_TTL = 12 * 3600
_LIST_TIMEOUT = 4
_MAX_LIST_BYTES = 8 << 20
_MAX_IMAGE_BYTES = 1 << 20
_IMAGE_HOSTS = ("minecraftpocket-servers.com", "raw.githubusercontent.com")
_IMAGE_TYPES = {b"\x89PNG": ".png", b"\xff\xd8\xff": ".jpg", b"GIF8": ".gif"}
_ASSET_PREFIX = "bol-srv-"
_DESCRIPTION_CHARS = 1500

# Marks a bundle carrying the components, and is the global they read.
MARK = "bol_HubView"
GLOBAL = "__bolServerBrowser"

# A tag every entry carries says nothing: the list's importer once stamped
# eight of them on all 659 servers. Those are left out of the categories.
_UBIQUITOUS_TAG = 0.6
_MIN_CATEGORY = 3

# --------------------------------------------------------------------------
# Strings the tab shows, by game language. Anything missing falls back to
# English.

STRINGS = {
    "en": {
        "search": "Search",
        "searchPlaceholder": "Name, address, category or keyword",
        "language": "Language",
        "allLanguages": "All languages",
        "gameMode": "Game mode",
        "allModes": "All modes",
        "addServer": "Add a server",
        "myServers": "My servers",
        "myServersHint": "The ones you added or played",
        "featured": "Featured experiences",
        "featuredHint": "The game's featured servers",
        "partners": "BedrockOnLinux partner",
        "partnerTag": "Partner",
        "partnersHint": "Servers that support BedrockOnLinux",
        "details": "Details",
        "back": "Back",
        "about": "About",
        "pictures": "Pictures",
        "activities": "Activities",
        "address": "Address: {0}",
        "website": "Website: {0}",
        "discord": "Discord: {0}",
        "players": "{0} / {1} players",
        "popular": "Most played",
        "popularIn": "Most played in {0}",
        "pingedHint": "Players online at launch",
        "everyServer": "Every server",
        "results": "Results",
        "count": "{0} servers",
        "seeAll": "See all",
        "showMore": "Show more",
        "noMatch": "No server matches this search.",
        "listUnavailable": "The server list could not be downloaded. "
                           "It is fetched again at the next launch.",
        "online": "{0} online",
        "noAnswer": "No answer",
        "server": "Server",
        "play": "Play",
        "favorite": "Add to my servers",
        "unfavorite": "Remove from my servers",
        "added": "{0} is now in your servers.",
        "removed": "{0} was removed from your servers.",
        "alreadyAdded": "This address is already in your servers under "
                        "another name.",
        "addFailed": "This server could not be added.",
        "removeFailed": "This server could not be removed.",
        "version": "Version {0}",
    },
    "fr": {
        "search": "Rechercher",
        "searchPlaceholder": "Nom, adresse, catégorie ou mot-clé",
        "language": "Langue",
        "allLanguages": "Toutes les langues",
        "gameMode": "Mode de jeu",
        "allModes": "Tous les modes",
        "addServer": "Ajouter un serveur",
        "myServers": "Mes serveurs",
        "myServersHint": "Ceux que vous avez ajoutés ou joués",
        "featured": "Expériences en vedette",
        "featuredHint": "Les serveurs en vedette du jeu",
        "partners": "Partenaire BedrockOnLinux",
        "partnerTag": "Partenaire",
        "partnersHint": "Des serveurs qui soutiennent BedrockOnLinux",
        "details": "Détails",
        "back": "Retour",
        "about": "À propos",
        "pictures": "Images",
        "activities": "Activités",
        "address": "Adresse : {0}",
        "website": "Site : {0}",
        "discord": "Discord : {0}",
        "players": "{0} / {1} joueurs",
        "popular": "Les plus joués",
        "popularIn": "Les plus joués en {0}",
        "pingedHint": "Joueurs en ligne au lancement",
        "everyServer": "Tous les serveurs",
        "results": "Résultats",
        "count": "{0} serveurs",
        "seeAll": "Tout voir",
        "showMore": "Afficher plus",
        "noMatch": "Aucun serveur ne correspond à cette recherche.",
        "listUnavailable": "La liste des serveurs n'a pas pu être "
                           "téléchargée. Nouvel essai au prochain lancement.",
        "online": "{0} en ligne",
        "noAnswer": "Pas de réponse",
        "server": "Serveur",
        "play": "Jouer",
        "favorite": "Ajouter à mes serveurs",
        "unfavorite": "Retirer de mes serveurs",
        "added": "{0} est maintenant dans vos serveurs.",
        "removed": "{0} a été retiré de vos serveurs.",
        "alreadyAdded": "Cette adresse est déjà dans vos serveurs sous un "
                        "autre nom.",
        "addFailed": "Ce serveur n'a pas pu être ajouté.",
        "removeFailed": "Ce serveur n'a pas pu être retiré.",
        "version": "Version {0}",
    },
}

LANGUAGE_NAMES = {
    "en": {"en": "English", "fr": "French", "de": "German", "es": "Spanish",
           "pt": "Portuguese", "pt-BR": "Portuguese (Brazil)",
           "it": "Italian", "nl": "Dutch", "pl": "Polish", "ru": "Russian",
           "tr": "Turkish", "id": "Indonesian", "vi": "Vietnamese",
           "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "ar": "Arabic",
           "th": "Thai", "uk": "Ukrainian", "cs": "Czech", "hu": "Hungarian",
           "ro": "Romanian", "sv": "Swedish", "fi": "Finnish", "da": "Danish",
           "no": "Norwegian", "el": "Greek", "he": "Hebrew", "hi": "Hindi",
           "ms": "Malay", "tl": "Filipino", "fa": "Persian"},
    "fr": {"en": "Anglais", "fr": "Français", "de": "Allemand",
           "es": "Espagnol", "pt": "Portugais",
           "pt-BR": "Portugais (Brésil)", "it": "Italien",
           "nl": "Néerlandais", "pl": "Polonais", "ru": "Russe",
           "tr": "Turc", "id": "Indonésien", "vi": "Vietnamien",
           "zh": "Chinois", "ja": "Japonais", "ko": "Coréen", "ar": "Arabe",
           "th": "Thaï", "uk": "Ukrainien", "cs": "Tchèque",
           "hu": "Hongrois", "ro": "Roumain", "sv": "Suédois",
           "fi": "Finnois", "da": "Danois", "no": "Norvégien", "el": "Grec",
           "he": "Hébreu", "hi": "Hindi", "ms": "Malais", "tl": "Filipino",
           "fa": "Persan"},
}


def enabled(settings):
    return bool(settings.get(SETTING, True))


def ui_language(game_language, environ=None):
    """The tab's language for the game's ``game_language`` (``fr_FR``).

    The game leaves the setting empty until the player picks a language,
    and then follows the system's, which under Wine is the launcher's
    locale: LC_ALL, then LC_MESSAGES, then LANG.
    """
    code = str(game_language or "").split("_")[0].lower()
    if not code:
        import os
        env = os.environ if environ is None else environ
        for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
            value = str(env.get(var) or "")
            if value and value not in ("C", "POSIX"):
                code = value.split(".")[0].split("_")[0].lower()
                break
    return code if code in STRINGS else "en"


# --------------------------------------------------------------------------
# The community list


def _get(url, limit, timeout):
    request = urllib.request.Request(
        url, headers={"User-Agent": f"{APP}/{VERSION}"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        blob = response.read(limit + 1)
    if len(blob) > limit:
        raise ValueError(f"{url} is larger than {limit} bytes")
    return blob


def _valid_entries(data):
    if not isinstance(data, list):
        return None
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name, ip = item.get("name"), item.get("ip")
        if not (isinstance(name, str) and name.strip()
                and isinstance(ip, str) and ip.strip()):
            continue
        out.append(item)
    return out


def fetch_list(cache=LIST_CACHE, now=None, timeout=_LIST_TIMEOUT,
               getter=_get):
    """The community list: refreshed when the cache is stale, else cached.

    Never raises. A failed refresh keeps the last list; with none ever
    downloaded the result is empty and the All section says so.
    """
    now = time.time() if now is None else now
    path = Path(cache) / _LIST_FILE
    cached = None
    try:
        cached = _valid_entries(json.loads(path.read_text(encoding="utf-8")))
        fresh = now - path.stat().st_mtime < _LIST_TTL
    except (OSError, ValueError):
        fresh = False
    if cached is not None and fresh:
        return cached
    try:
        blob = getter(LIST_URL, _MAX_LIST_BYTES, timeout)
        entries = _valid_entries(json.loads(blob.decode("utf-8")))
        if not entries:
            raise ValueError("the server list is empty")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(blob)
        tmp.replace(path)
        return entries
    except (OSError, ValueError, urllib.error.URLError) as e:
        if cached is None:
            warn(f"The community server list could not be downloaded: {e}")
        else:
            try:  # retried at the next refresh window, not every launch
                path.touch()
            except OSError:
                pass
        return cached or []


def _slug(entry):
    raw = str(entry.get("id") or entry.get("name") or "")
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    return slug or "server"


def _image_dims(blob):
    """(width, height) of a PNG, GIF or JPEG, or None."""
    try:
        if blob.startswith(b"\x89PNG"):
            return struct.unpack(">II", blob[16:24])
        if blob.startswith(b"GIF8"):
            return struct.unpack("<HH", blob[6:10])
        if blob.startswith(b"\xff\xd8"):
            i = 2
            while i + 9 < len(blob):
                if blob[i] != 0xFF:
                    return None
                marker = blob[i + 1]
                size = struct.unpack(">H", blob[i + 2:i + 4])[0]
                if marker in (0xC0, 0xC1, 0xC2):
                    h, w = struct.unpack(">HH", blob[i + 5:i + 9])
                    return w, h
                i += 2 + size
    except struct.error:
        return None
    return None


def _image_kind(blob):
    for magic, ext in _IMAGE_TYPES.items():
        if blob.startswith(magic):
            return ext
    return None


def _image_allowed(url):
    parts = urllib.parse.urlsplit(str(url or ""))
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(
        host == h or host.endswith("." + h) for h in _IMAGE_HOSTS)


# A server's pictures, by the payload key the tab reads them under: its
# square logo, its wide cover and the list's old banner strip, each cached
# in its own folder.
ART = (("lg", "logo", "logos"), ("cv", "cover", "covers"),
       ("img", "image", "images"))
# And a few more pictures for its page, cached as <slug>-<n>.
GALLERY_DIR = "gallery"
_GALLERY_MAX = 4


def _image_url(entry, field="image"):
    url = entry.get(field) or ""
    return url if isinstance(url, str) and _image_allowed(url) else ""


def _art_of(entry):
    """(payload key, URL, folder, file stem) of each picture of one entry:
    logo, cover, banner, then its gallery as g0, g1..."""
    slug = _slug(entry)
    for key, field, sub in ART:
        url = _image_url(entry, field)
        if url:
            yield key, url, sub, slug
    urls = entry.get("gallery")
    n = 0
    for url in urls if isinstance(urls, list) else ():
        if n == _GALLERY_MAX:
            break
        if isinstance(url, str) and _image_allowed(url):
            yield f"g{n}", url, GALLERY_DIR, f"{slug}-{n}"
            n += 1


def _art_rank(key):
    """Logos first, then covers, banners and the galleries."""
    keys = [k for k, _f, _s in ART]
    return keys.index(key) if key in keys else len(keys) + int(key[1:])


def cached_images(cache=LIST_CACHE, folder="images"):
    """{slug: Path} of the pictures of one kind already downloaded."""
    out = {}
    try:
        for p in (Path(cache) / folder).iterdir():
            if p.suffix in (".png", ".jpg", ".gif") and p.stat().st_size:
                out[p.stem] = p
    except OSError:
        pass
    return out


def download_images(entries, cache=LIST_CACHE, getter=_get, budget=None):
    """Fetch the logos, covers, banners and galleries not cached yet;
    returns how many were added.

    Runs off the launch path (see ``prefetch_images``): the game only shows
    the pictures already on disk when PLAY patched it, so a new one appears
    from the following launch on.
    """
    added = 0
    deadline = None if budget is None else time.monotonic() + budget
    todo = sorted((item for entry in entries for item in _art_of(entry)),
                  key=lambda item: _art_rank(item[0]))
    have = {}
    for _key, url, sub, stem in todo:
        if deadline is not None and time.monotonic() > deadline:
            return added
        if sub not in have:
            have[sub] = cached_images(cache, sub)
        if stem in have[sub]:
            continue
        folder = Path(cache) / sub
        try:
            blob = getter(url, _MAX_IMAGE_BYTES, 10)
            ext = _image_kind(blob)
            if not ext or not _image_dims(blob):
                continue
            folder.mkdir(parents=True, exist_ok=True)
            (folder / (stem + ext)).write_bytes(blob)
            have[sub][stem] = folder / (stem + ext)
            added += 1
        except (OSError, ValueError, urllib.error.URLError):
            continue
    return added


def prefetch_images(entries, cache=LIST_CACHE):
    """``download_images`` in a daemon thread. Never in PLAY's way."""
    t = threading.Thread(target=download_images, args=(entries, cache),
                         name="bol-server-art", daemon=True)
    t.start()
    return t


# --------------------------------------------------------------------------
# Who is online: the RakNet "unconnected ping" every Bedrock server answers
# (the game's own LAN and server-list pings), sent once per launch.

_RAKNET_MAGIC = bytes.fromhex("00ffff00fefefefefdfdfdfd12345678")
_STATUS_FILE = "status.json"
_STATUS_TTL = 24 * 3600
_PING_TIMEOUT = 2.5
_RESOLVE_WORKERS = 48


def _target_key(host, port):
    return f"{str(host).strip().lower()}:{int(port)}"


def _ping_packet(stamp):
    return (b"\x01" + struct.pack(">q", stamp) + _RAKNET_MAGIC
            + struct.pack(">q", 0x424F4C))


def parse_pong(blob):
    """{"o": online, "m": max} from an unconnected pong, or None."""
    if len(blob) < 35 or blob[0] != 0x1C or blob[17:33] != _RAKNET_MAGIC:
        return None
    size = struct.unpack(">H", blob[33:35])[0]
    fields = blob[35:35 + size].decode("utf-8", "replace").split(";")
    if len(fields) < 6 or fields[0] not in ("MCPE", "MCEE"):
        return None
    try:
        online, most = int(fields[4]), int(fields[5])
    except ValueError:
        return None
    if online < 0 or most < 0:
        return None
    return {"o": online, "m": most}


def _resolve(host):
    import socket
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET,
                                   socket.SOCK_DGRAM)
    except (OSError, UnicodeError):
        return None
    return infos[0][4][0] if infos else None


def ping_servers(targets, timeout=_PING_TIMEOUT, resolve=_resolve,
                 sock=None):
    """{"host:port": {"o", "m"}} for every target that answered in time.

    ``targets`` is an iterable of (host, port). One UDP socket, one packet
    per server; names are resolved in parallel first. Never raises: no
    network means an empty answer.
    """
    import socket
    from concurrent.futures import ThreadPoolExecutor, wait
    targets = {(_target_key(h, p), str(h).strip().lower(), int(p))
               for h, p in targets}
    if not targets:
        return {}
    deadline = time.monotonic() + timeout
    hosts = sorted({h for _, h, _ in targets})
    pool = ThreadPoolExecutor(max_workers=_RESOLVE_WORKERS,
                              thread_name_prefix="bol-resolve")
    futures = {h: pool.submit(resolve, h) for h in hosts}
    wait(futures.values(), timeout=max(0.1, timeout * 0.6))
    pool.shutdown(wait=False, cancel_futures=True)
    addresses = {}
    for key, host, port in targets:
        f = futures[host]
        ip = f.result() if f.done() and not f.cancelled() else None
        if ip:
            addresses.setdefault((ip, port), []).append(key)
    out = {}
    own = sock is None
    try:
        if own:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        packet = _ping_packet(int(time.monotonic() * 1000))
        for address in addresses:
            try:
                sock.sendto(packet, address)
            except OSError:
                continue
        import select
        while len(out) < len(targets):
            left = deadline - time.monotonic()
            if left <= 0:
                break
            ready, _, _ = select.select([sock], [], [], left)
            if not ready:
                break
            try:
                blob, address = sock.recvfrom(2048)
            except OSError:
                continue
            pong = parse_pong(blob)
            if pong:
                for key in addresses.get((address[0], address[1]), ()):
                    out[key] = pong
    except OSError:
        pass
    finally:
        if own and sock is not None:
            sock.close()
    return out


def ping_targets(entries, mine=None):
    out = []
    for e in entries:
        try:
            out.append((str(e["ip"]).strip(), int(e.get("port") or 19132)))
        except (KeyError, TypeError, ValueError):
            continue
    for key in mine or {}:
        host, _, port = key.rpartition(":")
        if host and port.isdigit():
            out.append((host, int(port)))
    return out


def cached_status(cache=LIST_CACHE, now=None):
    """The last ping's answers, or None when there is none from today."""
    now = time.time() if now is None else now
    try:
        data = json.loads((Path(cache) / _STATUS_FILE).read_text("utf-8"))
        if now - float(data["t"]) > _STATUS_TTL:
            return None
        status = data["s"]
        return status if isinstance(status, dict) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_status(status, cache=LIST_CACHE, now=None):
    path = Path(cache) / _STATUS_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"t": time.time() if now is None else now,
                                   "s": status}), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def refresh_status(targets, on_done=None, cache=LIST_CACHE):
    """Ping in a daemon thread, save the answers, then ``on_done(status)``.

    PLAY never waits on it: the game takes far longer to reach its menu than
    the ping takes, and ``on_done`` rewrites the bundle's first line in time
    in the usual case. Otherwise the counts saved here show next launch.
    """
    def run():
        status = ping_servers(targets)
        if not status:  # offline: keep the last counts rather than none
            return
        last = cached_status(cache) or {}
        if len(status) < len(last) // 2:
            # Far fewer answers than last time: this round lost them (a
            # swapping machine, a flaky link), half the servers did not go
            # down. The silent ones keep their last counts.
            status = dict(last, **status)
        save_status(status, cache)
        if on_done is not None:
            try:
                on_done(status)
            except Exception as e:  # cosmetic: a stale count at worst
                warn(f"Could not show the live player counts: {e}")
    t = threading.Thread(target=run, name="bol-server-ping", daemon=True)
    t.start()
    return t


# --------------------------------------------------------------------------
# The player's own list, which the game hands the UI by name only: its
# addresses tell which catalogue servers the player already has.

def player_servers(data_dirs):
    """{"host:port": name} from every ``external_servers.txt`` given."""
    out = {}
    for folder in data_dirs:
        try:
            text = (Path(folder) / "external_servers.txt").read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            # id:name:host:port:timestamp -- a name may hold colons.
            parts = line.strip().split(":")
            if len(parts) < 5 or not parts[-2].isdigit():
                continue
            name = ":".join(parts[1:-3]).strip()
            host = parts[-3].strip()
            if name and host:
                out[_target_key(host, parts[-2])] = name
    return out


# --------------------------------------------------------------------------
# What the injected tab reads


def _language_name(code, ui):
    names = LANGUAGE_NAMES.get(ui, LANGUAGE_NAMES["en"])
    return names.get(code) or names.get(code.split("-")[0]) or code.upper()


def _language_in_sentence(name, ui):
    # "Most played in French", but "Les plus joués en français".
    return name.lower() if ui == "fr" else name


# Each picture's height, in % of its width, under its own key.
_RATIO_KEYS = {"img": "ih", "lg": "lh", "cv": "ch"}


def build_payload(entries, *, on=True, ui="en", images=None,
                  partner_hosts=(), status=None, mine=None, partners=()):
    """The ``__bolServerBrowser`` object: settings, strings, servers.

    ``images`` maps a server's slug to its pictures (``install_images``):
    by payload key, the asset name each was copied under and its height as
    a percentage of its width. ``status`` is
    ``ping_servers``' answer ({"host:port": {"o", "m"}}), None when no ping
    ran: then nobody is shown offline. ``mine`` maps the player's own
    servers' "host:port" to the name they gave them. ``partners`` are
    BedrockOnLinux's partner servers as the page shows them, in their own
    box and on their own page (``branding.browser_partners``).
    """
    images = images or {}
    total = len(entries) or 1
    counts = {}
    for e in entries:
        for tag in e.get("tags") or ():
            if isinstance(tag, str) and tag.strip():
                counts[tag.strip()] = counts.get(tag.strip(), 0) + 1
    noise = {t for t, n in counts.items()
             if n / total >= _UBIQUITOUS_TAG and total >= 20}
    partner_hosts = {h.lower() for h in partner_hosts}
    servers, langs, seen = [], {}, set()
    for e in entries:
        slug = _slug(e)
        if slug in seen:
            continue
        seen.add(slug)
        try:
            port = int(e.get("port") or 19132)
        except (TypeError, ValueError):
            port = 19132
        lang = str(e.get("lang") or "")
        tags = [t.strip() for t in e.get("tags") or ()
                if isinstance(t, str) and t.strip()
                and t.strip() not in noise]
        desc = str(e.get("description") or "").strip()
        if len(desc) > _DESCRIPTION_CHARS:
            desc = desc[:_DESCRIPTION_CHARS].rstrip() + "…"
        s = {"i": "bol-" + slug, "n": str(e["name"]).strip(),
             "a": str(e["ip"]).strip(), "p": port, "l": lang,
             "d": desc, "t": tags}
        if lang:
            s["ln"] = _language_name(lang, ui)
            langs[lang] = langs.get(lang, 0) + 1
        for key, field in (("c", "country"), ("v", "version")):
            if e.get(field):
                s[key] = str(e[field]).strip()
        for key, field in (("w", "website"), ("dc", "discord")):
            url = str(e.get(field) or "").strip()
            if url.startswith(("https://", "http://")):
                s[key] = url
        for key, (name, ratio) in sorted(images.get(slug, {}).items(),
                                         key=lambda kv: _art_rank(kv[0])):
            if key in _RATIO_KEYS:
                s[key], s[_RATIO_KEYS[key]] = name, ratio
            else:
                s.setdefault("gl", []).append([name, ratio])
        if s["a"].lower() in partner_hosts:
            s["pt"] = 1
        if status is not None:
            live = status.get(_target_key(s["a"], port))
            if live:
                s["on"], s["mx"] = live["o"], live["m"]
            else:
                s["off"] = 1
        servers.append(s)
    cats = sorted(((t, n) for t, n in counts.items()
                   if t not in noise and n >= _MIN_CATEGORY),
                  key=lambda c: (-c[1], c[0].lower()))
    lang_list = sorted(langs.items(), key=lambda c: (-c[1], c[0]))
    strings = dict(STRINGS["en"])
    strings.update(STRINGS.get(ui, {}))
    return {
        "on": bool(on),
        "ui": ui,
        "strings": strings,
        "servers": servers,
        "cats": [[t, n] for t, n in cats],
        "langs": [[c, n, _language_name(c, ui),
                   _language_in_sentence(_language_name(c, ui), ui)]
                  for c, n in lang_list],
        "mine": dict(mine or {}),
        "pinged": status is not None,
        "partners": list(partners),
    }


def runtime_line(payload):
    """One line of JavaScript defining the global; no newline inside."""
    text = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    # Inside a <script>-less bundle "</" is harmless, but U+2028/9 and a
    # stray newline are not: ensure_ascii escapes both.
    return "var " + GLOBAL + "=" + text + ";"


def rewrite_runtime(js, payload):
    """Swap the global on a patched bundle's first line; True if done.

    Written to a temporary file and renamed over the bundle, so the game
    reading it at that moment sees the old file or the new one, whole.
    """
    js = Path(js)
    try:
        data = js.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    head, sep, body = data.partition("\n")
    at = head.find("var " + GLOBAL + "=")
    if at == -1 or MARK not in body:
        return False
    new = head[:at] + runtime_line(payload) + sep + body
    if new == data:
        return True
    try:
        tmp = js.with_name(js.name + ".bol-tmp")
        tmp.write_text(new, encoding="utf-8")
        tmp.replace(js)
    except OSError:
        return False
    return True


def install_images(hbui, entries, cache=LIST_CACHE):
    """Copy the cached pictures into one game's HBUI assets.

    Returns the ``images`` mapping ``build_payload`` takes: {slug: {key:
    (asset, height %)}}, the gallery under g0, g1... A picture that cannot
    be read or copied is simply not shown.
    """
    assets = Path(hbui) / "assets"
    out, have = {}, {}
    for e in entries:
        slug = _slug(e)
        for key, _url, sub, stem in _art_of(e):
            if sub not in have:
                have[sub] = cached_images(cache, sub)
            src = have[sub].get(stem)
            if src is None:
                continue
            kind = ("" if key == "img"
                    else "gl-" if sub == GALLERY_DIR else key + "-")
            try:
                blob = src.read_bytes()
                dims = _image_dims(blob)
                if not dims or not dims[0]:
                    continue
                name = _ASSET_PREFIX + kind + stem + src.suffix
                dst = assets / name
                if not dst.is_file() or dst.stat().st_size != len(blob):
                    shutil.copyfile(src, dst)
            except OSError:
                continue
            out.setdefault(slug, {})[key] = (
                "assets/" + name, round(dims[1] * 100 / dims[0], 2))
    return out


# --------------------------------------------------------------------------
# The bundle patch

_N = r"[\w$]+"
# A hook as each bundler writes it: a bare name, or `(0,r.useX)`.
_HOOK = r"(?:\(0,[\w$]+\.[\w$]+\)|[\w$]+)"
_COMPONENT = r"(?:[\w$]+\.)?[\w$]+"


def _template():
    blob = pkgutil.get_data("bol", "hbui/serverbrowser.js")
    if not blob:
        raise OSError("bundled hbui/serverbrowser.js is missing")
    return blob.decode("utf-8")


def _one(pattern, text, group=1):
    m = re.search(pattern, text)
    return m.group(group) if m else None


def _skip_string(data, i):
    """Index just past the string literal opening at ``data[i]``."""
    quote = data[i]
    i += 1
    while i < len(data):
        c = data[i]
        if c == "\\":
            i += 2
            continue
        if c == quote:
            return i + 1
        if quote == "`" and data.startswith("${", i):
            i = _match(data, i + 1, "{", "}")
            continue
        i += 1
    return len(data)


def _match(data, i, opening, closing):
    """Index just past the bracket matching the one at ``data[i]``."""
    depth = 0
    while i < len(data):
        c = data[i]
        if c in "\"'`":
            i = _skip_string(data, i)
            continue
        if c == opening:
            depth += 1
        elif c == closing:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(data)


def _function_at(data, start):
    """The text of the function declaration starting at ``start``.

    Brackets are matched, skipping string and template literals; the
    minified UI code holds no regular expression literal with a brace.
    """
    params = data.find("(", start)
    if params == -1:
        return data[start:]
    body = data.find("{", _match(data, params, "(", ")"))
    if body == -1:
        return data[start:]
    return data[start:_match(data, body, "{", "}")]


def _function_named(data, name):
    """``(start, text)`` of ``function name(``, or ``(None, None)``."""
    m = re.search(r"function " + re.escape(name) + r"\(", data)
    if not m:
        return None, None
    return m.start(), _function_at(data, m.start())


_TAB = re.compile(
    r"function (" + _N + r")\(\{routePrefix:" + _N + r'="/play",'
    r"showFeaturedServers:")
_TAB_ANCHOR = re.compile(
    r"(" + _N + r")\.createElement\((" + _N + r"),\{containerRef:" + _N
    + r",delegateFocusByAlias:(" + _N + r")\.SERVER_SIDE_MENU,"
    r"delegateFocusFromMemory:!0\}")


def _facet_def(data, name):
    return _one(r"(" + _N + r")=" + _HOOK + r'\("vanilla\.' + name + r'"\)',
                data)


def _discover(data):
    """{placeholder: minified name} for this bundle, or None.

    Also returns where the vanilla tab starts and its text, which the patch
    edits, as ``_tab_start`` / ``_tab``.
    """
    anchor = _TAB_ANCHOR.search(data)
    if not anchor:
        return None
    tab = None
    for m in _TAB.finditer(data, 0, anchor.start()):
        tab = m
    if tab is None:
        return None
    tab_text = _function_at(data, tab.start())
    if anchor.start() > tab.start() + len(tab_text):
        return None
    R = re.escape(anchor.group(1))
    names = {"R": anchor.group(1), "FOCUS": anchor.group(2),
             "ALIAS": anchor.group(3)}
    ce = R + r"\.createElement\("
    names["SIDE"] = _one(ce + r"(" + _N + r"),\{loading:" + _N
                         + r",routePrefix:" + _N + r",showFeaturedServers:",
                         tab_text)
    names["DETAILS"] = _one(ce + r"(" + _N + r"),\{loading:" + _N
                            + r",scrollViewRef:", tab_text)
    names["UFM"] = _one(r"=(" + _HOOK + r")\(\(?" + _N + r"=>" + _N
                        + r"\.fetchThirdPartyWorldsTaskState", tab_text)
    names["UFC"] = _one(r"=(" + _HOOK + r")\(\(?" + _N + r"=>\(" + _N + ","
                        + _N + r"\)=>\{" + _N + r"\.loadNetworkWorldDetails",
                        tab_text)
    m = re.search(r"===(" + _N + r")\((" + _N + r")\.External\)\?", tab_text)
    names["JU"], names["KD"] = (m.group(1), m.group(2)) if m else (None, None)
    names["EXT"] = _facet_def(data, "externalServerWorldList")
    names["TP"] = _facet_def(data, "thirdPartyWorldList")
    names["DET"] = _facet_def(data, "networkWorldDetails")
    names["JOINER"] = _facet_def(data, "networkWorldJoiner")
    names["NOTIF"] = _facet_def(data, "notificationOptions")
    if names["EXT"]:
        names["USF"] = _one(r"=(" + _HOOK + r")\(" + re.escape(names["EXT"])
                            + r"\)", tab_text)
    names["UNWRAP"] = _one(ce + r"(" + _COMPONENT + r"),\{data:" + _N
                           + r"\},\(?" + _N + r"=>\{switch\(" + _N
                           + r'\)\{case"banned"', data)

    if not names["SIDE"] or not names["DETAILS"]:
        return None
    _, side = _function_named(data, names["SIDE"])
    if not side:
        return None
    m = re.search(ce + r"(" + _N + r"),\{onClick:" + _N + r",Icon:(" + _N
                  + r")\.Plus,inputLegend:", side)
    names["BUTTON"], names["ICONS"] = (m.group(1), m.group(2)) if m else (
        None, None)
    router_var = _one(r"(" + _N + r")\.push\(`\$\{" + _N
                      + r"\}/servers/add`\)", side)
    if router_var:
        names["ROUTER"] = _one(r"[,{]" + re.escape(router_var) + r"=("
                               + _N + r")\(\)[,;]", side)

    # The details layout of a featured server, for the catalogue's own.
    names["PANEL"] = _one(ce + r"(" + _N + r"),\{focusable:!0,"
                          r'role:"neutral80",narrationText:', data)
    names["IMAGE"] = _one(ce + r"(" + _N + r"),\{aspectRatio:" + _N
                          + r'\.AspectRatio\["10:3"\]', data)
    _, details = _function_named(data, names["DETAILS"])
    if details:
        names["SCROLL"] = _one(ce + r"(" + _N + r"),\{loading:" + _N
                               + r",innerRef:" + _N + r",floating:", details)

    # The add-server form's text field and the facet state hook it uses.
    field = re.search(ce + r"(" + _N + r"),\{label:" + _N
                      + r'\("\.serverNameLabel"\),value:(' + _N + r")",
                      data)
    if field:
        names["TEXTFIELD"] = field.group(1)
        before = data[max(0, field.start() - 20000):field.start()]
        states = re.findall(r"\[" + re.escape(field.group(2)) + r","
                            + _N + r"\]=(" + _HOOK + r")\(", before)
        names["UFS"] = states[-1] if states else None
    # The game's gamepad shortcut: a button, its legend, its callback.
    m = re.search(ce + r"(" + _N + r"),\{options:\{button:(" + _N
                  + r")\.Y,inputLegend:", data)
    names["LEGEND"], names["KEYS"] = (m.group(1), m.group(2)) if m else (
        None, None)
    # The router's hook for the current location's hash: `/play/servers#<k>`
    # opens a server's own page. Not the route's `:id`: the game's tab loads
    # whatever server that names, and a catalogue key there crashes the
    # game. Optional: without it a tile joins at once.
    m = re.search(r"[,;{ ](" + _N + r")=\(\)=>" + _HOOK + r"\(\(?\(\{list:("
                  + _N + r")\},(" + _N + r")\)=>\3\?\2\[\2\.length-1\]\.hash:",
                  data)
    if m:
        names["HASH"] = m.group(1)
    m = re.search(r"\{routePrefix:(" + _N + r')="/play",', tab_text)
    names["_prefix"] = m.group(1) if m else None
    names.update(_layout_names(data))
    if not names["_prefix"] or not all(names.get(k) for k in _PLACEHOLDERS):
        return None
    names["_tab_start"] = tab.start()
    names["_tab"] = tab_text
    names["_tab_name"] = tab.group(1)
    return names


# The game's second Servers tab (1.26.60 on, behind the account flag
# ``mc-servers-tab-v2``) is an online layout of "experience" blocks: a hero
# banner, then lists of experiences (servers). The page reads those lists
# for its featured row, in its own tiles. Optional: without them the row
# shows the game's featured servers.
_LAYOUT_HOOK = re.compile(
    r"\{fabIds:" + _N + r",isLoading:" + _N + r",hasError:" + _N + r"\}=("
    + _HOOK + r")\((" + _N + r")(?:\|\|(" + _N + r"))?\)")
_LAYOUT_DEFAULTS = {"FABS": "bol_noLayout", "LAYOUT": '""',
                    "QUERY": "bol_noLayout"}
# Every optional name and its stand-in: the route's parameters too.
_OPTIONAL_DEFAULTS = dict(_LAYOUT_DEFAULTS, HASH="bol_noLayout")
# Which tab the game draws: the second one when the flag says so. With the
# page on, always the first, which draws the page.
_TAB_V2 = re.compile(
    r'(\("mc-servers-tab-v2"\);return ' + _N + r"\.createElement\(" + _N
    + r",\{data:(" + _N + r")\},\(?" + _N + r"=>)(" + _N + r")\?")


def _layout_names(data):
    names = dict(_LAYOUT_DEFAULTS)
    m = _LAYOUT_HOOK.search(data)
    if not m:
        return names
    # The query hook the layout reads its blocks and their images with.
    query = re.search(r"(" + _N + r')\("vanilla\.menus\.fabQuery",', data)
    layout = m.group(3)
    if not layout:
        # `a=read("mc-servers-tab-layout-id",Default)||Default,{...}=hook(a)`
        d = re.findall(r'"mc-servers-tab-layout-id"\),(' + _N + r")\)",
                       data[max(0, m.start() - 400):m.start()])
        layout = d[-1] if d else None
    if query and layout:
        names.update(FABS=m.group(1), LAYOUT=layout, QUERY=query.group(1))
    return names


_PLACEHOLDERS = (
    "R", "FOCUS", "SIDE", "DETAILS", "UFM", "UFC", "JU", "KD", "EXT", "TP",
    "DET", "JOINER", "NOTIF",
    "USF", "UNWRAP", "BUTTON", "ICONS", "ROUTER", "PANEL", "IMAGE", "SCROLL",
    "TEXTFIELD", "UFS", "LEGEND", "KEYS",
)


def _render(template, names):
    # Comments go: the bundle is minified, and nobody reads it there.
    code = re.sub(r"(?m)^\s*//.*\n", "", template)
    # The layout and route names are optional: their stand-ins draw our own
    # row and join a server at once.
    return re.sub(r"__([A-Z]+)__",
                  lambda m: names.get(m.group(1))
                  or _OPTIONAL_DEFAULTS[m.group(1)], code)


def patch_body(data):
    """Add the page to a bundle body; ``(text, status)``.

    The status is "applied", "already", "missing" (no Servers tab in this
    bundle) or "lost" (this build's tab no longer matches; left untouched).
    The vanilla tab draws the page instead of its own two panes while the
    global says the browser is on.
    """
    if MARK in data:
        return data, "already"
    if not _TAB_ANCHOR.search(data):
        return data, "missing"
    names = _discover(data)
    if not names:
        return data, "lost"
    tab_start, tab = names["_tab_start"], names["_tab"]
    anchors = list(_TAB_ANCHOR.finditer(tab))
    if len(anchors) != 1:
        return data, "lost"
    at = anchors[0].start()
    new_tab = (tab[:at] + "bol_on()?" + names["R"]
               + ".createElement(bol_Hub,{routePrefix:" + names["_prefix"]
               + "}):" + tab[at:])
    try:
        code = _render(_template(), names)
    except (OSError, KeyError):
        return data, "lost"
    out = data[:tab_start] + code + new_tab + data[tab_start + len(tab):]
    v2 = list(_TAB_V2.finditer(out))
    if len(v2) == 1:
        m = v2[0]
        out = (out[:m.start(3)] + m.group(3) + "&&!bol_on()" + out[m.end(3):])
    return out, "applied"
