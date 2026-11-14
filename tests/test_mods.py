"""LeviLamina mods: versions, package files, installs, imports, the loader."""
# SPDX-License-Identifier: MIT

import io
import json
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from bol import mods

ROOT = Path(__file__).resolve().parents[1]
LL = mods.LEVILAMINA


# ---------------------------------------------------------------- versions

@pytest.mark.parametrize("version,version_range,expected", [
    ("1.4.0", "1.4.*", True),
    ("1.5.0", "1.4.*", False),
    ("26.60.6", "26.60.*", True),
    ("26.51.9", "26.60.*", False),
    ("26.60.5-client.13", "26.60.5-client.13", True),
    ("26.60.6-rc.1", "26.60.*", False),
    ("1.16.8", "1.16.8", True),
    ("1.16.9", "1.16.8", False),
    ("1.9.0", ">=1.0.0 <2.0.0", True),
    ("2.0.0", ">=1.0.0 <2.0.0", False),
    ("26.51.3", ">=26.51.0 <26.52.0", True),
    ("1.2.3", "^1.0", True),
    ("2.0.0", "^1.0", False),
    ("0.23.1", "0.23.*", True),
    ("3.0.0", "1.x || 3.x", True),
    ("1.0.0", "*", True),
])
def test_ranges(version, version_range, expected):
    assert mods.satisfies(version, version_range) is expected


def test_versions_sort_newest_first_and_releases_after_their_candidates():
    assert mods.sort_versions(["1.0.0", "1.0.0-rc.1", "1.10.0", "1.9.0"]) \
        == ["1.10.0", "1.9.0", "1.0.0", "1.0.0-rc.1"]


def test_a_build_with_a_padded_fourth_number_is_the_same_build():
    assert mods.same_build("1.26.60.05", "1.26.60.5")
    assert not mods.same_build("1.26.60.05", "1.26.63.1")


# ---------------------------------------------------------------- TOML

LIP_TOML = '''
format = 4
id = "github.com/Example/Mod"
version = "0.2.0-beta.1"

[info]
name = "Mod"   # a comment
tags = ["utility", 'visual']

[[variants.""]]
side = "client"
platform = "win-x64"
preserve = ["mods/Mod/config/"]

[variants."".deps]
"github.com/LiteLDev/LeviLamina#client" = "26.60.*"

[[variants."".assets]]
type = "mcmod"
urls = [
  "https://{{id}}/releases/download/v{{version}}/mod.zip",
]

[[variants."26_51"]]
side = "client"
platform = "win-x64"
deps = { "github.com/LiteLDev/LeviLamina#client" = "26.51.*" }
'''


def test_the_fallback_toml_reader_reads_what_tomllib_reads():
    expected = {
        "format": 4, "id": "github.com/Example/Mod",
        "version": "0.2.0-beta.1",
        "info": {"name": "Mod", "tags": ["utility", "visual"]},
        "variants": {
            "": [{"side": "client", "platform": "win-x64",
                  "preserve": ["mods/Mod/config/"],
                  "deps": {"github.com/LiteLDev/LeviLamina#client":
                           "26.60.*"},
                  "assets": [{"type": "mcmod", "urls": [
                      "https://{{id}}/releases/download/v{{version}}/"
                      "mod.zip"]}]}],
            "26_51": [{"side": "client", "platform": "win-x64",
                       "deps": {"github.com/LiteLDev/LeviLamina#client":
                                "26.51.*"}}],
        },
    }
    assert mods._MiniToml(LIP_TOML).parse() == expected
    assert mods.parse_toml(LIP_TOML) == expected


def test_a_lip_toml_is_read_into_client_entries():
    document = mods._from_lip_toml(mods.parse_toml(LIP_TOML))
    entry = mods.client_entry(document, "")
    assert entry["assets"][0]["type"] == "mcmod"
    assert entry["preserve"] == ["mods/Mod/config/"]
    assert mods.pick_variant(document) == ""
    assert mods.pick_variant(document, "26_51") == "26_51"


