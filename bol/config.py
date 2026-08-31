"""bol.config — constants, paths, repos and URLs (no logic, no side effects)."""
# SPDX-License-Identifier: MIT

import os
from pathlib import Path

APP = "bedrock-on-linux"
PRETTY = "BedrockOnLinux"
VERSION = "2.2.5"
# Published Flatpak identity; used to print a runnable command for that layout.
FLATPAK_APP_ID = "io.github.wyze3306.BedrockOnLinux"

HOME = Path.home()
XDG_DATA_HOME = Path(
    os.environ.get("XDG_DATA_HOME") or HOME / ".local" / "share"
).expanduser()
XDG_CONFIG_HOME = Path(
    os.environ.get("XDG_CONFIG_HOME") or HOME / ".config"
).expanduser()
DEFAULT_DATA = XDG_DATA_HOME / APP
LEGACY_DATA = HOME / ".local" / "share" / APP

# Resolve relocation before exporting DATA; imported path constants cannot be
# changed afterwards. BOL_HOME takes priority over the persistent pointer.
INSTALL_LOCATION_FILE = XDG_CONFIG_HOME / APP / "install_location"
LEGACY_INSTALL_LOCATION_FILE = HOME / ".config" / APP / "install_location"

_bol_home = os.environ.get("BOL_HOME", "").strip()
if _bol_home:
    _data_path = _bol_home
else:
    _data_path = str(DEFAULT_DATA)
    # Keep the pre-XDG pointer fallback so upgrades retain relocated data.
    _pointer_candidates = (INSTALL_LOCATION_FILE,)
    if LEGACY_INSTALL_LOCATION_FILE != INSTALL_LOCATION_FILE:
        _pointer_candidates += (LEGACY_INSTALL_LOCATION_FILE,)
    for _pointer in _pointer_candidates:
        try:
            if not _pointer.is_file():
                continue
            _custom_home = _pointer.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if _custom_home:
            _data_path = _custom_home
            break

DATA = Path(_data_path)
PROTON_DIR = DATA / "proton"
UMU_DIR = DATA / "umu"
COMPAT = DATA / "compatdata"
PFX = COMPAT / "pfx"
GAMES = DATA / "games"
CONTENT = DATA / "content"
CACHE = DATA / "cache"
LOGS = DATA / "logs"
MSA_DIR = DATA / "msa"
SETTINGS = DATA / "settings.json"

GDK_PROTON_REPO = "Weather-OS/GDK-Proton"
UMU_REPO = "Open-Wine-Components/umu-launcher"
UMU_VERSION = "1.4.3"
UMU_ASSET = "umu-launcher-1.4.3-zipapp.tar"
UMU_ARCHIVE_SHA256 = \
    "3f8fdc033f547afdb3408ea48ad07194769405148dcfa2b2f945b7fb368a33bb"
UMU_RUN_SHA256 = \
    "577181dbff2eccdaa78b411c0fd1aa7fde574028449c3e0e99f508536a76870e"
MINGW_CURL = "https://mirror.msys2.org/mingw/mingw64/mingw-w64-x86_64-curl-8.17.0-1-any.pkg.tar.zst"
CACERT_URL = "https://curl.se/ca/cacert.pem"

# WineGDK reads the refresh token from this registry key and requires its
# hardcoded MSA application ID.
MSA_CLIENT_ID = "0000000048183522"
MSA_SCOPE = "service::user.auth.xboxlive.com::MBI_SSL"
MSA_CONNECT = "https://login.live.com/oauth20_connect.srf"
MSA_TOKEN = "https://login.live.com/oauth20_token.srf"
WINEGDK_REG = r"Software\Wine\WineGDK"

