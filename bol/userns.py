"""AppArmor's user-namespace restriction, which stops the game's container (#384).

Ubuntu 24.04 and the distributions built on it (TUXEDO OS, Zorin, Pop!_OS,
Linux Mint 22...) turn on ``kernel.apparmor_restrict_unprivileged_userns``:
a program without an AppArmor profile that allows it may still create a
user namespace, but gets no capability inside it. The Steam Linux Runtime
container that every Proton process runs in is built by bubblewrap, which
then cannot write its uid map, and the very first ``wineboot`` dies with

    bwrap: setting up uid map: Permission denied

Steam ships an AppArmor profile for its own copy of the runtime; the
launcher's copy, under its data folder, has none.
"""

import re
from pathlib import Path

SYSCTL = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
SYSCTL_NAME = "kernel.apparmor_restrict_unprivileged_userns"
SYSCTL_FILE = "/etc/sysctl.d/60-bedrock-on-linux-userns.conf"

_BWRAP_REFUSED = re.compile(
    r"bwrap: (?:setting up uid map: Permission denied|"
    r"No permissions to create (?:a )?new namespace|"
    r"Creating new namespace failed)", re.I)


def restricted(sysctl=None):
    """Whether AppArmor refuses capabilities in new user namespaces."""
    try:
        value = Path(SYSCTL if sysctl is None else sysctl).read_text().strip()
    except OSError:
        return False
    return value == "1"


def bwrap_refused(text):
    """Whether a log names bubblewrap failing to set up its namespace."""
    return bool(_BWRAP_REFUSED.search(text or ""))


def problem(log_text, sysctl=None):
    """A sentence naming the restriction behind a failed container, or ""."""
    if not bwrap_refused(log_text):
        return ""
    if restricted(sysctl):
        return (
            "AppArmor on this system refuses unprivileged user namespaces "
            f"({SYSCTL_NAME} = 1, the default on Ubuntu 24.04 and the "
            "distributions based on it), so the Steam Linux Runtime "
            "container that runs Proton cannot start. Steam installs an "
            "exception for its own copy of that runtime; the launcher's has "
            "none. Allow them again with: echo "
            f"'{SYSCTL_NAME} = 0' | sudo tee {SYSCTL_FILE} && sudo sysctl "
            "--system, then click PLAY again.")
    return (
        "The system refused the Steam Linux Runtime container a user "
        "namespace (bubblewrap could not set up its uid map), so Proton "
        "cannot start. Unprivileged user namespaces must be allowed: check "
        "the kernel.unprivileged_userns_clone and user.max_user_namespaces "
        "sysctls, and any AppArmor or SELinux policy that restricts them.")
