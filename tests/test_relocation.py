# tests/test_relocation.py
# SPDX-License-Identifier: MIT
"""Tests for the data directory relocation feature."""
import json
import os
import errno
import shutil
import sys
from pathlib import Path

# Add project root to sys.path so 'bol' can be imported
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import pytest

from bol import config
from bol import relocation
from bol.relocation import migrate_data, paths_overlap, RelocationError


def test_default_install_location():
    """The default location should be ~/.local/share/bedrock-on-linux."""
    expected = str(Path.home() / ".local/share" / "bedrock-on-linux")
    assert config.default_install_location() == expected


def test_is_relocation_allowed(monkeypatch):
    """Relocation should be allowed unless BOL_HOME is set."""
    monkeypatch.delenv("BOL_HOME", raising=False)
    assert config.is_relocation_allowed() is True
    monkeypatch.setenv("BOL_HOME", "/some/path")
    assert config.is_relocation_allowed() is False


def test_set_and_clear_install_location(monkeypatch, tmp_path):
    """set_install_location writes the pointer file; clear removes it."""
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(
        config,
        "INSTALL_LOCATION_FILE",
        tmp_path / ".config" / "bedrock-on-linux" / "install_location"
    )
    monkeypatch.delenv("BOL_HOME", raising=False)

    test_path = tmp_path / "my_custom_data"
    config.set_install_location(str(test_path))

    pointer_file = config.INSTALL_LOCATION_FILE
    assert pointer_file.exists()
    assert pointer_file.read_text().strip() == str(test_path)

    config.clear_install_location()
    assert not pointer_file.exists()


def test_setting_xdg_location_retires_legacy_pointer(monkeypatch, tmp_path):
    xdg_pointer = (
        tmp_path / "xdg-config" / "bedrock-on-linux" / "install_location"
    )
    legacy_pointer = (
        tmp_path / ".config" / "bedrock-on-linux" / "install_location"
    )
    legacy_pointer.parent.mkdir(parents=True)
    legacy_pointer.write_text("/old/location")
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(config, "INSTALL_LOCATION_FILE", xdg_pointer)
    monkeypatch.delenv("BOL_HOME", raising=False)

    config.set_install_location("/new/location")

    assert xdg_pointer.read_text() == "/new/location"
    assert not legacy_pointer.exists()


def test_clear_location_removes_legacy_fallback(monkeypatch, tmp_path):
    xdg_pointer = (
        tmp_path / "xdg-config" / "bedrock-on-linux" / "install_location"
    )
    legacy_pointer = (
        tmp_path / ".config" / "bedrock-on-linux" / "install_location"
    )
    xdg_pointer.parent.mkdir(parents=True)
    legacy_pointer.parent.mkdir(parents=True)
    xdg_pointer.write_text("/new/location")
    legacy_pointer.write_text("/old/location")
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(config, "INSTALL_LOCATION_FILE", xdg_pointer)
    monkeypatch.delenv("BOL_HOME", raising=False)

    config.clear_install_location()

    assert not xdg_pointer.exists()
    assert not legacy_pointer.exists()


def test_set_install_location_raises_when_bol_home_is_set(monkeypatch):
    monkeypatch.setenv("BOL_HOME", "/some/path")
    with pytest.raises(RuntimeError, match="Cannot change location when BOL_HOME is set externally"):
        config.set_install_location("/dummy/path")


def test_clear_install_location_raises_when_bol_home_is_set(monkeypatch):
    monkeypatch.setenv("BOL_HOME", "/some/path")
    with pytest.raises(RuntimeError, match="Cannot change location when BOL_HOME is set externally"):
        config.clear_install_location()


def test_get_install_location():
    assert config.get_install_location() == str(config.DATA)


# ===== helpers =====

def _make_user_data(base: Path, version="1.21.0"):
    """Populate `base` with a realistic user-data layout."""
    games_dir = base / "games" / version
    games_dir.mkdir(parents=True)
    (games_dir / "Minecraft.Windows.exe").write_text("exe")
    (games_dir / "test.txt").write_text("game file")

    (base / "compatdata" / "pfx").mkdir(parents=True)
    (base / "compatdata" / "pfx" / "test.txt").write_text("prefix file")

    (base / "content").mkdir()
    (base / "content" / "test.txt").write_text("content file")

    (base / "msa").mkdir()
    (base / "msa" / "token.json").write_text('{"token": "test"}')

    (base / "settings.json").write_text(json.dumps({
        "game_dir": str(games_dir),
        "other": "value",
    }))
    return games_dir


