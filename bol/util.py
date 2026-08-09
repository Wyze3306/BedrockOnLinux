"""bol.util — small shared helpers: run, settings, HTTP, downloads, GitHub, screen/proc."""
# SPDX-License-Identifier: MIT

import http.client
import fcntl
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .config import (
    APP,
    CACHE,
    DATA,
    FLATPAK_APP_ID,
    GAMES,
    LOGS,
    PROTON_DIR,
    SETTINGS,
    UMU_DIR,
)
from .log import IS_TTY, die, warn

_XDG_MIGRATION_CHECKED = False


def _ensure_xdg_storage():
    """Run the guarded legacy XDG migration before reading user state."""
    global _XDG_MIGRATION_CHECKED
    if _XDG_MIGRATION_CHECKED:
        return
    try:
        from .xdg_migration import migrate_legacy_flatpak_data
        migrate_legacy_flatpak_data()
    except Exception as exc:
        die(
            "Could not migrate the legacy data safely. The original "
            f"files were retained; free disk space/check permissions ({exc})."
        )
    _XDG_MIGRATION_CHECKED = True

def run(cmd, **kw):
    kw.setdefault("check", True)
    return subprocess.run(cmd, **kw)


def mkdirs():
    _ensure_xdg_storage()
    for d in (DATA, PROTON_DIR, UMU_DIR, CACHE, LOGS, GAMES):
        d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(DATA, 0o700)
    except OSError:
        pass


def load_settings():
    _ensure_xdg_storage()
    s = {}
    if SETTINGS.exists():
        try:
            s = json.loads(SETTINGS.read_text())
        except Exception:
            s = {}
    if not s.get("proton_dir") and not s.get("proton_url"):
        s.setdefault("proton_source", "winegdk")
    return s


