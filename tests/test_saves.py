"""The player's worlds, settings and servers across Minecraft versions."""
# SPDX-License-Identifier: MIT

import contextlib
import json
import os
import shutil
import time
from collections import namedtuple

import pytest

from bol import prefix as prefix_module
from bol import saves
from bol.log import BolError

ACCOUNT = "15576315838024289709"
_Usage = namedtuple("_Usage", "total used free")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A prefix, a games tree, a backups folder and a settings file, all
    under tmp_path -- never the machine's own."""
    settings = {}

    def load():
        return json.loads(json.dumps(settings))

    def save(value):
        settings.clear()
        settings.update(json.loads(json.dumps(value)))

    monkeypatch.setattr(saves, "load_settings", load)
    monkeypatch.setattr(saves, "save_settings", save)
    monkeypatch.setattr(saves, "BACKUPS", tmp_path / "data" / "backups")
    monkeypatch.setattr(saves, "GAMES", tmp_path / "data" / "games")
    monkeypatch.setattr(prefix_module, "require_prefix_idle",
                        lambda *_args, **_kwargs: True)
    monkeypatch.setattr(prefix_module, "prefix_operation_lock",
                        lambda *_args, **_kwargs: contextlib.nullcontext())
    pfx = tmp_path / "pfx"
    pfx.mkdir()
    return pfx, settings, tmp_path


def _account(pfx, edition="release", account=ACCOUNT):
    return (saves.users_dir(edition, pfx) / account / "games" / "com.mojang")


def _play(pfx, edition="release", worlds=("Survie",),
          options="gfx_viewdistance:96\n",
          servers="1:Linesia:play.linesia.net:19132:0\n"):
    """Leave behind what a signed-in session of ``edition`` saves."""
    base = _account(pfx, edition)
    for world in worlds:
        (base / "minecraftWorlds" / world / "db").mkdir(parents=True)
        (base / "minecraftWorlds" / world / "levelname.txt").write_text(world)
    (base / "minecraftpe").mkdir(parents=True, exist_ok=True)
    (base / "minecraftpe" / "options.txt").write_text(options)
    (base / "minecraftpe" / "external_servers.txt").write_text(servers)
    return base


def _select(settings, tmp_path, edition, version):
    build = tmp_path / "data" / "games" / edition / version
    build.mkdir(parents=True, exist_ok=True)
    settings["game_dir"] = str(build)
    settings["mc_edition"] = edition
    settings["mc_version"] = version


# ------------------------------------------------------------ where data is

def test_each_edition_reads_its_own_users_folder(tmp_path):
    assert saves.users_dir("release", tmp_path) == (
        tmp_path / "drive_c/users/steamuser/AppData/Roaming"
        / "Minecraft Bedrock" / "Users")
    assert saves.users_dir("preview", tmp_path).parent.name == \
        "Minecraft Bedrock Preview"


def test_a_downloaded_build_is_named_by_its_folder(env):
    _pfx, settings, tmp_path = env
    _select(settings, tmp_path, "preview", "1.26.60.20")
    settings["mc_edition"] = "release"   # a stale setting must not win
    assert saves.launched_build(settings) == ("preview", "1.26.60.20")


def test_an_imported_build_is_named_by_its_manifest(env):
    _pfx, settings, tmp_path = env
    folder = tmp_path / "elsewhere"
    folder.mkdir()
    (folder / "appxmanifest.xml").write_text(
        '<Package><Identity Name="Microsoft.MinecraftWindowsBeta" '
        'Version="1.26.6020.0" /></Package>')
    settings.update(game_dir=str(folder), mc_edition="release")
    assert saves.launched_build(settings) == ("preview", "1.26.60.20")


def test_no_build_selected_names_nothing(env):
    assert saves.launched_build({}) == (None, None)


# ------------------------------------------------------------ version change

def test_a_new_version_backs_up_what_it_is_about_to_open(env):
    pfx, settings, tmp_path = env
    _play(pfx)
    settings[saves.LAST_PLAYED] = {"release": "1.26.52.3"}
    _select(settings, tmp_path, "release", "1.26.60.1")

    assert saves.before_launch(settings, pfx) == ("release", "1.26.60.1")

    [backup] = saves.list_backups()
    assert backup["reason"] == "version-change"
    assert backup["version"] == "1.26.60.1"
    assert backup["previous"] == "1.26.52.3"
    assert backup["worlds"] == 1
    kept = backup["path"] / "Users" / ACCOUNT / "games" / "com.mojang"
    assert (kept / "minecraftWorlds" / "Survie" / "levelname.txt").exists()
    assert (kept / "minecraftpe" / "options.txt").read_text() == \
        "gfx_viewdistance:96\n"
    assert "Linesia" in (kept / "minecraftpe" / "external_servers.txt").read_text()
    assert settings[saves.LAST_PLAYED] == {"release": "1.26.60.1"}


