"""External client DLL injection regressions."""
# SPDX-License-Identifier: MIT

import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from bol import inject
from bol.log import BolError


ROOT = Path(__file__).resolve().parents[1]


def test_native_injector_does_not_free_a_live_loadlibrary_buffer():
    source = (ROOT / "src" / "injector.c").read_text(encoding="utf-8")
    assert "DWORD wait = WaitForSingleObject(th, 15000);" in source
    assert "if (wait != WAIT_OBJECT_0)" in source
    timeout_branch = source.split(
        "if (wait != WAIT_OBJECT_0)", 1
    )[1].split("DWORD mod", 1)[0]
    assert "VirtualFreeEx" not in timeout_branch
    assert "return 8;" in timeout_branch
    write_failure = source.split(
        "if (!WriteProcessMemory", 1
    )[1].split("HANDLE th", 1)[0]
    assert "VirtualFreeEx" in write_failure
    bundled = (ROOT / "bol" / "injector.exe").read_bytes()
    assert "ERR allocate process memory".encode("utf-16le") in bundled


@pytest.mark.parametrize(
    "cmdline",
    [
        b"/engine/files/bin/wine64-preloader\0winedbg\0--auto\0",
        (
            b"/engine/files/bin/wine\0"
            b"C:\\windows\\system32\\winedbg.exe\0--auto\0"
        ),
    ],
)
def test_winedbg_scanner_matches_exact_argv_entry(cmdline):
    assert inject._cmdline_has_winedbg(cmdline)


@pytest.mark.parametrize(
    "cmdline",
    [
        b"/mnt/winedbg-archives/Minecraft.Windows.exe\0",
        b"/games/winedbg.exe.backup\0",
        b"/engine/files/bin/wine\0--log=winedbg\0",
    ],
)
def test_winedbg_scanner_ignores_unrelated_substrings(cmdline):
    assert not inject._cmdline_has_winedbg(cmdline)


def test_cached_injector_same_size_but_old_content_is_replaced_atomically(
        tmp_path):
    cached = tmp_path / "injector.exe"
    cached.write_bytes(b"OLD!")
    with mock.patch.object(inject, "CACHE", tmp_path), \
            mock.patch.object(
                inject.pkgutil, "get_data", return_value=b"NEW!"
            ), \
            mock.patch.object(inject.os, "replace",
                              wraps=inject.os.replace) as replace:
        assert inject._extract_injector() == cached

    assert cached.read_bytes() == b"NEW!"
    replace.assert_called_once()
    assert not list(tmp_path.glob(".injector-*.tmp"))


def test_injector_rejects_missing_or_non_dll_file(tmp_path):
    with pytest.raises(BolError, match="DLL not found"):
        inject.run_injector(tmp_path / "missing.dll")
    text = tmp_path / "client.txt"
    text.write_text("not a dll")
    with pytest.raises(BolError, match=r"Not a \.dll"):
        inject.run_injector(text)


def test_injector_requires_running_minecraft(tmp_path):
    dll = tmp_path / "client.dll"
    dll.write_bytes(b"MZ")
    with mock.patch.object(inject, "_mc_running", return_value=False):
        with pytest.raises(BolError, match="Start Minecraft first"):
            inject.run_injector(dll)


