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
// tile that has focus, a search bar, the player's own servers, the featured
// experiences (partners and the game's featured servers, in big tiles), then
// rows of tiles (the most played ones, one row per category, then
// everything). Pressing a tile joins it: a catalogue server is first added
// to the player's own list, since the game only joins servers it knows,
// then joined the way the game's own details pane does.

var bol_state = {query: "", lang: "", open: {}};
var bol_ROW = 8;
var bol_PAGE = 24;
var bol_ctx = {act: null, load: null, direct: null, router: null,
               toast: null};

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
function bol_cards(bol_snap) {
  var bol_byName = bol_favIndex(bol_snap.favs), bol_catById = {};
  var bol_cat = bol_servers().map(function (bol_s, bol_i) {
    var bol_id = bol_favIdOf(bol_s, bol_byName);
    var bol_c = {k: bol_s.i, kind: bol_id != null ? "mine" : "cat",
                 id: bol_id, n: bol_s.n, s: bol_s, o: bol_i,
                 wide: bol_s.cv || "", wh: bol_s.ch || 56.25,
                 logo: bol_s.lg || "", banner: bol_s.img || "",
                 ih: bol_s.ih || 12.8, fav: bol_id != null};
    if (bol_id != null) bol_catById[bol_id] = bol_c;
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
          featured: bol_game(bol_snap.partners, "mine")
            .concat(bol_game(bol_snap.featured, "tp"))
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
  var bol_id = bol_c.id;
  if (bol_id == null && bol_c.s) bol_id = bol_add(bol_c.s);
  if (bol_id != null) bol_join(__KD__.External, bol_id);
}
function bol_toggleFav(bol_c) {
  if (!bol_c || !bol_ctx.act || bol_c.kind === "tp") return;
  if (bol_c.id != null) {
    var bol_r = bol_ctx.act("remove", null, bol_c.id);
    bol_say(bol_r && bol_r.error != null ? bol_t("removeFailed")
                                         : bol_t("removed", [bol_c.n]));
  } else if (bol_c.s && bol_add(bol_c.s) != null) {
    bol_say(bol_t("added", [bol_c.n]));
  }
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
// Looks.

var bol_WHITE = "#ffffff", bol_DIM = "#b1b2b5", bol_GREEN = "#6cc349";
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
function bol_div(bol_style, bol_kids) {
  return __R__.createElement.apply(null,
    ["div", {style: bol_style}].concat(bol_kids || []));
}
function bol_text(bol_value, bol_style) {
  return __R__.createElement("div", {style: bol_style}, bol_value);
}
function bol_line(bol_value, bol_size, bol_color, bol_extra) {
  var bol_st = {fontSize: bol_size, color: bol_color, whiteSpace: "nowrap",
                overflow: "hidden", textOverflow: "ellipsis"};
  for (var bol_k in bol_extra || {}) bol_st[bol_k] = bol_extra[bol_k];
  return bol_text(bol_value, bol_st);
}
// "● 1.2k online", "Offline", or the categories.
function bol_status(bol_c, bol_size) {
  var bol_s = bol_c.s;
  var bol_on = bol_s ? bol_s.on : bol_c.on;
  if (bol_on != null) {
    return bol_div({display: "flex", flexDirection: "row",
                    alignItems: "center", flexShrink: 0}, [
      bol_div({width: "0.9rem", height: "0.9rem", borderRadius: "50%",
               backgroundColor: bol_GREEN, marginRight: "0.7rem",
               flexShrink: 0}),
      bol_line(bol_t("online", [bol_compact(bol_on)]), bol_size,
               bol_GREEN)]);
  }
  if (bol_s && bol_s.off) return bol_line(bol_t("offline"), bol_size, bol_DIM);
  var bol_bits = bol_s ? (bol_s.t || []).slice(0, 2) : [];
  if (bol_s && bol_s.l) bol_bits.push(bol_s.l.toUpperCase());
  return bol_line(bol_bits.length ? bol_bits.join(" · ")
                  : (bol_c.motd || bol_t("server")), bol_size, bol_DIM);
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
                       textAlign: "center", padding: "0 1rem",
                       overflow: "hidden", maxHeight: "100%"})]);
}
// A logo badge over a cover: the server's mark stays readable.
function bol_Badge(bol_p) {
  var bol_c = bol_p.card;
  if (!bol_c.wide || !bol_c.logo) return null;
  return bol_div({position: "absolute", left: "1rem", bottom: "1rem",
                  width: bol_p.size, height: bol_p.size,
                  backgroundColor: "rgba(0,0,0,0.55)",
                  border: "0.2rem solid rgba(255,255,255,0.25)",
                  padding: "0.3rem"}, [
    __R__.createElement(__IMAGE__, {src: bol_c.logo, height: "100%",
                                    width: "100%"})]);
}
function bol_ArtWithBadge(bol_p) {
  var bol_h = __R__.createElement;
  return bol_div({position: "relative", height: bol_p.height}, [
    bol_h(bol_Art, bol_p),
    bol_h(bol_Badge, {card: bol_p.card, size: bol_p.badge})]);
}

