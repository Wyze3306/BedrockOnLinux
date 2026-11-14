"""Ed25519 verification must hold with and without `cryptography`."""
# SPDX-License-Identifier: MIT

import unittest

from bol import ed25519

# RFC 8032, section 7.1, TEST 1 to TEST 3.
VECTORS = (
    ("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
     "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
     "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
     "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
     "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
     "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
)


class Ed25519Tests(unittest.TestCase):
    def test_the_rfc_vectors_verify_on_both_paths(self):
        for public, message, signature in VECTORS:
            args = (bytes.fromhex(public), bytes.fromhex(message),
                    bytes.fromhex(signature))
            self.assertTrue(ed25519.verify(*args, pure=True))
            self.assertTrue(ed25519.verify(*args))

    def test_a_changed_message_or_signature_is_refused(self):
        public, message, signature = (bytes.fromhex(v) for v in VECTORS[2])
        flipped = bytearray(signature)
        flipped[3] ^= 1
        for pure in (True, False):
            self.assertFalse(ed25519.verify(public, message + b"!",
                                            signature, pure=pure))
            self.assertFalse(ed25519.verify(public, message, bytes(flipped),
                                            pure=pure))

    def test_malformed_input_is_not_a_signature_rather_than_an_error(self):
        public, message, signature = (bytes.fromhex(v) for v in VECTORS[0])
        for pure in (True, False):
            self.assertFalse(ed25519.verify(public[:31], message, signature,
                                            pure=pure))
            self.assertFalse(ed25519.verify(public, message, signature[:63],
                                            pure=pure))
            self.assertFalse(ed25519.verify(b"\xff" * 32, message, signature,
                                            pure=pure))
        self.assertFalse(ed25519.verify(None, message, signature))

    def test_a_scalar_past_the_group_order_is_refused(self):
        public, message, signature = (bytes.fromhex(v) for v in VECTORS[0])
        s = int.from_bytes(signature[32:], "little") + ed25519._L
        forged = signature[:32] + s.to_bytes(32, "little")
        self.assertFalse(ed25519.verify(public, message, forged, pure=True))


if __name__ == "__main__":
    unittest.main()
