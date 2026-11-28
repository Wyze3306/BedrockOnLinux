"""A stale copy of the settings writes back only what its holder changed."""
# SPDX-License-Identifier: MIT

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import util


class SettingsMergeTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        data = Path(self._td.name)
        self.file = data / "settings.json"
        for patcher in (mock.patch.object(util, "DATA", data),
                        mock.patch.object(util, "SETTINGS", self.file),
                        mock.patch.object(util, "_ensure_xdg_storage")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._td.cleanup)

    def write(self, data):
        self.file.write_text(json.dumps(data))

    def read(self):
        return json.loads(self.file.read_text())

    def test_a_stale_copy_keeps_what_another_writer_saved_meanwhile(self):
        # The #324 shape: a launch records a flag while another holder (the
        # engine check, a download, the window) still has its older copy.
        self.write({"_confine_applied": False, "mc_version": "1.26.65.1"})
        stale = util.load_settings()
        launch = util.load_settings()
        launch["_confine_applied"] = True
        util.save_settings(launch)
        stale["mc_version"] = "1.26.70.1"
        util.save_settings(stale)
        saved = self.read()
        self.assertIs(saved["_confine_applied"], True)
        self.assertEqual(saved["mc_version"], "1.26.70.1")

    def test_a_key_the_holder_removed_is_removed(self):
        self.write({"gone": 1, "kept": 2})
        s = util.load_settings()
        other = util.load_settings()
        other["new"] = 3
        util.save_settings(other)
        del s["gone"]
        util.save_settings(s)
        self.assertEqual(self.read(),
                         {"kept": 2, "new": 3, "proton_source": "winegdk"})

    def test_saving_the_same_copy_again_does_not_undo_a_later_change(self):
        self.write({"theme": "dark"})
        held = util.load_settings()
        held["rail_collapsed"] = True
        util.save_settings(held)
        other = util.load_settings()
        other["theme"] = "light"
        util.save_settings(other)
        util.save_settings(held)
        self.assertEqual(self.read()["theme"], "light")
        self.assertIs(self.read()["rail_collapsed"], True)

    def test_a_copy_of_a_loaded_dict_merges_too(self):
        # bol.proton keeps such a copy across a 1.5 GiB extraction.
        self.write({"proton_tag": "old"})
        s = util.load_settings()
        updated = s.copy()
        updated["proton_tag"] = "new"
        other = util.load_settings()
        other["ui_edition"] = "preview"
        util.save_settings(other)
        util.save_settings(updated)
        saved = self.read()
        self.assertEqual(saved["proton_tag"], "new")
        self.assertEqual(saved["ui_edition"], "preview")
        self.assertIsInstance(updated, util.LoadedSettings)

    def test_a_plain_dict_still_replaces_the_file(self):
        self.write({"a": 1, "b": 2})
        util.save_settings({"a": 5})
        self.assertEqual(self.read(), {"a": 5})

    def test_an_unreadable_file_is_replaced_whole(self):
        self.write({"a": 1})
        s = util.load_settings()
        self.file.write_text("{broken")
        s["b"] = 2
        util.save_settings(s)
        self.assertEqual(self.read(),
                         {"a": 1, "b": 2, "proton_source": "winegdk"})

    def test_a_missing_file_gets_what_the_holder_changed(self):
        s = util.load_settings()
        s["a"] = 1
        util.save_settings(s)
        self.assertEqual(self.read(), {"a": 1, "proton_source": "winegdk"})

    def test_values_inside_lists_are_compared_by_content(self):
        self.write({"dismissed_notices": ["one"]})
        s = util.load_settings()
        s["dismissed_notices"].append("two")
        util.save_settings(s)
        self.assertEqual(self.read()["dismissed_notices"], ["one", "two"])


if __name__ == "__main__":
    unittest.main()