// --------------------------------------------------------------------------
// A tile. Memoised: a new search or list update redraws only the tiles
// whose card changed.

function bol_TileView(bol_p) {
  var bol_c = bol_p.card, bol_R = __R__, bol_h = bol_R.createElement;
  var bol_ref = bol_R.useRef(bol_c);
  bol_ref.current = bol_c;
  var bol_onClick = bol_R.useCallback(function () {
    bol_play(bol_ref.current);
  }, []);
  var bol_onFocus = bol_R.useCallback(function (bol_f) {
    if (bol_f) bol_spotSet(bol_ref.current);
  }, []);
  var bol_onFav = bol_R.useCallback(function () {
    bol_toggleFav(bol_ref.current);
  }, []);
  return bol_div({width: "25%", padding: "0.6rem"}, [
    bol_h(__PANEL__, {compact: true, focusable: true, role: "neutral",
                      onClick: bol_onClick, onFocusChanged: bol_onFocus,
                      onHoverChanged: bol_onFocus,
                      onClickSecondary: bol_onFav,
                      narrationText: bol_c.n, inputLegend: bol_t("play")},
      bol_h(bol_ArtWithBadge, {card: bol_c, height: "13rem",
                               logoSize: "11rem", badge: "4.4rem"}),
      bol_div({padding: "0.8rem 1.1rem 1rem"}, [
        bol_line((bol_c.fav ? "★ " : "") + bol_c.n, "1.9rem",
                 bol_WHITE, {marginBottom: "0.4rem"}),
        bol_status(bol_c, "1.5rem")]))]);
}
var bol_Tile = __R__.memo(bol_TileView, function (bol_a, bol_b) {
  var bol_x = bol_a.card, bol_y = bol_b.card;
  return bol_x.k === bol_y.k && bol_x.fav === bol_y.fav
    && bol_x.id === bol_y.id && bol_x.n === bol_y.n
    && bol_x.wide === bol_y.wide && bol_x.on === bol_y.on;
});

// A featured experience: half the row, a big picture, its pitch.
function bol_BigTileView(bol_p) {
  var bol_c = bol_p.card, bol_R = __R__, bol_h = bol_R.createElement;
  var bol_ref = bol_R.useRef(bol_c);
  bol_ref.current = bol_c;
  var bol_onClick = bol_R.useCallback(function () {
    bol_play(bol_ref.current);
  }, []);
  var bol_onFocus = bol_R.useCallback(function (bol_f) {
    if (bol_f) bol_spotSet(bol_ref.current);
  }, []);
  var bol_onFav = bol_R.useCallback(function () {
    bol_toggleFav(bol_ref.current);
  }, []);
  var bol_s = bol_c.s;
  var bol_pitch = bol_s ? (bol_s.t || []).slice(0, 4).join(" · ")
                        : (bol_c.motd || "");
  return bol_div({width: "50%", padding: "0.6rem"}, [
    bol_h(__PANEL__, {compact: true, focusable: true, role: "neutral",
                      onClick: bol_onClick, onFocusChanged: bol_onFocus,
                      onHoverChanged: bol_onFocus,
                      onClickSecondary: bol_onFav,
                      narrationText: bol_c.n, inputLegend: bol_t("play")},
      bol_h(bol_ArtWithBadge, {card: bol_c, height: "21rem",
                               logoSize: "17rem", badge: "6.5rem",
                               big: "4rem"}),
      bol_div({padding: "1rem 1.4rem 1.2rem"}, [
        bol_div({display: "flex", flexDirection: "row",
                 alignItems: "center", marginBottom: "0.4rem"}, [
          bol_line((bol_c.fav ? "\u2605 " : "") + bol_c.n, "2.4rem",
                   bol_WHITE, {flexShrink: 1}),
          bol_div({flexGrow: 1}),
          bol_status(bol_c, "1.6rem")]),
        bol_line(bol_pitch || " ", "1.6rem", bol_DIM)]))]);
}
var bol_BigTile = __R__.memo(bol_BigTileView, function (bol_a, bol_b) {
  var bol_x = bol_a.card, bol_y = bol_b.card;
  return bol_x.k === bol_y.k && bol_x.fav === bol_y.fav
    && bol_x.id === bol_y.id && bol_x.n === bol_y.n
    && bol_x.wide === bol_y.wide && bol_x.on === bol_y.on;
});