def _make_user_data_with_internal_content_symlink(base: Path, version="1.21.0"):
    """Same as _make_user_data, but "content" is a symlink pointing
    inside the games dir (e.g. content -> games/<version>/resource_packs)
    instead of a real directory."""
    games_dir = _make_user_data(base, version)
    internal_target = games_dir / "resource_packs"
    internal_target.mkdir()
    (internal_target / "pack.mcpack").write_text("internal pack")

    content_link = base / "content"
    shutil.rmtree(content_link)
    content_link.symlink_to(internal_target)
    return games_dir


# ===== paths_overlap =====

def test_paths_overlap_same_dir(tmp_path):
    a = tmp_path / "data"
    a.mkdir()
    assert paths_overlap(a, a) is True


def test_paths_overlap_nested_new_inside_old(tmp_path):
    old_dir = tmp_path / "data"
    old_dir.mkdir()
    new_dir = old_dir / "sub" / "deeper"
    assert paths_overlap(old_dir, new_dir) is True


def test_paths_overlap_nested_old_inside_new(tmp_path):
    new_dir = tmp_path / "data"
    new_dir.mkdir()
    old_dir = new_dir / "sub"
    old_dir.mkdir()
    assert paths_overlap(old_dir, new_dir) is True


def test_paths_overlap_unrelated_dirs(tmp_path):
    a = tmp_path / "old"
    b = tmp_path / "new"
    a.mkdir()
    assert paths_overlap(a, b) is False


def test_migrate_data_rejects_overlap(tmp_path, monkeypatch):
    install_file = tmp_path / ".config" / "bedrock-on-linux" / "install_location"
    monkeypatch.setattr(config, "INSTALL_LOCATION_FILE", install_file)
    monkeypatch.setattr(relocation, "INSTALL_LOCATION_FILE", install_file)
    old_dir = tmp_path / "data"
    old_dir.mkdir()
    with pytest.raises(RelocationError):
        migrate_data(old_dir, old_dir / "nested")


def test_migration_refuses_to_break_existing_profile_links(
        tmp_path, monkeypatch):
    install_file = (
        tmp_path / ".config" / "bedrock-on-linux" / "install_location"
    )
    monkeypatch.setattr(config, "INSTALL_LOCATION_FILE", install_file)
    monkeypatch.setattr(relocation, "INSTALL_LOCATION_FILE", install_file)
    old_dir = tmp_path / "old"
    new_dir = tmp_path / "new"
    metadata = old_dir / "profiles" / "alice" / "profile.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text('{"name": "Alice", "slug": "alice"}')
    (old_dir / "games").mkdir()

    with pytest.raises(RelocationError, match="isolated account profiles"):
        migrate_data(old_dir, new_dir)

    assert (old_dir / "games").is_dir()
    assert not new_dir.exists()
    assert not install_file.exists()


def test_migration_ignores_profile_creation_lock_without_metadata(
        tmp_path, monkeypatch):
    install_file = (
        tmp_path / ".config" / "bedrock-on-linux" / "install_location"
    )
    monkeypatch.setattr(config, "INSTALL_LOCATION_FILE", install_file)
    monkeypatch.setattr(relocation, "INSTALL_LOCATION_FILE", install_file)
    old_dir = tmp_path / "old"
    old_dir.mkdir()
    _make_user_data(old_dir)
    profiles = old_dir / "profiles"
    profiles.mkdir()
    (profiles / ".alice.create.lock").write_text("")
    # A failed creation can also leave an uncommitted directory. Without its
    # atomically written profile.json it is not listed or launchable.
    (profiles / "alice").mkdir()

    new_dir = tmp_path / "new"
    migrate_data(old_dir, new_dir)

    assert (new_dir / "games" / "1.21.0"
            / "Minecraft.Windows.exe").is_file()
    assert install_file.read_text().strip() == str(new_dir)


# ===== end-to-end migration test (calls the real production code) =====

