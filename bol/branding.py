"""BedrockOnLinux's own touches on the game's menus.

Two of them, both applied to the installed game before every launch and both
best-effort -- neither may ever keep the game from starting:

* the yellow splash line under the title-screen logo reads from our own list
  of Linux jokes instead of Mojang's (``splashes.json``, a loose file);
* the partner servers this launcher ships with (see ``prefix.DEFAULT_SERVERS``)
  are shown in the Servers tab's "Featured Servers" section, with their icon,
  banner and description, instead of among the player's own servers.

Featured servers come from a Microsoft service the game queries at runtime,
so there is no list on disk to add one to. The partner is therefore still the
external server ``seed_default_servers`` writes -- which is what joining it
goes through -- and the HBUI bundle that draws the Servers tab is patched to
present that entry as a featured one.
"""

import json
import pkgutil
import re
import shutil
from pathlib import Path

from .log import ok, warn

# --------------------------------------------------------------------------
# Title-screen splashes

SPLASHES_REL = "data/resource_packs/vanilla/splashes.json"

# Shown one at a time, at random, in yellow under the logo. Short: the game
# shrinks a long line rather than wrapping it.
LINUX_SPLASHES = (
    "Now on Linux!",
    "Bedrock on Linux!",
    "Tux approved!",
    "I use Arch, btw!",
    "sudo make me a sandwich!",
    "Works on my distro!",
    "Year of the Linux desktop!",
    "Wine not?",
    "Not an emulator!",
    "No Windows were harmed!",
    "rm -rf /creepers",
    "chmod +x fun.sh",
    "apt install diamonds",
    "pacman -S cobblestone",
    "Penguins > creepers!",
    "GNU/Minecraft!",
    "Free as in freedom!",
    "Compiled from source!",
    "Now with 100% more penguins!",
    "Runs on a potato with Debian!",
    "Wayland ready!",
    "Vulkan-powered!",
    "Ctrl+Alt+Mine!",
    "Mining in userspace!",
    "It's a kernel feature!",
    "Sent to /dev/null: lag",
    "Flatpak, AppImage or .deb?",
    "Have you tried rebooting?",
    "Open source vibes!",
    "Tux says hi!",
    "There's no place like ~",
    "Distro-hopping to the Nether!",
    "Root access to the End!",
    "Linesia on Linux!",
)

# Marks a splashes.json this launcher wrote, so a game update that ships a
# new one is noticed and the original it brings is kept as the backup.
_SPLASHES_MARK = "Bedrock on Linux!"


