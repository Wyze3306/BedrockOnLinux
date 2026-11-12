"""The Steam Linux Runtime's missing locales are built once, then kept.

pressure-vessel compiles every locale the game wants whenever the host lacks
en_US.UTF-8 or the player's own, at each launch: 3 to 6 seconds before the
window on a French desktop with only fr_FR.UTF-8. These tests hold the kept
copy to what pressure-vessel would have built -- same names, same tool --
and to the old behaviour whenever anything about it is in doubt.
"""
# SPDX-License-Identifier: MIT

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import slr_locales


# Stands in for _v2-entry-point running pv-locale-gen in the container:
# records its call, then behaves as STATUS says.
_FAKE_ENTRY_POINT = """#!{python}
import json, os, sys
argv = sys.argv[1:]
assert argv[0] == "--verb=waitforexitandrun" and argv[1] == "--", argv
assert argv[2].endswith("/pv-locale-gen"), argv
out = argv[argv.index("--output-dir") + 1]
names = argv[argv.index("--output-dir") + 2:]
with open({calls!r}, "a") as log:
    log.write(json.dumps({{"names": names, "env": {{
        k: os.environ.get(k) for k in
        ("LANG", "LC_ALL", "LOCPATH", "PRESSURE_VESSEL_GENERATE_LOCALES")}}
    }}) + "\\n")
status = int(open({status!r}).read())
if status == 72:
    skip = os.environ.get("FAKE_SKIP", "")
    for name in names:
        if name != skip:
            os.makedirs(os.path.join(out, name), exist_ok=True)
            open(os.path.join(out, name, "LC_CTYPE"), "w").write("x")
sys.exit(status)
"""


class _Runtime:
    """A fake steamrt3 directory with a scriptable entry point."""

    def __init__(self, base, status=72, depot="3.0.20260714.251853"):
        self.root = Path(base) / "steamrt3"
        self.root.mkdir()
        (self.root / "VERSIONS.txt").write_text(
            "#Name\tVersion\n"
            f"depot\t{depot}\t\t\t# Overall version number\n")
        self.calls = Path(base) / "calls.jsonl"
        self.status = Path(base) / "status"
        self.set_status(status)
        entry = self.root / "_v2-entry-point"
        entry.write_text(_FAKE_ENTRY_POINT.format(
            python=sys.executable, calls=str(self.calls),
            status=str(self.status)))
        entry.chmod(0o755)

    def set_status(self, status):
        self.status.write_text(str(status))

    def invocations(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in
                self.calls.read_text().splitlines()]


def _only(*present):
    return lambda name: name in present


class WantedLocalesTests(unittest.TestCase):
    def test_follows_pv_locale_gen(self):
        self.assertEqual(
            slr_locales.wanted_locales(
                {"LC_TIME": "de_DE.UTF-8", "LANG": "fr_FR.UTF-8",
                 "LC_ALL": "fr_FR.UTF-8"}),
            ["de_DE.UTF-8", "fr_FR.UTF-8", "en_US.UTF-8"])

    def test_c_posix_and_paths_are_never_generated(self):
        self.assertEqual(
            slr_locales.wanted_locales(
                {"LANG": "C.UTF-8", "LC_ALL": "POSIX", "LC_CTYPE": "C",
                 "LC_TIME": "../../etc", "LC_NAME": "a/b"}),
            ["en_US.UTF-8"])


class ConfigureTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.runtime = _Runtime(self.base)
        self.cache = self.base / "cache" / "slr-locales"
        for patcher in (
                mock.patch.object(slr_locales, "CACHE_DIR", self.cache),
                mock.patch.object(slr_locales, "LOGS", self.base / "logs"),
                mock.patch.object(slr_locales, "host_glibc",
                                  return_value="glibc 2.41"),
                mock.patch.object(slr_locales, "_host_loads",
                                  return_value=True),
                mock.patch.object(slr_locales, "info")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _configure(self, env, present=("fr_FR.UTF-8",)):
        return slr_locales.configure(env, available=_only(*present),
                                     root=self.runtime.root)

    def test_first_launch_builds_and_hands_the_copy_over(self):
        env = {"LANG": "fr_FR.UTF-8"}
        kept = self._configure(env)
        self.assertIsNotNone(kept)
        self.assertEqual(env["LOCPATH"], kept)
        self.assertEqual(env["PRESSURE_VESSEL_GENERATE_LOCALES"], "0")
        for name in ("fr_FR.UTF-8", "en_US.UTF-8"):
            self.assertTrue((Path(kept) / name / "LC_CTYPE").is_file())
        (call,) = self.runtime.invocations()
        # Every wanted locale, not only the missing one: LOCPATH hides the
        # host's locale archive from the game.
        self.assertEqual(call["names"], ["fr_FR.UTF-8", "en_US.UTF-8"])
        self.assertEqual(call["env"]["LANG"], "fr_FR.UTF-8")
        self.assertIsNone(call["env"]["LOCPATH"])
        # Or the container's own startup would build a throwaway set first.
        self.assertEqual(call["env"]["PRESSURE_VESSEL_GENERATE_LOCALES"], "0")
        self.assertEqual(slr_locales.state(
            {"LANG": "fr_FR.UTF-8"}, available=_only("fr_FR.UTF-8"),
            root=self.runtime.root), "cached")

    def test_later_launches_reuse_it_without_the_runtime(self):
        first = self._configure({"LANG": "fr_FR.UTF-8"})
        env = {"LANG": "fr_FR.UTF-8"}
        self.assertEqual(self._configure(env), first)
        self.assertEqual(env["LOCPATH"], first)
        self.assertEqual(len(self.runtime.invocations()), 1)

    def test_a_new_glibc_or_runtime_builds_a_fresh_copy(self):
        first = self._configure({"LANG": "fr_FR.UTF-8"})
        with mock.patch.object(slr_locales, "host_glibc",
                               return_value="glibc 2.42"):
            second = self._configure({"LANG": "fr_FR.UTF-8"})
        self.assertNotEqual(first, second)
        self.assertFalse(Path(first).exists(), "the stale copy is pruned")
        (self.runtime.root / "VERSIONS.txt").write_text("depot\t3.0.1\n")
        with mock.patch.object(slr_locales, "host_glibc",
                               return_value="glibc 2.42"):
            third = self._configure({"LANG": "fr_FR.UTF-8"})
        self.assertNotEqual(second, third)
        self.assertEqual(len(self.runtime.invocations()), 3)

    def test_another_language_is_another_copy(self):
        self._configure({"LANG": "fr_FR.UTF-8"})
        env = {"LANG": "de_DE.UTF-8"}
        self._configure(env, present=())
        self.assertTrue((Path(env["LOCPATH"]) / "de_DE.UTF-8").is_dir())

    def test_nothing_to_do_when_the_host_has_them_all(self):
        env = {"LANG": "fr_FR.UTF-8"}
        self.assertIsNone(self._configure(
            env, present=("fr_FR.UTF-8", "en_US.UTF-8")))
        self.assertEqual(env, {"LANG": "fr_FR.UTF-8"})
        self.assertEqual(self.runtime.invocations(), [])

    def test_the_players_own_choice_is_left_alone(self):
        for extra in ({"PRESSURE_VESSEL_GENERATE_LOCALES": "0"},
                      {"PRESSURE_VESSEL_GENERATE_LOCALES": "1"},
                      {"LOCPATH": "/somewhere"}):
            env = {"LANG": "fr_FR.UTF-8", **extra}
            self.assertIsNone(self._configure(env))
            self.assertEqual(env, {"LANG": "fr_FR.UTF-8", **extra})
        self.assertEqual(self.runtime.invocations(), [])

    def test_no_runtime_yet_means_no_change(self):
        env = {"LANG": "fr_FR.UTF-8"}
        self.assertIsNone(slr_locales.configure(
            env, available=_only("fr_FR.UTF-8"),
            root=self.base / "missing"))
        self.assertNotIn("LOCPATH", env)

    def test_a_failure_keeps_the_old_behaviour_and_is_not_retried(self):
        self.runtime.set_status(1)
        env = {"LANG": "fr_FR.UTF-8"}
        self.assertIsNone(self._configure(env))
        self.assertNotIn("LOCPATH", env)
        self.assertNotIn("PRESSURE_VESSEL_GENERATE_LOCALES", env)
        self.runtime.set_status(72)
        self.assertIsNone(self._configure({"LANG": "fr_FR.UTF-8"}))
        self.assertEqual(len(self.runtime.invocations()), 1)
        self.assertEqual(slr_locales.state(
            {"LANG": "fr_FR.UTF-8"}, available=_only("fr_FR.UTF-8"),
            root=self.runtime.root), "failed")
        # A week later it is worth one more try.
        for marker in self.cache.glob("*.failed"):
            old = marker.stat().st_mtime - slr_locales.RETRY_AFTER - 60
            os.utime(marker, (old, old))
        self.assertIsNotNone(self._configure({"LANG": "fr_FR.UTF-8"}))
        self.assertEqual(len(self.runtime.invocations()), 2)

    def test_an_incomplete_copy_is_not_used(self):
        with mock.patch.dict(os.environ, {"FAKE_SKIP": "en_US.UTF-8"}):
            env = {"LANG": "fr_FR.UTF-8"}
            self.assertIsNone(self._configure(env))
        self.assertNotIn("LOCPATH", env)
        self.assertEqual([p.name for p in self.cache.iterdir()
                          if not p.name.endswith(".failed")], [])

    def test_a_copy_this_glibc_cannot_read_is_not_used(self):
        with mock.patch.object(slr_locales, "_host_loads",
                               return_value=False):
            env = {"LANG": "fr_FR.UTF-8"}
            self.assertIsNone(self._configure(env))
        self.assertNotIn("LOCPATH", env)

    def test_a_container_that_has_them_all_is_remembered(self):
        self.runtime.set_status(0)
        env = {"LANG": "fr_FR.UTF-8"}
        self.assertIsNone(self._configure(env))
        self.assertNotIn("LOCPATH", env)
        self.assertIsNone(self._configure({"LANG": "fr_FR.UTF-8"}))
        self.assertEqual(len(self.runtime.invocations()), 1)
        self.assertEqual(slr_locales.state(
            {"LANG": "fr_FR.UTF-8"}, available=_only("fr_FR.UTF-8"),
            root=self.runtime.root), "none-needed")

    def test_pending_before_the_first_launch(self):
        self.assertEqual(slr_locales.state(
            {"LANG": "fr_FR.UTF-8"}, available=_only("fr_FR.UTF-8"),
            root=self.runtime.root), "pending")
        self.assertIsNone(slr_locales.state(
            {"LANG": "fr_FR.UTF-8"},
            available=_only("fr_FR.UTF-8", "en_US.UTF-8"),
            root=self.runtime.root))


class HostLoadsTests(unittest.TestCase):
    def test_rejects_files_that_are_not_compiled_locales(self):
        with tempfile.TemporaryDirectory() as td:
            bogus = Path(td) / "xx_BOGUS.UTF-8"
            bogus.mkdir()
            for category in ("LC_CTYPE", "LC_COLLATE", "LC_TIME"):
                (bogus / category).write_bytes(b"not a locale")
            self.assertFalse(
                slr_locales._host_loads(Path(td), ["xx_BOGUS.UTF-8"]))


if __name__ == "__main__":
    unittest.main()
