"""bol.update — self-update of the launcher."""
# SPDX-License-Identifier: MIT

import os
import re
import sys
import zipfile
from pathlib import Path

from .config import SELF_REPO, VERSION
from .log import BolError
from .util import asset_url, download, gh_latest
from .xdg_migration import is_flatpak

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


def _package_dir():
    """Where the running bol package is (a path inside the .pyz for one)."""
    return Path(__file__).resolve().parent


def _self_path():
    """Real path of the running launcher file (resolves the ~/.local/bin
    symlink that install.sh creates)."""
    return Path(os.path.realpath(sys.argv[0] or __file__))


def flatpak_installation(info_path=Path("/.flatpak-info")):
    """'user' or 'system': the Flatpak installation this sandbox runs from.

    None outside Flatpak, and for a custom installation, whose name cannot be
    told from inside. Read from the app-path Flatpak records for the running
    instance, which is where it deployed the app.
    """
    try:
        text = Path(info_path).read_text(errors="replace")
    except OSError:
        return None
    match = re.search(r"^app-path=(.+)$", text, re.MULTILINE)
    if not match:
        return None
    path = match.group(1).strip()
    if path.startswith("/var/lib/flatpak/"):
        return "system"
    if "/.local/share/flatpak/" in path:
        return "user"
    return None


def _flatpak_update_message(rel, installation):
    """How to install release `rel` over the Flatpak that is running.

    A bundle is installed with the same flag as the copy it replaces. Without
    --user, flatpak installs a second, system-wide copy beside a per-user
    one, and every launch keeps starting the per-user one: it comes first
    (#315).
    """
    _url, name, _size = asset_url(rel,
                                  lambda n: n.lower().endswith(".flatpak"))
    bundle = name or f"BedrockOnLinux-{rel['version']}-x86_64.flatpak"
    flag = {"user": "--user ", "system": "--system "}.get(installation, "")
    msg = (f"Installed as a Flatpak — download {bundle} from {rel['url']} "
           f"and install it with: flatpak install {flag}./{bundle}")
    if installation == "user":
        msg += (" (keep --user: without it Flatpak adds a second, system-wide"
                " copy, and this one keeps starting instead)")
    return msg


def update_kind():
    """How the launcher is installed — decides how (and whether) it can
    replace itself: 'appimage' | 'git' | 'system' | 'file'."""
    if os.environ.get("APPIMAGE"):
        return "appimage"
    p = _self_path()
    package = _package_dir()
    # A dev checkout — leave updates to git. `python3 -m bol` runs
    # bol/__main__.py, so argv[0] is inside the package, not beside .git.
    if (p.parent / ".git").is_dir() or (package.parent / ".git").is_dir():
        return "git"
    # pip, or a distribution's build of the wheel (#306): argv[0] is the
    # console script, writable in a venv or ~/.local/bin, and swapping a .pyz
    # in for it would leave the installed package behind, still the old one.
    # Under `python3 -m bol` it is the package's own __main__.py.
    if (package.parent.name in ("site-packages", "dist-packages")
            or p.parent == package):
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
            if is_flatpak():
                return ("system", _flatpak_update_message(
                    rel, flatpak_installation()))
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
