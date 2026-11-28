"""Regression tests for GPU-free Wine registry updates."""
# SPDX-License-Identifier: MIT

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import wine_registry
from bol.log import BolError


SYSTEM = b"""WINE REGISTRY Version 2
;; All keys relative to REGISTRY\\Machine

#arch=win64

[Software\\\\Wine\\\\WineGDK] 1
#time=1
\"ForceMsaFacet\"=dword:00000000

[System\\\\CurrentControlSet] 1
#time=1
#link
\"SymbolicLinkValue\"=hex(6):00,00

[System\\\\Select] 1
#time=1
\"Current\"=dword:00000002

"""

USER = b"""WINE REGISTRY Version 2
;; All keys relative to REGISTRY\\User\\S-1-5-21-0-0-0-1000

#arch=win64

[Environment] 1
#time=1
\"TEMP\"=\"C:\\\\Temp\"

"""


class OfflineRegistryTests(unittest.TestCase):
    def make_prefix(self, root):
        prefix = Path(root) / "pfx"
        prefix.mkdir()
        (prefix / "system.reg").write_bytes(SYSTEM)
        (prefix / "user.reg").write_bytes(USER)
        return prefix

    def test_add_replace_delete_and_escape(self):
        with tempfile.TemporaryDirectory() as td:
            prefix = self.make_prefix(td)
            changed = wine_registry.update_prefix_registry(
                prefix,
                machine=[
                    wine_registry.reg_dword(
                        r"Software\Wine\WineGDK", "ForceMsaFacet", 1),
                    wine_registry.reg_sz(
                        r"Software\Wine\WineGDK", "RefreshToken",
                        'safe\\value"quoted'),
                ],
                user=[
                    wine_registry.reg_sz("Environment", "NEW_VALUE", "yes"),
                    wine_registry.reg_delete("Environment", "TEMP"),
                ],
            )
            self.assertTrue(changed)
            system = (prefix / "system.reg").read_text()
            user = (prefix / "user.reg").read_text()
            self.assertIn('"ForceMsaFacet"=dword:00000001', system)
            self.assertIn(
                '"RefreshToken"="safe\\\\value\\"quoted"', system)
            self.assertIn('"NEW_VALUE"="yes"', user)
            self.assertNotIn('"TEMP"=', user)
            self.assertEqual(stat.S_IMODE((prefix / "system.reg").stat().st_mode),
                             0o600)
            self.assertEqual(stat.S_IMODE((prefix / "user.reg").stat().st_mode),
                             0o600)

    def test_new_key_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            prefix = self.make_prefix(td)
            change = wine_registry.reg_dword(
                r"System\CurrentControlSet\Services\Example", "Start", 3)
            self.assertTrue(wine_registry.update_prefix_registry(
                prefix, machine=[change]))
            before = (prefix / "system.reg").read_bytes()
            self.assertFalse(wine_registry.update_prefix_registry(
                prefix, machine=[change]))
            self.assertEqual((prefix / "system.reg").read_bytes(), before)

    def test_current_control_set_targets_selected_concrete_hive(self):
        with tempfile.TemporaryDirectory() as td:
            prefix = self.make_prefix(td)
            change = wine_registry.reg_dword(
                r"System\CurrentControlSet\Services\FreshService", "Start", 3)
            self.assertTrue(wine_registry.update_prefix_registry(
                prefix, machine=[change]))
            system = (prefix / "system.reg").read_text()
            self.assertIn(
                r"[System\\ControlSet002\\Services\\FreshService]", system)
            self.assertNotIn(
                r"[System\\CurrentControlSet\\Services\\FreshService]", system)

    def test_second_replace_failure_restores_first_file(self):
        with tempfile.TemporaryDirectory() as td:
            prefix = self.make_prefix(td)
            original_system = (prefix / "system.reg").read_bytes()
            original_user = (prefix / "user.reg").read_bytes()
            real_replace = os.replace
            failed = False

            def fail_user_once(source, destination):
                nonlocal failed
                if Path(destination).name == "user.reg" and not failed:
                    failed = True
                    raise OSError("injected user.reg failure")
                return real_replace(source, destination)

            with mock.patch.object(wine_registry.os, "replace",
                                   side_effect=fail_user_once):
                with self.assertRaisesRegex(OSError, "injected"):
                    wine_registry.update_prefix_registry(
                        prefix,
                        machine=[wine_registry.reg_dword(
                            r"Software\Wine\WineGDK", "ForceMsaFacet", 1)],
                        user=[wine_registry.reg_sz(
                            "Environment", "NEW_VALUE", "yes")],
                    )

            self.assertEqual((prefix / "system.reg").read_bytes(),
                             original_system)
            self.assertEqual((prefix / "user.reg").read_bytes(), original_user)

    def test_rejects_non_wine_file_without_replacing_it(self):
        with tempfile.TemporaryDirectory() as td:
            prefix = self.make_prefix(td)
            path = prefix / "system.reg"
            path.write_text("not a registry")
            with self.assertRaisesRegex(ValueError, "not a Wine"):
                wine_registry.update_prefix_registry(
                    prefix, machine=[wine_registry.reg_dword("A", "B", 1)])
            self.assertEqual(path.read_text(), "not a registry")

    def test_live_prefix_is_rejected_before_registry_read_or_replace(self):
        with tempfile.TemporaryDirectory() as td:
            prefix = self.make_prefix(td)
            before = (prefix / "system.reg").read_bytes()
            with mock.patch(
                    "bol.prefix.require_prefix_idle",
                    side_effect=BolError("prefix active")):
                with self.assertRaisesRegex(BolError, "prefix active"):
                    wine_registry.update_prefix_registry(
                        prefix,
                        machine=[wine_registry.reg_dword("A", "B", 1)],
                    )
            self.assertEqual((prefix / "system.reg").read_bytes(), before)


