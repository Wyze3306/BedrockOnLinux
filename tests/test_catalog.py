"""The MCPEDL/CurseForge catalogue (bol/catalog.py), with no network."""
# SPDX-License-Identifier: MIT

import json
import os
import time
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from bol import catalog, content

USERS = "drive_c/users/steamuser/AppData/Roaming/Minecraft Bedrock/Users"

SEARCH = {
    "meta": {"total": 47, "page": 1, "page_size": 20, "page_count": 3},
    "results": [
        {"slug": "armored-backpack", "title": "Armored Backpack &amp; Co",
         "summary": "A backpack", "image": "https://media.forgecdn.net/a.png",
         "display_name": "Bruhh69", "downloadCount": 134,
         "average_rating": "4.0714", "type_id": 1,
         "cf_tags": [{"name": "Survival"}, {"name": "Survival"},
                     {"name": "Utility"}],
         "updated_at": "2026-11-12T14:23:00Z"},
        {"title": "no slug, skipped"},
    ],
}

DETAIL = {"data": {
    "slug": "armored-backpack", "title": "Armored Backpack", "type_id": 1,
    "image": "https://media.forgecdn.net/a.png",
    "submission_images": ["https://r2.mcpedl.com/1.png", "http://x/2.png"],
    "user": {"display_name": "Bruhh69"},
    "download_count": 134, "average_rating": "4",
    "description": "<p>Hello <b>world</b></p><ul><li>one</li></ul>"
                   "<script>alert(1)</script>",
    "minecraft_versions": [{"name": "1.21.30"}, {"name": "1.21.130"},
                           {"name": "1.21.50"}],
    "cf_link": "https://www.curseforge.com/minecraft-bedrock/addons/"
               "armored-backpack",
    "source": "curseforge",
    "downloads": [
        {"display_name": "Armored Backpack v1.7 (27.43 KB)",
         "name": "ArmoredBackpack_v1.7.1.mcaddon (27.43 KB)",
         "file": "https://edge.mcpedl.com/files/9575/10/"
                 "ArmoredBackpack_v1.7.1.mcaddon", "type": 5},
        {"display_name": "Armored Backpack v1.7 (27.43 KB)",
         "name": "ArmoredBackpack_v1.7.1.mcaddon (27.43 KB)",
         "file": "https://edge.mcpedl.com/files/9575/10/"
                 "ArmoredBackpack_v1.7.1.mcaddon", "type": 1},
        {"display_name": "download rp", "name": "download rp",
         "file": "/leaving/?url=https%3A%2F%2Fbstlar.com%2F28i%2Frp",
         "type": 3},
        {"display_name": "readme.txt", "name": "readme.txt (1 KB)",
         "file": "https://edge.mcpedl.com/files/1/2/readme.txt", "type": 1},
    ],
}}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "CATALOG_CACHE", tmp_path / "cache")
    monkeypatch.setattr(catalog, "INSTALLS_FILE", tmp_path / "installs.json")
    monkeypatch.setattr(catalog, "TRASH", tmp_path / "trash")
    return tmp_path


def _pack(path, name, module_type="data"):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("manifest.json", json.dumps({
            "format_version": 2,
            "header": {"name": name, "description": "d", "uuid": "u",
                       "version": [1, 0, 0]},
            "modules": [{"type": module_type, "uuid": "m",
                         "version": [1, 0, 0]}]}))
        z.writestr("pack_icon.png", b"png")
    return path


# ------------------------------------------------------------------ listing

def test_search_with_a_query_ranks_by_relevance(store):
    with mock.patch.object(catalog, "_get_json",
                           return_value=SEARCH) as get:
        page = catalog.search("  back   pack ", "addons", "new", page=2)
    path, params = get.call_args.args
    assert path == "search/advanced"
    assert params == {"type_id": 1, "page": 2, "q": "back pack"}
    assert page["pages"] == 3 and page["total"] == 47
    [item] = page["items"]
    assert item["title"] == "Armored Backpack & Co"
    assert item["rating"] == 4.1 and item["downloads"] == 134
    assert item["tags"] == ["Survival", "Utility"]
    assert item["kind"] == "addons"
    assert item["url"] == "https://mcpedl.com/armored-backpack/"


