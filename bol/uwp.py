"""bol.uwp — Minecraft for Windows as it shipped up to 1.21.51 (#387).

Before 1.21.120 the game was a UWP package, and Windows Update still serves
every release of it, 0.13 to 1.21.51, to anyone who asks. Wine cannot run a
UWP app; designgears' wine-uwp can: Wine 11 with the UWP app model
(CoreWindow, CoreMessaging, the package graph), running on Windows' own
system DLLs. This module brings the pieces together as wine-uwp's own
scripts would, without the host tools they assume (7-Zip, curl, sha256sum):

* the runtime, wine-uwp's release tarball, pinned by SHA-256;
* the Windows files it borrows, read out of Microsoft's evaluation ISO by
  HTTP ranges (bol.wim) -- about 450 MB of a 7 GB image;
* the Direct3D runtime (DXVK, vkd3d-proton) and the Store Engagement
  framework the game depends on, pinned;
* a prefix of the profile's own, made by wine-uwp's new-prefix.sh;
* each build: its package from Windows Update, checked against the SHA-1
  Windows Update gives for it, installed by wine-uwp's wineuwp-install into
  games/uwp/<version>/, so it is listed and removed like any other build.

wine-uwp answers "full licence" to every app that asks, the trial check
included, so it would run Minecraft for anyone. The launcher does not: the
Store is asked whether the signed-in account owns Minecraft for Windows (the
same product) before a build is installed, and again before each start
(xodus.require_licence).
"""
# SPDX-License-Identifier: MIT

import base64
import hashlib
import json
import os
import platform
import re
import select
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path

from . import wim
from .archive import safe_extract_tar
from .config import (
    APP,
    CACHE,
    GAMES,
    LOGS,
    MC_UWP_VERSIONDB_URL,
    STORE_ENGAGEMENT_APPX_SHA256,
    STORE_SERVICES_SDK_SHA256,
    STORE_SERVICES_SDK_URL,
    UWP_D3D_RUNTIME,
    WINDOWS_EVAL_ISO_SIZE,
    WINDOWS_EVAL_ISO_URL,
    WINE_UWP,
    WINE_UWP_PREFIX,
    WINE_UWP_SHA256,
    WINE_UWP_TAG,
    WINE_UWP_URL,
)
from .log import BolError, info, ok, warn
from .util import _fetch_with_fallback, download, sha256_file

SOURCE = "wine-uwp"
FAMILY = "Microsoft.MinecraftUWP_8wekyb3d8bbwe"
EXE = "Minecraft.Windows.exe"

_VERSION = re.compile(r"\d+\.\d+\.\d+(?:\.\d+)?")
_UPDATE_ID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", re.I)
_CATALOGUE_TTL = 12 * 3600
_ENGAGEMENT = "Microsoft.Services.Store.Engagement"
# What a first install needs on the disk besides the build: the runtime, the
# Windows files, the package downloaded beside the game it unpacks to.
_FIRST_INSTALL_ROOM = 5 << 30
# wine-uwp's release is built on a glibc 2.38 system: wineserver, winex11.so
# and the other Unix halves of Wine need its symbols.
_GLIBC_FLOOR = (2, 38)


class UwpError(BolError):
    """A UWP build could not be installed or started."""


def is_uwp(edition):
    """Whether an edition is one of the UWP builds wine-uwp runs."""
    return (edition or {}).get("source") == SOURCE


def host_problem():
    """Why wine-uwp cannot run on this system, or None."""
    if platform.machine().lower() not in ("x86_64", "amd64"):
        return ("The UWP builds of Minecraft run on wine-uwp, which is built "
                "for x86-64 PCs only.")
    try:
        version = os.confstr("CS_GNU_LIBC_VERSION").split()[1]
        have = tuple(int(part) for part in version.split(".")[:2])
    except (AttributeError, IndexError, OSError, ValueError):
        return None
    if have < _GLIBC_FLOOR:
        floor = ".".join(map(str, _GLIBC_FLOOR))
        return (f"The UWP builds of Minecraft run on wine-uwp, which needs "
                f"glibc {floor} or newer (Ubuntu 24.04, Debian 13, Fedora 39 "
                f"or later); this system has {version}. The Flatpak of this "
                "launcher brings its own, recent enough.")
    return None


