"""bol.scrub — the one pass everything that leaves this machine goes through.

A problem report is read by strangers: it becomes a public issue. So nothing
in it may say who wrote it. This module is that guarantee, and the only place
it is made: bol.report hands every byte it collects to a Scrubber, hands the
finished text to it once more on the way out, and the relay that files the
issue (relay/report_relay.py) runs the same file again on what it receives,
in case an older or altered launcher sent it.

Two kinds of thing are removed:

* what this machine knows to be personal, and is told about by the caller:
  the login name, the home directory, the host name, the gamertag, the names
  of the player's worlds, servers and profiles. Each is replaced wherever it
  appears, whatever surrounds it.

* what looks personal whoever sent it: home paths, e-mail addresses, IP and
  MAC addresses, tokens and anything shaped like one, long opaque numbers,
  identifiers, URL query strings.

A line that names something this pass cannot delimit -- "gamertag", "serial
number", "level name" -- is dropped whole rather than guessed at. Losing a
line of a log costs one question in the issue; leaking one cannot be undone.

It imports nothing from the launcher, on purpose: the relay copies this file
as it is.
"""
# SPDX-License-Identifier: MIT

import re
from collections import Counter
from urllib.parse import quote

DROPPED = "[line removed: could contain personal data]"
_DROPPED_MANY = "[{} lines removed: could contain personal data]"
MAX_LINE = 2000

# Names that identify nobody: every Steam Deck is "deck", every Proton prefix
# "steamuser". Replacing them would only shred words the logs are full of.
GENERIC_NAMES = frozenset((
    "user", "root", "admin", "administrator", "deck", "steamuser", "ubuntu",
    "debian", "fedora", "arch", "linux", "home", "test", "guest", "player",
    "gamer", "minecraft", "bedrock", "default", "localhost", "desktop",
    "laptop", "computer", "server", "world", "my world", "wine", "proton",
    "steam", "public", "shared", "nobody", "system", "none", "null", "main",
))

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-_]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# ---------------------------------------------------------------- secrets
_SENSITIVE_KEY = (
    r"[A-Za-z0-9_.-]*(?:token|secret|passw(?:or)?d|passphrase|credential"
    r"|ticket|uhs|session[_-]?id|device[_-]?id|client[_-]?id|machine[_-]?id"
    r"|user[_-]?id|account[_-]?id|install(?:ation)?[_-]?id|hw[_-]?id|xuid"
    r"|puid|api[_-]?key|access[_-]?key|private[_-]?key|secret[_-]?key|nonce"
    r"|challenge|proof|assertion|ppft|flowtoken"
    r"|(?:device|user|auth[a-z]*|pairing)[_-]?code)[A-Za-z0-9_.-]*")
_KEY_VALUE = re.compile(
    r"(?P<key>[\"']?(?<![A-Za-z0-9_.-])" + _SENSITIVE_KEY + r"[\"']?)"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s,;&\"']+)", re.I)
# Headers whose whole value is the secret, spaces and semicolons included.
_HEADER = re.compile(
    r"(?P<key>(?<![A-Za-z0-9])(?:proxy-)?(?:authorization|cookie|set-cookie"
    r"|x-[a-z-]*(?:token|auth|signature)[a-z-]*)[\"']?)"
    r"(?P<sep>\s*[:=]\s*).+", re.I)
_TOKEN_SHAPES = (
    re.compile(r"eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}(?:\.[A-Za-z0-9_-]*)?"),
    re.compile(r"XBL3\.0\s+x=\S+", re.I),
    re.compile(r"\b(?:Bearer|Basic|MSAuth1\.0|WLID1\.0)\s+[^\s\"']{8,}", re.I),
    re.compile(r"\bM\.[A-Z0-9]{1,6}_[A-Z0-9]{2,6}\.[^\s\"']{8,}"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"-----BEGIN [A-Z ]+-----.*"),
)
_URL_QUERY = re.compile(r"(?P<url>\b[a-z][a-z0-9+.-]*://[^\s?#\"'<>]+)[?#](?!<query>)[^\s\"'<>]*",
                        re.I)
