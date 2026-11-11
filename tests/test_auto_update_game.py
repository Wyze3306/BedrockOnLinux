"""Updating Minecraft before a start that shows no launcher window (#328)."""
# SPDX-License-Identifier: MIT

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import gamesetup

EDITION = {"id": "release", "name": "Minecraft", "beta": False}


def _versions(*names):
    return lambda _edition, offline_ok=True: [
        {"version": name, "urls": [], "installed": False} for name in names]


class UpdateBeforePlayTests(unittest.TestCase):
    def _run(self, settings, versions, setup=None, managed=True,
             always=False):
        with tempfile.TemporaryDirectory() as tmp:
            games_root = Path(tmp) / "games"
            folder = games_root / "release" / "1.26.60.5"
            folder.mkdir(parents=True)
            outside = Path(tmp) / "elsewhere"
            outside.mkdir()
            settings = dict(settings,
                            game_dir=str(folder if managed else outside))
            setup = setup or mock.Mock()
            with mock.patch.object(gamesetup, "GAMES", games_root), \
                    mock.patch.object(gamesetup, "load_settings",
                                      return_value=settings), \
                    mock.patch.object(gamesetup, "_auto_selection",
                                      return_value=(
                                          EDITION,
                                          settings.get("mc_version"))), \
                    mock.patch.object(gamesetup, "desktop_notify"):
                result = gamesetup.update_game_before_play(
                    always=always, setup=setup, versions=versions)
            return result, setup

    def test_a_newer_build_is_installed_and_named(self):
        result, setup = self._run(
            {"mc_version": "1.26.60.5", "auto_update_game": True},
            _versions("1.26.63.1", "1.26.60.5"))
        self.assertEqual(result, "1.26.63.1")
        setup.assert_called_once_with(mc_edition=EDITION,
                                      mc_version="1.26.63.1")

    def test_nothing_is_updated_unless_asked(self):
        result, setup = self._run({"mc_version": "1.26.60.5"},
                                  _versions("1.26.63.1", "1.26.60.5"))
        self.assertIsNone(result)
        setup.assert_not_called()

    def test_the_command_line_flag_asks_once(self):
        result, _ = self._run({"mc_version": "1.26.60.5"},
                              _versions("1.26.63.1"), always=True)
        self.assertEqual(result, "1.26.63.1")

    def test_the_newest_build_is_left_alone(self):
        result, setup = self._run({"mc_version": "1.26.63.1"},
                                  _versions("1.26.63.1", "1.26.60.5"),
                                  always=True)
        self.assertIsNone(result)
        setup.assert_not_called()

    def test_an_imported_copy_is_never_replaced(self):
        result, setup = self._run({"mc_version": "1.26.60.5"},
                                  _versions("1.26.63.1"), managed=False,
                                  always=True)
        self.assertIsNone(result)
        setup.assert_not_called()

    def test_an_unreachable_index_still_lets_the_game_start(self):
        def versions(_edition, offline_ok=True):
            raise OSError("network is unreachable")

        result, setup = self._run({"mc_version": "1.26.60.5"}, versions,
                                  always=True)
        self.assertIsNone(result)
        setup.assert_not_called()

    def test_a_failed_download_still_lets_the_game_start(self):
        setup = mock.Mock(side_effect=gamesetup.BolError("not signed in"))
        result, _ = self._run({"mc_version": "1.26.60.5"},
                              _versions("1.26.63.1"), setup=setup,
                              always=True)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