def test_search_without_a_query_sorts_the_kind(store):
    with mock.patch.object(catalog, "_get_json",
                           return_value=SEARCH) as get:
        catalog.search("", "maps", "popular_all")
    assert get.call_args.args[1] == {"type_id": 2, "page": 1,
                                     "sort": "popularity_all"}


def test_an_unknown_kind_is_refused(store):
    with pytest.raises(catalog.CatalogError):
        catalog.search("", "seeds")


def test_results_are_cached_and_served_stale_when_offline(store):
    with mock.patch.object(catalog, "_get_json", return_value=SEARCH) as get:
        catalog.search("x")
        catalog.search("x")
    assert get.call_count == 1
    for cached in (store / "cache").glob("search-*.json"):
        old = time.time() - 86400
        os.utime(cached, (old, old))
    offline = catalog.CatalogError("MCPEDL cannot be reached")
    with mock.patch.object(catalog, "_get_json", side_effect=offline):
        assert catalog.search("x")["total"] == 47
        with pytest.raises(catalog.CatalogError, match="reached"):
            catalog.search("never cached")


def test_featured_leaves_a_kind_empty_rather_than_failing(store):
    def fake(path, params):
        if params["type_id"] == 2:
            raise catalog.CatalogError("down")
        return SEARCH
    with mock.patch.object(catalog, "_get_json", side_effect=fake):
        shelves = catalog.featured(("addons", "maps"))
    assert [i["slug"] for i in shelves["addons"]] == ["armored-backpack"]
    assert shelves["maps"] == []


# ------------------------------------------------------------------ details

def test_details_split_files_from_links(store):
    with mock.patch.object(catalog, "_get_json", return_value=DETAIL) as get:
        item = catalog.details("armored-backpack")
    assert get.call_args.args[0] == "route/slug/armored-backpack"
    # The same file served twice (web and "VIP") is listed once.
    assert [f["name"] for f in item["files"]] == [
        "ArmoredBackpack_v1.7.1.mcaddon", "readme.txt"]
    first = item["files"][0]
    assert first["size"] == "27.43 KB" and first["installable"]
    assert first["label"] == "Armored Backpack v1.7"
    assert not item["files"][1]["installable"]
    [link] = item["links"]
    assert link["host"] == "bstlar.com"
    assert link["url"].startswith("https://mcpedl.com/leaving/")
    assert item["versions"] == ["1.21.130", "1.21.50", "1.21.30"]
    assert item["images"] == ["https://media.forgecdn.net/a.png",
                              "https://r2.mcpedl.com/1.png"]
    assert item["description"] == "Hello world\n• one"
    assert item["curseforge"].endswith("/addons/armored-backpack")


def test_details_refuse_a_name_that_is_not_a_slug(store):
    with mock.patch.object(catalog, "_get_json") as get:
        with pytest.raises(catalog.CatalogError):
            catalog.details("../../auth/user")
    get.assert_not_called()


@pytest.mark.parametrize("text, slug", [
    ("https://mcpedl.com/armored-backpack/", "armored-backpack"),
    ("mcpedl.com/armored-backpack", "armored-backpack"),
    ("https://www.curseforge.com/minecraft-bedrock/addons/armored-backpack",
     "armored-backpack"),
    ("https://www.curseforge.com/minecraft-bedrock/maps/x/files", "x"),
    ("armored-backpack", "armored-backpack"),
    ("https://www.curseforge.com/minecraft/mc-mods/jei", None),
    ("https://example.com/armored-backpack", None),
    ("two words", None),
    ("", None),
])
def test_links_resolve_to_a_slug(text, slug):
    assert catalog.resolve(text) == slug