def test_migration_full_workflow(tmp_path, monkeypatch):
    install_file = tmp_path / ".config" / "bedrock-on-linux" / "install_location"
    monkeypatch.setattr(config, "INSTALL_LOCATION_FILE", install_file)
    monkeypatch.setattr(relocation, "INSTALL_LOCATION_FILE", install_file)

    old_dir = tmp_path / "old_data"
    old_dir.mkdir()
    games_dir = _make_user_data(old_dir)
    gpu_marker = b'{"version":2,"phase":"running"}\n'
    gpu_ack = b'{"version":2,"acknowledged":true}\n'
    (old_dir / ".gpu-launch-in-progress.json").write_bytes(gpu_marker)
    (old_dir / ".gpu-safety-ack.json").write_bytes(gpu_ack)

    new_dir = tmp_path / "new_data"
    migrate_data(old_dir, new_dir)

    new_games_dir = new_dir / "games" / games_dir.name
    assert (new_games_dir / "Minecraft.Windows.exe").exists()
    assert (new_games_dir / "test.txt").read_text() == "game file"
    assert (new_dir / "compatdata" / "pfx" / "test.txt").read_text() == "prefix file"
    assert (new_dir / "content" / "test.txt").read_text() == "content file"
    assert (new_dir / "msa" / "token.json").read_text() == '{"token": "test"}'
    assert (new_dir / ".gpu-launch-in-progress.json").read_bytes() == gpu_marker
    assert (new_dir / ".gpu-safety-ack.json").read_bytes() == gpu_ack
    assert not (old_dir / ".gpu-launch-in-progress.json").exists()
    assert not (old_dir / ".gpu-safety-ack.json").exists()

    settings = json.loads((new_dir / "settings.json").read_text())
    assert settings["game_dir"] == str(new_games_dir.resolve())
    assert settings["other"] == "value"

    assert not (old_dir / "games").exists()

    assert config.get_install_location() == str(config.DATA)  # sanity: importable
    assert install_file.read_text().strip() == str(new_dir)


def test_migration_recreates_content_symlink(tmp_path, monkeypatch):
    install_file = tmp_path / ".config" / "bedrock-on-linux" / "install_location"
    monkeypatch.setattr(config, "INSTALL_LOCATION_FILE", install_file)
    monkeypatch.setattr(relocation, "INSTALL_LOCATION_FILE", install_file)

    old_dir = tmp_path / "old_data"
    old_dir.mkdir()
    _make_user_data(old_dir)

    external = tmp_path / "external_content"
    external.mkdir()
    (external / "pack.mcpack").write_text("pack")
    content_link = old_dir / "content"
    shutil.rmtree(content_link)
    content_link.symlink_to(external)

    new_dir = tmp_path / "new_data"
    migrate_data(old_dir, new_dir)

    new_content = new_dir / "content"
    assert new_content.is_symlink()
    assert new_content.resolve() == external.resolve()
    assert (new_content / "pack.mcpack").read_text() == "pack"


def test_migration_recreates_content_symlink_pointing_inside_old_dir(tmp_path, monkeypatch):
    install_file = tmp_path / ".config" / "bedrock-on-linux" / "install_location"
    monkeypatch.setattr(config, "INSTALL_LOCATION_FILE", install_file)
    monkeypatch.setattr(relocation, "INSTALL_LOCATION_FILE", install_file)

    old_dir = tmp_path / "old_data"
    old_dir.mkdir()
    games_dir = _make_user_data_with_internal_content_symlink(old_dir)

    new_dir = tmp_path / "new_data"
    migrate_data(old_dir, new_dir)

    new_content = new_dir / "content"
    expected_target = new_dir / "games" / games_dir.name / "resource_packs"
    assert new_content.is_symlink()
    assert new_content.resolve() == expected_target.resolve()
    assert (new_content / "pack.mcpack").read_text() == "internal pack"


def test_migration_rollback_on_failure(tmp_path, monkeypatch):
    install_file = tmp_path / ".config" / "bedrock-on-linux" / "install_location"
    monkeypatch.setattr(config, "INSTALL_LOCATION_FILE", install_file)
    monkeypatch.setattr(relocation, "INSTALL_LOCATION_FILE", install_file)

    old_dir = tmp_path / "old_data"
    old_dir.mkdir()
    _make_user_data(old_dir)

    new_dir = tmp_path / "new_data"

    def boom(*a, **kw):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(relocation, "_rewrite_game_dir", boom)

    with pytest.raises(RelocationError):
        migrate_data(old_dir, new_dir)

    assert (old_dir / "games").exists()
    assert (old_dir / "compatdata" / "pfx" / "test.txt").read_text() == "prefix file"
    assert (old_dir / "content" / "test.txt").read_text() == "content file"
    assert (old_dir / "msa" / "token.json").exists()
    assert (old_dir / "settings.json").exists()

    assert install_file.read_text().strip() == str(old_dir)


