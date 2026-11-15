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
    'bD=oi("vanilla.thirdPartyWorldList"),VM=oi("vanilla.networkWorldDetails"),'
    'QM=oi("vanilla.networkWorldJoiner"),JM=oi("vanilla.notificationOptions");'
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
    'function JX({onClick:e}){return t.createElement(lf,{'
    'options:{button:ye.Y,inputLegend:C(".viewXboxProfile")},onClick:e})}'
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
        self.assertIn(["fr", 11, "Français", "français"], p["langs"])
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
                    "image": "https://minecraftpocket-servers.com/c.png",
                    "logo": "https://raw.githubusercontent.com/l/c-logo.gif",
                    "cover": "https://raw.githubusercontent.com/l/c-cover.png"},
                   {"id": "d", "name": "D", "ip": "d.example.org",
                    "logo": "https://evil.example/d.png"}]
        blobs = {"a.png": self.PNG, "b.gif": self.GIF, "c.png": b"<html>",
                 "c-logo.gif": self.GIF, "c-cover.png": self.PNG}
        asked = []

        def get(url, limit, timeout):
            asked.append(url)
            return blobs[url.rsplit("/", 1)[1]]
        self.assertEqual(sb.download_images(entries, cache, getter=get), 4)
        self.assertFalse([u for u in asked if "evil" in u])
        images = sb.install_images(hbui, entries, cache)
        self.assertEqual(images["a"], {"img": ("assets/bol-srv-a.png", 12.82)})
        self.assertEqual(images["b"], {"img": ("assets/bol-srv-b.gif", 25.0)})
        self.assertEqual(images["c"], {
            "lg": ("assets/bol-srv-lg-c.gif", 25.0),
            "cv": ("assets/bol-srv-cv-c.png", 12.82)})
        self.assertNotIn("d", images)
        self.assertTrue((hbui / "assets" / "bol-srv-a.png").is_file())
        payload = sb.build_payload(entries, images=images)
        c = [x for x in payload["servers"] if x["n"] == "C"][0]
        self.assertEqual((c["lg"], c["lh"], c["cv"], c["ch"]),
                         ("assets/bol-srv-lg-c.gif", 25.0,
                          "assets/bol-srv-cv-c.png", 12.82))
        self.assertNotIn("img", c)