def test_supports_compares_the_first_three_numbers():
    item = {"versions": ["1.21.130", "1.21.50"]}
    assert catalog.supports(item, "1.21.130.4") is True
    assert catalog.supports(item, "1.21.60.1") is False
    assert catalog.supports({"versions": []}, "1.21.60.1") is True
    assert catalog.supports(item, None) is None


# ------------------------------------------------------------------ network

@pytest.mark.parametrize("url, allowed", [
    ("https://edge.mcpedl.com/files/1/2/a.mcpack", True),
    ("https://mediafilez.forgecdn.net/files/1/2/a.mcpack", True),
    ("https://api.mcpedl.com/api/download/1/?r=x", True),
    ("http://edge.mcpedl.com/files/1/2/a.mcpack", False),
    ("https://evilmcpedl.com/a.mcpack", False),
    ("https://bstlar.com/28i/rp", False),
])
def test_only_https_mcpedl_and_curseforge_hosts(url, allowed):
    assert catalog._host_allowed(url) is allowed


def test_a_redirect_off_the_allowed_hosts_is_refused():
    handler = catalog._HttpsOnlyRedirects()
    with pytest.raises(catalog.CatalogError, match="not MCPEDL"):
        handler.redirect_request(None, None, 302, "Found", {},
                                 "https://mediafire.com/file.mcpack")


def test_download_refuses_a_foreign_host(store):
    with pytest.raises(catalog.CatalogError, match="Refusing"):
        catalog.download({"name": "a.mcpack",
                          "url": "https://example.com/a.mcpack"})


class _Response:
    def __init__(self, data):
        self._data = data
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=-1):
        chunk, self._data = self._data[:size], self._data[size:]
        return chunk


def test_download_writes_the_file_and_reports_progress(store, tmp_path):
    pack = _pack(tmp_path / "src.mcpack", "P").read_bytes()
    seen = []
    with mock.patch.object(catalog, "_open",
                           return_value=_Response(pack)):
        path = catalog.download(
            {"name": "../evil name.mcpack",
             "url": "https://edge.mcpedl.com/f/a.mcpack"},
            progress=lambda got, total: seen.append((got, total)))
    assert path.parent == store / "cache" / "downloads"
    assert path.name == "evil name.mcpack"
    assert path.read_bytes() == pack
    assert seen[-1] == (len(pack), len(pack))
    assert not list(path.parent.glob(".part-*"))


def test_download_rejects_what_is_not_a_zip(store):
    with mock.patch.object(catalog, "_open",
                           return_value=_Response(b"<html>ad</html>")):
        with pytest.raises(catalog.CatalogError, match="not a Minecraft"):
            catalog.download({"name": "a.mcpack",
                              "url": "https://edge.mcpedl.com/a.mcpack"})
    assert not list((store / "cache" / "downloads").iterdir())


# ------------------------------------------------------------------ install

def test_a_plain_zip_is_imported_by_what_it_holds(tmp_path):
    world = tmp_path / "w.zip"
    with zipfile.ZipFile(world, "w") as z:
        z.writestr("My World/level.dat", "")
    packs = tmp_path / "p.zip"
    with zipfile.ZipFile(packs, "w") as z:
        z.writestr("Thing_BP.mcpack", "")
    junk = tmp_path / "j.zip"
    with zipfile.ZipFile(junk, "w") as z:
        z.writestr("readme.txt", "")
    assert catalog._content_suffix(world) == ".mcworld"
    assert catalog._content_suffix(packs) == ".mcaddon"
    with pytest.raises(catalog.CatalogError):
        catalog._content_suffix(junk)


def _fake_download(source):
    def download(file, progress=None, cancelled=None):
        target = Path(source).with_name(file["name"])
        target.write_bytes(Path(source).read_bytes())
        return target
    return download