# Exact WineGDK source used for the reviewed native-online engine.
WINEGDK_SOURCE_COMMIT = "75637b674e1f191e65753663c4c0c32bea05ba6e"
GDK_DEPS_URL = "https://github.com/minecraft-linux/mcpelauncher-gdk-dependencies/releases/download/v0.0.0"
GDK_DEPS_DLLS = ("libHttpClient.GDK.dll", "XCurl.dll")
# This OpenSSL XCurl payload avoids Wine secur32 failures against Azure and is
# built reproducibly by scripts/build-openssl-xcurl.sh.
OPENSSL_XCURL_SET = DATA / "xodus-xcurl" / "openssl-set"
OPENSSL_XCURL_REV = "504bb166e4e7"
# Integrity pin for the complete online-login payload.
OPENSSL_XCURL_ARCHIVE_SHA256 = "504bb166e4e737ad81c3ac8e7a917740b28478f69acd89e538c3bf921c29523f"
WINEGDK_OUT = PROTON_DIR / "GDK-Proton-xuser"
# Managed engines are accepted only when their archive and source identity
# match the reviewed pins below.
WINEGDK_PREBUILT_REPO = "Wyze3306/BedrockOnLinux"
# The commit alone does not identify vendored follow-up patches.
WINEGDK_SOURCE_MANIFEST_SHA256 = "0feb01ca058086eccf4f4a0e6895f541547ae89aa0d2ab86f08291224de5ed46"
WINEGDK_BUILD_REV = "wow64-archs-native17"
# native17 carries the ntdll loader patches the Microsoft Store packages need:
# 0007 maps the main image from a descriptor, 0008 from a path so it survives
# the Steam Linux Runtime container. Their game executable stays encrypted on
# disk, so an engine without them cannot start the game -- which is why an
# unset pin here makes _verify_engine_archive() refuse every candidate rather
# than fall back. Produced by the reviewed build-engine.yml run of this branch,
# which refuses any archive that does not reproduce these bytes.
#
# It also carries the first Wayland driver of our own build (issue #180).
# native16 was published twice: once before that fix and once after, under the
# same release tag and the same file name, which replaced the bytes 2.2.2 was
# pinned against and left every fresh install of it rejecting the engine. A
# revision is a name for one set of bytes -- rebuild under a new one rather
# than overwrite a published archive.
WINEGDK_ARCHIVE_SHA256 = "12fa379f012410832eab54c719efaa4e0e327a3b6839b0859f851d1b952abed2"
# Build workflows verify this deterministic intermediate before reusing it.
WINEGDK_PREFIX_SHA256 = "bfbb08107e7aa16842cf12814013a1cb94acd5a1c2fe8eaec24a401c4b57fc09"

SELF_REPO = WINEGDK_PREBUILT_REPO

# Where the launcher points people who ask where it came from -- the two
# links Discord shows under a play session, and the ones printed elsewhere.
SITE_URL = "https://wyze3306.github.io/BedrockOnLinux/"
DISCORD_INVITE = "https://discord.gg/5YJq54Yhbu"

# Discord Rich Presence. Discord names the *application* in "Playing ...", so
# the application registered on discord.com/developers must be called exactly
# like this launcher; the id below is the one it hands out. Empty means the
# feature is off everywhere, which is what a fork with no application of its
# own wants -- it must never advertise ours. BOL_DISCORD_APP_ID overrides it.
DISCORD_APP_ID = ""
# Rich Presence artwork. Each of these is either an asset key uploaded to the
# application (Rich Presence > Art Assets) or a full https:// URL, which
# Discord fetches and proxies itself -- and the URL form is why the image
# shows up at all here: an application can be configured and working while its
# Art Assets are still empty, and a key with no artwork behind it silently
# shows nothing. The default points at the icon this project already publishes
# on its own site, so the logo works with no developer-portal step. Empty
# means no image; the whole assets block is dropped when both are empty.
DISCORD_LARGE_IMAGE = f"{SITE_URL}discord-large.png"
DISCORD_SMALL_IMAGE = ""

# Minecraft is acquired through Xodus (GPL-3.0), which signs in to the user's
# own Microsoft account, obtains the title license and streams the MSIXVC
# package from the official Xbox CDN. It replaced a third-party repository that
# redistributed a DRM-stripped copy of the game. See third_party/xodus/README.md.
XODUS_REPO = "xodus-gaming/xodus"
XODUS_SOURCE_COMMIT = "4615749c6e02cc3b9acce2abbe9916fe8c376f9a"
# A patched binary is not the upstream commit's binary, so the rev names the
# patches too: <commit12>-p<n>, for the n patch files in
# third_party/xodus/patches. scripts/build-xodus-cli.sh derives it the same
# way, so adding a patch there and forgetting this makes the build produce an
# archive the workflow's assert cannot find -- which is the intended failure,
# not a silent replacement of a published one.
XODUS_REV = "4615749c6e02-p4"
# Integrity pin for the CI-built xodus-cli archive, produced by the reviewed
# build-xodus.yml run of this branch. The workflow refuses any archive that
# does not reproduce these bytes.
XODUS_ARCHIVE_SHA256 = "31399ede9c4c1c6543c0ae034555ef962cfb77592cd3e41090010c5473a80163"
XODUS_DIR = DATA / "xodus"
XODUS_BIN = XODUS_DIR / "xodus-cli"
# xodus-cli links wry/tao unconditionally, so libwebkit2gtk-4.1 has to be
# loadable before main() runs -- not only for the sign-in window, but for the
# download and for `xodus-cli run`, which starts every encrypted game. Hosts
# that ship no WebKitGTK and cannot install one (SteamOS and other immutable
# images, issue #184) get this runtime instead: the closure of that stack,
# built by build-xodus.yml from the same pinned snapshot as xodus-cli.
XODUS_WEBVIEW_DIR = DATA / "xodus-webview"
XODUS_WEBVIEW_REV = "trixie-1"
# Integrity pin for the CI-built runtime, produced by the reviewed
# build-xodus.yml run of this branch. Empty means "never published", and the
# launcher then reports the missing library instead of installing unverified
# bytes -- publish .github/workflows/build-xodus.yml and pin the SHA-256 it
# prints. One line, like every pin here: the build and CI checks read it with
# grep + cut, and a continuation makes them compare the variable name.
XODUS_WEBVIEW_SHA256 = "a9b04506446ba57fe40bae9e731857e681da230ce4db20e6613ae558441a0c6e"
# The compiled-in directory WebKitGTK spawns its helper processes from. Modern
# builds drop the WEBKIT_EXEC_PATH override (it is developer-mode only), so the
# bundled library carries this literal and the launcher rewrites it in place.
XODUS_WEBVIEW_EXEC_DIR = "/usr/lib/x86_64-linux-gnu/webkit2gtk-4.1"
# Xodus keeps its tokens in a file keyring (built with --features
# key-chain-file) instead of a D-Bus secret service, which does not exist in a
# Game Mode session or inside a Flatpak sandbox. It puts that file in $HOME,
# which is why xodus-cli is given a home of its own here: inside the Flatpak
# $HOME is a tmpfs thrown away with the sandbox, and a lost keyring is not
# merely a sign-out -- the next command provisions a *new* Microsoft device,
# and an account may hold ten before the Store refuses to license Minecraft at
# all ("Device group is full", issue #198). Under DATA the tokens persist
# wherever the rest of the launcher's state does, Flatpak included.
XODUS_HOME = DATA / "xodus-home"
XODUS_KEYRING = XODUS_HOME / ".xodus-keyring.ron"
# Where sign-ins made before that directory existed left their tokens.
LEGACY_XODUS_KEYRING = HOME / ".xodus-keyring.ron"

