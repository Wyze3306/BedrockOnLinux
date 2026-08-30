"""Read-only network diagnostics for Xbox/Minecraft connectivity.

The checks in this module deliberately do not mutate host networking.  In
particular, a UDP ``connect`` or an unanswered datagram cannot establish that
a remote RakNet port is open, so LAN diagnostics only inspect the route to a
caller-supplied, validated IP address.
"""
# SPDX-License-Identifier: MIT

import concurrent.futures
import ipaddress
import json
import re
import socket
import ssl
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional

from . import presence
from .platform import IS_MAC


NETWORK_ENDPOINTS = (
    ("Xbox Live", "user.auth.xboxlive.com", 443),
    # PlayFab REST hosts use <titleId>.playfabapi.com.  The distinct
    # b980a380.minecraft... URL used as an Xbox relying-party audience is not
    # a TLS service hostname and correctly fails certificate name validation.
    ("PlayFab", "b980a380.playfabapi.com", 443),
    ("Minecraft multiplayer", "multiplayer.minecraft.net", 443),
)

_VIRTUAL_INTERFACE_RE = re.compile(
    r"^(?:tun|tap|wg|tailscale|zerotier|zt|vpn|proton|mullvad|nordlynx|"
    r"docker|podman|virbr|br-|veth)"
    r"[a-z0-9_.-]*$",
    re.IGNORECASE,
)

_TLS_ADDRESS_TIMEOUT = 1.0


@dataclass(frozen=True)
class NetworkCheck:
    kind: str
    target: str
    ok: Optional[bool]
    detail: str


_RESOLVER_CODE = (
    "import json,socket,sys;"
    "r=socket.getaddrinfo(sys.argv[1],int(sys.argv[2]),"
    "type=socket.SOCK_STREAM);"
    "print(json.dumps(sorted({x[4][0] for x in r})))"
)


def _resolved_addresses(host, port, timeout):
    """Resolve *host* in a killable child process with a strict deadline."""

    try:
        completed = subprocess.run(
            [
                sys.executable, "-I", "-c", _RESOLVER_CODE,
                str(host), str(int(port)),
            ],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            f"DNS lookup timed out after {timeout:g}s"
        ) from exc
    except OSError as exc:
        raise OSError(
            "could not start the bounded DNS resolver"
        ) from exc
    if completed.returncode:
        reason = (completed.stderr or "").strip().splitlines()
        raise socket.gaierror(
            reason[-1][:300] if reason else
            f"resolver exited with status {completed.returncode}"
        )
    try:
        values = json.loads(completed.stdout)
        addresses = tuple(sorted({
            str(ipaddress.ip_address(value))
            for value in values
        }))
    except (TypeError, ValueError) as exc:
        raise socket.gaierror("resolver returned invalid address data") from exc
    return addresses


def _tls_version(host, port, timeout, addresses):
    """Complete bounded verified TLS to a resolved IP, retaining hostname SNI."""

    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    deadline = time.monotonic() + timeout
    last_error = None
    address_list = tuple(addresses)
    for index, address in enumerate(address_list):
        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0:
            break
        # Give every resolved address a chance.  A blackholed IPv6 route must
        # not consume the full endpoint deadline before IPv4 is attempted.
        addresses_left = len(address_list) - index
        if addresses_left == 1:
            attempt_timeout = remaining
        else:
            attempt_timeout = min(
                _TLS_ADDRESS_TIMEOUT,
                remaining / addresses_left,
            )
        attempt_deadline = now + attempt_timeout
        try:
            with socket.create_connection(
                    (address, port), timeout=attempt_timeout) as plain:
                handshake_timeout = min(
                    deadline, attempt_deadline,
                ) - time.monotonic()
                if handshake_timeout <= 0:
                    raise TimeoutError("TLS address attempt timed out")
                plain.settimeout(handshake_timeout)
                with context.wrap_socket(
                        plain, server_hostname=host) as secured:
                    return secured.version() or "TLS (version not reported)"
        except OSError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise TimeoutError(f"TLS handshake timed out after {timeout:g}s")