def test_install_imports_records_and_uninstalls(store, tmp_path):
    prefix = tmp_path / "prefix"
    source = _pack(tmp_path / "source.bin", "§aCool Pack", "resources")
    item = {"slug": "cool", "title": "Cool", "kind": "textures"}
    file = {"name": "Cool.mcpack", "url": "https://edge.mcpedl.com/c",
            "installable": True}
    with mock.patch.object(catalog, "download", _fake_download(source)):
        results = catalog.install(item, file, prefix=prefix)

    assert results == ["resource pack: Cool Pack"]
    installed = content._mojang_dir(prefix) / "resource_packs" / "Cool Pack"
    assert installed.is_dir()
    [record] = catalog.load_installs()
    assert record["paths"] == [str(installed)]
    assert catalog.installed_slugs() == {"cool"}

    entries = catalog.library(prefix)
    assert [(e["name"], e["kind"], e["slug"]) for e in entries] == [
        ("Cool Pack", "Resource pack", "cool")]
    assert entries[0]["icon"].endswith("pack_icon.png")

    assert catalog.uninstall("cool") == 1
    assert not installed.exists()
    assert catalog.load_installs() == []
    trashed = list((store / "trash").iterdir())
    assert len(trashed) == 1 and (trashed[0] / "manifest.json").is_file()


def test_installing_again_replaces_the_record(store, tmp_path):
    prefix = tmp_path / "prefix"
    source = _pack(tmp_path / "source.bin", "Again")
    item = {"slug": "again", "title": "Again"}
    file = {"name": "Again.mcpack", "url": "https://edge.mcpedl.com/a"}
    with mock.patch.object(catalog, "download", _fake_download(source)):
        catalog.install(item, file, prefix=prefix)
        catalog.install(item, file, prefix=prefix)
    assert len(catalog.load_installs()) == 1


def test_a_file_that_is_not_content_is_not_downloaded(store):
    with mock.patch.object(catalog, "download") as download:
        with pytest.raises(catalog.CatalogError):
            catalog.install({"slug": "x"}, {"name": "readme.txt",
                                            "installable": False})
    download.assert_not_called()


# ------------------------------------------------------------------ library

def test_library_lists_worlds_by_their_level_name(store, tmp_path):
    prefix = tmp_path / "prefix"
    account = prefix / USERS / "1234" / "games" / "com.mojang"
    (account / "minecraftpe").mkdir(parents=True)
    (account / "minecraftpe" / "options.txt").write_text("x")
    world = account / "minecraftWorlds" / "AbCdEf="
    world.mkdir(parents=True)
    (world / "level.dat").write_bytes(b"")
    (world / "levelname.txt").write_text("My Island\n")
    (account / "minecraftWorlds" / "not-a-world").mkdir()

    [entry] = catalog.library(prefix)
    assert entry["name"] == "My Island" and entry["kind"] == "World"
    assert entry["slug"] is None


def test_remove_moves_a_world_to_the_trash(store, tmp_path):
    world = tmp_path / "com.mojang" / "minecraftWorlds" / "w1"
    world.mkdir(parents=True)
    (world / "level.dat").write_bytes(b"save")
    target = catalog.remove(world)
    assert not world.exists()
    assert (target / "level.dat").read_bytes() == b"save"
    assert target.parent == store / "trash"


@pytest.mark.parametrize("relative", [
    "com.mojang", "com.mojang/minecraftWorlds", "elsewhere/minecraftWorlds/w",
    "com.mojang/minecraftpe/x",
])
def test_remove_refuses_anything_but_an_item_folder(store, tmp_path,
                                                    relative):
    folder = tmp_path / relative
    folder.mkdir(parents=True)
    with pytest.raises(catalog.CatalogError):
        catalog.remove(folder)
    assert folder.exists()


def test_old_trash_is_emptied(store):
    trash = store / "trash"
    old = trash / "old"
    new = trash / "new"
    old.mkdir(parents=True)
    new.mkdir()
    ancient = time.time() - 40 * 86400
    os.utime(old, (ancient, ancient))
    catalog._prune_trash()
    assert not old.exists() and new.exists()
