#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Give every COFF object inside the static libraries below DIR one fixed time.

    coff-stamp.py EPOCH DIR [DIR ...]

The ARM64EC build (BOL_WINEGDK_TARGET=aarch64) compiles Wine's PE side with
clang for the *-windows targets, and clang's integrated assembler writes the
wall-clock time into each COFF object's FileHeader.TimeDateStamp there (it
keeps MSVC's "incremental linker compatible" default). The linked modules
are put right by pe-stamp.py, but the objects Wine installs inside its
lib*.a archives keep the time of the build, so two builds of the same source
differed in those archives only. This writes EPOCH there instead. The archive
members' sizes and offsets do not move; import-library short objects, the
symbol tables and anything that is not a COFF object are left alone.
"""
import os
import struct
import sys

# The machines Wine builds PE code for: i386, x86-64, ARM64, ARM64EC, ARM64X,
# ARMv7 Thumb.
COFF_MACHINES = {0x014C, 0x8664, 0xAA64, 0xA641, 0xA64E, 0x01C4}


def stamp_archive(path, epoch):
    with open(path, "r+b") as archive:
        data = archive.read()
        if not data.startswith(b"!<arch>\n"):
            return 0
        stamped = 0
        offset = 8
        while offset + 60 <= len(data):
            header = data[offset:offset + 60]
            if header[58:60] != b"`\n":
                raise SystemExit("%s: bad archive member at %d" % (path, offset))
            size = int(header[48:58])
            body = offset + 60
            if size >= 20:
                machine = struct.unpack_from("<H", data, body)[0]
                if machine in COFF_MACHINES:
                    archive.seek(body + 4)   # FileHeader.TimeDateStamp
                    archive.write(struct.pack("<I", epoch))
                    stamped += 1
            offset = body + size + (size & 1)
    return stamped


def main(argv):
    if len(argv) < 3 or not argv[1].isdigit():
        raise SystemExit(__doc__)
    epoch = int(argv[1])
    if epoch > 0xFFFFFFFF:
        raise SystemExit("EPOCH does not fit a COFF timestamp")
    archives = objects = 0
    for root in argv[2:]:
        if not os.path.isdir(root):
            raise SystemExit("%s: not a directory" % root)
        for folder, _dirs, names in sorted(os.walk(root)):
            for name in sorted(names):
                path = os.path.join(folder, name)
                if not name.endswith(".a") or os.path.islink(path) \
                        or not os.path.isfile(path):
                    continue
                count = stamp_archive(path, epoch)
                archives += bool(count)
                objects += count
    print("stamped %d COFF objects in %d archives with %d"
          % (objects, archives, epoch))


if __name__ == "__main__":
    main(sys.argv)
