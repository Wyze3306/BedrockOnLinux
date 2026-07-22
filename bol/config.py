"""bol.config — constants, paths, repos and URLs (no logic, no side effects)."""
# SPDX-License-Identifier: MIT

import os
from pathlib import Path

APP = "bedrock-on-linux"
PRETTY = "BedrockOnLinux"
VERSION = "2.0.0"

HOME = Path.home()

# The app's data directory is itself relocatable (e.g. to a drive with
# more free space). BOL_HOME always wins if set. Otherwise, a small
# pointer file at a fixed, never-moving location can record a
# user-chosen location (set via Settings -> Advanced -> "Game files
# location" in the GUI). This has to be read here, before DATA is
# computed below -- nothing later can move DATA retroactively once
# other modules have already imported it.
INSTALL_LOCATION_FILE = HOME / ".config" / APP / "install_location"
if "BOL_HOME" not in os.environ and INSTALL_LOCATION_FILE.exists():
    try:
        _custom_home = INSTALL_LOCATION_FILE.read_text(
            encoding="utf-8").strip()
    except OSError:
        _custom_home = ""
    if _custom_home:
        os.environ["BOL_HOME"] = _custom_home

DATA = Path(os.environ.get("BOL_HOME", HOME / ".local/share" / APP))
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
UMU_VERSION = "1.4.0"
UMU_ASSET = "umu-launcher-1.4.0-zipapp.tar"
UMU_ARCHIVE_SHA256 = \
    "138ce4b8843608a257d4bee88191ca78a989778bcefd8abb3c1d1aaac3ac6fb8"
UMU_RUN_SHA256 = \
    "bdac203e74f77b8b375ce72de0671c5a227815b912faacd09ecf450ee6650f62"
GAME_ARCHIVE_REPO = "bubbles-wow/mcbe-gdk-unpack-archive"
MINGW_CURL = "https://mirror.msys2.org/mingw/mingw64/mingw-w64-x86_64-curl-8.17.0-1-any.pkg.tar.zst"
CACERT_URL = "https://curl.se/ca/cacert.pem"

# In-game Microsoft login: WineGDK's XUser has no sign-in UI — it reads an
# MSA OAuth refresh token from the prefix registry (WINEGDK_REG) and does the
# Xbox Live exchange itself. The launcher only runs the device-code flow and
# seeds that token. MSA_CLIENT_ID must equal WineGDK's hardcoded msaAppId or
# the refresh is rejected.
MSA_CLIENT_ID = "0000000048183522"
MSA_SCOPE = "service::user.auth.xboxlive.com::MBI_SSL"
MSA_CONNECT = "https://login.live.com/oauth20_connect.srf"
MSA_TOKEN = "https://login.live.com/oauth20_token.srf"
WINEGDK_REG = r"Software\Wine\WineGDK"

# Exact WineGDK source used for the reviewed native-online engine. It
# completes XGame/XUser identity, XSAPI context initialization, stable native
# task-queue bootstrap and Realms audience routing, implements the WinAppSDK
# file picker, and removes the Minecraft process-memory patcher.
WINEGDK_SOURCE_COMMIT = "75637b674e1f191e65753663c4c0c32bea05ba6e"
# OSS replacements for the game's GDK Xbox-Live DLLs (minecraft-linux project).
GDK_DEPS_URL = "https://github.com/minecraft-linux/mcpelauncher-gdk-dependencies/releases/download/v0.0.0"
GDK_DEPS_DLLS = ("libHttpClient.GDK.dll", "XCurl.dll")
# OpenSSL libcurl + CA-shim + cryptbase stub set: installed as XCurl.dll so
# Minecraft's PlayFab traffic goes over OpenSSL TLS instead of Wine secur32
# (whose handshake Azure Front Door silently FINs → endless sign-in loop).
# Too big to bundle (20 MB) — downloaded once from the app's releases as
# openssl-xcurl-set-<rev>.tar.gz. Built reproducibly from source by
# scripts/build-openssl-xcurl.sh (pinned msys2 curl closure + source shim +
# source cryptbase stub); the rev/SHA below track that build.
OPENSSL_XCURL_SET = DATA / "xodus-xcurl" / "openssl-set"
OPENSSL_XCURL_REV = "504bb166e4e7"
# Exact reviewed online-login payload. A filename/revision alone is not an
# integrity boundary; local siblings and downloaded assets must match this pin.
OPENSSL_XCURL_ARCHIVE_SHA256 = "504bb166e4e737ad81c3ac8e7a917740b28478f69acd89e538c3bf921c29523f"
WINEGDK_OUT = PROTON_DIR / "GDK-Proton-xuser"
# Prebuilt engine: users download GDK-Proton-xuser-<build-rev>.tar.gz from the
# app's releases instead of compiling Wine.  Managed engines are fail-closed:
# the launcher only accepts the exact archive hash compiled into this version.
# Build locally with scripts/package-engine.sh, then pin its printed SHA-256.
WINEGDK_PREBUILT_REPO = "Wyze3306/BedrockOnLinux"
# Bump when the build/packaging method changes → forces a clean rebuild.
WINEGDK_BUILD_REV = "wow64-archs-native5"
# SHA-256 of the reviewed, deterministic engine archive. An invalid value makes
# the installer fail closed rather than accepting a differently packed engine.
WINEGDK_ARCHIVE_SHA256 = "4c0b8b0f147b38bf4e34cef68ecda35172fc1728b43bf604d3554ccc595c72af"
# SHA-256 of the deterministic WineGDK prefix tarball (build intermediate, not a
# runtime download). build-winegdk.yml asserts it, and build-engine.yml verifies
# a reused prefix against it, so the reuse path fails fast against a committed
# expectation instead of only transitively via the engine archive hash above.
WINEGDK_PREFIX_SHA256 = "bbb249a999bc6000c0cf0cb2654382cb4e0988a6c2424cb1f12d8d2aa90810c6"

SELF_REPO = WINEGDK_PREBUILT_REPO

def get_install_location() -> str:
    """Where the app's data directory currently resolves to."""
    return str(DATA)

def default_install_location() -> str:
    """The location used when no custom install location is set."""
    return str(HOME / ".local/share" / APP)

def set_install_location(path) -> None:
    """Persist a custom data-directory location for future runs."""
    INSTALL_LOCATION_FILE.parent.mkdir(parents=True, exist_ok=True)
    INSTALL_LOCATION_FILE.write_text(
        str(Path(path).expanduser()), encoding="utf-8")

def clear_install_location() -> None:
    """Revert to the default location."""
    INSTALL_LOCATION_FILE.unlink(missing_ok=True)
