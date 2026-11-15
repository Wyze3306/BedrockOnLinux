"""SELinux's execmod rule, which stops Wine on a stock openSUSE (#380).

openSUSE Tumbleweed installs with SELinux enforcing, and its policy leaves
the ``selinuxuser_execmod`` boolean off. Wine needs it: Windows libraries
are relocated in place, and without execmod the kernel refuses to run the
pages Wine just wrote. Proton's first process dies on that, so ``wineboot``
exits with status 1 and, under ``WINEDEBUG=-all``, writes nothing at all.
Steam's and Lutris's RPMs pull in ``selinux-policy-targeted-gaming``, which
turns the boolean on; a Flatpak cannot pull in a host package, so every
Flathub user on Tumbleweed meets this on the first prefix.

Outside a sandbox the boolean is read from selinuxfs. Inside the Flatpak
there is no /sys/fs at all, so the most that can be known is that SELinux is
active -- the process label in /proc says so -- and which distribution the
host runs, from /run/host/os-release.
"""

import re
from pathlib import Path

BOOLEAN = "selinuxuser_execmod"
SELINUXFS = Path("/sys/fs/selinux")
PROC_LABEL = Path("/proc/self/attr/current")
OS_RELEASE = (Path("/run/host/os-release"), Path("/run/host/etc/os-release"),
              Path("/etc/os-release"), Path("/usr/lib/os-release"))

ALLOWED = "allowed"
DENIED = "denied"
UNKNOWN = "unknown"

# user:role:type[:level] -- AppArmor labels read "name (mode)" or
# "unconfined", and Smack's are a single word.
_SELINUX_LABEL = re.compile(r"[^\s:()]+:[^\s:()]+:[^\s:()]+(?::\S+)?")


def _read(path):
    try:
        return Path(path).read_text(errors="replace").strip("\0 \n\t")
    except OSError:
        return ""


def selinux_label(proc_label=None):
    """This process's SELinux context, or "" when SELinux is not the LSM."""
    text = _read(PROC_LABEL if proc_label is None else proc_label)
    return text if _SELINUX_LABEL.fullmatch(text) else ""


def execmod_state(selinuxfs=None, proc_label=None):
    """ALLOWED, DENIED (enforcing with the boolean off) or UNKNOWN (SELinux
    is active but its settings are out of sight, as in the Flatpak)."""
    root = Path(SELINUXFS if selinuxfs is None else selinuxfs)
    enforce = _read(root / "enforce")
    if not enforce:
        return UNKNOWN if selinux_label(proc_label) else ALLOWED
    if enforce != "1":
        return ALLOWED  # permissive: denials are logged, never enforced
    # "<current> <pending>", e.g. "0 0".
    value = _read(root / "booleans" / BOOLEAN).split()
    if not value:
        # A policy without the boolean has no such rule to trip on.
        return ALLOWED
    return ALLOWED if value[0] == "1" else DENIED


def summary(selinuxfs=None, proc_label=None):
    """One line for the system check."""
    state = execmod_state(selinuxfs, proc_label)
    if state == DENIED:
        return f"enforcing, {BOOLEAN} off (Wine cannot run)"
    if state == UNKNOWN:
        return f"active ({BOOLEAN} cannot be read from the sandbox)"
    selinuxfs = SELINUXFS if selinuxfs is None else selinuxfs
    if selinux_label(proc_label) or _read(Path(selinuxfs) / "enforce"):
        return "OK"
    return "not in use"


def host_is_suse(paths=None):
    for path in (OS_RELEASE if paths is None else paths):
        text = _read(path)
        if not text:
            continue
        fields = dict(re.findall(r'^([A-Z_]+)="?([^"\n]*)"?$', text, re.M))
        ids = f"{fields.get('ID', '')} {fields.get('ID_LIKE', '')}".lower()
        return "suse" in ids
    return False


def fix_hint(suse=None):
    suse = host_is_suse() if suse is None else suse
    if suse:
        return ("Install openSUSE's gaming policy, which turns it on: "
                "sudo zypper install selinux-policy-targeted-gaming "
                f"(or: sudo setsebool -P {BOOLEAN} 1). Steam's and Lutris's "
                "packages install it for you; a Flatpak cannot. openSUSE "
                "leaves it off as hardening, and every Wine or Proton game "
                "on this system needs it on.")
    return (f"Turn it on with: sudo setsebool -P {BOOLEAN} 1 "
            "(every Wine or Proton game on this system needs it on).")


def problem(failed=False, selinuxfs=None, proc_label=None, os_release=None):
    """A sentence naming the SELinux rule that stops Wine, or "".

    Before anything ran it speaks only when the rule is known to apply: the
    boolean is off, or SELinux is active on an openSUSE host whose default
    is off. Once Wine has ``failed`` an active SELinux whose settings cannot
    be read is worth naming too.
    """
    state = execmod_state(selinuxfs, proc_label)
    if state == ALLOWED:
        return ""
    suse = host_is_suse(os_release)
    if state == DENIED:
        return (f"SELinux is enforcing with {BOOLEAN} off, so it refuses "
                "Wine the memory it needs to load Windows programs. "
                + fix_hint(suse))
    if not (suse or failed):
        return ""
    where = "This openSUSE system" if suse else "This system"
    verdict = ("That is the likely cause." if failed else
               "If Minecraft or its Wine prefix will not start, that is the "
               "likely cause.")
    return (f"{where} runs SELinux, which stops Wine unless {BOOLEAN} is "
            "on (openSUSE leaves it off), and the launcher cannot read that "
            f"setting from inside its sandbox. {verdict} " + fix_hint(suse))