def test_a_tooth_json_merges_each_label_for_the_platform():
    tooth = {
        "tooth": "github.com/LiteLDev/LeviLamina", "version": "26.60.6",
        "variants": [
            {"platform": "win-x64",
             "dependencies": {"github.com/LiteLDev/bds": "1.26.60"},
             "assets": [{"type": "zip", "urls": ["https://x/server.zip"],
                         "placements": [{"type": "dir", "src": "LeviLamina/",
                                         "dest": "mods/LeviLamina"}]}]},
            {"label": "client", "platform": "win-x64",
             "dependencies": {"github.com/LiteLDev/PreLoader": "1.16.8"},
             "assets": [{"type": "zip", "urls": ["https://x/client.zip"],
                         "placements": [{"type": "dir", "src": "LeviLamina",
                                         "dest": "mods/LeviLamina"}]}],
             "scripts": {"post_install": [".\\PeEditor.exe -mb"]}},
            {"label": "client", "platform": "linux-*",
             "assets": [{"type": "zip", "urls": ["https://x/linux.zip"]}]},
        ],
    }
    document = mods._from_tooth(tooth)
    entry = mods.client_entry(document, "client")
    assert list(entry["deps"]) == ["github.com/LiteLDev/PreLoader"]
    assert [a["urls"] for a in entry["assets"]] == [["https://x/client.zip"]]
    # A format 3 dir placement is a directory whether or not it ends in /.
    assert entry["assets"][0]["place"] == [
        {"src": "LeviLamina/", "dest": "mods/LeviLamina/"}]
    assert entry["scripts"] is True
    assert mods.pick_variant(document) == "client"


# ---------------------------------------------------------------- install

def _zip(files):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return data.getvalue()


def _package(package_id, version, variants):
    return {"id": package_id, "version": version, "info": {},
            "variants": variants, "format": 4}


def _entry(deps=None, assets=None, preserve=None):
    return {"side": "client", "platform": "win-x64", "deps": deps or {},
            "assets": assets or [], "preserve": preserve or [], "purge": [],
            "scripts": False}


