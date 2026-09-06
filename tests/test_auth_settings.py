"""Regression tests for the managed WineGDK online-access setting."""
# SPDX-License-Identifier: MIT

import json
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from bol import auth


_UPGRADED_SYSTEM_REG = b"""WINE REGISTRY Version 2
;; All keys relative to REGISTRY\\Machine

#arch=win64

[Software\\\\Microsoft\\\\WindowsRuntime\\\\ActivatableClassId\\\\Microsoft.Windows.Storage.Pickers.FileOpenPicker] 1
#time=1

"""

_UPGRADED_USER_REG = b"""WINE REGISTRY Version 2
;; All keys relative to REGISTRY\\User\\S-1-5-21-0-0-0-1000

#arch=win64

[Environment] 1
#time=1

"""


class WineGdkPrerequisiteTests(unittest.TestCase):
    def test_file_picker_registration_repairs_upgraded_prefix_idempotently(self):
        with tempfile.TemporaryDirectory() as td:
            prefix = Path(td) / "pfx"
            prefix.mkdir()
            (prefix / "system.reg").write_bytes(_UPGRADED_SYSTEM_REG)
            (prefix / "user.reg").write_bytes(_UPGRADED_USER_REG)

            with mock.patch.object(auth, "active_prefix",
                                   return_value=prefix), \
                    mock.patch.object(auth, "load_settings",
                                      return_value={}), \
                    mock.patch.object(auth, "ok"), \
                    mock.patch("bol.prefix.require_prefix_idle"):
                auth.wine_apply_winegdk_prereqs()
                system_once = (prefix / "system.reg").read_bytes()
                user_once = (prefix / "user.reg").read_bytes()
                auth.wine_apply_winegdk_prereqs()

            system = (prefix / "system.reg").read_text()
            self.assertIn(
                r"[Software\\Microsoft\\WindowsRuntime\\ActivatableClassId"
                r"\\Microsoft.Windows.Storage.Pickers.FileOpenPicker]",
                system,
            )
            self.assertIn(
                r'"DllPath"="C:\\windows\\system32\\windows.storage.dll"',
                system,
            )
            self.assertEqual(system.count('"DllPath"='), 1)
            self.assertEqual((prefix / "system.reg").read_bytes(), system_once)
            self.assertEqual((prefix / "user.reg").read_bytes(), user_once)




