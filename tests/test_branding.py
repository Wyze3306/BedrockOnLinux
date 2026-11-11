"""The launcher's own touches on the game's menus (bol/branding.py).

The partner-server patch rewrites the minified HBUI bundle, so these tests
hold it to the two bundler shapes shipped so far -- `(0,r.useFacetMap)((e=>…))`
up to 1.26.52 and a bare `k(e=>…)` from 1.26.60 on -- with every local name
different between them, and to its all-or-nothing rule: a bundle it can only
half patch is left exactly as it was.
"""
# SPDX-License-Identifier: MIT

import json
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from bol import branding

# 1.26.60+ shape: hooks called directly, lone arrows without parentheses.
_DIRECT = (
    'function Fle({loading:e,routePrefix:n="/play",showFeaturedServers:a=!0,'
    'showCreatorServers:r=!0}){const i=Fo(bD),c=Fo(vM),'
    'y=k(e=>Array.from(e.featuredExperiences).map(e=>({message:e.msgOfTheDay,'
    'image:e.image,label:e.name,type:kD.ThirdParty,value:e.id})),[],[i]),'
    'C=k(e=>l(".featuredServer",[e.featuredExperiences.length.toString()]),'
    '[l],[i]),w=k(e=>l(".creatorServer",[e.creatorExperiences.length.'
    'toString()]),[l],[i]),P=k(e=>e.featuredExperiences.length>0&&a,[a],[i]),'
    'I=k(e=>e.externalServerWorlds.map(e=>({label:e.name,value:e.id,'
    'type:kD.External,message:e.msgOfTheDay})),[],[c]),'
    'x=k(e=>l(".externalServer",[e.externalServerWorlds.length.toString()]),'
    '[l],[c]),R=k(e=>e.externalServerWorlds.length>0,[],[c]);return y}'
    'const Dle="show-server-address-button";'
    'function Mle({item:e,onSelectServer:n}){const g=()=>{'
    'n(e.value,e.type,e.label)};return g}'
    'function xoe(){const l=Fo(VM),'
    'o=k(e=>e.networkDetails.type===kD.External,[],[l]);return o}'
    'function Poe(){const r=Fo(VM),'
    'i=k(e=>e.networkDetails.imagePath,[],[r]),'
    'c=k(e=>e.networkDetails.description,[],[r]);return[i,c]}'
    'function Gle(){const n=Fo(VM),'
    'a=k(e=>e.networkDetails.newsTitle,[],[n]),'
    'r=k(e=>e.networkDetails.newsDescription,[],[n]);return[a,r]}'
    'function woe(){const a=Fo(VM),'
    'r=k(e=>e.networkDetails.activities.length>0,[],[a]);return r}'
    'function koe(){const a=k(e=>e.networkDetails.activities,[],[Fo(VM)]);'
    'return a}'
)

# 1.26.52 shape: `(0,r.useFacetMap)((e=>…))`, other minified names.
_WRAPPED = (
    'function Dae({loading:e,routePrefix:t="/play",showFeaturedServers:a=!0,'
    'showCreatorServers:l=!0}){const i=(0,r.useSharedFacet)(sI),'
    's=(0,r.useSharedFacet)(uP),'
    'S=(0,r.useFacetMap)((e=>Array.from(e.featuredExperiences).map((e=>({'
    'message:e.msgOfTheDay,image:e.image,label:e.name,type:fI.ThirdParty,'
    'value:e.id})))),[],[i]),'
    'M=(0,r.useFacetMap)((e=>o(".featuredServer",[e.featuredExperiences.'
    'length.toString()])),[o],[i]),'
    'k=(0,r.useFacetMap)((e=>e.featuredExperiences.length>0&&a),[a],[i]),'
    'T=(0,r.useFacetMap)((e=>e.externalServerWorlds.map((e=>({label:e.name,'
    'value:e.id,type:fI.External,message:e.msgOfTheDay})))),[],[s]),'
    'I=(0,r.useFacetMap)((e=>o(".externalServer",[e.externalServerWorlds.'
    'length.toString()])),[o],[s]),'
    'O=(0,r.useFacetMap)((e=>e.externalServerWorlds.length>0),[],[s]);'
    'return S}function Oae({item:e,onSelectServer:t}){const g=(()=>{'
    't(e.value,e.type,e.label)});return g}'
    'function Sae(){const l=(0,r.useSharedFacet)(MP),'
    'o=(0,r.useFacetMap)((e=>e.networkDetails.type===fI.External),[],[l]);'
    'return o}function Cae(){const l=(0,r.useSharedFacet)(MP),'
    'i=(0,r.useFacetMap)((e=>e.networkDetails.imagePath),[],[l]),'
    's=(0,r.useFacetMap)((e=>e.networkDetails.description),[],[l]);'
    'return[i,s]}function Gae(){const t=(0,r.useSharedFacet)(MP),'
    'a=(0,r.useFacetMap)((e=>e.networkDetails.newsTitle),[],[t]),'
    'l=(0,r.useFacetMap)((e=>e.networkDetails.newsDescription),[],[t]);'
    'return[a,l]}function Hae(){const a=(0,r.useSharedFacet)(MP),'
    'r=(0,r.useFacetMap)((e=>e.networkDetails.activities.length>0),[],[a]);'
    'return r}function Kae(){const a=(0,r.useFacetMap)((e=>e.networkDetails.'
    'activities),[],[(0,r.useSharedFacet)(MP)]);return a}'
)