class Registry:
    """A fake levimc.org + GitHub for the resolver and the installer."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.files = {}      # (id, version) -> package document
        self.assets = {}     # url -> bytes
        self.versions = {}   # id -> [versions]

    def add(self, package_id, version, variants, assets=None):
        self.files[(package_id, version)] = _package(package_id, version,
                                                     variants)
        self.versions.setdefault(package_id, []).append(version)
        self.assets.update(assets or {})

    def download(self, urls, label, progress=None):
        url = urls[0]
        path = self.tmp / "dl" / mods._slug(url)
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(self.assets[url])
        return path, url

    def patches(self):
        return [
            mock.patch.object(mods, "package_file",
                              lambda pid, v: self.files[(pid, v)]),
            mock.patch.object(mods, "detail", lambda pid, refresh=False: None),
            mock.patch.object(mods, "package_tags",
                              lambda pid, refresh=False:
                              mods.sort_versions(self.versions[pid])),
            mock.patch.object(mods, "levilamina_versions",
                              lambda game, refresh=False:
                              ["26.60.6", "26.60.5"]
                              if mods.same_build(game, "1.26.60.5") else []),
            mock.patch.object(mods, "supported_builds",
                              lambda refresh=False: ["1.26.60.05"]),
            mock.patch.object(mods, "_download_asset", self.download),
            mock.patch.object(mods, "_refuse_while_running",
                              lambda game_dir: None),
        ]


def _game(tmp_path, version="1.26.6005"):
    game = tmp_path / "game"
    game.mkdir()
    (game / "Minecraft.Windows.exe").write_bytes(b"\xfb o encrypted")
    (game / "appxmanifest.xml").write_text(
        f'<Identity Name="Microsoft.MinecraftUWP" Version="1.26.{version.split(".")[-1]}.0"/>')
    return game


@pytest.fixture
def registry(tmp_path):
    reg = Registry(tmp_path)
    reg.add("github.com/LiteLDev/PreLoader", "1.16.8",
            {"": [_entry(assets=[{"type": "zip", "urls": ["https://pl"],
                                  "place": [{"src": "bin/PreLoader.dll",
                                             "dest": "PreLoader.dll"}]}])]},
            {"https://pl": _zip({"bin/PreLoader.dll": b"MZpreloader"})})
    for version in ("26.60.5", "26.60.6", "26.51.0"):
        reg.add(LL, version, {"client": [_entry(
            deps={"github.com/LiteLDev/PreLoader": "1.16.8",
                  "github.com/LiteLDev/PeEditor": "3.9.*"},
            assets=[{"type": "zip", "urls": [f"https://ll/{version}"],
                     "place": [{"src": "LeviLamina/",
                                "dest": "mods/LeviLamina/"}]}])]},
            {f"https://ll/{version}": _zip({
                "LeviLamina/manifest.json": json.dumps(
                    {"name": "LeviLamina", "entry": "LeviLamina.dll",
                     "type": "preload-native"}),
                "LeviLamina/LeviLamina.dll": version.encode()})})
    reg.add("github.com/Example/Old", "1.0.0", {"client": [_entry(
        deps={LL + "#client": "26.51.*"})]})
    reg.add("github.com/Example/Mod", "0.2.0", {
        "client": [_entry(deps={LL + "#client": "26.60.*"},
                          preserve=["mods/Mod/config/"],
                          assets=[{"type": "mcmod", "urls": ["https://mod"],
                                   "place": []}])]},
        {"https://mod": _zip({"manifest.json": json.dumps(
            {"name": "Mod", "entry": "Mod.dll", "type": "native"}),
            "Mod.dll": b"MZmod", "config/config.json": b"{}"})})
    reg.add("github.com/Example/Engine", "1.0.0", {
        "client": [_entry(deps={"github.com/Example/Engine#client_lua":
                                "{{version}}"})],
        "client_lua": [_entry(deps={LL: "26.60.*"}, assets=[
            {"type": "zip", "urls": ["https://lua"],
             "place": [{"src": "*.dll", "dest": "mods/Lua/"}]}])]},
        {"https://lua": _zip({"a.dll": b"a", "b.dll": b"b", "x.txt": b"x"})})
    patches = reg.patches()
    for patch in patches:
        patch.start()
    yield reg
    for patch in patches:
        patch.stop()


def test_installing_a_mod_brings_levilamina_its_preloader_and_the_loader(
        tmp_path, registry):
    game = _game(tmp_path)
    changed = mods.install(game, ["github.com/Example/Mod"], log=lambda m: 0)

    assert changed == ["github.com/LiteLDev/PreLoader", LL + "#client",
                       "github.com/Example/Mod#client"]
    assert (game / "PreLoader.dll").read_bytes() == b"MZpreloader"
    assert (game / "mods/LeviLamina/LeviLamina.dll").read_bytes() == \
        b"26.60.6"
    assert (game / "mods/Mod/Mod.dll").read_bytes() == b"MZmod"
    # PeEditor edits the executable's import table: never installed.
    assert not (game / "PeEditor.exe").exists()
    assert (game / mods.LOADER_NAME).read_bytes() == mods.loader_bytes()
    assert mods.has_levilamina(game)
    names = {i["folder"]: i for i in mods.installed(game)}
    assert names["LeviLamina"]["core"] and names["Mod"]["source"] == \
        "catalogue"


def test_a_mod_for_another_minecraft_is_refused_with_the_reason(
        tmp_path, registry):
    game = _game(tmp_path)
    with pytest.raises(mods.ModError, match="26.51"):
        mods.install(game, ["github.com/Example/Old"], log=lambda m: 0)


def test_a_build_levilamina_does_not_support_is_refused(tmp_path, registry):
    game = _game(tmp_path, version="1.26.6301")
    with pytest.raises(mods.ModError, match="does not support"):
        mods.install(game, ["github.com/Example/Mod"], log=lambda m: 0)


def test_variants_of_one_package_install_side_by_side(tmp_path, registry):
    game = _game(tmp_path)
    mods.install(game, ["github.com/Example/Engine"], log=lambda m: 0)
    state = mods.packages(game)
    assert "github.com/Example/Engine#client" in state
    assert "github.com/Example/Engine#client_lua" in state
    assert sorted(p.name for p in (game / "mods/Lua").iterdir()) == \
        ["a.dll", "b.dll"]


def test_removing_a_mod_takes_its_files_and_keeps_its_settings(
        tmp_path, registry):
    game = _game(tmp_path)
    mods.install(game, ["github.com/Example/Mod",
                        "github.com/Example/Engine"], log=lambda m: 0)
    (game / "mods/Mod/config/config.json").write_text('{"mine": 1}')

    mods.uninstall(game, "github.com/Example/Engine")
    assert not (game / "mods/Lua").exists()
    assert "github.com/Example/Engine#client_lua" not in mods.packages(game)

    mods.uninstall(game, "github.com/Example/Mod")
    assert not (game / "mods/Mod/Mod.dll").exists()
    assert (game / "mods/Mod/config/config.json").read_text() == \
        '{"mine": 1}'
    # LeviLamina was asked for with the first mod: it stays.
    assert mods.has_levilamina(game)


def test_uninstalling_levilamina_takes_every_package_and_the_loader(
        tmp_path, registry):
    game = _game(tmp_path)
    mods.install(game, ["github.com/Example/Mod"], log=lambda m: 0)
    mods.uninstall(game, LL)
    assert mods.packages(game) == {}
    assert not (game / "PreLoader.dll").exists()
    assert not (game / mods.LOADER_NAME).exists()


def test_a_package_cannot_write_outside_the_build_or_over_the_game(tmp_path):
    game = _game(tmp_path)
    for bad in ("../evil.dll", "/etc/passwd", "C:/x", ".lip/state.json",
                "Minecraft.Windows.exe", "vcruntime140_1.dll"):
        with pytest.raises(mods.ModError):
            mods._safe_target(game, bad)
    assert mods._safe_target(game, "mods/A/a.dll")[1] == "mods/A/a.dll"


# ---------------------------------------------------------------- imports

def test_importing_a_zip_with_its_mod_one_folder_down(tmp_path):
    game = _game(tmp_path)
    source = tmp_path / "cool.zip"
    source.write_bytes(_zip({
        "Cool/manifest.json": json.dumps({"name": "Cool", "entry": "c.dll",
                                          "type": "native"}),
        "Cool/c.dll": b"MZ"}))
    with mock.patch.object(mods, "_refuse_while_running", lambda d: None):
        assert mods.import_mod(game, source) == "Cool"
        with pytest.raises(mods.ModError, match="already installed"):
            mods.import_mod(game, source)
        assert mods.import_mod(game, source, overwrite=True) == "Cool"
    assert (game / "mods/Cool/c.dll").read_bytes() == b"MZ"
    assert [i["source"] for i in mods.installed(game)] == ["imported"]


def test_importing_a_dll_writes_a_manifest_for_it(tmp_path):
    game = _game(tmp_path)
    dll = tmp_path / "Solo.dll"
    dll.write_bytes(b"MZ")
    with mock.patch.object(mods, "_refuse_while_running", lambda d: None):
        assert mods.import_mod(game, dll) == "Solo"
    manifest = json.loads((game / "mods/Solo/manifest.json").read_text())
    assert manifest == {"name": "Solo", "entry": "Solo.dll",
                        "version": "0.0.0", "type": "preload-native"}


@pytest.mark.parametrize("files,reason", [
    ({"readme.txt": b"x"}, "no manifest.json"),
    ({"manifest.json": b"{"}, "not valid JSON"),
    ({"manifest.json": json.dumps({"name": "A"})}, "needs"),
    ({"manifest.json": json.dumps({"name": "A", "entry": "a.dll"})},
     "does not contain"),
    ({"manifest.json": json.dumps({"name": "A", "entry": "a.dll"}),
      "a.dll": b"", "../escape": b""}, "unsafe"),
])
def test_imports_that_are_not_a_mod_are_refused(tmp_path, files, reason):
    game = _game(tmp_path)
    source = tmp_path / "bad.zip"
    source.write_bytes(_zip(files))
    with mock.patch.object(mods, "_refuse_while_running", lambda d: None), \
            pytest.raises(mods.ModError, match=reason):
        mods.import_mod(game, source)
    assert [p.name for p in (game / "mods").iterdir()] == []


def test_a_mod_is_turned_off_by_renaming_its_manifest(tmp_path):
    game = _game(tmp_path)
    folder = game / "mods" / "A"
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text('{"name": "A"}')
    mods.set_enabled(game, "A", False)
    assert (folder / "manifest.json.close").exists()
    assert mods.installed(game)[0]["enabled"] is False
    mods.set_enabled(game, "A", True)
    assert (folder / "manifest.json").exists()


# ---------------------------------------------------------------- launch

def test_launch_puts_the_loader_in_front_only_with_levilamina_and_mods_on(
        tmp_path, registry):
    game = _game(tmp_path)
    env = {"WINEDLLOVERRIDES": "cryptbase=n,b"}
    assert mods.prepare_launch(game, env) is False
    assert env == {"WINEDLLOVERRIDES": "cryptbase=n,b"}

    mods.install(game, ["github.com/Example/Mod"], log=lambda m: 0)
    (game / mods.LOADER_NAME).unlink()
    assert mods.prepare_launch(game, env) is True
    assert env["WINEDLLOVERRIDES"] == "cryptbase=n,b;vcruntime140_1=n,b"
    assert (game / mods.LOADER_NAME).exists()

    mods.set_mods_enabled(game, False)
    env = {}
    assert mods.prepare_launch(game, env) is False
    assert not (game / mods.LOADER_NAME).exists()
    assert "WINEDLLOVERRIDES" not in env


def test_a_levilamina_left_from_another_minecraft_does_not_load(
        tmp_path, registry):
    game = _game(tmp_path)
    mods.install(game, [LL + "#client"], log=lambda m: 0)
    state = mods.load_state(game)
    state["packages"][LL + "#client"]["version"] = "26.51.0"
    mods.save_state(game, state)
    assert mods.prepare_launch(game, {}) is False
    assert not (game / mods.LOADER_NAME).exists()


def test_a_vcruntime140_1_that_is_not_ours_is_never_replaced(tmp_path):
    game = _game(tmp_path)
    (game / mods.LOADER_NAME).write_bytes(b"MZ someone else's runtime")
    with pytest.raises(mods.ModError, match="not the launcher's"):
        mods.install_loader(game)
    mods.remove_loader(game)
    assert (game / mods.LOADER_NAME).exists()


def test_the_bundled_loader_matches_its_source():
    source = (ROOT / "src" / "levi-loader.c").read_text()
    blob = (ROOT / "bol" / "levi-loader.dll").read_bytes()
    assert blob[:2] == b"MZ"
    for export in (b"__CxxFrameHandler4", b"__NLG_Dispatch2",
                   b"__NLG_Return2"):
        assert export.decode() in source
        assert export in blob
    # The markers install_loader() and the launcher's log rely on.
    assert b"bol levi-loader" in blob
    assert "PreLoader.dll" in source
    assert "PreLoader.dll".encode("utf-16le") in blob
    assert "mods\\\\bol-loader.log" in source
    # The CreateSwapChainForComposition stand-in (Playback's DXGI probe).
    assert "FACTORY_CREATE_FOR_COMPOSITION 24" in source
    assert "FACTORY_CREATE_FOR_HWND 15" in source
    assert b"composition swap chains stand in" in blob


# ---------------------------------------------------------------- try anyway

def test_try_anyway_borrows_the_nearest_supported_build(tmp_path, registry):
    game = _game(tmp_path, version="1.26.6301")
    with mock.patch.object(mods, "supported_builds",
                           lambda refresh=False: ["1.26.60.05",
                                                  "1.26.51.01"]):
        assert mods.nearest_supported("1.26.63.1") == "1.26.60.05"
        assert mods.nearest_supported("1.26.52.3") == "1.26.51.01"
        assert mods.nearest_supported("1.21.50") is None
        assert mods.effective_build(game) == ("1.26.63.1", False)
        with pytest.raises(mods.ModError, match="does not support"):
            mods.install(game, ["github.com/Example/Mod"], log=lambda m: 0)

        mods.set_force_unsupported(game, True)
        assert mods.effective_build(game) == ("1.26.60.05", True)
        mods.install(game, ["github.com/Example/Mod"], log=lambda m: 0)
        assert (game / "mods/LeviLamina/LeviLamina.dll").read_bytes() == \
            b"26.60.6"
        env = {}
        assert mods.prepare_launch(game, env) is True
        assert describe_forced(game)

        mods.set_force_unsupported(game, False)
        assert not (game / mods.LOADER_NAME).exists()
        assert mods.prepare_launch(game, {}) is False


def describe_forced(game):
    state = mods.describe(game)
    return state["forced"] and state["target_version"] == "1.26.60.05"


def test_a_windows_made_archive_with_backslashes_and_folder_entries(
        tmp_path, registry):
    """MelonRind's zip lists "MelonRind\\resource_packs\\" as an entry: it is
    a folder, not an empty file to write over the folder's place."""
    registry.add("github.com/Example/Win", "1.0.0", {"client": [_entry(
        deps={LL + "#client": "26.60.*"},
        assets=[{"type": "zip", "urls": ["https://win"],
                 "place": [{"src": "Win/", "dest": "mods/Win/"}]}])]},
        {"https://win": _zip({
            "Win\\resource_packs\\": b"",
            "Win\\manifest.json": json.dumps({"name": "Win",
                                              "entry": "Win.dll"}),
            "Win\\Win.dll": b"MZ",
            "Win\\resource_packs\\pack\\textures\\": b"",
            "Win\\resource_packs\\pack\\manifest.json": b"{}"})})
    game = _game(tmp_path)
    mods.install(game, ["github.com/Example/Win"], log=lambda m: 0)
    assert (game / "mods/Win/resource_packs").is_dir()
    assert (game / "mods/Win/resource_packs/pack/manifest.json").is_file()
    assert not (game / "mods/Win/resource_packs/pack/textures").exists()
    mode = (game / "mods/Win/Win.dll").stat().st_mode & 0o777
    assert mode & 0o044 == 0o044, oct(mode)