def _require_host():
    problem = host_problem()
    if problem:
        raise UwpError(problem)


# ----------------------------------------------------------------- paths


def runtime_dir():
    return WINE_UWP / "runtime" / WINE_UWP_TAG


def windows_dir():
    return WINE_UWP / "windows"


def ext_dir():
    return WINE_UWP / "ext"


def d3d_runtime_dir():
    return ext_dir() / "d3d-runtime" / "x86_64-windows"


def prefix_dir():
    return WINE_UWP_PREFIX


def install_data():
    """The data directory wineuwp-install is given: its stage/ and the
    frameworks beside it, next to the builds it makes.

    Resolved, because wineuwp-install writes the frameworks' paths into each
    build's launch.env: a profile reaches games/ through a link, and a path
    through one profile's link breaks once that profile is gone.
    """
    return (GAMES / "uwp").resolve() / ".wine-uwp"


def frameworks_dir():
    return install_data() / "stage" / "frameworks"


def _share():
    return runtime_dir() / "share" / "wine-uwp"


def _bin():
    return runtime_dir() / "bin"


# ------------------------------------------------------------- catalogue


def version_catalogue(edition, ignore_cache=False):
    """The UWP releases, newest first: ``{"version", "urls", "update_id"}``.

    From mc-w10-versiondb, which only says where each build is: the package
    itself comes from Windows Update, over TLS pinned to Microsoft's root,
    and is checked against the digest it gives (install). Betas and Previews
    are left out: Windows Update serves them to Xbox Insiders only.
    """
    try:
        rows = _fetch_with_fallback(
            "mc-w10-versiondb.json", MC_UWP_VERSIONDB_URL,
            ttl=0 if ignore_cache else _CATALOGUE_TTL)
    except Exception as exc:
        raise UwpError(
            f"Could not read the list of UWP builds of Minecraft ({exc}).") \
            from exc
    if not isinstance(rows, list):
        raise UwpError("The list of UWP builds of Minecraft is unreadable.")
    builds = {}
    for row in rows:
        if not (isinstance(row, list) and len(row) >= 3):
            continue
        version, update_id, kind = str(row[0]), str(row[1]), row[2]
        if kind != 0 or not _VERSION.fullmatch(version) \
                or not _UPDATE_ID.fullmatch(update_id):
            continue
        # A version listed twice is the later row's, as wine-uwp reads it.
        builds[version] = update_id.lower()
    out = [{"version": version, "urls": [], "update_id": update_id}
           for version, update_id in builds.items()]
    out.sort(key=lambda entry: _version_key(entry["version"]), reverse=True)
    return out


def _version_key(version):
    return tuple(int(part) for part in version.split("."))


# ------------------------------------------------------------ ownership


def require_ownership(edition):
    """The Store's word that the account owns Minecraft for Windows."""
    from . import xodus
    xodus.require_licence(edition["content_id"],
                          title="Minecraft for Windows")


# --------------------------------------------------------------- runtime


def _sha256_matches(path, expected):
    try:
        return sha256_file(path) == expected
    except OSError:
        return False


def _fetch_pinned(url, path, sha256, label, progress=None):
    """``url`` at ``path``, downloaded unless the copy there is the one
    pinned; a download that is not is deleted and refused."""
    if _sha256_matches(path, sha256):
        return path
    download(url, path, label, progress)
    if not _sha256_matches(path, sha256):
        path.unlink(missing_ok=True)
        raise UwpError(f"The downloaded {label} is not the file this "
                       "launcher was released with, so it was not used. Try "
                       "again.")
    return path


def _replace_dir(staged, target):
    """Move a finished directory into place, the previous one out of it."""
    retired = target.with_name("." + target.name + ".old")
    shutil.rmtree(retired, ignore_errors=True)
    if target.exists() or target.is_symlink():
        target.replace(retired)
    staged.replace(target)
    shutil.rmtree(retired, ignore_errors=True)