def test_injector_uses_game_prefix_and_wine_z_path(tmp_path):
    dll = tmp_path / "clients" / "Example Client.dll"
    dll.parent.mkdir()
    dll.write_bytes(b"MZ")
    engine = tmp_path / "engine"
    wine = engine / "files" / "bin" / "wine"
    wine.parent.mkdir(parents=True)
    wine.write_bytes(b"wine")
    injector_exe = tmp_path / "injector.exe"
    injector_exe.write_bytes(b"MZ")
    prefix = tmp_path / "prefix"
    logs = tmp_path / "logs"
    completed = subprocess.CompletedProcess([], 0)

    with mock.patch.object(inject, "_mc_running", return_value=True), \
            mock.patch.object(inject, "_wine_debugger_running",
                              return_value=False), \
            mock.patch.object(inject, "_post_injection_failure",
                              return_value=None), \
            mock.patch.object(inject, "proton_path", return_value=engine), \
            mock.patch.object(inject, "_extract_injector",
                              return_value=injector_exe), \
            mock.patch.object(inject, "active_prefix", return_value=prefix), \
            mock.patch.object(inject, "LOGS", logs), \
            mock.patch.object(inject.subprocess, "run",
                              return_value=completed) as run:
        assert inject.run_injector(dll) == "Example Client.dll"

    args, kwargs = run.call_args
    assert args[0] == [
        str(wine),
        str(injector_exe),
        "Z:" + str(dll.resolve()).replace("/", "\\"),
        "Minecraft.Windows.exe",
    ]
    assert kwargs["env"]["WINEPREFIX"] == str(prefix)
    assert kwargs["timeout"] == 60
    assert (logs / "injector.log").is_file()


def test_injector_reports_client_failure_with_64_bit_hint(tmp_path):
    dll = tmp_path / "bad.dll"
    dll.write_bytes(b"MZ")
    engine = tmp_path / "engine"
    wine = engine / "files" / "bin" / "wine"
    wine.parent.mkdir(parents=True)
    wine.write_bytes(b"wine")
    with mock.patch.object(inject, "_mc_running", return_value=True), \
            mock.patch.object(inject, "_wine_debugger_running",
                              return_value=False), \
            mock.patch.object(inject, "proton_path", return_value=engine), \
            mock.patch.object(inject, "_extract_injector",
                              return_value=tmp_path / "injector.exe"), \
            mock.patch.object(inject, "LOGS", tmp_path / "logs"), \
            mock.patch.object(
                inject.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 7)):
        with pytest.raises(BolError, match="64-bit"):
            inject.run_injector(dll)


def test_native_process_failure_is_not_misdiagnosed_as_bad_dll(tmp_path):
    dll = tmp_path / "client.dll"
    dll.write_bytes(b"MZ")
    engine = tmp_path / "engine"
    wine = engine / "files" / "bin" / "wine"
    wine.parent.mkdir(parents=True)
    wine.write_bytes(b"wine")
    with mock.patch.object(inject, "_mc_running", return_value=True), \
            mock.patch.object(inject, "_wine_debugger_running",
                              return_value=False), \
            mock.patch.object(inject, "proton_path", return_value=engine), \
            mock.patch.object(inject, "_extract_injector",
                              return_value=tmp_path / "injector.exe"), \
            mock.patch.object(inject, "LOGS", tmp_path / "logs"), \
            mock.patch.object(
                inject.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 3)):
        with pytest.raises(BolError, match="infrastructure failed") as raised:
            inject.run_injector(dll)

    assert "64-bit" not in str(raised.value)


def test_native_loadlibrary_timeout_has_specific_safe_diagnosis(tmp_path):
    dll = tmp_path / "slow-client.dll"
    dll.write_bytes(b"MZ")
    engine = tmp_path / "engine"
    wine = engine / "files" / "bin" / "wine"
    wine.parent.mkdir(parents=True)
    wine.write_bytes(b"wine")
    logs = tmp_path / "logs"
    with mock.patch.object(inject, "_mc_running", return_value=True), \
            mock.patch.object(inject, "_wine_debugger_running",
                              return_value=False), \
            mock.patch.object(inject, "proton_path", return_value=engine), \
            mock.patch.object(inject, "_extract_injector",
                              return_value=tmp_path / "injector.exe"), \
            mock.patch.object(inject, "LOGS", logs), \
            mock.patch.object(
                inject.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 8)):
        with pytest.raises(BolError, match="timed out.*retained safely"):
            inject.run_injector(dll)