class PartnerPatchShapes(unittest.TestCase):

    def _check(self, bundle, featured_facet, external_facet):
        out, status = branding._partner_patch(bundle)
        self.assertEqual(status, "applied")
        self.assertTrue(out.startswith("var __bolPartnerList="))
        # Every featured selector also reads the external-server facet.
        self.assertEqual(out.count(
            "],[%s,%s])" % (featured_facet, external_facet)), 3)
        self.assertEqual(out.count("__bolCount(__bolX)"), 2)
        self.assertEqual(out.count("__bolRest(e.externalServerWorlds)"), 3)
        self.assertIn("null!=e.bolType?e.bolType:e.type", out)
        self.assertIn("&&!__bolPartnerOf(e.networkDetails))", out)
        for field in ("imagePath", "description", "newsTitle",
                      "newsDescription", "activities"):
            self.assertNotIn("e=>e.networkDetails.%s" % field, out)
        # Patching again only refreshes the helpers.
        again, status = branding._partner_patch(out)
        self.assertEqual((again, status), (out, "already"))
        return out

    def test_direct_hook_bundle(self):
        self._check(_DIRECT, "i", "c")

    def test_wrapped_use_facet_map_bundle(self):
        self._check(_WRAPPED, "i", "s")

    def test_a_bundle_without_the_servers_tab_is_left_alone(self):
        self.assertEqual(branding._partner_patch("function x(){}"),
                         ("function x(){}", "missing"))

    def test_a_half_matching_bundle_is_not_touched(self):
        broken = _DIRECT.replace("n(e.value,e.type,e.label)",
                                 "n(e.value,e.kind,e.label)")
        self.assertEqual(branding._partner_patch(broken), (broken, "lost"))

    def test_a_bundle_patched_before_activities_gains_them(self):
        out, _ = branding._partner_patch(_DIRECT)
        older = out.replace(branding._ACTIVITIES_SHOWN_PATCHED,
                            branding._ACTIVITIES_SHOWN).replace(
            branding._DETAILS_FIELDS["activities"],
            "e.networkDetails.activities")
        self.assertNotEqual(older, out)
        self.assertEqual(branding._partner_patch(older), (out, "already"))

    def test_every_partner_image_is_bundled(self):
        for p in branding.PARTNERS:
            for name in branding._partner_images(p):
                with self.subTest(image=name):
                    self.assertTrue((Path(branding.__file__).parent
                                     / "partners" / name).is_file())

    def test_partner_table_updates_on_an_already_patched_bundle(self):
        out, _ = branding._partner_patch(_DIRECT)
        changed = [dict(branding.PARTNERS[0], message="Changed")]
        again, status = branding._partner_patch(out, changed)
        self.assertEqual(status, "already")
        self.assertIn('"message": "Changed"', again.split("\n", 1)[0])
        self.assertEqual(again.split("\n", 1)[1], out.split("\n", 1)[1])


