"""Launcher-side Minecraft content import regressions."""
# SPDX-License-Identifier: MIT

import json
import os
import zipfile

import pytest

from bol import content
from bol.log import BolError

_USERS_RELATIVE = (
    "drive_c/users/steamuser/AppData/Roaming/Minecraft Bedrock/Users"
)


def _signed_in(prefix, account="15576315838024289709", played=None):
    """A prefix in which the game has run once signed in as *account*.

    Returns that account's com.mojang folder — the one the game reads its
    own worlds, templates and skins from.
    """
    base = prefix / _USERS_RELATIVE / account / "games" / "com.mojang"
    options = base / "minecraftpe" / "options.txt"
    options.parent.mkdir(parents=True, exist_ok=True)
    options.write_text("gfx_viewdistance:96\n")
    if played is not None:
        os.utime(options, (played, played))
    return base


def _archive(path, manifest, *members):
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("manifest.json", json.dumps(manifest))
        for member in members:
            package.writestr(member, "")
    return path


def _template(path, name="Aether Legends"):
    """A .mctemplate archive, laid out the way the game exports one."""
    return _archive(path, {
        "format_version": 2,
        "header": {
            "name": name,
            "description": "test",
            "uuid": "0f2f1cbe-2b06-4f5a-9d33-6cf1cd1b21c0",
            "version": [1, 0, 0],
            "base_game_version": [1, 21, 0],
        },
        "modules": [{
            "type": "world_template",
            "uuid": "5d5f4e33-9f0f-4c6b-8e39-1f1f8f2c5c88",
            "version": [1, 0, 0],
        }],
    }, "level.dat", "levelname.txt", "db/CURRENT")


def test_mcskin_archive_is_installed_as_a_skin_pack(tmp_path):
    archive = tmp_path / "example.mcskin"
    manifest = {
        "format_version": 2,
        "header": {
            "name": "Example Skin",
            "description": "test",
            "uuid": "a598d32f-af25-4baa-8e03-b0115d761709",
            "version": [1, 0, 0],
            "min_engine_version": [1, 20, 0],
        },
        "modules": [{
            "type": "skin_pack",
            "uuid": "db908a93-7055-46a6-b716-7d4b6f6fa334",
            "version": [1, 0, 0],
        }],
    }
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("manifest.json", json.dumps(manifest))
        package.writestr("skins.json", "{}")

    prefix = tmp_path / "prefix"
    result = content.import_content(archive, prefix=prefix)

    destination = (
        content._mojang_dir(prefix) / "skin_packs" / "Example Skin"
    )
    assert result == ["skin pack: Example Skin"]
    assert json.loads((destination / "manifest.json").read_text()) == manifest


def test_world_template_is_imported_for_the_signed_in_account(tmp_path):
    """#188: a template under Users/Shared is invisible to a signed-in game.

    The game reads templates only from the folder of the account it is
    signed in as, so importing into the shared folder left the player with a
    template list holding nothing but their Marketplace purchases.
    """
    prefix = tmp_path / "prefix"
    account = _signed_in(prefix)
    archive = _template(tmp_path / "Aether Legends.mctemplate")

    result = content.import_content(archive, prefix=prefix)

    assert result == ["world template: Aether Legends"]
    assert (account / "world_templates" / "Aether Legends"
            / "manifest.json").is_file()
    assert not (content._mojang_dir(prefix) / "world_templates").exists()


def test_world_is_imported_for_the_signed_in_account(tmp_path):
    """Worlds are per-account for the same reason templates are."""
    prefix = tmp_path / "prefix"
    account = _signed_in(prefix)
    archive = _template(tmp_path / "One Block.mcworld", name="One Block")

    result = content.import_content(archive, prefix=prefix)

    assert result == ["world: One Block"]
    assert (account / "minecraftWorlds" / "One Block" / "level.dat").is_file()
    assert not (content._mojang_dir(prefix) / "minecraftWorlds").exists()


