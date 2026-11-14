"""bol.netpolicy — what the launcher connects to, and the player's say in it.

Two things live here, and nothing that opens a socket:

* ENDPOINTS, the one list of every host the launcher contacts, with what is
  sent there and why. The first-run notice, the Network page of Settings, the
  ``network`` command and the README section are all this list, so they
  cannot drift apart. tests/test_netpolicy.py holds the list against the
  hosts the source actually names.

* The update choice. "auto" checks for launcher updates and data fixes in the
  background, "ask" does it only when asked, "off" never does and plays on
  what the launcher shipped with. Until a choice is recorded nothing connects
  unprompted -- see decided().

No telemetry exists to opt out of: nothing here or anywhere else reports
usage, and no request carries an identifier of the install or the machine.
"""
# SPDX-License-Identifier: MIT

import os
from collections import namedtuple

from .config import APP, PRETTY
from .report import ISSUE_REPO, RELAY_URL

SETTING = "network_updates"
CHANNEL_SETTING = "update_channel"
MODES = ("auto", "ask", "off")
CHANNELS = ("stable", "prerelease")
ENV = "BOL_NETWORK_UPDATES"

MODE_LABELS = {
    "auto": "Automatic",
    "ask": "Only when I ask",
    "off": "Off",
}
MODE_HELP = {
    "auto": "Check for launcher updates and data fixes when the launcher "
            "starts. Nothing is installed without your click, except the "
            "small signed data file.",
    "ask": "Never check unprompted. A button (or the update command) checks "
           "once, when you use it.",
    "off": "Never check, and ignore any data file fetched earlier. The "
           "launcher keeps the pins it shipped with.",
}

# group "updates": governed by the choice above. Every other group is reached
# only by the feature it names, when the player uses that feature.
Endpoint = namedtuple("Endpoint", "group hosts sends why")

GROUPS = (
    ("updates", "Updates and data fixes (your choice below)"),
    ("install", "Installing or updating Minecraft and its runtime"),
    ("signin", "Signing in and playing online"),
    ("content", "Browsing add-ons, mods and changelogs"),
    ("report", "Reporting a problem (only when you press Send)"),
)

_PLAIN_GET = ("A plain download request: your IP address and the launcher's "
              "name as User-Agent. No account, no identifier.")

ENDPOINTS = (
    Endpoint(
        "updates", ("raw.githubusercontent.com",),
        _PLAIN_GET,
        "The signed data file: component pins, per-version Minecraft fixes "
        "and incident notices. Checked against a key built into the "
        "launcher, so the server cannot alter it."),
    Endpoint(
        "updates", ("api.github.com",),
        _PLAIN_GET,
        "The launcher's latest release, when the data file does not name "
        "one."),
    Endpoint(
        "updates", ("github.com", "*.githubusercontent.com"),
        _PLAIN_GET,
        "The launcher update itself, only after you click Update. Checked "
        "against its SHA-256 before it replaces anything."),
    Endpoint(
        "install", ("api.github.com", "github.com",
                    "*.githubusercontent.com", "codeload.github.com"),
        _PLAIN_GET,
        "The game engine, the Minecraft downloader, the online-login "
        "libraries, the sign-in web runtime, umu-launcher and GE-Proton, "
        "each checked against a pinned SHA-256."),
    Endpoint(
        "install", ("raw.githubusercontent.com",),
        _PLAIN_GET,
        "The index of Minecraft builds (MinecraftBedrockArchiver/GdkLinks) "
        "the version picker lists."),
    Endpoint(
        "install", ("mirror.msys2.org", "curl.se"),
        _PLAIN_GET,
        "libcurl and the CA certificate bundle the game's network stack "
        "uses."),
    Endpoint(
        "install", ("files.pythonhosted.org",),
        _PLAIN_GET,
        "The Qt interface (PySide6), only where the system has none and the "
        "launcher installs its own copy."),
    Endpoint(
        "install", ("repo.steampowered.com",),
        _PLAIN_GET,
        "The Steam Linux Runtime container, downloaded by umu-launcher on "
        "first launch."),
    Endpoint(
        "install", ("displaycatalog.mp.microsoft.com",
                    "fe3.delivery.mp.microsoft.com",
                    "*.xboxlive.com", "*.dl.delivery.mp.microsoft.com",
                    "store-images.s-microsoft.com"),
        "Your Microsoft account's tokens, to Microsoft only: the Store "
        "checks that the account owns the game.",
        "The Minecraft package itself, its licence and its Store artwork, "
        "from Microsoft's own servers."),
    Endpoint(
        "signin", ("login.live.com", "account.microsoft.com",
                   "*.auth.xboxlive.com", "sisu.xboxlive.com"),
        "Your Microsoft credentials, typed into Microsoft's own page, and "
        "the tokens it returns.",
        "Signing in to your Microsoft account for the Store and Xbox Live."),
    Endpoint(
        "signin", ("userpresence.xboxlive.com", "peoplehub.xboxlive.com",
                   "events.xboxlive.com", "vortex.data.microsoft.com",
                   "*.playfabapi.com",
                   "multiplayer.minecraft.net", "pocket.realms.minecraft.net"),
        "Your Xbox Live tokens and what the game itself would send: "
        "presence, achievements.",
        "Online play: presence, friends, achievements, Realms and "
        "multiplayer, on the game's behalf while it runs."),
    Endpoint(
        "content", ("api.mcpedl.com", "mcpedl.com", "www.curseforge.com",
                    "levimc.org", "bedrock.graphics"),
        _PLAIN_GET + " Search words when you search.",
        "The add-on catalogue, LeviLamina mods and BetterRTX presets you "
        "browse or install."),
    Endpoint(
        "content", ("feedback.minecraft.net", "api.github.com"),
        _PLAIN_GET,
        "The Minecraft and launcher changelogs, when you open them."),
)