def test_playing_the_same_version_again_costs_no_copy(env):
    pfx, settings, tmp_path = env
    _play(pfx)
    settings[saves.LAST_PLAYED] = {"release": "1.26.52.3"}
    _select(settings, tmp_path, "release", "1.26.52.3")
    saves.before_launch(settings, pfx)
    assert saves.list_backups() == []


def test_going_back_to_an_older_version_is_backed_up_too(env):
    # A world the newer build saved may not load in the older one.
    pfx, settings, tmp_path = env
    _play(pfx)
    settings[saves.LAST_PLAYED] = {"release": "1.26.60.1"}
    _select(settings, tmp_path, "release", "1.26.52.3")
    saves.before_launch(settings, pfx)
    [backup] = saves.list_backups()
    assert backup["previous"] == "1.26.60.1"


def test_the_first_launch_after_this_feature_keeps_a_copy(env):
    # Nothing says which build saved the data, so it is kept once.
    pfx, settings, tmp_path = env
    _play(pfx)
    _select(settings, tmp_path, "release", "1.26.52.3")
    saves.before_launch(settings, pfx)
    assert len(saves.list_backups()) == 1
    saves.before_launch(settings, pfx)
    assert len(saves.list_backups()) == 1


def test_a_first_ever_launch_has_nothing_to_back_up(env):
    pfx, settings, tmp_path = env
    _select(settings, tmp_path, "release", "1.26.52.3")
    saves.before_launch(settings, pfx)
    assert saves.list_backups() == []
    assert settings[saves.LAST_PLAYED] == {"release": "1.26.52.3"}


def test_only_the_last_five_backups_are_kept(env):
    pfx, settings, tmp_path = env
    _play(pfx)
    for minor in range(7):
        _select(settings, tmp_path, "release", f"1.26.{50 + minor}.1")
        saves.before_launch(settings, pfx)
    backups = saves.list_backups()
    assert len(backups) == saves.KEEP
    assert backups[0]["version"] == "1.26.56.1"
    assert backups[-1]["version"] == "1.26.52.1"


def test_no_room_for_a_backup_never_stops_the_game(env, monkeypatch):
    pfx, settings, tmp_path = env
    _play(pfx)
    monkeypatch.setattr(saves.shutil, "disk_usage",
                        lambda _path: _Usage(10, 10, 1024))
    warnings = []
    monkeypatch.setattr(saves, "warn", warnings.append)
    _select(settings, tmp_path, "release", "1.26.60.1")
    assert saves.before_launch(settings, pfx) == ("release", "1.26.60.1")
    assert saves.list_backups() == []
    assert any("Not enough free space" in text for text in warnings)


def test_a_failed_copy_leaves_no_half_backup(env, monkeypatch):
    pfx, _settings, _tmp_path = env
    _play(pfx)

    def broken(*_args, **_kwargs):
        raise OSError("disk went away")

    monkeypatch.setattr(saves.shutil, "copytree", broken)
    with pytest.raises(BolError, match="disk went away"):
        saves.back_up("release", "version-change", prefix=pfx)
    assert list(saves.BACKUPS.iterdir()) == []


# ------------------------------------------------------------ restore

def test_restoring_puts_the_old_data_back_and_keeps_the_current_one(env):
    pfx, settings, tmp_path = env
    base = _play(pfx, worlds=("Ancien",))
    backup = saves.back_up("release", "version-change", version="1.26.60.1",
                           previous="1.26.52.3", prefix=pfx)
    # The new version then changed things.
    (base / "minecraftWorlds" / "Nouveau").mkdir()
    (base / "minecraftpe" / "options.txt").write_text("gfx_viewdistance:32\n")

    kept = saves.restore_backup(backup, prefix=pfx)

    assert (base / "minecraftpe" / "options.txt").read_text() == \
        "gfx_viewdistance:96\n"
    assert not (base / "minecraftWorlds" / "Nouveau").exists()
    assert (kept / "Users" / ACCOUNT / "games" / "com.mojang"
            / "minecraftWorlds" / "Nouveau").is_dir()
    assert saves._record(kept)["reason"] == "before-restore"
    assert backup.is_dir()


def test_restoring_the_oldest_backup_never_prunes_it_away(env):
    pfx, settings, tmp_path = env
    _play(pfx)
    for minor in range(saves.KEEP):
        _select(settings, tmp_path, "release", f"1.26.{50 + minor}.1")
        saves.before_launch(settings, pfx)
    oldest = saves.list_backups()[-1]["path"]
    saves.restore_backup(oldest, prefix=pfx)
    assert oldest.is_dir()


