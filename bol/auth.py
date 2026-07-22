"""bol.auth — Microsoft / Xbox Live native login (MSA + pre-auth chain)."""
# SPDX-License-Identifier: MIT

import json
import os
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .config import (
    DATA,
    MSA_CLIENT_ID,
    MSA_CONNECT,
    MSA_DIR,
    MSA_SCOPE,
    MSA_TOKEN,
    WINEGDK_REG,
)
from .log import BolError, die, err, info, ok, warn
from .prefix import active_prefix
from .util import http_post_form, load_settings, save_settings
from .wine_registry import (
    reg_delete,
    reg_dword,
    reg_sz,
    update_prefix_registry,
)

def msa_load():
    f = MSA_DIR / "token.json"
    if f.is_file():
        try:
            return json.loads(f.read_text())
        except Exception:
            pass
    return {}


def msa_save(tok):
    MSA_DIR.mkdir(parents=True, exist_ok=True)
    p = MSA_DIR / "token.json"
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix=".token-", suffix=".tmp", dir=MSA_DIR)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(tok, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
        os.chmod(MSA_DIR, 0o700)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def msa_signed_in():
    return bool(msa_load().get("refresh_token"))


def msa_gamertag():
    if not msa_signed_in():
        return None
    try:
        j = json.loads((DATA / "winegdk-preauth" / "device.json").read_text())
        return j.get("xbl_gamertag")
    except Exception:
        return None


def msa_logout():
    """Forget account credentials without retaining reusable Xbox tokens."""
    try:
        # Rotate the account generation, purge Xbox tokens and remove the MSA
        # refresh token under one lock. An in-flight login/launch either wins
        # before this block (and is then deleted) or observes the new epoch and
        # is refused; it can never resurrect the old account afterwards.
        _purge_account_preauth(MSA_DIR / "token.json")
        if MSA_DIR.is_dir():
            try:
                fd = os.open(MSA_DIR, os.O_RDONLY
                             | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError:
                pass
    except Exception as exc:
        # Fail closed: keeping the current account linked is safer than showing
        # "signed out" while its old XSTS cache could survive for a new login.
        raise BolError("Could not safely clear the Microsoft/Xbox account "
                       "cache; sign-out was cancelled.") from exc
    return True


def msa_refresh(refresh_token):
    """Trade a refresh token for a fresh one (same shape WineGDK's XUser uses
    internally). Returns the token dict, or None if it was rejected."""
    t = http_post_form(MSA_TOKEN, {
        "client_id": MSA_CLIENT_ID, "scope": MSA_SCOPE,
        "grant_type": "refresh_token", "refresh_token": refresh_token})
    return t if t.get("refresh_token") else None


class NativeAuth:
    """MSA device-code login for the no-ProxyPass path. We only obtain an
    OAuth refresh token; WineGDK's XUser reads it from the prefix registry
    and performs the Xbox Live / XSTS exchange itself. GUI-compatible with
    ProxyPass (`.auth`, `.start`, `.stop`, `.running`)."""

    def __init__(self):
        self.auth = None
        self.online = False
        self.proc = None
        self.dest = None
        self._stop = False

    def running(self):
        return False

    def signed_in(self):
        return msa_signed_in()

    def start(self, on_auth=None, on_online=None, dest=None):
        if msa_signed_in():
            self.online = True
            if on_online:
                on_online()
            ok("Microsoft account already linked (in-game login)")
            return
        self._stop = False
        # Capture the account generation synchronously before the worker can
        # race a Sign-out. A token response from this flow may only be stored
        # while this exact generation is still current.
        account_epoch = _account_cache_epoch(DATA / "winegdk-preauth")
        threading.Thread(target=self._flow,
                          args=(on_auth, on_online, account_epoch),
                          daemon=True).start()

    def _flow(self, on_auth, on_online, account_epoch=None):
        try:
            if account_epoch is None:
                account_epoch = _account_cache_epoch(
                    DATA / "winegdk-preauth")
            d = http_post_form(MSA_CONNECT, {
                "client_id": MSA_CLIENT_ID, "scope": MSA_SCOPE,
                "response_type": "device_code"})
            if "device_code" not in d:
                die("Microsoft device-code request failed: "
                    f"{d.get('error_description') or d.get('error') or d}")
            url = d.get("verification_uri") or "https://www.microsoft.com/link"
            code = d.get("user_code")
            self.auth = (url, code)
            if on_auth:
                on_auth(url, code)
            info(f"Microsoft sign-in → {url} code {code}")
            interval = max(int(d.get("interval", 5) or 5), 1)
            deadline = time.time() + int(d.get("expires_in", 900) or 900)
            dc = d["device_code"]
            while not self._stop and time.time() < deadline:
                time.sleep(interval)
                if self._stop:
                    return
                # Legacy live.com grant string — matches WineGDK XUser.c.
                t = http_post_form(MSA_TOKEN, {
                    "client_id": MSA_CLIENT_ID,
                    "grant_type": "device_code", "device_code": dc})
                if self._stop:
                    return
                e = t.get("error")
                if e == "authorization_pending":
                    continue
                if e == "slow_down":
                    interval += 5
                    continue
                if e:
                    die(f"Microsoft sign-in failed: "
                        f"{t.get('error_description') or e}")
                if t.get("refresh_token"):
                    token = {"refresh_token": t["refresh_token"],
                             "obtained": int(time.time())}
                    if not msa_save_for_account_epoch(token, account_epoch):
                        warn("Microsoft sign-in response arrived after the "
                             "account was signed out; discarded it.")
                        return
                    if self._stop or _account_cache_epoch(
                            DATA / "winegdk-preauth") != account_epoch:
                        return
                    self.auth = None
                    self.online = True
                    if on_online:
                        on_online()
                    ok("Microsoft account linked (in-game login)")
                    return
            if not self._stop:
                warn("Microsoft sign-in timed out — click 'Sign in' again.")
        except BolError:
            pass
        except Exception as ex:
            err(f"Native login error: {ex}")

    def stop(self):
        self._stop = True


class _HttpResp:
    """Minimal requests-style response built on urllib, so xbl_preauth can drop
    the third-party `requests` dependency — only cryptography remains."""

    def __init__(self, status_code, raw):
        self.status_code = status_code
        self._raw = raw
        self.text = raw.decode("utf-8", "replace")

    def json(self):
        return json.loads(self._raw)


_ONLINE_PREAUTH_REQUIREMENTS = {
    "device_token": "device_token_expiry",
    "user_token": "user_token_expiry",
    "xbl_token": "xbl_token_expiry",
    "sisu_token": "sisu_expiry",
    "sisu_rp": None,
    "sisu_uhs": None,
    "mp_token": "mp_expiry",
    "mp_rp": None,
    "mp_uhs": None,
    "realms_token": "realms_expiry",
    "realms_rp": None,
    "realms_uhs": None,
    "xbl_xuid": None,
}


_WINEGDK_EXPIRY_EPOCH_FIELDS = {
    "user_token_expiry": "user_token_expiry_epoch",
    "xbl_token_expiry": "xbl_token_expiry_epoch",
    "sisu_expiry": "sisu_expiry_epoch",
    "mp_expiry": "mp_expiry_epoch",
    "realms_expiry": "realms_expiry_epoch",
    "lic_expiry": "lic_expiry_epoch",
}


_XBOX_SUBMICROSECOND_FRACTION = re.compile(
    r"(\.\d{6})\d+(?=(?:Z|[+-]\d{2}:\d{2})?$)"
)


def _normalize_xbox_expiry(raw):
    """Convert Xbox's ISO timestamp to Python 3.9's accepted grammar."""
    normalized = _XBOX_SUBMICROSECOND_FRACTION.sub(r"\1", raw.strip())
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    return normalized


def _parse_xbox_expiry(raw):
    """Parse an Xbox ``NotAfter`` timestamp on every supported Python.

    Xbox currently returns seven fractional-second digits (100 ns precision),
    while :meth:`datetime.datetime.fromisoformat` on Python 3.9 accepts only
    three or six.  Discarding precision below one microsecond is harmless for
    expiry checks and keeps the portable zipapp compatible with Python 3.9.
    """
    from datetime import datetime

    return datetime.fromisoformat(_normalize_xbox_expiry(raw))


def _normalize_xbl_privileges(raw):
    """Return an optional, canonical Xbox privilege claim.

    Xbox currently exposes ``DisplayClaims.xui[0].prv`` as a space-separated
    string.  Accept a sequence as well so the launcher remains tolerant of a
    service-side representation change, but only retain unsigned decimal IDs.
    Keeping this as a canonical string makes it straightforward for WineGDK's
    small JSON reader to consume without trusting the original claim shape.
    """
    if isinstance(raw, str):
        values = re.split(r"[\s,]+", raw.strip()) if raw.strip() else []
    elif isinstance(raw, (list, tuple)):
        values = raw
    else:
        return None

    privileges = set()
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            privilege = value
        elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
            # Xbox privilege values are 32-bit enum IDs.  Bound both the text
            # length and parsed value so a malformed claim cannot grow an
            # arbitrarily large Python integer.
            if len(value) > 10:
                continue
            privilege = int(value, 10)
        else:
            continue
        if 0 <= privilege <= 0xffffffff:
            privileges.add(privilege)
    if not privileges:
        return None
    return " ".join(str(privilege) for privilege in sorted(privileges))


def _xbl_privilege_claim(claims):
    """Return ``(present, canonical_value)`` for an XBL ``prv`` claim.

    Presence is intentionally separate from the value: a legacy cache has no
    privilege information and needs WineGDK's compatibility fallback, whereas
    an explicit empty or malformed service claim means that no privileges were
    granted and must fail closed.
    """
    if not isinstance(claims, dict) or "prv" not in claims:
        return False, None
    return True, _normalize_xbl_privileges(claims.get("prv")) or ""


def _xbl_gamertag_claims(claims):
    """Return optional modern gamertag claims under launcher cache keys."""
    if not isinstance(claims, dict):
        return {}
    result = {}
    for claim, field in (
        ("mgt", "xbl_modern_gamertag"),
        ("mgs", "xbl_modern_gamertag_suffix"),
        ("umg", "xbl_unique_modern_gamertag"),
    ):
        value = claims.get(claim)
        if isinstance(value, str):
            result[field] = value
    return result


def _with_winegdk_expiry_epochs(payload):
    """Return a copy with WineGDK's decimal epoch expiry fields.

    The ISO ``NotAfter`` values remain the source of truth for launcher-side
    validation.  WineGDK cannot parse those timestamps directly, so its
    pre-auth loader consumes these derived string fields instead.
    """
    from datetime import timezone

    if not isinstance(payload, dict):
        return payload
    enriched = dict(payload)
    for iso_field, epoch_field in _WINEGDK_EXPIRY_EPOCH_FIELDS.items():
        raw = enriched.get(iso_field)
        try:
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError
            stamp = _parse_xbox_expiry(raw)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            enriched[epoch_field] = str(int(stamp.timestamp()))
        except (ValueError, OverflowError, OSError):
            # Never preserve an epoch that disagrees with a missing or invalid
            # ISO source value. Required ISO fields are still rejected by
            # _online_preauth_problems below.
            enriched.pop(epoch_field, None)
    return enriched


def _online_preauth_problems(payload, now=None, min_ttl=60):
    """Describe missing or expired fields in an online pre-auth payload."""
    from datetime import timezone

    if not isinstance(payload, dict):
        return ["invalid JSON object"]
    now = time.time() if now is None else now
    problems = []
    for field, expiry_field in _ONLINE_PREAUTH_REQUIREMENTS.items():
        if not payload.get(field):
            problems.append(f"missing {field}")
            continue
        if not expiry_field:
            continue
        raw = payload.get(expiry_field)
        if not isinstance(raw, str) or not raw.strip():
            problems.append(f"missing {expiry_field}")
            continue
        try:
            stamp = _parse_xbox_expiry(raw)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            if stamp.timestamp() <= now + min_ttl:
                problems.append(f"expired {field}")
        except (ValueError, OverflowError):
            problems.append(f"invalid {expiry_field}")
    return problems


def _load_online_preauth(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return {}


@contextmanager
def _account_cache_lock(cache):
    """Serialize account-token stores and logout purges."""
    import fcntl

    cache.mkdir(parents=True, exist_ok=True)
    lock_path = cache / ".account-cache.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _account_cache_epoch(cache):
    """Return the logout generation; legacy installs have no marker yet."""
    marker = cache / ".account-epoch"
    try:
        value = marker.read_text().strip()
    except FileNotFoundError:
        return "legacy"
    except OSError:
        return None
    if re.fullmatch(r"[0-9a-f]{32}", value):
        return value
    return None


def _cached_account_matches(payload, epoch):
    if (epoch is None or not isinstance(payload, dict)
            or (epoch != "legacy"
                and not re.fullmatch(r"[0-9a-f]{32}", epoch))):
        return False
    return payload.get("_account_epoch", "legacy") == epoch


def msa_save_for_account_epoch(token, expected_epoch):
    """Store a login response only if no logout rotated its generation.

    The same cache lock serializes this check with ``_purge_account_preauth``.
    If saving wins, the following logout deletes the token; if logout wins,
    the generation mismatch rejects the in-flight response.
    """

    if (expected_epoch != "legacy"
            and (not isinstance(expected_epoch, str)
                 or not re.fullmatch(r"[0-9a-f]{32}", expected_epoch))):
        return False
    cache = DATA / "winegdk-preauth"
    with _account_cache_lock(cache):
        if _account_cache_epoch(cache) != expected_epoch:
            return False
        msa_save(token)
    return True


def msa_session_snapshot():
    """Read the refresh token and account generation atomically."""

    cache = DATA / "winegdk-preauth"
    with _account_cache_lock(cache):
        return msa_load(), _account_cache_epoch(cache)


def account_epoch_is_current(expected_epoch):
    """Check a launch's account generation under the same logout lock."""

    cache = DATA / "winegdk-preauth"
    with _account_cache_lock(cache):
        return (_account_cache_epoch(cache) == expected_epoch
                and expected_epoch is not None)


def _purge_account_preauth(msa_token_path=None):
    """Invalidate and remove account-bound XSTS data, preserving device keys."""
    import tempfile

    cache = DATA / "winegdk-preauth"
    cache.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(cache, 0o700)
    except OSError:
        pass
    with _account_cache_lock(cache):
        # Rotate first. If power is lost before unlink, any old device.json is
        # still rejected on the next run because its generation no longer
        # matches. device-key.pem and device-id.txt deliberately survive.
        fd, tmp = tempfile.mkstemp(prefix=".account-epoch-", suffix=".tmp",
                                   dir=cache)
        staged = tmp
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(os.urandom(16).hex() + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(staged, 0o600)
            os.replace(staged, cache / ".account-epoch")
            staged = None
        finally:
            if staged is not None:
                try:
                    os.unlink(staged)
                except OSError:
                    pass

        (cache / "device.json").unlink(missing_ok=True)
        # A hard power-off may leave a pre-rename file containing the same
        # account tokens. It is never loaded, but remove it on logout as well.
        for stale in cache.glob(".device-*.tmp"):
            stale.unlink(missing_ok=True)
        if msa_token_path is not None:
            Path(msa_token_path).unlink(missing_ok=True)
        try:
            dir_fd = os.open(cache, os.O_RDONLY
                             | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass


def _store_online_preauth(path, payload, expected_epoch=None):
    """Atomically persist a complete online payload; never store a partial."""
    payload = _with_winegdk_expiry_epochs(payload)
    if _online_preauth_problems(payload):
        return False
    import tempfile
    with _account_cache_lock(path.parent):
        current_epoch = _account_cache_epoch(path.parent)
        if current_epoch is None:
            return False
        if (expected_epoch is not None
                and current_epoch != expected_epoch):
            return False
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".device-",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(payload, indent=2))
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return True


def xbl_preauth(msa_access_token, expected_account_epoch=None):
    """Run the whole Xbox Live auth chain (device + user + SISU tokens) from
    the host's OpenSSL stack and persist it as winegdk-preauth/device.json,
    where xgameruntime.dll short-circuits its own HTTP calls.

    Needed because Azure TCP-RSTs every *.auth.xboxlive.com / sisu call made
    through Wine's GnuTLS (fingerprinted as non-Schannel) — the same requests
    from the host succeed. Returns True only when a complete, unexpired online
    payload (including the multiplayer and Realms XSTS tokens) is available.
    A failed refresh never overwrites a previously valid payload with
    device-only data.
    """
    import base64, uuid as _uuid
    cache = DATA / "winegdk-preauth"
    cache.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(cache, 0o700)
    except OSError:
        pass
    out_path = cache / "device.json"
    current_epoch = _account_cache_epoch(cache)
    account_epoch = (current_epoch if expected_account_epoch is None
                     else expected_account_epoch)
    if current_epoch != account_epoch or account_epoch is None:
        warn("xbl_preauth: the Microsoft account changed before Xbox "
             "pre-auth started; refusing stale credentials.")
        return False
    if account_epoch is None:
        warn("Xbox Live pre-auth: account-cache generation is invalid or "
             "unreadable; refusing cached credentials. Sign out and sign in "
             "again to rebuild it safely.")
        return False
    cached = _load_online_preauth(out_path)
    cached_ready = (_cached_account_matches(cached, account_epoch)
                    and not _online_preauth_problems(cached))

    def _fallback(message):
        warn(message)
        current_epoch = _account_cache_epoch(cache)
        current_ready = (
            current_epoch == account_epoch
            and _cached_account_matches(cached, account_epoch)
            and not _online_preauth_problems(cached)
        )
        if current_ready:
            upgraded = _with_winegdk_expiry_epochs(cached)
            if upgraded != cached:
                if not _store_online_preauth(
                        out_path, upgraded, expected_epoch=account_epoch):
                    warn("Xbox Live pre-auth: account changed while upgrading "
                         "the cached WineGDK credentials; refusing the old "
                         "online payload.")
                    return False
                cached.clear()
                cached.update(upgraded)
                info("Xbox Live pre-auth: upgraded cached token expirations "
                     "for WineGDK.")
            info("Xbox Live pre-auth: keeping the complete unexpired cached "
                 "online tokens.")
            return True
        if cached_ready and current_epoch == account_epoch:
            warn("Xbox Live pre-auth: cached online tokens expired while the "
                 "refresh was in progress; refusing stale credentials.")
        return False

    if not msa_access_token:
        return _fallback("xbl_preauth: no fresh Microsoft access token; cannot "
                         "refresh the Xbox multiplayer chain.")
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import hashes, serialization
    except ImportError as e:
        return _fallback(f"xbl_preauth: missing Python dep ({e}) — cannot "
                         "refresh online tokens.")
    key_path = cache / "device-key.pem"
    # Reuse persisted EC P-256 key + UUID across launches so Xbox Live sees
    # the same device on every session.
    if key_path.exists() and (cache / "device-id.txt").exists():
        try:
            with open(key_path, "rb") as f:
                priv = serialization.load_pem_private_key(f.read(), password=None)
            device_id = (cache / "device-id.txt").read_text().strip()
        except Exception:
            priv = None; device_id = None
    else:
        priv = None; device_id = None
    if priv is None:
        priv = ec.generate_private_key(ec.SECP256R1())
        device_id = "{" + str(_uuid.uuid4()) + "}"
        with open(key_path, "wb") as f:
            f.write(priv.private_bytes(serialization.Encoding.PEM,
                                       serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
        (cache / "device-id.txt").write_text(device_id)
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    pub_numbers = priv.public_key().public_numbers()
    x_b64 = base64.b64encode(pub_numbers.x.to_bytes(32, "big")).decode()
    y_b64 = base64.b64encode(pub_numbers.y.to_bytes(32, "big")).decode()
    proof_key = {"alg": "ES256", "crv": "P-256", "kty": "EC",
                 "use": "sig", "x": x_b64, "y": y_b64}
    # Build the Xbox Live signature blob — wire format is ver(4) + ts(8) +
    # raw ECDSA-P-256 r||s (64), 76 bytes total. The bytes SIGNED are a
    # hash input that puts 0x00 separators between every field:
    #   ver(4) || \0 || ts(8) || \0 || method || \0 || path || \0 || auth || \0 || body || \0
    # SHA-256 of this is what gets signed (matches Wine-side
    # DeviceAuth_SignRequest in dlls/xgameruntime/.../DeviceAuth.c).
    import time as _time
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    def _sign_header(method, path, body_bytes):
        now_ft = int((_time.time() + 11644473600) * 1e7)
        ver = (1).to_bytes(4, "big")
        ts = now_ft.to_bytes(8, "big")
        hash_input = (ver + b"\0" + ts + b"\0"
                      + method.encode() + b"\0"
                      + path.encode() + b"\0"
                      + b"" + b"\0"
                      + body_bytes + b"\0")
        sig_der = priv.sign(hash_input, ec.ECDSA(hashes.SHA256()))
        r2, s2 = decode_dss_signature(sig_der)
        sig_raw = r2.to_bytes(32, "big") + s2.to_bytes(32, "big")
        return base64.b64encode(ver + ts + sig_raw).decode()
    def _xbl_post(url, body_dict):
        import urllib.error
        import urllib.request
        from urllib.parse import urlparse
        body_bytes = json.dumps(body_dict, separators=(",", ":")).encode()
        path = urlparse(url).path
        req = urllib.request.Request(url, data=body_bytes, method="POST",
            headers={
                "User-Agent": "XAL Xbox Live Game (Windows; SDK; 1.0.0.0)",
                "Content-Type": "application/json",
                "x-xbl-contract-version": "1",
                "Signature": _sign_header("POST", path, body_bytes),
            })
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return _HttpResp(resp.status, resp.read())
        except urllib.error.HTTPError as e:
            return _HttpResp(e.code, e.read())

    try:
        r = _xbl_post("https://device.auth.xboxlive.com/device/authenticate", {
            "RelyingParty": "http://auth.xboxlive.com",
            "TokenType": "JWT",
            "Properties": {
                "AuthMethod": "ProofOfPossession",
                "Id": device_id,
                "DeviceType": "Win32",
                "Version": "10.0.22631",
                "ProofKey": proof_key,
            },
        })
    except Exception as e:
        return _fallback(f"xbl_preauth: device.auth POST failed: {e}")
    if r.status_code != 200:
        return _fallback(f"xbl_preauth: device.auth HTTP {r.status_code}")
    j = r.json()
    device_token = j["Token"]

    user_token = None
    user_token_expiry = None
    if msa_access_token:
        try:
            ru = _xbl_post("https://user.auth.xboxlive.com/user/authenticate", {
                "RelyingParty": "http://auth.xboxlive.com",
                "TokenType": "JWT",
                "Properties": {
                    "AuthMethod": "RPS",
                    "SiteName": "user.auth.xboxlive.com",
                    "RpsTicket": "t=" + msa_access_token,
                },
            })
            if ru.status_code == 200:
                uj = ru.json()
                user_token = uj["Token"]
                user_token_expiry = uj.get("NotAfter", "")
            else:
                warn(f"xbl_preauth: user.auth HTTP {ru.status_code}")
        except Exception as e:
            warn(f"xbl_preauth: user.auth POST failed: {e}")

    # ---- 3a. sisu /authorize for http://xboxlive.com ----
    # Returns the DisplayClaims (xid, gtg, agg, …) LoadDefaultUser needs to
    # populate the XUser handle (its own xsts.auth call would RST under Wine).
    def _sisu(rp):
        if not msa_access_token: return None
        try:
            r = _xbl_post("https://sisu.xboxlive.com/authorize", {
                "AccessToken": "t=" + msa_access_token,
                "AppId": "0000000048183522",
                "deviceToken": device_token,
                "Sandbox": "RETAIL",
                "UseModernGamertag": True,
                "SiteName": "user.auth.xboxlive.com",
                "RelyingParty": rp,
                "OfferTermsAcceptance": True,
                "AcceptOffers": True,
                "ProofKey": proof_key,
            })
            if r.status_code != 200:
                warn(f"xbl_preauth: sisu({rp}) HTTP {r.status_code}")
                return None
            return r.json()
        except Exception as e:
            warn(f"xbl_preauth: sisu({rp}) failed: {e}")
            return None

    xbl_sisu = _sisu("http://xboxlive.com") or {}
    xbl_auth = xbl_sisu.get("AuthorizationToken", {}) if xbl_sisu else {}
    xbl_token = xbl_auth.get("Token")
    xbl_expiry = xbl_auth.get("NotAfter", "") if xbl_auth else ""
    xbl_claims = {}
    try:
        xbl_claims = xbl_auth["DisplayClaims"]["xui"][0]
    except (KeyError, IndexError, TypeError):
        pass
    xbl_privileges_present, xbl_privileges = _xbl_privilege_claim(xbl_claims)

    pf_sisu = _sisu("https://b980a380.minecraft.playfabapi.com/") or {}
    pf_auth = pf_sisu.get("AuthorizationToken", {}) if pf_sisu else {}
    sisu_rp = "https://b980a380.minecraft.playfabapi.com/" if pf_auth.get("Token") else None
    sisu_token = pf_auth.get("Token")
    sisu_expiry = pf_auth.get("NotAfter", "")
    sisu_uhs = None
    try:
        sisu_uhs = pf_auth["DisplayClaims"]["xui"][0].get("uhs")
    except (KeyError, IndexError, TypeError):
        pass

    # ---- 3c. sisu /authorize for the multiplayer RP, used when joining a
    # third-party server — without a pre-minted token the live SISU call RSTs
    # and the join fails (pings still work).
    mp_sisu = _sisu("https://multiplayer.minecraft.net/") or {}
    mp_auth = mp_sisu.get("AuthorizationToken", {}) if mp_sisu else {}
    mp_rp = "https://multiplayer.minecraft.net/" if mp_auth.get("Token") else None
    mp_token = mp_auth.get("Token")
    mp_expiry = mp_auth.get("NotAfter", "")
    mp_uhs = None
    try:
        mp_uhs = mp_auth["DisplayClaims"]["xui"][0].get("uhs")
    except (KeyError, IndexError, TypeError):
        pass

    # ---- 3d. sisu /authorize for the Bedrock Realms RP.  The current game
    # reaches Realms through *.realms.minecraft-services.net, but that service
    # still validates an XSTS token whose audience is the canonical legacy
    # Bedrock RP.  A generic http://xboxlive.com token reaches the edge but is
    # rejected with HTTP 401.
    realms_relying_party = "https://pocket.realms.minecraft.net/"
    realms_sisu = _sisu(realms_relying_party) or {}
    realms_auth = (realms_sisu.get("AuthorizationToken", {})
                   if realms_sisu else {})
    realms_rp = realms_relying_party if realms_auth.get("Token") else None
    realms_token = realms_auth.get("Token")
    realms_expiry = realms_auth.get("NotAfter", "")
    realms_uhs = None
    try:
        realms_uhs = realms_auth["DisplayClaims"]["xui"][0].get("uhs")
    except (KeyError, IndexError, TypeError):
        pass

    # ---- 3e. sisu /authorize for the licensing RP, used by the in-game
    # Marketplace — its catalog/entitlement edges (collections/purchase.
    # mp.microsoft.com, inventory/licensing.xboxlive.com) only accept an XSTS
    # token minted for http://licensing.xboxlive.com. Pre-mint it here so the
    # store catalog loads instead of hanging on a live SISU call (which RSTs
    # under Wine GnuTLS).
    lic_sisu = _sisu("http://licensing.xboxlive.com") or {}
    lic_auth = lic_sisu.get("AuthorizationToken", {}) if lic_sisu else {}
    lic_rp = "http://licensing.xboxlive.com" if lic_auth.get("Token") else None
    lic_token = lic_auth.get("Token")
    lic_expiry = lic_auth.get("NotAfter", "")
    lic_uhs = None
    try:
        lic_uhs = lic_auth["DisplayClaims"]["xui"][0].get("uhs")
    except (KeyError, IndexError, TypeError):
        pass

    # Export the EC P-256 key as BCRYPT_ECCPRIVATE_BLOB so xgameruntime.dll
    # can BCryptImportKeyPair() it byte-for-byte. Layout (104 bytes):
    #   dwMagic (LE u32 = BCRYPT_ECDSA_PRIVATE_P256_MAGIC 0x32534345)
    #   cbKey   (LE u32 = 32)
    #   X       (32 big-endian)
    #   Y       (32 big-endian)
    #   d       (32 big-endian, the private scalar)
    priv_d = priv.private_numbers().private_value
    ecc_blob = (
        (0x32534345).to_bytes(4, "little") + (32).to_bytes(4, "little")
        + pub_numbers.x.to_bytes(32, "big")
        + pub_numbers.y.to_bytes(32, "big")
        + priv_d.to_bytes(32, "big")
    )
    out = {
        # Logout rotates this non-secret generation before deleting the file.
        # It prevents an in-flight request or a crash-left cache from crossing
        # into the next Microsoft account.
        "_account_epoch": account_epoch,
        "device_id": device_id,
        "ecc_private_blob_b64": base64.b64encode(ecc_blob).decode(),
        "device_token": device_token,
        "device_token_expiry": j.get("NotAfter", ""),
        "user_token": user_token,
        "user_token_expiry": user_token_expiry,
        "xbl_token": xbl_token,
        "xbl_token_expiry": xbl_expiry,
        "xbl_xuid": xbl_claims.get("xid"),
        "xbl_gamertag": xbl_claims.get("gtg"),
        "xbl_age_group": xbl_claims.get("agg"),
        "xbl_uhs": xbl_claims.get("uhs"),
        "sisu_rp": sisu_rp,
        "sisu_token": sisu_token,
        "sisu_uhs": sisu_uhs,
        "sisu_expiry": sisu_expiry,
        "mp_rp": mp_rp,
        "mp_token": mp_token,
        "mp_uhs": mp_uhs,
        "mp_expiry": mp_expiry,
        "realms_rp": realms_rp,
        "realms_token": realms_token,
        "realms_uhs": realms_uhs,
        "realms_expiry": realms_expiry,
        "lic_rp": lic_rp,
        "lic_token": lic_token,
        "lic_uhs": lic_uhs,
        "lic_expiry": lic_expiry,
        "obtained": int(_time.time()),
    }
    # Modern gamertag components are optional SISU claims.  Keep them
    # separate because GDK callers provide component-specific buffer sizes;
    # returning the classic tag for ModernSuffix can fail XblContext setup.
    out.update(_xbl_gamertag_claims(xbl_claims))
    # Optional for backwards compatibility: old, otherwise complete caches do
    # not carry this claim and remain valid.  New caches expose it to WineGDK
    # without storing or logging the full DisplayClaims object.
    if xbl_privileges_present:
        out["xbl_privileges"] = xbl_privileges
    problems = _online_preauth_problems(out)
    if problems:
        return _fallback("xbl_preauth: incomplete online chain ("
                         + ", ".join(problems) + "); refusing to replace "
                         "device.json with a partial payload.")
    # Atomic write: two launches (or a launch racing a stale one) both ran
    # xbl_preauth and a plain write_text let their output interleave, leaving a
    # corrupted xbl_xuid in device.json — which the game then loads and faults
    # on. Write to a temp file in the same dir and rename, so a reader only ever
    # sees a complete file and the last writer wins cleanly.
    if not _store_online_preauth(out_path, out,
                                 expected_epoch=account_epoch):
        return _fallback("xbl_preauth: account changed while refreshing; "
                         "refusing to store or reuse the old online payload.")
    bits = ["device"]
    if user_token: bits.append("user")
    if xbl_token: bits.append("XBL")
    if sisu_token: bits.append("SISU-pf")
    if mp_token: bits.append("SISU-mp")
    if realms_token: bits.append("SISU-realms")
    if lic_token: bits.append("SISU-lic")
    ok(f"Xbox Live pre-auth: {', '.join(bits)}")
    return True


def wine_reg_set_refresh_token(token):
    """Seed the MSA refresh token where WineGDK's XUser reads it
    (HKLM\\Software\\Wine\\WineGDK 'RefreshToken').  The prefix is offline at
    this point, so write ``system.reg`` atomically instead of starting a whole
    UMU/Wine/Explorer session merely to run ``reg.exe``.  Apart from being much
    faster, this avoids a second graphics-driver initialisation before the
    actual game."""
    if not isinstance(token, str) or not token or "\x00" in token:
        warn("Could not write WineGDK RefreshToken: invalid token value.")
        return False
    try:
        update_prefix_registry(
            active_prefix(),
            machine=[reg_sz(WINEGDK_REG, "RefreshToken", token)],
        )
    except Exception as e:
        # Never include the token in an exception/log message.
        warn(f"Could not write WineGDK RefreshToken offline: {type(e).__name__}")
        return False
    ok("In-game login token written to the offline Wine prefix")
    return True


def wine_apply_winegdk_prereqs():
    """Registry prereqs: ConsoleMode=8 (console enum → the XSAPI code path;
    1 = Win32 PC would block the Servers tab as a 'dev build'), TLS 1.2
    forced, and the WindowsAppRuntime UI-mute env vars in HKCU\\Environment
    (pressure-vessel filters MICROSOFT_* out of the host env)."""
    machine = [
        reg_dword(r"Software\Microsoft\Windows NT\CurrentVersion\OEM",
                  "ConsoleMode", 8),
    ]
    # Azure rejects Wine GnuTLS' TLS 1.3 handshake (7-byte fatal Alert →
    # 0x80090304); forcing TLS 1.2 via DefaultSecureProtocols lets the
    # SISU/XSTS and PlayFab POSTs through.
    machine.extend([
        reg_dword(
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings\WinHttp",
            "DefaultSecureProtocols", 2560),
        reg_dword(
            r"Software\Microsoft\SchannelTLS\Protocols\TLS 1.3\Client",
            "DisabledByDefault", 1),
    ])
    user = []
    for name, val in (
        ("MICROSOFT_WINDOWSAPPRUNTIME_BOOTSTRAP_INITIALIZE_SHOWUI", "0"),
        ("MICROSOFT_WINDOWSAPPRUNTIME_BOOTSTRAP_INITIALIZE_FAILFAST", "0"),
        ("MICROSOFT_WINDOWSAPPRUNTIME_DEPLOYMENT_INITIALIZE_ONERRORSHOWUI",
         "0"),
    ):
        user.append(reg_sz("Environment", name, val))
    # Issue #26: in windowed mode the mouse cursor escapes the game window when
    # the player looks around. Minecraft confines the cursor with ClipCursor
    # during mouse-look, but under Wine that grab is unreliable for an
    # individually-managed top-level window (the compositor/focus handshake can
    # drop it), so the OS pointer drifts out — only visible when the game isn't
    # fullscreen. Running inside a Wine virtual desktop gives Wine a single
    # owning X window it fully controls, which makes ClipCursor confine to the
    # game window reliably. Opt-in (setting `confine_cursor` / env
    # BOL_CONFINE_CURSOR=1) because it changes windowing for the whole prefix.
    # A persisted marker lets the common path revert cleanly on toggle-off.
    from .util import _screen_wh
    confine = (os.environ.get("BOL_CONFINE_CURSOR", "").lower()
               in ("1", "yes", "on", "true")
               or load_settings().get("confine_cursor", False))
    applied = load_settings().get("_confine_applied", False)
    if confine:
        # Re-ensure every launch (not just on the enable edge) so the keys
        # survive a prefix reset, which recreates the prefix and would wipe
        # them.
        wh = _screen_wh() or ("1920", "1080")
        user.extend([
            reg_sz(r"Software\Wine\Explorer", "Desktop", "Default"),
            reg_sz(r"Software\Wine\Explorer\Desktops", "Default",
                   f"{wh[0]}x{wh[1]}"),
        ])
    elif applied:
        user.extend([
            reg_delete(r"Software\Wine\Explorer", "Desktop"),
            reg_delete(r"Software\Wine\Explorer\Desktops", "Default"),
        ])

    try:
        update_prefix_registry(active_prefix(), machine=machine, user=user)
    except Exception as e:
        die("Could not configure the offline WineGDK registry safely "
            f"({type(e).__name__}). Repair the managed Wine prefix and try "
            "again.")

    if confine:
        if not applied:
            s2 = load_settings()
            s2["_confine_applied"] = True
            save_settings(s2)
        ok(f"Cursor confinement ON (virtual desktop {wh[0]}x{wh[1]}).")
    elif applied:
        s2 = load_settings()
        s2["_confine_applied"] = False
        save_settings(s2)
        ok("Cursor confinement OFF (virtual desktop removed).")
    ok("WineGDK prereqs applied offline (ConsoleMode=8, TLS 1.2 forced, "
       "UI muted)")
