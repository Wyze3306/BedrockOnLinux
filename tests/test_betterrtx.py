"""BetterRTX presets, installed into a Minecraft build (#166).

A preset is three compiled RenderDragon materials copied over the game's
own. The game loads only materials of its own format, so these tests hold
the check that comes before every write, and the way back to the originals.
"""
# SPDX-License-Identifier: MIT

import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from bol import betterrtx
from bol.log import BolError


def _material(version, tag=b""):
    name = b"RenderDragon.CompiledMaterialDefinition"
    return (struct.pack("<QI", 0x0A11DA1A, len(name)) + name
            + struct.pack("<Q", version) + tag)


def _api_entry(uuid, version=25, game="v26.40.26"):
    base = f"https://cdn.bedrock.graphics/presets/base/{game}/1.4.4/{uuid}/"
    entry = {"uuid": uuid, "name": f"BetterRTX {uuid}",
             "stub": base + "RTXStub.material.bin",
             "tonemapping": base + "RTXPostFX.Tonemapping.material.bin",
             "bloom": base + "RTXPostFX.Bloom.material.bin",
             "gameVersion": game}
    if version is not None:
        entry["materialVersion"] = version
    return entry


class BuildCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.game = Path(self.tmp.name) / "1.26.40.4"
        self.materials = self.game / "data" / "renderer" / "materials"
        self.materials.mkdir(parents=True)
        (self.game / "Minecraft.Windows.exe").write_bytes(b"MZ")
        self.originals = {}
        for name in betterrtx.MATERIALS.values():
            self.originals[name] = _material(25, b"vanilla " + name.encode())
            (self.materials / name).write_bytes(self.originals[name])
        closed = mock.patch("bol.prefix._mc_running", return_value=False)
        self.running = closed.start()
        self.addCleanup(closed.stop)

    def _downloader(self, version=25):
        def fetch(url, dest, label=None):
            Path(dest).write_bytes(_material(version, url.encode()))
        return fetch

    def _current(self):
        return {name: (self.materials / name).read_bytes()
                for name in betterrtx.MATERIALS.values()}


class FormatTests(unittest.TestCase):
    def test_the_header_names_the_format(self):
        self.assertEqual(betterrtx.material_version(_material(26)), 26)

    def test_anything_else_has_no_format(self):
        self.assertIsNone(betterrtx.material_version(b"PK\x03\x04 a zip"))
        self.assertIsNone(betterrtx.material_version(b""))


class PresetListTests(unittest.TestCase):
    def test_presets_keep_their_links_and_format(self):
        presets = betterrtx.list_presets(fetch=lambda _url: [
            _api_entry("default"), _api_entry("old", version=None),
            {"uuid": "broken", "stub": "http://insecure/x"}, "junk"])
        self.assertEqual([p["uuid"] for p in presets], ["default", "old"])
        self.assertEqual(presets[0]["material_version"], 25)
        self.assertEqual(presets[0]["game_version"], "26.40.26")
        self.assertIsNone(presets[1]["material_version"])

    def test_an_unreachable_site_is_named(self):
        def offline(_url):
            raise OSError("no route to host")
        with self.assertRaisesRegex(BolError, "bedrock.graphics"):
            betterrtx.list_presets(fetch=offline)


class InstallTests(BuildCase):
    def _preset(self, version=25):
        return betterrtx.list_presets(
            fetch=lambda _url: [_api_entry("default", version=version)])[0]

    def test_a_preset_replaces_the_three_materials_and_keeps_the_originals(self):
        with mock.patch.object(betterrtx, "ok"):
            betterrtx.install_preset(self._preset(), self.game,
                                     downloader=self._downloader())
        current = self._current()
        for name, data in current.items():
            self.assertEqual(betterrtx.material_version(data), 25)
            self.assertIn(b"cdn.bedrock.graphics", data)
        kept = self.game / betterrtx.STATE_DIR / "original"
        for name, data in self.originals.items():
            self.assertEqual((kept / name).read_bytes(), data)
        self.assertEqual(betterrtx.status(self.game)["uuid"], "default")

    def test_a_preset_for_another_format_is_refused_before_any_download(self):
        def never(*_args, **_kwargs):
            raise AssertionError("downloaded a preset it would refuse")
        with self.assertRaisesRegex(BolError, "format 24"):
            betterrtx.install_preset(self._preset(version=24), self.game,
                                     downloader=never)
        self.assertEqual(self._current(), self.originals)

    def test_files_of_another_format_are_refused_before_any_write(self):
        # An older preset names no format; its files say it instead.
        with self.assertRaisesRegex(BolError, "format 24"):
            betterrtx.install_preset(self._preset(version=None), self.game,
                                     downloader=self._downloader(version=24))
        self.assertEqual(self._current(), self.originals)
        self.assertFalse((self.game / betterrtx.STATE_DIR).exists())

    def test_nothing_is_written_while_the_game_runs(self):
        self.running.return_value = True
        with self.assertRaisesRegex(BolError, "Close Minecraft"):
            betterrtx.install_preset(self._preset(), self.game,
                                     downloader=self._downloader())
        self.assertEqual(self._current(), self.originals)

    def test_a_second_preset_keeps_the_real_originals(self):
        with mock.patch.object(betterrtx, "ok"):
            for _ in range(2):
                betterrtx.install_preset(self._preset(), self.game,
                                         downloader=self._downloader())
            self.assertTrue(betterrtx.restore(self.game))
        self.assertEqual(self._current(), self.originals)

    def test_restore_puts_everything_back(self):
        with mock.patch.object(betterrtx, "ok"):
            betterrtx.install_preset(self._preset(), self.game,
                                     downloader=self._downloader())
            self.assertTrue(betterrtx.restore(self.game))
        self.assertEqual(self._current(), self.originals)
        self.assertFalse((self.game / betterrtx.STATE_DIR).exists())
        self.assertIsNone(betterrtx.status(self.game)["installed"])

    def test_restoring_a_build_without_betterrtx_changes_nothing(self):
        self.assertFalse(betterrtx.restore(self.game))
        self.assertEqual(self._current(), self.originals)


class RtpackTests(BuildCase):
    def _pack(self, files):
        pack = Path(self.tmp.name) / "Prizma.rtpack"
        with zipfile.ZipFile(pack, "w") as archive:
            for name, data in files.items():
                archive.writestr("Prizma/" + name, data)
        return pack

    def test_a_pack_installs_the_materials_it_holds(self):
        stub = _material(25, b"prizma stub")
        pack = self._pack({"RTXStub.material.bin": stub,
                           "readme.txt": b"hello"})
        with mock.patch.object(betterrtx, "ok"):
            betterrtx.install_rtpack(pack, self.game)
        current = self._current()
        self.assertEqual(current["RTXStub.material.bin"], stub)
        self.assertEqual(current["RTXPostFX.Bloom.material.bin"],
                         self.originals["RTXPostFX.Bloom.material.bin"])
        self.assertEqual(betterrtx.status(self.game)["installed"], "Prizma")

    def test_a_pack_for_another_format_is_refused(self):
        pack = self._pack({"RTXStub.material.bin": _material(26)})
        with self.assertRaisesRegex(BolError, "format 26"):
            betterrtx.install_rtpack(pack, self.game)
        self.assertEqual(self._current(), self.originals)

    def test_something_that_is_not_a_pack_is_named(self):
        bogus = Path(self.tmp.name) / "notes.rtpack"
        bogus.write_text("not a zip")
        with self.assertRaisesRegex(BolError, "not a BetterRTX pack"):
            betterrtx.install_rtpack(bogus, self.game)


if __name__ == "__main__":
    unittest.main()