@unittest.skipUnless(shutil.which("node"), "needs node")
class PartnerRuntime(unittest.TestCase):
    """The helpers the bundle calls, run as the game's V8 would run them."""

    def _eval(self, expr):
        script = (branding._partner_runtime(branding.PARTNERS)
                  + "var kd={ThirdParty:0,External:1};"
                  + "var ext={externalServerWorlds:["
                  + "{id:'1',name:'zeqa'},{id:'2',name:'Linesia SkyFaction'},"
                  + "{id:'3',name:'linesia again'}]};"
                  + "process.stdout.write(JSON.stringify(" + expr + "));")
        out = subprocess.run(["node", "-e", script], capture_output=True,
                             text=True, check=True).stdout
        return json.loads(out)

    def test_the_first_linesia_entry_becomes_a_featured_tile(self):
        tiles = self._eval("__bolFeatured(ext,kd)")
        self.assertEqual(len(tiles), 1)
        self.assertEqual(tiles[0]["value"], "2")
        self.assertEqual(tiles[0]["type"], 0)
        self.assertEqual(tiles[0]["bolType"], 1)
        self.assertEqual(tiles[0]["label"], "Linesia")
        self.assertEqual(tiles[0]["message"], "SKYFACTION")
        self.assertTrue(tiles[0]["image"].startswith("assets/bol-partner-"))

    def test_the_promoted_entry_leaves_the_players_own_list(self):
        rest = self._eval("__bolRest(ext.externalServerWorlds)")
        self.assertEqual([s["id"] for s in rest], ["1", "3"])

    def test_the_partner_page_has_activity_cards(self):
        cards = self._eval(
            "__bolDetail({address:'play.linesia.net',activities:[]},"
            "'activities','activities')")
        self.assertEqual(len(cards), 4)
        for card in cards:
            self.assertEqual(set(card), {"title", "subtitle", "description",
                                         "imagePath"})
            self.assertTrue(card["imagePath"].startswith("assets/bol-partner-"))

    def test_details_are_recognised_by_address(self):
        self.assertTrue(self._eval(
            "__bolDetail({address:'PLAY.linesia.net',name:'x',"
            "imagePath:''},'banner','imagePath')").endswith(".jpg"))
        self.assertEqual(self._eval(
            "__bolDetail({address:'other.net',name:'Linesia',"
            "imagePath:'keep'},'banner','imagePath')"), "keep")


class PartnerInstall(unittest.TestCase):

    def test_bundle_and_images_are_installed_with_a_backup(self):
        with TemporaryDirectory() as tmp:
            hbui = Path(tmp) / branding.HBUI_REL
            (hbui / "assets").mkdir(parents=True)
            js = hbui / "index-abc.js"
            js.write_text(_DIRECT, encoding="utf-8")
            self.assertTrue(branding.feature_partner_servers(tmp))
            self.assertIn("__bolPartnerList", js.read_text(encoding="utf-8"))
            self.assertEqual(
                (hbui / "index-abc.js.bol-orig").read_text(encoding="utf-8"),
                _DIRECT)
            for p in branding.PARTNERS:
                for name in branding._partner_images(p):
                    self.assertGreater((hbui / "assets" / (
                        "bol-partner-" + name)).stat().st_size, 1000)
            # Idempotent.
            before = js.read_text(encoding="utf-8")
            self.assertTrue(branding.feature_partner_servers(tmp))
            self.assertEqual(js.read_text(encoding="utf-8"), before)


class Splashes(unittest.TestCase):

    def _game(self, tmp, data):
        path = Path(tmp) / branding.SPLASHES_REL
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_vanilla_splashes_are_replaced_and_kept_aside(self):
        vanilla = {"splashes": ["Made by Mojang!"],
                   "conditional": [{"requires": {"trialMode": True},
                                    "splashes": ["Try for free"]}]}
        with TemporaryDirectory() as tmp:
            path = self._game(tmp, vanilla)
            self.assertTrue(branding.apply_linux_splashes(tmp))
            now = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(now["splashes"], list(branding.LINUX_SPLASHES))
            self.assertEqual(now["conditional"], vanilla["conditional"])
            kept = path.with_name("splashes.json.bol-orig")
            self.assertEqual(json.loads(kept.read_text()), vanilla)
            # A second run leaves the backup holding the vanilla list.
            self.assertTrue(branding.apply_linux_splashes(tmp))
            self.assertEqual(json.loads(kept.read_text()), vanilla)

    def test_a_missing_or_unreadable_file_is_left_alone(self):
        with TemporaryDirectory() as tmp:
            self.assertFalse(branding.apply_linux_splashes(tmp))
            path = self._game(tmp, ["not", "a", "dict"])
            self.assertFalse(branding.apply_linux_splashes(tmp))
            self.assertEqual(json.loads(path.read_text()), ["not", "a", "dict"])

    def test_every_splash_is_short(self):
        for line in branding.LINUX_SPLASHES:
            with self.subTest(line=line):
                self.assertLessEqual(len(line), 32)


if __name__ == "__main__":
    unittest.main()
