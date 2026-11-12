"""The vendored vkd3d-proton bundle, its build recipe and its release wiring
must describe one and the same payload."""
from __future__ import annotations

import hashlib
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "third_party/vkd3d-proton-universal"
BUILD_RECIPE = ROOT / "scripts/build-vkd3d-universal.sh"
PACKAGE_ENGINE = ROOT / "scripts/package-engine.sh"

# provenance.env key -> the bundle file whose SHA-256 it locks.
LOCKED_FILES = {
    "VKD3D_REVERT_PATCH_SHA256": "restore-nv-dgc.patch",
    "VKD3D_OCCLUSION_PATCH_SHA256": "fix-occluded-frame-latency.patch",
    "VKD3D_LIMITER_PATCH_SHA256": "fix-frame-limiter-busy-wait.patch",
    "VKD3D_LICENSE_SHA256": "COPYING.LGPL-2.1",
    "VKD3D_SUBMODULE_LOCK_SHA256": "submodules.lock",
    "VKD3D_OUTPUT_HASHES_SHA256": "OUTPUT-SHA256SUMS",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _lock() -> dict:
    values = {}
    for line in (BUNDLE / "provenance.env").read_text().splitlines():
        match = re.fullmatch(r"([A-Z0-9_]+)='([^']*)'", line)
        if match:
            values[match.group(1)] = match.group(2)
    return values


class VendoredBundleTests(unittest.TestCase):
    def test_provenance_lock_matches_every_vendored_file(self):
        lock = _lock()
        for key, name in LOCKED_FILES.items():
            with self.subTest(file=name):
                self.assertEqual(lock[key], _sha256(BUNDLE / name))

    def test_packager_pins_the_same_bundle_the_recipe_builds(self):
        script = PACKAGE_ENGINE.read_text(encoding="utf-8")
        body = script[script.index("verify_reviewed_provenance() {"):]
        body = body[:body.index("\n}\n")]
        pinned = dict(
            (name, digest) for name, digest in re.findall(
                r'verify_sha256 "\$root/([^"]+)" \\\n\s+"([0-9a-f]{64})"',
                body))
        expected = {name: _sha256(BUNDLE / name)
                    for name in LOCKED_FILES.values()}
        expected["provenance.env"] = _sha256(BUNDLE / "provenance.env")
        self.assertEqual(pinned, expected)

        files = script[script.index("PROVENANCE_FILES=("):]
        files = files[:files.index(")")].split()[1:]
        self.assertEqual(sorted(files), sorted(expected))

    def test_output_hash_lock_lists_the_four_universal_dlls(self):
        entries = [line.split() for line in
                   (BUNDLE / "OUTPUT-SHA256SUMS").read_text().splitlines()]
        self.assertEqual([path for _, path in entries], [
            "x64/d3d12.dll", "x64/d3d12core.dll",
            "x86/d3d12.dll", "x86/d3d12core.dll"])
        for digest, _ in entries:
            self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_recipe_applies_the_limiter_fix_after_the_occlusion_fix(self):
        script = BUILD_RECIPE.read_text(encoding="utf-8")
        revert_check = script.index(
            'cmp "$REVERT_PATCH" "$GENERATED_PATCH"')
        occlusion = script.index(
            'apply --index --whitespace=nowarn "$OCCLUSION_PATCH"')
        limiter = script.index(
            'apply --index --whitespace=nowarn "$LIMITER_PATCH"')
        first_build = script.index("build_arch 64 ")
        self.assertLess(revert_check, occlusion)
        self.assertLess(occlusion, limiter)
        self.assertLess(limiter, first_build)
        self.assertIn('"$VKD3D_LIMITER_PATCH_SHA256"', script)
        # The patch travels with the built payload's provenance.
        install = script[script.index('install -m 0644 "$LOCK_FILE"'):]
        self.assertIn('"$LIMITER_PATCH"', install[:install.index("\n{")])

    def test_limiter_patch_only_changes_the_wine_threshold(self):
        patch = (BUNDLE / "fix-frame-limiter-busy-wait.patch").read_text()
        self.assertEqual(
            re.findall(r"^diff --git a/(\S+)", patch, re.M),
            ["libs/vkd3d/swapchain.c"])
        self.assertIn("+    if (is_wine && sleep_state->NtDelayExecution)",
                      patch)
        self.assertIn("+        sleep_state->sleep_threshold_ns = 500000;",
                      patch)
        # Windows and native builds keep the scaled upstream threshold.
        self.assertEqual(
            patch.count("+    sleep_state->sleep_threshold_scales = true;"),
            2)


if __name__ == "__main__":
    unittest.main()
