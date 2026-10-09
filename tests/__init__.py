"""The launcher's test suite, as a regular package.

A namespace package loses to any regular package of the same name anywhere on
sys.path, and some PyPI projects install their own top-level ``tests``
package into site-packages. With one of those installed, ``from
tests.guiharness import ...`` found theirs, and every GUI test failed to
import. Being a regular package, found first through the repository root on
sys.path, this one is the ``tests`` the suite imports.
"""
# SPDX-License-Identifier: MIT
