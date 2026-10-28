"""Regression tests for the two HBUI Play-screen patches.

Both were anchored on identifiers a Minecraft build re-minifies, so both went
quietly dead when the UI was rebundled: the Servers tab went back to "You need
a Microsoft account" and the in-game Sign-in link -- which reaches a sign-in
the engine does not implement -- came back to answer "Failed to log in"
(#227/#228/#229). The point of these tests is that the anchors survive
renaming, and that a build they cannot handle is reported instead of skipped.
"""
# SPDX-License-Identifier: MIT

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from bol import fixups

# The shape shipped by 1.26.44: module alias `r.`, component factory `n.`,
# notice component `nD`, state hook `T3`, warning component `I3`.
_GATE_NEW = (
    'function T3(){return(0,r.useFacetMap)(((e,t)=>{'
    'const{isSignedInPlatformNetwork:a,isLoggedInWithMicrosoftAccount:n,'
    'hasPremiumNetworkAccess:c}=e,l=pI(t.platform),o=dI(t.platform);'
    'return n&&!a&&l?"platform-guest-nintendo":!n&&a&&o&&!c?'
    '"msa-guest-playstation":"not-signed-in"}),[],[(0,r.useSharedFacet)(lC)])}'
)
_LINK_NEW = (
    'function I3({location:e}){const{t}=Ku("notLoggedInWarning"),a=f(),c=VT(),'
    'l=(0,n.useCallback)((()=>{c({screen:`${O3(e)}`,button:"SignIn"}),'
    'a.push(`/sign-in?signInSource=${O3(e)}_NotLoggedInWarning_OreUI`)}),'
    '[c,a,e]);return n.createElement(nD,{role:"noticeTint"},'
    'n.createElement(nD.Link,{onClick:l},t(".loginLink")))}'
)
# The shape the original needles were written for: alias `l.`, factory `r.`,
# notice component `sx`, hook `wB`, warning component `kB`.
_GATE_OLD = (
    'function wB(){return(0,l.useFacetMap)(((e,t)=>{'
    'const{isSignedInPlatformNetwork:a,isLoggedInWithMicrosoftAccount:n,'
    'hasPremiumNetworkAccess:c}=e;'
    'return!n?"msa-guest-playstation":"not-signed-in"}),[],[])}'
)
_LINK_OLD = (
    'function kB({location:e}){const l=(0,n.useCallback)((()=>{'
    'a.push(`/sign-in?signInSource=PlayScreen_NotLoggedInWarning_OreUI`)}),'
    '[a,e]);return r.createElement(sx,{role:"noticeTint"},'
    'r.createElement(sx.Link,{onClick:l},"login"))}'
)
# The shape shipped by 1.26.60: hooks are called directly (`k(` for
# useFacetMap) and a lone arrow function lost its parentheses.
_GATE_DIRECT = (
    'function mle(){return k((e,t)=>{'
    'const{isSignedInPlatformNetwork:n,isLoggedInWithMicrosoftAccount:a,'
    'hasPremiumNetworkAccess:r}=e,l=oW(t.platform),o=rW(t.platform);'
    'return a&&!n&&l?"platform-guest-nintendo":!a&&n&&o&&!r?'
    '"msa-guest-playstation":"not-signed-in"},[],[Fo(WD),Fo(ci)])}'
)
_LINK_DIRECT = (
    'function ple({location:e}){const{t:n}=zu("notLoggedInWarning"),a=_(),'
    'r=pB(),l=(0,t.useCallback)(()=>{r({screen:`${fle(e)}`,button:"SignIn"}),'
    'a.push(`/sign-in?signInSource=${fle(e)}_NotLoggedInWarning_OreUI`)},'
    '[r,a,e]);return t.createElement(SX,{role:"noticeTint"},'
    't.createElement(SX.Link,{onClick:l},n(".loginLink")))}'
)
# A zero-argument facet hook that reads none of the account state, sitting
# right before the real one: the marks leak into an unbounded look-ahead.
_DECOY = ('function p3(){return(0,r.useFacetMap)((e=>e.thirdPartyServers),'
          '[],[(0,r.useSharedFacet)(sC)])}')


def _game_dir(root, bundle, name="index-0de4683795677b1eaae4.js"):
    hbui = Path(root) / "data" / "gui" / "dist" / "hbui"
    hbui.mkdir(parents=True)
    (hbui / name).write_text(bundle)
    return Path(root)


