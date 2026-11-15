"""BedrockOnLinux's own Servers tab (bol/serverbrowser.py).

The bundle tests run on a synthetic stand-in for the game's Servers tab, in
the two bundler shapes shipped so far (bare hooks from 1.26.60 on,
``(0,r.useFacetMap)`` before), with the same anchors the real bundles carry:
the patch must find every name, add the components once, and leave a bundle
it cannot fully understand exactly as it was. Where Node is installed, the
patched text is parsed and the tab's search logic run for real.
"""
# SPDX-License-Identifier: MIT

import json
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from bol import branding, serverbrowser as sb

NODE = shutil.which("node")

_DIRECT = (
    'const vM=oi("vanilla.externalServerWorldList"),'
    'bD=oi("vanilla.thirdPartyWorldList"),VM=oi("vanilla.networkWorldDetails");'
    'function Ale({label:e,value:n,image:a,subtitle:r}){const c=Q(null!=r?r:""),'
    's=Q(null!=a?a:"");return t.createElement(XJ.ListItem,{value:n},c,s)}'
    'function Mle({item:e,onSelectServer:n}){const a=Fo(VM),'
    'o=k(e=>e.label,[],[e]),i=k(e=>e.value,[],[e]),'
    'c=k(e=>null!=e.message?e.message:"",[],[e]),'
    's=k(e=>null!=e.image?e.image:"",[],[e]),'
    'd=k((e,t)=>e===t.networkDetails.id,[],[i,a]),'
    'g=()=>{n(e.value,e.type,e.label)};'
    'return t.createElement(Ale,{label:o,value:i,image:s,subtitle:c,'
    'selected:d,onSelect:g})}'
    'function Fle({loading:e,routePrefix:n="/play",showFeaturedServers:a=!0,'
    'showCreatorServers:r=!0}){const{t:l}=zu("PlayScreen.serverTab"),'
    '{t:o}=zu("PlayScreen.serverTab.narration"),i=Fo(bD),c=Fo(vM),'
    's=l(".addServer"),d=_(),g=()=>{d.push(`${n}/servers/add`)},'
    'E=(e,t)=>{d.replace(`${n}/servers/${e}`)},'
    'b=({item:e})=>t.createElement(Mle,{item:e,onSelectServer:E}),'
    'y=k(e=>Array.from(e.featuredExperiences).map(e=>({message:e.msgOfTheDay,'
    'image:e.image,label:e.name,type:kD.ThirdParty,value:e.id})),[],[i]),'
    'C=k(e=>l(".featuredServer",[e.featuredExperiences.length.toString()]),'
    '[l],[i]),P=k(e=>e.featuredExperiences.length>0&&a,[a],[i]),'
    'I=k(e=>e.externalServerWorlds.map(e=>({label:e.name,value:e.id,'
    'type:kD.External,message:e.msgOfTheDay})),[],[c]),'
    'R=k(e=>e.externalServerWorlds.length>0,[],[c]);'
    'return t.createElement(Pl,{role:m},t.createElement(of,{text:o(".context")},'
    't.createElement(XJ,{spacingAroundSideMenu:B,loading:e},'
    't.createElement(re,{when:D},t.createElement(XJ.Item,null,'
    't.createElement(Cg,{onClick:g,Icon:HL.Plus,inputLegend:s},s))),'
    't.createElement(XJ.ItemList,{items:y,renderItem:b}),'
    't.createElement(XJ.ItemList,{items:I,renderItem:b}),'
    't.createElement(Ud,{size:4}))))}'
    'const Dde=()=>{const[x,R]=nt(""),W=e=>R(e);'
    'return t.createElement(wz,{label:P(".serverNameLabel"),value:x,'
    'onChange:W})};'
    'function Poe(){const r=Fo(VM),i=k(e=>e.networkDetails.imagePath,[],[r]),'
    'c=k(e=>e.networkDetails.description,[],[r]);'
    'return t.createElement("div",null,t.createElement(Bm,{aspectRatio:'
    'Bm.AspectRatio["10:3"],src:i}),t.createElement(Zf,{focusable:!0,'
    'role:"neutral80",narrationText:c},t.createElement(ng,null,'
    'a(".ServerDescription.title")),t.createElement(Ud,{size:2}),'
    't.createElement(ag,null,c)))}'
    'function Gle(){const n=Fo(VM),a=k(e=>e.networkDetails.newsTitle,[],[n]),'
    'r=k(e=>e.networkDetails.newsDescription,[],[n]);return[a,r]}'
    'function woe(){const a=Fo(VM),'
    'r=k(e=>e.networkDetails.activities.length>0,[],[a]);return r}'
    'function koe(){const a=k(e=>e.networkDetails.activities,[],[Fo(VM)]);'
    'return a}'
    'function xoe({loading:e,scrollViewRef:n,routePrefix:a="/play"}){'
    'const l=Fo(VM),o=k(e=>e.networkDetails.type===kD.External,[],[l]);'
    'return t.createElement(qp,{loading:e,innerRef:n,floating:!1},o)}'
    'function Ooe({routePrefix:e="/play",showFeaturedServers:n=!0,'
    'showCreatorServers:a=!0}){const r=Fo(VM),o=Fo(vM),i=Fo(bD),c=(0,t.useRef)(null),'
    'd=k(e=>e.fetchThirdPartyWorldsTaskState===lU.DONE,[],[i]),g=j(),'
    'E=k(e=>null==e?void 0:e.id,[],[g]),'
    'b=k(e=>(null==e?void 0:e.type)===jU(kD.External)?kD.External:'
    'kD.ThirdParty,[],[g]),h=J(e=>(t,n)=>{e.loadNetworkWorldDetails(t,n)},'
    '[],[r]);return t.createElement(Lm,{containerRef:c,delegateFocusByAlias:'
    'Ole.SERVER_SIDE_MENU,delegateFocusFromMemory:!0},t.createElement(AR.Row,'
    '{noWrap:!0},t.createElement(Fle,{loading:d,routePrefix:e,'
    'showFeaturedServers:n,showCreatorServers:a}),t.createElement(xoe,'
    '{loading:d,scrollViewRef:c,routePrefix:e})))}'
    'function Noe(){const a=ele();return t.createElement(Po,{data:a},'
    'a=>{switch(a){case"banned":return null}})}'
)


