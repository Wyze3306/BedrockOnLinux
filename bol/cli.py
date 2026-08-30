"""bol.cli — argument parsing and command dispatch (main)."""
# SPDX-License-Identifier: MIT

import argparse
import sys
from pathlib import Path

from .auth import NativeAuth, msa_signed_in
from .config import APP, PRETTY, VERSION
from .content import cmd_import
from .doctor import doctor
from .games import (
    installed_builds,
    list_editions,
    list_versions,
    remove_build,
)
from .gamesetup import do_setup
from .launch import (
    direct_launch_readiness,
    launch,
)
from .log import BolError, IS_TTY, desktop_notify, die, err, info, ok, warn
from .network import diagnose_network
from .platform import has_display
from .prefix import reset_prefix
from .profiles import (
    create_profile,
    list_profiles,
    play_launch_command,
    profile_launch_command,
    require_profile_shortcuts_supported,
    require_shortcuts_supported,
    write_play_shortcut,
    write_profile_shortcut,
)
from .update import check_for_update, self_update, update_kind


def _run_network_diagnostics(host_ip=None):
    """Run and display the read-only connectivity checks."""
    healthy, checks = diagnose_network(host_ip)
    for check in checks:
        state = "OK" if check.ok is True else (
            "ÉCHEC" if check.ok is False else "INFO"
        )
        print(
            f"  {check.kind:14} {check.target}: {state} — {check.detail}"
        )
    return healthy


def _fmt_size(size):
    value = float(size or 0)
    for unit in ("B", "KiB", "MiB"):
        if value < 1024:
            return f"{value:.0f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def _build_notes(build):
    notes = []
    if build["in_use"]:
        notes.append("in use")
    if not build["playable"]:
        notes.append("incomplete")
    if build["legacy"]:
        notes.append("installed before the move to the Store")
    if not build["managed"]:
        notes.append("your own folder")
    return notes


# Said by every command that lists or removes a build, because it is the one
# thing a player needs to know before deleting one and the one thing the
# folder name does not tell them.
_BUILDS_KEEP_NOTE = (
    "Worlds, settings, screenshots and packs are kept with your profile, not "
    "in these folders, so removing a build removes only the download."
)


def _list_installed_builds():
    """Every Minecraft build on this machine, and what it takes up."""
    builds = installed_builds()
    if not builds:
        info("No Minecraft build is installed.")
        return
    total = 0
    for build in builds:
        total += build["size"] or 0
        notes = _build_notes(build)
        print(f"  {build['version']:<14}{_fmt_size(build['size']):>10}  "
              f"{build['name']}"
              + (f"  ({', '.join(notes)})" if notes else ""))
        print(f"      {build['path']}")
    info(f"{len(builds)} build(s), {_fmt_size(total)} in total. "
         + _BUILDS_KEEP_NOTE)


def _remove_installed_build(wanted, edition_id=None):
    """Delete one downloaded build, named by version or by folder."""
    builds = installed_builds(with_size=False)
    if "/" in wanted:
        target = Path(wanted).expanduser().resolve()
        matches = [b for b in builds if b["path"].resolve() == target]
    else:
        matches = [b for b in builds if b["version"] == wanted]
        if edition_id:
            matches = [b for b in matches if b["edition"] == edition_id]
    if not matches:
        die(f"No installed build matches '{wanted}'. See: {APP} versions "
            "--installed")
    if len(matches) > 1:
        die(f"'{wanted}' matches {len(matches)} installed builds. Name the "
            "edition with --mc, or pass the folder instead: "
            + ", ".join(str(b["path"]) for b in matches))
    build = matches[0]
    if not build["managed"]:
        die(f"{build['path']} is a folder you pointed the launcher at, so it "
            "is not the launcher's to delete.")
    freed = remove_build(build["path"])
    ok(f"Removed {build['name']} {build['version']} — {_fmt_size(freed)} "
       "freed.")
    info(_BUILDS_KEEP_NOTE)


def _open_gui():
    """Open the launcher window, loading Qt only when it is actually asked for.

    Importing bol.gui pulls the whole Qt stack in, which used to happen for
    every command on the way to `main()`. That made two failures much worse
    than they are: a toolkit that is not installed yet (portable .pyz, bare
    checkout) never reached the pip bootstrap below, and a host missing one of
    Qt's own shared libraries took every other command down with a traceback
    nobody could act on -- "ImportError: libzstd.so.1: cannot open shared
    object file" in place of a launcher (issue #205). Imported here, the GUI
    is the only thing that needs Qt, and what is missing gets a name.
    """
    from . import deps
    # Only the toolkit itself is worth stopping for, and only when it really
    # is absent: bol.gui bootstraps the rest (packaging, python-xlib) once it
    # is imported, and works without them.
    if not deps.have("PySide6") and "PySide6" in deps.ensure_gui_deps():
        die("The launcher window needs the Qt toolkit (PySide6), which is "
            "not installed here and could not be installed automatically. "
            "Install it with pip, or use the AppImage, Flatpak, .deb or .rpm "
            f"— each of those carries it. `{APP} play` needs none of it.")
    try:
        from .gui import gui
    except ImportError as exc:
        library = deps.missing_shared_library(exc)
        if library is None:
            raise
        die(f"The launcher window needs the system library {library}, which "
            f"this system does not have. Install the package your "
            f"distribution ships it in, then open the launcher again — "
            f"`{APP} play` and `{APP} doctor` keep working without it.")
    gui()


