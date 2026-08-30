"""Isolated account/prefix profiles, and the desktop/Steam shortcuts."""
# SPDX-License-Identifier: MIT

import fcntl
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from .config import APP, DATA, FLATPAK_APP_ID, PRETTY, XDG_DATA_HOME
from .platform import IS_MAC
from .log import BolError


_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}")
# Big, account-independent payloads: the game builds, the engine, the runtimes
# and the downloader binary. Deliberately not here: "xodus-home", which holds
# the Microsoft Store session. A keyring can hold one user, so sharing it would
# have every profile sign the previous one out -- and profiles exist to keep
# accounts apart, which is why "msa" is per-profile too. Each profile
# therefore takes one Store device of its own (issue #198).
_SHARED_DIRS = ("games", "proton", "umu", "cache", "xodus", "xodus-xcurl",
                "xodus-webview")


def profile_slug(name):
    display = str(name).strip()
    if not _PROFILE_NAME.fullmatch(display) or ".." in display:
        raise BolError(
            "Profile names must be 1–40 characters and use only letters, "
            "numbers, spaces, '.', '_' or '-'."
        )
    slug = re.sub(r"[^a-z0-9]+", "-", display.lower()).strip("-")
    if not slug:
        raise BolError("The profile name does not contain a usable identifier.")
    return slug


def _metadata_path(profile_dir):
    return Path(profile_dir) / "profile.json"