def ensure_runtime(progress=None):
    """wine-uwp's release, unpacked once into the shared folder."""
    root = runtime_dir()
    marker = root / ".bol-sha256"
    try:
        if marker.read_text().strip() == WINE_UWP_SHA256 \
                and (root / "bin" / "wine").is_file():
            return root
    except OSError:
        pass
    root.parent.mkdir(parents=True, exist_ok=True)
    archive = CACHE / f"wine-uwp-{WINE_UWP_TAG}.tar.xz"
    info(f"Downloading wine-uwp {WINE_UWP_TAG}, which runs the UWP builds "
         "of Minecraft …")
    _fetch_pinned(WINE_UWP_URL, archive, WINE_UWP_SHA256,
                  f"wine-uwp {WINE_UWP_TAG}", progress)
    staging = root.with_name("." + root.name + ".unpacking")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        with tarfile.open(archive) as tar:
            safe_extract_tar(tar, staging)
        top = staging / "wine-uwp"
        if not (top / "bin" / "wine").is_file() \
                or not (top / "share" / "wine-uwp" / "run.sh").is_file():
            raise UwpError("The wine-uwp download holds no Wine.")
        (top / ".bol-sha256").write_text(WINE_UWP_SHA256 + "\n")
        _replace_dir(top, root)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    archive.unlink(missing_ok=True)
    _prune_runtimes(keep=root)
    ok(f"wine-uwp {WINE_UWP_TAG} ready")
    return root


def _prune_runtimes(keep):
    """Older wine-uwp releases, which nothing starts any more."""
    for folder in keep.parent.iterdir():
        if folder != keep and folder.is_dir() and not folder.is_symlink() \
                and folder.name.startswith("v"):
            shutil.rmtree(folder, ignore_errors=True)


def _manifest():
    path = _share() / "packaging" / "harvest" / "manifest.txt"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UwpError(f"wine-uwp's list of Windows files is missing "
                       f"({exc}).") from exc
    return text, hashlib.sha256(text.encode()).hexdigest()


def windows_files_ready():
    stamp = windows_dir() / ".bol-harvest.json"
    try:
        record = json.loads(stamp.read_text(encoding="utf-8"))
        manifest = _manifest()[1]
    except (OSError, ValueError, UwpError):
        return False
    return (record.get("manifest") == manifest and (
        windows_dir() / "Windows" / "System32" / "config" / "SOFTWARE")
        .is_file())


def ensure_windows_files(progress=None):
    """The Windows files wine-uwp borrows, out of Microsoft's ISO."""
    out = windows_dir()
    if windows_files_ready():
        return out
    text, manifest = _manifest()
    lines = wim.manifest_lines(text)
    info("Reading the Windows files wine-uwp needs from Microsoft's Windows "
         "11 evaluation image: about 450 MB of the 7 GB ISO, straight from "
         "Microsoft's server …")
    try:
        source = wim.HttpFile.open(WINDOWS_EVAL_ISO_URL, user_agent=APP)
    except wim.WimError:
        raise
    except OSError as exc:
        raise UwpError(f"Microsoft's download server for the Windows image "
                       f"could not be reached ({exc}).") from exc
    if source.size != WINDOWS_EVAL_ISO_SIZE:
        warn("Microsoft now serves another Windows 11 evaluation image than "
             "the one wine-uwp was tested with; reading it anyway. Every "
             "file is still checked against the image's own checksums.")
    pool = wim.decompression_pool()
    try:
        image = wim.open_iso_wim(source, pool=pool)
        count, missing = wim.harvest(image, lines, out, progress=progress)
    finally:
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
    record = {"manifest": manifest, "iso_size": source.size,
              "iso_url": source.url, "files": count, "missing": len(missing),
              "read": source.fetched, "when": int(time.time())}
    (out / ".bol-harvest.json").write_text(json.dumps(record, indent=1) + "\n",
                                          encoding="utf-8")
    ok(f"{count} Windows files ready ({source.fetched >> 20} MiB read from "
       "the image)")
    return out


