"""PLAY with other profiles' games running (bol.gamesetup.setup_or_join)."""
# SPDX-License-Identifier: MIT

import fcntl
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import gamesetup, prefix
from bol.log import BolError

EDITION = {"id": "release", "name": "Minecraft"}


class SetupOrJoinTests(unittest.TestCase):
    def test_with_no_other_game_it_is_the_full_setup(self):
        with mock.patch.object(gamesetup, "do_setup") as setup:
            gamesetup.setup_or_join(EDITION, "1.26.60", busy=lambda: False)
        setup.assert_called_once_with(mc_edition=EDITION,
                                      mc_version="1.26.60", progress=None)

    def test_an_installed_build_joins_the_running_games(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "release" / "1.26.60"
            with mock.patch.object(gamesetup, "version_dir",
                                   return_value=folder), \
                    mock.patch.object(gamesetup, "_game_root",
                                      return_value=folder), \
                    mock.patch.object(gamesetup, "use_game_dir") as use, \
                    mock.patch.object(gamesetup, "do_setup") as setup:
                gamesetup.setup_or_join(EDITION, "1.26.60",
                                        busy=lambda: True)
        setup.assert_not_called()
        use.assert_called_once_with(folder)

    def test_a_build_to_download_says_why_it_waits(self):
        with mock.patch.object(gamesetup, "_game_root", return_value=None), \
                mock.patch.object(gamesetup, "do_setup") as setup:
            with self.assertRaises(BolError) as raised:
                gamesetup.setup_or_join(EDITION, "1.26.70",
                                        busy=lambda: True)
        setup.assert_not_called()
        said = str(raised.exception)
        self.assertIn("1.26.70 is not installed yet", said)
        self.assertIn("already installed", said)


class SharedAssetsBusyTests(unittest.TestCase):
    def test_reports_a_held_shared_lock_and_never_keeps_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            games = Path(tmp) / "games"
            with mock.patch.object(prefix, "GAMES", games):
                self.assertFalse(prefix.shared_assets_busy())
                lock = Path(tmp) / ".shared-assets.lock"
                fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    self.assertFalse(prefix.shared_assets_busy())
                    fcntl.flock(fd, fcntl.LOCK_SH)
                    self.assertTrue(prefix.shared_assets_busy())
                finally:
                    os.close(fd)
                self.assertFalse(prefix.shared_assets_busy())


if __name__ == "__main__":
    unittest.main()
