"""XDG and Flatpak storage regression tests."""
# SPDX-License-Identifier: MIT

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from bol import xdg_migration
from bol.xdg_migration import migrate_legacy_flatpak_data


ROOT = Path(__file__).resolve().parents[1]


def _config_snapshot(tmp_path, extra_env=None):
    env = dict(os.environ)
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "PYTHONPATH": str(ROOT),
    })
    env.pop("BOL_HOME", None)
    env.update(extra_env or {})
    code = (
        "import json; from bol import config; "
        "print(json.dumps({'data': str(config.DATA), "
        "'default': config.default_install_location(), "
        "'legacy': str(config.LEGACY_DATA), "
        "'pointer': str(config.INSTALL_LOCATION_FILE)}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env,
        text=True, capture_output=True, check=True,
    )
    return json.loads(result.stdout)


def test_config_uses_xdg_data_and_config_homes(tmp_path):
    values = _config_snapshot(tmp_path)
    assert values["data"] == str(
        tmp_path / "xdg-data" / "bedrock-on-linux"
    )
    assert values["default"] == values["data"]
    assert values["pointer"] == str(
        tmp_path / "xdg-config" / "bedrock-on-linux" / "install_location"
    )


def test_flatpak_bazzite_paths_keep_legacy_source_outside_private_xdg(
        tmp_path):
    home = Path("/var/home/player")
    private = (
        home / ".var" / "app"
        / "io.github.wyze3306.BedrockOnLinux" / "data"
    )
    values = _config_snapshot(tmp_path, {
        "HOME": str(home),
        "XDG_DATA_HOME": str(private),
        "XDG_CONFIG_HOME": str(
            home / ".var" / "app"
            / "io.github.wyze3306.BedrockOnLinux" / "config"
        ),
        "FLATPAK_ID": "io.github.wyze3306.BedrockOnLinux",
    })

    assert values["data"] == str(private / "bedrock-on-linux")
    assert values["legacy"] == str(
        home / ".local" / "share" / "bedrock-on-linux"
    )
    assert values["data"] != values["legacy"]


def test_bol_home_keeps_highest_priority_over_xdg(tmp_path):
    chosen = tmp_path / "separate-profile"
    values = _config_snapshot(tmp_path, {"BOL_HOME": str(chosen)})
    assert values["data"] == str(chosen)


def test_empty_bol_home_uses_safe_xdg_default_not_working_directory(tmp_path):
    values = _config_snapshot(tmp_path, {"BOL_HOME": ""})
    assert values["data"] == str(
        tmp_path / "xdg-data" / "bedrock-on-linux"
    )


def test_xdg_pointer_overrides_default_location(tmp_path):
    pointer = (
        tmp_path / "xdg-config" / "bedrock-on-linux" / "install_location"
    )
    pointer.parent.mkdir(parents=True)
    chosen = tmp_path / "large-drive" / "minecraft"
    pointer.write_text(str(chosen), encoding="utf-8")
    values = _config_snapshot(tmp_path)
    assert values["data"] == str(chosen)
    assert values["default"] == str(
        tmp_path / "xdg-data" / "bedrock-on-linux"
    )


def test_legacy_config_pointer_remains_effective_after_xdg_upgrade(tmp_path):
    pointer = (
        tmp_path / "home" / ".config" / "bedrock-on-linux"
        / "install_location"
    )
    pointer.parent.mkdir(parents=True)
    chosen = tmp_path / "legacy-relocated-data"
    pointer.write_text(str(chosen), encoding="utf-8")

    values = _config_snapshot(tmp_path)

    assert values["data"] == str(chosen)
    assert values["pointer"] == str(
        tmp_path / "xdg-config" / "bedrock-on-linux" / "install_location"
    )


def test_flatpak_legacy_tree_moves_before_use(tmp_path):
    old_data = tmp_path / "host-data" / "bedrock-on-linux"
    new_data = tmp_path / "flatpak-data" / "bedrock-on-linux"
    old_umu = tmp_path / "host-data" / "umu"
    new_umu = new_data / "umu"
    (old_data / "compatdata" / "pfx").mkdir(parents=True)
    (old_data / "compatdata" / "pfx" / "user.reg").write_text(
        "REGEDIT4", encoding="utf-8",
    )
    old_umu.mkdir(parents=True)
    (old_umu / "runtime-version").write_text("steamrt3")

    assert migrate_legacy_flatpak_data(
        environ={"FLATPAK_ID": "io.github.wyze3306.BedrockOnLinux"},
        info_path=tmp_path / "no-flatpak-info",
        old_data=old_data,
        new_data=new_data,
        old_umu=old_umu,
        new_umu=new_umu,
    )
    # The Flatpak's one-release transition mount is read-only, so the source
    # remains as a recovery copy while all future writes go to private XDG.
    assert old_data.is_dir()
    assert (new_data / "compatdata" / "pfx" / "user.reg").is_file()
    assert (new_umu / "runtime-version").read_text() == "steamrt3"
    assert (new_data / ".xdg-storage").is_file()
    assert (new_data / ".xdg-storage").read_text() == "copied-read-only\n"