def _profile_base(base_data=None):
    base = Path(DATA if base_data is None else base_data).expanduser().resolve()
    if base_data is not None or base.parent.name != "profiles":
        return base
    try:
        metadata = json.loads(_metadata_path(base).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return base
    if metadata.get("name") and metadata.get("slug") == base.name:
        return base.parent.parent
    return base


def profiles_root(base_data=None):
    return _profile_base(base_data) / "profiles"


def _ensure_shared_link(profile_dir, base_data, name):
    target_path = Path(base_data) / name
    if target_path.is_symlink():
        if not target_path.is_dir():
            raise BolError(f"Shared profile target is a dangling link: {target_path}")
    elif target_path.exists():
        if not target_path.is_dir():
            raise BolError(f"Shared profile target is not a directory: {target_path}")
    else:
        # Another profile process may win this shared-link creation race.
        target_path.mkdir(parents=True, exist_ok=True)
    target = target_path.resolve()
    link = Path(profile_dir) / name
    if link.is_symlink():
        if not link.is_dir():
            raise BolError(f"Profile shared path is a dangling link: {link}")
        if link.resolve() != target:
            raise BolError(f"Profile shared path points elsewhere: {link}")
        return
    if link.exists():
        raise BolError(f"Profile shared path is not a symlink: {link}")
    link.symlink_to(target, target_is_directory=True)


def _environ_uses_profile_home(environ, profile_dir):
    """Match an exact NUL-delimited BOL_HOME entry."""
    target = b"BOL_HOME=" + os.fsencode(str(Path(profile_dir).resolve()))
    return target in environ.split(b"\0")


def profile_processes(profile_dir):
    """Return live PIDs running BedrockOnLinux with this exact BOL_HOME."""
    found = []
    for pdir in Path("/proc").glob("[0-9]*"):
        try:
            if _environ_uses_profile_home(
                    pdir.joinpath("environ").read_bytes(), profile_dir):
                pid = int(pdir.name)
                if pid != os.getpid():
                    found.append(pid)
        except Exception:
            continue
    return sorted(set(found))


def require_profile_idle(profile_dir, action="modify profile"):
    """Fail before a profile mutation while game or launcher processes are live."""
    p_dir = Path(profile_dir)
    pfx = p_dir / "compatdata" / "pfx"
    if pfx.is_dir():
        from .prefix import prefix_processes
        live_wine = prefix_processes(pfx)
        if live_wine:
            raise BolError(
                f"Cannot {action}: {len(live_wine)} Wine/Proton process(es) still "
                "use this profile. Close Minecraft or use 'Force stop Minecraft' first."
            )

    lock_path = p_dir / ".launch.lock"
    if lock_path.exists():
        try:
            lock_fd = os.open(lock_path, os.O_RDWR, 0o600)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BolError(
                    f"Cannot {action}: a game session or preparation task is "
                    "currently active in this profile. Close Minecraft first."
                ) from exc
            finally:
                os.close(lock_fd)
        except OSError:
            pass

    live_gui = profile_processes(p_dir)
    if live_gui:
        raise BolError(
            f"Cannot {action}: {len(live_gui)} launcher window(s) are currently "
            "open for this profile. Close those windows first."
        )


def create_profile(name, base_data=None):
    """Create an account/prefix-isolated profile while sharing large assets."""
    display = str(name).strip()
    slug = profile_slug(display)
    if slug == "default":
        raise BolError("The name 'Default' is reserved for the main installation root.")
    base = _profile_base(base_data)
    root = profiles_root(base)
    profile_dir = root / slug
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / f".{slug}.create.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            os.chmod(lock_path, 0o600)
        except OSError:
            pass
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        profile_dir.mkdir(mode=0o700, exist_ok=True)
        try:
            os.chmod(profile_dir, 0o700)
        except OSError:
            pass

        # Re-creating an existing profile repairs it: an interrupted creation
        # leaves a directory with no metadata, and a profile made before a
        # release that shares one more directory still misses that link.
        metadata_path = _metadata_path(profile_dir)
        if metadata_path.exists():
            try:
                existing = json.loads(
                    metadata_path.read_text(encoding="utf-8")
                )
            except Exception as exc:
                raise BolError(
                    f"Profile metadata is unreadable: {metadata_path}"
                ) from exc
            if existing.get("name") != display:
                raise BolError(
                    f"Profile identifier '{slug}' is already used by "
                    f"'{existing.get('name', 'another profile')}'."
                )

        for directory in _SHARED_DIRS:
            _ensure_shared_link(profile_dir, base, directory)

        fd, staged_name = tempfile.mkstemp(
            prefix=".profile-", suffix=".json", dir=profile_dir
        )
        staged = Path(staged_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"name": display, "slug": slug}, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(staged, 0o600)
            os.replace(staged, metadata_path)
        finally:
            staged.unlink(missing_ok=True)
        return profile_dir
    finally:
        os.close(lock_fd)


def list_profiles(base_data=None):
    root = profiles_root(base_data)
    if not root.is_dir():
        return []
    found = []
    for metadata in root.glob("*/profile.json"):
        try:
            item = json.loads(metadata.read_text(encoding="utf-8"))
            item["path"] = str(metadata.parent)
            if item.get("name") and item.get("slug"):
                found.append(item)
        except Exception:
            continue
    found.sort(key=lambda p: str(p.get("name", "")).lower())
    return found


def delete_profile(name, base_data=None, applications_dir=None):
    """Delete an isolated profile directory and its shortcuts."""
    display = str(name).strip()
    slug = profile_slug(display)
    if slug == "default":
        raise BolError("The main 'Default' profile cannot be deleted.")
    base = _profile_base(base_data)
    root = profiles_root(base)
    profile_dir = root / slug
    if not profile_dir.is_dir():
        raise BolError(f"Profile '{display}' does not exist.")

    require_profile_idle(profile_dir, f"delete profile '{display}'")

    for item in profile_dir.iterdir():
        if item.is_symlink():
            try:
                item.unlink()
            except OSError:
                pass

    shutil.rmtree(profile_dir)

    apps = shortcuts_dir(applications_dir)
    for shortcut in _profile_shortcut_paths(apps, slug, display):
        shortcut.unlink(missing_ok=True)
    return True


def rename_profile(old_name, new_name, base_data=None, applications_dir=None):
    """Rename the display name and directory of an existing profile."""
    old_display = str(old_name).strip()
    old_slug = profile_slug(old_display)
    if old_slug == "default":
        raise BolError("The main 'Default' profile cannot be renamed.")

    new_display = str(new_name).strip()
    new_slug = profile_slug(new_display)
    if new_slug == "default":
        raise BolError("The name 'Default' is reserved for the main installation root.")

    base = _profile_base(base_data)
    root = profiles_root(base)
    old_dir = root / old_slug
    new_dir = root / new_slug

    if not old_dir.is_dir():
        raise BolError(f"Profile '{old_display}' does not exist.")

    if new_slug != old_slug and new_dir.exists():
        raise BolError(f"A profile named '{new_display}' already exists.")

    require_profile_idle(old_dir, f"rename profile '{old_display}'")

    if new_slug != old_slug:
        old_dir.rename(new_dir)
        target_dir = new_dir
    else:
        target_dir = old_dir

    fd, staged_name = tempfile.mkstemp(
        prefix=".profile-", suffix=".json", dir=target_dir
    )
    staged = Path(staged_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"name": new_display, "slug": new_slug}, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(staged, 0o600)
        os.replace(staged, _metadata_path(target_dir))
    finally:
        staged.unlink(missing_ok=True)

    apps = shortcuts_dir(applications_dir)
    old_prof_desktop, old_play_desktop = _profile_shortcut_paths(
        apps, old_slug, old_display)

    if new_slug != old_slug:
        had_prof_desktop = old_prof_desktop.exists()
        had_play_desktop = old_play_desktop.exists()
        old_prof_desktop.unlink(missing_ok=True)
        old_play_desktop.unlink(missing_ok=True)
        if had_prof_desktop and profile_shortcuts_supported():
            try:
                write_profile_shortcut(
                    new_display, profile_dir=target_dir, applications_dir=apps
                )
            except Exception:
                pass
        if had_play_desktop and profile_shortcuts_supported():
            try:
                write_play_shortcut(
                    profile_name=new_display, profile_dir=target_dir,
                    applications_dir=apps
                )
            except Exception:
                pass
    else:
        if old_prof_desktop.exists() and profile_shortcuts_supported():
            try:
                write_profile_shortcut(
                    new_display, profile_dir=target_dir, applications_dir=apps
                )
            except Exception:
                pass
        if old_play_desktop.exists() and profile_shortcuts_supported():
            try:
                write_play_shortcut(
                    profile_name=new_display, profile_dir=target_dir,
                    applications_dir=apps
                )
            except Exception:
                pass

    return target_dir



def _desktop_quote(value):
    escaped = str(value)
    # Desktop Exec fields require doubled percent signs for literal values.
    escaped = escaped.replace("%", "%%")
    for old, new in (("\\", "\\\\"), ('"', '\\"'), ("`", "\\`"),
                     ("$", "\\$")):
        escaped = escaped.replace(old, new)
    return f'"{escaped}"'


def profile_shortcuts_supported(environ=None, info_path=Path("/.flatpak-info")):
    """Whether this package can install host-visible profile shortcuts."""
    source = os.environ if environ is None else environ
    return not (
        source.get("FLATPAK_ID") or Path(info_path).is_file()
    )


def require_profile_shortcuts_supported(
        environ=None, info_path=Path("/.flatpak-info")):
    if not profile_shortcuts_supported(environ, info_path):
        raise BolError(
            "Isolated profile shortcuts cannot be installed from the Flatpak "
            "sandbox. Use the AppImage, .deb or native package for the "
            "multi-profile Steam shortcut workflow."
        )


def require_shortcuts_supported(
        environ=None, info_path=Path("/.flatpak-info")):
    if not profile_shortcuts_supported(environ, info_path):
        raise BolError(
            "Shortcuts cannot be written from the Flatpak sandbox: neither "
            "the host desktop nor Steam can see the sandbox's private "
            "applications directory. This build already offers the same "
            "launch from the app menu entry's 'Play without the launcher' "
            "action, and Steam can run it as a non-Steam game with the "
            f"command: flatpak run {FLATPAK_APP_ID} play"
        )


def launcher_executable(explicit=None):
    if explicit:
        return str(Path(explicit).expanduser().resolve())
    # AppImage shortcuts must target APPIMAGE, not the temporary mount.
    appimage = os.environ.get("APPIMAGE", "").strip()
    if appimage:
        candidate = Path(appimage).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
    installed = shutil.which(APP)
    if installed:
        return installed
    return str(Path(sys.argv[0]).expanduser().resolve())


def _desktop_command(argument, profile_dir=None, executable=None):
    """Exec field running one launcher command, optionally profile-scoped."""
    prefix = ("env BOL_HOME=" + _desktop_quote(profile_dir) + " "
              if profile_dir is not None else "")
    return (prefix + _desktop_quote(launcher_executable(executable))
            + " " + argument)


def _shell_command(argument, profile_dir=None, executable=None):
    """Shell-display form of the same command, for Steam's target field."""
    import shlex
    prefix = (f"BOL_HOME={shlex.quote(str(Path(profile_dir).resolve()))} "
              if profile_dir is not None else "")
    return (prefix + shlex.quote(launcher_executable(executable))
            + " " + argument)


def _profile_shortcut_paths(apps, slug, display):
    """The two shortcut files one profile owns, named the way this OS names
    them: a slugged ``.desktop`` pair on Linux, a readable ``.command`` pair
    on macOS. Delete and rename both go through this so a renamed profile
    never leaves an orphan behind on either platform."""
    apps = Path(apps)
    if IS_MAC:
        return (apps / f"{PRETTY} — {display}.command",
                apps / f"Minecraft Bedrock — {display}.command")
    return (apps / f"{APP}-profile-{slug}.desktop",
            apps / f"{APP}-play-{slug}.desktop")


def shortcuts_dir(applications_dir=None):
    """Where a double-clickable shortcut goes on this OS.

    Linux uses the XDG applications directory, which is what both the desktop
    menu and Steam's "Add a Non-Steam Game" browse. macOS has no equivalent
    for a generated launcher entry, so the shortcut is a ``.command`` script
    in ``~/Applications`` -- a real folder in the Finder sidebar, and a file
    Finder runs in Terminal on a double-click.
    """
    if applications_dir is not None:
        return Path(applications_dir)
    if IS_MAC:
        return Path.home() / "Applications"
    return XDG_DATA_HOME / "applications"


def _write_command_script(entry, name, comment, command):
    """The macOS shortcut: an executable shell script Finder can launch."""
    entry.write_text(
        "#!/bin/sh\n"
        f"# {name}\n"
        f"# {comment}\n"
        "# Generated by BedrockOnLinux — it is rewritten whenever the\n"
        "# shortcut is created again.\n"
        f"exec {command}\n",
        encoding="utf-8",
    )
    os.chmod(entry, 0o755)
    return entry


def _write_desktop_entry(entry, name, comment, command):
    def one_line(value):
        return str(value).replace("\n", " ").replace("\r", " ")

    entry.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={one_line(name)}\n"
        f"Comment={one_line(comment)}\n"
        f"Exec={command}\n"
        "Icon=bedrock-on-linux\n"
        "Terminal=false\n"
        "Categories=Game;\n",
        encoding="utf-8",
    )
    os.chmod(entry, 0o644)
    return entry


