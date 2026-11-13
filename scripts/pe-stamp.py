#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Give every PE file below the given folders one fixed link time.

    pe-stamp.py EPOCH DIR [DIR ...]

Wine's ``make install INSTALL_PROGRAM_FLAGS=-s`` strips each PE module with
the MinGW strip, and strip writes the file again with the time of the run in
FileHeader.TimeDateStamp: binutils only takes SOURCE_DATE_EPOCH there from
2.41 on, and the engine is built with Debian 11's 2.35. Two builds of the
same source would then never be the same bytes. This puts EPOCH back, the
value the linker wrote before strip ran, and clears OptionalHeader.CheckSum,
which covers the timestamp and which Wine does not read. Nothing else in the
file is touched; files that are not PE images are left alone.
"""
import os
import struct
import sys


def stamp(path, epoch):
    with open(path, "r+b") as image:
        head = image.read(0x40)
        if len(head) < 0x40 or head[:2] != b"MZ":
            return False
        pe = struct.unpack_from("<I", head, 0x3C)[0]
        image.seek(pe)
        header = image.read(24 + 68)
        if len(header) < 24 + 68 or header[:4] != b"PE\0\0":
            return False
        if struct.unpack_from("<H", header, 24)[0] not in (0x10B, 0x20B):
            return False
        image.seek(pe + 8)                  # FileHeader.TimeDateStamp
        image.write(struct.pack("<I", epoch))
        image.seek(pe + 24 + 64)            # OptionalHeader.CheckSum
        image.write(struct.pack("<I", 0))
    return True


def main(argv):
    if len(argv) < 3 or not argv[1].isdigit():
        raise SystemExit(__doc__)
    epoch = int(argv[1])
    if epoch > 0xFFFFFFFF:
        raise SystemExit("EPOCH does not fit a PE timestamp")
    stamped = 0
    for root in argv[2:]:
        if not os.path.isdir(root):
            raise SystemExit("%s: not a directory" % root)
        for folder, _dirs, names in sorted(os.walk(root)):
            for name in sorted(names):
                path = os.path.join(folder, name)
                if os.path.islink(path) or not os.path.isfile(path):
                    continue
                stamped += stamp(path, epoch)
    if not stamped:
        raise SystemExit("no PE file below " + " ".join(argv[2:]))
    print("stamped %d PE files with %d" % (stamped, epoch))


if __name__ == "__main__":
    main(sys.argv)
