// BedrockOnLinux's own Servers tab, injected into the game's HBUI bundle by
// bol/serverbrowser.py. SPDX-License-Identifier: MIT
//
// This is not a module: the launcher pastes it into the bundle's own scope,
// right before the vanilla Servers-tab component, after replacing every
// __NAME__ placeholder with the minified name that build gives the matching
// React component or hook (see serverbrowser._discover). It may therefore
// only use those placeholders, its own bol_* names and plain JavaScript:
// every local starts with bol_ so it can never shadow a minified name.
//
// The data (catalogue, live player counts, settings, strings) is the
// __bolServerBrowser global the launcher writes on the bundle's first line.
// With it missing or switched off, the vanilla tab shows.
//
// One page, built like a game store: a spotlight at the top describing the
// tile that has focus, a search bar, the player's own servers,
// BedrockOnLinux's partners, the game's featured experiences in big tiles,
// then rows of tiles (the most played ones, one row per category, then
// everything). Pressing a tile opens that server's own page (its pictures,
// description, activities) at /play/servers?bol#<key>, an entry of the
// game's history of its own, so Back leaves it; Play there joins it. A
// catalogue server is first added to the player's own list, since the game
// only joins servers it knows, then joined the way the game's own details
// pane does.

var bol_state = {query: "", lang: "", mode: "", open: {}};
var bol_ROW = 8;
var bol_PAGE = 24;
var bol_ctx = {act: null, load: null, direct: null, router: null,
               toast: null, hash: false, prefix: "/play"};

function bol_data() {
  return typeof __bolServerBrowser !== "undefined" && __bolServerBrowser
    ? __bolServerBrowser : null;
}
function bol_on() {
  var bol_d = bol_data();
  return !!(bol_d && bol_d.on);
}
function bol_t(bol_key, bol_args) {
  var bol_d = bol_data(), bol_s = bol_d && bol_d.strings || {};
  var bol_out = String(bol_s[bol_key] != null ? bol_s[bol_key] : bol_key);
  (bol_args || []).forEach(function (bol_a, bol_i) {
    bol_out = bol_out.split("{" + bol_i + "}").join(String(bol_a));
  });
  return bol_out;
}
function bol_fold(bol_s) {
  bol_s = String(bol_s == null ? "" : bol_s).toLowerCase();
  try {
    bol_s = bol_s.normalize("NFD").replace(/[̀-ͯ]/g, "");
  } catch (bol_e) {}
  return bol_s;
}
function bol_words(bol_q) {
  return bol_fold(bol_q).split(/\s+/).filter(function (bol_w) {
    return bol_w.length > 0;
  });
}
function bol_servers() {
  var bol_d = bol_data();
  return bol_d && bol_d.servers || [];
}
// How well a catalogue server answers a search: the name weighs most, then
// the address and categories, then the description. 0 = no match.
function bol_score(bol_s, bol_ws) {
  if (!bol_ws.length) return 1;
  var bol_name = bol_fold(bol_s.n), bol_addr = bol_fold(bol_s.a);
  var bol_tags = bol_fold((bol_s.t || []).join(" ") + " " + (bol_s.ln || "")
                          + " " + (bol_s.l || "") + " " + (bol_s.c || ""));
  var bol_desc = bol_fold(bol_s.d), bol_total = 0;
  for (var bol_i = 0; bol_i < bol_ws.length; bol_i++) {
    var bol_w = bol_ws[bol_i], bol_best = 0;
    if (bol_name.indexOf(bol_w) === 0) bol_best = 8;
    else if (bol_name.indexOf(bol_w) >= 0) bol_best = 6;
    else if (bol_addr.indexOf(bol_w) >= 0) bol_best = 4;
    else if (bol_tags.indexOf(bol_w) >= 0) bol_best = 3;
    else if (bol_desc.indexOf(bol_w) >= 0) bol_best = 1;
    if (!bol_best) return 0;
    bol_total += bol_best;
  }
  return bol_total;
}
function bol_nameMatches(bol_name, bol_ws) {
  var bol_n = bol_fold(bol_name);
  return bol_ws.every(function (bol_w) { return bol_n.indexOf(bol_w) >= 0; });
}
function bol_compact(bol_n) {
  if (bol_n >= 10000) return Math.round(bol_n / 1000) + "k";
  if (bol_n >= 1000) return (Math.round(bol_n / 100) / 10) + "k";
  return String(bol_n);
}
// Most played first; servers that did not answer the last ping last.
function bol_byPlayers(bol_a, bol_b) {
  var bol_x = bol_a.s, bol_y = bol_b.s;
  var bol_ox = bol_x && bol_x.off ? 1 : 0, bol_oy = bol_y && bol_y.off ? 1 : 0;
  if (bol_ox !== bol_oy) return bol_ox - bol_oy;
  var bol_px = bol_x && bol_x.on || 0, bol_py = bol_y && bol_y.on || 0;
  if (bol_px !== bol_py) return bol_py - bol_px;
  return bol_a.o - bol_b.o;
}

// --------------------------------------------------------------------------
// Cards: one shape for a catalogue server, one of the player's own and a
// featured one of the game, so every row draws and joins them alike.

function bol_favIndex(bol_favs) {
  var bol_out = {};
  bol_favs.forEach(function (bol_f) {
    bol_out[bol_fold(bol_f.name)] = bol_f.id;
  });
  return bol_out;
}
// The game's id of a catalogue server already in the player's list: the
// launcher read that list's addresses (the game only hands out names).
function bol_favIdOf(bol_s, bol_byName) {
  var bol_d = bol_data(), bol_mine = bol_d && bol_d.mine || {};
  var bol_name = bol_mine[String(bol_s.a).toLowerCase() + ":" + bol_s.p];
  if (bol_name != null && bol_byName[bol_fold(bol_name)] != null)
    return bol_byName[bol_fold(bol_name)];
  var bol_id = bol_byName[bol_fold(bol_s.n)];
  return bol_id != null ? bol_id : null;
}
// A server's message of the day, unless it is a game string key the UI
// never translated ("progressScreen.message.locating").
function bol_motd(bol_m) {
  bol_m = String(bol_m == null ? "" : bol_m);
  return /^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)+$/.test(bol_m) ? "" : bol_m;
}
// The partners' own card: their banner, icon and text from the launcher,
// the catalogue's entry (and its player count) when the list has them, and
// the player's own entry when they added it, which the partner patch takes
// out of their list.
function bol_partnerCards(bol_snap, bol_catByHost) {
  var bol_d = bol_data();
  return (bol_d && bol_d.partners || []).map(function (bol_p, bol_i) {
    var bol_own = bol_snap.partners.filter(function (bol_e) {
      return bol_fold(bol_e.name) === bol_fold(bol_p.n);
    })[0];
    var bol_cat = bol_catByHost[String(bol_p.a).toLowerCase()];
    var bol_id = bol_own ? bol_own.id : bol_cat ? bol_cat.id : null;
    // Some networks' proxies never answer the status ping: a partner that
    // stayed silent is not called offline.
    var bol_s = bol_cat ? bol_cat.s : null;
    if (bol_s && bol_s.off) {
      bol_s = bol_merge({}, bol_s);
      delete bol_s.off;
    }
    return {k: bol_p.k, kind: bol_id != null ? "mine" : "cat", id: bol_id,
            n: bol_p.n, o: bol_i, fav: bol_id != null, pt: bol_p,
            s: bol_s || {i: bol_p.k, n: bol_p.n, a: bol_p.a, p: bol_p.p,
                         d: bol_p.d, t: [], l: ""},
            wide: bol_p.bn, wh: bol_p.bh || 30, logo: bol_p.lg, banner: ""};
  });
}
function bol_cards(bol_snap) {
  var bol_byName = bol_favIndex(bol_snap.favs), bol_catById = {};
  var bol_ptId = {}, bol_catByHost = {};
  bol_snap.partners.forEach(function (bol_e) {
    bol_ptId[bol_fold(bol_e.name)] = bol_e.id;
  });
  var bol_cat = bol_servers().map(function (bol_s, bol_i) {
    var bol_id = bol_favIdOf(bol_s, bol_byName);
    if (bol_id == null && bol_s.pt && bol_ptId[bol_fold(bol_s.n)] != null)
      bol_id = bol_ptId[bol_fold(bol_s.n)];
    // A partner that stayed silent is not called silent either.
    if (bol_s.pt && bol_s.off) {
      bol_s = bol_merge({}, bol_s);
      delete bol_s.off;
    }
    var bol_c = {k: bol_s.i, kind: bol_id != null ? "mine" : "cat",
                 id: bol_id, n: bol_s.n, s: bol_s, o: bol_i,
                 wide: bol_s.cv || "", wh: bol_s.ch || 56.25,
                 logo: bol_s.lg || "", banner: bol_s.img || "",
                 ih: bol_s.ih || 12.8, fav: bol_id != null};
    if (bol_id != null) bol_catById[bol_id] = bol_c;
    if (bol_s.pt) bol_catByHost[String(bol_s.a).toLowerCase()] = bol_c;
    return bol_c;
  });
  var bol_mine = bol_snap.favs.map(function (bol_f, bol_i) {
    return bol_catById[bol_f.id] || {
      k: "x" + bol_f.id, kind: "mine", id: bol_f.id, n: bol_f.name,
      motd: bol_motd(bol_f.motd), o: bol_i, fav: true, s: null};
  });
  function bol_game(bol_list, bol_kind) {
    return bol_list.map(function (bol_e, bol_i) {
      return {k: bol_kind + bol_e.id, kind: bol_kind, id: bol_e.id,
              n: bol_e.name, motd: bol_motd(bol_e.motd),
              logo: bol_e.image || "", wide: bol_e.banner || "", wh: 30,
              o: bol_i, s: null, fav: bol_kind === "mine"};
    });
  }
  return {cat: bol_cat, mine: bol_mine,
          partners: bol_partnerCards(bol_snap, bol_catByHost),
          featured: bol_game(bol_snap.featured, "tp")
            .concat(bol_game(bol_snap.creators, "tp"))};
}