// The last tile of a row that has more: "See all (42)" / "Show more".
function bol_MoreTile(bol_p) {
  var bol_h = __R__.createElement;
  return bol_div({width: "25%", padding: "0.6rem"}, [
    bol_h(__PANEL__, {compact: true, focusable: true, role: "neutral",
                      onClick: bol_p.onClick, narrationText: bol_p.label,
                      inputLegend: bol_p.label},
      bol_div({height: "17.2rem", display: "flex", flexDirection: "column",
               justifyContent: "center", alignItems: "center"}, [
        bol_text("+" + bol_p.count, {fontSize: "3.6rem", color: bol_WHITE}),
        bol_text(bol_p.label, {fontSize: "1.7rem", color: bol_DIM,
                               marginTop: "0.6rem"})]))]);
}

function bol_Row(bol_p) {
  var bol_h = __R__.createElement, bol_cards = bol_p.cards;
  var bol_limit = bol_p.limit, bol_kids = [];
  var bol_shown = bol_cards.length > bol_limit ? bol_limit - 1 : bol_limit;
  var bol_T = bol_p.big ? bol_BigTile : bol_Tile;
  bol_cards.slice(0, bol_shown).forEach(function (bol_c) {
    bol_kids.push(bol_h(bol_T, {key: bol_c.k, card: bol_c}));
  });
  if (bol_cards.length > bol_shown)
    bol_kids.push(bol_h(bol_MoreTile, {
      key: "more", count: bol_cards.length - bol_shown,
      label: bol_limit <= bol_ROW ? bol_t("seeAll") : bol_t("showMore"),
      onClick: bol_p.onMore}));
  return bol_div({marginBottom: "1.8rem"}, [
    bol_div({display: "flex", flexDirection: "row", alignItems: "flex-end",
             padding: "0 0.6rem 0.4rem"}, [
      bol_line(bol_p.title, bol_p.big ? "2.8rem" : "2.3rem", bol_WHITE,
               {flexShrink: 0}),
      bol_line(bol_p.hint || "", "1.6rem", bol_DIM,
               {marginLeft: "1.2rem", flexShrink: 1})]),
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
  var bol_desc = bol_s ? (bol_s.d || bol_s.motd || "") : (bol_c.motd || "");
  var bol_addr = bol_s ? bol_s.a + (bol_s.p !== 19132 ? ":" + bol_s.p : "")
                       : "";
  var bol_buttons = [
    bol_div({width: "18rem", marginRight: "1rem", flexShrink: 0}, [
      bol_h(__BUTTON__, {variant: "hero", inputLegend: bol_t("play"),
                         onClick: function () { bol_play(bol_c); }},
            bol_t("play"))])];
  if (bol_c.kind !== "tp")
    bol_buttons.push(bol_div({width: "26rem", flexShrink: 0}, [
      bol_h(__BUTTON__, {variant: "secondary",
                         inputLegend: bol_c.fav ? bol_t("unfavorite")
                                                : bol_t("favorite"),
                         onClick: function () { bol_toggleFav(bol_c); }},
            bol_c.fav ? bol_t("unfavorite") : bol_t("favorite"))]));
  return bol_div({display: "flex", flexDirection: "row", height: "23rem",
                  flexShrink: 0, backgroundColor: "rgba(0,0,0,0.6)",
                  border: "0.2rem solid #1e1e1f", margin: "0 0.6rem 1rem"}, [
    bol_div({width: "38%", flexShrink: 0}, [
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
        bol_line((bol_c.fav ? "\u2605 " : "") + bol_c.n, "3.2rem",
                 bol_WHITE, {flexShrink: 1})]),
      bol_div({display: "flex", flexDirection: "row", alignItems: "center",
               marginTop: "0.5rem"}, [
        bol_status(bol_c, "1.7rem"),
        bol_line(bol_meta.join("  ·  "), "1.7rem", bol_DIM,
                 {marginLeft: "1.6rem", flexShrink: 1})]),
      bol_text(bol_desc, {fontSize: "1.6rem", color: bol_DIM,
                          marginTop: "0.8rem", flexGrow: 1, flexBasis: 0,
                          overflow: "hidden"}),
      bol_div({display: "flex", flexDirection: "row", alignItems: "center",
               marginTop: "0.8rem"}, bol_buttons.concat([
        bol_line(bol_addr, "1.6rem", bol_DIM,
                 {marginLeft: "1.6rem", flexShrink: 1})]))])]);
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
  // What the details hold, for the featured servers' walk.
  var bol_detNow = __UFM__(function (bol_v) {
    var bol_n = bol_v.networkDetails || {};
    return {ok: !!bol_v.hasLoadedDetails,
            id: bol_n.id == null ? "" : String(bol_n.id),
            img: bol_n.imagePath || "", on: bol_n.playerCount,
            mx: bol_n.capacity, ping: bol_n.pingStatus || 0};
  }, [], [bol_det]);
  var bol_h = __R__.createElement;
  return bol_h(__UNWRAP__, {data: bol_snap}, function (bol_s) {
    return bol_h(__UNWRAP__, {data: bol_detNow}, function (bol_d) {
      return bol_h(bol_HubView, {snap: bol_s, det: bol_d,
                                 prefix: bol_p.routePrefix});
    });
  });
}

