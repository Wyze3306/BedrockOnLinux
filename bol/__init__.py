"""BedrockOnLinux — install & run Minecraft Bedrock (Windows GDK) on Linux.

GDK-Proton built from a WineGDK fork gives native in-game Microsoft login.

The entry points are :func:`bol.cli.main` (CLI/GUI dispatch) and
:mod:`bol.__main__` (``python3 -m bol`` and the packaged ``.pyz``). The one
import-time side effect is turning off Python 3.15's lazy imports (PEP 810):
PySide6 replaces ``builtins.__import__``, and a lazy name reached after that,
such as ``concurrent.futures.ThreadPoolExecutor``, stays an unresolved
``lazy_import`` object (#356). It runs here, before any other import, so every
module the launcher loads binds its names eagerly, as on Python 3.14.
"""
# SPDX-License-Identifier: MIT
import sys

if hasattr(sys, "set_lazy_imports_filter"):
    sys.set_lazy_imports_filter(lambda *_: False)

from .config import VERSION
from . import relocation as relocation

__version__ = VERSION
