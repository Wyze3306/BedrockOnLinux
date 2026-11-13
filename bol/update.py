"""bol.update — self-update of the launcher.

One path for every way the launcher is installed. update_kind() names the
install; PLANS says what an update is for it:

  kind      asset      how
  appimage  .AppImage  swap the file (delta through appimageupdatetool)
  file      .pyz       swap the file
  deb       .deb       hand the checked package to apt/dpkg through pkexec
  rpm       .rpm       hand the checked package to dnf/zypper/rpm
  flatpak, nix, git, system    not ours to replace: say who updates it

Whatever the kind, the same three things hold:

* the release comes from the signed data feed when it names one (bol.feed),
  else from GitHub's API, and bol.netpolicy decides whether anything is
  asked at all;
* nothing is installed unless its SHA-256 matches -- the feed's signed
  digest, else the release's own checksum list -- and a release with
  neither is refused;
* the new launcher is run once (``--version``) before it replaces anything,
  and a swapped file keeps its predecessor so bol.rollback can put it back
  if the new one never comes up.
"""
# SPDX-License-Identifier: MIT

import glob
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections import namedtuple
from pathlib import Path

from . import rollback
from .config import CACHE, PRETTY, SELF_REPO, VERSION
from .log import BolError
from .util import asset_url, download, gh_latest, gh_releases
from .xdg_migration import is_flatpak

# The name the release's .deb and .rpm install the launcher under.
PACKAGE = "bedrock-on-linux"

# How each install kind updates. `how` is "swap" (replace the running file),
# "package" (the system's package manager installs it) or "handoff" (someone
# else owns the install).
Plan = namedtuple("Plan", "how suffix")
PLANS = {
    "appimage": Plan("swap", ".appimage"),
    "file": Plan("swap", ".pyz"),
    "deb": Plan("package", ".deb"),
    "rpm": Plan("package", ".rpm"),
    "flatpak": Plan("handoff", ".flatpak"),
    "nix": Plan("handoff", None),
    "git": Plan("handoff", None),
    "system": Plan("handoff", None),
}


def _ver_tuple(s):
    """Parse 'v1.2.3' / '1.2.3' into a comparable tuple; non-numeric parts → 0."""
    out = []
    for part in (s or "").lstrip("vV").strip().split("."):
        m = re.match(r"\d+", part)
        out.append(int(m.group()) if m else 0)
    return tuple(out) or (0,)


def _from_github(rel):
    tag = rel.get("tag_name") or ""
    return {"version": tag.lstrip("vV"), "tag": tag,
            "url": rel.get("html_url", ""),
            "notes": (rel.get("body") or "").strip(),
            "source": "github",
            "assets": rel.get("assets", [])}


def _github_release(channel):
    """The newest release GitHub lists for `channel`, or None."""
    if channel == "prerelease":
        # Versioned pre-releases only: the rolling "nightly" tag is not one.
        releases = [r for r in gh_releases(SELF_REPO, 30) or []
                    if not r.get("draft")
                    and re.fullmatch(r"v\d+(\.\d+)+", r.get("tag_name") or "")]
        if not releases:
            return None
        return _from_github(max(
            releases, key=lambda r: _ver_tuple(r["tag_name"])))
    rel = gh_latest(SELF_REPO)
    return _from_github(rel) if rel and rel.get("tag_name") else None