def ensure_d3d_runtime(progress=None):
    """DXVK's dxgi/d3d11 and vkd3d-proton's d3d12/d3d12core, where
    wineuwp-install links them beside the game (install-d3d-runtime.sh)."""
    out = d3d_runtime_dir()
    out.mkdir(parents=True, exist_ok=True)
    for name, version, url, sha256, dlls in UWP_D3D_RUNTIME:
        folder = ext_dir() / f"{name}-{version}"
        x64 = folder / "x64"
        if not all((x64 / dll).is_file() for dll in dlls):
            archive = CACHE / Path(url).name
            _fetch_pinned(url, archive, sha256, f"{name} {version}", progress)
            staging = folder.with_name("." + folder.name + ".unpacking")
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            try:
                _extract_tarball(archive, staging)
                tops = [p for p in staging.iterdir() if p.is_dir()]
                if len(tops) != 1 or not all(
                        (tops[0] / "x64" / dll).is_file() for dll in dlls):
                    raise UwpError(f"The {name} download holds no x64 DLLs.")
                _replace_dir(tops[0], folder)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
            archive.unlink(missing_ok=True)
        for dll in dlls:
            link = out / dll
            target = (x64 / dll).resolve()
            if link.is_symlink() and Path(os.readlink(link)) == target:
                continue
            link.unlink(missing_ok=True)
            link.symlink_to(target)
    return out


def _extract_tarball(archive, dest):
    archive = Path(archive)
    if archive.name.endswith(".zst"):
        unzstd = shutil.which("unzstd") or shutil.which("zstd")
        if not unzstd:
            raise UwpError("zstd is needed to unpack vkd3d-proton; install "
                           "the zstd package.")
        with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
            plain = Path(tmp) / "archive.tar"
            subprocess.run([unzstd, "-q", "-d", "-f", "-o", str(plain),
                            str(archive)], check=True,
                           stdout=subprocess.DEVNULL)
            with tarfile.open(plain) as tar:
                safe_extract_tar(tar, dest)
        return
    with tarfile.open(archive) as tar:
        safe_extract_tar(tar, dest)


