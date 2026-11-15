"""BedrockOnLinux's own Servers tab: favorites, partners, every server.

The game's Servers tab lists the player's own servers next to Microsoft's
featured ones. With the ``server_browser`` setting on (the default) the side
menu of that tab is replaced by three sections the player switches between,
by mouse or controller alike:

* **Favorites** -- the player's own server list, the one the game keeps in
  ``external_servers.txt``, with the game's own "Add server" form for any
  address that is not in the community list;
* **Partners** -- BedrockOnLinux's partner servers, then Microsoft's featured
  and creator servers;
* **All** -- the community list github.com/Wyze3306/bedrock-servers-list,
  searchable by name, address, category or any word of a description, and
  filtered by category and language.

A server of the community list gets its own details pane (banner, address,
categories, description) with "Play", which adds it to the player's list and
joins through the game's online-play warning exactly as the game's "Add and
play" does, and "Add to favorites".

How it gets there: bol/hbui/serverbrowser.js holds the React components,
written against placeholders. ``_discover`` reads, from anchors the Servers
tab cannot drop without being rewritten, the minified name each build gives
the components and hooks they use, and the components are pasted into the
bundle's own scope next to the vanilla tab. The vanilla tab then draws them
instead of its own side menu and details pane, but only while the
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
MARK = "bol_SideView"
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
        "tabFavorites": "Favorites ({0})",
        "tabPartners": "Partners ({0})",
        "tabAll": "All ({0})",
        "search": "Search",
        "searchPlaceholder": "Name, address, category or keyword",
        "addServer": "Add a server",
        "yourServers": "Your servers ({0})",
        "noFavorites": "No favorites yet. Add a server by its address, or "
                       "open one under All and choose Add to favorites.",
        "noMatch": "No server matches this search.",
        "bolPartners": "BedrockOnLinux partners",
        "featured": "Featured servers ({0})",
        "creators": "Creator servers ({0})",
        "noPartners": "The featured servers have not loaded yet.",
        "allCategories": "All categories",
        "allLanguages": "All languages",
        "category": "Category",
        "language": "Language",
        "count": "{0} servers",
        "back": "Back",
        "servers": "Servers ({0})",
        "showMore": "Show more ({0} of {1})",
        "listUnavailable": "The server list could not be downloaded. "
                           "It is fetched again at the next launch.",
        "narrationContext": "Servers",
        "play": "Play",
        "favorite": "Add to favorites",
        "unfavorite": "Remove from favorites",
        "added": "Added to your favorites.",
        "alreadyAdded": "This address is already in your servers under "
                        "another name.",
        "addFailed": "This server could not be added.",
        "removeFailed": "This server could not be removed.",
        "version": "Version {0}",
        "about": "About",
        "noDescription": "This server has no description.",
        "links": "Links",
        "source": "From the community list "
                  "github.com/Wyze3306/bedrock-servers-list",
    },
    "fr": {
        "tabFavorites": "Favoris ({0})",
        "tabPartners": "Partenaires ({0})",
        "tabAll": "Tous ({0})",
        "search": "Rechercher",
        "searchPlaceholder": "Nom, adresse, catégorie ou mot-clé",
        "addServer": "Ajouter un serveur",
        "yourServers": "Vos serveurs ({0})",
        "noFavorites": "Aucun favori pour l'instant. Ajoutez un serveur par "
                       "son adresse, ou ouvrez-en un dans Tous et choisissez "
                       "Ajouter aux favoris.",
        "noMatch": "Aucun serveur ne correspond à cette recherche.",
        "bolPartners": "Partenaires BedrockOnLinux",
        "featured": "Serveurs en vedette ({0})",
        "creators": "Serveurs de créateurs ({0})",
        "noPartners": "Les serveurs en vedette ne sont pas encore chargés.",
        "allCategories": "Toutes les catégories",
        "allLanguages": "Toutes les langues",
        "category": "Catégorie",
        "language": "Langue",
        "count": "{0} serveurs",
        "back": "Retour",
        "servers": "Serveurs ({0})",
        "showMore": "Afficher plus ({0} sur {1})",
        "listUnavailable": "La liste des serveurs n'a pas pu être "
                           "téléchargée. Nouvel essai au prochain lancement.",
        "narrationContext": "Serveurs",
        "play": "Jouer",
        "favorite": "Ajouter aux favoris",
        "unfavorite": "Retirer des favoris",
        "added": "Ajouté à vos favoris.",
        "alreadyAdded": "Cette adresse est déjà dans vos serveurs sous un "
                        "autre nom.",
        "addFailed": "Ce serveur n'a pas pu être ajouté.",
        "removeFailed": "Ce serveur n'a pas pu être retiré.",
        "version": "Version {0}",
        "about": "Présentation",
        "noDescription": "Ce serveur n'a pas de description.",
        "links": "Liens",
        "source": "Issu de la liste communautaire "
                  "github.com/Wyze3306/bedrock-servers-list",
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


def ui_language(game_language):
    """The tab's language for the game's ``game_language`` (``fr_FR``)."""
    code = str(game_language or "").split("_")[0].lower()
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


def _image_url(entry):
    url = entry.get("logo") or entry.get("image") or ""
    return url if _image_allowed(url) else ""


def cached_images(cache=LIST_CACHE):
    """{slug: Path} of the banners already downloaded."""
    out = {}
    try:
        for p in (Path(cache) / "images").iterdir():
            if p.suffix in (".png", ".jpg", ".gif") and p.stat().st_size:
                out[p.stem] = p
    except OSError:
        pass
    return out


def download_images(entries, cache=LIST_CACHE, getter=_get, budget=None):
    """Fetch the banners not cached yet; returns how many were added.

    Runs off the launch path (see ``prefetch_images``): the game only shows
    the banners already on disk when PLAY patched it, so a new one appears
    from the following launch on.
    """
    folder = Path(cache) / "images"
    have = cached_images(cache)
    added = 0
    deadline = None if budget is None else time.monotonic() + budget
    for entry in entries:
        if deadline is not None and time.monotonic() > deadline:
            break
        slug, url = _slug(entry), _image_url(entry)
        if not url or slug in have:
            continue
        try:
            blob = getter(url, _MAX_IMAGE_BYTES, 10)
            ext = _image_kind(blob)
            if not ext or not _image_dims(blob):
                continue
            folder.mkdir(parents=True, exist_ok=True)
            (folder / (slug + ext)).write_bytes(blob)
            have[slug] = folder / (slug + ext)
            added += 1
        except (OSError, ValueError, urllib.error.URLError):
            continue
    return added


def prefetch_images(entries, cache=LIST_CACHE):
    """``download_images`` in a daemon thread. Never in PLAY's way."""
    t = threading.Thread(target=download_images, args=(entries, cache),
                         name="bol-server-banners", daemon=True)
    t.start()
    return t