class ReadUserStringTests(unittest.TestCase):
    def prefix(self, root, user=USER):
        prefix = Path(root) / "pfx"
        prefix.mkdir()
        (prefix / "user.reg").write_bytes(user)
        return prefix

    def test_reads_a_string_with_its_escapes_undone(self):
        with tempfile.TemporaryDirectory() as td:
            prefix = self.prefix(td)
            self.assertEqual(wine_registry.read_user_string(
                prefix, "Environment", "TEMP"), "C:\\Temp")

    def test_reads_back_what_an_offline_write_stored(self):
        with tempfile.TemporaryDirectory() as td:
            prefix = self.prefix(td)
            (prefix / "system.reg").write_bytes(SYSTEM)
            value = 'a "quoted" \\ value'
            with mock.patch("bol.prefix.require_prefix_idle"):
                wine_registry.update_prefix_registry(prefix, user=[
                    wine_registry.reg_sz(r"Software\Wine\Explorer",
                                         "Desktop", value)])
            self.assertEqual(wine_registry.read_user_string(
                prefix, r"Software\Wine\Explorer", "Desktop"), value)

    def test_missing_file_key_or_value_reads_none(self):
        with tempfile.TemporaryDirectory() as td:
            prefix = self.prefix(td)
            read = wine_registry.read_user_string
            self.assertIsNone(read(prefix, "Environment", "PATH"))
            self.assertIsNone(read(prefix, r"Software\Wine\Explorer",
                                   "Desktop"))
            self.assertIsNone(read(Path(td) / "none", "Environment", "TEMP"))

    def test_a_value_that_is_not_a_string_reads_none(self):
        user = USER + b'[Software\\\\Wine] 1\n"Count"=dword:00000001\n\n'
        with tempfile.TemporaryDirectory() as td:
            prefix = self.prefix(td, user)
            self.assertIsNone(wine_registry.read_user_string(
                prefix, r"Software\Wine", "Count"))

    def test_a_subkey_is_not_its_parent(self):
        user = (USER + b'[Software\\\\Wine\\\\Explorer\\\\Desktops] 1\n'
                b'"Default"="1600x900"\n\n')
        with tempfile.TemporaryDirectory() as td:
            prefix = self.prefix(td, user)
            read = wine_registry.read_user_string
            self.assertIsNone(read(prefix, r"Software\Wine\Explorer",
                                   "Default"))
            self.assertEqual(read(prefix, r"Software\Wine\Explorer\Desktops",
                                  "Default"), "1600x900")


if __name__ == "__main__":
    unittest.main()