def test_a_folder_that_is_not_a_backup_is_refused(env, tmp_path):
    pfx, _settings, _tmp_path = env
    stray = tmp_path / "stray"
    (stray / "Users").mkdir(parents=True)
    with pytest.raises(BolError, match="not a backup"):
        saves.restore_backup(stray, prefix=pfx)


def test_a_swap_cut_between_its_two_renames_keeps_the_real_data(env):
    pfx, _settings, _tmp_path = env
    _play(pfx)
    target = saves.users_dir("release", pfx)
    # Stopped after the data went out and before the copy came in.
    os.rename(target, target.with_name(saves._OUTGOING))
    target.with_name(saves._INCOMING).mkdir()
    saves._recover_interrupted_swap(target, "release", pfx)
    assert (target / ACCOUNT / "games" / "com.mojang" / "minecraftpe"
            / "options.txt").exists()
    assert not target.with_name(saves._OUTGOING).exists()
    assert not target.with_name(saves._INCOMING).exists()


def test_a_game_started_after_a_cut_swap_does_not_cost_the_real_data(env):
    """The game makes a fresh Users folder where the data went out; the next
    recovery used to delete the data that went out as the leftover."""
    pfx, settings, tmp_path = env
    _play(pfx, worlds=("Survie",))
    target = saves.users_dir("release", pfx)
    os.rename(target, target.with_name(saves._OUTGOING))
    (target.with_name(saves._INCOMING) / "x").mkdir(parents=True)
    # The session in between, on a profile the game made from nothing.
    _play(pfx, worlds=("Nouveau monde",))
    _select(settings, tmp_path, "release", "1.26.52.3")
    settings[saves.LAST_PLAYED] = {"release": "1.26.52.3"}

    saves.before_launch(settings, pfx)

    worlds = _account(pfx) / "minecraftWorlds"
    assert sorted(p.name for p in worlds.iterdir()) == ["Survie"]
    assert not target.with_name(saves._OUTGOING).exists()
    assert not target.with_name(saves._INCOMING).exists()
    # What that session saved is kept, as a backup that says what it is.
    kept = [b for b in saves.list_backups()
            if b["reason"] == "after-interrupted-swap"]
    assert len(kept) == 1
    assert (kept[0]["path"] / "Users" / ACCOUNT / "games" / "com.mojang"
            / "minecraftWorlds" / "Nouveau monde").is_dir()


def test_a_completed_swap_only_loses_what_it_replaced(env):
    pfx, _settings, _tmp_path = env
    _play(pfx, worlds=("Nouveau",))
    target = saves.users_dir("release", pfx)
    # Stopped while deleting what the swap replaced.
    (target.with_name(saves._OUTGOING) / "old").mkdir(parents=True)
    saves._recover_interrupted_swap(target, "release", pfx)
    assert (_account(pfx) / "minecraftWorlds" / "Nouveau").is_dir()
    assert not target.with_name(saves._OUTGOING).exists()
    assert saves.list_backups() == []


def test_no_room_to_keep_the_new_folder_moves_nothing(env, monkeypatch):
    pfx, settings, tmp_path = env
    _play(pfx, worlds=("Survie",))
    target = saves.users_dir("release", pfx)
    os.rename(target, target.with_name(saves._OUTGOING))
    target.with_name(saves._INCOMING).mkdir()
    _play(pfx, worlds=("Nouveau monde",))
    monkeypatch.setattr(saves.shutil, "disk_usage",
                        lambda _path: _Usage(10, 10, 0))
    _select(settings, tmp_path, "release", "1.26.52.3")

    with pytest.raises(BolError, match="Nothing was deleted"):
        saves.before_launch(settings, pfx)

    assert (target.with_name(saves._OUTGOING) / ACCOUNT / "games"
            / "com.mojang" / "minecraftWorlds" / "Survie").is_dir()
    assert (_account(pfx) / "minecraftWorlds" / "Nouveau monde").is_dir()


# ------------------------------------------------------------ preview

def test_preview_starts_from_a_copy_of_the_stable_data(env):
    pfx, settings, tmp_path = env
    stable = _play(pfx, worlds=("Survie", "Créatif"))
    _select(settings, tmp_path, "preview", "1.26.60.20")
    assert saves.preview_copy_pending(settings, pfx)

    saves.before_launch(settings, pfx)

    preview = _account(pfx, "preview")
    assert sorted(p.name for p in (preview / "minecraftWorlds").iterdir()) \
        == ["Créatif", "Survie"]
    assert (preview / "minecraftpe" / "options.txt").read_text() == \
        "gfx_viewdistance:96\n"
    assert "Linesia" in (preview / "minecraftpe"
                         / "external_servers.txt").read_text()
    # A copy: the stable game keeps its own.
    assert (stable / "minecraftWorlds" / "Survie").is_dir()
    assert settings[saves.PREVIEW_DATA] == "copied"
    # Its original is right beside it, so no backup of the copy as well.
    assert saves.list_backups() == []
    assert not saves.preview_copy_pending(settings, pfx)


