from pathlib import Path

import pytest

import bol.optiscaler as optiscaler


def _payload(root: Path):
    payload = root / "payload"
    (payload / "OptiScaler").mkdir(parents=True)
    (payload / "OptiScaler.dll").write_bytes(b"proxy")
    (payload / "OptiScaler.ini").write_text(
        "[Spoofing]\nSpoofedVendorId=auto\nDxgi=auto\n\n"
        "[Inputs]\nEnableDlssInputs=auto\n\n"
        "[FrameGeneration]\nActive=false\n",
        encoding="utf-8",
    )
    (payload / "OptiScaler" / "amd_fidelityfx_framegeneration_dx12.dll").write_bytes(b"fg")
    return payload


def _game(root: Path):
    game = root / "game"
    game.mkdir()
    (game / "Minecraft.Windows.exe").write_bytes(b"minecraft")
    (game / "nvngx_dlss.dll").write_bytes(b"ngx")
    return game


def test_merge_dll_override_replaces_only_dxgi():
    result = optiscaler._merge_dll_override(
        "cryptbase=n,b;dxgi=b;vrclient=", "dxgi.dll=n,b"
    )
    assert result == "cryptbase=n,b;vrclient=;dxgi.dll=n,b"


def test_apply_environment_preserves_existing_overrides():
    env = {"WINEDLLOVERRIDES": "cryptbase=n,b;dxgi=n"}
    optiscaler.apply_environment(env)
    assert env["WINEDLLOVERRIDES"] == "cryptbase=n,b;dxgi.dll=n,b"
    assert env["DXVK_NVAPI_ALLOW_OTHER_DRIVERS"] == "1"
    assert env["DXVK_NVAPI_GPU_ARCH"] == "AD100"


def test_ini_patch_is_scoped_to_named_section(tmp_path):
    path = tmp_path / "OptiScaler.ini"
    path.write_text(
        "[Spoofing]\nSpoofedVendorId=auto\nOther=keep\n"
        "[Elsewhere]\nSpoofedVendorId=leave-this-alone\n",
        encoding="utf-8",
    )
    optiscaler._patch_ini(path)
    text = path.read_text(encoding="utf-8")
    assert "SpoofedVendorId=0x10de" in text
    assert "Other=keep" in text
    assert "[Elsewhere]\nSpoofedVendorId=leave-this-alone" in text
    assert "[Inputs]" in text
    assert "EnableDlssInputs=true" in text


def test_dxvk_block_preserves_user_config_and_is_idempotent(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    conf = game / "dxvk.conf"
    conf.write_text("dxgi.maxFrameRate = 144\n", encoding="utf-8")
    optiscaler._write_dxvk_conf(game)
    optiscaler._write_dxvk_conf(game)
    text = conf.read_text(encoding="utf-8")
    assert "dxgi.maxFrameRate = 144" in text
    assert text.count(optiscaler._DXVK_BEGIN) == 1
    optiscaler._remove_dxvk_conf_block(game)
    assert conf.read_text(encoding="utf-8") == "dxgi.maxFrameRate = 144\n"


def test_sync_to_game_installs_proxy_and_physical_nvngx(tmp_path, monkeypatch):
    payload = _payload(tmp_path)
    game = _game(tmp_path)
    game_ini = game / "OptiScaler.ini"
    game_ini.write_text(
        "[Spoofing]\nDxgi=auto\n\n[FrameGeneration]\nActive=true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(optiscaler, "PAYLOAD", payload)

    result = optiscaler.sync_to_game(game)

    assert result == game.resolve()
    assert (game / "dxgi.dll").read_bytes() == b"proxy"
    assert (game / "nvngx.dll").read_bytes() == b"ngx"
    assert (game / optiscaler.MARKER).is_file()
    assert (game / "OptiScaler" / "amd_fidelityfx_framegeneration_dx12.dll").is_file()
    text = game_ini.read_text(encoding="utf-8")
    assert "Active=true" in text
    assert "Dxgi=true" in text
    assert "EnableDlssInputs=true" in text


def test_sync_refuses_to_overwrite_unmanaged_dxgi(tmp_path, monkeypatch):
    payload = _payload(tmp_path)
    game = _game(tmp_path)
    (game / "dxgi.dll").write_bytes(b"another mod")
    monkeypatch.setattr(optiscaler, "PAYLOAD", payload)

    with pytest.raises(Exception, match="already exists"):
        optiscaler.sync_to_game(game)