def test_populated_xdg_tree_is_never_merged_with_legacy_data(tmp_path):
    old_data = tmp_path / "old" / "bedrock-on-linux"
    new_data = tmp_path / "new" / "bedrock-on-linux"
    old_data.mkdir(parents=True)
    new_data.mkdir(parents=True)
    (old_data / "world.dat").write_text("old")
    (new_data / "world.dat").write_text("new")

    assert not migrate_legacy_flatpak_data(
        environ={"FLATPAK_ID": "io.github.wyze3306.BedrockOnLinux"},
        info_path=tmp_path / "no-flatpak-info",
        old_data=old_data,
        new_data=new_data,
        old_umu=tmp_path / "old-umu",
        new_umu=new_data / "umu",
    )
    assert (old_data / "world.dat").read_text() == "old"
    assert (new_data / "world.dat").read_text() == "new"


def test_native_custom_xdg_copies_legacy_tree_and_keeps_recovery_backup(
        tmp_path):
    old_data = tmp_path / "old"
    new_data = tmp_path / "new"
    old_umu = tmp_path / "old-umu"
    old_data.mkdir()
    old_umu.mkdir()
    (old_data / "settings.json").write_text('{"other": "kept"}')
    (old_umu / "runtime-version").write_text("steamrt3", encoding="utf-8")
    assert migrate_legacy_flatpak_data(
        environ={},
        info_path=tmp_path / "no-flatpak-info",
        old_data=old_data,
        new_data=new_data,
        old_umu=old_umu,
        new_umu=new_data / "umu",
    )
    assert json.loads((old_data / "settings.json").read_text())["other"] == "kept"
    assert json.loads((new_data / "settings.json").read_text())["other"] == "kept"
    assert (old_umu / "runtime-version").read_text() == "steamrt3"
    assert (new_data / "umu" / "runtime-version").read_text() == "steamrt3"


def test_xdg_migration_refuses_isolated_profiles_without_splitting_roots(
        tmp_path):
    old_data = tmp_path / "legacy"
    new_data = tmp_path / "xdg" / "bedrock-on-linux"
    profile = old_data / "profiles" / "second-player"
    profile.mkdir(parents=True)
    (profile / "profile.json").write_text(
        '{"name":"Second Player"}', encoding="utf-8")
    (old_data / "games").mkdir()
    (profile / "games").symlink_to(old_data / "games",
                                   target_is_directory=True)
    (old_data / "settings.json").write_text('{"preserved":true}')

    with pytest.raises(RuntimeError, match="isolated account profiles"):
        migrate_legacy_flatpak_data(
            environ={},
            info_path=tmp_path / "no-flatpak-info",
            old_data=old_data,
            new_data=new_data,
            old_umu=tmp_path / "no-old-umu",
            new_umu=new_data / "umu",
        )

    assert not new_data.exists()
    assert json.loads(
        (old_data / "settings.json").read_text())["preserved"]
    assert (profile / "games").is_symlink()
    assert (profile / "games").resolve() == (old_data / "games").resolve()


def _staging_leftovers(destination):
    return sorted(destination.parent.glob(
        f".{destination.name}.xdg-migration-*"
    ))


def test_transactional_copy_cleans_partial_staging_after_copy_failure(
        tmp_path, monkeypatch):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    (source / "complete").write_text("source", encoding="utf-8")

    def fail_after_partial_copy(_source, partial, **_kwargs):
        Path(partial).mkdir(exist_ok=True)
        (Path(partial) / "incomplete").write_text("partial", encoding="utf-8")
        raise OSError("disk full")

    monkeypatch.setattr(xdg_migration.shutil, "copytree",
                        fail_after_partial_copy)
    with pytest.raises(OSError, match="disk full"):
        xdg_migration._copy_tree_transactionally(source, destination)

    assert not destination.exists()
    assert _staging_leftovers(destination) == []


def test_transactional_copy_cleans_staging_after_activation_failure(
        tmp_path, monkeypatch):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    (source / "complete").write_text("source", encoding="utf-8")

    def fail_activation(_source, _destination):
        raise OSError("rename failed")

    monkeypatch.setattr(xdg_migration.os, "replace", fail_activation)
    with pytest.raises(OSError, match="rename failed"):
        xdg_migration._copy_tree_transactionally(source, destination)

    assert not destination.exists()
    assert _staging_leftovers(destination) == []


