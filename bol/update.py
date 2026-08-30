"""bol.update — self-update of the launcher."""
# SPDX-License-Identifier: MIT

import glob
import hashlib
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from .config import CACHE, SELF_REPO, VERSION
from .log import BolError
from .platform import IS_MAC
from .util import asset_url, download, gh_latest
from .xdg_migration import is_flatpak

# The name the release's .deb and .rpm install the launcher under.
PACKAGE = "bedrock-on-linux"

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
    replace itself: 'appimage' | 'git' | 'deb' | 'rpm' | 'system' | 'file'."""
    if os.environ.get("APPIMAGE"):
        return "appimage"
    p = _self_path()
    package = _package_dir()
    # A dev checkout — leave updates to git. `python3 -m bol` runs
    # bol/__main__.py, so argv[0] is inside the package, not beside .git.
    if (p.parent / ".git").is_dir() or (package.parent / ".git").is_dir():
        return "git"
    if IS_MAC and ".app/Contents/" in str(p):
        # Inside an application bundle. Swapping the one file this process
        # runs from would leave the rest of the bundle -- Info.plist, the
        # icon, the frameworks, the code signature -- at the old version, and
        # a signed bundle whose contents changed under it will not launch
        # again. Replacing a bundle is a download-and-drag, so say that.
        return "system"
    # pip, or a distribution's build of the wheel (#306): argv[0] is the
    # console script, writable in a venv or ~/.local/bin, and swapping a .pyz
    # in for it would leave the installed package behind, still the old one.
    # Under `python3 -m bol` it is the package's own __main__.py.
    if (package.parent.name in ("site-packages", "dist-packages")
            or p.parent == package):
        return package_format() or "system"
    if str(p).startswith(("/usr/", "/app/", "/bin/")) or not os.access(p, os.W_OK):
        # Packaged or read-only. The release's own .deb and .rpm can be
        # updated from here, through the package manager that installed
        # them (#294); anything else is left to whoever packaged it.
        return package_format() or "system"
    return "file"                          # a plain user-writable script


def package_format(runner=None):
    """'deb' or 'rpm' when the release's package installed this launcher.

    Asked of the package database rather than guessed from the path: a
    distribution's own build lives under /usr too, and is not ours to
    replace. Never inside a Flatpak, whose /usr is the runtime's.
    """
    if is_flatpak():
        return None
    runner = runner or subprocess.run
    target = str(_package_dir() / "__init__.py")
    for kind, argv in (("deb", ["dpkg-query", "-S", target]),
                       ("rpm", ["rpm", "-qf", "--qf", "%{NAME}\\n", target])):
        if runner is subprocess.run and not shutil.which(argv[0]):
            continue
        try:
            result = runner(argv, capture_output=True, text=True,
                            errors="replace", timeout=15)
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode:
            continue
        owner = (result.stdout or "").strip().splitlines()
        owner = owner[0].split(":", 1)[0].strip() if owner else ""
        if owner == PACKAGE:
            return kind
    return None


def _expected_sha256(rel, name, progress=None):
    """The SHA-256 the release's checksum list gives `name`, or None."""
    url, sums_name, _ = asset_url(rel, lambda n: n.endswith("-SHA256SUMS"))
    if not url:
        return None
    sums = CACHE / "updates" / sums_name
    sums.unlink(missing_ok=True)
    download(url, sums, label=sums_name, progress=progress)
    try:
        listing = sums.read_text(errors="replace")
    finally:
        sums.unlink(missing_ok=True)
    for line in listing.splitlines():
        digest, _, listed = line.strip().partition("  ")
        if listed.lstrip("*") == name and re.fullmatch(r"[0-9a-f]{64}",
                                                        digest.lower()):
            return digest.lower()
    return None


