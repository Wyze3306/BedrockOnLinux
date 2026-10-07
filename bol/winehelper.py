"""bol.winehelper — the small Windows programs run beside the game.

src/fullscreen-nudge.c and src/desktop-bridge.c talk to the game's window
from inside Wine, which only a program in the same prefix and wineserver can
do. They ship inside the package, are written out to the cache when they
differ from what is there, and run with the engine's own wine.
"""
# SPDX-License-Identifier: MIT

import os
import pkgutil
import tempfile
from pathlib import Path

from .config import CACHE
from .prefix import active_prefix
from .proton import proton_path


def extract(name):
    """The helper `name` from the package, written to the cache; or None."""
    blob = pkgutil.get_data("bol", name)
    if not blob:
        return None
    CACHE.mkdir(parents=True, exist_ok=True)
    target = CACHE / name
    try:
        if target.is_file() and target.read_bytes() == blob:
            return target
    except OSError:
        pass
    fd, staged_name = tempfile.mkstemp(prefix=".helper-", suffix=".tmp",
                                       dir=CACHE)
    staged = Path(staged_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(blob)
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)
    return target


def command(name):
    """(argv, env) that run helper `name` in the game's prefix, or None."""
    engine = proton_path()
    if not engine:
        return None
    wine = Path(engine) / "files" / "bin" / "wine"
    if not wine.exists():
        return None
    helper = extract(name)
    if helper is None:
        return None
    env = dict(os.environ, WINEPREFIX=str(active_prefix()), WINEDEBUG="-all")
    return [str(wine), str(helper)], env