_URL_USERINFO = re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^\s/@\"']+@", re.I)

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")

# -------------------------------------------------------------- addresses
_MAC = re.compile(r"(?<![0-9A-Fa-f:.-])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}"
                  r"(?![0-9A-Fa-f:-])")
_IPV6 = re.compile(
    r"(?<![\w:.])(?:"
    r"(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}"
    r"|(?:[0-9A-Fa-f]{1,4}:){1,7}:(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,6})?"
    r"|::(?:[0-9A-Fa-f]{1,4}:){0,6}[0-9A-Fa-f]{1,4}"
    r")(?:%\w+)?(?![\w:])")
_IPV4 = re.compile(r"(?<![\w.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})"
                   r"(?![\w]|\.\d)")
# A four-part number right after one of these words is a version, and the
# Minecraft build ("1.26.60.5") is the first thing a report must keep.
_VERSION_BEFORE = re.compile(
    r"(?:version|versions|ver|build|minecraft|bedrock|preview|release|beta"
    r"|education|windows|driver|nvidia|mesa|vulkan|wine|proton|kernel|linux"
    r"|launcher|bedrockonlinux|game|engine|update|updating|installed)"
    r"\s*[:=/\"'(\[v-]{0,3}\s*$", re.I)

# A port or a prefix length right behind it settles it the other way.
_ADDRESS_AFTER = re.compile(r":\d{2,5}(?!\d)|/\d{1,2}(?![\d.])")

# ------------------------------------------------------------------ paths
_PATHS = (
    (re.compile(r"(?<![\w.-])(/(?:var/)?home/)[^/\s:\"'<>|]+"), r"\1<user>"),
    (re.compile(r"(?<![\w.-])(/Users/)[^/\s:\"'<>|]+"), r"\1<user>"),
    (re.compile(r"(?i)\b([a-z]:[\\/]+(?:users|home|Documents and Settings)"
                r"[\\/]+)[^\\/\s:\"'<>|]+"), r"\1<user>"),
    (re.compile(r"(?<![\w.-])(/run/user/)\d+"), r"\1<uid>"),
    # A volume label may hold spaces: take everything up to the next slash.
    (re.compile(r"(?<![\w.-])(/(?:run/)?media/)(?!<volume>)[^/\"'|]+"
                r"(?:/[^/\"'|]+)?"), r"\1<volume>"),
    (re.compile(r"(?<![\w.-])(/mnt/)(?!<volume>)[^/\"'|]+"),
     r"\1<volume>"),
    # The launcher's own profile folders are named after the profile.
    (re.compile(r"([\\/]profiles[\\/])(?!<profile>)[^\\/\s:\"'<>|]+"),
     r"\1<profile>"),
)

# A folder of the player's own, under their home: its name is theirs
# ("Documents/Dupont taxes"), and a path with spaces has no end this pass
# can find. Hidden folders are programs' data and stay; anything else goes,
# to the end of the line unless a file with a telling extension closes it.
_PRIVATE_PATH = re.compile(
    r"(?P<root>(?<![\w.~<-])~|(?<![\w.-])/(?:var/)?home/<user>"
    r"|(?<![\w.-])/Users/<user>|\b[A-Za-z]:~"
    r"|\b[A-Za-z]:[\\/]+home[\\/]+<user>)"
    r"(?P<sep>[\\/])(?![.<])(?P<rest>.+)$")
_TELLING_FILE = re.compile(
    r"(?P<dirs>(?:[^\\/]*[\\/])*?)(?P<base>[^\\/\s\"']+\.(?:dll|exe|so|mcpack|mcaddon"
    r"|mcworld|mctemplate|mcskin|rtpack|AppImage|pyz|deb|rpm))(?![\w.])"
    r"(?P<tail>.*)$", re.I)