def _fetch_checked(rel, url, name, target, progress=None):
    """Download release asset `name` to `target`; an error message, or None.

    Checked against the release's SHA-256 list when it has one, which every
    release since 2.2.8 does. The file is downloaded under a name carrying
    the version, so an interrupted download is only ever resumed with the
    bytes of the same release: resumed with another's, the result was half
    of each, and nothing checked an AppImage before it replaced the running
    one.
    """
    listed = asset_url(rel, lambda n: n.endswith("-SHA256SUMS"))[0]
    expected = _expected_sha256(rel, name, progress) if listed else None
    if listed and not expected:
        return (f"Release v{rel['version']} lists no checksum for {name}, so "
                f"it was not installed — download it from {rel['url']}")
    target.unlink(missing_ok=True)
    download(url, target, label=name, progress=progress)
    if expected:
        digest = hashlib.sha256()
        with open(target, "rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            target.unlink(missing_ok=True)
            return (f"The downloaded {name} does not match the release's "
                    "checksum, so it was not installed.")
    return None


def _staging(dest, rel):
    """Where the update to `dest` is downloaded: hidden, and per version.

    What earlier updates of `dest` left half-downloaded goes: a later
    release replaces them, and the unversioned name of older launchers is
    the one a resume could have mixed.
    """
    staged = dest.with_name(f".{dest.name}.{rel['version']}.new")
    leftovers = [dest.with_name(f"{dest.name}.new"),
                 dest.with_name(f"{dest.name}.new.part")]
    leftovers += dest.parent.glob(f".{glob.escape(dest.name)}.*.new.part")
    for leftover in leftovers:
        if leftover != staged.with_name(staged.name + ".part"):
            leftover.unlink(missing_ok=True)
    return staged


def _installer(kind, package):
    """The command that installs `package` as root, or None."""
    if not shutil.which("pkexec"):
        return None
    if kind == "deb":
        if shutil.which("apt-get"):
            return ["pkexec", "apt-get", "install", "-y", str(package)]
        return ["pkexec", "dpkg", "-i", str(package)]
    if shutil.which("dnf"):
        return ["pkexec", "dnf", "install", "-y", str(package)]
    if shutil.which("zypper"):
        # The release's .rpm is not signed; zypper refuses that unless told.
        return ["pkexec", "zypper", "--non-interactive", "install",
                "--allow-unsigned-rpm", str(package)]
    return ["pkexec", "rpm", "-U", str(package)]


def _manual_command(kind, package):
    if kind == "deb":
        return f"sudo apt install {package}"
    return f"sudo dnf install {package}"


def _package_update(rel, kind, progress=None):
    """Download the release's `kind` package, check it, and install it.

    The password prompt is the system's (pkexec), as for any package; the
    file it installs is the one the release's checksum list names.
    """
    suffix = "." + kind
    url, name, _ = asset_url(rel, lambda n: n.lower().endswith(suffix))
    if not url:
        return ("error", f"No {suffix} in release v{rel['version']} to update "
                         f"from — download it from {rel['url']}")
    if not asset_url(rel, lambda n: n.endswith("-SHA256SUMS"))[0]:
        # apt and dnf install what they are given, as root: never an
        # unchecked file.
        return ("error", f"Release v{rel['version']} has no checksum list, "
                         f"so {name} was not installed — download it from "
                         f"{rel['url']}")
    package = CACHE / "updates" / name
    package.parent.mkdir(parents=True, exist_ok=True)
    problem = _fetch_checked(rel, url, name, package, progress)
    if problem:
        return ("error", problem)
    argv = _installer(kind, package)
    if argv is None:
        return ("system", f"Downloaded v{rel['version']} to {package}. "
                          f"Install it with: {_manual_command(kind, package)}")
    try:
        result = subprocess.run(argv, capture_output=True, text=True,
                                errors="replace", timeout=1800)
    except (OSError, subprocess.SubprocessError) as exc:
        return ("error", f"Could not start the package manager ({exc}). "
                         f"Install {package} with: "
                         f"{_manual_command(kind, package)}")
    if result.returncode in (126, 127) and argv[0] == "pkexec":
        # pkexec: the prompt was dismissed, or no agent could show one.
        return ("error", "The update was downloaded but not installed: the "
                         "password prompt was cancelled. Install it with: "
                         f"{_manual_command(kind, package)}")
    if result.returncode:
        said = [line for line in (result.stderr or result.stdout or "")
                .splitlines() if line.strip()]
        return ("error", f"The package manager could not install v"
                         f"{rel['version']} (exit {result.returncode}"
                         + (f": {said[-1].strip()}" if said else "")
                         + f"). The package is in {package}.")
    package.unlink(missing_ok=True)
    return ("ok", f"Updated to v{rel['version']} — restart to use it.")


def self_update(rel, progress=None):
    """Replace the running launcher with release `rel`. Returns (state, msg);
    state is 'ok' | 'git' | 'system' | 'error'. Never raises — callers just
    show the message."""
    kind = update_kind()
    try:
        if kind == "git":
            return ("git", "This is a git checkout — run `git pull` to update.")
        if kind in ("deb", "rpm"):
            return _package_update(rel, kind, progress)
        if kind == "system":
            if IS_MAC and ".app/Contents/" in str(_self_path()):
                return ("system",
                        f"Download v{rel['version']} from {rel['url']} and "
                        "drag it into Applications, replacing this one — an "
                        "application bundle updates by being replaced whole.")
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
            tmp = _staging(dest, rel)
            problem = _fetch_checked(rel, url, name, tmp, progress)
            if problem:
                return ("error", problem)
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
        tmp = _staging(target, rel)
        problem = _fetch_checked(rel, url, name, tmp, progress)
        if problem:
            return ("error", problem)
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