def test_transactional_copy_discards_a_stale_staging_tree(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    (source / "world.dat").write_text("kept", encoding="utf-8")
    # A killed earlier run (same sandbox PID) left an incomplete copy behind.
    stale = destination.with_name(
        f".{destination.name}.xdg-migration-{os.getpid()}"
    )
    stale.mkdir()
    (stale / "incomplete").write_text("partial", encoding="utf-8")

    xdg_migration._copy_tree_transactionally(source, destination)

    assert (destination / "world.dat").read_text(encoding="utf-8") == "kept"
    assert not (destination / "incomplete").exists()
    assert not stale.exists()
    assert _staging_leftovers(destination) == []


def test_migration_recovers_from_a_stale_staging_tree(tmp_path):
    old_data = tmp_path / "legacy"
    new_data = tmp_path / "xdg" / "bedrock-on-linux"
    (old_data / "msa").mkdir(parents=True)
    (old_data / "msa" / "account.json").write_text(
        '{"gamertag":"kept"}', encoding="utf-8")
    new_data.parent.mkdir(parents=True)
    stale = new_data.with_name(
        f".{new_data.name}.xdg-migration-{os.getpid()}"
    )
    (stale / "msa").mkdir(parents=True)

    assert migrate_legacy_flatpak_data(
        environ={"FLATPAK_ID": "io.github.wyze3306.BedrockOnLinux"},
        info_path=tmp_path / "no-flatpak-info",
        old_data=old_data,
        new_data=new_data,
        old_umu=tmp_path / "no-old-umu",
        new_umu=new_data / "umu",
    )

    assert json.loads(
        (new_data / "msa" / "account.json").read_text())["gamertag"] == "kept"
    assert not stale.exists()
    assert _staging_leftovers(new_data) == []


def test_migration_reanchors_game_dir_and_internal_content_link(tmp_path):
    old_data = tmp_path / "legacy"
    new_data = tmp_path / "xdg" / "bedrock-on-linux"
    game = old_data / "games" / "1.2.3" / "package"
    resource_packs = game / "resource_packs"
    resource_packs.mkdir(parents=True)
    (game / "Minecraft.Windows.exe").write_text("exe")
    (resource_packs / "pack.mcpack").write_text("pack")
    (old_data / "settings.json").write_text(json.dumps({
        "game_dir": str(game.resolve()),
        "other": "preserved",
    }))
    (old_data / "content").symlink_to(resource_packs.resolve())

    assert migrate_legacy_flatpak_data(
        environ={},
        info_path=tmp_path / "no-flatpak-info",
        old_data=old_data,
        new_data=new_data,
        old_umu=tmp_path / "no-old-umu",
        new_umu=new_data / "umu",
    )

    expected_game = new_data / "games" / "1.2.3" / "package"
    settings = json.loads((new_data / "settings.json").read_text())
    assert settings["game_dir"] == str(expected_game.resolve())
    assert settings["other"] == "preserved"
    assert (new_data / "content").is_symlink()
    assert (new_data / "content").resolve() == (
        expected_game / "resource_packs"
    ).resolve()
    assert (new_data / "content" / "pack.mcpack").read_text() == "pack"


def test_settings_symlink_is_materialized_without_modifying_external_target(
        tmp_path):
    old_data = tmp_path / "legacy"
    new_data = tmp_path / "xdg" / "bedrock-on-linux"
    external = tmp_path / "external-settings.json"
    game = old_data / "games" / "1.2.3" / "payload"
    game.mkdir(parents=True)
    original = json.dumps({
        "game_dir": str(game.resolve()),
        "external": "must stay unchanged",
    })
    external.write_text(original, encoding="utf-8")
    (old_data / "settings.json").symlink_to(external)

    assert migrate_legacy_flatpak_data(
        environ={},
        info_path=tmp_path / "no-flatpak-info",
        old_data=old_data,
        new_data=new_data,
        old_umu=tmp_path / "no-old-umu",
        new_umu=new_data / "umu",
    )

    assert external.read_text(encoding="utf-8") == original
    assert (old_data / "settings.json").is_symlink()
    copied = new_data / "settings.json"
    assert copied.is_file()
    assert not copied.is_symlink()
    values = json.loads(copied.read_text(encoding="utf-8"))
    assert values["game_dir"] == str(
        (new_data / "games" / "1.2.3" / "payload").resolve()
    )


def test_reanchor_failure_never_activates_destination(tmp_path, monkeypatch):
    old_data = tmp_path / "legacy"
    new_data = tmp_path / "xdg" / "bedrock-on-linux"
    old_data.mkdir()
    (old_data / "settings.json").write_text(
        '{"game_dir": "/legacy/games/version"}',
        encoding="utf-8",
    )

    monkeypatch.setattr(
        xdg_migration,
        "_reanchor_copied_paths",
        mock.Mock(side_effect=OSError("reanchor failed")),
    )
    with pytest.raises(OSError, match="reanchor failed"):
        migrate_legacy_flatpak_data(
            environ={},
            info_path=tmp_path / "no-flatpak-info",
            old_data=old_data,
            new_data=new_data,
            old_umu=tmp_path / "no-old-umu",
            new_umu=new_data / "umu",
        )

    assert not new_data.exists()
    assert (old_data / "settings.json").is_file()
    assert not list(new_data.parent.glob(
        ".bedrock-on-linux.xdg-migration-*"
    ))


def test_same_directory_through_symlink_is_a_migration_noop(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    (actual / "world.dat").write_text("kept", encoding="utf-8")
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)

    assert not migrate_legacy_flatpak_data(
        environ={},
        info_path=tmp_path / "no-flatpak-info",
        old_data=alias,
        new_data=actual,
        old_umu=tmp_path / "no-old-umu",
        new_umu=actual / "umu",
    )
    assert (actual / "world.dat").read_text(encoding="utf-8") == "kept"


def test_relocated_install_location_is_not_migrated_into(tmp_path):
    # Relocation moves the user's data and leaves the engine, caches and logs
    # in the old root. Reading that as legacy data put the migration lock
    # beside the chosen folder, whose parent the user could not write to, and
    # the launcher refused every start (#255).
    home = tmp_path / "home"
    leftovers = home / ".local" / "share" / "bedrock-on-linux" / "cache"
    leftovers.mkdir(parents=True)
    (leftovers / "engine.tar.gz").write_bytes(b"left behind")
    mount = tmp_path / "media" / "player"
    chosen = mount / "bedrock-on-linux"
    chosen.mkdir(parents=True)
    (chosen / "settings.json").write_text("{}", encoding="utf-8")
    pointer = tmp_path / "xdg-config" / "bedrock-on-linux" / "install_location"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(str(chosen), encoding="utf-8")
    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "PYTHONPATH": str(ROOT),
    })
    for name in ("BOL_HOME", "XDG_DATA_HOME", "FLATPAK_ID"):
        env.pop(name, None)

    mount.chmod(0o555)
    try:
        result = subprocess.run(
            [sys.executable, "-c",
             "from bol.util import _ensure_xdg_storage; "
             "_ensure_xdg_storage(); print('ready')"],
            cwd=ROOT, env=env, text=True, capture_output=True,
        )
    finally:
        mount.chmod(0o755)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "ready" in result.stdout
    assert "legacy" not in result.stdout.lower()
    assert not (mount / ".bedrock-on-linux-xdg-migration.lock").exists()
    assert sorted(path.name for path in chosen.iterdir()) == ["settings.json"]
    assert (leftovers / "engine.tar.gz").read_bytes() == b"left behind"


