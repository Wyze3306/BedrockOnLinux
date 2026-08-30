#!/usr/bin/env python3
"""Write a macOS .icns from one PNG, without macOS.

``iconutil`` is the tool for this and it exists only on a Mac, which would
make the application bundle unbuildable anywhere else. The format it writes is
not complicated, though: an ``icns`` header followed by one chunk per icon
size, and every size modern macOS reads may hold a PNG verbatim. So this
writes those chunks directly.

Only sizes at or below the source resolution are emitted. Upscaling a 256px
icon into the 512 and 1024 slots would produce a blurry icon that macOS then
*prefers*, because it picks the largest slot it finds -- an icon that is worse
than the one it replaced.

Usage: scripts/png2icns.py SOURCE.png OUT.icns
"""
# SPDX-License-Identifier: MIT

import struct
import sys
from io import BytesIO
from pathlib import Path

# OSType -> pixel size. The @2x variants are separate slots holding the same
# pixel count as a larger @1x one, and macOS wants both: an icon missing its
# retina slots is rendered from the @1x one and looks soft on every Mac made
# in the last decade.
_SLOTS = (
    ("icp4", 16),    # 16x16
    ("icp5", 32),    # 32x32
    ("icp6", 64),    # 64x64
    ("ic07", 128),   # 128x128
    ("ic08", 256),   # 256x256
    ("ic09", 512),   # 512x512
    ("ic10", 1024),  # 512x512@2x
    ("ic11", 32),    # 16x16@2x
    ("ic12", 64),    # 32x32@2x
    ("ic13", 256),   # 128x128@2x
    ("ic14", 512),   # 256x256@2x
)


def build(source: Path, sizes=None):
    """Return the bytes of an .icns rendered from ``source``."""
    from PIL import Image

    with Image.open(source) as original:
        image = original.convert("RGBA")
        widest = min(image.size)
        rendered = {}
        chunks = []
        for ostype, size in _SLOTS:
            if size > widest and sizes is None:
                continue
            if sizes is not None and size not in sizes:
                continue
            if size not in rendered:
                scaled = image.resize((size, size), Image.LANCZOS)
                buffer = BytesIO()
                scaled.save(buffer, format="PNG")
                rendered[size] = buffer.getvalue()
            payload = rendered[size]
            chunks.append(ostype.encode("ascii")
                          + struct.pack(">I", len(payload) + 8)
                          + payload)

    if not chunks:
        raise SystemExit(f"{source}: too small for any icon size")
    body = b"".join(chunks)
    return b"icns" + struct.pack(">I", len(body) + 8) + body


def main(argv):
    if len(argv) != 3:
        raise SystemExit(__doc__.strip().splitlines()[-1])
    source, destination = Path(argv[1]), Path(argv[2])
    destination.write_bytes(build(source))
    print(f"wrote {destination}")


if __name__ == "__main__":
    main(sys.argv)