def ensure_store_engagement(progress=None):
    """The Microsoft.Services.Store.Engagement framework (x64), from the
    Store Services SDK installer (fetch-store-engagement.sh, without 7z)."""
    from .gameinput import _cab_payload, _msi_embedded_cab
    fw = frameworks_dir()
    if any(fw.glob(f"{_ENGAGEMENT}_*_x64__8wekyb3d8bbwe/AppxManifest.xml")):
        return fw
    msi = CACHE / "MicrosoftStoreServicesSDK-10.0.5.msi"
    _fetch_pinned(STORE_SERVICES_SDK_URL, msi, STORE_SERVICES_SDK_SHA256,
                  "Microsoft Store Services SDK", progress)
    package = next((data for _size, data in _cab_payload(
        _msi_embedded_cab(msi.read_bytes()) or b"")
        if hashlib.sha256(data).hexdigest() == STORE_ENGAGEMENT_APPX_SHA256),
        None)
    if package is None:
        raise UwpError("The Store Services SDK holds no x64 Store Engagement "
                       "framework.")
    with zipfile.ZipFile(_bytes_file(package)) as appx:
        manifest = appx.read("AppxManifest.xml").decode("utf-8", "replace")
        version = re.search(r'<Identity\b[^>]*\bVersion="([^"]+)"', manifest,
                            re.S).group(1)
        target = fw / f"{_ENGAGEMENT}_{version}_x64__8wekyb3d8bbwe"
        staging = target.with_name("." + target.name + ".unpacking")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            from .wustore import _unpack_zip
            _unpack_zip(appx, staging)
            _replace_dir(staging, target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    msi.unlink(missing_ok=True)
    return fw


def _bytes_file(data):
    import io
    return io.BytesIO(data)


# ------------------------------------------------------------- processes


def environment(prefix=None, debug=None):
    """What wine-uwp's scripts and programs run with (bin/wine-uwp's own
    exports): which Wine, which prefix, where the Windows files are."""
    env = dict(os.environ)
    for name in ("WINEPREFIX", "WINEDLLOVERRIDES", "WINEDLLPATH", "WINEARCH",
                 "WINELOADER", "WINESERVER", "WINEDEBUG", "WINEESYNC",
                 "WINEFSYNC", "WAYLAND_DISPLAY", "PROTON_EAC_RUNTIME"):
        env.pop(name, None)
    share = _share()
    binaries = _bin()
    env.update({
        "WINE_UWP_ROOT": str(share),
        "UWP_BIN": str(WINE_UWP),
        "WIN11": str(windows_dir()),
        "WINE_UWP_WINDOWS_TREE": str(windows_dir()),
        "WINE_UWP_GAMES_DATA": str(WINE_UWP / "xbox-games"),
        "WINE_UWP_D3D_RUNTIME": str(d3d_runtime_dir()),
        "WINEPREFIX": str(prefix or prefix_dir()),
        "WINE_BUILD": str(binaries),
        "SHIM_DIR": str(share / "shims"),
        "PATH": os.pathsep.join([str(binaries)] + _python_path()
                                + [env.get("PATH") or "/usr/bin:/bin"]),
        "WINEDEBUG": debug or "-all,err+all",
    })
    return env


def _python_path():
    """A directory with ``python3`` in it when the host has none: wine-uwp's
    prefix setup is partly Python, and an AppImage carries its own."""
    if shutil.which("python3"):
        return []
    shim = WINE_UWP / "host-bin"
    shim.mkdir(parents=True, exist_ok=True)
    link = shim / "python3"
    if not link.is_symlink() or os.readlink(link) != sys.executable:
        link.unlink(missing_ok=True)
        link.symlink_to(sys.executable)
    return [str(shim)]


def _run(cmd, env, what, progress=None, cwd=None, log=None, timeout=None):
    """Run one of wine-uwp's steps, its "STAGE" lines said as they come.

    ``progress`` is called every half second so that the window's KILL
    stops it: it raises Cancelled, and the step is ended with its prefix.
    """
    log_handle = open(log, "a", encoding="utf-8") if log else None
    proc = subprocess.Popen(
        cmd, env=env, cwd=cwd, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, start_new_session=True)
    tail = []
    started = time.monotonic()
    pending = b""
    try:
        fd = proc.stdout.fileno()
        while True:
            ready, _, _ = select.select([fd], [], [], 0.5)
            chunk = os.read(fd, 65536) if ready else b""
            if chunk:
                pending += chunk
                *lines, pending = pending.split(b"\n")
                for raw in lines:
                    line = raw.decode("utf-8", "replace").rstrip("\r")
                    if log_handle:
                        log_handle.write(line + "\n")
                    tail = (tail + [line])[-40:]
                    if line.startswith("STAGE "):
                        info(line[6:].strip() + " …")
            elif ready or proc.poll() is not None:
                # The end of the output, or of the step while something it
                # started (wineserver) still holds the pipe open.
                break
            if progress:
                progress(0, 0)
            if timeout and time.monotonic() - started > timeout:
                raise UwpError(f"{what} did not finish within "
                               f"{timeout // 60} minutes.")
        proc.wait()
    except BaseException:
        _stop_group(proc, env)
        raise
    finally:
        proc.stdout.close()
        if log_handle:
            log_handle.close()
    if proc.returncode:
        detail = "\n".join(line for line in tail if line.strip())[-1500:]
        raise UwpError(f"{what} failed (exit {proc.returncode}).\n{detail}")
    return tail


def _stop_group(proc, env):
    try:
        os.killpg(proc.pid, 15)
    except OSError:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, 9)
        except OSError:
            pass
    stop_prefix(env.get("WINEPREFIX"))


