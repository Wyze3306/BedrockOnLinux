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
// The data (catalogue, settings, strings) is the __bolServerBrowser global
// the launcher writes on the bundle's first line at every launch. With it
// missing or switched off, every entry point below falls back to vanilla.
//
// Three sections, one side menu: the player's own servers ("favorites"),
// the partner and featured servers, and the community list. A catalogue
// server has no id in the game until it is added to the player's list, so
// its details pane is drawn here, and "Play" adds it first and then joins
// through the game's own online-play warning route, as "Add and play" does.

var bol_state = {section: "", query: "", cat: "", lang: "", shown: 0,
                 picker: ""};
var bol_PAGE = 40;

function bol_data() {
  return typeof __bolServerBrowser !== "undefined" && __bolServerBrowser
    ? __bolServerBrowser : null;
}
function bol_on() {
  var bol_d = bol_data();
  return !!(bol_d && bol_d.on);
}
function bol_isCat(bol_id) {
  return typeof bol_id === "string" && bol_id.indexOf("bol-") === 0;
}
function bol_isCatRoute(bol_id) {
  return bol_on() && bol_isCat(bol_id);
}
// The list items' own "selected" test: by route for a catalogue server, since
// the game's details then hold whatever it loaded in the background.
function bol_selected(bol_v, bol_nd, bol_r) {
  var bol_id = bol_r && bol_r.id;
  if (bol_isCatRoute(bol_id)) return bol_v === bol_id;
  return bol_v === bol_nd.networkDetails.id;
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
function bol_server(bol_id) {
  var bol_all = bol_servers();
  for (var bol_i = 0; bol_i < bol_all.length; bol_i++)
    if (bol_all[bol_i].i === bol_id) return bol_all[bol_i];
  return null;
}
// What a search matches: name and address first, then the categories,
// country, language and the description.
function bol_score(bol_s, bol_ws) {
  if (!bol_ws.length) return 1;
  if (!bol_s._head) {
    bol_s._head = bol_fold(bol_s.n + " " + bol_s.a);
    bol_s._body = bol_fold((bol_s.t || []).join(" ") + " " + (bol_s.c || "")
                           + " " + (bol_s.l || "") + " " + (bol_s.d || ""));
  }
  var bol_score = 0;
  for (var bol_i = 0; bol_i < bol_ws.length; bol_i++) {
    if (bol_s._head.indexOf(bol_ws[bol_i]) >= 0) bol_score += 3;
    else if (bol_s._body.indexOf(bol_ws[bol_i]) >= 0) bol_score += 1;
    else return 0;
  }
  return bol_score;
}
function bol_nameMatches(bol_name, bol_ws) {
  var bol_f = bol_fold(bol_name);
  return bol_ws.every(function (bol_w) { return bol_f.indexOf(bol_w) >= 0; });
}
// The player's servers by folded name, which is all the list carries: an
// entry added from the catalogue keeps the catalogue's name.
function bol_favIndex(bol_favs) {
  var bol_ix = {};
  (bol_favs || []).forEach(function (bol_f) {
    bol_ix[bol_fold(bol_f.name)] = bol_f.id;
  });
  return bol_ix;
}
function bol_subtitle(bol_s) {
  var bol_bits = (bol_s.t || []).slice(0, 2);
  if (bol_s.l) bol_bits.push(String(bol_s.l).toUpperCase());
  return bol_bits.join(" · ");
}
function bol_catalogue(bol_st, bol_favs) {
  var bol_ws = bol_words(bol_st.query), bol_ix = bol_favIndex(bol_favs);
  var bol_out = [];
  bol_servers().forEach(function (bol_s, bol_n) {
    if (bol_st.cat && (bol_s.t || []).indexOf(bol_st.cat) < 0) return;
    if (bol_st.lang && bol_s.l !== bol_st.lang) return;
    var bol_sc = bol_score(bol_s, bol_ws);
    if (bol_sc > 0) bol_out.push({s: bol_s, sc: bol_sc, n: bol_n});
  });
  bol_out.sort(function (bol_a, bol_b) {
    return (bol_b.s.pt ? 1 : 0) - (bol_a.s.pt ? 1 : 0)
      || bol_b.sc - bol_a.sc || bol_a.n - bol_b.n;
  });
  return bol_out.map(function (bol_e) {
    var bol_fav = bol_ix[bol_fold(bol_e.s.n)] != null;
    return {label: (bol_fav ? "★ " : "") + bol_e.s.n,
            value: bol_e.s.i, type: __KD__.External,
            message: bol_subtitle(bol_e.s)};
  });
}
// The live lists as plain data, one snapshot per engine update.
function bol_snapshot(bol_x, bol_tp) {
  var bol_ext = Array.from(bol_x.externalServerWorlds);
  var bol_partners = [];
  if (typeof __bolPromoted === "function") {
    bol_partners = __bolPromoted(bol_x).map(function (bol_e) {
      return {id: bol_e.s.id, name: bol_e.p.label, motd: bol_e.p.message,
              image: bol_e.p.icon};
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
function bol_option(bol_value, bol_label, bol_count) {
  return {label: bol_label, value: "bolopt-" + bol_value,
          type: __KD__.External,
          message: bol_count == null ? "" : bol_t("count", [bol_count])};
}

// --------------------------------------------------------------------------
// Small wrappers, so that every hook sits at the top of its own component.

function bol_Text(bol_p) {
  return __R__.createElement("fast-text", {text: __WRAP__(bol_p.text)});
}
function bol_List(bol_p) {
  var bol_items = __WRAP__(bol_p.items), bol_pick = bol_p.onSelect;
  var bol_render = __R__.useCallback(function (bol_o) {
    return __R__.createElement(__ITEM__, {item: bol_o.item,
                                          onSelectServer: bol_pick});
  }, [bol_pick]);
  return __R__.createElement(__XJ__.ItemList, {items: bol_items,
                                               renderItem: bol_render});
}
function bol_Section(bol_p) {
  var bol_h = __R__.createElement;
  return bol_h(__R__.Fragment, null,
    bol_h(__XJ__.Divider, {label: bol_h(bol_Text, {text: bol_p.label})}),
    bol_h(bol_List, {items: bol_p.items, onSelect: bol_p.onSelect}));
}
function bol_Note(bol_p) {
  var bol_h = __R__.createElement;
  return bol_h(__XJ__.Item, null,
    bol_h("div", {style: {padding: "0.5rem 0.25rem"}},
      bol_h(__BODY__, null, bol_p.text)));
}
// A row of buttons sharing the width: the section tabs, the filters.
function bol_Row(bol_p) {
  var bol_h = __R__.createElement;
  return bol_h(__XJ__.Item, null,
    bol_h("div", {style: {display: "flex", flexDirection: "row"}},
      bol_p.buttons.map(function (bol_b, bol_i) {
        return bol_h("div", {key: bol_b.key || String(bol_i),
                             style: {flexGrow: 1, flexBasis: 0,
                                     marginLeft: bol_i ? "0.25rem" : 0}},
          bol_h(__BUTTON__, {onClick: bol_b.onClick,
                             variant: bol_b.active ? "primary" : "secondary",
                             inputLegend: bol_b.legend || bol_b.label},
                bol_b.label));
      })));
}

// --------------------------------------------------------------------------
// The side menu.

function bol_Side(bol_p) {
  var bol_ext = __USF__(__EXT__), bol_tp = __USF__(__TP__);
  var bol_snap = __UFM__(function (bol_x, bol_y) {
    return bol_snapshot(bol_x, bol_y);
  }, [], [bol_ext, bol_tp]);
  var bol_router = __ROUTER__();
  var bol_h = __R__.createElement;
  return bol_h(__UNWRAP__, {data: bol_snap}, function (bol_s) {
    return bol_h(bol_SideView, {snap: bol_s, router: bol_router,
                                loading: bol_p.loading,
                                prefix: bol_p.routePrefix || "/play"});
  });
}

function bol_SideView(bol_p) {
  var bol_R = __R__, bol_h = bol_R.createElement, bol_snap = bol_p.snap;
  var bol_init = bol_state.section
    || (bol_snap.favs.length ? "fav" : "all");
  var bol_sec = bol_R.useState(bol_init);
  var bol_query = bol_R.useState(bol_state.query);
  var bol_cat = bol_R.useState(bol_state.cat);
  var bol_lang = bol_R.useState(bol_state.lang);
  var bol_shown = bol_R.useState(bol_state.shown || bol_PAGE);
  var bol_picker = bol_R.useState(bol_state.picker);
  var bol_field = __UFS__(bol_state.query);
  function bol_set(bol_hook, bol_key) {
    return function (bol_v) { bol_state[bol_key] = bol_v; bol_hook[1](bol_v); };
  }
  var bol_setSec = bol_set(bol_sec, "section");
  var bol_setQuery = bol_set(bol_query, "query");
  var bol_setCat = bol_set(bol_cat, "cat");
  var bol_setLang = bol_set(bol_lang, "lang");
  var bol_setShown = bol_set(bol_shown, "shown");
  var bol_setPicker = bol_set(bol_picker, "picker");
  var bol_prefix = bol_p.prefix, bol_router = bol_p.router;
  var bol_onType = bol_R.useCallback(function (bol_v) {
    bol_field[1](bol_v);
    bol_setQuery(String(bol_v == null ? "" : bol_v));
    bol_setShown(bol_PAGE);
  }, []);
  var bol_select = bol_R.useCallback(function (bol_value, bol_type) {
    var bol_v = String(bol_value);
    if (bol_v.indexOf("bolopt-") === 0) {
      var bol_opt = bol_v.slice(7);
      if (bol_state.picker === "cat") bol_setCat(bol_opt === "*" ? "" : bol_opt);
      else bol_setLang(bol_opt === "*" ? "" : bol_opt);
      bol_setShown(bol_PAGE);
      bol_setPicker("");
      return;
    }
    var bol_kind = bol_isCat(bol_v) ? __KD__.External : bol_type;
    bol_router.replace(bol_prefix + "/servers/" + bol_v + "/"
                       + __JU__(bol_kind));
  }, [bol_router, bol_prefix]);

  var bol_st = {query: bol_query[0], cat: bol_cat[0], lang: bol_lang[0]};
  var bol_ws = bol_words(bol_st.query);
  // A partner is one of the player's own entries drawn as a featured tile:
  // the item's bolType routes it as the external server it is (the partner
  // patch reads it in the list item).
  function bol_mine(bol_list, bol_kind, bol_route) {
    return bol_list.filter(function (bol_e) {
      return bol_nameMatches(bol_e.name, bol_ws);
    }).map(function (bol_e) {
      return {label: bol_e.name, value: bol_e.id, type: bol_kind,
              bolType: bol_route, message: bol_e.motd || "",
              image: bol_e.image};
    });
  }
  var bol_favs = bol_mine(bol_snap.favs, __KD__.External);
  var bol_partners = bol_mine(bol_snap.partners, __KD__.ThirdParty,
                              __KD__.External);
  var bol_featured = bol_mine(bol_snap.featured, __KD__.ThirdParty);
  var bol_creators = bol_mine(bol_snap.creators, __KD__.ThirdParty);
  var bol_cata = bol_catalogue(bol_st, bol_snap.favs);
  var bol_partnerCount = bol_partners.length + bol_featured.length
    + bol_creators.length;

  var bol_kids = [];
  bol_kids.push(bol_h(bol_Row, {key: "tabs", buttons: [
    {key: "fav", label: bol_t("tabFavorites", [bol_favs.length]),
     active: bol_sec[0] === "fav",
     onClick: function () { bol_setPicker(""); bol_setSec("fav"); }},
    {key: "partners", label: bol_t("tabPartners", [bol_partnerCount]),
     active: bol_sec[0] === "partners",
     onClick: function () { bol_setPicker(""); bol_setSec("partners"); }},
    {key: "all", label: bol_t("tabAll", [bol_cata.length]),
     active: bol_sec[0] === "all",
     onClick: function () { bol_setPicker(""); bol_setSec("all"); }}
  ]}));
  bol_kids.push(bol_h(__XJ__.Item, {key: "search"},
    bol_h(__TEXTFIELD__, {label: bol_t("search"), value: bol_field[0],
                          onChange: bol_onType,
                          placeholder: bol_t("searchPlaceholder")})));

  if (bol_sec[0] === "fav") {
    bol_kids.push(bol_h(__XJ__.Item, {key: "add"},
      bol_h(__BUTTON__, {onClick: function () {
        bol_router.push(bol_prefix + "/servers/add");
      }, Icon: __ICONS__.Plus, inputLegend: bol_t("addServer"),
         variant: "secondary"}, bol_t("addServer"))));
    if (bol_favs.length)
      bol_kids.push(bol_h(bol_Section, {key: "favs", items: bol_favs,
        label: bol_t("yourServers", [bol_favs.length]),
        onSelect: bol_select}));
    else
      bol_kids.push(bol_h(bol_Note, {key: "nofav", text: bol_ws.length
        ? bol_t("noMatch") : bol_t("noFavorites")}));
  } else if (bol_sec[0] === "partners") {
    if (bol_partners.length)
      bol_kids.push(bol_h(bol_Section, {key: "bolp", items: bol_partners,
        label: bol_t("bolPartners"), onSelect: bol_select}));
    if (bol_featured.length)
      bol_kids.push(bol_h(bol_Section, {key: "feat", items: bol_featured,
        label: bol_t("featured", [bol_featured.length]),
        onSelect: bol_select}));
    if (bol_creators.length)
      bol_kids.push(bol_h(bol_Section, {key: "crea", items: bol_creators,
        label: bol_t("creators", [bol_creators.length]),
        onSelect: bol_select}));
    if (!bol_partnerCount)
      bol_kids.push(bol_h(bol_Note, {key: "nopart", text: bol_ws.length
        ? bol_t("noMatch") : bol_t("noPartners")}));
  } else if (bol_picker[0]) {
    var bol_isCatPicker = bol_picker[0] === "cat";
    var bol_d = bol_data() || {};
    var bol_opts = [bol_option("*", bol_isCatPicker ? bol_t("allCategories")
                                                    : bol_t("allLanguages"))];
    (bol_isCatPicker ? bol_d.cats : bol_d.langs || []).forEach(function (bol_o) {
      bol_opts.push(bol_option(bol_o[0], bol_isCatPicker ? bol_o[0]
                               : bol_o[2] || bol_o[0], bol_o[1]));
    });
    bol_kids.push(bol_h(bol_Row, {key: "back", buttons: [
      {key: "back", label: bol_t("back"),
       onClick: function () { bol_setPicker(""); }}]}));
    bol_kids.push(bol_h(bol_Section, {key: "opts", items: bol_opts,
      label: bol_isCatPicker ? bol_t("category") : bol_t("language"),
      onSelect: bol_select}));
  } else {
    var bol_langName = bol_st.lang;
    ((bol_data() || {}).langs || []).forEach(function (bol_o) {
      if (bol_o[0] === bol_st.lang) bol_langName = bol_o[2] || bol_o[0];
    });
    bol_kids.push(bol_h(bol_Row, {key: "filters", buttons: [
      {key: "cat", label: bol_st.cat || bol_t("allCategories"),
       legend: bol_t("category"), active: !!bol_st.cat,
       onClick: function () { bol_setPicker("cat"); }},
      {key: "lang", label: bol_langName || bol_t("allLanguages"),
       legend: bol_t("language"), active: !!bol_st.lang,
       onClick: function () { bol_setPicker("lang"); }}
    ]}));
    if (!bol_servers().length)
      bol_kids.push(bol_h(bol_Note, {key: "nolist",
                                     text: bol_t("listUnavailable")}));
    else if (!bol_cata.length)
      bol_kids.push(bol_h(bol_Note, {key: "nores", text: bol_t("noMatch")}));
    else {
      var bol_n = Math.min(bol_shown[0], bol_cata.length);
      bol_kids.push(bol_h(bol_Section, {key: "cata",
        items: bol_cata.slice(0, bol_n),
        label: bol_t("servers", [bol_cata.length]), onSelect: bol_select}));
      if (bol_n < bol_cata.length)
        bol_kids.push(bol_h(bol_Row, {key: "more", buttons: [
          {key: "more", label: bol_t("showMore", [bol_n, bol_cata.length]),
           onClick: function () { bol_setShown(bol_n + bol_PAGE); }}]}));
    }
  }
  bol_kids.push(bol_h(__SPACER__, {key: "end", size: 4}));

  var bol_role = "neutralAlpha60";
  return bol_h(__THEME__, {role: bol_role},
    bol_h(__NARRATION__, {text: bol_t("narrationContext")},
      bol_h.apply(null, [__XJ__, {
        spacingAroundSideMenu: bol_h(__SPACER__, {size: 2}),
        loading: bol_p.loading,
        gamepadAlias: __ALIAS__.SERVER_SIDE_MENU,
        delegateFocusByAlias: "remember",
        role: bol_role}].concat(bol_kids))));
}

// --------------------------------------------------------------------------
// The details pane: a catalogue server's own, the game's for anything else.

function bol_Details(bol_p) {
  var bol_route = __ROUTE__(), bol_h = __R__.createElement;
  var bol_id = __UFM__(function (bol_r) {
    var bol_v = bol_r && bol_r.id;
    return bol_isCatRoute(bol_v) ? bol_v : "";
  }, [], [bol_route]);
  return bol_h(__UNWRAP__, {data: bol_id}, function (bol_v) {
    return bol_v
      ? bol_h(bol_CatDetails, {key: bol_v, id: bol_v,
                               scrollViewRef: bol_p.scrollViewRef})
      : bol_h(__DETAILS__, bol_p);
  });
}

function bol_CatDetails(bol_p) {
  var bol_ext = __USF__(__EXT__), bol_h = __R__.createElement;
  var bol_favs = __UFM__(function (bol_x) {
    return Array.from(bol_x.externalServerWorlds).map(function (bol_e) {
      return {id: bol_e.id, name: bol_e.name};
    });
  }, [], [bol_ext]);
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
  var bol_router = __ROUTER__();
  return bol_h(__UNWRAP__, {data: bol_favs}, function (bol_list) {
    return bol_h(bol_CatView, {id: bol_p.id, favs: bol_list, act: bol_act,
                               router: bol_router,
                               scrollViewRef: bol_p.scrollViewRef});
  });
}

function bol_CatView(bol_p) {
  var bol_R = __R__, bol_h = bol_R.createElement;
  var bol_s = bol_server(bol_p.id);
  var bol_msg = bol_R.useState("");
  if (!bol_s)
    return bol_h("div", {style: {padding: "1rem"}},
      bol_h(__BODY__, null, bol_t("listUnavailable")));
  var bol_favId = bol_favIndex(bol_p.favs)[bol_fold(bol_s.n)];
  var bol_isFav = bol_favId != null;
  function bol_add() {
    var bol_r = bol_p.act("add", bol_s);
    if (bol_r.error != null) {
      bol_msg[1](bol_t(bol_r.error === 6 ? "alreadyAdded" : "addFailed"));
      return null;
    }
    return bol_r.id;
  }
  function bol_play() {
    var bol_id = bol_isFav ? bol_favId : bol_add();
    if (bol_id == null) return;
    bol_p.router.push("/online-play-warning/" + __JU__(__KD__.External) + "/"
                      + bol_id);
  }
  function bol_toggle() {
    if (bol_isFav) {
      var bol_r = bol_p.act("remove", bol_s, bol_favId);
      bol_msg[1](bol_r.error != null ? bol_t("removeFailed") : "");
    } else if (bol_add() != null) bol_msg[1](bol_t("added"));
  }
  var bol_meta = [bol_s.a + (bol_s.p !== 19132 ? ":" + bol_s.p : "")];
  if (bol_s.ln || bol_s.l) bol_meta.push(bol_s.ln || bol_s.l);
  if (bol_s.c) bol_meta.push(bol_s.c);
  if (bol_s.v) bol_meta.push(bol_t("version", [bol_s.v]));
  var bol_links = [];
  if (bol_s.w) bol_links.push(bol_s.w);
  if (bol_s.dc) bol_links.push(bol_s.dc);
  var bol_gap = bol_h(__SPACER__, {size: 2});

  var bol_body = [
    bol_s.img ? bol_h("div", {key: "img", style: {width: "100%"}},
      bol_h(__IMAGE__, {src: bol_s.img, paddingTopPercentage: bol_s.ih || 25}))
      : null,
    bol_h(__PANEL__, {key: "head", role: "neutral80"},
      bol_h(__HEADING__, null, bol_s.n),
      bol_h(__SPACER__, {size: 1}),
      bol_h(__BODY__, null, bol_meta.join(" · ")),
      (bol_s.t || []).length
        ? bol_h(__BODY__, null, bol_s.t.join(" · ")) : null,
      bol_gap,
      bol_h("div", {style: {display: "flex", flexDirection: "row"}},
        bol_h("div", {style: {flexGrow: 2, flexBasis: 0}},
          bol_h(__BUTTON__, {variant: "hero", horizontal: true,
                             delayedClick: true, onClick: bol_play,
                             inputLegend: bol_t("play"),
                             gamepadAlias: __ALIAS__.SERVER_DESCRIPTION},
                bol_t("play"))),
        bol_h("div", {style: {flexGrow: 1, flexBasis: 0,
                              marginLeft: "0.5rem"}},
          bol_h(__BUTTON__, {variant: "secondary", onClick: bol_toggle,
                             inputLegend: bol_isFav ? bol_t("unfavorite")
                                                    : bol_t("favorite")},
                bol_isFav ? bol_t("unfavorite") : bol_t("favorite")))),
      bol_msg[0] ? bol_h(__BODY__, null, bol_msg[0]) : null),
    bol_h(__PANEL__, {key: "desc", focusable: true, role: "neutral80",
                      narrationText: bol_s.n + " . " + (bol_s.d || "")},
      bol_h(__HEADING__, null, bol_t("about")),
      bol_gap,
      bol_h(__BODY__, null, bol_s.d || bol_t("noDescription"))),
    bol_links.length ? bol_h(__PANEL__, {key: "links", role: "neutral80"},
      bol_h(__HEADING__, null, bol_t("links")),
      bol_gap,
      bol_links.map(function (bol_l) {
        return bol_h(__BODY__, {key: bol_l}, bol_l);
      })) : null,
    bol_h(__PANEL__, {key: "src", role: "neutral80"},
      bol_h(__BODY__, null, bol_t("source")))
  ];
  return bol_h.apply(null, [__SCROLL__, {
    loading: false, innerRef: bol_p.scrollViewRef, floating: false,
    alwaysVisibleChildren: bol_h(__SPACER__, {size: 2})}].concat(bol_body));
}