def write_profile_shortcut(
        name, profile_dir=None, base_data=None, applications_dir=None,
        executable=None):
    """Write a desktop entry Steam can add as a distinct non-Steam game."""
    # Host desktop and Steam cannot see Flatpak's private applications path.
    if applications_dir is None:
        require_profile_shortcuts_supported()
    display = str(name).strip()
    slug = profile_slug(display)
    directory = Path(profile_dir or create_profile(display, base_data))
    apps = shortcuts_dir(applications_dir)
    apps.mkdir(parents=True, exist_ok=True)
    if IS_MAC:
        return _write_command_script(
            apps / f"{PRETTY} — {display}.command",
            f"{PRETTY} — {display}",
            f"Isolated Xbox profile: {display}",
            _shell_command("gui", directory, executable),
        )
    return _write_desktop_entry(
        apps / f"{APP}-profile-{slug}.desktop",
        f"{PRETTY} — {display}",
        f"Isolated Xbox profile: {display}",
        _desktop_command("gui", directory, executable),
    )


def write_play_shortcut(
        profile_name=None, profile_dir=None, base_data=None,
        applications_dir=None, executable=None):
    """Write a desktop entry that starts Minecraft with no launcher window.

    It runs the same guarded launch as the PLAY button, so it needs an
    installation the launcher has already prepared; see
    `bol.launch.direct_launch_readiness`.
    """
    if applications_dir is None:
        require_shortcuts_supported()
    directory = None if profile_dir is None else Path(profile_dir)
    suffix = ""
    name = "Minecraft Bedrock"
    comment = f"Start Minecraft directly, without the {PRETTY} window"
    if profile_name is not None:
        display = str(profile_name).strip()
        suffix = "-" + profile_slug(display)
        if directory is None:
            directory = Path(create_profile(display, base_data))
        name = f"Minecraft Bedrock — {display}"
        comment = (f"Start Minecraft directly for the {display} profile, "
                   f"without the {PRETTY} window")
    apps = shortcuts_dir(applications_dir)
    apps.mkdir(parents=True, exist_ok=True)
    if IS_MAC:
        return _write_command_script(
            apps / f"{name}.command", name, comment,
            _shell_command("play", directory, executable),
        )
    return _write_desktop_entry(
        apps / f"{APP}-play{suffix}.desktop",
        name,
        comment,
        _desktop_command("play", directory, executable),
    )