# ------------------------------------------------------------- identifiers
_GUID = re.compile(r"\{?\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
                   r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b\}?")
# Wine's own trace lines name COM interfaces and classes by GUID, and those
# are what an engine bug report is read for. Anywhere else a GUID is a
# device, a session or an account.
_WINE_CHANNEL = re.compile(r"\b(?:fixme|err|warn|trace|info):[a-z0-9_-]+:")
_GUID_OWNER = re.compile(
    r"device|session|user|token|xuid|account|machine|serial|uuid|hwid|puid"
    r"|install|client|player|sandbox|titleid|deployment", re.I)
_HEX_RUN = re.compile(r"(?<![0-9A-Za-z])(?:0x)?[0-9A-Fa-f]{32,}(?![0-9A-Za-z])")
_BLOB = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{40,}={0,2}"
                   r"(?![A-Za-z0-9+/_=-])")
# An Xbox user ID in its hexadecimal form, and one announced by its name.
_XUID = re.compile(r"\b0009[0-9A-Fa-f]{12}\b"
                   r"|(?<=\bxuid)\W{0,3}(?:0x)?[0-9A-Fa-f]{8,}\b"
                   r"|(?<=\bpuid)\W{0,3}(?:0x)?[0-9A-Fa-f]{8,}\b", re.I)
_LONG_NUMBER = re.compile(r"(?<![\w.])\d{15,}(?!\w)")

# ----------------------------------------------- lines that cannot be kept
# What follows these words is a name or a number of the player's, in a shape
# nothing here can delimit. Drop the line.
_DROP = re.compile(
    r"gamer[ _-]?tag|\bxuid\b|\bpuid\b|display[ _-]?name|real[ _-]?name"
    r"|first[ _-]?name|last[ _-]?name|full[ _-]?name|user[ _-]?name"
    r"|level[ _-]?name|world[ _-]?name|server[ _-]?name|realm[ _-]?name"
    r"|profile[ _-]?name|skin[ _-]?name|\bmotd\b|host[ _-]?name"
    r"|computer[ _-]?name|machine[ _-]?name|\bserial\b|serial[ _-]?num"
    r"|product[ _-]?uuid|machine-id|passw(?:or)?d|passphrase|birth"
    r"|e-?mail|\bphone\b|\biban\b|credit[ _-]?card", re.I)

_DROP_VALUE = re.compile(
    r"(?P<key>(?:" + _DROP.pattern + r")[\w ]{0,12}?\s*[:=]\s*)"
    r"(?!<[a-z-]+>)[^\s,;]+", re.I)

_PLACEHOLDER = re.compile(r"<[a-z][a-z0-9-]*>")

# -------------------------------------------- custom environment variables
ENV_ALLOWED_PREFIXES = (
    "PROTON_", "WINE", "DXVK_", "VKD3D_", "MESA_", "RADV_", "NVK_", "ANV_",
    "INTEL_", "AMD_", "ZINK_", "GALLIUM_", "LIBGL_", "__GL_", "__NV_",
    "__GLX_", "__VK_", "VK_", "DRI_", "SDL_", "BOL_", "UMU_", "STAGING_",
    "MANGOHUD", "GAMEMODE", "OBS_VKCAPTURE", "ENABLE_", "DISABLE_",
    "PRESSURE_VESSEL_", "STEAM_COMPAT_", "QT_", "GDK_", "LC_",
)
ENV_ALLOWED_NAMES = frozenset((
    "LANG", "LANGUAGE", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP",
    "LD_PRELOAD", "LD_LIBRARY_PATH", "DISPLAY", "WAYLAND_DISPLAY", "TZ",
))
_ENV_SECRET = re.compile(r"TOKEN|SECRET|PASS|AUTH|COOKIE|KEY|CRED|USER|LOGIN"
                         r"|MAIL|PROXY", re.I)


def env_allowed(name):
    """Whether a custom environment variable may be reported at all."""
    name = str(name)
    if _ENV_SECRET.search(name):
        return False
    return name in ENV_ALLOWED_NAMES or name.startswith(ENV_ALLOWED_PREFIXES)