class HbuiPatchTests(unittest.TestCase):
    def _run(self, bundle):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        gd = _game_dir(tmp.name, bundle)
        with mock.patch.object(fixups, "ok"), mock.patch.object(fixups, "warn"):
            applied = fixups._patch_hbui_signin_gate(gd)
        js = next(gd.glob("data/gui/dist/hbui/index-*.js"))
        return applied, gd, js, js.read_text()

    def test_patches_the_bundle_shape_that_broke_them(self):
        applied, _gd, js, out = self._run(_GATE_NEW + _LINK_NEW)
        self.assertTrue(applied)
        self.assertIn('function T3(){return"";return(0,r.useFacetMap)', out)
        self.assertIn('[c,a,e]);return null;return n.createElement(nD,', out)
        self.assertTrue(Path(str(js) + ".bol-orig").exists())

    def test_still_patches_the_shape_the_needles_were_written_for(self):
        applied, _gd, _js, out = self._run(_GATE_OLD + _LINK_OLD)
        self.assertTrue(applied)
        self.assertIn('function wB(){return"";return(0,l.useFacetMap)', out)
        self.assertIn('[a,e]);return null;return r.createElement(sx,', out)

    def test_patches_the_directly_called_hooks_of_1_26_60(self):
        applied, gd, _js, out = self._run(_GATE_DIRECT + _LINK_DIRECT)
        self.assertTrue(applied)
        self.assertIn('function mle(){return"";return k(', out)
        self.assertIn('[r,a,e]);return null;return t.createElement(SX,', out)
        with mock.patch.object(fixups, "ok"), mock.patch.object(fixups, "warn"):
            self.assertTrue(fixups._patch_hbui_signin_gate(gd))
        self.assertEqual(
            fixups.hbui_signin_gate_status(str(gd))[0],
            "OK (gate + link patched)")

    def test_is_idempotent(self):
        _applied, gd, js, once = self._run(_GATE_NEW + _LINK_NEW)
        with mock.patch.object(fixups, "ok"), mock.patch.object(fixups, "warn"):
            self.assertTrue(fixups._patch_hbui_signin_gate(gd))
        self.assertEqual(js.read_text(), once)
        self.assertEqual(once.count('return"";return(0,'), 1)
        self.assertEqual(once.count("return null;return "), 1)

    def test_leaves_a_neighbouring_facet_hook_alone(self):
        _applied, _gd, _js, out = self._run(_DECOY + _GATE_NEW + _LINK_NEW)
        self.assertIn('function p3(){return(0,r.useFacetMap)', out)
        self.assertIn('function T3(){return"";', out)
        self.assertEqual(out.count('return"";return(0,'), 1)

    def test_a_rebundled_ui_is_reported_not_skipped(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        gd = _game_dir(tmp.name, 'function q1(){return 42}')
        with mock.patch.object(fixups, "ok") as said_ok, \
                mock.patch.object(fixups, "warn") as warned:
            self.assertFalse(fixups._patch_hbui_signin_gate(gd))
        said_ok.assert_not_called()
        warned.assert_called_once()
        message = warned.call_args[0][0]
        self.assertIn("Microsoft-account gate", message)
        self.assertIn("Sign-in link", message)

    def test_a_missing_bundle_is_reported(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / "data").mkdir()
        with mock.patch.object(fixups, "warn") as warned:
            self.assertFalse(fixups._patch_hbui_signin_gate(Path(tmp.name)))
        warned.assert_called_once()

    def test_one_dead_anchor_still_writes_the_other_and_warns(self):
        applied, _gd, _js, out = self._run(_GATE_NEW)
        self.assertFalse(applied)
        self.assertIn('function T3(){return"";', out)


class HbuiStatusTests(unittest.TestCase):
    def _status(self, bundle):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        gd = _game_dir(tmp.name, bundle)
        return gd, fixups.hbui_signin_gate_status(str(gd))

    def test_reports_an_unpatched_build_as_pending(self):
        _gd, (summary, problem) = self._status(_GATE_NEW + _LINK_NEW)
        self.assertEqual(summary, "not applied yet")
        self.assertIn("Install / Update", problem)

    def test_reports_a_patched_build_as_healthy(self):
        gd, _ = self._status(_GATE_NEW + _LINK_NEW)
        with mock.patch.object(fixups, "ok"), mock.patch.object(fixups, "warn"):
            fixups._patch_hbui_signin_gate(gd)
        summary, problem = fixups.hbui_signin_gate_status(str(gd))
        self.assertIsNone(problem)
        self.assertIn("patched", summary)

    def test_reports_a_rebundled_ui_as_a_problem(self):
        _gd, (summary, problem) = self._status('function q1(){return 42}')
        self.assertIn("NOT PATCHED", summary)
        self.assertIn("rebundled", problem)

    def test_says_nothing_without_a_game(self):
        self.assertEqual(fixups.hbui_signin_gate_status(""),
                         ("no game installed", None))


if __name__ == "__main__":
    unittest.main()