def _endpoint_checks(label, host, port, timeout, deadline, resolver, tls_probe):
    try:
        addresses = tuple(resolver(host, port, timeout))
        if not addresses:
            raise OSError("resolver returned no address")
        dns = NetworkCheck(
            "dns", host, True,
            f"{label}: " + ", ".join(addresses[:4]),
        )
    except Exception as exc:
        reason = str(exc).strip() or type(exc).__name__
        return (
            NetworkCheck("dns", host, False, f"{label}: {reason}"),
            NetworkCheck(
                "tls", host, False,
                f"{label}: skipped because DNS resolution failed",
            ),
        )

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return (
            dns,
            NetworkCheck(
                "tls", host, False,
                f"{label}: diagnostic timed out before the TLS handshake",
            ),
        )
    try:
        if tls_probe is _tls_version:
            version = tls_probe(
                host, port, min(timeout, remaining), addresses)
        else:
            # Preserve the small three-argument seam used by offline tests and
            # downstream callers supplying their own already-bounded probe.
            version = tls_probe(host, port, min(timeout, remaining))
        tls = NetworkCheck(
            "tls", host, True, f"{label}: {version or 'TLS handshake complete'}",
        )
    except Exception as exc:
        reason = str(exc).strip() or type(exc).__name__
        tls = NetworkCheck("tls", host, False, f"{label}: {reason}")
    return dns, tls


