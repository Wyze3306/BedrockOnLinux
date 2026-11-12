"""bol.storeart — each edition's own logo, as the Microsoft Store shows it.

The launcher's edition picker shows the game's real icon rather than one
drawn here. The Store's public catalogue (displaycatalog.mp.microsoft.com,
the one the Store app and xodus read) lists a product's pictures; the
largest "Logo" is fetched once, scaled down by the CDN, and kept in the
cache for a month. Offline, or for a product the catalogue does not know,
there is simply no logo and the picker draws a neutral tile.
"""
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.parse
import urllib.request

from .config import APP, CACHE, VERSION

CATALOG = "https://displaycatalog.mp.microsoft.com/v7.0/products"
ART_CACHE = CACHE / "store-art"
_TTL = 30 * 86400
_MAX_BYTES = 1 << 20
_SIZE = 128
_ALLOWED_HOSTS = ("store-images.s-microsoft.com",)


def _get(url, timeout=15):
    request = urllib.request.Request(
        url, headers={"User-Agent": f"{APP}/{VERSION}"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(_MAX_BYTES + 1)


def logo_url(product):
    """The address of a product's largest Store logo, or None."""
    query = urllib.parse.urlencode({"bigIds": product, "market": "US",
                                    "languages": "en-us"})
    data = json.loads(_get(f"{CATALOG}?{query}").decode("utf-8"))
    products = data.get("Products") or []
    if not products:
        return None
    images = []
    for props in products[0].get("LocalizedProperties") or []:
        images += [i for i in props.get("Images") or []
                   if i.get("ImagePurpose") == "Logo" and i.get("Uri")]
    if not images:
        return None
    best = max(images, key=lambda i: int(i.get("Width") or 0))
    uri = best["Uri"]
    url = "https:" + uri if uri.startswith("//") else uri
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in _ALLOWED_HOSTS:
        return None
    return f"{url}?w={_SIZE}&h={_SIZE}"


def logo_bytes(product, refresh=False):
    """The picture bytes of a product's logo, cached; None when unknown."""
    if not product:
        return None
    path = ART_CACHE / f"{product}.img"
    try:
        fresh = time.time() - path.stat().st_mtime < _TTL
    except OSError:
        fresh = False
    if fresh and not refresh:
        try:
            return path.read_bytes()
        except OSError:
            pass
    try:
        url = logo_url(product)
        data = _get(url) if url else None
    except Exception:  # noqa: BLE001 - offline: last copy, or no logo
        data = None
    if not data or len(data) > _MAX_BYTES:
        try:
            return path.read_bytes()
        except OSError:
            return None
    try:
        ART_CACHE.mkdir(parents=True, exist_ok=True)
        fd, staged = tempfile.mkstemp(dir=ART_CACHE, prefix=".art-")
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(staged, path)
    except OSError:
        pass
    return data


def logos(editions):
    """``{edition id: bytes}`` for every edition with a logo."""
    out = {}
    for entry in editions:
        data = logo_bytes(entry.get("product"))
        if data:
            out[entry["id"]] = data
    return out