def test_packs_stay_in_the_folder_shared_between_accounts(tmp_path):
    """Packs are not per-account: the game reads them from Users/Shared."""
    prefix = tmp_path / "prefix"
    account = _signed_in(prefix)
    archive = _archive(tmp_path / "example.mcpack", {
        "format_version": 2,
        "header": {
            "name": "Example Pack",
            "description": "test",
            "uuid": "0cbbb1a2-27fd-45f4-a1de-2a1f43dcb6f2",
            "version": [1, 0, 0],
        },
        "modules": [{
            "type": "resources",
            "uuid": "9a4b4a41-1f8f-4a1e-9e30-8f26a3b19d21",
            "version": [1, 0, 0],
        }],
    }, "pack_icon.png")

    result = content.import_content(archive, prefix=prefix)

    assert result == ["resource pack: Example Pack"]
    assert (content._mojang_dir(prefix) / "resource_packs" / "Example Pack"
            / "manifest.json").is_file()
    assert not (account / "resource_packs").exists()


def test_import_falls_back_to_shared_before_the_first_launch(tmp_path):
    """With no account folder yet there is nothing to prefer over Shared."""
    prefix = tmp_path / "prefix"
    archive = _template(tmp_path / "Aether Legends.mctemplate")

    content.import_content(archive, prefix=prefix)

    assert (content._mojang_dir(prefix) / "world_templates" / "Aether Legends"
            / "manifest.json").is_file()


def test_content_left_in_the_shared_folder_is_reported(tmp_path, capsys):
    """An install that imported before this fix still has the old copies."""
    prefix = tmp_path / "prefix"
    _signed_in(prefix)
    stranded = content._mojang_dir(prefix) / "world_templates" / "Old Import"
    stranded.mkdir(parents=True)
    archive = _template(tmp_path / "Aether Legends.mctemplate")

    content.import_content(archive, prefix=prefix)

    assert "Old Import" in capsys.readouterr().out
    assert stranded.is_dir()


def test_the_account_that_played_last_gets_the_import(tmp_path):
    """Several accounts can share a prefix; only one of them is playing."""
    prefix = tmp_path / "prefix"
    stale = _signed_in(prefix, "11111111111111111111", played=1_000_000)
    current = _signed_in(prefix, "22222222222222222222", played=2_000_000)
    archive = _template(tmp_path / "Aether Legends.mctemplate")

    content.import_content(archive, prefix=prefix)

    assert (current / "world_templates" / "Aether Legends"
            / "manifest.json").is_file()
    assert not (stale / "world_templates").exists()


def test_preview_imports_into_its_own_shared_folder(tmp_path):
    from unittest import mock

    from bol import perfcheck
    with mock.patch.object(perfcheck, "_launched_edition",
                           return_value="preview"):
        folder = content._mojang_dir(tmp_path)
    assert "/Minecraft Bedrock Preview/Users/Shared/" in str(folder)
    with mock.patch.object(perfcheck, "_launched_edition", return_value=None):
        assert "/Minecraft Bedrock/Users/Shared/" in str(
            content._mojang_dir(tmp_path))


def _pack_manifest(name, module_type):
    return {
        "format_version": 2,
        "header": {"name": name, "description": "test",
                   "uuid": "11111111-2222-3333-4444-%012d" % len(name),
                   "version": [1, 0, 0]},
        "modules": [{"type": module_type,
                     "uuid": "55555555-6666-7777-8888-%012d" % len(name),
                     "version": [1, 0, 0]}],
    }