def test_injector_timeout_is_actionable(tmp_path):
    dll = tmp_path / "slow.dll"
    dll.write_bytes(b"MZ")
    engine = tmp_path / "engine"
    wine = engine / "files" / "bin" / "wine"
    wine.parent.mkdir(parents=True)
    wine.write_bytes(b"wine")
    with mock.patch.object(inject, "_mc_running", return_value=True), \
            mock.patch.object(inject, "_wine_debugger_running",
                              return_value=False), \
            mock.patch.object(inject, "proton_path", return_value=engine), \
            mock.patch.object(inject, "_extract_injector",
                              return_value=tmp_path / "injector.exe"), \
            mock.patch.object(inject, "LOGS", tmp_path / "logs"), \
            mock.patch.object(
                inject.subprocess, "run",
                side_effect=subprocess.TimeoutExpired(["wine"], 60)):
        with pytest.raises(BolError, match="timed out"):
            inject.run_injector(dll)


def test_injector_refuses_game_already_in_winedbg(tmp_path):
    dll = tmp_path / "client.dll"
    dll.write_bytes(b"MZ")
    with mock.patch.object(inject, "_mc_running", return_value=True), \
            mock.patch.object(inject, "_wine_debugger_running",
                              return_value=True):
        with pytest.raises(BolError, match="already stopped.*crash debugger"):
            inject.run_injector(dll)


def test_post_injection_monitor_detects_winedbg_without_waiting():
    with mock.patch.object(inject, "_wine_debugger_running",
                           return_value=True), \
            mock.patch.object(inject, "_mc_running", return_value=True):
        assert "crash debugger" in inject._post_injection_failure(timeout=0)


def test_post_injection_monitor_detects_game_exit_without_waiting():
    with mock.patch.object(inject, "_wine_debugger_running",
                           return_value=False), \
            mock.patch.object(inject, "_mc_running", return_value=False):
        assert "Minecraft exited" in inject._post_injection_failure(timeout=0)


def test_post_injection_monitor_accepts_stable_game_without_waiting():
    with mock.patch.object(inject, "_wine_debugger_running",
                           return_value=False), \
            mock.patch.object(inject, "_mc_running", return_value=True):
        assert inject._post_injection_failure(timeout=0) is None


def test_injector_reports_asynchronous_client_crash(tmp_path):
    dll = tmp_path / "Borion.dll"
    dll.write_bytes(b"MZ")
    engine = tmp_path / "engine"
    wine = engine / "files" / "bin" / "wine"
    wine.parent.mkdir(parents=True)
    wine.write_bytes(b"wine")
    logs = tmp_path / "logs"
    with mock.patch.object(inject, "_mc_running", return_value=True), \
            mock.patch.object(inject, "_wine_debugger_running",
                              return_value=False), \
            mock.patch.object(inject, "_post_injection_failure",
                              return_value="Minecraft entered Wine's crash "
                                           "debugger immediately after the "
                                           "DLL was loaded (winedbg)"), \
            mock.patch.object(inject, "proton_path", return_value=engine), \
            mock.patch.object(inject, "_extract_injector",
                              return_value=tmp_path / "injector.exe"), \
            mock.patch.object(inject, "LOGS", logs), \
            mock.patch.object(
                inject.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 0)):
        with pytest.raises(BolError, match="exact Minecraft version"):
            inject.run_injector(dll)
    assert "ERR post-injection" in (logs / "injector.log").read_text()


