"""bol.ed25519 — Ed25519 signature verification (RFC 8032).

The data feed (bol.feed) is trusted because of its signature and nothing
else, so verifying one must work on every install: `cryptography` does it when
it is importable, and the reference arithmetic below does it when it is not
(a .pyz on a host without the package). Verification only handles public
values, so the pure-Python path has no secret to leak through its timing.
Signing is not here: the launcher never holds a private key.
"""
# SPDX-License-Identifier: MIT

import hashlib

_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _recover_x(y, sign):
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _I % _P
    if (x * x - x2) % _P:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_GY = 4 * pow(5, _P - 2, _P) % _P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _add(a, b):
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    e = (y1 - x1) * (y2 - x2) % _P
    f = (y1 + x1) * (y2 + x2) % _P
    g = 2 * t1 * t2 * _D % _P
    h = 2 * z1 * z2 % _P
    return ((f - e) * (h - g) % _P, (h + g) * (f + e) % _P,
            (h - g) * (h + g) % _P, (f - e) * (f + e) % _P)


def _mul(scalar, point):
    result = (0, 1, 1, 0)
    while scalar:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def _decode(raw):
    if len(raw) != 32:
        return None
    y = int.from_bytes(raw, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def _same(a, b):
    return ((a[0] * b[2] - b[0] * a[2]) % _P == 0
            and (a[1] * b[2] - b[1] * a[2]) % _P == 0)


def _verify_pure(public, message, signature):
    if len(public) != 32 or len(signature) != 64:
        return False
    a = _decode(public)
    r = _decode(signature[:32])
    if a is None or r is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        return False
    h = int.from_bytes(
        hashlib.sha512(signature[:32] + public + message).digest(),
        "little") % _L
    return _same(_mul(s, _G), _add(r, _mul(h, a)))


def verify(public, message, signature, pure=False):
    """True when `signature` is `public`'s Ed25519 signature of `message`.

    Never raises: anything malformed is simply not a valid signature.
    """
    try:
        public, message, signature = \
            bytes(public), bytes(message), bytes(signature)
    except (TypeError, ValueError):
        return False
    if not pure:
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PublicKey,
            )
        except Exception:
            pass
        else:
            try:
                Ed25519PublicKey.from_public_bytes(public).verify(
                    signature, message)
                return True
            except (InvalidSignature, ValueError, TypeError):
                return False
    try:
        return _verify_pure(public, message, signature)
    except Exception:
        return False
