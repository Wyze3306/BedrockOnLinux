"""The launcher's one entry point.

``python3 -m bol``, the ``bedrock-on-linux`` script, the zipapp (.pyz) and
the console script a wheel installs all run :func:`main`. ``optiscaler`` is
not a subcommand of :mod:`bol.cli`, and the OptiScaler bootstrap has to run
before it: an entry point that called ``bol.cli.main`` itself answered
``bedrock-on-linux optiscaler install`` with "invalid choice" (#306).
"""
# SPDX-License-Identifier: MIT
import os
import sys


def _profile_option(argv):
    """``(name, the arguments after it)`` for ``--profile NAME`` written
    before the command, or None. After the command it is that command's own
    (``shortcut --profile``), and stays there."""
    if len(argv) < 2:
        return None
    first = argv[1]
    if first == "--profile":
        if len(argv) < 3:
            return "", argv[2:]
        return argv[2], argv[3:]
    if first.startswith("--profile="):
        return first.split("=", 1)[1], argv[2:]
    return None


def _same_launcher(argv, orig_argv, executable):
    """The command that started this launcher, with ``argv[1:]`` swapped
    for what the caller puts after it. orig_argv keeps "-m bol" and any
    interpreter option, which a path to bol/__main__.py loses."""
    tail = len(argv) - 1
    orig = list(orig_argv or [])
    if orig and len(orig) > tail and orig[len(orig) - tail:] == list(argv[1:]):
        return [executable] + orig[1:len(orig) - tail]
    return [executable, os.path.abspath(argv[0])]


def relaunch_in_profile(argv=None, execve=os.execve, base_data=None):
    """Run ``--profile NAME <command>`` as <command> in that profile (#397).

    A profile is a BOL_HOME, and the data root is fixed the moment
    bol.config is imported, so the command starts again with BOL_HOME set
    rather than switching root halfway through. This is what lets a Steam
    launch option, per Steam user, open the launcher or start the game in
    one profile: ``--profile "Name" play``.
    """
    argv = sys.argv if argv is None else argv
    option = _profile_option(argv)
    if option is None:
        return
    name, rest = option
    from .log import BolError
    from .profiles import _profile_environment, find_profile
    if not name.strip():
        print("BedrockOnLinux: --profile needs the name of a profile.",
              file=sys.stderr, flush=True)
        sys.exit(2)
    try:
        folder = find_profile(name, base_data)
    except BolError as exc:
        print(f"BedrockOnLinux: {exc}", file=sys.stderr, flush=True)
        sys.exit(2)
    env = _profile_environment(folder, base_data)
    cmd = _same_launcher(argv, getattr(sys, "orig_argv", None),
                         sys.executable) + list(rest)
    execve(cmd[0], cmd, env)


def main():
    # First of all: an update that left a launcher unable to start is undone
    # here, before anything of the new version has had a chance to fail.
    from .rollback import guard
    guard()
    # Apple silicon under Asahi: Wine and FEX-Emu need the 4 KiB pages of a
    # muvm microVM, so the whole launcher moves there before it starts
    # anything x86-64 (#250).
    from .hostarch import relaunch_in_muvm
    relaunch_in_muvm()
    relaunch_in_profile()
    if len(sys.argv) > 1 and sys.argv[1] == "optiscaler":
        from .optiscaler import cli_main
        entry = lambda: cli_main(sys.argv[2:])
    else:
        from .optiscaler import bootstrap
        bootstrap()
        from .cli import main as entry
    try:
        entry()
    except KeyboardInterrupt:
        print()
        sys.exit(130)


if __name__ == "__main__":
    main()