_REPORT_SENDS = (
    "The report exactly as the window shows it before you send: your title "
    "and comment, system information and log excerpts, with names, paths, "
    "addresses, account details and tokens removed. Your IP address, like "
    "any web request.")

ENDPOINTS += (
    Endpoint(
        "report", ("github.com",),
        "Nothing by the launcher itself: your browser opens the form, and "
        "you submit it with your own GitHub account.",
        f"The new-issue form of {ISSUE_REPO}, filled in with your report, "
        "when no report relay is used."),
)
if RELAY_URL:
    # Named the day a relay is deployed and RELAY_URL is set (bol.report).
    from urllib.parse import urlsplit as _urlsplit
    ENDPOINTS += (
        Endpoint(
            "report", (_urlsplit(RELAY_URL).hostname,),
            _REPORT_SENDS,
            f"The {PRETTY} report relay, which files your report as a "
            f"public issue on github.com/{ISSUE_REPO}. It keeps no copy "
            "and does not log your address."),
    )

NO_TELEMETRY = ("No telemetry, no usage statistics and no install or machine "
                "identifier is ever sent, to anyone.")


def _settings(settings):
    if settings is not None:
        return settings
    from .util import load_settings
    return load_settings()


def mode(settings=None):
    """'auto', 'ask' or 'off'; None while the player has not chosen.

    BOL_NETWORK_UPDATES decides for one run without recording anything, for
    packagers and scripted runs.
    """
    forced = os.environ.get(ENV, "").strip().lower()
    if forced in MODES:
        return forced
    value = _settings(settings).get(SETTING)
    return value if value in MODES else None


def decided(settings=None):
    return mode(settings) is not None


def background_allowed(settings=None):
    """Whether the launcher may check for updates without being asked."""
    return mode(settings) == "auto"


def feed_allowed(settings=None):
    """Whether signed data fetched earlier may be applied at all."""
    return mode(settings) in ("auto", "ask")


def set_mode(value):
    if value not in MODES:
        raise ValueError(f"unknown update mode {value!r}")
    from .util import load_settings, save_settings
    settings = load_settings()
    settings[SETTING] = value
    save_settings(settings)
    return value


def channel(settings=None):
    value = _settings(settings).get(CHANNEL_SETTING)
    return value if value in CHANNELS else "stable"


def set_channel(value):
    if value not in CHANNELS:
        raise ValueError(f"unknown update channel {value!r}")
    from .util import load_settings, save_settings
    settings = load_settings()
    settings[CHANNEL_SETTING] = value
    save_settings(settings)
    return value


def by_group():
    """[(title, [Endpoint, ...]), ...] in display order."""
    return [(title, [e for e in ENDPOINTS if e.group == key])
            for key, title in GROUPS]


def disclosure_text():
    """The whole list as plain text, for a terminal."""
    lines = [f"{PRETTY} connects to these hosts, and to no other:", ""]
    for title, endpoints in by_group():
        lines.append(title)
        for endpoint in endpoints:
            lines.append("  " + ", ".join(endpoint.hosts))
            lines.append("      why:  " + endpoint.why)
            lines.append("      sent: " + endpoint.sends)
        lines.append("")
    lines.append(NO_TELEMETRY)
    return "\n".join(lines)


def disclosure_html():
    """The same list as the rich text a QLabel shows."""
    from html import escape
    parts = []
    for title, endpoints in by_group():
        parts.append(f"<p><b>{escape(title)}</b></p><ul>")
        for endpoint in endpoints:
            parts.append(
                "<li><code>" + escape(", ".join(endpoint.hosts)) + "</code>"
                "<br>" + escape(endpoint.why)
                + "<br><i>Sent: " + escape(endpoint.sends) + "</i></li>")
        parts.append("</ul>")
    parts.append(f"<p><b>{escape(NO_TELEMETRY)}</b></p>")
    return "".join(parts)


def choose_in_terminal(ask=input, out=print, interactive=None):
    """Show the list and record a choice, once; the mode now in force.

    Without a terminal nothing is recorded and nothing is checked in the
    background: a script gets one line saying where the choice is made.
    """
    current = mode()
    if current is not None:
        return current
    if interactive is None:
        from .log import IS_TTY
        interactive = IS_TTY
    if not interactive:
        out(f":: Update checks are off until you choose: {APP} network")
        return None
    out(disclosure_text())
    out("")
    for key in MODES:
        out(f"  {key:<5} {MODE_HELP[key]}")
    while True:
        try:
            answer = ask("Check for updates and data fixes? "
                         "[auto/ask/off] ").strip().lower()
        except EOFError:
            return None
        picked = next((m for m in MODES if answer and m.startswith(answer)),
                      None)
        # "a" is both auto and ask: make the player spell it.
        if picked and not (answer == "a"):
            return set_mode(picked)
        out("Type auto, ask or off.")