def _wrapped(text):
    """The same tab as the 1.26.52 bundler wrote it."""
    for bare, wrapped in (("k(", "(0,r.useFacetMap)("),
                          ("Fo(", "(0,r.useSharedFacet)("),
                          ("J(", "(0,r.useFacetCallback)("),
                          ("nt(", "(0,r.useFacetState)("),
                          ("Q(", "(0,r.useFacetWrap)("),
                          ("oi(", "(0,r.sharedFacet)("),
                          ("t.createElement(", "n.createElement("),
                          ("(0,t.useRef)", "(0,n.useRef)"),
                          ("(Po,", "(r.Unwrap,")):
        if not bare[0].isalnum():
            text = text.replace(bare, wrapped)
            continue
        text = re.sub(r"(?<![\w$.])" + re.escape(bare),
                      lambda _m, w=wrapped: w, text)
    return text


def _entries():
    common = ["Survival", "PvP", "Economy"]
    out = []
    for i in range(30):
        out.append({"id": f"srv-{i}", "name": f"Server {i}",
                    "ip": f"play{i}.example.org", "port": 19132,
                    "lang": "fr" if i % 3 == 0 else "en",
                    "description": f"Number {i}", "tags": common + (
                        ["Skyblock"] if i % 2 else ["Factions"])})
    out.append({"id": "linesia", "name": "Linesia", "ip": "play.linesia.net",
                "port": 19132, "lang": "fr", "description": "x" * 5000,
                "tags": common + ["Factions"], "country": "France",
                "website": "https://linesia.net", "discord": "javascript:x"})
    out.append({"name": "", "ip": "nameless.example.org"})
    out.append("not a server")
    return out


class Payload(unittest.TestCase):
    def setUp(self):
        self.entries = sb._valid_entries(_entries())

    def test_tags_every_server_carries_are_not_categories(self):
        p = sb.build_payload(self.entries)
        cats = dict(p["cats"])
        self.assertEqual(set(cats), {"Skyblock", "Factions"})
        for s in p["servers"]:
            self.assertNotIn("Survival", s["t"])

    def test_invalid_entries_are_dropped_and_ids_are_prefixed(self):
        p = sb.build_payload(self.entries)
        self.assertEqual(len(p["servers"]), 31)
        self.assertTrue(all(s["i"].startswith("bol-") for s in p["servers"]))

    def test_partner_flag_links_and_description_length(self):
        p = sb.build_payload(self.entries, partner_hosts=["play.linesia.net"])
        lin = next(s for s in p["servers"] if s["n"] == "Linesia")
        self.assertEqual(lin["pt"], 1)
        self.assertEqual(lin["w"], "https://linesia.net")
        self.assertNotIn("dc", lin)  # not a web address
        self.assertLessEqual(len(lin["d"]), sb._DESCRIPTION_CHARS + 1)

    def test_language_names_and_strings_follow_the_game(self):
        p = sb.build_payload(self.entries, ui=sb.ui_language("fr_FR"))
        self.assertEqual(p["strings"]["play"], "Jouer")
        self.assertIn(["fr", 11, "Français"], p["langs"])
        p = sb.build_payload(self.entries, ui=sb.ui_language("ja_JP"))
        self.assertEqual(p["strings"]["play"], "Play")
        self.assertEqual(set(sb.STRINGS["fr"]), set(sb.STRINGS["en"]))

    def test_runtime_is_one_line(self):
        p = sb.build_payload(self.entries)
        p["servers"][0]["d"] = "a\nb c"
        line = sb.runtime_line(p)
        self.assertNotIn("\n", line)
        self.assertNotIn(" ", line)
        self.assertTrue(line.startswith("var __bolServerBrowser={"))

    def test_off_still_defines_the_global(self):
        p = sb.build_payload([], on=False)
        self.assertFalse(p["on"])
        self.assertEqual(p["servers"], [])


