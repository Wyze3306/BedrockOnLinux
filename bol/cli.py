"""bol.cli — argument parsing and command dispatch (main)."""
# SPDX-License-Identifier: MIT

import argparse
import os
import sys

from .auth import NativeAuth
from .config import APP, PRETTY, VERSION
from .content import cmd_import
from .doctor import doctor
from .games import list_mc_versions
from .gamesetup import do_setup
from .gui import gui
from .launch import launch
from .log import BolError, IS_TTY, die, info, ok, warn
from .prefix import reset_prefix
from .update import check_for_update, self_update, update_kind

def main():
    p = argparse.ArgumentParser(prog=APP, description=f"{PRETTY} {VERSION}")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("gui", help="open the launcher (default)")
    sub.add_parser("play", help="launch Minecraft")
    sp = sub.add_parser("setup", help="download & prepare Minecraft")
    sp.add_argument("--mc", metavar="VERSION",
                    help="Minecraft version tag (e.g. 1.26.21.1)")
    sp.add_argument("--beta", action="store_true", help="allow beta versions")
    sp.add_argument("--force", action="store_true", help="re-download / rebuild")
    lv = sub.add_parser("versions", help="list available Minecraft versions")
    lv.add_argument("--beta", action="store_true")
    sub.add_parser("login", help="sign in to a Microsoft account")
    ip = sub.add_parser("import",
                        help="import .mcpack/.mcaddon/.mcworld/.mctemplate")
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
    sub.add_parser("update", help="check for and install launcher updates")

    sub.add_parser("changelog", help="display the launcher's release changelog history")

    a = p.parse_args()
    try:
        if a.cmd == "setup":
            mc = None
            if a.mc:
                mc = next((v for v in list_mc_versions(True)
                           if v["tag"] == a.mc), None)
                if not mc:
                    die(f"Minecraft version '{a.mc}' not found.")
            do_setup(mc_ver=mc, force=a.force)
            ok(f"Done. Run:  {APP} play")
        elif a.cmd == "play":
            launch()
        elif a.cmd == "versions":
            for v in list_mc_versions(a.beta):
                print(f"  {v['tag']:<14}{'beta' if v['beta'] else 'stable':>7}"
                      f"   {v['size']>>20} MiB")
        elif a.cmd == "login":
            na = NativeAuth()
            if na.signed_in():
                ok("A Microsoft account is already linked.")
            else:
                na._flow(None, None)
                if not na.signed_in():
                    sys.exit(1)
        elif a.cmd == "doctor":
            sys.exit(0 if doctor(a.acknowledge_gpu_crash) else 1)
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
            gui()
        else:
            if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
                gui()
            else:
                p.print_help()
    except BolError:
        sys.exit(1)