class OnlinePreauthPayloadTests(unittest.TestCase):
    @staticmethod
    def payload(expiry="2999-01-01T00:00:00Z"):
        return {
            "device_token": "device",
            "device_token_expiry": expiry,
            "user_token": "user",
            "user_token_expiry": expiry,
            "xbl_token": "xbl",
            "xbl_token_expiry": expiry,
            "achievements_token": "achievements",
            "achievements_uhs": "42",
            "achievements_expiry": expiry,
            "xbl_xuid": "1234",
            "sisu_token": "playfab",
            "sisu_rp": "https://example.playfabapi.com/",
            "sisu_uhs": "42",
            "sisu_expiry": expiry,
            "mp_token": "multiplayer",
            "mp_rp": "https://multiplayer.minecraft.net/",
            "mp_uhs": "42",
            "mp_expiry": expiry,
            "realms_token": "realms",
            "realms_rp": "https://pocket.realms.minecraft.net/",
            "realms_uhs": "42",
            "realms_expiry": expiry,
            "lic_token": "license",
            "lic_rp": "https://licensing.minecraft.net/",
            "lic_uhs": "42",
            "lic_expiry": expiry,
        }

    def test_complete_future_payload_is_ready(self):
        self.assertEqual(auth._online_preauth_problems(self.payload()), [])

    def test_legacy_payload_without_xbl_privileges_remains_ready(self):
        payload = self.payload()
        self.assertNotIn("xbl_privileges", payload)
        self.assertEqual(auth._online_preauth_problems(payload), [])

    def test_xbl_privileges_are_canonical_and_deduplicated(self):
        self.assertEqual(
            auth._normalize_xbl_privileges(
                "254 185 000188 254 invalid -1 4294967296"),
            "185 188 254",
        )
        self.assertEqual(
            auth._normalize_xbl_privileges([254, "185", "0185", True]),
            "185 254",
        )

    def test_invalid_xbl_privilege_claim_is_omitted(self):
        self.assertIsNone(auth._normalize_xbl_privileges(None))
        self.assertIsNone(auth._normalize_xbl_privileges("invalid -1"))

    def test_xbl_privilege_claim_distinguishes_absent_from_empty(self):
        self.assertEqual(auth._xbl_privilege_claim({}), (False, None))
        self.assertEqual(auth._xbl_privilege_claim({"prv": ""}), (True, ""))
        self.assertEqual(
            auth._xbl_privilege_claim({"prv": "254 185 254"}),
            (True, "185 254"),
        )
        # A present but malformed claim must not become the permissive legacy
        # fallback merely because no valid IDs could be extracted.
        self.assertEqual(
            auth._xbl_privilege_claim({"prv": "invalid -1"}),
            (True, ""),
        )

    def test_modern_gamertag_claims_are_kept_as_distinct_components(self):
        self.assertEqual(
            auth._xbl_gamertag_claims({
                "mgt": "ModernPlayer",
                "mgs": "1234",
                "umg": "ModernPlayer#1234",
                "gtg": "ClassicPlayer1234",
            }),
            {
                "xbl_modern_gamertag": "ModernPlayer",
                "xbl_modern_gamertag_suffix": "1234",
                "xbl_unique_modern_gamertag": "ModernPlayer#1234",
            },
        )
        self.assertEqual(
            auth._xbl_gamertag_claims({"mgt": None, "mgs": 1234}), {})

    def test_xbox_seven_digit_expiry_is_python39_compatible(self):
        # Xbox NotAfter uses 100 ns precision. Python 3.9's fromisoformat()
        # accepts at most six fractional digits, so the launcher must normalize
        # this real service format before parsing it.
        service_expiry = "2999-01-01T00:00:00.1234567Z"
        self.assertEqual(
            auth._normalize_xbox_expiry(service_expiry),
            "2999-01-01T00:00:00.123456+00:00",
        )
        payload = self.payload(service_expiry)
        self.assertEqual(auth._online_preauth_problems(payload), [])

    def test_missing_multiplayer_token_is_rejected(self):
        payload = self.payload()
        payload["mp_token"] = None
        self.assertIn("missing mp_token",
                      auth._online_preauth_problems(payload))

    def test_partial_achievements_credentials_are_omitted(self):
        for field in auth._ACHIEVEMENTS_FIELDS:
            with self.subTest(field=field):
                payload = self.payload()
                payload.pop(field)
                with tempfile.TemporaryDirectory() as td:
                    path = Path(td) / "device.json"
                    self.assertTrue(auth._store_online_preauth(path, payload))
                    stored = json.loads(path.read_text())
                for optional in auth._ACHIEVEMENTS_CACHE_FIELDS:
                    self.assertNotIn(optional, stored)
                self.assertEqual(auth._online_preauth_problems(stored), [])

    def test_invalid_or_expired_achievements_credentials_are_omitted(self):
        cases = (
            {"achievements_token": ""},
            {"achievements_token": "   "},
            {"achievements_token": 123},
            {"achievements_expiry": "not-a-timestamp"},
            {"achievements_expiry": "2000-01-01T00:00:00Z"},
        )
        for updates in cases:
            with self.subTest(updates=updates):
                payload = self.payload()
                payload.update(updates)
                payload["achievements_expiry_epoch"] = "stale"
                with tempfile.TemporaryDirectory() as td:
                    path = Path(td) / "device.json"
                    self.assertTrue(auth._store_online_preauth(path, payload))
                    stored = json.loads(path.read_text())
                for field in auth._ACHIEVEMENTS_CACHE_FIELDS:
                    self.assertNotIn(field, stored)
                self.assertEqual(auth._online_preauth_problems(stored), [])

    def test_achievements_uhs_is_strict_decimal_uint64(self):
        invalid_values = (
            "not-decimal",
            "+42",
            "-42",
            " 42",
            "42 ",
            "42junk",
            "４２",
            "0",
            str(1 << 64),
            42,
        )
        for value in invalid_values:
            with self.subTest(value=value):
                payload = self.payload()
                payload["achievements_uhs"] = value
                with tempfile.TemporaryDirectory() as td:
                    path = Path(td) / "device.json"
                    self.assertTrue(auth._store_online_preauth(path, payload))
                    stored = json.loads(path.read_text())
                for field in auth._ACHIEVEMENTS_CACHE_FIELDS:
                    self.assertNotIn(field, stored)

    def test_achievements_uhs_is_canonicalized(self):
        for value, expected in (
                ("00000042", "42"),
                (str((1 << 64) - 1), str((1 << 64) - 1))):
            with self.subTest(value=value):
                payload = self.payload()
                payload["achievements_uhs"] = value
                with tempfile.TemporaryDirectory() as td:
                    path = Path(td) / "device.json"
                    self.assertTrue(auth._store_online_preauth(path, payload))
                    stored = json.loads(path.read_text())
                self.assertEqual(stored["achievements_uhs"], expected)
                self.assertRegex(
                    stored["achievements_expiry_epoch"], r"^[0-9]+$")

    def test_missing_realms_token_is_rejected(self):
        payload = self.payload()
        payload["realms_token"] = None
        self.assertIn("missing realms_token",
                      auth._online_preauth_problems(payload))

    def test_missing_realms_routing_fields_are_rejected(self):
        for field in ("realms_rp", "realms_uhs"):
            with self.subTest(field=field):
                payload = self.payload()
                payload[field] = None
                self.assertIn(f"missing {field}",
                              auth._online_preauth_problems(payload))

    def test_expired_realms_token_is_rejected(self):
        payload = self.payload()
        payload["realms_expiry"] = "2000-01-01T00:00:00Z"
        self.assertIn("expired realms_token",
                      auth._online_preauth_problems(payload))

    def test_expired_token_is_rejected(self):
        problems = auth._online_preauth_problems(
            self.payload("2000-01-01T00:00:00Z"))
        self.assertIn("expired mp_token", problems)

    def test_partial_payload_never_replaces_existing_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "device.json"
            path.write_text("keep-me")
            payload = self.payload()
            payload["xbl_token"] = None
            self.assertFalse(auth._store_online_preauth(path, payload))
            self.assertEqual(path.read_text(), "keep-me")

    def test_complete_payload_is_written_atomically_and_private(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "device.json"
            self.assertTrue(auth._store_online_preauth(path, self.payload()))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            stored = auth._load_online_preauth(path)
            self.assertEqual(stored["mp_token"], "multiplayer")
            self.assertEqual(stored["realms_token"], "realms")
            expected = str(int(auth._parse_xbox_expiry(
                "2999-01-01T00:00:00Z").timestamp()))
            for epoch_field in auth._WINEGDK_EXPIRY_EPOCH_FIELDS.values():
                self.assertEqual(stored[epoch_field], expected)

    def test_invalid_required_iso_expiry_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "device.json"
            payload = self.payload()
            payload["mp_expiry"] = "not-an-iso-timestamp"
            self.assertFalse(auth._store_online_preauth(path, payload))
            self.assertFalse(path.exists())

    def test_corrupt_or_unreadable_account_epoch_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache = root / "winegdk-preauth"
            cache.mkdir()
            path = cache / "device.json"
            payload = self.payload()
            self.assertTrue(auth._store_online_preauth(path, payload))
            before = path.read_bytes()

            (cache / ".account-epoch").write_text("not-a-valid-generation\n")
            self.assertIsNone(auth._account_cache_epoch(cache))
            self.assertFalse(auth._store_online_preauth(path, payload))
            with mock.patch.object(auth, "DATA", root), \
                    mock.patch.object(auth, "warn"):
                self.assertFalse(auth.xbl_preauth(""))
            self.assertEqual(path.read_bytes(), before)

            # Permission/read failures use the same fail-closed state and may
            # neither match an old payload nor authorize a new store.
            with mock.patch.object(auth, "_account_cache_epoch",
                                   return_value=None):
                self.assertFalse(auth._cached_account_matches(payload, None))
                self.assertFalse(auth._store_online_preauth(path, payload))
            self.assertEqual(path.read_bytes(), before)

    def test_missing_access_token_reuses_complete_unexpired_cache(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache = root / "winegdk-preauth"
            cache.mkdir()
            path = cache / "device.json"
            path.write_text(json.dumps(self.payload()))
            self.assertNotIn("mp_expiry_epoch", json.loads(path.read_text()))
            with mock.patch.object(auth, "DATA", root), \
                    mock.patch.object(auth, "warn"), \
                    mock.patch.object(auth, "info"):
                self.assertTrue(auth.xbl_preauth(""))
            stored = json.loads(path.read_text())
            for epoch_field in auth._WINEGDK_EXPIRY_EPOCH_FIELDS.values():
                self.assertRegex(stored[epoch_field], r"^[0-9]+$")

    def test_missing_access_token_reuses_cache_without_achievements(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache = root / "winegdk-preauth"
            cache.mkdir()
            payload = self.payload()
            for field in auth._ACHIEVEMENTS_CACHE_FIELDS:
                payload.pop(field, None)
            path = cache / "device.json"
            path.write_text(json.dumps(payload))

            with mock.patch.object(auth, "DATA", root), \
                    mock.patch.object(auth, "warn"), \
                    mock.patch.object(auth, "info"):
                self.assertTrue(auth.xbl_preauth(""))

            stored = json.loads(path.read_text())
            for field in auth._ACHIEVEMENTS_CACHE_FIELDS:
                self.assertNotIn(field, stored)
            self.assertEqual(auth._online_preauth_problems(stored), [])

    def test_missing_access_token_removes_invalid_achievements_cache(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache = root / "winegdk-preauth"
            cache.mkdir()
            payload = auth._with_winegdk_expiry_epochs(self.payload())
            payload["achievements_uhs"] = "42junk"
            path = cache / "device.json"
            path.write_text(json.dumps(payload))

            with mock.patch.object(auth, "DATA", root), \
                    mock.patch.object(auth, "warn"), \
                    mock.patch.object(auth, "info"):
                self.assertTrue(auth.xbl_preauth(""))

            stored = json.loads(path.read_text())
            for field in auth._ACHIEVEMENTS_CACHE_FIELDS:
                self.assertNotIn(field, stored)

    def test_missing_access_token_does_not_accept_partial_cache(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache = root / "winegdk-preauth"
            cache.mkdir()
            partial = self.payload()
            partial["mp_token"] = None
            path = cache / "device.json"
            path.write_text(json.dumps(partial))
            before = path.read_bytes()
            with mock.patch.object(auth, "DATA", root), \
                    mock.patch.object(auth, "warn"), \
                    mock.patch.object(auth, "info"):
                self.assertFalse(auth.xbl_preauth(""))
            self.assertEqual(path.read_bytes(), before)

    def test_launch_epoch_mismatch_refuses_even_valid_cached_tokens(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache = root / "winegdk-preauth"
            cache.mkdir()
            current_epoch = "c" * 32
            (cache / ".account-epoch").write_text(current_epoch + "\n")
            payload = self.payload()
            payload["_account_epoch"] = current_epoch
            auth._store_online_preauth(cache / "device.json", payload,
                                       expected_epoch=current_epoch)

            with mock.patch.object(auth, "DATA", root), \
                    mock.patch.object(auth, "warn"):
                self.assertFalse(auth.xbl_preauth("", "d" * 32))

    def test_logout_purges_account_tokens_but_keeps_device_identity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            msa = root / "msa"
            cache = root / "winegdk-preauth"
            prefix = root / "pfx"
            msa.mkdir()
            cache.mkdir()
            prefix.mkdir()
            (msa / "token.json").write_text(
                json.dumps({"refresh_token": "old-account"}))
            (prefix / "system.reg").write_text(
                "WINE REGISTRY Version 2\n"
                ";; All keys relative to REGISTRY\\Machine\n\n"
                "[Software\\\\Wine\\\\WineGDK] 1\n"
                "#time=1\n"
                '"RefreshToken"="old-account"\n'
                '"KeepMe"="yes"\n\n'
            )
            old_payload = self.payload()
            auth._store_online_preauth(cache / "device.json", old_payload)
            key = b"device-private-key"
            device_id = "{stable-device-id}"
            (cache / "device-key.pem").write_bytes(key)
            (cache / "device-id.txt").write_text(device_id)
            (cache / ".device-crash.tmp").write_text("old account tokens")

            with mock.patch.object(auth, "DATA", root), \
                    mock.patch.object(auth, "MSA_DIR", msa), \
                    mock.patch.object(auth, "active_prefix",
                                      return_value=prefix), \
                    mock.patch.object(auth, "prefix_operation_lock"), \
                    mock.patch("bol.prefix.require_prefix_idle"):
                self.assertTrue(auth.msa_logout())

            self.assertFalse((msa / "token.json").exists())
            self.assertFalse((cache / "device.json").exists())
            self.assertFalse((cache / ".device-crash.tmp").exists())
            self.assertEqual((cache / "device-key.pem").read_bytes(), key)
            self.assertEqual((cache / "device-id.txt").read_text(), device_id)
            self.assertRegex((cache / ".account-epoch").read_text().strip(),
                             r"^[0-9a-f]{32}$")
            registry = (prefix / "system.reg").read_text()
            self.assertNotIn('"RefreshToken"=', registry)
            self.assertIn('"KeepMe"="yes"', registry)
            self.assertEqual(
                stat.S_IMODE((prefix / "system.reg").stat().st_mode), 0o600)

            # Even if an old cache is restored after a crash, its legacy epoch
            # cannot be consumed by the next account.
            (cache / "device.json").write_text(json.dumps(old_payload))
            with mock.patch.object(auth, "DATA", root), \
                    mock.patch.object(auth, "warn"), \
                    mock.patch.object(auth, "info"):
                self.assertFalse(auth.xbl_preauth(""))

    def test_logout_registry_failure_preserves_account_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            msa = root / "msa"
            cache = root / "winegdk-preauth"
            msa.mkdir()
            cache.mkdir()
            token = json.dumps({"refresh_token": "must-survive"})
            payload = self.payload()
            epoch = "a" * 32
            (msa / "token.json").write_text(token)
            payload["_account_epoch"] = epoch
            (cache / "device.json").write_text(json.dumps(payload))
            (cache / ".account-epoch").write_text(epoch + "\n")

            with mock.patch.object(auth, "DATA", root), \
                    mock.patch.object(auth, "MSA_DIR", msa), \
                    mock.patch.object(auth, "prefix_operation_lock"), \
                    mock.patch.object(
                        auth, "wine_reg_remove_refresh_token",
                        side_effect=OSError("simulated registry failure")):
                with self.assertRaisesRegex(
                        auth.BolError, "Wine prefix; sign-out was cancelled"):
                    auth.msa_logout()

            self.assertEqual((msa / "token.json").read_text(), token)
            self.assertTrue((cache / "device.json").is_file())
            self.assertEqual(
                (cache / ".account-epoch").read_text(), epoch + "\n")

    def test_logout_lock_failure_changes_no_credential_store(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            msa = root / "msa"
            msa.mkdir()
            token = json.dumps({"refresh_token": "must-survive"})
            (msa / "token.json").write_text(token)
            registry_clear = mock.Mock()
            cache_purge = mock.Mock()

            with mock.patch.object(auth, "DATA", root), \
                    mock.patch.object(auth, "MSA_DIR", msa), \
                    mock.patch.object(
                        auth, "prefix_operation_lock",
                        side_effect=auth.BolError("prefix is active")), \
                    mock.patch.object(
                        auth, "wine_reg_remove_refresh_token", registry_clear), \
                    mock.patch.object(
                        auth, "_purge_account_preauth", cache_purge):
                with self.assertRaisesRegex(auth.BolError, "prefix is active"):
                    auth.msa_logout()

            registry_clear.assert_not_called()
            cache_purge.assert_not_called()
            self.assertEqual((msa / "token.json").read_text(), token)

    def test_logout_cache_failure_does_not_restore_registry_token(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            msa = root / "msa"
            msa.mkdir()
            (msa / "token.json").write_text(
                json.dumps({"refresh_token": "still-canonical"}))
            registry_clear = mock.Mock(return_value=True)

            with mock.patch.object(auth, "DATA", root), \
                    mock.patch.object(auth, "MSA_DIR", msa), \
                    mock.patch.object(auth, "prefix_operation_lock"), \
                    mock.patch.object(
                        auth, "wine_reg_remove_refresh_token", registry_clear), \
                    mock.patch.object(
                        auth, "_purge_account_preauth",
                        side_effect=OSError("simulated cache failure")):
                with self.assertRaisesRegex(
                        auth.BolError, "account cache; sign-out was cancelled"):
                    auth.msa_logout()

            registry_clear.assert_called_once_with()
            self.assertTrue((msa / "token.json").is_file())

    def test_remove_registry_token_is_noop_before_prefix_boot(self):
        with tempfile.TemporaryDirectory() as td:
            prefix = Path(td) / "not-created"
            with mock.patch.object(auth, "active_prefix",
                                   return_value=prefix), \
                    mock.patch.object(auth, "update_prefix_registry") as update:
                self.assertTrue(auth.wine_reg_remove_refresh_token())
            update.assert_not_called()

    def test_fallback_rechecks_cache_expiry_after_failed_post(self):
        from datetime import datetime, timezone

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache = root / "winegdk-preauth"
            cache.mkdir()
            expiry_text = "2030-01-01T00:00:00.1234567Z"
            expiry = datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp()
            auth._store_online_preauth(
                cache / "device.json", self.payload(expiry_text))

            # First validation: two minutes remain. The device POST then fails
            # after the cache has crossed its 60-second safety margin. A stale
            # boolean from function entry would incorrectly return True here.
            with mock.patch.object(auth, "DATA", root), \
                    mock.patch.object(
                        auth.time, "time",
                        side_effect=[expiry - 120, expiry - 30, expiry]), \
                    mock.patch("urllib.request.urlopen",
                               side_effect=OSError("simulated timeout")), \
                    mock.patch.object(auth, "warn"), \
                    mock.patch.object(auth, "info"):
                self.assertFalse(auth.xbl_preauth("fresh-access-token"))

    def test_fresh_cache_skips_the_network_chain(self):
        """PLAY should not redo what _warm_xbox_preauth just finished."""
        epoch = "b" * 32
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache = root / "winegdk-preauth"
            cache.mkdir()
            (cache / ".account-epoch").write_text(epoch + "\n")
            payload = self.payload()
            payload["_account_epoch"] = epoch
            auth._store_online_preauth(cache / "device.json", payload,
                                       expected_epoch=epoch)

            with mock.patch.object(auth, "DATA", root), \
                    mock.patch("urllib.request.urlopen") as urlopen, \
                    mock.patch.object(auth, "warn"), \
                    mock.patch.object(auth, "info"), \
                    mock.patch.object(auth, "ok"):
                self.assertTrue(
                    auth.xbl_preauth("fresh-access-token", epoch))

            urlopen.assert_not_called()

    def test_cache_inside_reuse_margin_still_refreshes(self):
        """A cache that clears the 60s floor but not the reuse margin still
        drives a real refresh, so a session doesn't sign off mid-play."""
        from datetime import datetime, timezone

        epoch = "c" * 32
        soon = datetime.fromtimestamp(
            time.time() + auth._PREAUTH_REUSE_MARGIN / 2, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache = root / "winegdk-preauth"
            cache.mkdir()
            (cache / ".account-epoch").write_text(epoch + "\n")
            payload = self.payload(soon)
            payload["_account_epoch"] = epoch
            auth._store_online_preauth(cache / "device.json", payload,
                                       expected_epoch=epoch)

            with mock.patch.object(auth, "DATA", root), \
                    mock.patch("urllib.request.urlopen",
                               side_effect=OSError("simulated timeout")) \
                    as urlopen, \
                    mock.patch.object(auth, "warn"), \
                    mock.patch.object(auth, "info"):
                # The network attempt is made (not skipped) even though it
                # then fails; the still-valid cache is what recovers True
                # here, via the offline-resilience fallback, not the reuse
                # fast path this test is guarding.
                auth.xbl_preauth("fresh-access-token", epoch)

            urlopen.assert_called()

    def test_achievements_use_user_only_xsts_and_services_keep_sisu(self):
        expiry = "2999-01-01T00:00:00.1234567Z"
        epoch = "e" * 32
        requests = []

        class Response:
            def __init__(self, payload):
                self.status = 200
                self.payload = json.dumps(payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return self.payload

        def authorization(label, uhs="42", claims=None):
            xui = {"uhs": uhs}
            if claims:
                xui.update(claims)
            return {
                "AuthorizationToken": {
                    "Token": label,
                    "NotAfter": expiry,
                    "DisplayClaims": {"xui": [xui]},
                },
            }

        def xsts_authorization(label, uhs="42", claims=None):
            return authorization(label, uhs, claims)["AuthorizationToken"]

        responses = iter([
            {"Token": "device", "NotAfter": expiry},
            {"Token": "user", "NotAfter": expiry},
            xsts_authorization("achievements", uhs="00042"),
            authorization("xbl", uhs="84", claims={
                "xid": "1234", "gtg": "Player", "agg": "Adult",
            }),
            authorization("playfab"),
            authorization("multiplayer"),
            authorization("realms"),
            authorization("license"),
        ])

        def urlopen(request, timeout):
            self.assertEqual(timeout, 15)
            requests.append((request.full_url, json.loads(request.data)))
            return Response(next(responses))

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache = root / "winegdk-preauth"
            cache.mkdir()
            (cache / ".account-epoch").write_text(epoch + "\n")
            with mock.patch.object(auth, "DATA", root), \
                    mock.patch("urllib.request.urlopen", side_effect=urlopen), \
                    mock.patch.object(auth, "warn"), \
                    mock.patch.object(auth, "info"), \
                    mock.patch.object(auth, "ok"):
                self.assertTrue(auth.xbl_preauth("fresh-access-token", epoch))

            self.assertEqual(
                [request["RelyingParty"] for _, request in requests],
                [
                    "http://auth.xboxlive.com",
                    "http://auth.xboxlive.com",
                    "http://xboxlive.com",
                    "http://xboxlive.com",
                    "https://b980a380.minecraft.playfabapi.com/",
                    "https://multiplayer.minecraft.net/",
                    "https://pocket.realms.minecraft.net/",
                    "http://licensing.xboxlive.com",
                ],
            )
            self.assertEqual(
                requests[2],
                (
                    "https://xsts.auth.xboxlive.com/xsts/authorize",
                    {
                        "RelyingParty": "http://xboxlive.com",
                        "TokenType": "JWT",
                        "Properties": {
                            "SandboxId": "RETAIL",
                            "UserTokens": ["user"],
                        },
                    },
                ),
            )
            for url, request in requests[3:]:
                self.assertEqual(
                    url, "https://sisu.xboxlive.com/authorize")
                self.assertEqual(request["AppId"], "0000000048183522")
                self.assertEqual(
                    request["AccessToken"], "t=fresh-access-token")
            stored = json.loads((cache / "device.json").read_text())
            self.assertEqual(stored["xbl_token"], "xbl")
            self.assertEqual(stored["xbl_uhs"], "84")
            self.assertEqual(stored["achievements_token"], "achievements")
            self.assertEqual(stored["achievements_uhs"], "42")
            self.assertRegex(
                stored["achievements_expiry_epoch"], r"^[0-9]+$")
            self.assertEqual(stored["realms_rp"],
                             "https://pocket.realms.minecraft.net/")
            self.assertEqual(stored["realms_token"], "realms")
            self.assertRegex(stored["realms_expiry_epoch"], r"^[0-9]+$")

    def test_achievements_xsts_failure_does_not_block_core_preauth(self):
        expiry = "2999-01-01T00:00:00Z"
        epoch = "f" * 32

        class Response:
            def __init__(self, status, payload):
                self.status = status
                self.payload = json.dumps(payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return self.payload

        def authorization(label, claims=None):
            xui = {"uhs": "84"}
            if claims:
                xui.update(claims)
            return {
                "AuthorizationToken": {
                    "Token": label,
                    "NotAfter": expiry,
                    "DisplayClaims": {"xui": [xui]},
                },
            }

        responses = iter([
            Response(200, {"Token": "device", "NotAfter": expiry}),
            Response(200, {"Token": "user", "NotAfter": expiry}),
            Response(503, {"error": "temporarily unavailable"}),
            Response(200, authorization("xbl", {
                "xid": "1234", "gtg": "Player", "agg": "Adult",
            })),
            Response(200, authorization("playfab")),
            Response(200, authorization("multiplayer")),
            Response(200, authorization("realms")),
            Response(200, authorization("license")),
        ])

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache = root / "winegdk-preauth"
            cache.mkdir()
            (cache / ".account-epoch").write_text(epoch + "\n")
            with mock.patch.object(auth, "DATA", root), \
                    mock.patch("urllib.request.urlopen",
                               side_effect=lambda *_args, **_kwargs:
                               next(responses)) as urlopen, \
                    mock.patch.object(auth, "warn"), \
                    mock.patch.object(auth, "info"), \
                    mock.patch.object(auth, "ok"):
                self.assertTrue(auth.xbl_preauth(
                    "fresh-access-token", epoch))

            self.assertEqual(urlopen.call_count, 8)
            stored = json.loads((cache / "device.json").read_text())
            for field in auth._ACHIEVEMENTS_CACHE_FIELDS:
                self.assertNotIn(field, stored)
            self.assertEqual(stored["xbl_token"], "xbl")
            self.assertEqual(stored["mp_token"], "multiplayer")
            self.assertEqual(stored["realms_token"], "realms")
            self.assertIsNone(auth.xbl_preauth_diagnostic())


class SisuRefusalFallbackTests(unittest.TestCase):
    """#149: SISU answered HTTP 403 for every relying party while XSTS kept
    issuing tokens for the same audiences and the same account.  The chain has
    to be completed through XSTS instead of failing the whole sign-in."""

    expiry = "2999-01-01T00:00:00Z"

    def setUp(self):
        auth._clear_xbl_preauth_diagnostic()
        self.addCleanup(auth._clear_xbl_preauth_diagnostic)

    class _Response:
        def __init__(self, status, payload):
            self.status = status
            self.payload = json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return self.payload

    def _xsts_token(self, relying_party):
        xui = {"uhs": "84"}
        if relying_party == "http://xboxlive.com":
            xui.update({"xid": "1234", "gtg": "Player", "agg": "Adult",
                        "prv": "190 191"})
        return {"Token": "xsts-" + relying_party, "NotAfter": self.expiry,
                "DisplayClaims": {"xui": [xui]}}

    def _run(self, xsts_status):
        """Drive a full pre-auth where every SISU call is refused.

        ``xsts_status`` maps a device-bound XSTS request to its HTTP status so
        a test can also refuse that variant and check the user-only retry.
        """
        requests = []

        def urlopen(request, timeout=None):
            body = json.loads(request.data)
            requests.append((request.full_url, body))
            if request.full_url.startswith("https://device.auth"):
                return self._Response(
                    200, {"Token": "device", "NotAfter": self.expiry})
            if request.full_url.startswith("https://user.auth"):
                return self._Response(
                    200, {"Token": "user", "NotAfter": self.expiry})
            if request.full_url.startswith("https://sisu"):
                # Exactly what the report shows: a bare 403 with no XErr.
                return self._Response(403, {"Message": "Forbidden"})
            device_bound = "DeviceToken" in body["Properties"]
            status = xsts_status if device_bound else 200
            if status != 200:
                return self._Response(status, {"Message": "Forbidden"})
            return self._Response(
                200, self._xsts_token(body["RelyingParty"]))

        epoch = "a" * 32
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache = root / "winegdk-preauth"
            cache.mkdir()
            (cache / ".account-epoch").write_text(epoch + "\n")
            with mock.patch.object(auth, "DATA", root), \
                    mock.patch("urllib.request.urlopen",
                               side_effect=urlopen), \
                    mock.patch.object(auth, "warn"), \
                    mock.patch.object(auth, "info"), \
                    mock.patch.object(auth, "ok"):
                completed = auth.xbl_preauth("fresh-access-token", epoch)
            payload = cache / "device.json"
            # A refused chain writes nothing; let the caller assert on that
            # rather than fail here with a missing file.
            stored = json.loads(payload.read_text()) if payload.exists() else {}
        return completed, stored, requests

    def test_every_audience_is_minted_through_xsts(self):
        completed, stored, requests = self._run(xsts_status=200)

        self.assertTrue(completed)
        self.assertEqual(stored["xbl_token"], "xsts-http://xboxlive.com")
        self.assertEqual(stored["sisu_token"],
                         "xsts-https://b980a380.minecraft.playfabapi.com/")
        self.assertEqual(stored["mp_token"],
                         "xsts-https://multiplayer.minecraft.net/")
        self.assertEqual(stored["realms_token"],
                         "xsts-https://pocket.realms.minecraft.net/")
        self.assertEqual(stored["lic_token"],
                         "xsts-http://licensing.xboxlive.com")
        self.assertEqual(stored["xbl_xuid"], "1234")
        self.assertEqual(stored["realms_rp"],
                         "https://pocket.realms.minecraft.net/")
        # Every SISU refusal was answered by an XSTS request for the same
        # audience, and the replacement carries the device identity.
        fallbacks = [body for url, body in requests
                     if url.startswith("https://xsts")
                     and "DeviceToken" in body["Properties"]]
        self.assertEqual(
            [body["RelyingParty"] for body in fallbacks],
            ["http://xboxlive.com",
             "https://b980a380.minecraft.playfabapi.com/",
             "https://multiplayer.minecraft.net/",
             "https://pocket.realms.minecraft.net/",
             "http://licensing.xboxlive.com"],
        )
        for body in fallbacks:
            self.assertEqual(body["Properties"]["DeviceToken"], "device")
            self.assertIn("ProofKey", body["Properties"])

    def test_a_recovered_sign_in_leaves_no_failure_diagnostic(self):
        """The refusals must not survive as 'Xbox Live rejected this account'
        advice once the tokens were obtained anyway."""

        self._run(xsts_status=200)

        self.assertIsNone(auth.xbl_preauth_diagnostic())

    def test_device_bound_refusal_retries_with_the_user_token_alone(self):
        completed, stored, requests = self._run(xsts_status=403)

        self.assertTrue(completed)
        self.assertEqual(stored["mp_token"],
                         "xsts-https://multiplayer.minecraft.net/")
        user_only = [body for url, body in requests
                     if url.startswith("https://xsts")
                     and "DeviceToken" not in body["Properties"]]
        # The achievements token plus one retry for each of the five audiences.
        self.assertEqual(len(user_only), 6)


class BareForbiddenClassificationTests(unittest.TestCase):
    """Xbox Live names an XErr whenever it truly rejects an account, so a bare
    403 must not send people to xbox.com to repair a healthy profile (#149)."""

    class _Response:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload

        def json(self):
            return self._payload

    def test_forbidden_without_an_xerr_is_not_an_account_verdict(self):
        category, status, error_code = auth._xbl_response_error(
            self._Response(403, {"Message": "Forbidden"}))

        self.assertEqual(category, "service")
        self.assertEqual(status, 403)
        self.assertIsNone(error_code)

    def test_forbidden_with_an_xerr_still_blames_the_account(self):
        category, _status, error_code = auth._xbl_response_error(
            self._Response(403, {"XErr": 2148916233}))

        self.assertEqual(category, "account")
        self.assertEqual(error_code, 2148916233)

    def test_unauthorized_without_an_xerr_still_blames_the_account(self):
        category, _status, _error_code = auth._xbl_response_error(
            self._Response(401, {"Message": "Unauthorized"}))

        self.assertEqual(category, "account")


class NativeAuthCancellationTests(unittest.TestCase):
    @staticmethod
    def _device_response():
        return {
            "device_code": "device-code",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://example.invalid/link",
            "interval": 1,
            "expires_in": 900,
        }

    def test_logout_during_token_post_cannot_resurrect_account(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache = root / "winegdk-preauth"
            msa = root / "msa"
            prefix = root / "pfx"
            cache.mkdir()
            msa.mkdir()
            prefix.mkdir()
            epoch = "a" * 32
            (cache / ".account-epoch").write_text(epoch + "\n")
            native = auth.NativeAuth()
            online = mock.Mock()

            def post(url, _data):
                if url == auth.MSA_CONNECT:
                    return self._device_response()
                # Sign out completes while the token POST is in flight.
                auth.msa_logout()
                return {"refresh_token": "must-not-survive"}

            # Without an active_prefix() override msa_logout() reaches the
            # machine's real prefix: it would attempt a registry write on it,
            # and the failure that follows makes the sign-out fail closed
            # before it can rotate the generation this test asserts on.
            with mock.patch.object(auth, "DATA", root), \
                    mock.patch.object(auth, "MSA_DIR", msa), \
                    mock.patch.object(auth, "active_prefix",
                                      return_value=prefix), \
                    mock.patch.object(auth, "prefix_operation_lock"), \
                    mock.patch.object(auth, "http_post_form",
                                      side_effect=post), \
                    mock.patch.object(auth.time, "sleep"), \
                    mock.patch.object(auth, "warn"), \
                    mock.patch.object(auth, "info"):
                native._flow(None, online, epoch)

            self.assertFalse((msa / "token.json").exists())
            online.assert_not_called()
            self.assertNotEqual(
                (cache / ".account-epoch").read_text().strip(), epoch)

    def test_stop_during_token_post_discards_response(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache = root / "winegdk-preauth"
            msa = root / "msa"
            cache.mkdir()
            msa.mkdir()
            epoch = "b" * 32
            (cache / ".account-epoch").write_text(epoch + "\n")
            native = auth.NativeAuth()

            def post(url, _data):
                if url == auth.MSA_CONNECT:
                    return self._device_response()
                native.stop()
                return {"refresh_token": "cancelled"}

            with mock.patch.object(auth, "DATA", root), \
                    mock.patch.object(auth, "MSA_DIR", msa), \
                    mock.patch.object(auth, "http_post_form",
                                      side_effect=post), \
                    mock.patch.object(auth.time, "sleep"), \
                    mock.patch.object(auth, "info"):
                native._flow(None, None, epoch)

            self.assertFalse((msa / "token.json").exists())


if __name__ == "__main__":
    unittest.main()
