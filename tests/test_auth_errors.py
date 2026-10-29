"""Regression tests for actionable, credential-free Xbox auth failures."""
# SPDX-License-Identifier: MIT

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import auth


class _Response:
    def __init__(self, status, payload):
        self.status = status
        self._payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self._payload


class MsaRefreshResultTests(unittest.TestCase):
    def test_invalid_grant_is_preserved_for_the_gui(self):
        with mock.patch.object(auth, "http_post_form", return_value={
                "error": "invalid_grant",
                "error_description": "refresh token was revoked",
        }):
            self.assertEqual(auth.msa_refresh_result("old"),
                             (None, "invalid_grant"))

    def test_success_keeps_the_rotated_refresh_token(self):
        response = {"access_token": "access", "refresh_token": "new"}
        with mock.patch.object(auth, "http_post_form",
                               return_value=response):
            self.assertEqual(auth.msa_refresh_result("old"),
                             (response, None))


class PreauthFailureDiagnosticTests(unittest.TestCase):
    access_secret = "msa-access-token-must-not-leak"
    response_secret = "xbox-response-token-must-not-leak"

    def run_failure(self, urlopen_effect):
        warnings = []
        urlopen = mock.Mock()
        if isinstance(urlopen_effect, BaseException):
            urlopen.side_effect = urlopen_effect
        elif isinstance(urlopen_effect, (list, tuple)):
            urlopen.side_effect = urlopen_effect
        else:
            urlopen.return_value = urlopen_effect
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(auth, "DATA", root), \
                    mock.patch("urllib.request.urlopen", urlopen), \
                    mock.patch.object(auth, "warn",
                                      side_effect=lambda text:
                                      warnings.append(str(text))), \
                    mock.patch.object(auth, "info"), \
                    mock.patch.object(auth, "ok"):
                self.assertFalse(auth.xbl_preauth(self.access_secret))
        diagnostic = auth.xbl_preauth_diagnostic()
        self.assertIsNotNone(diagnostic)
        visible = json.dumps({
            "diagnostic": diagnostic,
            "message": auth.xbl_preauth_error_message(),
            "warnings": warnings,
        })
        self.assertNotIn(self.access_secret, visible)
        self.assertNotIn(self.response_secret, visible)
        return diagnostic

    def test_network_exception_is_sanitized_and_actionable(self):
        diagnostic = self.run_failure(
            OSError("timeout while sending " + self.response_secret))

        self.assertEqual(diagnostic["stage"], "device-auth")
        self.assertEqual(diagnostic["category"], "network")
        self.assertNotIn("http_status", diagnostic)
        self.assertIn("DNS", auth.xbl_preauth_error_message())

    def test_missing_xbox_profile_is_an_account_failure(self):
        expiry = "2999-01-01T00:00:00Z"
        failure = {
            "XErr": 2148916233,
            "Token": self.response_secret,
            "Message": "private " + self.access_secret,
        }
        diagnostic = self.run_failure([
            _Response(200, {"Token": "device", "NotAfter": expiry}),
            *[_Response(401, failure) for _ in range(6)],
        ])

        self.assertEqual(diagnostic, {
            "stage": "user-auth",
            "category": "account",
            "message": auth._XBL_PREAUTH_MESSAGES["account"],
            "http_status": 401,
            "error_code": 2148916233,
        })
        self.assertIn("Xbox profile", auth.xbl_preauth_error_message())

    def test_child_account_error_gets_age_family_guidance(self):
        expiry = "2999-01-01T00:00:00Z"
        failure = {
            "XErr": "2148916238",
            "Token": self.response_secret,
        }
        diagnostic = self.run_failure([
            _Response(200, {"Token": "device", "NotAfter": expiry}),
            _Response(200, {"Token": "user", "NotAfter": expiry}),
            *[_Response(403, failure) for _ in range(6)],
        ])

        self.assertEqual(diagnostic["stage"], "xsts-achievements")
        self.assertEqual(diagnostic["category"], "age")
        self.assertEqual(diagnostic["http_status"], 403)
        self.assertEqual(diagnostic["error_code"], 2148916238)
        self.assertIn("family", auth.xbl_preauth_error_message())

    def test_unreachable_token_refresh_is_reported_as_a_network_failure(self):
        """Being offline must not read as 'Xbox Live rejected this account'."""

        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(auth, "DATA", Path(td)), \
                    mock.patch.object(auth, "warn"), \
                    mock.patch.object(auth, "info"), \
                    mock.patch.object(auth, "ok"):
                self.assertFalse(
                    auth.xbl_preauth("", refresh_unreachable=True))

        diagnostic = auth.xbl_preauth_diagnostic()
        self.assertEqual(diagnostic["stage"], "microsoft-token")
        self.assertEqual(diagnostic["category"], "network")
        self.assertIn("Internet connection", auth.xbl_preauth_error_message())

    def test_a_rejected_token_asks_for_a_new_sign_in(self):
        """Microsoft refusing the saved token is not an Xbox profile problem:
        Xbox Live was never reached (#331)."""

        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(auth, "DATA", Path(td)), \
                    mock.patch.object(auth, "warn"), \
                    mock.patch.object(auth, "info"), \
                    mock.patch.object(auth, "ok"):
                self.assertFalse(auth.xbl_preauth(""))

        diagnostic = auth.xbl_preauth_diagnostic()
        self.assertEqual(diagnostic["stage"], "microsoft-token")
        self.assertEqual(diagnostic["category"], "signin")
        with mock.patch("bol.network.clock_is_unsynchronized",
                        return_value=True):
            message = auth.xbl_preauth_error_message()
        self.assertIn("Sign out in the launcher, sign in again", message)
        self.assertNotIn("Xbox profile", message)
        self.assertNotIn("clock", message)

    def test_accessor_returns_a_copy(self):
        self.run_failure(OSError("offline"))
        diagnostic = auth.xbl_preauth_diagnostic()
        diagnostic["message"] = self.response_secret

        self.assertNotEqual(
            auth.xbl_preauth_diagnostic()["message"], self.response_secret)


class ClockSkewGuidanceTests(unittest.TestCase):
    """A drifted clock is rejected exactly like an unusable Xbox account."""

    def setUp(self):
        auth._clear_xbl_preauth_diagnostic()
        self.addCleanup(auth._clear_xbl_preauth_diagnostic)

    @staticmethod
    def _message(category, unsynchronized):
        auth._record_xbl_preauth_diagnostic("user-auth", category)
        with mock.patch("bol.network.clock_is_unsynchronized",
                        return_value=unsynchronized):
            return auth.xbl_preauth_error_message()

    def test_account_rejection_names_the_clock_when_it_is_adrift(self):
        message = self._message("account", True)

        self.assertIn("Xbox profile", message)
        self.assertIn("clock", message)
        self.assertIn("timedatectl set-ntp true", message)

    def test_no_clock_advice_when_the_clock_is_fine(self):
        message = self._message("account", False)

        self.assertIn("Xbox profile", message)
        self.assertNotIn("clock", message)

    def test_network_failures_keep_their_own_advice(self):
        """A dead network is not a clock problem, however adrift the clock."""

        message = self._message("network", True)

        self.assertIn("DNS", message)
        self.assertNotIn("clock", message)

    def test_a_failing_clock_probe_never_breaks_the_message(self):
        auth._record_xbl_preauth_diagnostic("user-auth", "account")
        with mock.patch("bol.network.clock_is_unsynchronized",
                        side_effect=OSError("no timedatectl")):
            self.assertIn("Xbox profile", auth.xbl_preauth_error_message())


if __name__ == "__main__":
    unittest.main()