// --------------------------------------------------------------------------
// Joining and the player's list.

function bol_say(bol_text) {
  if (bol_ctx.toast) bol_ctx.toast(bol_text);
}
// Joining as the game's own details pane does: straight away when the
// player has turned the safety warning off, through the game's warning
// screen otherwise. The details of the server are loaded first either way,
// as the game's tab always has them loaded by the time anyone presses Play.
function bol_join(bol_kind, bol_id) {
  if (bol_ctx.load) bol_ctx.load(bol_id, bol_kind);
  if (bol_ctx.direct && bol_ctx.direct(bol_kind, bol_id)) return;
  bol_ctx.router.push("/online-play-warning/" + __JU__(bol_kind) + "/"
                      + bol_id);
}
function bol_add(bol_s) {
  var bol_r = bol_ctx.act("add", bol_s);
  if (bol_r && bol_r.error == null && bol_r.id != null) return bol_r.id;
  bol_say(bol_r && bol_r.error === 6 ? bol_t("alreadyAdded")
                                     : bol_t("addFailed"));
  return null;
}
function bol_play(bol_c) {
  if (!bol_c || !bol_ctx.router) return;
  if (bol_c.kind === "tp") {
    bol_join(__KD__.ThirdParty, bol_c.id);
    return;
  }
  if (bol_c.kind === "exp") {
    if (bol_c.route) bol_ctx.router.push(bol_c.route);
    return;
  }
  var bol_id = bol_c.id;
  if (bol_id == null && bol_c.s) bol_id = bol_add(bol_c.s);
  if (bol_id != null) bol_join(__KD__.External, bol_id);
}
function bol_toggleFav(bol_c) {
  if (!bol_c || !bol_ctx.act || bol_c.kind === "tp" || bol_c.kind === "exp")
    return;
  if (bol_c.id != null) {
    var bol_r = bol_ctx.act("remove", null, bol_c.id);
    bol_say(bol_r && bol_r.error != null ? bol_t("removeFailed")
                                         : bol_t("removed", [bol_c.n]));
  } else if (bol_c.s && bol_add(bol_c.s) != null) {
    bol_say(bol_t("added", [bol_c.n]));
  }
}

// A server's own page, at /play/servers?bol#<card key>: an entry of the
// game's history of its own, so the game's Back leaves it. Never the
// route's /:id/:type, which the game's tab loads a server from: a
// catalogue key there crashes the game. The "?bol" is what tells it from
// the tab itself, since the router refuses to push the path it is on.
// Without the hash hook in this build, a tile joins at once.
function bol_open(bol_c) {
  if (!bol_c || !bol_ctx.router) return;
  if (bol_c.kind === "exp" || !bol_ctx.hash) {
    bol_play(bol_c);
    return;
  }
  bol_ctx.router.push(bol_ctx.prefix + "/servers?bol#"
                      + encodeURIComponent(String(bol_c.k)));
}
// The card a page's key names: a partner first, then the catalogue, the
// player's own servers and the game's featured ones.
function bol_findCard(bol_all, bol_key) {
  if (!bol_key) return null;
  var bol_lists = [bol_all.partners, bol_all.cat, bol_all.mine,
                   bol_all.featured];
  for (var bol_i = 0; bol_i < bol_lists.length; bol_i++) {
    for (var bol_j = 0; bol_j < bol_lists[bol_i].length; bol_j++) {
      if (String(bol_lists[bol_i][bol_j].k) === bol_key)
        return bol_lists[bol_i][bol_j];
    }
  }
  // A featured server of the game's layout, not in its featured list.
  return bol_key.slice(0, 2) === "tp" && bol_key.length > 2
    ? {k: bol_key, kind: "tp", id: bol_key.slice(2), n: "", s: null, o: 0,
       logo: "", wide: "", fav: false}
    : null;
}

// --------------------------------------------------------------------------
// The spotlight follows focus through a tiny store, so that moving between
// tiles redraws the spotlight alone, never the hundred tiles.

var bol_spot = {v: null, ls: []};
function bol_spotSet(bol_v) {
  if (bol_spot.v === bol_v) return;
  bol_spot.v = bol_v;
  bol_spot.ls.slice().forEach(function (bol_f) { bol_f(bol_v); });
}
function bol_useSpot() {
  var bol_s = __R__.useState(bol_spot.v);
  __R__.useEffect(function () {
    var bol_f = function (bol_v) { bol_s[1](bol_v); };
    bol_spot.ls.push(bol_f);
    return function () {
      var bol_i = bol_spot.ls.indexOf(bol_f);
      if (bol_i >= 0) bol_spot.ls.splice(bol_i, 1);
    };
  }, []);
  return bol_s[0];
}

// --------------------------------------------------------------------------
// The game's featured servers come with an icon only; their banner and
// player count are in their details, which the game loads one server at a
// time. Walked through once per session, in the background, one by one.

var bol_feat = {info: {}, tried: {}, pending: null, at: 0};
var bol_FEAT_WAIT = 6000, bol_PING_WAIT = 2500;

// A featured card with what its details told.
function bol_withDetails(bol_c) {
  var bol_i = bol_c.kind === "tp" ? bol_feat.info[bol_c.id] : null;
  if (!bol_i) return bol_c;
  var bol_out = {};
  for (var bol_k in bol_c) bol_out[bol_k] = bol_c[bol_k];
  if (bol_i.img) { bol_out.wide = bol_i.img; bol_out.wh = 30; }
  if (bol_i.on != null) { bol_out.on = bol_i.on; bol_out.mx = bol_i.mx; }
  return bol_out;
}

// One step of the walk, from the details as they stand: records the server
// being loaded once its details (and ping) are in, then loads the next.
// Returns how long to wait before looking again, or 0 when done.
function bol_featStep(bol_det, bol_ids, bol_now) {
  var bol_f = bol_feat;
  if (bol_f.pending != null) {
    var bol_mine = bol_det && bol_det.ok && bol_det.id === bol_f.pending;
    var bol_age = bol_now - bol_f.at;
    if (bol_mine) {
      var bol_pinged = bol_det.ping > 0 || bol_age > bol_PING_WAIT;
      bol_f.info[bol_f.pending] = {
        img: bol_det.img || "",
        on: bol_det.ping > 0 && bol_det.on >= 0 ? bol_det.on : null,
        mx: bol_det.mx};
      if (!bol_pinged) return 400;
    } else if (bol_age < bol_FEAT_WAIT) {
      return 400;
    }
    bol_f.pending = null;
  }
  for (var bol_i = 0; bol_i < bol_ids.length; bol_i++) {
    var bol_id = String(bol_ids[bol_i]);
    if (bol_f.tried[bol_id]) continue;
    if (!bol_ctx.load) return 0;
    bol_f.tried[bol_id] = 1;
    bol_f.pending = bol_id;
    bol_f.at = bol_now;
    bol_ctx.load(bol_id, __KD__.ThirdParty);
    return 400;
  }
  return 0;
}

// --------------------------------------------------------------------------
// Looks: the game's own, a black outline round stone panels lit from the
// top with the darker lip every Minecraft button has, its display font for
// names and titles and its pixel font for the rest.

var bol_WHITE = "#ffffff", bol_DIM = "#b1b2b5", bol_GREEN = "#6cc349";
var bol_INK = "#1e1e1f", bol_STONE = "#48494a", bol_LIP = "#313233";
var bol_GOLD = "#ffc72c", bol_RED = "#f26b6b", bol_GRASS = "#3c8527";
var bol_TEN = "\"Minecraft Ten v2\", \"Noto Sans\"";
var bol_SEVEN = "\"Minecraft Seven v2\", \"Noto Sans\"";
var bol_SHADOW = "0.2rem 0.2rem 0 rgba(0,0,0,0.5)";
var bol_PALETTES = [
  ["#3a6f2a", "#1d3a14"], ["#8a2b2b", "#3d1111"], ["#2a5d8a", "#11283d"],
  ["#6b3a8a", "#2c163b"], ["#8a6a2b", "#3d2c11"], ["#2b8a7d", "#113d37"],
  ["#8a2b6a", "#3d112d"], ["#4a4f8a", "#1d1f3d"], ["#5d6b2b", "#283011"],
  ["#8a4a2b", "#3d1e11"]];
var bol_TAG_COLORS = {pvp: 1, skyblock: 2, factions: 3, prison: 7,
                      minigames: 4, bedwars: 6, survival: 0, smp: 0,
                      roleplay: 6, creative: 5, oneblock: 2, parkour: 8,
                      anarchy: 9, earth: 5, modded: 7, hardcore: 1};