# GetBasePackage only ever answers with the current build, but Microsoft's CDN
# keeps the older ones reachable, and MinecraftBedrockArchiver/GdkLinks indexes
# where they live. That index holds no game data: every URL points at
# assets*.xboxlive.com, and Xodus still reads the package's own content id from
# the downloaded header and asks Microsoft for that licence, so the account
# still has to own Minecraft. It only restores the choice of build.
GDK_LINKS_REPO = "MinecraftBedrockArchiver/GdkLinks"
GDK_LINKS_URL = ("https://raw.githubusercontent.com/"
                 "MinecraftBedrockArchiver/GdkLinks/master/urls.json")
# The CDN serves these over plain HTTP only. The payload is AES-XTS encrypted
# and worthless without the licence, so this costs no confidentiality; the
# content id below is checked against every indexed URL so a bad or tampered
# index cannot point the downloader at a different product.
MC_PRODUCTS = (
    {"id": "release", "product": "9NBLGGH2JHXJ", "channel": "release",
     "content_id": "7792d9ce-355a-493c-afbd-768f4a77c3b0",
     "name": "Minecraft for Windows", "beta": False},
    {"id": "preview", "product": "9P5X4QVLC2XR", "channel": "preview",
     "content_id": "98bd2335-9b01-4e4c-bd05-ccc01614078b",
     "name": "Minecraft Preview for Windows", "beta": True},
)


def _legacy_install_location_file() -> Path:
    return HOME / ".config" / APP / "install_location"


def get_install_location() -> str:
    """Where the app's data directory currently resolves to."""
    return str(DATA)

def default_install_location() -> str:
    """The location used when no custom install location is set."""
    return str(DEFAULT_DATA)

def set_install_location(path) -> None:
    """Persist a custom data-directory location for future runs.

    Raises RuntimeError if BOL_HOME is set externally (relocation disabled).
    """
    if os.environ.get("BOL_HOME", "").strip():
        raise RuntimeError("Cannot change location when BOL_HOME is set externally")
    INSTALL_LOCATION_FILE.parent.mkdir(parents=True, exist_ok=True)
    INSTALL_LOCATION_FILE.write_text(
        str(Path(path).expanduser()), encoding="utf-8")
    legacy = _legacy_install_location_file()
    if legacy != INSTALL_LOCATION_FILE:
        legacy.unlink(missing_ok=True)

def clear_install_location() -> None:
    """Revert to the default location.

    Raises RuntimeError if BOL_HOME is set externally (relocation disabled).
    """
    if os.environ.get("BOL_HOME", "").strip():
        raise RuntimeError("Cannot change location when BOL_HOME is set externally")
    INSTALL_LOCATION_FILE.unlink(missing_ok=True)
    legacy = _legacy_install_location_file()
    if legacy != INSTALL_LOCATION_FILE:
        legacy.unlink(missing_ok=True)

def is_relocation_allowed() -> bool:
    """Return True if the data directory can be relocated via the GUI."""
    return not bool(os.environ.get("BOL_HOME", "").strip())