class ListCache(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.calls = 0

    def _getter(self, blob=None, fail=False):
        def get(url, limit, timeout):
            self.calls += 1
            self.assertEqual(url, sb.LIST_URL)
            if fail:
                raise OSError("offline")
            return blob
        return get

    def test_first_download_is_cached(self):
        blob = json.dumps(_entries()).encode()
        got = sb.fetch_list(self.dir, getter=self._getter(blob))
        self.assertEqual(len(got), 31)
        self.assertTrue((self.dir / "servers.json").is_file())

    def test_a_fresh_cache_does_not_connect(self):
        (self.dir / "servers.json").write_text(json.dumps(_entries()))
        got = sb.fetch_list(self.dir, getter=self._getter(fail=True))
        self.assertEqual((len(got), self.calls), (31, 0))

    def test_a_stale_cache_survives_a_failed_refresh(self):
        path = self.dir / "servers.json"
        path.write_text(json.dumps(_entries()))
        old = time.time() - 2 * sb._LIST_TTL
        import os
        os.utime(path, (old, old))
        got = sb.fetch_list(self.dir, getter=self._getter(fail=True))
        self.assertEqual((len(got), self.calls), (31, 1))
        # ... and is not retried at every launch.
        sb.fetch_list(self.dir, getter=self._getter(fail=True))
        self.assertEqual(self.calls, 1)

    def test_nothing_at_all_is_an_empty_list(self):
        self.assertEqual(sb.fetch_list(self.dir, getter=self._getter(
            fail=True)), [])
        self.assertEqual(sb.fetch_list(self.dir, getter=self._getter(
            b"[]")), [])


class Images(unittest.TestCase):
    PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
           + (468).to_bytes(4, "big") + (60).to_bytes(4, "big") + b"\0" * 20)
    GIF = b"GIF89a" + (400).to_bytes(2, "little") + (100).to_bytes(
        2, "little") + b"\0" * 20

    def test_dimensions(self):
        self.assertEqual(sb._image_dims(self.PNG), (468, 60))
        self.assertEqual(sb._image_dims(self.GIF), (400, 100))
        self.assertIsNone(sb._image_dims(b"<html>"))

    def test_only_https_from_known_hosts(self):
        self.assertTrue(sb._image_allowed(
            "https://minecraftpocket-servers.com/server/1/banner.gif"))
        self.assertFalse(sb._image_allowed(
            "http://minecraftpocket-servers.com/server/1/banner.gif"))
        self.assertFalse(sb._image_allowed("https://evil.example/b.png"))

    def test_download_then_install(self):
        cache = Path(tempfile.mkdtemp())
        hbui = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, cache)
        self.addCleanup(shutil.rmtree, hbui)
        (hbui / "assets").mkdir()
        entries = [{"id": "a", "name": "A", "ip": "a.example.org",
                    "image": "https://minecraftpocket-servers.com/a.png"},
                   {"id": "b", "name": "B", "ip": "b.example.org",
                    "image": "https://minecraftpocket-servers.com/b.gif"},
                   {"id": "c", "name": "C", "ip": "c.example.org",
                    "image": "https://minecraftpocket-servers.com/c.png"}]
        blobs = {"a.png": self.PNG, "b.gif": self.GIF, "c.png": b"<html>"}

        def get(url, limit, timeout):
            return blobs[url.rsplit("/", 1)[1]]
        self.assertEqual(sb.download_images(entries, cache, getter=get), 2)
        images = sb.install_images(hbui, entries, cache)
        self.assertEqual(images["a"], ("assets/bol-srv-a.png", 12.82))
        self.assertEqual(images["b"], ("assets/bol-srv-b.gif", 25.0))
        self.assertNotIn("c", images)
        self.assertTrue((hbui / "assets" / "bol-srv-a.png").is_file())