function bol_palette(bol_c) {
  var bol_tags = bol_c.s && bol_c.s.t || [];
  for (var bol_i = 0; bol_i < bol_tags.length; bol_i++) {
    var bol_k = bol_fold(bol_tags[bol_i]).replace(/[^a-z]/g, "");
    if (bol_TAG_COLORS[bol_k] != null)
      return bol_PALETTES[bol_TAG_COLORS[bol_k]];
  }
  var bol_h = 0, bol_n = String(bol_c.n);
  for (var bol_j = 0; bol_j < bol_n.length; bol_j++)
    bol_h = (bol_h * 31 + bol_n.charCodeAt(bol_j)) % 1000;
  return bol_PALETTES[bol_h % bol_PALETTES.length];
}
function bol_merge(bol_a, bol_b) {
  for (var bol_k in bol_b || {}) bol_a[bol_k] = bol_b[bol_k];
  return bol_a;
}
function bol_div(bol_style, bol_kids) {
  return __R__.createElement.apply(null,
    ["div", {style: bol_style}].concat(bol_kids || []));
}
function bol_text(bol_value, bol_style) {
  return __R__.createElement("div", {style: bol_style}, bol_value);
}
function bol_line(bol_value, bol_size, bol_color, bol_extra) {
  return bol_text(bol_value, bol_merge({
    fontSize: bol_size, color: bol_color, fontFamily: bol_SEVEN,
    whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis"},
    bol_extra));
}
// A name or a title: the display font, with its drop shadow.
function bol_title(bol_value, bol_size, bol_extra) {
  return bol_line(bol_value, bol_size, bol_WHITE, bol_merge(
    {fontFamily: bol_TEN, textShadow: bol_SHADOW}, bol_extra));
}
// A small black label, as the game prints a world's mode on its tile.
function bol_chip(bol_value, bol_extra) {
  return bol_text(bol_value, bol_merge({
    fontFamily: bol_SEVEN, fontSize: "1.3rem", color: bol_WHITE,
    backgroundColor: "rgba(0,0,0,0.6)", padding: "0.15rem 0.6rem",
    marginRight: "0.5rem", flexShrink: 0, whiteSpace: "nowrap"},
    bol_extra));
}
function bol_partnerChip(bol_size) {
  return bol_chip(bol_t("partnerTag").toUpperCase(), {
    backgroundColor: bol_GOLD, color: bol_INK, fontSize: bol_size || "1.3rem",
    border: "0.2rem solid " + bol_INK});
}
// The pixel pip a status starts with.
function bol_pip(bol_color) {
  return bol_div({width: "0.8rem", height: "0.8rem", flexShrink: 0,
                  backgroundColor: bol_color, marginRight: "0.6rem",
                  border: "0.1rem solid " + bol_INK});
}
// A stone panel: lit top edge, shaded right edge, the lip under it.
function bol_stone(bol_kids, bol_extra) {
  return bol_div(bol_merge({
    display: "flex", flexDirection: "column", flexGrow: 1,
    backgroundColor: bol_STONE,
    borderTop: "0.2rem solid #5d5e60",
    borderLeft: "0.2rem solid #545557",
    borderRight: "0.2rem solid #3d3e40",
    borderBottom: "0.4rem solid " + bol_LIP}, bol_extra), bol_kids);
}
// A tile's frame: the black outline, its picture, a stone panel under it.
function bol_frame(bol_art, bol_kids, bol_extra) {
  return bol_div(bol_merge({display: "flex", flexDirection: "column",
                            border: "0.2rem solid " + bol_INK,
                            backgroundColor: bol_INK}, bol_extra), [
    bol_art ? bol_div({borderBottom: "0.2rem solid " + bol_INK}, [bol_art])
            : null,
    bol_stone(bol_kids)]);
}
function bol_online(bol_c) {
  return bol_c.s ? bol_c.s.on : bol_c.on;
}
// Whether the ping said anything about a server.
function bol_known(bol_c) {
  return bol_online(bol_c) != null || !!(bol_c.s && bol_c.s.off);
}
// "■ 1.2k online", "Offline", or the categories.
function bol_status(bol_c, bol_size) {
  var bol_s = bol_c.s, bol_on = bol_online(bol_c);
  if (bol_on != null) {
    return bol_div({display: "flex", flexDirection: "row",
                    alignItems: "center", flexShrink: 0}, [
      bol_pip(bol_GREEN),
      bol_line(bol_t("online", [bol_compact(bol_on)]), bol_size,
               bol_GREEN)]);
  }
  // Many networks' proxies never answer the status ping: silent, not down.
  if (bol_s && bol_s.off)
    return bol_div({display: "flex", flexDirection: "row",
                    alignItems: "center", flexShrink: 0}, [
      bol_pip(bol_DIM), bol_line(bol_t("noAnswer"), bol_size, bol_DIM)]);
  var bol_bits = bol_s ? (bol_s.t || []).slice(0, 2) : [];
  if (bol_s && bol_s.l) bol_bits.push(bol_s.l.toUpperCase());
  return bol_line(bol_bits.length ? bol_bits.join(" · ")
                  : (bol_c.by || bol_c.motd || bol_t("server")), bol_size,
                  bol_DIM);
}
// Under a tile's name: its categories as labels, else who runs it.
function bol_sub(bol_c, bol_n, bol_size) {
  var bol_s = bol_c.s;
  var bol_tags = bol_s ? (bol_s.t || []).slice(0, bol_n) : [];
  if (bol_s && bol_s.l) bol_tags.push(bol_s.l.toUpperCase());
  var bol_box = {display: "flex", flexDirection: "row", alignItems: "center",
                 overflow: "hidden", height: "2.2rem", marginTop: "0.4rem"};
  if (bol_tags.length)
    return bol_div(bol_box, bol_tags.map(function (bol_x) {
      return bol_chip(bol_x, bol_size ? {fontSize: bol_size} : null);
    }));
  return bol_div(bol_box, [
    bol_line(bol_c.by || bol_c.motd || bol_t("server"),
             bol_size || "1.4rem", bol_DIM)]);
}
function bol_name(bol_c, bol_size, bol_extra) {
  return bol_div(bol_merge({display: "flex", flexDirection: "row",
                            alignItems: "center", overflow: "hidden"},
                           bol_extra), [
    bol_c.fav ? bol_text("★", {fontSize: bol_size, color: bol_GOLD,
                                    marginRight: "0.5rem", flexShrink: 0,
                                    textShadow: bol_SHADOW})
              : null,
    bol_title(bol_c.n, bol_size, {flexShrink: 1})]);
}

// The picture of a card: its wide cover cropped to the box, else its logo
// on its category's colours, else its banner strip on them, else its name.
function bol_Art(bol_p) {
  var bol_c = bol_p.card, bol_h = __R__.createElement;
  var bol_pal = bol_palette(bol_c);
  var bol_box = {height: bol_p.height, overflow: "hidden", display: "flex",
                 flexDirection: "column", justifyContent: "center",
                 backgroundImage: "linear-gradient(160deg, " + bol_pal[0]
                   + " 0%, " + bol_pal[1] + " 100%)",
                 backgroundColor: bol_pal[1]};
  if (bol_c.wide && !bol_p.noWide)
    return bol_div(bol_box, [
      bol_h(__IMAGE__, {src: bol_c.wide, paddingTopPercentage: bol_c.wh})]);
  if (bol_c.logo) {
    bol_box.alignItems = "center";
    var bol_size = bol_p.logoSize || bol_p.height;
    return bol_div(bol_box, [
      bol_h(__IMAGE__, {src: bol_c.logo, height: bol_size,
                        width: bol_size})]);
  }
  if (bol_c.banner)
    return bol_div(bol_box, [bol_div({padding: "0 0.8rem"}, [
      bol_h(__IMAGE__, {src: bol_c.banner,
                        paddingTopPercentage: bol_c.ih})])]);
  return bol_div(bol_box, [
    bol_text(bol_c.n, {fontSize: bol_p.big || "2.5rem", color: bol_WHITE,
                       fontFamily: bol_TEN, textShadow: bol_SHADOW,
                       textAlign: "center", padding: "0 1rem",
                       overflow: "hidden", maxHeight: "100%"})]);
}
// A logo badge over a cover: the server's mark stays readable.
function bol_Badge(bol_p) {
  var bol_c = bol_p.card;
  if (!bol_c.wide || !bol_c.logo) return null;
  return bol_div({position: "absolute", left: "0.8rem", bottom: "0.8rem",
                  width: bol_p.size, height: bol_p.size,
                  backgroundColor: "rgba(0,0,0,0.6)",
                  border: "0.2rem solid " + bol_INK, padding: "0.3rem"}, [
    __R__.createElement(__IMAGE__, {src: bol_c.logo, height: "100%",
                                    width: "100%"})]);
}
// Who is on, in the picture's corner, as the game marks a live world.
function bol_Players(bol_p) {
  var bol_c = bol_p.card, bol_on = bol_online(bol_c);
  var bol_box = {position: "absolute", top: "0.8rem", right: "0.8rem",
                 display: "flex", flexDirection: "row", alignItems: "center",
                 backgroundColor: "rgba(0,0,0,0.7)",
                 padding: "0.2rem 0.7rem"};
  if (bol_on != null)
    return bol_div(bol_box, [bol_pip(bol_GREEN),
                             bol_line(bol_compact(bol_on), bol_p.size,
                                      bol_WHITE)]);
  return null;
}
function bol_ArtWithBadge(bol_p) {
  var bol_h = __R__.createElement;
  return bol_div({position: "relative", height: bol_p.height}, [
    bol_h(bol_Art, bol_p),
    bol_h(bol_Badge, {card: bol_p.card, size: bol_p.badge}),
    bol_h(bol_Players, {card: bol_p.card, size: bol_p.chip || "1.4rem"})]);
}

// --------------------------------------------------------------------------
// Tiles. Memoised: a new search or list update redraws only the tiles
// whose card changed.

// What pressing, focusing and the second button do on one tile.
function bol_useTile(bol_c) {
  var bol_R = __R__, bol_ref = bol_R.useRef(bol_c);
  bol_ref.current = bol_c;
  return {
    onClick: bol_R.useCallback(function () { bol_open(bol_ref.current); },
                               []),
    onFocus: bol_R.useCallback(function (bol_f) {
      if (bol_f) bol_spotSet(bol_ref.current);
    }, []),
    onFav: bol_R.useCallback(function () {
      bol_toggleFav(bol_ref.current);
    }, [])};
}
function bol_panel(bol_c, bol_on, bol_kids) {
  return __R__.createElement.apply(null, [__PANEL__, {
    compact: true, isTransparent: true, focusable: true, role: "neutral",
    onClick: bol_on.onClick, onFocusChanged: bol_on.onFocus,
    onHoverChanged: bol_on.onFocus, onClickSecondary: bol_on.onFav,
    narrationText: bol_c.n,
    inputLegend: bol_ctx.hash && bol_c.kind !== "exp" ? bol_t("details")
                                                        : bol_t("play")}]
    .concat(bol_kids));
}
function bol_sameCard(bol_a, bol_b) {
  var bol_x = bol_a.card, bol_y = bol_b.card;
  return bol_x.k === bol_y.k && bol_x.fav === bol_y.fav
    && bol_x.id === bol_y.id && bol_x.n === bol_y.n
    && bol_x.wide === bol_y.wide && bol_x.on === bol_y.on
    && bol_x.s === bol_y.s;
}

function bol_TileView(bol_p) {
  var bol_c = bol_p.card, bol_h = __R__.createElement;
  var bol_on = bol_useTile(bol_c);
  return bol_div({width: "25%", padding: "0.6rem"}, [
    bol_panel(bol_c, bol_on, [bol_frame(
      bol_h(bol_ArtWithBadge, {card: bol_c, height: "13rem",
                               logoSize: "11rem", badge: "4.4rem"}),
      [bol_div({padding: "0.7rem 1rem 0.7rem"}, [
        bol_name(bol_c, "1.9rem"), bol_sub(bol_c, 2)])])])]);
}
var bol_Tile = __R__.memo(bol_TileView, bol_sameCard);

// A featured experience: half or a third of the row, a big picture.
function bol_BigTileView(bol_p) {
  var bol_c = bol_p.card, bol_h = __R__.createElement;
  var bol_on = bol_useTile(bol_c);
  var bol_third = !!bol_p.third;
  return bol_div({width: bol_third ? "33.33%" : "50%", padding: "0.6rem"}, [
    bol_panel(bol_c, bol_on, [bol_frame(
      bol_h(bol_ArtWithBadge, {card: bol_c,
                               height: bol_third ? "15.5rem" : "21rem",
                               logoSize: "17rem", badge: "6.5rem",
                               big: "4rem", chip: "1.6rem"}),
      [bol_div({padding: "0.8rem 1.2rem 0.8rem"}, [
        bol_name(bol_c, bol_third ? "2.1rem" : "2.5rem"),
        bol_sub(bol_c, 4, "1.4rem")])])])]);
}
var bol_BigTile = __R__.memo(bol_BigTileView, bol_sameCard);

// A partner: the whole row, its banner beside its pitch.
function bol_PartnerTileView(bol_p) {
  var bol_c = bol_p.card, bol_pt = bol_c.pt, bol_h = __R__.createElement;
  var bol_on = bol_useTile(bol_c);
  return bol_div({width: "100%", padding: "0.6rem"}, [
    bol_panel(bol_c, bol_on, [bol_div({
      display: "flex", flexDirection: "row",
      border: "0.2rem solid " + bol_INK, backgroundColor: bol_INK}, [
      bol_div({width: "52%", flexShrink: 0, position: "relative",
               display: "flex", flexDirection: "column",
               justifyContent: "center",
               borderRight: "0.2rem solid " + bol_INK}, [
        bol_h(__IMAGE__, {src: bol_pt.bn, paddingTopPercentage: bol_pt.bh}),
        bol_h(bol_Players, {card: bol_c, size: "1.6rem"})]),
      bol_stone([
        bol_div({display: "flex", flexDirection: "row",
                 alignItems: "center"}, [
          bol_div({width: "6.4rem", height: "6.4rem", flexShrink: 0,
                   marginRight: "1.2rem", border: "0.2rem solid " + bol_INK,
                   backgroundColor: "rgba(0,0,0,0.4)"}, [
            bol_h(__IMAGE__, {src: bol_pt.lg, height: "100%",
                              width: "100%"})]),
          bol_div({display: "flex", flexDirection: "column",
                   flexGrow: 1, flexBasis: 0, overflow: "hidden"}, [
            bol_div({display: "flex", flexDirection: "row"}, [
              bol_partnerChip()]),
            bol_name(bol_c, "3rem", {marginTop: "0.4rem"})])]),
        bol_div({display: "flex", flexDirection: "row",
                 alignItems: "center", marginTop: "0.8rem"}, [
          bol_chip(bol_pt.msg),
          bol_div({marginLeft: "0.6rem"}, [bol_status(bol_c, "1.6rem")])]),
        bol_text(bol_pt.d.split("\n")[0], {
          fontFamily: bol_SEVEN, fontSize: "1.5rem", color: bol_DIM,
          marginTop: "0.8rem", overflow: "hidden", maxHeight: "6.6rem",
          flexShrink: 1})],
        {padding: "1.2rem 1.6rem", overflow: "hidden", flexGrow: 0,
         width: "48%"})])])]);
}
var bol_PartnerTile = __R__.memo(bol_PartnerTileView, bol_sameCard);

// The last tile of a row that has more: "See all (42)" / "Show more".
function bol_MoreTile(bol_p) {
  var bol_h = __R__.createElement;
  return bol_div({width: "25%", padding: "0.6rem"}, [
    bol_h(__PANEL__, {compact: true, isTransparent: true, focusable: true,
                      role: "neutral", onClick: bol_p.onClick,
                      narrationText: bol_p.label,
                      inputLegend: bol_p.label},
      bol_frame(null, [bol_div({
        height: "17.6rem", display: "flex", flexDirection: "column",
        justifyContent: "center", alignItems: "center"}, [
        bol_title("+" + bol_p.count, "3.6rem"),
        bol_line(bol_p.label, "1.7rem", bol_DIM, {marginTop: "0.6rem"})])]))]);
}

// A row's heading: a coloured block, the title, its hint, a rule.
function bol_heading(bol_title_, bol_hint, bol_big, bol_accent) {
  return bol_div({display: "flex", flexDirection: "row",
                  alignItems: "center", padding: "0.4rem 0.6rem 0.6rem"}, [
    bol_div({width: "0.8rem", height: bol_big ? "2.6rem" : "2.2rem",
             backgroundColor: bol_accent || bol_GRASS, flexShrink: 0,
             marginRight: "0.9rem", border: "0.2rem solid " + bol_INK}),
    bol_title(bol_title_, bol_big ? "2.6rem" : "2.2rem", {flexShrink: 0}),
    bol_hint ? bol_line(bol_hint, "1.5rem", bol_DIM,
                        {marginLeft: "1.2rem", flexShrink: 1})
             : null,
    bol_div({flexGrow: 1, height: "0.2rem", marginLeft: "1.2rem",
             backgroundColor: "rgba(255,255,255,0.16)"})]);
}

function bol_Row(bol_p) {
  var bol_h = __R__.createElement, bol_cards = bol_p.cards;
  var bol_limit = bol_p.limit, bol_kids = [];
  var bol_shown = bol_cards.length > bol_limit ? bol_limit - 1 : bol_limit;
  var bol_T = bol_p.partner ? bol_PartnerTile
            : bol_p.big ? bol_BigTile : bol_Tile;
  bol_cards.slice(0, bol_shown).forEach(function (bol_c) {
    bol_kids.push(bol_h(bol_T, {key: bol_c.k, card: bol_c}));
  });
  if (bol_cards.length > bol_shown)
    bol_kids.push(bol_h(bol_MoreTile, {
      key: "more", count: bol_cards.length - bol_shown,
      label: bol_limit <= bol_ROW ? bol_t("seeAll") : bol_t("showMore"),
      onClick: bol_p.onMore}));
  return bol_div({marginBottom: "1.8rem"}, [
    bol_p.title ? bol_heading(bol_p.title, bol_p.hint, bol_p.big,
                              bol_p.partner ? bol_GOLD : null)
                : null,
    bol_div({display: "flex", flexDirection: "row", flexWrap: "wrap"},
            bol_kids)]);
}

// --------------------------------------------------------------------------
// The spotlight.

function bol_Spotlight(bol_p) {
  var bol_h = __R__.createElement;
  var bol_c = bol_useSpot() || bol_p.fallback;
  if (!bol_c) return bol_div({height: "23rem", flexShrink: 0});
  // The focused card as the list stands now: favourite since, or removed.
  bol_c = bol_p.byKey[bol_c.k] || bol_c;
  var bol_s = bol_c.s, bol_meta = [];
  if (bol_s && bol_s.ln) bol_meta.push(bol_s.ln);
  if (bol_s && bol_s.t && bol_s.t.length)
    bol_meta.push(bol_s.t.slice(0, 3).join(", "));
  if (bol_s && bol_s.v) bol_meta.push(bol_t("version", [bol_s.v]));
  var bol_desc = bol_c.pt ? bol_c.pt.d.split("\n")[0]
    : bol_s ? (bol_s.d || bol_s.motd || "") : (bol_c.motd || "");
  var bol_addr = bol_s ? bol_s.a + (bol_s.p !== 19132 ? ":" + bol_s.p : "")
                       : "";
  var bol_buttons = [
    bol_div({width: "18rem", marginRight: "1rem", flexShrink: 0}, [
      bol_h(__BUTTON__, {variant: "hero", inputLegend: bol_t("play"),
                         onClick: function () { bol_play(bol_c); }},
            bol_t("play"))])];
  if (bol_c.kind !== "tp" && bol_c.kind !== "exp")
    bol_buttons.push(bol_div({width: "26rem", flexShrink: 0}, [
      bol_h(__BUTTON__, {variant: "secondary",
                         inputLegend: bol_c.fav ? bol_t("unfavorite")
                                                : bol_t("favorite"),
                         onClick: function () { bol_toggleFav(bol_c); }},
            bol_c.fav ? bol_t("unfavorite") : bol_t("favorite"))]));
  return bol_div({display: "flex", flexDirection: "row", height: "23rem",
                  flexShrink: 0, backgroundColor: "rgba(0,0,0,0.65)",
                  border: "0.2rem solid " + bol_INK,
                  borderBottom: "0.4rem solid " + bol_INK,
                  margin: "0 0.6rem 1rem"}, [
    bol_div({width: "38%", flexShrink: 0,
             borderRight: "0.2rem solid " + bol_INK}, [
      bol_h(bol_Art, {card: bol_c, height: "22.6rem", big: "4rem",
                      logoSize: "19rem"})]),
    bol_div({flexGrow: 1, flexBasis: 0, padding: "1.2rem 1.8rem",
             display: "flex", flexDirection: "column", overflow: "hidden"}, [
      bol_div({display: "flex", flexDirection: "row",
               alignItems: "center"}, [
        bol_c.wide && bol_c.logo
          ? bol_div({width: "4.6rem", height: "4.6rem", marginRight: "1rem",
                     flexShrink: 0}, [
              bol_h(__IMAGE__, {src: bol_c.logo, height: "100%",
                                width: "100%"})])
          : null,
        bol_name(bol_c, "3.2rem", {flexShrink: 1}),
        bol_c.pt ? bol_div({marginLeft: "1rem", flexShrink: 0}, [
          bol_partnerChip("1.4rem")]) : null]),
      bol_div({display: "flex", flexDirection: "row", alignItems: "center",
               marginTop: "0.5rem"}, [
        bol_known(bol_c) ? bol_div({marginRight: "1.6rem"}, [
          bol_status(bol_c, "1.7rem")]) : null,
        bol_line(bol_meta.join("  ·  "), "1.7rem", bol_DIM,
                 {flexShrink: 1})]),
      bol_text(bol_desc, {fontSize: "1.6rem", color: bol_DIM,
                          fontFamily: bol_SEVEN,
                          marginTop: "0.8rem", flexGrow: 1, flexBasis: 0,
                          overflow: "hidden"}),
      bol_div({display: "flex", flexDirection: "row", alignItems: "center",
               marginTop: "0.8rem"}, bol_buttons.concat([
        bol_line(bol_addr, "1.6rem", bol_DIM,
                 {marginLeft: "1.6rem", flexShrink: 1})]))])]);
}

// --------------------------------------------------------------------------
// The featured experiences: the servers the game's own online layout lists
// (its second Servers tab, when this build has it and the account gets
// one), in our big tiles, without its banner or its own lists. The game's
// featured servers in our tiles otherwise.

function bol_noLayout() { return null; }

// The experiences one layout block lists, as plain values.
function bol_expsOf(bol_v) {
  var bol_f = bol_v && bol_v.fab;
  var bol_list = bol_f && bol_f.pagedExperiences
    && bol_f.pagedExperiences.experiences;
  var bol_out = [];
  Array.from(bol_list || []).forEach(function (bol_e) {
    if (!bol_e || !bol_e.title) return;
    var bol_l = bol_e.listing || {};
    bol_out.push({
      eid: String(bol_e.experienceId || ""),
      lid: bol_e.linksTo && bol_e.linksTo.id ? String(bol_e.linksTo.id) : "",
      n: String(bol_e.title.value || ""),
      by: String(bol_e.creatorName || ""),
      url: bol_l.displayImage && bol_l.displayImage.url || "",
      motd: bol_l.motd && bol_l.motd.value || ""});
  });
  return bol_out;
}

// Reads one block and hands its experiences up; draws nothing.
function bol_Report(bol_p) {
  var bol_key = JSON.stringify(bol_p.list);
  __R__.useEffect(function () {
    bol_p.onList(bol_p.id, bol_p.list);
  }, [bol_key]);
  return null;
}
function bol_FabSource(bol_p) {
  var bol_h = __R__.createElement;
  var bol_q = __QUERY__("vanilla.menus.fabQuery", bol_p.id);
  var bol_list = __UFM__(bol_expsOf, [], [bol_q]);
  return bol_h(__UNWRAP__, {data: bol_list}, function (bol_l) {
    return bol_h(bol_Report, {id: bol_p.id, list: bol_l,
                              onList: bol_p.onList});
  });
}

// One experience: its picture, once the game has it on disk, in a big
// tile. A server of the game's featured list is joined in one press; any
// other opens the experience's own page.
function bol_ExpTile(bol_p) {
  var bol_h = __R__.createElement, bol_e = bol_p.exp;
  var bol_q = __QUERY__("vanilla.menus.imageQuery", bol_e.url);
  var bol_src = __UFM__(function (bol_v) {
    return bol_v && bol_v.localPath ? String(bol_v.localPath) : "";
  }, [], [bol_q]);
  return bol_h(__UNWRAP__, {data: bol_src}, function (bol_path) {
    var bol_tp = bol_p.tp[bol_fold(bol_e.n)];
    var bol_c = {k: "exp" + (bol_e.eid || bol_e.n), n: bol_e.n,
                 motd: bol_e.motd, by: bol_e.by, s: null, o: 0,
                 wide: bol_path, wh: 56.25, logo: "", fav: false};
    if (bol_tp) {
      bol_c.kind = "tp";
      bol_c.id = bol_tp.id;
      bol_c = bol_withDetails(bol_c);
      bol_c.wide = bol_path || bol_c.wide;
      bol_c.wh = bol_path ? 56.25 : bol_c.wh;
    } else {
      bol_c.kind = "exp";
      bol_c.route = bol_e.eid && bol_e.lid
        ? "/play/servers/experience/" + bol_e.eid + "/" + bol_e.lid : "";
    }
    return bol_h(bol_BigTile, {card: bol_c, third: true});
  });
}

function bol_ExpGrid(bol_p) {
  var bol_R = __R__, bol_h = bol_R.createElement;
  var bol_lists = bol_R.useState({});
  var bol_onList = bol_R.useCallback(function (bol_id, bol_list) {
    bol_lists[1](function (bol_old) {
      var bol_next = {};
      for (var bol_k in bol_old) bol_next[bol_k] = bol_old[bol_k];
      bol_next[bol_id] = bol_list;
      return bol_next;
    });
  }, []);
  var bol_seen = {}, bol_exps = [];
  bol_p.ids.forEach(function (bol_id) {
    (bol_lists[0][bol_id] || []).forEach(function (bol_e) {
      var bol_key = bol_fold(bol_e.n);
      if (!bol_key || bol_seen[bol_key]) return;
      bol_seen[bol_key] = 1;
      bol_exps.push(bol_e);
    });
  });
  var bol_kids = bol_p.ids.map(function (bol_id) {
    return bol_h(bol_FabSource, {key: "src" + bol_id, id: bol_id,
                                 onList: bol_onList});
  });
  bol_exps.forEach(function (bol_e) {
    bol_kids.push(bol_h(bol_ExpTile, {key: "exp" + (bol_e.eid || bol_e.n),
                                      exp: bol_e, tp: bol_p.tp}));
  });
  return bol_div({display: "flex", flexDirection: "row", flexWrap: "wrap"},
                 bol_kids);
}

function bol_Featured(bol_p) {
  var bol_h = __R__.createElement, bol_lay = bol_p.layout;
  var bol_ours = bol_h(bol_Row, bol_p.row);
  if (!bol_lay) return bol_ours;
  var bol_lst = __UFM__(function (bol_err, bol_busy, bol_ids) {
    return bol_err || bol_busy || !bol_ids || !bol_ids.length
      ? [] : Array.from(bol_ids).map(String);
  }, [], [bol_lay.hasError, bol_lay.isLoading, bol_lay.fabIds]);
  return bol_h(__UNWRAP__, {data: bol_lst}, function (bol_ids) {
    if (!bol_ids.length) return bol_ours;
    var bol_tp = {};
    bol_p.row.cards.forEach(function (bol_c) {
      if (bol_c.kind === "tp") bol_tp[bol_fold(bol_c.n)] = bol_c;
    });
    return bol_div({marginBottom: "1.8rem"}, [
      bol_heading(bol_p.row.title, bol_p.row.hint, true),
      bol_h(bol_ExpGrid, {ids: bol_ids, tp: bol_tp})]);
  });
}

// --------------------------------------------------------------------------
// A server's own page: its big picture beside its name, players and
// buttons, then its description, its activities and its other pictures.
// A partner's come from the launcher, a catalogue server's from the list,
// a featured server's from the details the game loads for it.

function bol_section(bol_title_, bol_kids) {
  return bol_div({marginTop: "1.6rem", flexShrink: 0}, [
    bol_heading(bol_title_, "", false),
    bol_div({display: "flex", flexDirection: "row", flexWrap: "wrap"},
            bol_kids)]);
}
// A description's first lines: some servers paste their whole forum post.
// A line as the game's font can draw it: descriptions are pasted from
// Discord, with its markdown and emoji the font has no glyph for.
function bol_plain(bol_l) {
  return String(bol_l)
    .replace(/[\uD800-\uDBFF][\uDC00-\uDFFF]/g, "")
    .replace(/[\u2190-\u21FF\u2300-\u27BF\u2B00-\u2BFF\uFE0F\u200D]/g, "")
    .replace(/<a?:\w+:\d+>/g, "")
    .replace(/\*\*|__|~~|`/g, "")
    .replace(/^\s*(?:(?:>+|[-*\u2022](?=\s))\s*)+/, "")
    .replace(/^[\s\-|:\u2013\u2014]+$/, "")
    .replace(/\s{2,}/g, " ")
    .trim();
}
function bol_paragraphs(bol_value) {
  var bol_lines = String(bol_value || "").split("\n").map(bol_plain)
    .filter(function (bol_l) { return bol_l; });
  if (bol_lines.length > 8) bol_lines = bol_lines.slice(0, 8).concat(["\u2026"]);
  return bol_lines.map(function (bol_l) {
    return bol_text(bol_l, {fontFamily: bol_SEVEN, fontSize: "1.7rem",
                            color: "#e6e6e6", marginBottom: "0.8rem"});
  });
}
// Something to look at that the controller can still reach: focusable,
// read out, pressing it does nothing.
function bol_still(bol_label, bol_width, bol_kids) {
  return bol_div({width: bol_width, padding: "0.6rem"}, [
    __R__.createElement.apply(null, [__PANEL__, {
      compact: true, isTransparent: true, focusable: true, role: "neutral",
      narrationText: bol_label}].concat(bol_kids))]);
}
function bol_Activity(bol_p) {
  var bol_a = bol_p.act, bol_h = __R__.createElement;
  return bol_still(bol_a.t, "25%", [bol_frame(
    bol_a.img ? bol_div({height: "14rem", overflow: "hidden",
                         display: "flex", flexDirection: "column",
                         justifyContent: "center",
                         backgroundColor: bol_INK}, [
      bol_h(__IMAGE__, {src: bol_a.img, paddingTopPercentage: 100})])
              : null,
    [bol_div({padding: "0.8rem 1rem 1rem"}, [
      bol_title(bol_a.t, "2rem"),
      bol_a.st ? bol_line(bol_a.st, "1.5rem", bol_GOLD,
                          {marginTop: "0.3rem"}) : null,
      bol_text(bol_a.d, {fontFamily: bol_SEVEN, fontSize: "1.4rem",
                         color: bol_DIM, marginTop: "0.5rem",
                         height: "8.4rem", overflow: "hidden"})])])]);
}
function bol_Picture(bol_p) {
  return bol_still(bol_p.label, "50%", [bol_div({
    border: "0.2rem solid " + bol_INK, borderBottom: "0.4rem solid "
      + bol_INK, backgroundColor: bol_INK}, [
    __R__.createElement(__IMAGE__, {src: bol_p.pic[0],
                                    paddingTopPercentage: bol_p.pic[1]})])]);
}

function bol_Details(bol_p) {
  var bol_R = __R__, bol_h = bol_R.createElement;
  var bol_rootRef = bol_R.useRef(null);
  var bol_c = bol_p.card, bol_pt = bol_c.pt, bol_s = bol_c.s;
  var bol_det = bol_p.det;
  var bol_has = bol_det && bol_det.ok && bol_c.id != null
    && bol_det.id === String(bol_c.id) ? bol_det : null;
  // The details of a server the game knows (its featured ones, the
  // player's own), loaded again if something else loaded meanwhile.
  bol_R.useEffect(function () {
    if (bol_has || bol_c.id == null || !bol_ctx.load) return undefined;
    var bol_kd = bol_c.kind === "tp" ? __KD__.ThirdParty : __KD__.External;
    bol_ctx.load(String(bol_c.id), bol_kd);
    var bol_timer = setTimeout(function () {
      bol_ctx.load(String(bol_c.id), bol_kd);
    }, 900);
    return function () { clearTimeout(bol_timer); };
  }, [bol_has ? 1 : 0, bol_c.k]);
  if (bol_has && bol_c.kind === "tp")
    bol_c = bol_merge(bol_merge({}, bol_c), {
      n: bol_c.n || bol_has.name,
      on: bol_has.ping > 0 && bol_has.on >= 0 ? bol_has.on : bol_c.on});

  var bol_pics = [];
  function bol_pic(bol_src, bol_ratio) {
    if (bol_src && !bol_pics.some(function (bol_x) {
      return bol_x[0] === bol_src;
    })) bol_pics.push([bol_src, bol_ratio || 56.25]);
  }
  if (bol_pt) bol_pic(bol_pt.bn, bol_pt.bh);
  if (bol_c.wide) bol_pic(bol_c.wide, bol_c.wh);
  if (bol_has && bol_has.img) bol_pic(bol_has.img, 30);
  (bol_s && bol_s.gl || []).forEach(function (bol_g) {
    bol_pic(bol_g[0], bol_g[1]);
  });
  if (bol_c.banner) bol_pic(bol_c.banner, bol_c.ih);
  var bol_hero = bol_pics[0];
  var bol_desc = bol_pt ? bol_pt.d
    : bol_s && bol_s.d ? bol_s.d
    : bol_has && bol_has.desc ? bol_has.desc : (bol_c.motd || "");
  var bol_acts = bol_pt ? bol_pt.acts : bol_has ? bol_has.acts : [];
  var bol_news = bol_pt ? bol_pt.news : bol_has ? bol_has.news : "";

  // Two rows of chips, each kept on its line: the game's layout does not
  // grow a box for a wrapped flex line, which then covers what follows.
  var bol_chips = [], bol_tags = [];
  if (bol_pt) bol_chips.push(bol_partnerChip("1.4rem"));
  if (bol_s && bol_s.ln) bol_chips.push(bol_chip(bol_s.ln));
  if (bol_s && bol_s.v) bol_chips.push(bol_chip(bol_t("version", [bol_s.v])));
  (bol_s && bol_s.t || []).slice(0, 4).forEach(function (bol_x) {
    bol_tags.push(bol_chip(bol_x));
  });
  var bol_addr = bol_s && bol_s.a
    ? bol_s.a + (bol_s.p && bol_s.p !== 19132 ? ":" + bol_s.p : "") : "";
  var bol_on = bol_online(bol_c), bol_mx = bol_s ? bol_s.mx : bol_c.mx;
  if (bol_has && bol_c.kind === "tp") bol_mx = bol_has.mx;

  var bol_buttons = [bol_div({marginTop: "1rem"}, [
    bol_h(__BUTTON__, {variant: "hero", inputLegend: bol_t("play"),
                       onClick: function () { bol_play(bol_c); }},
          bol_t("play"))])];
  var bol_second = [];
  if (bol_c.kind !== "tp")
    bol_second.push(bol_div({flexGrow: 1, flexBasis: 0, marginRight: "1rem"},
      [bol_h(__BUTTON__, {variant: "secondary",
                          inputLegend: bol_c.fav ? bol_t("unfavorite")
                                                 : bol_t("favorite"),
                          onClick: function () { bol_toggleFav(bol_c); }},
             bol_c.fav ? bol_t("unfavorite") : bol_t("favorite"))]));
  bol_second.push(bol_div({flexGrow: bol_second.length ? 0 : 1,
                           width: bol_second.length ? "16rem" : "auto",
                           flexShrink: 0}, [
    bol_h(__BUTTON__, {variant: "secondary", inputLegend: bol_t("back"),
                       onClick: function () { bol_ctx.router.goBack(); }},
          bol_t("back"))]));
  bol_buttons.push(bol_div({display: "flex", flexDirection: "row",
                            marginTop: "0.8rem"}, bol_second));

  var bol_top = bol_div({display: "flex", flexDirection: "row",
                         flexShrink: 0, margin: "0 0.6rem"}, [
    bol_div({width: "58%", flexShrink: 0, display: "flex",
             flexDirection: "column", justifyContent: "center",
             border: "0.2rem solid " + bol_INK,
             borderBottom: "0.4rem solid " + bol_INK,
             backgroundColor: bol_INK}, [
      bol_hero ? bol_h(__IMAGE__, {src: bol_hero[0],
                                   paddingTopPercentage: bol_hero[1]})
               : bol_h(bol_Art, {card: bol_c, height: "24rem",
                                 logoSize: "20rem", big: "4.6rem",
                                 noWide: true})]),
    bol_div({flexGrow: 1, flexBasis: 0, marginLeft: "1.2rem",
             display: "flex", flexDirection: "column",
             border: "0.2rem solid " + bol_INK}, [bol_stone([
      bol_div({display: "flex", flexDirection: "row",
               alignItems: "center"}, [
        bol_c.logo ? bol_div({width: "7rem", height: "7rem", flexShrink: 0,
                              marginRight: "1.2rem",
                              border: "0.2rem solid " + bol_INK,
                              backgroundColor: "rgba(0,0,0,0.4)"}, [
          bol_h(__IMAGE__, {src: bol_c.logo, height: "100%",
                            width: "100%"})]) : null,
        bol_name(bol_c, "3.4rem", {flexGrow: 1, flexBasis: 0})]),
      bol_chips.length
        ? bol_div({display: "flex", flexDirection: "row", overflow: "hidden",
                   marginTop: "0.8rem"}, bol_chips) : null,
      bol_tags.length
        ? bol_div({display: "flex", flexDirection: "row", overflow: "hidden",
                   marginTop: "0.5rem"}, bol_tags) : null,
      !bol_known(bol_c) ? null :
      bol_div({display: "flex", flexDirection: "row", alignItems: "center",
               marginTop: "0.8rem"}, [
        bol_status(bol_c, "1.8rem"),
        bol_on != null && bol_mx > 0
          ? bol_line(bol_t("players", [bol_on, bol_mx]), "1.6rem", bol_DIM,
                     {marginLeft: "1.2rem"}) : null]),
      bol_pt && bol_pt.msg || bol_c.motd
        ? bol_line(bol_pt ? bol_pt.msg : bol_c.motd, "1.6rem", bol_DIM,
                   {marginTop: "0.5rem"}) : null,
      bol_div({flexGrow: 1})].concat(bol_buttons).concat([
      bol_p.msg ? bol_line(bol_p.msg, "1.6rem", bol_GREEN,
                           {marginTop: "0.6rem"}) : null,
      bol_addr ? bol_line(bol_t("address", [bol_addr]), "1.5rem", bol_DIM,
                          {marginTop: "0.6rem"}) : null]),
      {padding: "1.2rem 1.4rem 1rem"})])]);

  var bol_body = [bol_top];
  if (bol_acts.length)
    bol_body.push(bol_section(bol_t("activities"),
      bol_acts.map(function (bol_a, bol_i) {
        return bol_h(bol_Activity, {key: "a" + bol_i, act: bol_a});
      })));
  var bol_more = bol_pics.slice(1);
  if (bol_more.length)
    bol_body.push(bol_section(bol_t("pictures"),
      bol_more.map(function (bol_x, bol_i) {
        return bol_h(bol_Picture, {key: "p" + bol_i, pic: bol_x,
                                   label: bol_c.n + " " + (bol_i + 1)});
      })));
  var bol_about = bol_paragraphs(bol_desc);
  if (bol_news) bol_about.push(bol_text(bol_news, {
    fontFamily: bol_SEVEN, fontSize: "1.6rem", color: bol_GOLD,
    marginTop: "0.4rem"}));
  var bol_links = [];
  if (bol_s && bol_s.w) bol_links.push(bol_t("website", [bol_s.w]));
  if (bol_s && bol_s.dc) bol_links.push(bol_t("discord", [bol_s.dc]));
  bol_links.forEach(function (bol_l) {
    bol_about.push(bol_line(bol_l, "1.6rem", bol_DIM,
                            {marginTop: "0.4rem"}));
  });
  if (bol_about.length)
    bol_body.push(bol_section(bol_t("about"), [
      bol_div({padding: "0.4rem 0.6rem", width: "100%"}, bol_about)]));
  bol_body.push(bol_div({height: "2rem", flexShrink: 0}));
  return bol_h("div", {ref: bol_rootRef,
                       style: {display: "flex", flexDirection: "column",
                               height: "100%", padding: "1rem 2rem 0"}},
    bol_h(__FOCUS__, {containerRef: bol_rootRef,
                      delegateFocusFromMemory: true},
      bol_div({flexGrow: 1, flexShrink: 1, flexBasis: 0,
               display: "flex", flexDirection: "column"}, [
        bol_h.apply(null, [__SCROLL__, {loading: false}].concat(bol_body))])));
}

// --------------------------------------------------------------------------
// The page.

function bol_snapshot(bol_x, bol_tp) {
  var bol_ext = Array.from(bol_x.externalServerWorlds);
  var bol_partners = [];
  if (typeof __bolPromoted === "function") {
    bol_partners = __bolPromoted(bol_x).map(function (bol_e) {
      return {id: bol_e.s.id, name: bol_e.p.label, motd: bol_e.p.message,
              image: bol_e.p.icon, banner: bol_e.p.banner};
    });
    if (typeof __bolRest === "function") bol_ext = __bolRest(bol_ext);
  }
  function bol_exp(bol_list) {
    return Array.from(bol_list || []).map(function (bol_e) {
      return {id: bol_e.id, name: bol_e.name, motd: bol_e.msgOfTheDay,
              image: bol_e.image};
    });
  }
  return {
    favs: bol_ext.map(function (bol_e) {
      return {id: bol_e.id, name: bol_e.name, motd: bol_e.msgOfTheDay};
    }),
    partners: bol_partners,
    featured: bol_exp(bol_tp.featuredExperiences),
    creators: bol_exp(bol_tp.creatorExperiences)
  };
}

function bol_Hub(bol_p) {
  var bol_ext = __USF__(__EXT__), bol_tp = __USF__(__TP__);
  var bol_snap = __UFM__(function (bol_x, bol_y) {
    return bol_snapshot(bol_x, bol_y);
  }, [], [bol_ext, bol_tp]);
  // Writes go through the live list object, as the game's own form does:
  // it answers null on success and an error code otherwise.
  var bol_act = __UFC__(function (bol_x) {
    return function (bol_op, bol_s, bol_id) {
      if (bol_op === "remove")
        return {error: bol_x.removeExternalServerWorld(parseInt(bol_id))};
      var bol_err = bol_x.addExternalServerWorld(bol_s.n, bol_s.a, bol_s.p);
      return {error: bol_err, id: bol_x.addedServerId};
    };
  }, [], [bol_ext]);
  var bol_det = __USF__(__DET__);
  bol_ctx.load = __UFC__(function (bol_v) {
    return function (bol_id, bol_kind) {
      bol_v.loadNetworkWorldDetails(String(bol_id), bol_kind);
    };
  }, [], [bol_det]);
  var bol_joiner = __USF__(__JOINER__), bol_opts = __USF__(__NOTIF__);
  bol_ctx.direct = __UFC__(function (bol_j, bol_o) {
    return function (bol_kind, bol_id) {
      var bol_ext = bol_kind === __KD__.External;
      if (bol_ext ? !bol_o.doNotShowMultiplayerIpSafetyWarning
                  : !bol_o.doNotShowMultiplayerOnlineSafetyWarning)
        return false;
      if (bol_ext) bol_j.joinExternalServer(String(bol_id));
      else bol_j.joinThirdPartyServer(String(bol_id));
      return true;
    };
  }, [], [bol_joiner, bol_opts]);
  bol_ctx.act = bol_act;
  bol_ctx.router = __ROUTER__();
  // What the details hold: for the featured servers' walk, and for the
  // page of a server whose details the game has.
  var bol_detNow = __UFM__(function (bol_v) {
    var bol_n = bol_v.networkDetails || {};
    return {ok: !!bol_v.hasLoadedDetails,
            id: bol_n.id == null ? "" : String(bol_n.id),
            img: bol_n.imagePath || "", on: bol_n.playerCount,
            mx: bol_n.capacity, ping: bol_n.pingStatus || 0,
            name: String(bol_n.name || bol_n.title || ""),
            desc: String(bol_n.description || ""),
            news: String(bol_n.newsDescription || ""),
            acts: Array.from(bol_n.activities || []).map(function (bol_a) {
              return {t: String(bol_a.title || ""),
                      st: String(bol_a.subtitle || ""),
                      d: String(bol_a.description || ""),
                      img: String(bol_a.imagePath || "")};
            })};
  }, [], [bol_det]);
  // The game's featured-experience layout, when this build has one.
  var bol_layout = __FABS__(__LAYOUT__);
  // The location's hash, which names the server whose page is open.
  var bol_hash = __HASH__();
  bol_ctx.hash = !!bol_hash;
  bol_ctx.prefix = bol_p.routePrefix || "/play";
  var bol_route = __R__.useState("");
  var bol_onRoute = __R__.useCallback(function (bol_id, bol_key) {
    bol_route[1](bol_key);
  }, []);
  var bol_h = __R__.createElement;
  return bol_div({display: "flex", flexDirection: "column",
                  height: "100%"}, [
    bol_hash ? bol_h(bol_RouteSource, {facet: bol_hash,
                                       onRoute: bol_onRoute})
               : null,
    bol_h(__UNWRAP__, {data: bol_snap}, function (bol_s) {
      return bol_h(__UNWRAP__, {data: bol_detNow}, function (bol_d) {
        return bol_h(bol_HubView, {snap: bol_s, det: bol_d,
                                   layout: bol_layout, route: bol_route[0],
                                   prefix: bol_p.routePrefix});
      });
    })]);
}

// Hands the hash's card key up, "" on the tab itself. Draws nothing; while
// the screen is not the active one the hash has no value and the page
// keeps the one it had.
function bol_RouteSource(bol_p) {
  var bol_h = __R__.createElement;
  var bol_key = __UFM__(function (bol_v) {
    var bol_k = String(bol_v || "").replace(/^#/, "");
    try {
      return decodeURIComponent(bol_k);
    } catch (bol_e) {
      return bol_k;
    }
  }, [], [bol_p.facet]);
  return bol_h(__UNWRAP__, {data: bol_key}, function (bol_k) {
    return bol_h(bol_Report, {id: "route", list: bol_k,
                              onList: bol_p.onRoute});
  });
}

function bol_langChoices() {
  var bol_d = bol_data(), bol_out = [""];
  (bol_d && bol_d.langs || []).slice(0, 8).forEach(function (bol_l) {
    bol_out.push(bol_l[0]);
  });
  return bol_out;
}
// The game modes to pick from: the commonest categories.
function bol_modeChoices() {
  var bol_d = bol_data(), bol_out = [""];
  (bol_d && bol_d.cats || []).slice(0, 12).forEach(function (bol_c) {
    bol_out.push(bol_c[0]);
  });
  return bol_out;
}
// A language's name, alone ("French") or inside a sentence ("français").
function bol_langName(bol_code, bol_inSentence) {
  if (!bol_code) return bol_t("allLanguages");
  var bol_d = bol_data(), bol_l = (bol_d && bol_d.langs || []).filter(
    function (bol_x) { return bol_x[0] === bol_code; })[0];
  if (!bol_l) return bol_code.toUpperCase();
  return bol_inSentence && bol_l[3] ? bol_l[3] : bol_l[2];
}

// The rows, from the cards and the search, language and mode filters.
function bol_rowsFor(bol_all, bol_ws, bol_lg, bol_md) {
  var bol_d = bol_data() || {}, bol_out = [];
  function bol_row(bol_key, bol_title, bol_cards, bol_hint, bol_big) {
    if (bol_cards.length)
      bol_out.push({key: bol_key, title: bol_title, cards: bol_cards,
                    hint: bol_hint || "", big: !!bol_big});
  }
  var bol_inLang = function (bol_c) {
    return (!bol_lg || (bol_c.s && bol_c.s.l === bol_lg))
      && (!bol_md || (bol_c.s && (bol_c.s.t || []).indexOf(bol_md) >= 0));
  };
  var bol_cat = bol_all.cat.filter(bol_inLang);
  var bol_any = !!(bol_lg || bol_md);
  if (bol_ws.length) {
    var bol_hits = [];
    bol_all.mine.concat(bol_all.featured).forEach(function (bol_c) {
      if (!bol_c.s && bol_nameMatches(bol_c.n, bol_ws))
        bol_hits.push({c: bol_c, sc: 9});
    });
    // A partner the catalogue lacks; the catalogue's tile stands for any
    // other.
    bol_all.partners.forEach(function (bol_c) {
      if (bol_c.s.i === bol_c.k && bol_score(bol_c.s, bol_ws) > 0)
        bol_hits.push({c: bol_c, sc: 9});
    });
    bol_cat.forEach(function (bol_c) {
      var bol_sc = bol_score(bol_c.s, bol_ws);
      if (bol_sc > 0) bol_hits.push({c: bol_c, sc: bol_sc});
    });
    // Relevance first, but a server that is down never above one that is up.
    bol_hits.sort(function (bol_a, bol_b) {
      var bol_da = bol_a.c.s && bol_a.c.s.off ? 1 : 0;
      var bol_db = bol_b.c.s && bol_b.c.s.off ? 1 : 0;
      return bol_da - bol_db || bol_b.sc - bol_a.sc
        || bol_byPlayers(bol_a.c, bol_b.c);
    });
    var bol_found = bol_hits.map(function (bol_x) { return bol_x.c; });
    bol_row("results", bol_t("results"), bol_found,
            bol_t("count", [bol_found.length]));
    return bol_out;
  }
  // Every server, the ones that answered the ping first: many networks'
  // proxies never answer it (Linesia among them), and they are no less
  // played for that. Only the overall "most played" row keeps to those
  // whose players it could count.
  var bol_ranked = bol_cat.slice().sort(bol_byPlayers);
  var bol_live = bol_ranked.filter(function (bol_c) { return !bol_c.s.off; });
  bol_row("mine", bol_t("myServers"), bol_all.mine.filter(bol_inLang),
          bol_t("myServersHint"));
  if (!bol_any && bol_all.partners.length)
    bol_out.push({key: "partners", title: bol_t("partners"),
                  cards: bol_all.partners, hint: bol_t("partnersHint"),
                  big: true, partner: true});
  if (!bol_any)
    bol_row("featured", bol_t("featured"), bol_all.featured,
            bol_t("featuredHint"), true);
  var bol_ui = bol_d.ui || "en";
  if (!bol_any && bol_ui !== "en")
    bol_row("local", bol_t("popularIn", [bol_langName(bol_ui, true)]),
            bol_ranked.filter(function (bol_c) {
              return bol_c.s.l === bol_ui;
            }));
  if (bol_any) {
    var bol_title_ = bol_lg ? bol_t("popularIn", [bol_langName(bol_lg, true)])
                            : bol_t("popular");
    if (bol_md) bol_title_ += " \u00b7 " + bol_md;
    bol_row("popular", bol_title_, bol_ranked,
            bol_t("count", [bol_ranked.length]));
    return bol_out;
  }
  bol_row("popular", bol_t("popular"), bol_live,
          bol_d.pinged ? bol_t("pingedHint") : "");
  (bol_d.cats || []).slice(0, 8).forEach(function (bol_cg) {
    bol_row("cat-" + bol_cg[0], bol_cg[0], bol_ranked.filter(function (bol_c) {
      return (bol_c.s.t || []).indexOf(bol_cg[0]) >= 0;
    }));
  });
  bol_row("all", bol_t("everyServer"), bol_ranked,
          bol_t("count", [bol_ranked.length]));
  return bol_out;
}

function bol_HubView(bol_p) {
  var bol_R = __R__, bol_h = bol_R.createElement;
  var bol_rootRef = bol_R.useRef(null);
  var bol_query = bol_R.useState(bol_state.query);
  var bol_lang = bol_R.useState(bol_state.lang);
  var bol_mode = bol_R.useState(bol_state.mode);
  var bol_open = bol_R.useState(bol_state.open);
  var bol_msg = bol_R.useState("");
  var bol_field = __UFS__(bol_state.query);
  bol_ctx.toast = bol_msg[1];
  bol_R.useEffect(function () {
    if (!bol_msg[0]) return undefined;
    var bol_timer = setTimeout(function () { bol_msg[1](""); }, 4000);
    return function () { clearTimeout(bol_timer); };
  }, [bol_msg[0]]);
  var bol_onType = bol_R.useCallback(function (bol_v) {
    bol_field[1](bol_v);
    bol_state.query = String(bol_v == null ? "" : bol_v);
    bol_query[1](bol_state.query);
  }, []);
  var bol_base = bol_R.useMemo(function () { return bol_cards(bol_p.snap); },
                               [bol_p.snap]);
  var bol_tick = bol_R.useState(0);
  var bol_featIds = bol_base.featured.filter(function (bol_c) {
    return bol_c.kind === "tp";
  }).map(function (bol_c) { return String(bol_c.id); });
  // The page of the server the route names, over the hub kept as it was.
  var bol_page = bol_R.useRef(null);
  bol_R.useEffect(function () {
    if (bol_p.route) return undefined;
    var bol_before = JSON.stringify(bol_feat.info);
    var bol_wait = bol_featStep(bol_p.det, bol_featIds, Date.now());
    if (JSON.stringify(bol_feat.info) !== bol_before)
      bol_tick[1](function (bol_n) { return bol_n + 1; });
    if (!bol_wait) return undefined;
    var bol_timer = setTimeout(function () {
      bol_tick[1](function (bol_n) { return bol_n + 1; });
    }, bol_wait);
    return function () { clearTimeout(bol_timer); };
  }, [bol_p.det, bol_featIds.join(","), bol_tick[0], bol_p.route]);
  var bol_all = bol_R.useMemo(function () {
    return {cat: bol_base.cat, mine: bol_base.mine,
            partners: bol_base.partners,
            featured: bol_base.featured.map(bol_withDetails)};
  }, [bol_base, bol_tick[0]]);
  var bol_shown = bol_p.route ? bol_findCard(bol_all, bol_p.route) : null;
  // A server removed from the player's list keeps its page open.
  if (bol_p.route && !bol_shown && bol_page.current
      && bol_page.current.route === bol_p.route)
    bol_shown = bol_page.current.card;
  bol_page.current = bol_shown ? {route: bol_p.route, card: bol_shown}
                               : null;
  var bol_ws = bol_words(bol_query[0]);
  var bol_rows = bol_rowsFor(bol_all, bol_ws, bol_lang[0], bol_mode[0]).map(
    function (bol_r) {
      var bol_limit = bol_open[0][bol_r.key] || bol_ROW;
      var bol_props = {
        key: bol_r.key, title: bol_r.title, hint: bol_r.hint,
        cards: bol_r.cards, big: bol_r.big, partner: bol_r.partner,
        limit: bol_r.big ? bol_r.cards.length : bol_limit,
        onMore: function () {
          var bol_next = {};
          for (var bol_k in bol_state.open)
            bol_next[bol_k] = bol_state.open[bol_k];
          bol_next[bol_r.key] = bol_limit <= bol_ROW ? bol_PAGE
                                                     : bol_limit + bol_PAGE;
          bol_state.open = bol_next;
          bol_open[1](bol_next);
        }};
      return bol_r.key === "featured"
        ? bol_h(bol_Featured, {key: "featured", row: bol_props,
                               layout: bol_p.layout})
        : bol_h(bol_Row, bol_props);
    });
  if (!bol_rows.length)
    bol_rows.push(bol_text(bol_ws.length ? bol_t("noMatch")
                                         : bol_t("listUnavailable"),
                           {fontSize: "2rem", color: bol_DIM,
                            padding: "2rem 0.6rem"}));

  var bol_byKey = bol_R.useMemo(function () {
    var bol_m = {};
    bol_all.cat.concat(bol_all.mine, bol_all.featured,
                       bol_all.partners).forEach(
      function (bol_c) { bol_m[bol_c.k] = bol_c; });
    return bol_m;
  }, [bol_all]);
  var bol_fallback = bol_all.partners[0] || bol_all.featured[0]
    || bol_all.mine[0]
    || bol_all.cat.slice().sort(bol_byPlayers)[0] || null;
  var bol_langs = bol_langChoices();
  var bol_cycleLang = function () {
    var bol_i = bol_langs.indexOf(bol_lang[0]);
    bol_state.lang = bol_langs[(bol_i + 1) % bol_langs.length];
    bol_lang[1](bol_state.lang);
  };
  var bol_modes = bol_modeChoices();
  var bol_cycleMode = function () {
    var bol_i = bol_modes.indexOf(bol_mode[0]);
    bol_state.mode = bol_modes[(bol_i + 1) % bol_modes.length];
    bol_mode[1](bol_state.mode);
  };
  var bol_toolbar = bol_div({display: "flex", flexDirection: "row",
                             alignItems: "flex-end", flexShrink: 0,
                             margin: "0 0.6rem 1.2rem"}, [
    bol_div({flexGrow: 2, flexBasis: 0}, [
      bol_h(__TEXTFIELD__, {label: bol_t("search"), value: bol_field[0],
                            placeholder: bol_t("searchPlaceholder"),
                            onChange: bol_onType})]),
    bol_div({width: "24rem", marginLeft: "1rem"}, [
      bol_h(__BUTTON__, {variant: bol_lang[0] ? "primary" : "secondary",
                         inputLegend: bol_t("language"),
                         onClick: bol_cycleLang},
            bol_langName(bol_lang[0]))]),
    bol_div({width: "24rem", marginLeft: "1rem"}, [
      bol_h(__BUTTON__, {variant: bol_mode[0] ? "primary" : "secondary",
                         inputLegend: bol_t("gameMode"),
                         onClick: bol_cycleMode},
            bol_mode[0] || bol_t("allModes"))]),
    bol_div({width: "28rem", marginLeft: "1rem"}, [
      bol_h(__BUTTON__, {variant: "secondary", Icon: __ICONS__.Plus,
                         inputLegend: bol_t("addServer"),
                         onClick: function () {
                           bol_ctx.router.push((bol_p.prefix || "/play")
                                               + "/servers/add");
                         }}, bol_t("addServer"))])]);
  var bol_hub = bol_h("div", {ref: bol_rootRef,
                       style: {display: "flex",
                               visibility: bol_shown ? "hidden" : "visible",
                               flexDirection: "column",
                               height: "100%", padding: "1rem 2rem 0"}},
    bol_h(__FOCUS__, {containerRef: bol_rootRef, hidden: !!bol_shown,
                      disabled: !!bol_shown,
                      delegateFocusFromMemory: true},
      bol_shown ? null : bol_h(__LEGEND__, {options: {button: __KEYS__.Y,
                                   inputLegend: "\u2605 " + bol_t("myServers")},
                         onClick: function () { bol_toggleFav(bol_spot.v); }}),
      // One scroll for the whole page, the spotlight and search included:
      // fixed above the rows they left a 16:9 window no room for them.
      bol_div({flexGrow: 1, flexShrink: 1, flexBasis: 0,
               display: "flex", flexDirection: "column"}, [
        bol_h.apply(null, [__SCROLL__, {loading: false},
          bol_h(bol_Spotlight, {fallback: bol_fallback, byKey: bol_byKey}),
          bol_toolbar,
          bol_msg[0] ? bol_line(bol_msg[0], "1.7rem", bol_GREEN,
                                {margin: "0 0.6rem 0.8rem", flexShrink: 0})
                     : null].concat(bol_rows)
                    .concat([bol_div({height: "2rem"})]))])));
  // The hub stays laid out under the page, hidden: Back finds it scrolled
  // where it was.
  return bol_div({display: "flex", flexDirection: "column",
                  height: "100%", position: "relative"}, [
    bol_hub,
    bol_shown ? bol_div({position: "absolute", top: 0, left: 0, right: 0,
                         bottom: 0}, [
      bol_h(bol_Details, {key: bol_shown.k, card: bol_byKey[bol_shown.k]
                            || bol_shown, det: bol_p.det,
                          msg: bol_msg[0]})]) : null]);
}