# --------------------------------------------------------------------------
# What the injected tab reads


def _language_name(code, ui):
    names = LANGUAGE_NAMES.get(ui, LANGUAGE_NAMES["en"])
    return names.get(code) or names.get(code.split("-")[0]) or code.upper()


def build_payload(entries, *, on=True, ui="en", images=None,
                  partner_hosts=()):
    """The ``__bolServerBrowser`` object: settings, strings, servers.

    ``images`` maps a server's slug to the asset name its banner was copied
    under, with its height as a percentage of its width.
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
        if slug in images:
            s["img"], s["ih"] = images[slug]
        if s["a"].lower() in partner_hosts:
            s["pt"] = 1
        servers.append(s)
    cats = sorted(((t, n) for t, n in counts.items()
                   if t not in noise and n >= _MIN_CATEGORY),
                  key=lambda c: (-c[1], c[0].lower()))
    lang_list = sorted(langs.items(), key=lambda c: (-c[1], c[0]))
    strings = dict(STRINGS["en"])
    strings.update(STRINGS.get(ui, {}))
    return {
        "on": bool(on),
        "strings": strings,
        "servers": servers,
        "cats": [[t, n] for t, n in cats],
        "langs": [[c, n, _language_name(c, ui)] for c, n in lang_list],
    }


def runtime_line(payload):
    """One line of JavaScript defining the global; no newline inside."""
    text = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    # Inside a <script>-less bundle "</" is harmless, but U+2028/9 and a
    # stray newline are not: ensure_ascii escapes both.
    return "var " + GLOBAL + "=" + text + ";"


def install_images(hbui, entries, cache=LIST_CACHE):
    """Copy the cached banners into one game's HBUI assets.

    Returns the ``images`` mapping ``build_payload`` takes. A banner that
    cannot be read or copied is simply not shown.
    """
    have = cached_images(cache)
    assets = Path(hbui) / "assets"
    out = {}
    for e in entries:
        slug = _slug(e)
        src = have.get(slug)
        if src is None:
            continue
        try:
            blob = src.read_bytes()
            dims = _image_dims(blob)
            if not dims or not dims[0]:
                continue
            name = _ASSET_PREFIX + slug + src.suffix
            dst = assets / name
            if not dst.is_file() or dst.stat().st_size != len(blob):
                shutil.copyfile(src, dst)
        except OSError:
            continue
        out[slug] = ("assets/" + name, round(dims[1] * 100 / dims[0], 2))
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
    m = re.search(r"(" + _N + r")=(" + _N + r")\(\)," + _N + r"="
                  + _HOOK + r"\(\(?e=>null==e\?void 0:e\.id", tab_text)
    names["ROUTE"] = m.group(2) if m else None
    m = re.search(r"===(" + _N + r")\((" + _N + r")\.External\)\?", tab_text)
    names["JU"], names["KD"] = (m.group(1), m.group(2)) if m else (None, None)
    names["EXT"] = _facet_def(data, "externalServerWorldList")
    names["TP"] = _facet_def(data, "thirdPartyWorldList")
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
    names["XJ"] = _one(ce + r"(" + _N + r"),\{spacingAroundSideMenu:", side)
    m = re.search(ce + r"(" + _N + r"),\{onClick:" + _N + r",Icon:(" + _N
                  + r")\.Plus,inputLegend:", side)
    names["BUTTON"], names["ICONS"] = (m.group(1), m.group(2)) if m else (
        None, None)
    names["ITEM"] = _one(ce + r"(" + _N + r"),\{item:" + _N
                         + r",onSelectServer:", side)
    names["SPACER"] = _one(ce + r"(" + _N + r"),\{size:4\}\)", side)
    m = re.search(r"return " + ce + r"(" + _N + r"),\{role:" + _N + r"\},"
                  + ce + r"(" + _N + r"),\{text:" + _N
                  + r'\("\.context"\)\}', side)
    names["THEME"], names["NARRATION"] = (m.group(1), m.group(2)) if m else (
        None, None)
    router_var = _one(r"(" + _N + r")\.push\(`\$\{" + _N
                      + r"\}/servers/add`\)", side)
    if router_var:
        names["ROUTER"] = _one(r"[,{]" + re.escape(router_var) + r"=("
                               + _N + r")\(\)[,;]", side)

    # The details layout of a featured server, for the catalogue's own.
    names["PANEL"] = _one(ce + r"(" + _N + r"),\{focusable:!0,"
                          r'role:"neutral80",narrationText:', data)
    m = re.search(ce + r"(" + _N + r"),null," + _N
                  + r'\("\.ServerDescription\.title"\)\),' + ce + _N
                  + r",\{size:2\}\)," + ce + r"(" + _N + r"),null,", data)
    names["HEADING"], names["BODY"] = (m.group(1), m.group(2)) if m else (
        None, None)
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
    if names.get("ITEM"):
        _, item = _function_named(data, names["ITEM"])
        if item:
            names["WRAP"] = _wrap_hook(data, names["ITEM"], item)
    if not all(names.get(k) for k in _PLACEHOLDERS):
        return None
    names["_tab_start"] = tab.start()
    names["_tab"] = tab_text
    names["_tab_name"] = tab.group(1)
    return names


def _wrap_hook(data, item_name, item_text):
    """useFacetWrap: the list item's image line wraps a plain prop with it."""
    # The item renders a "rich" row component that wraps its optional props:
    # `c=Q(null!=r?r:"")`.
    inner = _one(r"\.createElement\((" + _N + r"),\{label:" + _N
                 + r",value:" + _N + r",image:", item_text)
    if not inner:
        return None
    _, row = _function_named(data, inner)
    if not row:
        return None
    return _one(r"=(" + _HOOK + r")\(null!=" + _N + r"\?" + _N + r':""\)',
                row)


