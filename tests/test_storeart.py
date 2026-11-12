import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import storeart


def _catalog(*images):
    return json.dumps({"Products": [{"LocalizedProperties": [
        {"Images": list(images)}]}]}).encode()


class LogoUrlTests(unittest.TestCase):
    def test_the_largest_logo_is_picked_and_scaled_by_the_cdn(self):
        data = _catalog(
            {"ImagePurpose": "Logo", "Width": 100,
             "Uri": "//store-images.s-microsoft.com/image/small"},
            {"ImagePurpose": "Logo", "Width": 1080,
             "Uri": "//store-images.s-microsoft.com/image/big"},
            {"ImagePurpose": "Screenshot", "Width": 4000,
             "Uri": "//store-images.s-microsoft.com/image/shot"})
        with mock.patch.object(storeart, "_get", return_value=data):
            self.assertEqual(
                storeart.logo_url("9NBLGGH2JHXJ"),
                "https://store-images.s-microsoft.com/image/big?w=128&h=128")

    def test_a_logo_on_another_host_is_refused(self):
        data = _catalog({"ImagePurpose": "Logo", "Width": 300,
                         "Uri": "https://example.com/logo.png"})
        with mock.patch.object(storeart, "_get", return_value=data):
            self.assertIsNone(storeart.logo_url("X"))

    def test_an_unknown_product_has_no_logo(self):
        with mock.patch.object(storeart, "_get",
                               return_value=b'{"Products": []}'):
            self.assertIsNone(storeart.logo_url("X"))


class LogoBytesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(storeart, "ART_CACHE", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_fetched_logo_is_cached(self):
        with mock.patch.object(storeart, "logo_url", return_value="u"), \
                mock.patch.object(storeart, "_get", return_value=b"png"):
            self.assertEqual(storeart.logo_bytes("P"), b"png")
        with mock.patch.object(storeart, "logo_url") as url:
            self.assertEqual(storeart.logo_bytes("P"), b"png")
        url.assert_not_called()

    def test_offline_falls_back_to_the_stale_copy(self):
        path = Path(self.tmp.name) / "P.img"
        path.write_bytes(b"old")
        os.utime(path, (0, 0))
        with mock.patch.object(storeart, "logo_url",
                               side_effect=OSError("offline")):
            self.assertEqual(storeart.logo_bytes("P"), b"old")
            self.assertIsNone(storeart.logo_bytes("Q"))

    def test_logos_skips_editions_without_one(self):
        with mock.patch.object(storeart, "logo_bytes",
                               side_effect=lambda p: b"x" if p == "A" else None):
            self.assertEqual(
                storeart.logos([{"id": "a", "product": "A"},
                                {"id": "b", "product": "B"}]),
                {"a": b"x"})


if __name__ == "__main__":
    unittest.main()