function bol_langChoices() {
  var bol_d = bol_data(), bol_out = [""];
  (bol_d && bol_d.langs || []).slice(0, 8).forEach(function (bol_l) {
    bol_out.push(bol_l[0]);
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

// The rows, from the cards and the search/language filters.
function bol_rowsFor(bol_all, bol_ws, bol_lg) {
  var bol_d = bol_data() || {}, bol_out = [];
  function bol_row(bol_key, bol_title, bol_cards, bol_hint, bol_big) {
    if (bol_cards.length)
      bol_out.push({key: bol_key, title: bol_title, cards: bol_cards,
                    hint: bol_hint || "", big: !!bol_big});
  }
  var bol_inLang = function (bol_c) {
    return !bol_lg || (bol_c.s && bol_c.s.l === bol_lg);
  };
  var bol_cat = bol_all.cat.filter(bol_inLang);
  if (bol_ws.length) {
    var bol_hits = [];
    bol_all.mine.concat(bol_all.featured).forEach(function (bol_c) {
      if (!bol_c.s && bol_nameMatches(bol_c.n, bol_ws))
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
  var bol_live = bol_cat.filter(function (bol_c) { return !bol_c.s.off; })
    .sort(bol_byPlayers);
  bol_row("mine", bol_t("myServers"), bol_all.mine.filter(bol_inLang),
          bol_t("myServersHint"));
  if (!bol_lg)
    bol_row("featured", bol_t("featured"), bol_all.featured,
            bol_t("featuredHint"), true);
  var bol_ui = bol_d.ui || "en";
  if (!bol_lg && bol_ui !== "en")
    bol_row("local", bol_t("popularIn", [bol_langName(bol_ui, true)]),
            bol_live.filter(function (bol_c) { return bol_c.s.l === bol_ui; }));
  bol_row("popular", bol_lg ? bol_t("popularIn", [bol_langName(bol_lg, true)])
                            : bol_t("popular"), bol_live,
          bol_d.pinged ? bol_t("pingedHint") : "");
  (bol_d.cats || []).slice(0, 6).forEach(function (bol_cg) {
    bol_row("cat-" + bol_cg[0], bol_cg[0], bol_live.filter(function (bol_c) {
      return (bol_c.s.t || []).indexOf(bol_cg[0]) >= 0;
    }));
  });
  var bol_every = bol_cat.slice().sort(bol_byPlayers);
  bol_row("all", bol_t("everyServer"), bol_every,
          bol_t("count", [bol_every.length]));
  return bol_out;
}

function bol_HubView(bol_p) {
  var bol_R = __R__, bol_h = bol_R.createElement;
  var bol_rootRef = bol_R.useRef(null);
  var bol_query = bol_R.useState(bol_state.query);
  var bol_lang = bol_R.useState(bol_state.lang);
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
  bol_R.useEffect(function () {
    var bol_before = JSON.stringify(bol_feat.info);
    var bol_wait = bol_featStep(bol_p.det, bol_featIds, Date.now());
    if (JSON.stringify(bol_feat.info) !== bol_before)
      bol_tick[1](function (bol_n) { return bol_n + 1; });
    if (!bol_wait) return undefined;
    var bol_timer = setTimeout(function () {
      bol_tick[1](function (bol_n) { return bol_n + 1; });
    }, bol_wait);
    return function () { clearTimeout(bol_timer); };
  }, [bol_p.det, bol_featIds.join(","), bol_tick[0]]);
  var bol_all = bol_R.useMemo(function () {
    return {cat: bol_base.cat, mine: bol_base.mine,
            featured: bol_base.featured.map(bol_withDetails)};
  }, [bol_base, bol_tick[0]]);
  var bol_ws = bol_words(bol_query[0]);
  var bol_rows = bol_rowsFor(bol_all, bol_ws, bol_lang[0]).map(
    function (bol_r) {
      var bol_limit = bol_open[0][bol_r.key] || bol_ROW;
      return bol_h(bol_Row, {
        key: bol_r.key, title: bol_r.title, hint: bol_r.hint,
        cards: bol_r.cards, big: bol_r.big,
        limit: bol_r.big ? bol_r.cards.length : bol_limit,
        onMore: function () {
          var bol_next = {};
          for (var bol_k in bol_state.open)
            bol_next[bol_k] = bol_state.open[bol_k];
          bol_next[bol_r.key] = bol_limit <= bol_ROW ? bol_PAGE
                                                     : bol_limit + bol_PAGE;
          bol_state.open = bol_next;
          bol_open[1](bol_next);
        }});
    });
  if (!bol_rows.length)
    bol_rows.push(bol_text(bol_ws.length ? bol_t("noMatch")
                                         : bol_t("listUnavailable"),
                           {fontSize: "2rem", color: bol_DIM,
                            padding: "2rem 0.6rem"}));

  var bol_byKey = bol_R.useMemo(function () {
    var bol_m = {};
    bol_all.cat.concat(bol_all.mine, bol_all.featured).forEach(
      function (bol_c) { bol_m[bol_c.k] = bol_c; });
    return bol_m;
  }, [bol_all]);
  var bol_fallback = bol_all.featured[0] || bol_all.mine[0]
    || bol_all.cat.slice().sort(bol_byPlayers)[0] || null;
  var bol_langs = bol_langChoices();
  var bol_cycleLang = function () {
    var bol_i = bol_langs.indexOf(bol_lang[0]);
    bol_state.lang = bol_langs[(bol_i + 1) % bol_langs.length];
    bol_lang[1](bol_state.lang);
  };
  var bol_toolbar = bol_div({display: "flex", flexDirection: "row",
                             alignItems: "flex-end", flexShrink: 0,
                             margin: "0 0.6rem 1.2rem"}, [
    bol_div({flexGrow: 2, flexBasis: 0}, [
      bol_h(__TEXTFIELD__, {label: bol_t("search"), value: bol_field[0],
                            placeholder: bol_t("searchPlaceholder"),
                            onChange: bol_onType})]),
    bol_div({width: "26rem", marginLeft: "1rem"}, [
      bol_h(__BUTTON__, {variant: bol_lang[0] ? "primary" : "secondary",
                         inputLegend: bol_t("language"),
                         onClick: bol_cycleLang},
            bol_langName(bol_lang[0]))]),
    bol_div({width: "28rem", marginLeft: "1rem"}, [
      bol_h(__BUTTON__, {variant: "secondary", Icon: __ICONS__.Plus,
                         inputLegend: bol_t("addServer"),
                         onClick: function () {
                           bol_ctx.router.push((bol_p.prefix || "/play")
                                               + "/servers/add");
                         }}, bol_t("addServer"))])]);
  return bol_h("div", {ref: bol_rootRef,
                       style: {display: "flex", flexDirection: "column",
                               height: "100%", padding: "1rem 2rem 0"}},
    bol_h(__FOCUS__, {containerRef: bol_rootRef,
                      delegateFocusFromMemory: true},
      bol_h(bol_Spotlight, {fallback: bol_fallback, byKey: bol_byKey}),
      bol_toolbar,
      bol_msg[0] ? bol_line(bol_msg[0], "1.7rem", bol_GREEN,
                            {margin: "0 0.6rem 0.8rem", flexShrink: 0})
                 : null,
      bol_h(__LEGEND__, {options: {button: __KEYS__.Y,
                                   inputLegend: "\u2605 " + bol_t("myServers")},
                         onClick: function () { bol_toggleFav(bol_spot.v); }}),
      bol_div({flexGrow: 1, flexShrink: 1, flexBasis: 0,
               display: "flex", flexDirection: "column"}, [
        bol_h.apply(null, [__SCROLL__, {loading: false}].concat(bol_rows)
                    .concat([bol_div({height: "2rem"})]))])));
}