_PLACEHOLDERS = (
    "R", "FOCUS", "ALIAS", "SIDE", "DETAILS", "UFM", "UFC", "ROUTE", "JU",
    "KD", "EXT", "TP", "USF", "UNWRAP", "XJ", "BUTTON", "ICONS", "ITEM",
    "SPACER", "THEME", "NARRATION", "ROUTER", "PANEL", "HEADING", "BODY",
    "IMAGE", "SCROLL", "TEXTFIELD", "UFS", "WRAP",
)

# The list item's "selected" test, which also has to know a catalogue
# server, whose id the game never loads.
_ITEM_SELECTED = re.compile(
    r"(\(?)\(e,t\)=>e===t\.networkDetails\.id(\)?,\[\],\[" + _N + "," + _N
    + r")\]")
_ROUTE_ID = "e=>null==e?void 0:e.id"


def _render(template, names):
    # Comments go: the bundle is minified, and nobody reads it there.
    code = re.sub(r"(?m)^\s*//.*\n", "", template)
    return re.sub(r"__([A-Z]+)__", lambda m: names[m.group(1)], code)


def patch_body(data):
    """Add the components to a bundle body; ``(text, status)``.

    The status is "applied", "already", "missing" (no Servers tab in this
    bundle) or "lost" (this build's tab no longer matches; left untouched).
    """
    if MARK in data:
        return data, "already"
    if not _TAB_ANCHOR.search(data):
        return data, "missing"
    names = _discover(data)
    if not names:
        return data, "lost"
    tab_start, tab = names["_tab_start"], names["_tab"]
    R = names["R"]
    new_tab = tab
    for comp, repl in ((names["SIDE"], "bol_Side"),
                       (names["DETAILS"], "bol_Details")):
        old = R + ".createElement(" + comp + ",{"
        if new_tab.count(old) != 1:
            return data, "lost"
        new_tab = new_tab.replace(
            old, R + ".createElement(bol_on()?" + repl + ":" + comp + ",{")
    if new_tab.count(_ROUTE_ID) != 1:
        return data, "lost"
    new_tab = new_tab.replace(
        _ROUTE_ID, "e=>null==e||bol_isCatRoute(e.id)?void 0:e.id")
    item_start, item = _function_named(data, names["ITEM"])
    if item_start is None or item_start > tab_start:
        return data, "lost"
    sel = _ITEM_SELECTED.findall(item)
    if len(sel) != 1:
        return data, "lost"
    new_item = _ITEM_SELECTED.sub(
        lambda m: (m.group(1) + "(e,t,bol_r)=>bol_selected(e,t,bol_r)"
                   + m.group(2) + "," + names["ROUTE"] + "()]"), item)
    try:
        code = _render(_template(), names)
    except (OSError, KeyError):
        return data, "lost"
    out = (data[:item_start] + new_item
           + data[item_start + len(item):tab_start]
           + code + new_tab + data[tab_start + len(tab):])
    return out, "applied"