class BundlePatch(unittest.TestCase):
    def _patched(self, bundle):
        out, status = sb.patch_body(bundle)
        self.assertEqual(status, "applied")
        return out

    def test_direct_shape_names(self):
        names = sb._discover(_DIRECT)
        expect = {"R": "t", "FOCUS": "Lm", "SIDE": "Fle", "DETAILS": "xoe",
                  "UFM": "k", "UFC": "J", "JU": "jU", "KD": "kD",
                  "EXT": "vM", "TP": "bD", "DET": "VM", "JOINER": "QM",
                  "NOTIF": "JM", "USF": "Fo",
                  "UNWRAP": "Po", "BUTTON": "Cg", "ICONS": "HL",
                  "ROUTER": "_", "PANEL": "Zf", "IMAGE": "Bm", "SCROLL": "qp",
                  "TEXTFIELD": "wz", "UFS": "nt", "LEGEND": "lf",
                  "KEYS": "ye", "_prefix": "e"}
        self.assertEqual({k: names[k] for k in expect}, expect)

    # The game's second Servers tab, as 1.26.63 writes it (and the flag
    # that picks it), and as the 1.26.70 previews read the layout id.
    _V2 = (
        'function Cie(){const e=Jc(),n=Do(oK("mc-servers-tab-layout-id"),'
        'yie),{fabIds:a,isLoading:r,hasError:l}=Fee(n||yie),o=k((e,t,n)=>'
        'e||!t&&0===n.length,[],[l,r,a]);return t.createElement(Po,{data:o},'
        'e=>e?null:t.createElement(Wee,{fabIds:a,registry:Sie}))}'
        'const yie="ServerTab",Mee=e=>lr("vanilla.menus.fabQuery",e);'
        'const Ede=()=>{const e=z0("mc-servers-tab-v2");return '
        't.createElement(Po,{data:e},e=>e?t.createElement(wie,null):'
        't.createElement(Noe,null))};')
    _V2_PREVIEW = (
        'function H7(){const e=ss(),a=zo(fG("mc-servers-tab-layout-id"),z7)'
        '||z7,{fabIds:r,isLoading:l,hasError:o}=r3(a),i=T((e,t,n)=>e,[],'
        '[o,l,r]);return t.createElement(E6,{fabIds:r,registry:j7})}'
        'const t3=e=>lr("vanilla.menus.fabQuery",e);')

    def test_the_games_featured_layout_is_found(self):
        self.assertEqual(sb._layout_names(_DIRECT + self._V2), {
            "FABS": "Fee", "LAYOUT": "yie", "QUERY": "lr"})
        self.assertEqual(sb._layout_names(self._V2_PREVIEW), {
            "FABS": "r3", "LAYOUT": "z7", "QUERY": "lr"})
        # A build without it draws our own row.
        self.assertEqual(sb._layout_names(_DIRECT), sb._LAYOUT_DEFAULTS)
        out = self._patched(_DIRECT)
        self.assertIn("bol_noLayout(\"\")", out)

    def test_the_second_tab_gives_way_to_the_page(self):
        out = self._patched(_DIRECT + self._V2)
        self.assertIn('e=>e&&!bol_on()?t.createElement(wie,null):', out)
        self.assertIn("Fee(yie)", out)
        self.assertIn('lr("vanilla.menus.fabQuery",', out)
        self.assertIn('lr("vanilla.menus.imageQuery",', out)

    def test_wrapped_shape_names(self):
        names = sb._discover(_wrapped(_DIRECT))
        self.assertEqual(names["UFM"], "(0,r.useFacetMap)")
        self.assertEqual(names["USF"], "(0,r.useSharedFacet)")
        self.assertEqual(names["UNWRAP"], "r.Unwrap")
        self.assertEqual(names["R"], "n")
        self.assertEqual(names["LEGEND"], "lf")

    def test_the_tab_switches_on_the_global(self):
        for bundle in (_DIRECT, _wrapped(_DIRECT)):
            out = self._patched(bundle)
            R = sb._discover(bundle)["R"]
            self.assertIn("return " + "bol_on()?" + R
                          + ".createElement(bol_Hub,{routePrefix:e}):"
                          + R + ".createElement(Lm,{containerRef:c,", out)
            self.assertEqual(out.count("function bol_HubView("), 1)
            # Pasted next to the tab, in its scope; no placeholder left.
            self.assertLess(out.index("function bol_Hub("),
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
                       _DIRECT.replace("button:ye.Y", "button:ye.X"),
                       _DIRECT.replace(".serverNameLabel", ".nameLabel")):
            self.assertEqual(sb.patch_body(broken), (broken, "lost"))

    @unittest.skipUnless(NODE, "node is not installed")
    def test_the_patched_bundle_parses(self):
        for bundle in (_DIRECT, _wrapped(_DIRECT)):
            out = self._patched(bundle)
            r = subprocess.run([NODE, "--check", "-"], input=out,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)


