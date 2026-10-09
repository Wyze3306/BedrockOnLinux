"""Server resource packs in the prefix's Temp do not pile up for ever.

Each pack version a server sends is unpacked in its own folder, and nothing
cleans Temp under Wine: one prefix held 28 versions of the same pack.
"""
# SPDX-License-Identifier: MIT

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from bol import packcache

DAY = 24 * 3600
UI_PACK = "9251abd9-5f21-48d7-8b33-86db45d27342"


class PackCacheTests(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.prefix = Path(holder.name)
        self.now = time.time()
        self.cache = (self.prefix / packcache._TEMP / "Minecraft Bedrock"
                      / "minecraftpe" / "packcache" / "resource")
        self.cache.mkdir(parents=True)

    def _pack(self, name, uuid, version, age_days, cache=None, files=3):
        folder = (cache or self.cache) / name
        (folder / "textures").mkdir(parents=True)
        (folder / "manifest.json").write_text(json.dumps(
            {"format_version": 2,
             "header": {"uuid": uuid, "version": version, "name": name}}))
        for index in range(files):
            (folder / "textures" / f"{index}.png").write_bytes(b"x" * 100)
        when = self.now - age_days * DAY
        for path in [folder, *folder.rglob("*")]:
            os.utime(path, (when, when))
        return folder

    def test_old_versions_of_a_pack_go_and_the_newest_stays(self):
        old = self._pack("a", UI_PACK, [3, 56, 1], age_days=9)
        older = self._pack("b", UI_PACK, [3, 9, 0], age_days=20)
        newest = self._pack("c", UI_PACK, [3, 68, 0], age_days=30)

        removed, freed = packcache.prune(self.prefix, now=self.now)

        self.assertEqual(removed, 2)
        self.assertGreater(freed, 0)
        self.assertFalse(old.exists())
        self.assertFalse(older.exists())
        # Months unused, but the newest a server sent: kept.
        self.assertTrue(newest.exists())

    def test_a_version_replaced_within_the_week_is_kept(self):
        recent = self._pack("a", UI_PACK, [3, 66, 0], age_days=2)
        self._pack("b", UI_PACK, [3, 68, 0], age_days=1)
        self.assertEqual(packcache.prune(self.prefix, now=self.now), (0, 0))
        self.assertTrue(recent.exists())

    def test_versions_compare_as_numbers(self):
        nine = self._pack("a", UI_PACK, [3, 9, 0], age_days=30)
        ten = self._pack("b", UI_PACK, [3, 10, 0], age_days=30)
        packcache.prune(self.prefix, now=self.now)
        self.assertFalse(nine.exists())
        self.assertTrue(ten.exists())

    def test_different_packs_never_replace_each_other(self):
        one = self._pack("a", UI_PACK, [1, 0, 0], age_days=30)
        other = self._pack("b", "0b3d1f6e-5a2c-4c8e-9d1f-2a7b6c5d4e3f",
                           [9, 0, 0], age_days=30)
        self.assertEqual(packcache.prune(self.prefix, now=self.now), (0, 0))
        self.assertTrue(one.exists() and other.exists())

    def test_a_folder_it_cannot_read_is_left_alone(self):
        self._pack("a", UI_PACK, [3, 68, 0], age_days=30)
        stray = self.cache / "unreadable"
        stray.mkdir()
        (stray / "manifest.json").write_text("{not json")
        os.utime(stray, (self.now - 60 * DAY,) * 2)
        packcache.prune(self.prefix, now=self.now)
        self.assertTrue(stray.exists())

    def test_preview_has_its_own_cache(self):
        preview = (self.prefix / packcache._TEMP / "Minecraft Bedrock Preview"
                   / "minecraftpe" / "packcache" / "resource")
        preview.mkdir(parents=True)
        old = self._pack("a", UI_PACK, [1, 0, 0], age_days=30, cache=preview)
        self._pack("b", UI_PACK, [2, 0, 0], age_days=30, cache=preview)
        # The stable cache holds only the old version: it stays there.
        stable = self._pack("c", UI_PACK, [1, 0, 0], age_days=30)
        packcache.prune(self.prefix, now=self.now)
        self.assertFalse(old.exists())
        self.assertTrue(stable.exists())

    def test_a_prefix_without_a_cache_is_nothing_to_do(self):
        self.assertEqual(packcache.prune(self.prefix / "nowhere"), (0, 0))


if __name__ == "__main__":
    unittest.main()