def save_settings(s):
    _ensure_xdg_storage()
    DATA.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(DATA, 0o700)
    except OSError:
        pass
    lock_path = DATA / ".settings.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    staged = None
    try:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        fd, name = tempfile.mkstemp(prefix=".settings-", suffix=".tmp",
                                    dir=DATA)
        staged = Path(name)
        with os.fdopen(fd, "w") as stream:
            json.dump(s, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(staged, 0o600)
        os.replace(staged, SETTINGS)
        staged = None
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


# Compatibility variables the launcher configures itself from Settings. The
# Advanced custom-environment field is applied last and deliberately keeps the
# final word over them, but overriding one silently replaces a supported
# setting: a bad value then crashes the game at every launch with nothing
# naming the cause. Issue #134 shows what that costs — the reporter wiped the
# whole installation three times chasing it.
LAUNCHER_OWNED_ENV = (
    "PROTON_ENABLE_WAYLAND",
    "WINE_DISABLE_VULKAN_OPWR",
    "PROTON_PREFER_SDL",
    "PROTON_DISABLE_HIDRAW",
    "PROTON_NO_STEAMINPUT",
    "PROTON_NO_WM_DECORATION",
    "PROTON_USE_WINED3D",
    "PROTON_LOG",
    "PROTON_LOG_DIR",
    # Wine 11 has no esync/fsync, so ntsync is the only fast synchronization
    # path left. A stale global export of this must not silently serialise
    # every Minecraft worker thread behind the wineserver; the Advanced
    # custom-environment field remains the supported way to turn it off.
    "PROTON_NO_NTSYNC",
)

# The Settings control which configures a launcher-owned variable properly,
# for the ones a user is likely to reach for by hand.
LAUNCHER_OWNED_ENV_ALTERNATIVE = {
    "PROTON_USE_WINED3D": "the Legacy compatibility renderer in Settings",
    "PROTON_LOG": "Advanced diagnostics in Settings",
    "PROTON_LOG_DIR": "Advanced diagnostics in Settings",
}


def _custom_env_pairs(custom_env, quiet=False):
    """KEY=VALUE pairs declared by a custom-environment string."""
    if not custom_env or not str(custom_env).strip():
        return []
    try:
        tokens = shlex.split(str(custom_env).strip())
    except ValueError as e:
        if not quiet:
            warn(f"Custom environment variables ignored — invalid syntax "
                 f"({e}). Check for a missing closing quote.")
        return []
    pairs = []
    for token in tokens:
        if "=" not in token:
            continue
        key, _, value = token.partition("=")
        key = key.strip()
        if key:
            pairs.append((key, value))
    return pairs


def custom_env_keys(custom_env):
    """Variable names the custom-environment field would set.

    Inspection only — never reports a syntax error, so that reading the field
    to explain a crash cannot double up the warning ``apply_custom_env``
    already emits when it applies the same string.
    """
    return [key for key, _ in _custom_env_pairs(custom_env, quiet=True)]


def custom_env_map(custom_env):
    """The KEY=VALUE pairs the custom-environment field would set.

    Inspection only, like ``custom_env_keys``: never reports a syntax error,
    so reading the field to explain a performance or crash symptom cannot
    double up the warning ``apply_custom_env`` already emits.
    """
    return dict(_custom_env_pairs(custom_env, quiet=True))


def launcher_owned_overrides(custom_env):
    """Launcher-owned variables the custom-environment field overrides."""
    found = []
    for key in custom_env_keys(custom_env):
        if key in LAUNCHER_OWNED_ENV and key not in found:
            found.append(key)
    return found


def apply_custom_env(env, custom_env):
    """Merge KEY=VALUE tokens from a space-separated string into env."""
    for key, value in _custom_env_pairs(custom_env):
        env[key] = value


def launcher_command(*arguments, environ=None, argv=None,
                     info_path=Path("/.flatpak-info"), which=None):
    """Return a command line the user can actually run for this installation.

    ``bedrock-on-linux`` only exists on PATH for the distribution packages. An
    AppImage, a portable zipapp and the Flatpak all have to be invoked through
    their own entry point, so printing the bare program name told those users
    to run a command their shell cannot find (issue #136).
    """
    source = os.environ if environ is None else environ
    args = " ".join(shlex.quote(str(item)) for item in arguments)

    def command(*parts):
        line = " ".join(parts)
        return f"{line} {args}".rstrip()

    if source.get("FLATPAK_ID") or Path(info_path).is_file():
        app_id = str(source.get("FLATPAK_ID") or "").strip() or FLATPAK_APP_ID
        return command("flatpak", "run", shlex.quote(app_id))

    # AppImage mounts itself at a temporary path; APPIMAGE is the real file.
    appimage = str(source.get("APPIMAGE") or "").strip()
    if appimage:
        candidate = Path(appimage).expanduser()
        if candidate.is_file():
            return command(shlex.quote(str(candidate)))

    installed = (which or shutil.which)(APP)
    if installed:
        return command(APP)

    # Portable zipapps and an uninstalled checkout run from an explicit path.
    entry = ((argv if argv is not None else sys.argv) or [""])[0]
    try:
        launcher = Path(entry).expanduser().resolve()
    except (OSError, RuntimeError):
        return command(APP)
    if entry and launcher.is_file() and launcher.suffix != ".py":
        return command(shlex.quote(str(launcher)))
    return command(APP)


def http_json(url, timeout=10):
    # Never forward ambient credentials to these public endpoints.
    headers = {"User-Agent": APP, "Accept": "application/vnd.github+json"}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def http_post_form(url, fields):
    """POST application/x-www-form-urlencoded → parsed JSON. OAuth endpoints
    return their error payload with a 4xx, so decode the body either way."""
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"User-Agent": APP, "Accept": "application/json",
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            raise


# Retry and resume large downloads after transient network failures.
_RETRYABLE = (urllib.error.URLError, TimeoutError, socket.timeout,
              ConnectionError, http.client.IncompleteRead,
              http.client.HTTPException)


def download(url, dest: Path, label=None, progress=None, attempts=5):
    label = label or dest.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    last_err = None
    for attempt in range(1, attempts + 1):
        have = tmp.stat().st_size if tmp.exists() else 0
        headers = {"User-Agent": APP}
        if have:
            headers["Range"] = f"bytes={have}-"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                resuming = have > 0 and getattr(r, "status", 200) == 206
                if have and not resuming:
                    have = 0
                if resuming:
                    cr = r.headers.get("Content-Range", "")
                    total = int(cr.rsplit("/", 1)[-1]) if "/" in cr else 0
                else:
                    total = int(r.headers.get("Content-Length", 0))
                got = last = have
                with open(tmp, "ab" if resuming else "wb") as f:
                    while True:
                        chunk = r.read(1 << 16)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        if progress and total:
                            progress(got, total)
                        if IS_TTY and total and got - last > (1 << 21):
                            last = got
                            print(f"\r:: {label}: {got*100//total:3d}% "
                                  f"({got>>20}/{total>>20} MiB)", end="", flush=True)
                if total and got < total:
                    raise http.client.IncompleteRead(b"", total - got)
            if IS_TTY:
                print()
            tmp.replace(dest)
            return dest
        except urllib.error.HTTPError as e:
            if e.code == 416 and tmp.exists():
                tmp.unlink(missing_ok=True)
                last_err = e
            elif e.code < 500:
                die(f"Download failed: {url}\n{e}")
            else:
                last_err = e
        except _RETRYABLE as e:
            last_err = e
        if attempt < attempts:
            wait = min(2 ** attempt, 15)
            warn(f"{label}: connection dropped ({last_err}); resuming in "
                 f"{wait}s [{attempt}/{attempts - 1}] …")
            time.sleep(wait)
    die(f"Download failed after {attempts} attempts: {url}\n{last_err}")


def _fetch_with_fallback(cache_file, url, ttl=3600):
    cache_path = CACHE / cache_file
    if cache_path.exists():
        try:
            mtime = cache_path.stat().st_mtime
            if time.time() - mtime < ttl:
                with open(cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass

    try:
        data = http_json(url)
        if data:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            except Exception:
                pass
            return data
    except Exception:
        if cache_path.exists():
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        raise


def gh_latest(repo):
    cache_file = f"releases_latest_{repo.replace('/', '_')}.json"
    return _fetch_with_fallback(cache_file, f"https://api.github.com/repos/{repo}/releases/latest")


def gh_releases(repo, per_page=100, fetch_all=False, ignore_cache=False):
    cache_file = f"releases_{repo.replace('/', '_')}_{'all' if fetch_all else per_page}.json"
    cache_path = CACHE / cache_file
    
    if cache_path.exists() and not ignore_cache:
        try:
            if time.time() - cache_path.stat().st_mtime < 43200:
                with open(cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass

    try:
        if not fetch_all:
            data = http_json(f"https://api.github.com/repos/{repo}/releases?per_page={per_page}")
        else:
            data = []
            page = 1
            while True:
                chunk = http_json(f"https://api.github.com/repos/{repo}/releases?per_page=100&page={page}")
                if not chunk: break
                data.extend(chunk)
                if len(chunk) < 100: break
                page += 1
                
        if data:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            except Exception:
                pass
            return data
    except Exception:
        if cache_path.exists():
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        raise
    return []


def mc_releases(fetch_all=True, ignore_cache=False):
    beta = load_settings().get("show_betas", False)
    
    def fetch_section(section):
        cache_file = f"releases_mc_{section}_{'all' if fetch_all else '100'}.json"
        cache_path = CACHE / cache_file
        
        if cache_path.exists() and not ignore_cache:
            try:
                if time.time() - cache_path.stat().st_mtime < 43200:
                    with open(cache_path, "r", encoding="utf-8") as f:
                        return json.load(f)
            except Exception:
                pass
                
        try:
            articles = []
            url = f"https://feedback.minecraft.net/api/v2/help_center/en-us/sections/{section}/articles.json?per_page=100"
            while url:
                data = http_json(url)
                if not data or "articles" not in data:
                    break
                articles.extend(data["articles"])
                if not fetch_all:
                    break
                url = data.get("next_page")
                
            res = {"articles": articles}
            if articles:
                try:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(cache_path, "w", encoding="utf-8") as f:
                        json.dump(res, f, indent=2)
                except Exception:
                    pass
                return res
        except Exception:
            if cache_path.exists():
                try:
                    with open(cache_path, "r", encoding="utf-8") as f:
                        return json.load(f)
                except Exception:
                    pass
            raise
        return {"articles": []}

    import re
    def extract_versions(title):
        matches = re.findall(r"(\d+)\.(\d+)(?:\.(\d+))?(?:\.(\d+))?", title)
        res = []
        for m in matches:
            parts = [int(x) if x else 0 for x in m]
            if parts[0] > 10:
                parts = [1, parts[0], parts[1], parts[2]]
            res.append(tuple(parts))
        return res

    official = fetch_section("360001186971")["articles"]
    articles = []
    
    for a in official:
        vs = extract_versions(a.get("title", ""))
        if not vs or any(v >= (1, 21, 120, 0) for v in vs):
            articles.append(a)
    
    if beta:
        betas = fetch_section("360001185332")["articles"]
        for a in betas:
            vs = extract_versions(a.get("title", ""))
            if not vs or any(v >= (1, 21, 120, 21) for v in vs):
                articles.append(a)
        
    articles.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return {"articles": articles}


def asset_url(release, predicate):
    for a in release.get("assets", []):
        if predicate(a["name"]):
            return a["browser_download_url"], a["name"], a.get("size", 0)
    return None, None, 0


def format_display_version(text, is_beta=False):
    import re
    def repl(m):
        prefix = m.group(1)
        major = int(m.group(2))
        rest = m.group(3)
        if not is_beta:
            parts = rest.split('.')
            if len(parts) == 3:
                rest = '.' + parts[1]
        if major >= 22:
            return str(major) + rest
        return prefix + str(major) + rest
    return re.sub(r"(?<!\d)(v?1\.)(\d+)(\.\d+(?:\.\d+)?)", repl, text)


def _screen_wh(runner=None):
    """Primary screen WxH (for gamescope/Wine desktop sizing), or None. See
    bol.x11.primary_output_size for how the primary monitor is found."""
    from .x11 import primary_output_size
    return primary_output_size(runner)