def test_a_failed_install_leaves_no_files_behind(tmp_path, registry):
    registry.add("github.com/Example/Bad", "1.0.0", {"client": [_entry(
        deps={LL + "#client": "26.60.*"},
        assets=[{"type": "zip", "urls": ["https://bad"],
                 "place": [{"src": "", "dest": "mods/Bad/"}]}])]},
        {"https://bad": _zip({"a.dll": b"a", "b.dll": b"b"})})
    game = _game(tmp_path)
    mods.install(game, [LL + "#client"], log=lambda m: 0)
    real_replace = mods.os.replace
    calls = []

    def failing_replace(src, dst):
        calls.append(dst)
        if str(dst).endswith("b.dll"):
            raise OSError("disk full")
        return real_replace(src, dst)

    with mock.patch.object(mods.os, "replace", failing_replace), \
            pytest.raises(OSError, match="disk full"):
        mods.install(game, ["github.com/Example/Bad"], log=lambda m: 0)
    assert not (game / "mods/Bad").exists()
    assert "github.com/Example/Bad#client" not in mods.packages(game)


# ---------------------------------------------------------------- load check

def _pe(exports=(), imports=None):
    """A minimal PE32+ image with an export and an import table."""
    import struct
    imports = imports or {}
    sect_rva, sect_raw = 0x1000, 0x400
    body = bytearray(0x3000)

    def put(off, data):
        body[off:off + len(data)] = data
        return off

    cursor = 0x10

    def alloc(data):
        nonlocal cursor
        at = cursor
        put(at, data)
        cursor += len(data) + (-len(data)) % 8
        return at
    export_rva = 0
    if exports:
        names = [alloc(n.encode() + b"\0") for n in exports]
        name_table = alloc(b"".join(struct.pack("<I", sect_rva + n)
                                    for n in names))
        ords = alloc(b"".join(struct.pack("<H", i)
                              for i in range(len(names))))
        funcs = alloc(b"\0" * 4 * len(names))
        directory = alloc(struct.pack(
            "<IIHHIIIIIII", 0, 0, 0, 0, 0, 0, len(names), len(names),
            sect_rva + funcs, sect_rva + name_table, sect_rva + ords))
        export_rva = sect_rva + directory
    import_rva = 0
    if imports:
        descriptors = []
        for dll, names in imports.items():
            hints = [alloc(b"\0\0" + n.encode() + b"\0") for n in names]
            thunks = alloc(b"".join(struct.pack("<Q", sect_rva + h)
                                    for h in hints) + b"\0" * 8)
            dll_name = alloc(dll.encode() + b"\0")
            descriptors.append(struct.pack("<IIIII", sect_rva + thunks, 0, 0,
                                           sect_rva + dll_name,
                                           sect_rva + thunks))
        table = alloc(b"".join(descriptors) + b"\0" * 20)
        import_rva = sect_rva + table
    head = bytearray(sect_raw)
    head[:2] = b"MZ"
    struct.pack_into("<I", head, 0x3C, 0x80)
    head[0x80:0x84] = b"PE\0\0"
    coff = 0x84
    struct.pack_into("<HHIIIHH", head, coff, 0x8664, 1, 0, 0, 0, 240, 0)
    opt = coff + 20
    struct.pack_into("<H", head, opt, 0x20B)
    struct.pack_into("<II", head, opt + 112, export_rva, 40)
    struct.pack_into("<II", head, opt + 120, import_rva, 40)
    sect = opt + 240
    struct.pack_into("<8sIIII", head, sect, b".data", len(body), sect_rva,
                     len(body), sect_raw)
    return bytes(head) + bytes(body)


