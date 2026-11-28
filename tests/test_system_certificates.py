"""The certificates the launcher's own HTTPS requests trust (#249).

An OpenSSL from Guix or Nix looks for certificates in its store directory,
which holds none, and Guix on another distribution points SSL_CERT_FILE at a
~/.guix-profile that a sandbox may not show.
"""
# SPDX-License-Identifier: MIT

import ssl
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import fixups, util

PEM = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"


class SystemCertificatesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bundle = self.root / "system.pem"
        self.bundle.write_text(PEM)

    def _paths(self, cafile, capath):
        return ssl.DefaultVerifyPaths(
            str(cafile), str(capath), "SSL_CERT_FILE", str(cafile),
            "SSL_CERT_DIR", str(capath))

    def _use(self, environ, cafile, capath, bundle=None):
        with mock.patch.object(ssl, "get_default_verify_paths",
                               return_value=self._paths(cafile, capath)), \
                mock.patch.object(fixups, "_system_ca_bundle",
                                  return_value=bundle):
            return util.use_system_certificates(environ)

    def test_openssl_s_own_certificates_are_kept(self):
        cafile = self.root / "cert.pem"
        cafile.write_text(PEM)
        environ = {}
        self.assertIsNone(self._use(environ, cafile, self.root / "none",
                                    bundle=self.bundle))
        self.assertEqual(environ, {})

    def test_a_folder_of_certificates_is_enough(self):
        folder = self.root / "certs"
        folder.mkdir()
        (folder / "5ad8a5d6.0").write_text(PEM)
        environ = {}
        self.assertIsNone(self._use(environ, self.root / "none.pem", folder,
                                    bundle=self.bundle))
        self.assertEqual(environ, {})

    def test_an_empty_store_directory_gets_the_system_bundle(self):
        # Guix's OpenSSL: <store>/etc/ssl/cert.pem missing, certs/ empty.
        store = self.root / "store-openssl" / "etc" / "ssl"
        (store / "certs").mkdir(parents=True)
        environ = {}
        self.assertEqual(self._use(environ, store / "cert.pem",
                                   store / "certs", bundle=self.bundle),
                         self.bundle)
        self.assertEqual(environ, {"SSL_CERT_FILE": str(self.bundle)})

    def test_a_dangling_profile_path_is_replaced(self):
        # Guix on a foreign distribution, in a sandbox that hides the profile.
        gone = self.root / "home" / ".guix-profile" / "etc" / "ssl"
        environ = {"SSL_CERT_FILE": str(gone / "certs" / "ca.crt"),
                   "SSL_CERT_DIR": str(gone / "certs")}
        store = self.root / "store"
        self.assertEqual(self._use(environ, store / "cert.pem",
                                   store / "certs", bundle=self.bundle),
                         self.bundle)
        self.assertEqual(environ["SSL_CERT_FILE"], str(self.bundle))

    def test_a_stub_file_does_not_count_as_certificates(self):
        cafile = self.root / "cert.pem"
        cafile.write_text("# placeholder\n")
        environ = {}
        self.assertEqual(self._use(environ, cafile, self.root / "none",
                                   bundle=self.bundle), self.bundle)

    def test_nothing_changes_without_a_bundle_to_give(self):
        environ = {}
        self.assertIsNone(self._use(environ, self.root / "none.pem",
                                    self.root / "none"))
        self.assertEqual(environ, {})


if __name__ == "__main__":
    unittest.main()