def test_migration_rollback_restores_backup_when_source_move_fails(tmp_path, monkeypatch):
    install_file = tmp_path / ".config" / "bedrock-on-linux" / "install_location"
    monkeypatch.setattr(config, "INSTALL_LOCATION_FILE", install_file)
    monkeypatch.setattr(relocation, "INSTALL_LOCATION_FILE", install_file)

    old_dir = tmp_path / "old_data"
    old_dir.mkdir()
    _make_user_data(old_dir)

    new_dir = tmp_path / "new_data"
    new_dir.mkdir()
    existing_games = new_dir / "games"
    existing_games.mkdir(parents=True)
    (existing_games / "marker.txt").write_text("pre-existing destination data")

    def failing_transfer(src, dst, item):
        raise RuntimeError("simulated mid-move failure")

    # The destination's backup is taken; then moving the source fails.
    monkeypatch.setattr(relocation, "_transfer", failing_transfer)

    with pytest.raises(RelocationError):
        migrate_data(old_dir, new_dir)

    assert (new_dir / "games" / "marker.txt").read_text() == "pre-existing destination data"
    assert not (new_dir / "games.old").exists()
    assert (old_dir / "games").exists()
    assert install_file.read_text().strip() == str(old_dir)


def _across_filesystems(monkeypatch):
    """Every rename fails as it does between two filesystems."""
    def exdev(src, dst):
        raise OSError(errno.EXDEV, "Invalid cross-device link")
    monkeypatch.setattr(relocation.os, "rename", exdev)


def test_a_copy_cut_short_on_another_drive_leaves_the_data_where_it_was(
        tmp_path, monkeypatch):
    """A full destination drive used to have the partial copy moved back
    *into* the original folder (games/games)."""
    install_file = tmp_path / ".config" / "bedrock-on-linux" / "install_location"
    monkeypatch.setattr(config, "INSTALL_LOCATION_FILE", install_file)
    monkeypatch.setattr(relocation, "INSTALL_LOCATION_FILE", install_file)
    old_dir = tmp_path / "old_data"
    old_dir.mkdir()
    _make_user_data(old_dir)
    (old_dir / "games" / "release").mkdir(parents=True, exist_ok=True)
    (old_dir / "games" / "release" / "big.bin").write_bytes(b"x" * 64)
    new_dir = tmp_path / "new_data"
    _across_filesystems(monkeypatch)
    real_copy2, real_copytree = shutil.copy2, shutil.copytree

    def full_drive(src, dst, *a, **kw):
        if Path(src).name == "big.bin":
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_copy2(src, dst, *a, **kw)

    def copytree(src, dst, symlinks=False, ignore=None, copy_function=None,
                 *rest, **kw):
        # copytree recurses through the module attribute, positionally.
        return real_copytree(src, dst, symlinks, ignore, full_drive,
                             *rest, **kw)

    monkeypatch.setattr(relocation.shutil, "copytree", copytree)

    with pytest.raises(RelocationError, match="No space left"):
        migrate_data(old_dir, new_dir)

    assert (old_dir / "games" / "release" / "big.bin").read_bytes() == b"x" * 64
    assert not (old_dir / "games" / "games").exists()
    assert not (new_dir / "games").exists()
    assert install_file.read_text().strip() == str(old_dir)