@contextmanager
def _auto_inject_env(tmp_path, window=True, delay=0.0, settle=0.0,
                     ceiling=30.0, menu=True, menu_ceiling=150.0):
    """Drive the auto-injection watcher without a game, a window or a wait."""
    logs = tmp_path / "logs"
    with mock.patch.object(inject, "LOGS", logs), \
            mock.patch.object(inject, "_mc_running", return_value=True), \
            mock.patch.object(inject, "desktop_notify") as notify, \
            mock.patch.object(inject, "_game_window_present",
                              return_value=window) as present, \
            mock.patch.object(inject, "run_injector",
                              return_value="client.dll") as run_inj, \
            mock.patch.object(inject, "_AUTO_INJECT_POLL", 0.01), \
            mock.patch.object(inject, "_AUTO_INJECT_WINDOW_SETTLE", settle), \
            mock.patch.object(inject, "_AUTO_INJECT_WINDOW_CEILING", ceiling), \
            mock.patch.object(inject, "_AUTO_INJECT_MENU_CEILING",
                              menu_ceiling), \
            mock.patch.object(inject, "_menu_reached",
                              return_value=menu) as menu_reached:
        yield SimpleNamespace(notify=notify, run_injector=run_inj,
                              window=present, delay=delay, logs=logs,
                              menu=menu_reached)


def test_perform_auto_inject_local_success(tmp_path):
    dll = tmp_path / "client.dll"
    dll.write_bytes(b"MZ")
    settings = {
        "injector_auto_enable": True,
        "injector_dll_type": "file",
        "injector_dll_path": str(dll),
        "injector_delay": 0,
    }
    with _auto_inject_env(tmp_path) as env:
        inject.perform_auto_inject(settings)
    env.run_injector.assert_called_once_with(Path(dll))
    env.notify.assert_called_once()


def test_perform_auto_inject_remote_success(tmp_path):
    url = "https://example.com/client.dll"
    settings = {
        "injector_auto_enable": True,
        "injector_dll_type": "url",
        "injector_dll_url": url,
        "injector_delay": 0,
    }
    with _auto_inject_env(tmp_path) as env, \
            mock.patch.object(inject, "CACHE", tmp_path), \
            mock.patch("bol.util.download") as download:
        inject.perform_auto_inject(settings)
    dest = tmp_path / inject.auto_inject_download_path(url).name
    download.assert_called_once_with(url, dest, label="Auto-inject DLL")
    env.run_injector.assert_called_once_with(dest)
    env.notify.assert_called_once()


def test_each_download_url_caches_under_its_own_name():
    """A shared cache name splices one interrupted DLL onto the next.

    `download` resumes from the partial file beside its destination, so two
    URLs sharing a name produce a file that is the right length, passes the
    transfer's integrity check, and is a chimera of both.
    """
    first = inject.auto_inject_download_path("https://example.com/a.dll")
    second = inject.auto_inject_download_path("https://example.com/b.dll")
    assert first != second
    assert first == inject.auto_inject_download_path("https://example.com/a.dll")
    assert first.suffix == ".dll"


def test_the_configured_delay_is_a_floor_the_window_cannot_cut_short(tmp_path):
    """The delay is the only control over a client that loads too early."""
    dll = tmp_path / "client.dll"
    dll.write_bytes(b"MZ")
    settings = {"injector_dll_type": "file", "injector_dll_path": str(dll),
                "injector_delay": 0.4}
    with _auto_inject_env(tmp_path, window=True) as env:
        started = time.monotonic()
        inject.perform_auto_inject(settings)
        elapsed = time.monotonic() - started
    env.run_injector.assert_called_once()
    assert elapsed >= 0.4


def test_injection_waits_for_the_window_past_the_delay(tmp_path):
    """A DLL loaded into a game that has not opened yet lands mid-startup."""
    dll = tmp_path / "client.dll"
    dll.write_bytes(b"MZ")
    settings = {"injector_dll_type": "file", "injector_dll_path": str(dll),
                "injector_delay": 0}
    with _auto_inject_env(tmp_path) as env:
        env.window.side_effect = [False, False, False, True]
        inject.perform_auto_inject(settings)
    assert env.window.call_count == 4
    env.run_injector.assert_called_once()


def test_a_session_that_never_shows_an_x_window_still_injects(tmp_path):
    """Wayland gives the game no X window, so that wait is capped."""
    dll = tmp_path / "client.dll"
    dll.write_bytes(b"MZ")
    settings = {"injector_dll_type": "file", "injector_dll_path": str(dll),
                "injector_delay": 0}
    with _auto_inject_env(tmp_path, window=False, ceiling=0.2) as env:
        inject.perform_auto_inject(settings)
    env.run_injector.assert_called_once()