def test_an_addon_made_of_mcpack_files_installs_each_pack(tmp_path):
    """A .mcaddon is a zip of .mcpack files, not of the packs' folders."""
    prefix = tmp_path / "prefix"
    bp = _archive(tmp_path / "Thing_BP.mcpack",
                  _pack_manifest("Thing BP", "data"))
    rp = _archive(tmp_path / "Thing_RP.mcpack",
                  _pack_manifest("Thing RP", "resources"))
    addon = tmp_path / "Thing.mcaddon"
    with zipfile.ZipFile(addon, "w") as package:
        package.write(bp, "Thing_BP.mcpack")
        package.write(rp, "Thing_RP.mcpack")
    destinations = []

    result = content.import_content(addon, prefix=prefix,
                                    destinations=destinations)

    shared = content._mojang_dir(prefix)
    assert sorted(result) == ["behavior pack: Thing BP",
                              "resource pack: Thing RP"]
    assert (shared / "behavior_packs" / "Thing BP" / "manifest.json").is_file()
    assert (shared / "resource_packs" / "Thing RP" / "manifest.json").is_file()
    assert sorted(p.name for p in destinations) == ["Thing BP", "Thing RP"]


def test_a_world_bundled_in_an_addon_is_imported_as_a_world(tmp_path):
    prefix = tmp_path / "prefix"
    account = _signed_in(prefix)
    world = _template(tmp_path / "Arena.mcworld", name="Arena")
    pack = _archive(tmp_path / "Arena_BP.mcpack",
                    _pack_manifest("Arena BP", "data"))
    addon = tmp_path / "Arena.mcaddon"
    with zipfile.ZipFile(addon, "w") as package:
        package.write(world, "Arena.mcworld")
        package.write(pack, "Arena_BP.mcpack")

    result = content.import_content(addon, prefix=prefix)

    assert "world: Arena" in result
    assert "behavior pack: Arena BP" in result
    assert (account / "minecraftWorlds" / "Arena" / "level.dat").is_file()


def test_a_world_zipped_inside_its_own_folder_is_lifted_out(tmp_path):
    """The game only lists a world whose level.dat is at the top."""
    prefix = tmp_path / "prefix"
    account = _signed_in(prefix)
    archive = tmp_path / "Castle.mcworld"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("Castle Map/level.dat", "")
        package.writestr("Castle Map/levelname.txt", "Castle")
        package.writestr("Castle Map/db/CURRENT", "")

    content.import_content(archive, prefix=prefix)

    world = account / "minecraftWorlds" / "Castle"
    assert (world / "level.dat").is_file()
    assert (world / "db" / "CURRENT").is_file()
    assert not (world / "Castle Map").exists()


def test_colour_codes_do_not_end_up_in_folder_names(tmp_path):
    prefix = tmp_path / "prefix"
    archive = _archive(tmp_path / "x.mcpack",
                       _pack_manifest("§cPart §bII §lTools", "data"))

    result = content.import_content(archive, prefix=prefix)

    assert result == ["behavior pack: Part II Tools"]


def test_a_pack_named_through_its_translations_gets_its_real_name(tmp_path):
    prefix = tmp_path / "prefix"
    archive = tmp_path / "t.mcpack"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("manifest.json", json.dumps(
            _pack_manifest("pack.name", "resources")))
        package.writestr("texts/en_US.lang",
                         "pack.name=Primal Tamers\t#\npack.description=x\n")

    result = content.import_content(archive, prefix=prefix)

    assert result == ["resource pack: Primal Tamers"]


def _world_zip(path, folder="", extra=()):
    with zipfile.ZipFile(path, "w") as package:
        package.writestr(folder + "level.dat", "")
        package.writestr(folder + "levelname.txt", "Fish")
        package.writestr(folder + "db/CURRENT", "")
        for member in extra:
            package.writestr(folder + member, "{}")
    return path


def test_a_web_page_saved_as_mcworld_is_named_for_what_it_is(tmp_path):
    prefix = tmp_path / "prefix"
    page = tmp_path / "LE FISH (24).mcworld"
    page.write_text("<!DOCTYPE html><html><head><title>Download</title>")

    with pytest.raises(BolError, match="web page"):
        content.import_content(page, prefix=prefix)