def test_a_deletion_cut_short_on_another_drive_is_put_back_whole(
        tmp_path, monkeypatch):
    install_file = tmp_path / ".config" / "bedrock-on-linux" / "install_location"
    monkeypatch.setattr(config, "INSTALL_LOCATION_FILE", install_file)
    monkeypatch.setattr(relocation, "INSTALL_LOCATION_FILE", install_file)
    old_dir = tmp_path / "old_data"
    old_dir.mkdir()
    _make_user_data(old_dir)
    (old_dir / "games" / "a.bin").write_bytes(b"a")
    (old_dir / "games" / "b.bin").write_bytes(b"b")
    new_dir = tmp_path / "new_data"
    _across_filesystems(monkeypatch)
    real_rmtree = shutil.rmtree

    cut = []

    def cut_short(path, *a, **kw):
        if Path(path) == old_dir / "games" and not cut:
            cut.append(path)
            (old_dir / "games" / "a.bin").unlink()
            raise OSError(errno.EIO, "Input/output error")
        return real_rmtree(path, *a, **kw)

    monkeypatch.setattr(relocation.shutil, "rmtree", cut_short)

    with pytest.raises(RelocationError):
        migrate_data(old_dir, new_dir)

    assert (old_dir / "games" / "a.bin").read_bytes() == b"a"
    assert (old_dir / "games" / "b.bin").read_bytes() == b"b"
    assert not (new_dir / "games").exists()


def test_the_world_backups_move_with_the_data(tmp_path, monkeypatch):
    assert "backups" in relocation.DIRS_TO_MOVE


# ===== failures during the rewrite/recreate steps must be fatal =====

def test_rewrite_game_dir_raises_on_invalid_json(tmp_path):
    """_rewrite_game_dir must propagate real I/O/parse errors instead of
    swallowing them -- a corrupt settings.json is not a "success"."""
    new_dir = tmp_path / "new_data"
    new_dir.mkdir()
    (new_dir / "settings.json").write_text("not valid json{")

    with pytest.raises(json.JSONDecodeError):
        relocation._rewrite_game_dir(new_dir, tmp_path / "old_games")


def test_migration_rollback_when_symlink_recreation_fails(tmp_path, monkeypatch):
    install_file = tmp_path / ".config" / "bedrock-on-linux" / "install_location"
    monkeypatch.setattr(config, "INSTALL_LOCATION_FILE", install_file)
    monkeypatch.setattr(relocation, "INSTALL_LOCATION_FILE", install_file)

    old_dir = tmp_path / "old_data"
    old_dir.mkdir()
    _make_user_data_with_internal_content_symlink(old_dir)

    new_dir = tmp_path / "new_data"

    def boom(*a, **kw):
        raise OSError("simulated symlink failure")

    monkeypatch.setattr(relocation, "_recreate_content_symlink", boom)

    with pytest.raises(RelocationError):
        migrate_data(old_dir, new_dir)

    assert (old_dir / "games").exists()
    assert (old_dir / "content").is_symlink()
    assert install_file.read_text().strip() == str(old_dir)


def test_migration_restores_settings_and_symlink_when_pointer_write_fails(tmp_path, monkeypatch):
    """The exact scenario Wyze3306 flagged: settings.json and the
    content symlink have ALREADY been successfully rewritten/recreated
    at new_dir, and only the final pointer write (set_install_location)
    fails. _rollback() alone would move the *rewritten* files back --
    this verifies the original, pre-relocation game_dir and symlink
    target are what's actually restored at old_dir."""
    install_file = tmp_path / ".config" / "bedrock-on-linux" / "install_location"
    monkeypatch.setattr(config, "INSTALL_LOCATION_FILE", install_file)
    monkeypatch.setattr(relocation, "INSTALL_LOCATION_FILE", install_file)  # <-- CRITICAL

    old_dir = tmp_path / "old_data"
    old_dir.mkdir()
    _make_user_data_with_internal_content_symlink(old_dir)

    original_game_dir = json.loads((old_dir / "settings.json").read_text())["game_dir"]
    original_link_target = os.readlink(str(old_dir / "content"))

    new_dir = tmp_path / "new_data"

    def boom(*a, **kw):
        raise RuntimeError("simulated pointer write failure")

    monkeypatch.setattr(relocation, "set_install_location", boom)

    with pytest.raises(RelocationError):
        migrate_data(old_dir, new_dir)

    # settings.json back at old_dir must have the ORIGINAL game_dir,
    # not the rewritten new_dir-pointing one.
    restored_settings = json.loads((old_dir / "settings.json").read_text())
    assert restored_settings["game_dir"] == original_game_dir

    # content symlink back at old_dir must point at its ORIGINAL
    # target, not the re-anchored new_dir one.
    assert os.readlink(str(old_dir / "content")) == original_link_target
    assert (old_dir / "content" / "pack.mcpack").read_text() == "internal pack"

    assert install_file.read_text().strip() == str(old_dir)