def filter_env(pairs):
    """Split (name, value) pairs into the reportable ones and a count of the
    rest. The rest is not named: a variable's name can be the secret."""
    kept, hidden = [], 0
    for name, value in pairs:
        if env_allowed(name):
            kept.append((name, value))
        else:
            hidden += 1
    return kept, hidden


def _is_private_ipv4(octets):
    a, b = octets[0], octets[1]
    return (a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168)
            or (a == 169 and b == 254) or (a == 100 and 64 <= b <= 127))


class Scrubber:
    """Removes personal data from text. See the module docstring.

    `known` is an iterable of (value, placeholder): what the caller knows to
    be personal on this machine. The relay passes none and relies on shapes
    alone.
    """

    def __init__(self, known=()):
        self.stats = Counter()
        self._known = []
        seen = set()
        for value, placeholder in known:
            value = str(value or "").strip()
            if len(value) < 3 or value.lower() in GENERIC_NAMES:
                continue
            for variant in {value, value.replace(" ", "_"),
                            value.replace(" ", "%20"), quote(value),
                            value.replace("/", "\\")}:
                if variant.lower() in seen or len(variant) < 3:
                    continue
                seen.add(variant.lower())
                self._known.append((variant, placeholder))
        # Longest first, so a host name "alice-pc" is not left as "<user>-pc".
        self._known.sort(key=lambda item: -len(item[0]))
        # One alternation rather than one pattern per value: a player with
        # hundreds of worlds must not make every log line cost hundreds of
        # searches.
        self._placeholders = {value.lower(): placeholder
                              for value, placeholder in self._known}
        self._known_pattern = re.compile(
            r"(?<![A-Za-z0-9])(?:" + "|".join(
                re.escape(value) for value, _ in self._known)
            + r")(?![A-Za-z0-9])", re.I) if self._known else None

    # ------------------------------------------------------------ public
    def text(self, text, prose=False):
        """`text` with everything personal removed.

        `prose` is for what the player typed themselves, the title and the
        comment: the same replacements, but no line is dropped for a word it
        contains -- "my world name is gone" has to survive.
        """
        if not text:
            return ""
        out, dropped = [], 0
        for line in str(text).splitlines():
            clean = self._line(line, prose)
            if clean is None:
                dropped += 1
                continue
            if dropped:
                out.append(self._drop_marker(dropped))
                dropped = 0
            out.append(clean)
        if dropped:
            out.append(self._drop_marker(dropped))
        return "\n".join(out)

    def residue(self, text):
        """Known personal values still present in `text` (should be none)."""
        if self._known_pattern is None:
            return []
        return sorted({match[0] for match in
                       self._known_pattern.finditer(text)})

    # ----------------------------------------------------------- private
    def _drop_marker(self, count):
        self.stats["lines removed"] += count
        return DROPPED if count == 1 else _DROPPED_MANY.format(count)

    def _count(self, kind, pattern, repl, line):
        line, count = pattern.subn(repl, line)
        if count:
            self.stats[kind] += count
        return line

    def _line(self, line, prose):
        line = _CONTROL.sub("", _ANSI.sub("", line))
        # A marker this pass wrote earlier (a second run, here or on the
        # relay) is not a line to judge again.
        if line == DROPPED or (line.startswith("[") and line.endswith(
                " lines removed: could contain personal data]")):
            return line
        if len(line) > MAX_LINE:
            line = line[:MAX_LINE] + " [line cut]"
        # Whole addresses first, or a known login would leave half of one.
        line = self._count("e-mail addresses", _EMAIL, "<email>", line)
        if self._known_pattern is not None:
            line = self._count(
                "personal names or paths", self._known_pattern,
                lambda m: self._placeholders.get(m[0].lower(), "<redacted>"),
                line)
        # Before the key=value pass, which would otherwise leave a query
        # string half rewritten.
        line = self._count("urls", _URL_USERINFO, r"\1", line)
        line = self._count("urls", _URL_QUERY, r"\g<url>?<query>", line)
        line = self._count("tokens", _HEADER,
                           lambda m: m["key"] + m["sep"] + "<redacted>", line)
        line = self._count("tokens", _KEY_VALUE, self._key_value, line)
        for shape in _TOKEN_SHAPES:
            line = self._count("tokens", shape, "<redacted>", line)
        line = self._count("MAC addresses", _MAC, "<mac>", line)
        line = self._count("IP addresses", _IPV6, self._ipv6, line)
        line = self._ipv4(line)
        for pattern, repl in _PATHS:
            line = self._count("paths", pattern, repl, line)
        line = self._private_paths(line)
        line = self._guids(line)
        line = self._count("identifiers", _XUID, " <xuid>", line)
        line = self._count("identifiers", _HEX_RUN, "<hex>", line)
        line = self._count("identifiers", _BLOB, self._blob, line)
        line = self._count("identifiers", _LONG_NUMBER, "<number>", line)
        if prose:
            # The player's own sentence stays, but a pasted "serial: X" does
            # not keep its X.
            line = self._count("identifiers", _DROP_VALUE,
                               r"\g<key><redacted>", line)
        elif _DROP.search(_PLACEHOLDER.sub("", line)):
            return None
        # Belt and braces: nothing known may survive what came before.
        if self._known_pattern is not None \
                and self._known_pattern.search(line):
            return None
        return line

    @staticmethod
    def _key_value(match):
        value = match["value"]
        if _PLACEHOLDER.fullmatch(value.strip("\"'")):
            return match[0]
        quote_char = value[0] if value[:1] in "\"'" else ""
        return (match["key"] + match["sep"] + quote_char + "<redacted>"
                + quote_char)

    @staticmethod
    def _ipv6(match):
        text = match[0]
        # "::" alone, a C++ scope or a clock ("12:34:56") is not an address.
        if text.count(":") < 2 or text in ("::", "::1"):
            return text
        if "::" not in text and text.count(":") != 7:
            return text
        return "<ip>"

    def _ipv4(self, line):
        def repl(match):
            octets = [int(part) for part in match.groups()]
            if any(octet > 255 for octet in octets):
                return match[0]
            if match[0] in ("127.0.0.1", "0.0.0.0"):
                return match[0]
            if not _ADDRESS_AFTER.match(line, match.end()) \
                    and _VERSION_BEFORE.search(
                    line[max(0, match.start() - 40):match.start()]):
                return match[0]
            self.stats["IP addresses"] += 1
            return "<lan-ip>" if _is_private_ipv4(octets) else "<ip>"
        return _IPV4.sub(repl, line)

    def _private_paths(self, line):
        match = _PRIVATE_PATH.search(line)
        if not match:
            return line
        self.stats["paths"] += 1
        named = _TELLING_FILE.match(match["rest"])
        kept = match["root"] + match["sep"]
        if not named:
            return line[:match.start()] + kept + "<path>"
        if named["dirs"]:
            kept += "<path>" + match["sep"]
        return (line[:match.start()] + kept + named["base"]
                + self._private_paths(named["tail"]))

    def _guids(self, line):
        if not _GUID.search(line):
            return line
        if _WINE_CHANNEL.search(line) and not _GUID_OWNER.search(line):
            return line
        return self._count("identifiers", _GUID, "<guid>", line)

    @staticmethod
    def _blob(match):
        text = match[0]
        if not (re.search(r"\d", text) and re.search(r"[a-z]", text)
                and re.search(r"[A-Z]", text)):
            return text                 # a long identifier or a plain path
        if text.count("/") * 16 > len(text):
            return text                 # a path: far more slashes than base64
        return "<blob>"


def scrub(text, prose=False):
    """`text` scrubbed by shape alone, with nothing known about its author."""
    return Scrubber().text(text, prose=prose)