def test_a_download_cut_short_is_named_for_what_it_is(tmp_path):
    prefix = tmp_path / "prefix"
    whole = _world_zip(tmp_path / "whole.mcworld").read_bytes()
    cut = tmp_path / "cut.mcworld"
    cut.write_bytes(whole[:len(whole) // 2])
    empty = tmp_path / "empty.mcpack"
    empty.write_bytes(b"")

    with pytest.raises(BolError, match="cut short"):
        content.import_content(cut, prefix=prefix)
    with pytest.raises(BolError, match="empty"):
        content.import_content(empty, prefix=prefix)


def test_a_zip_with_data_after_its_end_is_still_imported(tmp_path):
    prefix = tmp_path / "prefix"
    account = _signed_in(prefix)
    padded = tmp_path / "Padded.mcworld"
    padded.write_bytes(_world_zip(tmp_path / "w.zip").read_bytes()
                       + b"\0" * 200_000)
    assert not zipfile.is_zipfile(padded)

    assert content.import_content(padded, prefix=prefix) == ["world: Padded"]
    assert (account / "minecraftWorlds" / "Padded" / "level.dat").is_file()


def test_a_world_in_a_plain_zip_is_imported_as_a_world(tmp_path):
    prefix = tmp_path / "prefix"
    account = _signed_in(prefix)
    archive = _world_zip(tmp_path / "Lava Fishing.zip", folder="Lava/",
                         extra=["behavior_packs/bp/manifest.json"])

    result = content.import_content(archive, prefix=prefix)

    # The world's own packs stay inside it: no separate behaviour pack.
    assert result == ["world: Lava Fishing"]
    world = account / "minecraftWorlds" / "Lava Fishing"
    assert (world / "behavior_packs" / "bp" / "manifest.json").is_file()


def test_an_unpacked_folder_is_imported(tmp_path):
    prefix = tmp_path / "prefix"
    account = _signed_in(prefix)
    world = tmp_path / "My Map"
    (world / "db").mkdir(parents=True)
    (world / "level.dat").write_bytes(b"")
    pack = tmp_path / "Cool Pack"
    pack.mkdir()
    (pack / "manifest.json").write_text(json.dumps(
        _pack_manifest("Cool Pack", "resources")))

    assert content.import_content(world, prefix=prefix) == ["world: My Map"]
    assert content.import_content(pack, prefix=prefix) == \
        ["resource pack: Cool Pack"]
    assert (account / "minecraftWorlds" / "My Map" / "level.dat").is_file()
    # The original folders are copied, not moved.
    assert (world / "level.dat").is_file() and (pack / "manifest.json").is_file()


def test_something_that_is_no_minecraft_content_says_where_to_look(tmp_path):
    prefix = tmp_path / "prefix"
    archive = tmp_path / "sodium.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("fabric.mod.json", "{}")

    with pytest.raises(BolError, match="no manifest.json"):
        content.import_content(archive, prefix=prefix)


def test_java_edition_mods_are_named_as_such(tmp_path):
    archive = tmp_path / "Lava Fishing-EXTRACT_ME.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("lavafishing-1.21.1-all.jar", "PK")

    with pytest.raises(BolError, match="Java Edition"):
        content.import_content(archive, prefix=tmp_path / "prefix")


def test_a_levilamina_mod_or_a_dll_is_sent_to_the_mods_page(tmp_path):
    prefix = tmp_path / "prefix"
    archive = tmp_path / "LeviOptimize.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("LeviOptimize/manifest.json", json.dumps(
            {"name": "LeviOptimize", "entry": "LeviOptimize.dll",
             "type": "native"}))
        package.writestr("LeviOptimize/LeviOptimize.dll", "MZ")
    dll = tmp_path / "client.mcpack"
    dll.write_bytes(b"MZ\x90\x00" + b"\0" * 100)

    with pytest.raises(BolError, match="LeviLamina mod"):
        content.import_content(archive, prefix=prefix)
    with pytest.raises(BolError, match="Windows program"):
        content.import_content(dll, prefix=prefix)
    # Nothing landed among the packs.
    assert not (content._mojang_dir(prefix) / "resource_packs").exists()