class BundlePatch(unittest.TestCase):
    def _patched(self, bundle):
        out, status = sb.patch_body(bundle)
        self.assertEqual(status, "applied")
        return out

    def test_direct_shape_names(self):
        names = sb._discover(_DIRECT)
        expect = {"R": "t", "FOCUS": "Lm", "ALIAS": "Ole", "SIDE": "Fle",
                  "DETAILS": "xoe", "UFM": "k", "UFC": "J", "ROUTE": "j",
                  "JU": "jU", "KD": "kD", "EXT": "vM", "TP": "bD",
                  "USF": "Fo", "UNWRAP": "Po", "XJ": "XJ", "BUTTON": "Cg",
                  "ICONS": "HL", "ITEM": "Mle", "SPACER": "Ud",
                  "THEME": "Pl", "NARRATION": "of", "ROUTER": "_",
                  "PANEL": "Zf", "HEADING": "ng", "BODY": "ag",
                  "IMAGE": "Bm", "SCROLL": "qp", "TEXTFIELD": "wz",
                  "UFS": "nt", "WRAP": "Q"}
        self.assertEqual({k: names[k] for k in expect}, expect)

    def test_wrapped_shape_names(self):
        names = sb._discover(_wrapped(_DIRECT))
        self.assertEqual(names["UFM"], "(0,r.useFacetMap)")
        self.assertEqual(names["USF"], "(0,r.useSharedFacet)")
        self.assertEqual(names["UNWRAP"], "r.Unwrap")
        self.assertEqual(names["R"], "n")
        self.assertEqual(names["WRAP"], "(0,r.useFacetWrap)")

    def test_the_tab_switches_on_the_global(self):
        for bundle in (_DIRECT, _wrapped(_DIRECT)):
            out = self._patched(bundle)
            self.assertIn(".createElement(bol_on()?bol_Side:Fle,{", out)
            self.assertIn(".createElement(bol_on()?bol_Details:xoe,{", out)
            self.assertIn("e=>null==e||bol_isCatRoute(e.id)?void 0:e.id",
                          out)
            self.assertIn("(e,t,bol_r)=>bol_selected(e,t,bol_r)", out)
            self.assertEqual(out.count("function bol_SideView("), 1)
            # Pasted next to the tab, in its scope; no placeholder left.
            self.assertLess(out.index("function bol_Side("),
                            out.index("function Ooe("))
            self.assertNotRegex(out, r"__[A-Z]+__")
            self.assertNotIn("// ", out[out.index("var bol_state"):
                                        out.index("function Ooe(")])

    def test_already_and_missing(self):
        out = self._patched(_DIRECT)
        self.assertEqual(sb.patch_body(out), (out, "already"))
        self.assertEqual(sb.patch_body("function x(){}"),
                         ("function x(){}", "missing"))

    def test_a_half_matching_tab_is_not_touched(self):
        for broken in (_DIRECT.replace("Icon:HL.Plus", "Icon:HL.Add"),
                       _DIRECT.replace('"vanilla.thirdPartyWorldList"',
                                       '"vanilla.thirdPartyWorlds"'),
                       _DIRECT.replace("e===t.networkDetails.id",
                                       "t.networkDetails.id===e"),
                       _DIRECT.replace(".serverNameLabel", ".nameLabel")):
            self.assertEqual(sb.patch_body(broken), (broken, "lost"))

    @unittest.skipUnless(NODE, "node is not installed")
    def test_the_patched_bundle_parses(self):
        for bundle in (_DIRECT, _wrapped(_DIRECT)):
            out = self._patched(bundle)
            r = subprocess.run([NODE, "--check", "-"], input=out,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)