def test_cli_migrates_before_first_flatpak_profile_command(tmp_path):
    home = tmp_path / "home"
    old_data = home / ".local" / "share" / "bedrock-on-linux"
    new_data = tmp_path / "private-data" / "bedrock-on-linux"
    (old_data / "msa").mkdir(parents=True)
    (old_data / "msa" / "account.json").write_text('{"gamertag":"kept"}')
    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "XDG_DATA_HOME": str(tmp_path / "private-data"),
        "XDG_CONFIG_HOME": str(tmp_path / "private-config"),
        "FLATPAK_ID": "io.github.wyze3306.BedrockOnLinux",
        "PYTHONPATH": str(ROOT),
    })
    env.pop("BOL_HOME", None)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "sys.argv=['bedrock-on-linux','profiles','create','Alice']; "
                "from bol.cli import main; main()"
            ),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )

    # Profile shortcuts are intentionally refused in Flatpak, but the guarded
    # migration happens first and therefore cannot be blocked by a new root.
    assert result.returncode == 1
    assert "cannot be installed from the Flatpak" in result.stdout
    assert json.loads(
        (new_data / "msa" / "account.json").read_text()
    )["gamertag"] == "kept"
    assert not (new_data / "profiles").exists()


def test_flatpak_manifest_mounts_exact_legacy_home_path_read_only():
    manifest = (
        ROOT / "flatpak" / "io.github.wyze3306.BedrockOnLinux.yml"
    ).read_text(encoding="utf-8")
    assert "--filesystem=~/.local/share/bedrock-on-linux:ro" in manifest
    assert "--filesystem=xdg-data/bedrock-on-linux" not in manifest
    assert "--filesystem=xdg-data/umu" not in manifest
    assert "--filesystem=~/.steam" not in manifest