def test_pe_reads_exports_and_imports(tmp_path):
    from bol.pe import PE
    path = tmp_path / "a.dll"
    path.write_bytes(_pe(["f", "g"], {"LeviLamina.dll": ["f", "h"]}))
    image = PE(path)
    assert image.export_names() == {"f", "g"}
    assert image.imports() == {"levilamina.dll": {"f", "h"}}


def _mod(game, folder, dll, enabled=True):
    path = game / "mods" / folder
    path.mkdir(parents=True)
    (path / f"{folder}.dll").write_bytes(dll)
    name = "manifest.json" if enabled else "manifest.json.close"
    (path / name).write_text(json.dumps({"name": folder,
                                         "entry": f"{folder}.dll"}))


def test_a_mod_missing_levilamina_functions_is_switched_off_at_launch(
        tmp_path, registry):
    game = _game(tmp_path)
    mods.install(game, [LL + "#client"], log=lambda m: 0)
    (game / "mods/LeviLamina/LeviLamina.dll").write_bytes(
        _pe(["ll::new", "ll::common"]))
    _mod(game, "Good", _pe(["good::api"], {"LeviLamina.dll":
                                            ["ll::common"]}))
    _mod(game, "Old", _pe([], {"LeviLamina.dll": ["ll::common",
                                                   "ll::gone"]}))
    _mod(game, "Needy", _pe([], {"Old.dll": ["x"], "kernel32.dll": ["y"]}))
    _mod(game, "Off", _pe([], {"LeviLamina.dll": ["ll::gone"]}),
         enabled=False)

    problems = mods.load_problems(game)
    assert set(problems) == {"Old", "Needy"}
    assert "another version of LeviLamina" in problems["Old"]
    assert "Old" in problems["Needy"]

    assert mods.prepare_launch(game, {}) is True
    states = {i["folder"]: i for i in mods.installed(game)}
    assert states["Good"]["enabled"] and not states["Old"]["enabled"]
    assert "another version" in states["Old"]["blocked"]
    # Switched back on by hand, it is no longer marked.
    mods.set_enabled(game, "Old", True)
    assert {i["folder"]: i for i in mods.installed(game)}["Old"][
        "blocked"] is None