def check_for_update(channel=None):
    """Return a dict describing a NEWER release of the launcher itself, or
    None (already current, or any network/parse error — a background update
    check must never get in the way of using the app).

    The signed data feed answers when it names a newer release, with a
    digest for every asset. Otherwise GitHub is asked, and its answer still
    takes the feed's digests when the feed knows that version.
    """
    from . import feed, netpolicy
    try:
        channel = channel or netpolicy.channel()
    except Exception:
        channel = "stable"
    signed = None
    try:
        signed = feed.launcher_release(channel)
    except Exception:
        signed = None
    if signed and _ver_tuple(signed["version"]) > _ver_tuple(VERSION):
        return signed
    try:
        rel = _github_release(channel)
    except Exception:
        return None
    if not rel or not rel["tag"] \
            or _ver_tuple(rel["tag"]) <= _ver_tuple(VERSION):
        return None
    return rel


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
    replace itself: one of the keys of PLANS."""
    if os.environ.get("APPIMAGE"):
        return "appimage"
    if is_flatpak():
        return "flatpak"
    p = _self_path()
    package = _package_dir()
    if str(package).startswith("/nix/store/") \
            or str(p).startswith("/nix/store/"):
        return "nix"
    # A dev checkout — leave updates to git. `python3 -m bol` runs
    # bol/__main__.py, so argv[0] is inside the package, not beside .git.
    if (p.parent / ".git").exists() or (package.parent / ".git").exists():
        return "git"
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


def _asset(rel, suffix):
    """(url, name, signed sha256 or None) of the asset ending in `suffix`."""
    for asset in rel.get("assets", []):
        if asset["name"].lower().endswith(suffix):
            return (asset["browser_download_url"], asset["name"],
                    asset.get("sha256"))
    return None, None, None


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _expected_sha256(rel, name, progress=None):
    """The SHA-256 `name` must have, or None when nothing states one.

    The signed data feed's digest when the release came from it; else the
    one in the release's checksum list, which every release since 2.2.8 has.
    """
    for asset in rel.get("assets", []):
        if asset["name"] == name and re.fullmatch(
                r"[0-9a-f]{64}", str(asset.get("sha256") or "")):
            return asset["sha256"]
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


def _fetch_checked(rel, url, name, target, progress=None, delta=None):
    """Download release asset `name` to `target`; an error message, or None.

    Nothing unchecked is ever left at `target`: a release that states no
    SHA-256 for the asset is refused, and so are bytes that do not match.
    The file is downloaded under a name carrying the version, so an
    interrupted download is only ever resumed with the bytes of the same
    release. `delta` may produce `target` from the installed file instead of
    downloading it whole; what it produces is checked like a download, and
    discarded for a full one when it does not match.
    """
    expected = _expected_sha256(rel, name, progress)
    if not expected:
        return (f"Release v{rel['version']} lists no checksum for {name}, so "
                f"it was not installed — download it from {rel['url']}")
    target.unlink(missing_ok=True)
    if delta is not None:
        try:
            if delta(target) and _sha256(target) == expected:
                return None
        except Exception:
            pass
        target.unlink(missing_ok=True)
    download(url, target, label=name, progress=progress)
    if _sha256(target) != expected:
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


def _appimage_delta(dest, name):
    """A delta download of AppImage `name` from the installed `dest`.

    appimageupdatetool reads the update information the AppImage carries
    and fetches only the blocks that changed (the .zsync beside every
    release, #191). It is used when the host has it; the result is one more
    candidate for the SHA-256 check, never trusted for having come from it.
    Returns a callable for _fetch_checked, or None without the tool.
    """
    tool = shutil.which("appimageupdatetool")
    if not tool:
        return None

    def run(target):
        with tempfile.TemporaryDirectory(
                prefix=".bol-delta-", dir=str(dest.parent)) as work:
            result = subprocess.run(
                [tool, str(dest)], cwd=work, capture_output=True, text=True,
                errors="replace", timeout=900)
            produced = Path(work) / name
            if result.returncode or not produced.is_file():
                return False
            os.replace(produced, target)
            return True
    return run


def _probe(kind, path, version, runner=None):
    """Start the staged launcher once; an error message, or None.

    `--version` imports the launcher and prints what it is, without a
    window, a prompt or a connection. A file that cannot do that here -- a
    truncated image, an interpreter it does not support, a FUSE it cannot
    mount -- is not swapped in.
    """
    runner = runner or subprocess.run
    argv = [str(path), "--version"] if kind == "appimage" \
        else [sys.executable, str(path), "--version"]
    env = dict(os.environ, **{rollback.PROBE_ENV: "1"})
    try:
        result = runner(argv, capture_output=True, text=True,
                        errors="replace", timeout=120, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"it could not be started ({exc})"
    said = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode:
        last = said.splitlines()[-1].strip() if said else "no output"
        return f"it exited with {result.returncode} ({last})"
    if version not in said:
        return f"it does not report version {version}"
    return None


def _keep_previous(dest):
    """Keep the file about to be replaced, for bol.rollback."""
    backup = rollback.backup_path(dest)
    backup.unlink(missing_ok=True)
    try:
        os.link(dest, backup)           # no copy: the swap leaves it behind
    except OSError:
        shutil.copy2(dest, backup)
    return backup


def _swap_update(rel, kind, progress=None):
    """Replace the running AppImage or .pyz with the release's."""
    plan = PLANS[kind]
    dest = Path(os.environ["APPIMAGE"]) if kind == "appimage" \
        else _self_path()
    url, name, _ = _asset(rel, plan.suffix)
    if not url:
        return ("error", f"No {plan.suffix.lstrip('.')} in release "
                         f"v{rel['version']} to update from — download it "
                         f"from {rel['url']}")
    staged = _staging(dest, rel)
    delta = _appimage_delta(dest, name) if kind == "appimage" else None
    problem = _fetch_checked(rel, url, name, staged, progress, delta)
    if problem:
        return ("error", problem)
    if kind == "file" and not zipfile.is_zipfile(staged):
        staged.unlink(missing_ok=True)      # a .pyz is a shebang + zip
        return ("error", "The downloaded update looked wrong — kept the "
                         "current version.")
    os.chmod(staged, 0o755 if kind == "appimage" else dest.stat().st_mode)
    problem = _probe(kind, staged, rel["version"])
    if problem:
        staged.unlink(missing_ok=True)
        return ("error", f"v{rel['version']} was downloaded and checked, but "
                         f"{problem} on this system — kept v{VERSION}.")
    try:
        _keep_previous(dest)
    except OSError:
        pass                # no predecessor kept: still a checked update
    staged.replace(dest)
    rollback.arm(dest, VERSION, rel["version"])
    return ("ok", f"Updated to v{rel['version']} — restart to use it.")


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


def _installed_starts(version):
    """Whether the launcher the package manager just installed starts.

    None when it does, else what went wrong. The package manager keeps no
    previous version to swap back, so this cannot undo the install; it can
    say so at once, with the way back, instead of at the next start.
    """
    command = shutil.which(PACKAGE)
    if not command:
        return None                     # nothing to ask: do not guess
    env = dict(os.environ, **{rollback.PROBE_ENV: "1"})
    try:
        result = subprocess.run([command, "--version"], capture_output=True,
                                text=True, errors="replace", timeout=120,
                                env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"it could not be started ({exc})"
    said = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode or version not in said:
        last = said.splitlines()[-1].strip() if said else "no output"
        return f"it does not start (exit {result.returncode}: {last})"
    return None


def _package_update(rel, kind, progress=None):
    """Download the release's `kind` package, check it, and install it.

    The password prompt is the system's (pkexec), as for any package; the
    file it installs is the one whose SHA-256 the release states. apt and
    dnf install what they are given, as root: never an unchecked file.
    """
    suffix = PLANS[kind].suffix
    url, name, _ = _asset(rel, suffix)
    if not url:
        return ("error", f"No {suffix} in release v{rel['version']} to update "
                         f"from — download it from {rel['url']}")
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
    broken = _installed_starts(rel["version"])
    if broken:
        previous = (f"https://github.com/{SELF_REPO}/releases/tag/"
                    f"v{VERSION}")
        return ("error", f"v{rel['version']} was installed, but {broken}. "
                         f"This window still runs v{VERSION}: reinstall its "
                         f"{suffix} from {previous} to go back, and please "
                         "report it.")
    return ("ok", f"Updated to v{rel['version']} — restart to use it.")


def _handoff(rel, kind):
    """Who updates an install that is not the launcher's to replace."""
    if kind == "git":
        return ("git", "This is a git checkout — run `git pull` to update.")
    if kind == "flatpak" or (kind == "system" and is_flatpak()):
        return ("system", _flatpak_update_message(rel, flatpak_installation()))
    if kind == "nix":
        return ("system",
                f"Installed with Nix — v{rel['version']} arrives through "
                "your flake or channel: `nix flake update` where the input "
                "is declared, then rebuild.")
    return ("system",
            f"Installed from a package — update with your package "
            f"manager, or download v{rel['version']} from {rel['url']}")


def self_update(rel, progress=None):
    """Replace the running launcher with release `rel`. Returns (state, msg);
    state is 'ok' | 'git' | 'system' | 'error'. Never raises — callers just
    show the message."""
    kind = update_kind()
    plan = PLANS.get(kind, PLANS["system"])
    try:
        if plan.how == "swap":
            return _swap_update(rel, kind, progress)
        if plan.how == "package":
            return _package_update(rel, kind, progress)
        return _handoff(rel, kind)
    except BolError as e:
        return ("error", str(e))
    except Exception as e:
        return ("error", f"Update failed: {e}")


def describe_install():
    """One line for Settings and `network`: how this install updates."""
    kind = update_kind()
    return {
        "appimage": "AppImage — replaces itself, keeps the previous one",
        "file": "Portable .pyz — replaces itself, keeps the previous one",
        "deb": ".deb — installed through apt, with your password",
        "rpm": ".rpm — installed through your package manager, with your "
               "password",
        "flatpak": "Flatpak — updated with flatpak",
        "nix": "Nix — updated through your flake or channel",
        "git": "git checkout — updated with git pull",
        "system": "distribution package — updated by your package manager",
    }.get(kind, kind) + f" ({PRETTY} {VERSION})"