def stop_prefix(prefix=None):
    """End every Wine process of a UWP prefix (``wineserver -k``)."""
    wineserver = _bin() / "wineserver"
    if not wineserver.is_file():
        return
    env = environment(prefix)
    try:
        subprocess.run([str(wineserver), "-k"], env=env, timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        pass


# ---------------------------------------------------------------- prefix


def prefix_ready(prefix=None):
    return (Path(prefix or prefix_dir()) / ".wine-uwp-ready").is_file()


def ensure_prefix(progress=None):
    """The profile's wine-uwp prefix, built from the Windows files by
    wine-uwp's new-prefix.sh the first time."""
    prefix = prefix_dir()
    if prefix_ready(prefix):
        return prefix
    from .prefix import prefix_processes
    if prefix_processes(prefix):
        raise UwpError("A Wine process still uses the UWP prefix. Close "
                       "Minecraft, then try again.")
    shutil.rmtree(prefix, ignore_errors=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    info("Building the Wine prefix for the UWP builds (a few minutes, once "
         "per profile) …")
    env = environment(prefix)
    try:
        _run(["bash", str(_share() / "packaging" / "new-prefix.sh"),
              str(windows_dir()), str(prefix)], env,
             "Building the UWP prefix", progress,
             log=LOGS / "wine-uwp-prefix.log", timeout=40 * 60)
    except BaseException:
        shutil.rmtree(prefix, ignore_errors=True)
        raise
    (prefix / ".wine-uwp-ready").touch()
    (prefix / ".bol-runtime").write_text(WINE_UWP_TAG + "\n")
    ok("UWP prefix ready")
    return prefix


def prepare(progress=None):
    """Everything a UWP build needs besides itself, made once."""
    _require_host()
    ensure_runtime(progress)
    ensure_windows_files(progress)
    ensure_d3d_runtime(progress)
    ensure_prefix(progress)


# --------------------------------------------------------------- install


def _free_space(path):
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def _package_url(update_id):
    from . import wustore
    try:
        urls = wustore.file_urls(update_id, 1)
    except Exception as exc:
        raise UwpError(f"Windows Update could not be asked for the package "
                       f"({exc}).") from exc
    # The package's own location carries the signed "P1=" parameter; the
    # other file of the update is its block map.
    for digest, url in urls.items():
        if "P1=" in url:
            return digest, url
    raise UwpError("Windows Update no longer serves this build of "
                   "Minecraft.")


def _sha1_b64(path):
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return base64.b64encode(digest.digest()).decode()


def _manifest_identity(folder):
    try:
        text = (Path(folder) / "AppxManifest.xml").read_text(errors="ignore")
    except OSError:
        return None
    match = re.search(r"<Identity\b[^>]*>", text, re.S)
    if not match:
        return None
    attrs = dict(re.findall(r'(\w+)="([^"]*)"', match.group(0)))
    return attrs


def _require_room(dest, version):
    """Refuse, before anything is fetched, when the build cannot fit."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    free = _free_space(dest.parent)
    first = not (prefix_ready() and windows_files_ready())
    needed = (3 << 30) + (_FIRST_INSTALL_ROOM if first else 0)
    if free is not None and free < needed:
        raise UwpError(
            f"Not enough free space to install Minecraft {version}: it needs "
            f"about {needed / (1 << 30):.0f} GiB and "
            f"{free / (1 << 30):.1f} GiB is free on {dest.parent}.")


def install(edition, entry, dest, progress=None):
    """Install one UWP build into ``dest`` (games/uwp/<version>/)."""
    dest = Path(dest)
    version = entry["version"]
    update_id = entry.get("update_id")
    if not update_id:
        raise UwpError(f"Minecraft {version} is not a UWP build Windows "
                       "Update knows.")
    _require_host()
    # Before anything is downloaded: these builds are for the account's own
    # copy, and nothing else would ask.
    require_ownership(edition)
    _require_room(dest, version)
    prepare(progress)
    ensure_store_engagement(progress)

    package = install_data() / f"Minecraft.Windows_{version}.appx"
    try:
        digest, url = _package_url(update_id)
        info(f"Downloading Minecraft {version} from Microsoft's Windows "
             "Update servers …")
        download(url, package, f"Minecraft {version}", progress)
        if _sha1_b64(package) != digest:
            raise UwpError(
                f"The downloaded Minecraft {version} does not match the "
                "package Windows Update published, so it was not installed. "
                "Try again.")
        _install_package(edition, package, version, dest, progress)
    finally:
        package.unlink(missing_ok=True)
        Path(str(package) + ".part").unlink(missing_ok=True)
    return dest


def _install_package(edition, package, version, dest, progress=None):
    """wineuwp-install ``package`` (a file it only reads), then move the
    build it makes into ``dest``, once it is seen to be Minecraft."""
    data = install_data()
    stage = data / "stage" / version
    try:
        info(f"Installing Minecraft {version} into wine-uwp …")
        env = environment()
        _run(["wine", "wineuwp-install", str(package), str(data),
              "--windows", str(windows_dir()), "--stage", version,
              "--category", edition["wu_category"]], env,
             f"Installing Minecraft {version}", progress,
             log=LOGS / "wine-uwp-install.log", timeout=40 * 60)
        identity = _manifest_identity(stage) or {}
        if identity.get("Name") != FAMILY.split("_")[0] \
                or not (stage / EXE).is_file():
            raise UwpError(f"wineuwp-install did not install Minecraft "
                           f"{version}.")
        stage_text = (stage / "identity.env").read_text(errors="ignore")
        if f"WINE_UWP_PACKAGE_FAMILY_NAME={FAMILY}" not in stage_text:
            raise UwpError("wineuwp-install installed another package than "
                           "Minecraft for Windows.")
        _replace_dir(stage, dest)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        for leftover in (stage.with_name(version + ".new"),
                         stage.with_name(version + ".old")):
            shutil.rmtree(leftover, ignore_errors=True)
    return dest


# ----------------------------------------------------- a package file


# What a player may have kept of a UWP build: the package Windows Update
# served (an .appx; .msix is the same format), or a bundle of them.
PACKAGE_SUFFIXES = (".appx", ".msix", ".appxbundle", ".msixbundle")
# The publisher the family name's "8wekyb3d8bbwe" is the hash of.
_PUBLISHER = ("CN=Microsoft Corporation, O=Microsoft Corporation, "
              "L=Redmond, S=Washington, C=US")
_MANIFEST_CAP = 4 << 20
_ENCRYPTED_MAGIC = (b"EXPH", b"EXBH")


def is_package_file(path):
    """Whether ``path`` is named as a UWP package or bundle."""
    return Path(path).suffix.lower() in PACKAGE_SUFFIXES


def build_of_package_version(version):
    """The build a package version is: Bedrock packs its patch and build
    into the third field, so 1.18.1201.0 is 1.18.12.1 and 1.18.2.0 is
    1.18.0.2, as mc-w10-versiondb names them."""
    parts = str(version).split(".")
    if len(parts) != 4 or not all(part.isdigit() for part in parts) \
            or parts[3] != "0":
        return str(version)
    major, minor, packed = int(parts[0]), int(parts[1]), int(parts[2])
    return f"{major}.{minor}.{packed // 100}.{packed % 100}"


def _xml_attrs(tag):
    return dict(re.findall(r'([\w:]+)\s*=\s*"([^"]*)"', tag))


def _zip_text(archive, name):
    """A small text member of a package, found whatever its case."""
    member = next((item for item in archive.infolist()
                   if item.filename.lower() == name.lower()), None)
    if member is None:
        return None
    if member.file_size > _MANIFEST_CAP:
        raise UwpError(f"The package's {name} is too large to be a "
                       "manifest.")
    with archive.open(member) as handle:
        text = handle.read(_MANIFEST_CAP).decode("utf-8", "replace")
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


def read_package(path):
    """What a package file holds, before anything is done with it:
    ``{"path", "name", "publisher", "version", "build", "arch", "bundle",
    "signed"}``, or UwpError saying why it cannot be installed.

    Only the manifest is read. A package that is not Microsoft's own
    signature -- one someone patched -- is still installed: the copy is the
    account's either way, which is what is checked (import_package).
    """
    path = Path(path).expanduser()
    try:
        with open(path, "rb") as handle:
            magic = handle.read(4)
    except OSError as exc:
        raise UwpError(f"The file could not be read ({exc}).") from exc
    if magic in _ENCRYPTED_MAGIC:
        raise UwpError(
            "This is an encrypted Store package. Import the .appx of the "
            "build instead, or install it from the list of versions.")
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise UwpError("This file is not an app package (.appx, .msix or "
                       "a bundle of them).") from exc
    with archive:
        bundle_text = _zip_text(archive, "AppxMetadata/AppxBundleManifest.xml")
        if bundle_text is not None:
            identity = re.search(r"<Identity\b[^>]*>", bundle_text, re.S)
            attrs = _xml_attrs(identity.group(0)) if identity else {}
            packages = [_xml_attrs(tag) for tag in
                        re.findall(r"<Package\b[^>]*>", bundle_text, re.S)]
            apps = [pkg for pkg in packages
                    if pkg.get("Type", "application").lower() == "application"]
            chosen = next((pkg for pkg in apps if pkg.get("Architecture", "")
                           .lower() in ("x64", "neutral")), None)
            arches = sorted({pkg.get("Architecture", "?") for pkg in apps})
            found = {"name": attrs.get("Name"),
                     "publisher": attrs.get("Publisher"),
                     "version": (chosen or {}).get("Version")
                     or attrs.get("Version"),
                     "arch": (chosen or {}).get("Architecture")
                     or "/".join(arches), "bundle": True}
        else:
            text = _zip_text(archive, "AppxManifest.xml")
            identity = re.search(r"<Identity\b[^>]*>", text or "", re.S)
            if identity is None:
                raise UwpError("This file has no app manifest: it is not "
                               "an app package.")
            attrs = _xml_attrs(identity.group(0))
            found = {"name": attrs.get("Name"),
                     "publisher": attrs.get("Publisher"),
                     "version": attrs.get("Version"),
                     "arch": attrs.get("ProcessorArchitecture", "neutral"),
                     "bundle": False}
        found["signed"] = any(item.filename == "AppxSignature.p7x"
                              for item in archive.infolist())
    if found["name"] != FAMILY.split("_")[0] \
            or found["publisher"] != _PUBLISHER:
        raise UwpError(f"This package is not Minecraft for Windows (it is "
                       f"{found['name'] or 'an unnamed package'}).")
    if not _VERSION.fullmatch(found["version"] or ""):
        raise UwpError("This package does not say which build of "
                       "Minecraft it is.")
    found["build"] = build_of_package_version(found["version"])
    if (found["arch"] or "").lower() not in ("x64", "neutral"):
        arch = (found["arch"] or "").lower()
        kind = ("the 32-bit (x86) build" if arch == "x86"
                else f"the {found['arch'] or 'unknown'} build")
        raise UwpError(
            f"This package is {kind} of Minecraft {found['build']}. "
            "wine-uwp runs only the 64-bit (x64) builds: import the x64 "
            f"package of {found['build']}, or install it from the list of "
            "versions.")
    found["path"] = path
    return found


def import_package(edition, package, dest, progress=None):
    """Install a package file the player has (read_package's answer): a
    build they downloaded or backed up themselves, patched or not.

    It is played with the account's own copy as any UWP build is: the Store
    is asked first, as it is before a download. The file is only read.
    """
    dest = Path(dest)
    version = package["build"]
    _require_host()
    require_ownership(edition)
    _require_room(dest, version)
    prepare(progress)
    ensure_store_engagement(progress)
    return _install_package(edition, Path(package["path"]).resolve(), version,
                            dest, progress)


# ---------------------------------------------------------------- launch


def launch_command(game_dir, debug=None):
    """``(argv, env, cwd)`` that start the build in ``game_dir`` as
    wine-uwp's run.sh does: wineuwp-launch, the program a desktop entry of
    an installed app runs, given the stage and the shared defaults."""
    game_dir = Path(game_dir)
    env = environment(debug=debug)
    argv = ["wine", "wineuwp-launch", "--stage", str(game_dir),
            "--defaults", str(_share() / "launch-defaults.env"),
            "--graphics", "x11"]
    return argv, env, str(game_dir)


def before_launch(env, progress=None):
    """A clean wineserver and the prefix's overrides checked (run.sh)."""
    stop_prefix(env.get("WINEPREFIX"))
    _run(["bash", str(_share() / "install-overrides.sh")], env,
         "Checking the UWP prefix", progress,
         log=LOGS / "wine-uwp-prefix.log", timeout=15 * 60)


def check_ready():
    """Why a UWP build cannot start here yet, or None."""
    if not (runtime_dir() / "bin" / "wine").is_file():
        return "wine-uwp is not installed yet."
    if not windows_files_ready():
        return "The Windows files wine-uwp needs are not there yet."
    if not prefix_ready():
        return "This profile has no UWP prefix yet."
    return None


__all__ = ["SOURCE", "UwpError", "is_uwp", "version_catalogue", "install",
           "prepare", "require_ownership", "launch_command", "before_launch",
           "check_ready", "stop_prefix", "prefix_dir", "is_package_file",
           "read_package", "import_package", "PACKAGE_SUFFIXES"]