def test_injection_waits_for_the_main_menu_past_the_window(tmp_path):
    """Injected on the loading screen, Flarial lost the mouse (#281)."""
    dll = tmp_path / "client.dll"
    dll.write_bytes(b"MZ")
    settings = {"injector_dll_type": "file", "injector_dll_path": str(dll),
                "injector_delay": 0}
    with _auto_inject_env(tmp_path, window=True) as env:
        env.menu.side_effect = [False, False, False, True]
        inject.perform_auto_inject(settings)
    assert env.menu.call_count == 4
    env.run_injector.assert_called_once()


def test_a_menu_that_never_says_so_is_injected_after_a_while(tmp_path):
    dll = tmp_path / "client.dll"
    dll.write_bytes(b"MZ")
    settings = {"injector_dll_type": "file", "injector_dll_path": str(dll),
                "injector_delay": 0}
    with _auto_inject_env(tmp_path, window=True, menu=False,
                          menu_ceiling=0.2) as env:
        inject.perform_auto_inject(settings)
    env.run_injector.assert_called_once()
    assert "never said its menu was up" in (
        env.logs / "injector.log").read_text()


def test_the_menu_is_told_by_what_the_game_writes_after_it(tmp_path):
    flags = (tmp_path / "drive_c/users/steamuser/AppData/Roaming/"
             "Minecraft Bedrock/Flighting/currentTreatments")
    flags.parent.mkdir(parents=True)
    flags.write_text("{}")
    os.utime(flags, (1000, 1000))
    # Left by an earlier session: not this one's menu.
    assert not inject._menu_reached(2000, prefix=tmp_path)
    os.utime(flags, (3000, 3000))
    assert inject._menu_reached(2000, prefix=tmp_path)
    assert not inject._menu_reached(2000, prefix=tmp_path / "missing")


def test_preview_writes_its_flags_in_its_own_folder(tmp_path):
    marker = (tmp_path / "drive_c/users/steamuser/AppData/Roaming/"
              "Minecraft Bedrock Preview/bootstrapStorage/"
              "bootstrap_settings.json")
    marker.parent.mkdir(parents=True)
    marker.write_text("{}")
    assert inject._menu_reached(0, prefix=tmp_path)


def test_auto_inject_stops_when_the_game_exits_before_it_is_ready(tmp_path):
    dll = tmp_path / "client.dll"
    dll.write_bytes(b"MZ")
    settings = {"injector_dll_type": "file", "injector_dll_path": str(dll),
                "injector_delay": 0}
    with _auto_inject_env(tmp_path, window=False, ceiling=5.0) as env:
        inject._mc_running.side_effect = [True, True, False]
        inject.perform_auto_inject(settings)
    env.run_injector.assert_not_called()
    env.notify.assert_not_called()


def test_an_empty_local_path_is_reported_and_nothing_is_injected(tmp_path):
    with _auto_inject_env(tmp_path) as env:
        inject.perform_auto_inject({"injector_dll_type": "file"})
    env.run_injector.assert_not_called()
    assert "Local DLL path is empty" in (env.logs / "injector.log").read_text()


def test_start_auto_inject_does_nothing_unless_it_was_asked_for():
    with mock.patch.object(inject.threading, "Thread") as thread:
        assert inject.start_auto_inject({}) is None
        assert inject.start_auto_inject({"injector_auto_enable": False}) is None
    thread.assert_not_called()


def test_start_auto_inject_watches_in_the_background():
    settings = {"injector_auto_enable": True}
    with mock.patch.object(inject, "perform_auto_inject") as watch:
        watcher = inject.start_auto_inject(settings)
        assert watcher.daemon
        watcher.join(5)
    watch.assert_called_once_with(settings)