def apply_linux_splashes(game_dir, splashes=LINUX_SPLASHES):
    """Make the title screen's yellow splash line one of ours.

    The vanilla file is kept beside it as ``splashes.json.bol-orig``, and its
    ``conditional`` sets (the trial-mode lines) are carried over untouched.
    Returns True when the file now holds our list.
    """
    path = Path(game_dir) / SPLASHES_REL
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or not isinstance(data.get("splashes"),
                                                    list):
        return False
    if data["splashes"] == list(splashes):
        return True
    try:
        if _SPLASHES_MARK not in data["splashes"]:
            shutil.copy2(path, path.with_name(path.name + ".bol-orig"))
        data["splashes"] = list(splashes)
        tmp = path.with_name(path.name + ".bol-tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        warn(f"Could not write the title-screen splashes: {e}")
        return False
    ok("Title-screen splashes set to the Linux ones")
    return True


# --------------------------------------------------------------------------
# Partner servers in "Featured Servers"

HBUI_REL = "data/gui/dist/hbui"

# One entry per partner. ``host`` is what identifies the server in its
# details (they carry the address); ``key`` what identifies it in the list,
# which carries only the name the player sees -- the one
# ``prefix.DEFAULT_SERVERS`` gave it, unless they renamed it.
PARTNERS = (
    {
        "key": "linesia",
        "host": "play.linesia.net",
        "port": 19132,
        "label": "Linesia SkyFaction",
        "message": "SkyFaction · Kitmap · PvP Faction · FR",
        "description": (
            "Linesia is a French Minecraft Bedrock server: SkyFaction, "
            "Kitmap and PvP Faction, with islands, jobs, prestiges and "
            "events. Playable on PC, mobile and console.\n\n"
            "BedrockOnLinux partner server."),
        "news_title": "play.linesia.net · port 19132",
        "news": "Featured by BedrockOnLinux. Have fun, from Tux!",
        "icon": "linesia-icon.png",
        "banner": "linesia-banner.jpg",
    },
)

_ASSET_PREFIX = "bol-partner-"

# Everything the patch adds sits behind this name, which is also how an
# already-patched bundle is recognised.
_PARTNER_MARK = "__bolPartnerList"

# A useFacetMap call as each bundler writes it: `(0,r.useFacetMap)(` up to
# 1.26.52, a bare imported name from 1.26.60 on -- in which case it also drops
# the parentheses around a lone arrow function (see fixups._HBUI_FACET_MAP).
_FM = r'(?:\(0,[\w$]+\.useFacetMap\)|[\w$]+)\(\(?'

# The Servers-tab side menu: the one component listing both featured and
# external servers. Every list-side edit stays inside it.
_SIDE_MENU = re.compile(
    r'function [\w$]+\(\{loading:\w+,routePrefix:\w+="/play",'
    r'showFeaturedServers:')
# The featured list, rebuilt as partners + Microsoft's featured servers. The
# selector gains the external-server facet as a second input.
_FEATURED_LIST = re.compile(
    r'(?P<fm>' + _FM + r')e=>(?P<body>Array\.from\(e\.featuredExperiences\)'
    r'\.map\(\(?e=>\(\{message:e\.msgOfTheDay,image:e\.image,label:e\.name,'
    r'type:(?P<kd>[\w$]+)\.ThirdParty,value:e\.id\}\)\)?\))'
    r'(?P<close>\)?,\[\],\[)(?P<feat>[\w$]+)\]')
# Its count (the section title) and its visibility: both read the length.
_FEATURED_LENGTH = re.compile(
    r'(?P<fm>' + _FM + r')e=>(?P<body>(?:(?!=>)[^;])*?'
    r'e\.featuredExperiences\.length(?:(?!=>)[^;])*?)'
    r'(?P<close>\)?,\[[^\]]*\],\[)(?P<feat>[\w$]+)\]')
# The external-server facet, read off the selector that says whether the
# "External servers" section shows at all.
_EXTERNAL_FACET = re.compile(
    r'e=>e\.externalServerWorlds\.length>0\)?,\[\],\[(?P<ext>[\w$]+)\]')
# A list item hands its own type to the router; a partner goes as the
# external server it really is.
_SELECT_SERVER = "(e.value,e.type,e.label)"
_SELECT_SERVER_PATCHED = "(e.value,null!=e.bolType?e.bolType:e.type,e.label)"
# The details pane picks its layout on the server's type: a partner gets the
# featured-server one (banner, description, news) rather than the address
# form.
_DETAILS_LAYOUT = re.compile(
    r'(?P<head>' + _FM + r'e=>)(?P<body>e\.networkDetails\.type===[\w$]+'
    r'\.External)(?P<close>\)?,\[\],\[)')
# The fields that layout shows which an external server does not have.
_DETAILS_FIELDS = {
    "imagePath": "__bolDetail(e.networkDetails,\"banner\",\"imagePath\")",
    "description": "__bolDetail(e.networkDetails,\"description\","
                   "\"description\")",
    "newsTitle": "__bolDetail(e.networkDetails,\"news_title\",\"newsTitle\")",
    "newsDescription": "__bolDetail(e.networkDetails,\"news\","
                       "\"newsDescription\")",
}


def _partner_runtime(partners):
    """The helpers the patched bundle calls, as one line of JavaScript."""
    table = [{
        "key": p["key"].lower(),
        "host": p["host"].lower(),
        "label": p["label"],
        "message": p["message"],
        "description": p["description"],
        "news_title": p["news_title"],
        "news": p["news"],
        "icon": "assets/" + _ASSET_PREFIX + p["icon"],
        "banner": "assets/" + _ASSET_PREFIX + p["banner"],
    } for p in partners]
    return (
        "var " + _PARTNER_MARK + "=" + json.dumps(table, ensure_ascii=True)
        + ";"
        # Which partner a server is: by address when the object has one (the
        # details do), by name otherwise (list entries carry none).
        "function __bolPartnerOf(s){if(!s)return null;"
        "var a=String(s.address||\"\").toLowerCase(),"
        "n=String(s.name||\"\").toLowerCase();"
        "for(var i=0;i<" + _PARTNER_MARK + ".length;i++){"
        "var p=" + _PARTNER_MARK + "[i];"
        "if(a?a===p.host:n.indexOf(p.key)>=0)return p}return null}"
        # The external entries promoted to partners: the first per partner.
        "function __bolPromoted(x){var w=x&&x.externalServerWorlds,"
        "out=[],seen=[];if(!w)return out;w=Array.from(w);"
        "for(var i=0;i<w.length;i++){var p=__bolPartnerOf({name:w[i].name});"
        "if(p&&seen.indexOf(p)<0){seen.push(p);out.push({p:p,s:w[i]})}}"
        "return out}"
        "function __bolFeatured(x,kd){return __bolPromoted(x).map("
        "function(e){return{message:e.p.message,image:e.p.icon,"
        "label:e.p.label,type:kd.ThirdParty,bolType:kd.External,"
        "value:e.s.id}})}"
        "function __bolCount(x){return __bolPromoted(x).length}"
        "function __bolRest(w){var ids=__bolPromoted({externalServerWorlds:w})"
        ".map(function(e){return e.s.id});"
        "return Array.from(w).filter(function(s){"
        "return ids.indexOf(s.id)<0})}"
        "function __bolDetail(d,k,f){var p=__bolPartnerOf(d);"
        "return p?p[k]:d[f]}\n")


def _patch_side_menu(data):
    """Rewrite the Servers-tab side menu; ``(text, ok)``."""
    m = _SIDE_MENU.search(data)
    if not m:
        return data, False
    end = data.find("}function ", m.end())
    alt = data.find("}const ", m.end())
    end = min(i for i in (end, alt) if i != -1) if max(end, alt) != -1 else -1
    if end == -1:
        return data, False
    menu = data[m.start():end]
    ext = _EXTERNAL_FACET.search(menu)
    featured = _FEATURED_LIST.search(menu)
    if not ext or not featured:
        return data, False
    ext, feat = ext.group("ext"), featured.group("feat")
    kd = featured.group("kd")

    # The player's own list no longer shows the promoted entry.
    menu = menu.replace("e.externalServerWorlds",
                        "__bolRest(e.externalServerWorlds)")
    menu = _FEATURED_LIST.sub(
        lambda f: (f.group("fm") + "(e,__bolX)=>__bolFeatured(__bolX,"
                   + kd + ").concat(" + f.group("body") + ")"
                   + f.group("close") + feat + "," + ext + "]"),
        menu, count=1)

    def widen(f):
        if f.group("feat") != feat:
            return f.group(0)
        body = f.group("body").replace(
            "e.featuredExperiences.length",
            "(e.featuredExperiences.length+__bolCount(__bolX))")
        return (f.group("fm") + "(e,__bolX)=>" + body + f.group("close")
                + feat + "," + ext + "]")

    menu, widened = _FEATURED_LENGTH.subn(widen, menu)
    if widened < 2 or menu.count("__bolCount(") < 2:
        return data, False
    return data[:m.start()] + menu + data[end:], True


def _partner_patch(data, partners=PARTNERS):
    """Present the partner servers as featured ones in one HBUI bundle.

    Returns ``(text, status)``: "applied", "already", "missing" (not the
    bundle holding the Servers tab) or "lost" (it is, but some anchor no
    longer matches, so nothing was changed). All or nothing: half a patch
    could list a server whose details then fail to load.
    """
    if data.startswith("var " + _PARTNER_MARK + "="):
        # Patched by this or an earlier launcher: the call sites stay, the
        # partner table and helpers on the first line are brought up to date.
        return (_partner_runtime(partners) + data[data.index("\n") + 1:],
                "already")
    if _PARTNER_MARK in data:
        return data, "already"
    if not _SIDE_MENU.search(data):
        return data, "missing"
    new, side = _patch_side_menu(data)
    if not side or new.count(_SELECT_SERVER) != 1:
        return data, "lost"
    new = new.replace(_SELECT_SERVER, _SELECT_SERVER_PATCHED)
    new, layouts = _DETAILS_LAYOUT.subn(
        lambda f: (f.group("head") + "(" + f.group("body")
                   + "&&!__bolPartnerOf(e.networkDetails))"
                   + f.group("close")), new)
    if layouts != 1:
        return data, "lost"
    for field, expr in _DETAILS_FIELDS.items():
        new, n = re.subn(r'(?<=e=>)e\.networkDetails\.' + field
                         + r'(?=\)?,\[\],\[)', lambda _m, x=expr: x, new)
        if n != 1:
            return data, "lost"
    return _partner_runtime(partners) + new, "applied"


def _install_partner_assets(hbui, partners):
    assets = hbui / "assets"
    for p in partners:
        for name in (p["icon"], p["banner"]):
            blob = pkgutil.get_data("bol", "partners/" + name)
            if not blob:
                raise OSError(f"bundled partners/{name} is missing")
            dst = assets / (_ASSET_PREFIX + name)
            try:
                if dst.read_bytes() == blob:
                    continue
            except OSError:
                pass
            dst.write_bytes(blob)


def feature_partner_servers(game_dir, partners=PARTNERS):
    """Show the partner servers under "Featured Servers". Never fatal."""
    hbui = Path(game_dir) / HBUI_REL
    bundles = sorted(p for p in hbui.glob("index-*.js"))
    if not bundles:
        return False
    try:
        _install_partner_assets(hbui, partners)
    except OSError as e:
        warn(f"Could not install the partner-server images: {e}")
        return False
    status = "missing"
    for js in bundles:
        try:
            data = js.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        new, status = _partner_patch(data, partners)
        if status in ("applied", "already") and new != data:
            try:
                bak = js.with_name(js.name + ".bol-orig")
                if not bak.exists():
                    shutil.copy2(js, bak)
                tmp = js.with_name(js.name + ".bol-tmp")
                tmp.write_text(new, encoding="utf-8")
                tmp.replace(js)
            except OSError as e:
                warn(f"Could not patch the Servers tab: {e}")
                return False
            if status == "applied":
                ok("Partner servers added to Featured Servers")
            return True
        if status in ("already", "lost"):
            break
    if status == "lost":
        warn("This Minecraft build rebundled its Servers tab, so the partner "
             "servers stay listed under your own servers instead of Featured "
             "Servers.")
    return status == "already"


def apply_branding(game_dir):
    """Everything above, for one installed game. Never raises."""
    for step in (apply_linux_splashes, feature_partner_servers):
        try:
            step(game_dir)
        except Exception as e:  # cosmetic: never in the way of PLAY
            warn(f"{step.__name__}: {e}")