def _report_launch_failure(message):
    """Show why PLAY stopped when a shortcut left no terminal to print to."""
    if IS_TTY:
        return
    desktop_notify(f"Minecraft did not start.\n{message}")


def main():
    p = argparse.ArgumentParser(prog=APP, description=f"{PRETTY} {VERSION}")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("gui", help="open the launcher (default)")
    sub.add_parser("play", help="launch Minecraft")
    sp = sub.add_parser("setup", help="download & prepare Minecraft")
    sp.add_argument("--mc", metavar="EDITION",
                    help="Minecraft edition to install: release or preview")
    sp.add_argument("--version", metavar="BUILD",
                    help="Bedrock build to install (default: the newest); "
                         "see 'versions'")
    sp.add_argument("--beta", action="store_true", help="allow beta editions")
    sp.add_argument("--force", action="store_true", help="re-download / rebuild")
    lv = sub.add_parser("versions", help="list installable Minecraft builds")
    lv.add_argument("--beta", action="store_true")
    lv.add_argument(
        "--installed", action="store_true",
        help="list the builds downloaded on this machine and what they take "
             "up, instead of what can be installed")
    lv.add_argument(
        "--remove", metavar="BUILD",
        help="delete a downloaded build, by version or by folder; worlds, "
             "settings and screenshots are kept")
    lv.add_argument(
        "--mc", metavar="EDITION",
        help="which edition --remove means, when a build is installed for "
             "more than one")
    sub.add_parser("login", help="sign in to a Microsoft account")
    sub.add_parser(
        "store-login",
        help=("link the Microsoft account that owns Minecraft, so it can be "
              "downloaded from the Microsoft Store"),
    )
    ip = sub.add_parser("import",
                        help=("import .mcpack/.mcaddon/.mcworld/.mctemplate/"
                              ".mcskin"))
    ip.add_argument("files", nargs="+", metavar="FILE",
                    help="content file(s) to import")
    sub.add_parser("repair", help="reset the Wine prefix")
    dp = sub.add_parser("doctor", help="check host requirements")
    dp.add_argument(
        "--acknowledge-gpu-crash",
        action="store_true",
        help=("after repairing the graphics driver and rebooting, clear the "
              "interrupted-launch safety block"),
    )
    dp.add_argument(
        "--network",
        action="store_true",
        help="run read-only Xbox/Minecraft connectivity checks",
    )
    dp.add_argument(
        "--host",
        metavar="IP",
        help="also inspect the route to a LAN host IP (implies --network)",
    )
    pp = sub.add_parser(
        "profiles",
        help="create or list isolated Xbox account profiles",
    )
    profiles_sub = pp.add_subparsers(dest="profiles_cmd")
    profiles_create = profiles_sub.add_parser(
        "create",
        help="create an isolated profile and desktop shortcut",
    )
    profiles_create.add_argument("name", metavar="NAME")
    profiles_sub.add_parser("list", help="list isolated profiles")
    sc = sub.add_parser(
        "shortcut",
        help="create a desktop/Steam shortcut that launches Minecraft "
             "directly, without the launcher window",
    )
    sc.add_argument(
        "--profile",
        metavar="NAME",
        help="launch an isolated profile instead of the default installation",
    )
    sub.add_parser("update", help="check for and install launcher updates")

    sub.add_parser("changelog", help="display the launcher's release changelog history")

    a = p.parse_args()
    try:
        # Migration must precede every write to the new XDG root.
        from .util import _ensure_xdg_storage
        _ensure_xdg_storage()
        if a.cmd == "setup":
            mc = None
            if a.mc:
                mc = next((v for v in list_editions(True)
                           if v["id"] == a.mc.strip().lower()), None)
                if not mc:
                    die(f"Unknown Minecraft edition '{a.mc}'; expected "
                        + " or ".join(v["id"] for v in list_editions(True))
                        + ".")
            do_setup(mc_edition=mc, mc_version=a.version, force=a.force)
            ok(f"Done. Run:  {APP} play")
        elif a.cmd == "play":
            # Offline is a real way to play, so this warns and carries on --
            # but it says so here rather than letting Realms, servers and
            # friends come up missing in-game with nothing having mentioned
            # a sign-in (#240).
            if not msa_signed_in():
                warn("Not signed in for online play: no Realms, no servers, "
                     "no friends and no Marketplace.")
                info(f"Sign in with:  {APP} login")
            launch()
        elif a.cmd == "shortcut":
            require_shortcuts_supported()
            profile = create_profile(a.profile) if a.profile else None
            entry = write_play_shortcut(
                profile_name=a.profile, profile_dir=profile)
            ok(f"Desktop shortcut: {entry}")
            print("Steam command: " + play_launch_command(profile))
            info("It starts Minecraft with no launcher window. Add it to "
                 "Steam with 'Add a Non-Steam Game' to play it from the "
                 "library, including Steam Deck Game Mode.")
            # A profile's own installation state lives under its BOL_HOME.
            for pending in ([] if profile else direct_launch_readiness()):
                warn(pending)
        elif a.cmd == "versions":
            if a.remove:
                _remove_installed_build(a.remove, a.mc)
            elif a.installed:
                _list_installed_builds()
            else:
                for edition in list_editions(a.beta):
                    print(f"{edition['name']}  (--mc {edition['id']})")
                    for build in list_versions(edition["id"]):
                        print(f"    {build['version']:<14}"
                              f"{'installed' if build['installed'] else ''}")
                info("Builds are downloaded from Microsoft's own CDN with "
                     "your account, which must own Minecraft.")
        elif a.cmd == "store-login":
            from . import xodus as _xodus
            if _xodus.signed_in():
                ok("A Microsoft account is already linked for the download.")
            else:
                _xodus.login()
        elif a.cmd == "login":
            na = NativeAuth()
            if na.signed_in():
                ok("A Microsoft account is already linked.")
            else:
                na._flow(None, None)
                if not na.signed_in():
                    sys.exit(1)
        elif a.cmd == "doctor":
            system_healthy = doctor(a.acknowledge_gpu_crash)
            network_healthy = True
            if a.network or a.host:
                network_healthy = _run_network_diagnostics(a.host)
            sys.exit(0 if system_healthy and network_healthy else 1)
        elif a.cmd == "profiles":
            if a.profiles_cmd == "create":
                require_profile_shortcuts_supported()
                profile = create_profile(a.name)
                shortcut = write_profile_shortcut(
                    a.name,
                    profile_dir=profile,
                )
                ok(f"Profile: {profile}")
                ok(f"Desktop shortcut: {shortcut}")
                print("Steam command: " + profile_launch_command(profile))
            elif a.profiles_cmd == "list":
                profiles = list_profiles()
                if not profiles:
                    info("No isolated profiles.")
                for profile in profiles:
                    print(
                        f"  {profile['name']} ({profile['slug']}): "
                        f"{profile['path']}"
                    )
            else:
                pp.print_help()
        elif a.cmd == "update":
            rel = check_for_update()
            if not rel:
                ok(f"{PRETTY} {VERSION} is up to date.")
            else:
                info(f"Update available: v{rel['version']} "
                     f"(you have {VERSION}).")
                go = (update_kind() in ("git", "system") or not IS_TTY
                      or input(f"Install v{rel['version']} now? [y/N] ")
                      .strip().lower() == "y")
                if go:
                    state, msg = self_update(rel)
                    (ok if state == "ok" else warn)(msg)
                else:
                    info("Update skipped.")
        elif a.cmd == "import":
            cmd_import(a.files)
        elif a.cmd == "repair":
            reset_prefix()
        elif a.cmd == "changelog":
            try:
                from .util import gh_latest
                from .config import SELF_REPO
                rel = gh_latest(SELF_REPO)
                if not rel:
                    print("No release found.")
                    return
                tag = rel.get("tag_name", "Unknown")
                date = (rel.get("published_at") or "").split("T")[0]
                body = rel.get("body") or ""
                print(f"Release {tag} ({date})")
                print("-" * (len(tag) + len(date) + 11))
                print(body.strip())
            except Exception as e:
                print(f"Error fetching changelog: {e}")
        elif a.cmd == "gui":
            _open_gui()
        else:
            # macOS always answers yes here: a process started from Finder,
            # from a .app or from Terminal can open a window with no display
            # variable to look at, and requiring one would print help at every
            # double-click.
            if has_display():
                _open_gui()
            else:
                p.print_help()
    except BolError as exc:
        from .xodus import DEVICE_PAGE, DeviceLimitReached, NotSignedIn
        if not getattr(exc, "reported", False):
            err(str(exc))
        if isinstance(exc, NotSignedIn):
            # The download's own account link, which the GUI offers with a
            # button. Name the command that does it rather than leaving the
            # terminal with a sign-in it cannot start.
            info(f"Sign in with:  {APP} store-login")
        if isinstance(exc, DeviceLimitReached):
            # The remedy is a web page and the way back is a command; the
            # window opens the one and presses the other. Here both are simply
            # named, so the terminal is not left with a paragraph to re-read.
            info(f"Remove a device at:  {DEVICE_PAGE}")
            info(f"Then run again:  {APP} {a.cmd or 'setup'}")
        if a.cmd == "play":
            _report_launch_failure(str(exc))
        sys.exit(1)