def _run_read_only(runner, argv, timeout):
    """Run one fixed, argument-vector command without a shell."""

    return runner(
        argv,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _clock_check(runner, timeout):
    argv = [
        "timedatectl", "show",
        "--property=NTPSynchronized",
        "--property=NTP",
        "--property=LocalRTC",
    ]
    try:
        result = _run_read_only(runner, argv, timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return NetworkCheck(
            "clock", "host", None,
            "RTC/NTP status unavailable: " + type(exc).__name__,
        )
    if result.returncode:
        detail = (result.stderr or "").strip() or \
            f"timedatectl exited with status {result.returncode}"
        return NetworkCheck("clock", "host", None,
                            "RTC/NTP status unavailable: " + detail)

    values = {}
    for line in (result.stdout or "").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip() or "unknown"
    detail = (
        f"NTP enabled={values.get('NTP', 'unknown')}; "
        f"NTP synchronized={values.get('NTPSynchronized', 'unknown')}; "
        f"RTC uses local time={values.get('LocalRTC', 'unknown')}"
    )
    synchronized = values.get("NTPSynchronized", "").strip().lower()
    if synchronized in {"no", "false", "0"}:
        return NetworkCheck(
            "clock", "host", False,
            detail + ". The system clock is not synchronized; correct the "
            "host date/time before retrying Xbox authentication or LAN play.",
        )
    # LocalRTC=yes alone is informational; it does not prove clock drift.
    return NetworkCheck("clock", "host", None, detail)


def clock_is_unsynchronized(runner=None, timeout=3.0):
    """True only when the host positively reports an unsynchronized clock.

    Xbox Live rejects XSTS requests whose signature falls outside the token
    validity window, so a drifted clock surfaces as an account rejection.
    Unavailable RTC/NTP information stays False: never blame the clock on a
    host that could not be asked.
    """

    try:
        return _clock_check(runner or subprocess.run, timeout).ok is False
    except Exception:
        return False


def _virtual_interface_check(runner, timeout):
    try:
        result = _run_read_only(
            runner, ["ip", "-o", "link", "show", "up"], timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return NetworkCheck(
            "virtual-interfaces", "host", None,
            "Active VPN/bridge interface list unavailable: "
            + type(exc).__name__,
        )
    if result.returncode:
        detail = (result.stderr or "").strip() or \
            f"ip exited with status {result.returncode}"
        return NetworkCheck(
            "virtual-interfaces", "host", None,
            "Active VPN/bridge interface list unavailable: " + detail,
        )

    interfaces = []
    for line in (result.stdout or "").splitlines():
        match = re.match(r"^\d+:\s+([^:]+):", line)
        if not match:
            continue
        name = match.group(1).split("@", 1)[0]
        if _VIRTUAL_INTERFACE_RE.fullmatch(name):
            interfaces.append(name)
    interfaces = sorted(set(interfaces))
    if interfaces:
        detail = (
            "Active VPN/container-bridge interface(s): "
            + ", ".join(interfaces)
            + ". Presence alone does not prove which route Minecraft uses."
        )
    else:
        detail = (
            "No active interface with a common VPN or container-bridge name "
            "was found."
        )
    return NetworkCheck("virtual-interfaces", "host", None, detail)


def _route_check(host_ip, runner, timeout):
    raw = str(host_ip).strip()
    try:
        # Scoped strings contain an interface name as well as an IP.  Reject
        # them so only the validated address reaches ``ip`` as an argument.
        if not raw or "%" in raw:
            raise ValueError
        address = str(ipaddress.ip_address(raw))
    except ValueError:
        return NetworkCheck(
            "route", raw or "(empty)", False,
            "Route target must be a literal IPv4 or IPv6 address.",
        )

    # iproute2 on Linux, BSD route(8) on macOS. Same question -- which
    # interface reaches this LAN host -- asked of two different tools, which
    # is why the parsing below is per-tool rather than shared.
    argv = (["/sbin/route", "-n", "get", address] if IS_MAC
            else ["ip", "route", "get", address])
    try:
        result = _run_read_only(runner, argv, timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return NetworkCheck(
            "route", address, False,
            "Route lookup unavailable: " + type(exc).__name__,
        )
    if result.returncode:
        detail = (result.stderr or "").strip() or \
            f"{argv[0]} exited with status {result.returncode}"
        return NetworkCheck("route", address, False, detail)

    output = result.stdout or ""
    first_line = next(
        (line.strip() for line in output.splitlines() if line.strip()),
        "",
    )
    if not first_line:
        return NetworkCheck("route", address, False,
                            "Route lookup returned no route.")
    if IS_MAC:
        # route(8) prints one "key: value" pair per line rather than a single
        # route line, so read the whole output instead of only the first one.
        interface = re.search(r"^\s*interface:\s*(\S+)", output, re.MULTILINE)
        source = re.search(r"^\s*gateway:\s*(\S+)", output, re.MULTILINE)
        summary_line = (interface.group(0).strip() if interface
                        else first_line)
    else:
        interface = re.search(r"(?:^|\s)dev\s+(\S+)", first_line)
        source = re.search(r"(?:^|\s)src\s+(\S+)", first_line)
        summary_line = first_line
    facts = []
    if interface:
        facts.append("interface=" + interface.group(1))
    if source:
        facts.append(("gateway=" if IS_MAC else "source=") + source.group(1))
    if not facts:
        facts.append(summary_line[:300])
    return NetworkCheck(
        "route", address, True,
        "Kernel route: " + "; ".join(facts)
        + ". A route does not prove the host's UDP port is open.",
    )


def _social_checks(snapshot):
    """Turn a social snapshot into the three lines a "can't join" report needs.

    "I can't join my friend's world" (#243, #244) has three separate answers
    and no way to tell them apart from the outside: the account is invisible,
    the social graph does not load, or the friend is simply not in a session
    anyone could join.  Reachability alone answers none of them -- the hosts
    resolve and handshake perfectly while all three are true.

    None of these fail the report.  An account nobody can see is a real
    problem, but it is not a broken host, and a quiet evening with no friend
    in a world is not a fault at all.
    """

    if snapshot.error:
        return [NetworkCheck("xbox social", "peoplehub", None,
                             "Not measured: " + snapshot.error + ".")]
    checks = []
    if snapshot.state is None:
        checks.append(NetworkCheck(
            "xbox presence", "userpresence", None,
            "Xbox Live did not say whether this account looks online."))
    else:
        online = snapshot.state == "Online"
        checks.append(NetworkCheck(
            "xbox presence", "userpresence", None,
            "Xbox Live sees this account as %s.%s"
            % (snapshot.state,
               "" if online else
               " Friends see the same, so nobody can join or invite it. "
               "Expected while Minecraft is not running; reported during a "
               "game, it is the fault.")))
    checks.append(NetworkCheck(
        "xbox friends", "peoplehub", True,
        "%d friends readable." % snapshot.friends))
    checks.append(NetworkCheck(
        "xbox sessions", "peoplehub", None,
        "%d of %d friends are in a multiplayer session right now. A friend "
        "who is not in one has no world to join, whatever the game shows."
        % (snapshot.in_session, snapshot.friends)))
    return checks


def diagnose_network(host_ip=None, *, endpoints=NETWORK_ENDPOINTS,
                     timeout=3.0, resolver=None, tls_probe=None, runner=None,
                     social=None):
    """Return ``(ok, results)`` for read-only network diagnostics.

    Endpoint DNS resolution and certificate-verified TLS handshakes run in
    parallel.  ``host_ip`` is optional; when present, it must be a literal IP
    and is passed to ``ip route get`` only after validation.  ``resolver``,
    ``tls_probe``, ``runner`` and ``social`` are injectable to keep tests
    offline.  Unavailable RTC/NTP information, VPN-interface observations and
    the Xbox social lines use ``ok=None``.  A positively reported
    unsynchronized clock is actionable and fails the report because it can
    invalidate Xbox/TLS authentication.
    """

    resolver = resolver or _resolved_addresses
    tls_probe = tls_probe or _tls_version
    runner = runner or subprocess.run
    timeout = max(0.05, float(timeout))
    endpoint_list = tuple(endpoints)
    endpoint_results = {}
    deadline = time.monotonic() + timeout

    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, len(endpoint_list)),
        thread_name_prefix="bol-netdiag",
    )
    futures = {}
    try:
        for index, (label, host, port) in enumerate(endpoint_list):
            future = executor.submit(
                _endpoint_checks, label, host, int(port), timeout, deadline,
                resolver, tls_probe,
            )
            futures[future] = (index, label, host)
        done, pending = concurrent.futures.wait(
            futures, timeout=max(0, deadline - time.monotonic()),
        )
        for future in done:
            index, label, host = futures[future]
            try:
                endpoint_results[index] = future.result()
            except Exception as exc:
                reason = str(exc).strip() or type(exc).__name__
                endpoint_results[index] = (
                    NetworkCheck("dns", host, False, f"{label}: {reason}"),
                    NetworkCheck("tls", host, False, f"{label}: {reason}"),
                )
        for future in pending:
            index, label, host = futures[future]
            future.cancel()
            detail = f"{label}: diagnostic timed out after {timeout:g}s"
            endpoint_results[index] = (
                NetworkCheck("dns", host, False, detail),
                NetworkCheck("tls", host, False, detail),
            )
    finally:
        # A stalled libc resolver must not hold up the CLI after our deadline.
        executor.shutdown(wait=False, cancel_futures=True)

    checks = []
    for index in range(len(endpoint_list)):
        checks.extend(endpoint_results[index])
    checks.append(_clock_check(runner, timeout))
    checks.append(_virtual_interface_check(runner, timeout))
    if host_ip is not None:
        checks.append(_route_check(host_ip, runner, timeout))
    # Asked last: it is the only check that spends a signed-in round-trip,
    # and it is worth nothing if the hosts above are unreachable anyway.
    try:
        snapshot = (social or presence.social_snapshot)()
    except Exception as exc:  # never let a diagnostic be the thing that fails
        checks.append(NetworkCheck(
            "xbox social", "peoplehub", None,
            "Not measured (%s)." % type(exc).__name__))
    else:
        checks.extend(_social_checks(snapshot))

    return all(check.ok is not False for check in checks), checks