def profile_launch_command(profile_dir, executable=None):
    """Shell-display form for adding a profile directly to Steam."""
    return _shell_command("gui", profile_dir, executable)


def play_launch_command(profile_dir=None, executable=None):
    """Shell-display form of the launcher-free launch, for Steam."""
    return _shell_command("play", profile_dir, executable)


def current_profile_info(base_data=None):
    """Return metadata for the active profile, or 'Default' for the main root."""
    base = Path(DATA if base_data is None else base_data).expanduser().resolve()
    if base.parent.name == "profiles":
        try:
            metadata = json.loads(_metadata_path(base).read_text(encoding="utf-8"))
            if metadata.get("name") and metadata.get("slug") == base.name:
                return {
                    "name": metadata["name"],
                    "slug": metadata["slug"],
                    "path": str(base),
                }
        except (OSError, ValueError, TypeError):
            pass
    return {
        "name": "Default",
        "slug": "default",
        "path": None,
    }


def current_profile_name(base_data=None):
    """Return the display name of the currently active profile."""
    return current_profile_info(base_data)["name"]


def relaunch_with_profile(profile_path=None, base_data=None, executable=None):
    """Relaunch the launcher GUI in the target profile's environment."""
    from .config import DEFAULT_DATA
    base_root = _profile_base(base_data)
    env = dict(os.environ)
    if profile_path:
        env["BOL_HOME"] = str(Path(profile_path).expanduser().resolve())
    else:
        if base_root.resolve() != DEFAULT_DATA.resolve():
            env["BOL_HOME"] = str(base_root.resolve())
        else:
            env.pop("BOL_HOME", None)

    exe = launcher_executable(explicit=executable)
    if exe.endswith(".py") or Path(exe).name == APP:
        args = [sys.executable, exe, "gui"]
    else:
        args = [exe, "gui"]

    os.environ.clear()
    os.environ.update(env)

    try:
        os.execv(args[0], args)
    except OSError:
        import subprocess
        subprocess.Popen(args, env=env)
        sys.exit(0)


def open_profile_window(profile_path=None, base_data=None, executable=None):
    """Spawn a new launcher GUI window for the target profile."""
    import subprocess
    from .config import DEFAULT_DATA
    base_root = _profile_base(base_data)
    env = dict(os.environ)
    if profile_path:
        env["BOL_HOME"] = str(Path(profile_path).expanduser().resolve())
    else:
        if base_root.resolve() != DEFAULT_DATA.resolve():
            env["BOL_HOME"] = str(base_root.resolve())
        else:
            env.pop("BOL_HOME", None)

    exe = launcher_executable(explicit=executable)
    if exe.endswith(".py") or Path(exe).name == APP:
        args = [sys.executable, exe, "gui"]
    else:
        args = [exe, "gui"]

    return subprocess.Popen(
        args,
        env=env,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