def test_preview_is_copied_into_only_once(env):
    pfx, settings, tmp_path = env
    _play(pfx)
    _select(settings, tmp_path, "preview", "1.26.60.20")
    saves.before_launch(settings, pfx)
    # A world deleted in Preview stays deleted.
    preview = _account(pfx, "preview")
    shutil.rmtree(preview / "minecraftWorlds")
    _select(settings, tmp_path, "preview", "1.26.60.21")
    saves.before_launch(settings, pfx)
    assert not (preview / "minecraftWorlds").exists()


def test_preview_with_worlds_of_its_own_is_never_written_over(env):
    pfx, settings, tmp_path = env
    _play(pfx, worlds=("Stable",))
    _play(pfx, edition="preview", worlds=("Preview",))
    _select(settings, tmp_path, "preview", "1.26.60.20")
    assert not saves.preview_copy_pending(settings, pfx)
    saves.before_launch(settings, pfx)
    worlds = _account(pfx, "preview") / "minecraftWorlds"
    assert [p.name for p in worlds.iterdir()] == ["Preview"]
    assert settings[saves.PREVIEW_DATA] == "separate"


def test_a_player_who_keeps_preview_apart_is_not_copied_into(env):
    pfx, settings, tmp_path = env
    _play(pfx)
    settings[saves.PREVIEW_DATA] = "separate"
    _select(settings, tmp_path, "preview", "1.26.60.20")
    saves.before_launch(settings, pfx)
    assert not (_account(pfx, "preview") / "minecraftWorlds").exists()


def test_copying_later_backs_up_what_preview_had(env):
    pfx, settings, tmp_path = env
    _play(pfx, worlds=("Stable",))
    _play(pfx, edition="preview", worlds=("Preview",))
    saves.copy_to_preview_when_idle(prefix=pfx)
    worlds = _account(pfx, "preview") / "minecraftWorlds"
    assert [p.name for p in worlds.iterdir()] == ["Stable"]
    [backup] = saves.list_backups()
    assert backup["edition"] == "preview"
    assert backup["reason"] == "before-preview-copy"


def test_nothing_to_copy_is_said_rather_than_done(env):
    pfx, _settings, _tmp_path = env
    with pytest.raises(BolError, match="no worlds or settings"):
        saves.copy_to_preview(prefix=pfx)


# ------------------------------------------------------------ signed out

def _options(path, mtime):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("gfx_viewdistance:96\n")
    os.utime(path, (mtime, mtime))


def test_a_session_that_wrote_only_the_signed_out_settings_ran_signed_out(
        tmp_path):
    users = saves.users_dir("release", tmp_path)
    started = time.time()
    _options(users / ACCOUNT / "games/com.mojang/minecraftpe/options.txt",
             started - 3600)
    _options(users / "Shared/games/com.mojang/minecraftpe/options.txt",
             started + 5)
    assert saves.ran_signed_out("release", started, tmp_path)


def test_a_signed_in_session_is_not_mistaken_for_one(tmp_path):
    users = saves.users_dir("release", tmp_path)
    started = time.time()
    _options(users / ACCOUNT / "games/com.mojang/minecraftpe/options.txt",
             started + 5)
    _options(users / "Shared/games/com.mojang/minecraftpe/options.txt",
             started + 5)
    assert not saves.ran_signed_out("release", started, tmp_path)


def test_signed_out_with_no_account_data_to_miss_says_nothing(tmp_path):
    users = saves.users_dir("release", tmp_path)
    started = time.time()
    _options(users / "Shared/games/com.mojang/minecraftpe/options.txt",
             started + 5)
    assert not saves.ran_signed_out("release", started, tmp_path)


def test_an_old_signed_out_file_is_not_this_session(tmp_path):
    users = saves.users_dir("release", tmp_path)
    started = time.time()
    _options(users / ACCOUNT / "games/com.mojang/minecraftpe/options.txt",
             started - 60)
    _options(users / "Shared/games/com.mojang/minecraftpe/options.txt",
             started - 30)
    assert not saves.ran_signed_out("release", started, tmp_path)
    assert not saves.ran_signed_out(None, started, tmp_path)