class Ping(unittest.TestCase):
    @staticmethod
    def _pong(text):
        body = text.encode()
        return (b"\x1c" + b"\0" * 16 + sb._RAKNET_MAGIC
                + len(body).to_bytes(2, "big") + body)

    def test_a_pong_gives_the_player_counts(self):
        self.assertEqual(sb.parse_pong(self._pong(
            "MCPE;My server;800;1.26.60;17;100;123;Sub;Survival;1;19132;")),
            {"o": 17, "m": 100})
        self.assertIsNone(sb.parse_pong(self._pong("MCPE;broken")))
        self.assertIsNone(sb.parse_pong(b"\x1c" + b"\0" * 40))
        self.assertIsNone(sb.parse_pong(self._pong(
            "MCPE;x;1;1;many;100")))

    def test_answers_are_matched_to_every_name_of_an_address(self):
        pong = self._pong("MCPE;m;800;1.26;3;20;1;s;Survival;")

        class Sock:
            def __init__(self):
                self.sent, self.inbox = [], []
            def setblocking(self, flag):
                pass
            def sendto(self, packet, address):
                self.sent.append(address)
                if address == ("10.0.0.1", 19132):
                    self.inbox.append((pong, address))
            def recvfrom(self, size):
                self._r.recv(1)
                return self.inbox.pop(0)
            def fileno(self):
                return self._r.fileno()
        sock = Sock()
        import socket as real
        a, b = real.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        sock._r = a
        b.send(b"x")  # select() then sees the fake socket readable once
        names = {"a.example.org": "10.0.0.1", "b.example.org": "10.0.0.1",
                 "down.example.org": "10.0.0.2", "gone.example.org": None}
        got = sb.ping_servers(
            [("A.example.org", 19132), ("b.example.org", 19132),
             ("down.example.org", 19132), ("gone.example.org", 19132)],
            timeout=0.5, resolve=names.get, sock=sock)
        self.assertEqual(got, {"a.example.org:19132": {"o": 3, "m": 20},
                               "b.example.org:19132": {"o": 3, "m": 20}})
        self.assertEqual(sorted(sock.sent), [("10.0.0.1", 19132),
                                             ("10.0.0.2", 19132)])

    def test_status_reaches_the_payload_and_its_cache_expires(self):
        cache = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, cache)
        sb.save_status({"play1.example.org:19132": {"o": 5, "m": 50}},
                       cache, now=1000)
        status = sb.cached_status(cache, now=1000 + 60)
        self.assertIsNone(sb.cached_status(cache, now=1000 + 2 * 86400))
        p = sb.build_payload(sb._valid_entries(_entries()), status=status)
        by = {s["n"]: s for s in p["servers"]}
        self.assertEqual((by["Server 1"]["on"], by["Server 1"]["mx"]),
                         (5, 50))
        self.assertEqual(by["Server 2"]["off"], 1)
        self.assertTrue(p["pinged"])
        p = sb.build_payload(sb._valid_entries(_entries()))
        self.assertFalse(any("off" in s for s in p["servers"]))
        self.assertFalse(p["pinged"])


