"""bol.update — self-update of the launcher."""
# SPDX-License-Identifier: MIT

import os
import re
import sys
import zipfile
from pathlib import Path

from .config import SELF_REPO, VERSION
from .log import BolError
from .platform import IS_MAC
from .util import asset_url, download, gh_latest

def _ver_tuple(s):
    """Parse 'v1.2.3' / '1.2.3' into a comparable tuple; non-numeric parts → 0."""
    out = []
    for part in (s or "").lstrip("vV").strip().split("."):
        m = re.match(r"\d+", part)
        out.append(int(m.group()) if m else 0)
    return tuple(out) or (0,)


def check_for_update():
    """Return a dict describing a NEWER release of the launcher itself, or
    None (already current, or any network/parse error — a background update
    check must never get in the way of using the app)."""
    try:
        rel = gh_latest(SELF_REPO)
    except Exception:
        return None
    tag = rel.get("tag_name") or ""
    if not tag or _ver_tuple(tag) <= _ver_tuple(VERSION):
        return None
    return {"version": tag.lstrip("vV"), "tag": tag,
            "url": rel.get("html_url", ""),
            "notes": (rel.get("body") or "").strip(),
            "assets": rel.get("assets", [])}


def _self_path():
    """Real path of the running launcher file (resolves the ~/.local/bin
    symlink that install.sh creates)."""
    return Path(os.path.realpath(sys.argv[0] or __file__))


def update_kind():
    """How the launcher is installed — decides how (and whether) it can
    replace itself: 'appimage' | 'git' | 'system' | 'file'."""
    if os.environ.get("APPIMAGE"):
        return "appimage"
    p = _self_path()
    if (p.parent / ".git").is_dir():       # dev checkout — leave updates to git
        return "git"
    if IS_MAC and ".app/Contents/" in str(p):
        # Inside an application bundle. Swapping the one file this process
        # runs from would leave the rest of the bundle -- Info.plist, the
        # icon, the frameworks, the code signature -- at the old version, and
        # a signed bundle whose contents changed under it will not launch
        # again. Replacing a bundle is a download-and-drag, so say that.
        return "system"
    if str(p).startswith(("/usr/", "/app/", "/bin/")) or not os.access(p, os.W_OK):
        return "system"                    # packaged / read-only install
    return "file"                          # a plain user-writable script


def self_update(rel, progress=None):
    """Replace the running launcher with release `rel`. Returns (state, msg);
    state is 'ok' | 'git' | 'system' | 'error'. Never raises — callers just
    show the message."""
    kind = update_kind()
    try:
        if kind == "git":
            return ("git", "This is a git checkout — run `git pull` to update.")
        if kind == "system":
            if IS_MAC and ".app/Contents/" in str(_self_path()):
                return ("system",
                        f"Download v{rel['version']} from {rel['url']} and "
                        "drag it into Applications, replacing this one — an "
                        "application bundle updates by being replaced whole.")
            return ("system",
                    f"Installed from a package — update with your package "
                    f"manager, or download v{rel['version']} from {rel['url']}")
        if kind == "appimage":
            dest = Path(os.environ["APPIMAGE"])
            url, name, _ = asset_url(rel,
                                     lambda n: n.lower().endswith(".appimage"))
            if not url:
                return ("error", "No AppImage in the release to update from.")
            tmp = dest.with_name(dest.name + ".new")
            download(url, tmp, label=name, progress=progress)
            os.chmod(tmp, 0o755)
            tmp.replace(dest)
            return ("ok", f"Updated to v{rel['version']} — restart to use it.")
        # 'file': a portable single-file zipapp (.pyz) — swap it for the
        # release's .pyz asset (the bol/ package is bundled inside it).
        target = _self_path()
        url, name, _ = asset_url(rel, lambda n: n.lower().endswith(".pyz"))
        if not url:
            return ("error",
                    f"No .pyz in release v{rel['version']} to update from — "
                    f"download it from {rel['url']}")
        tmp = target.with_name(target.name + ".new")
        download(url, tmp, label=name, progress=progress)
        if not zipfile.is_zipfile(tmp):       # a .pyz is a shebang + zip
            tmp.unlink(missing_ok=True)
            return ("error",
                    "The downloaded update looked wrong — kept the current "
                    "version.")
        os.chmod(tmp, target.stat().st_mode)
        tmp.replace(target)
        return ("ok", f"Updated to v{rel['version']} — restart to use it.")
    except BolError as e:
        return ("error", str(e))
    except Exception as e:
        return ("error", f"Update failed: {e}")
