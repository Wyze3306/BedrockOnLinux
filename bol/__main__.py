"""Entry point for ``python3 -m bol`` and the packaged zipapp (.pyz)."""
# SPDX-License-Identifier: MIT
import sys

if len(sys.argv) > 1 and sys.argv[1] == "optiscaler":
    from .optiscaler import cli_main
    _entry = lambda: cli_main(sys.argv[2:])
else:
    from .optiscaler import bootstrap
    bootstrap()
    from .cli import main
    _entry = main

if __name__ == "__main__":
    try:
        _entry()
    except KeyboardInterrupt:
        print()
        sys.exit(130)