class PlayerServers(unittest.TestCase):
    def test_every_account_list_is_read_by_address(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        one, two = root / "1", root / "2"
        one.mkdir()
        two.mkdir()
        (one / "external_servers.txt").write_text(
            "1:Linesia SkyFaction:play.linesia.net:19132:1791021913\n"
            "2:Odd: name:Play.Example.org:19133:1\nbroken line\n")
        (two / "external_servers.txt").write_text("")
        self.assertEqual(sb.player_servers([one, two, root / "none"]), {
            "play.linesia.net:19132": "Linesia SkyFaction",
            "play.example.org:19133": "Odd: name"})

    def test_the_language_falls_back_to_the_locale(self):
        self.assertEqual(sb.ui_language("", {"LANG": "fr_FR.UTF-8"}), "fr")
        self.assertEqual(sb.ui_language("", {"LC_ALL": "C",
                                             "LANG": "fr_CA.UTF-8"}), "fr")
        self.assertEqual(sb.ui_language("en_US", {"LANG": "fr_FR"}), "en")
        self.assertEqual(sb.ui_language("", {}), "en")


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
        self.assertIn("function bol_HubView(", body)
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
        self.assertNotIn("bol_HubView", js.read_text(encoding="utf-8"))

    def test_the_ping_rewrites_the_global_alone(self):
        game, js = self._game(_DIRECT)
        entries = sb._valid_entries(_entries())
        branding.feature_partner_servers(game,
                                         browser=sb.build_payload(entries))
        before = js.read_text(encoding="utf-8").partition("\n")
        live = sb.build_payload(entries, status={
            "play1.example.org:19132": {"o": 9, "m": 10}})
        self.assertTrue(sb.rewrite_runtime(js, live))
        after = js.read_text(encoding="utf-8").partition("\n")
        self.assertEqual(after[2], before[2])
        self.assertTrue(after[0].startswith("var __bolPartnerList="))
        self.assertIn('"on":9', after[0])
        self.assertEqual(after[0].count("__bolServerBrowser="), 1)

    def test_a_tab_the_browser_cannot_read_keeps_the_partners(self):
        game, js = self._game(_DIRECT.replace(".serverNameLabel", ".x"))
        on = sb.build_payload(sb._valid_entries(_entries()))
        self.assertTrue(branding.feature_partner_servers(game, browser=on))
        text = js.read_text(encoding="utf-8")
        self.assertIn("__bolPartnerOf", text)
        self.assertNotIn("__bolServerBrowser", text)


@unittest.skipUnless(NODE, "node is not installed")
class TabLogic(unittest.TestCase):
    """The page's cards, rows and search, run in Node on the real template."""

    def _run(self, payload, expr):
        names = {k: "S_" + k for k in sb._PLACEHOLDERS}
        code = sb._render(sb._template(), names)
        script = ("var S_KD={External:1,ThirdParty:2};"
                  "var S_R={memo:function(f){return f;}};"
                  + sb.runtime_line(payload) + code
                  + ";process.stdout.write(JSON.stringify(" + expr + "))")
        r = subprocess.run([NODE, "-"], input=script, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def setUp(self):
        entries = sb._valid_entries(_entries())
        entries[4]["description"] = "Un serveur Créatif très calme"
        status = {f"play{i}.example.org:19132": {"o": i * 10, "m": 500}
                  for i in range(0, 30, 2)}
        status["play.linesia.net:19132"] = {"o": 1000, "m": 2000}
        self.payload = sb.build_payload(
            entries, ui="fr", partner_hosts=["play.linesia.net"],
            status=status,
            mine={"play3.example.org:19132": "My third"})
        self.snap = ('{favs:[{id:5,name:"My third"},{id:6,name:"Elsewhere",'
                     'motd:"progressScreen.message.locating"}],'
                     'partners:[],featured:[{id:"h",name:"Hive",'
                     'image:"hive.png"}],creators:[]}')

    def rows(self, query="", lang=""):
        return self._run(self.payload, (
            'bol_rowsFor(bol_cards(%s),bol_words(%s),%s).map(function(r){'
            'return [r.key,r.title,r.cards.map(function(c){return c.n})]})'
            % (self.snap, json.dumps(query), json.dumps(lang))))

    def test_rows_in_order_with_the_most_played_first(self):
        rows = self.rows()
        keys = [r[0] for r in rows]
        self.assertEqual(keys[:4], ["mine", "featured", "local", "popular"])
        self.assertEqual(keys[-1], "all")
        self.assertIn("cat-Factions", keys)
        mine = rows[0][2]
        self.assertEqual(mine, ["Server 3", "Elsewhere"])
        self.assertEqual(rows[1][2], ["Hive"])
        self.assertEqual(rows[2][1], "Les plus joués en français")
        self.assertEqual(rows[2][2][:3], ["Linesia", "Server 24",
                                          "Server 18"])
        popular = rows[3][2]
        self.assertEqual(popular[:2], ["Linesia", "Server 28"])
        self.assertNotIn("Server 1", popular)  # down: not featured
        every = rows[-1][2]
        self.assertEqual(len(every), 31)
        self.assertEqual(every[-1], "Server 29")  # down ones last

    def test_search_by_address_category_and_folded_keyword(self):
        def names(q):
            return self.rows(q)[0][2]
        self.assertEqual(names("play7.example"), ["Server 7"])
        self.assertEqual(names("creatif"), ["Server 4"])
        self.assertEqual(names("elsew"), ["Elsewhere"])
        self.assertEqual(self.rows("nothing at all"), [])
        # Equally relevant: the ones that answered the ping first.
        found = names("number")
        self.assertEqual(found[:2], ["Server 28", "Server 26"])
        self.assertEqual(found[-1], "Server 29")

    def test_language_filter(self):
        rows = {r[0]: r for r in self.rows(lang="en")}
        self.assertNotIn("featured", rows)
        self.assertNotIn("mine", rows)  # both of the player's are not English
        self.assertEqual(rows["popular"][1], "Les plus joués en anglais")
        for _, _, names in rows.values():
            for n in names:
                self.assertNotEqual(int(n.split()[1]) % 3, 0, n)

    def test_cards_know_the_players_servers(self):
        got = self._run(self.payload, (
            'bol_cards(%s).cat.filter(function(c){return c.fav})'
            '.map(function(c){return [c.n,c.id,c.kind]})' % self.snap))
        self.assertEqual(got, [["Server 3", 5, "mine"]])
        got = self._run(self.payload, (
            'bol_cards(%s).mine.map(function(c){return c.motd||""})'
            % self.snap))
        self.assertEqual(got[1], "")  # an untranslated key is not shown

    def test_featured_walk_reads_each_servers_details_once(self):
        # Two featured servers: the first answers its details and its ping,
        # the second never loads; both end up tried, the first enriched.
        got = self._run(self.payload, (
            '(function(){var asked=[];bol_ctx.load=function(i,k){'
            'asked.push([i,k])};var ids=["h","g"],t=0,steps=[];'
            'steps.push(bol_featStep({ok:false},ids,t));'
            'steps.push(bol_featStep({ok:true,id:"h",img:"b.png",on:12,'
            'mx:99,ping:0},ids,t+=100));'
            'steps.push(bol_featStep({ok:true,id:"h",img:"b.png",on:12,'
            'mx:99,ping:2},ids,t+=300));'
            'steps.push(bol_featStep({ok:true,id:"h"},ids,t+=7000));'
            'steps.push(bol_featStep({ok:true,id:"h"},ids,t+=100));'
            'var c=bol_withDetails(bol_cards(%s).featured[0]);'
            'return [asked,steps,c.wide,c.wh,c.on]})()' % self.snap))
        asked, steps, wide, wh, on = got
        self.assertEqual(asked, [["h", 2], ["g", 2]])
        self.assertEqual(steps, [400, 400, 400, 0, 0])
        self.assertEqual((wide, wh, on), ("b.png", 30, 12))

    def test_a_layout_block_gives_its_experiences(self):
        got = self._run(self.payload, (
            'bol_expsOf({fab:{pagedExperiences:{experiences:['
            '{experienceId:"e1",linksTo:{id:"l1"},title:{value:"MegaSMP"},'
            'creatorName:"InPvP",listing:{displayImage:{url:"https://x/a"},'
            'motd:{value:"SURVIVAL"}}},{title:null}]}}}).concat('
            'bol_expsOf({fab:{experience:{}}}))'))
        self.assertEqual(got, [{"eid": "e1", "lid": "l1", "n": "MegaSMP",
                                "by": "InPvP", "url": "https://x/a",
                                "motd": "SURVIVAL"}])

    def test_catalogue_cards_carry_their_art(self):
        entries = [{"id": "a", "name": "A", "ip": "a.example.org",
                    "description": "x", "lang": "en"}]
        payload = sb.build_payload(entries, images={"a": {
            "lg": ("assets/l.png", 100.0), "cv": ("assets/c.jpg", 56.25)}})
        got = self._run(payload, (
            'bol_cards({favs:[],partners:[],featured:[],creators:[]}).cat'
            '.map(function(c){return [c.logo,c.wide,c.wh,c.banner]})'))
        self.assertEqual(got, [["assets/l.png", "assets/c.jpg", 56.25, ""]])

    def test_strings_and_counts(self):
        got = self._run(self.payload, '[bol_t("online",[bol_compact(1234)]),'
                        'bol_compact(20452),bol_langName("fr"),'
                        'bol_langName("fr",true),bol_langName("")]')
        self.assertEqual(got, ["1.2k en ligne", "20k", "Français",
                               "français", "Toutes les langues"])


if __name__ == "__main__":
    unittest.main()