class Branding(unittest.TestCase):
    """The browser rides on the partner patch's first line."""

    def _game(self, bundle):
        game = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, game)
        hbui = game / branding.HBUI_REL
        (hbui / "assets").mkdir(parents=True)
        js = hbui / "index-0123.js"
        js.write_text(bundle, encoding="utf-8")
        return game, js

    def test_switching_off_rewrites_the_first_line_only(self):
        game, js = self._game(_DIRECT)
        on = sb.build_payload(sb._valid_entries(_entries()))
        self.assertTrue(branding.feature_partner_servers(game, browser=on))
        first = js.read_text(encoding="utf-8")
        head, _, body = first.partition("\n")
        self.assertTrue(head.startswith("var __bolPartnerList="))
        self.assertIn('var __bolServerBrowser={"on":true', head)
        self.assertIn("function bol_SideView(", body)
        self.assertTrue((js.parent / (js.name + ".bol-orig")).is_file())

        off = sb.build_payload([], on=False)
        self.assertTrue(branding.feature_partner_servers(game, browser=off))
        second = js.read_text(encoding="utf-8")
        head2, _, body2 = second.partition("\n")
        self.assertEqual(body2, body)
        self.assertIn('var __bolServerBrowser={"on":false', head2)
        self.assertEqual(head2.count("__bolServerBrowser="), 1)

    def test_no_payload_keeps_the_partner_patch_alone(self):
        game, js = self._game(_DIRECT)
        self.assertTrue(branding.feature_partner_servers(game))
        self.assertNotIn("bol_SideView", js.read_text(encoding="utf-8"))

    def test_a_tab_the_browser_cannot_read_keeps_the_partners(self):
        game, js = self._game(_DIRECT.replace(".serverNameLabel", ".x"))
        on = sb.build_payload(sb._valid_entries(_entries()))
        self.assertTrue(branding.feature_partner_servers(game, browser=on))
        text = js.read_text(encoding="utf-8")
        self.assertIn("__bolPartnerOf", text)
        self.assertNotIn("__bolServerBrowser", text)


@unittest.skipUnless(NODE, "node is not installed")
class TabLogic(unittest.TestCase):
    """The tab's search and lists, run in Node against the real template."""

    def _run(self, payload, expr):
        names = {k: "S_" + k for k in sb._PLACEHOLDERS}
        names["KD"] = "S_KD"
        code = sb._render(sb._template(), names)
        script = ("var S_KD={External:1,ThirdParty:2};"
                  + sb.runtime_line(payload) + code
                  + ";process.stdout.write(JSON.stringify(" + expr + "))")
        r = subprocess.run([NODE, "-"], input=script, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def setUp(self):
        entries = sb._valid_entries(_entries())
        entries[4]["description"] = "Un serveur Créatif très calme"
        self.payload = sb.build_payload(entries,
                                        partner_hosts=["play.linesia.net"])

    def test_partners_come_first_then_the_list_order(self):
        got = self._run(self.payload,
                        'bol_catalogue({query:"",cat:"",lang:""},[])')
        self.assertEqual(len(got), 31)
        self.assertEqual(got[0]["label"], "Linesia")
        self.assertEqual(got[0]["message"], "Factions · FR")

    def test_search_by_address_category_and_folded_keyword(self):
        q = 'bol_catalogue({query:%s,cat:"",lang:""},[])'
        got = self._run(self.payload, q % '"play7.example"')
        self.assertEqual([g["label"] for g in got], ["Server 7"])
        got = self._run(self.payload, q % '"creatif"')
        self.assertEqual([g["label"] for g in got], ["Server 4"])
        got = self._run(self.payload, q % '"skyblock 1"')
        self.assertEqual([g["label"] for g in got],
                         ["Server %d" % i for i in (1, 11, 13, 15, 17, 19,
                                                     21)])

    def test_filters_and_favorite_marks(self):
        got = self._run(self.payload, 'bol_catalogue({query:"",'
                        'cat:"Skyblock",lang:"fr"},[{id:5,name:"server 3"}])')
        self.assertEqual([g["label"] for g in got],
                         ["★ Server 3", "Server 9", "Server 15",
                          "Server 21", "Server 27"])

    def test_snapshot_moves_partners_out_of_favorites(self):
        got = self._run(self.payload, (
            'bol_snapshot({externalServerWorlds:[{id:1,name:"Mine"}]},'
            '{featuredExperiences:[{id:"f",name:"Hive",msgOfTheDay:"m",'
            'image:"i"}],creatorExperiences:null})'))
        self.assertEqual(got["favs"], [{"id": 1, "name": "Mine"}])
        self.assertEqual(got["featured"][0]["name"], "Hive")
        self.assertEqual(got["creators"], [])

    def test_strings_and_routes(self):
        got = self._run(self.payload, '[bol_t("showMore",[40,659]),'
                        'bol_isCatRoute("bol-x"),bol_isCatRoute("12"),'
                        'bol_selected("bol-x",{networkDetails:{id:"f"}},'
                        '{id:"bol-x"}),bol_selected("f",{networkDetails:'
                        '{id:"f"}},{id:"bol-x"})]')
        self.assertEqual(got, ["Show more (40 of 659)", True, False, True,
                               False])


if __name__ == "__main__":
    unittest.main()
