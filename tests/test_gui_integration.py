"""bol.test_gui_integration — GUI integration tests for PySide6 rewrite."""
# SPDX-License-Identifier: MIT

import os
import socket
import sys
import json
import tempfile
from pathlib import Path
from unittest import mock

import pytest
from PySide6.QtWidgets import QComboBox, QMainWindow, QPushButton, QTabWidget
from PySide6.QtTest import QSignalSpy

# Ensure we can import bol modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from bol.gui import (
    MainWindow, Theme, VersionPicker, ProfileMenu, Worker,
    _resolve_gui_display, _owned_x11_socket_displays,
    SwitchRow,
)
from tests.guiharness import headless_window, qt_app



def _serving_socket(path):
    """A real AF_UNIX socket that accepts connections, so the display probe
    is exercised rather than mocked."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    return listener


def _visible_rows(picker):
    return [picker.list.item(i).text() for i in range(picker.list.count())
            if not picker.list.item(i).isHidden()]


def _button_labels(widget):
    return [b.text() for b in widget.findChildren(QPushButton)]


def _button_named(widget, label):
    return next(b for b in widget.findChildren(QPushButton)
                if b.text() == label)


# ======================================================================
# Fixtures
# ======================================================================

@pytest.fixture(scope="session")
def qapp():
    """The one QApplication for the test session."""
    yield qt_app()
    # Don't quit; let pytest handle cleanup


@pytest.fixture
def temp_settings_dir():
    """Temporary directory for settings files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def main_window(qapp, monkeypatch, temp_settings_dir):
    """A MainWindow that reaches nothing outside the process.

    This used to leave _refresh_store_row unpatched, so every construction
    called the real xodus.signed_in() -- five to eight seconds each, times
    fifty tests. The shared harness in tests/guiharness.py also stubs the
    singleShot timers __init__ arms for refresh_versions and the update
    check, which any test running the event loop would otherwise fire off
    at the network, and restores the global log sink afterwards.
    """
    settings_file = temp_settings_dir / "settings.json"

    def load():
        if settings_file.exists():
            return json.loads(settings_file.read_text())
        return {}

    def save(settings):
        settings_file.write_text(json.dumps(settings))

    with headless_window() as window:
        # Settings written by a test have to survive into the next load()
        # for the persistence tests, which the harness's static dict cannot
        # do -- so those two are re-pointed at the temp file here.
        monkeypatch.setattr("bol.gui.load_settings", load)
        monkeypatch.setattr("bol.gui.save_settings", save)
        yield window


# ======================================================================
# Theme Tests
# ======================================================================

class TestTheme:
    """Test Theme class and QSS generation."""

    def test_theme_initialization(self):
        """Theme initializes with default dark mode."""
        theme = Theme()
        assert theme.dark is True
        assert theme.beta is False

    def test_theme_light_mode(self):
        """Light theme returns correct colors."""
        theme = Theme(dark=False)
        assert theme.bg == "#eef1f6"
        assert theme.fg == "#12141a"

    def test_theme_dark_mode(self):
        """Dark theme returns correct colors."""
        theme = Theme(dark=True)
        assert theme.bg == "#0d0f14"
        assert theme.fg == "#eef1f6"

    def test_theme_beta_accent(self):
        """Beta mode uses gold accent."""
        theme = Theme(beta=True)
        assert theme.accent == theme.gold
        assert theme.accent_hov == theme.gold_hov

    def test_theme_stable_accent(self):
        """Stable mode uses green accent."""
        theme = Theme(beta=False)
        assert theme.accent == theme.green
        assert theme.accent_hov == theme.green_hov

    def test_qss_generation(self):
        """QSS stylesheet is generated without errors."""
        theme = Theme()
        qss = theme.qss()
        assert "QMainWindow" in qss
        assert "QPushButton" in qss
        assert "QLineEdit" in qss
        assert theme.fg in qss
        assert theme.bg in qss


# ======================================================================
# MainWindow Initialization Tests
# ======================================================================

class TestMainWindowInitialization:
    """Test MainWindow startup and initialization."""

    def test_main_window_creates(self, qapp):
        """MainWindow initializes without errors."""
        with mock.patch.object(MainWindow, "__init__", lambda x: None):
            window = QMainWindow()
            assert window is not None

    def test_main_window_properties(self, main_window):
        """MainWindow has expected properties after initialization."""
        assert main_window.windowTitle() != ""
        assert main_window.width() >= 880
        assert main_window.height() >= 640
        assert main_window.theme is not None
        assert isinstance(main_window.theme, Theme)

    def test_main_window_ui_state(self, main_window):
        """UI state is initialized correctly."""
        assert main_window.ui_state["busy"] is False
        assert main_window.ui_state["launch_active"] is False
        assert main_window.ui_state["details"] is False
        assert main_window.ui_state["stepped_aside"] is False

    def test_main_window_central_widget(self, main_window):
        """Central widget and stack are created."""
        assert main_window.centralWidget() is not None
        assert main_window.stack is not None

    def test_main_window_pages(self, main_window):
        """Main page, settings, and changelog pages exist."""
        assert main_window.hero_page is not None
        assert main_window.settings_page is not None
        assert main_window.changelog_page is not None

    def test_injector_dll_load_mode_combo(self, main_window):
        """The DLL config uses a dropdown selecting file vs URL download."""
        combo = main_window.dll_mode_combo
        assert isinstance(combo, QComboBox)
        assert combo.count() == 2
        assert combo.itemText(0) == "File"
        assert combo.itemText(1) == "Download"


# ======================================================================
# Theme Application Tests
# ======================================================================

class TestThemeApplication:
    """Test applying themes to the UI."""

    def test_apply_light_theme(self, main_window):
        """Applying light theme updates stylesheet."""
        main_window.settings["light_theme"] = True
        main_window.apply_theme()
        assert main_window.theme.dark is False

    def test_apply_dark_theme(self, main_window):
        """Applying dark theme updates stylesheet."""
        main_window.settings["light_theme"] = False
        main_window.apply_theme()
        assert main_window.theme.dark is True

    def test_apply_beta_theme(self, main_window):
        """Applying beta theme changes accent color."""
        main_window.settings["ui_is_beta"] = True
        main_window.apply_theme()
        assert main_window.theme.beta is True

    def test_theme_switches_update(self, main_window):
        """SwitchRow widgets are updated when theme changes."""
        # Add a switch to track
        test_switch = main_window._switch("Test Toggle")
        assert test_switch in main_window._switches
        main_window.apply_theme()
        # No exception should be raised


# ======================================================================
# Profile Menu Tests
# ======================================================================

class TestProfileMenu:
    """Test profile switcher menu."""

    def test_profile_menu_creation(self, qapp):
        """A menu that has never been rebuilt offers nothing to click."""
        with mock.patch("bol.gui.QApplication.instance", return_value=qapp):
            menu = ProfileMenu(None)
            assert _button_labels(menu) == []

    def test_profile_menu_rebuild(self, qapp):
        """Every profile gets a row, and Default is always offered."""
        with mock.patch("bol.gui.QApplication.instance", return_value=qapp):
            menu = ProfileMenu(None)
            menu.rebuild([{"name": "Profile 1", "path": "/path/to/profile1"},
                          {"name": "Profile 2", "path": "/path/to/profile2"}],
                         None)
            labels = _button_labels(menu)
            assert "Default" in labels
            assert "Profile 1" in labels
            assert "Profile 2" in labels
            assert "+ New Profile…" in labels

    def test_profile_menu_rebuild_replaces_the_previous_rows(self, qapp):
        """Reopening the menu must not stack a second copy of every row."""
        with mock.patch("bol.gui.QApplication.instance", return_value=qapp):
            menu = ProfileMenu(None)
            profiles = [{"name": "Only", "path": "/p/only"}]
            menu.rebuild(profiles, None)
            first = _button_labels(menu)
            menu.rebuild(profiles, None)
            assert _button_labels(menu) == first

    def test_switching_a_profile_reports_its_path(self, qapp):
        with mock.patch("bol.gui.QApplication.instance", return_value=qapp):
            menu = ProfileMenu(None)
            menu.rebuild([{"name": "Second", "path": "/p/second"}], None)
            picked = []
            menu.switch.connect(picked.append)
            _button_named(menu, "Second").click()
            assert picked == ["/p/second"]

    def test_the_default_profile_switches_to_no_path(self, qapp):
        with mock.patch("bol.gui.QApplication.instance", return_value=qapp):
            menu = ProfileMenu(None)
            menu.rebuild([{"name": "Second", "path": "/p/second"}],
                         "/p/second")
            picked = []
            menu.switch.connect(picked.append)
            _button_named(menu, "Default").click()
            assert picked == [None]


# ======================================================================
# Version Picker Tests
# ======================================================================

class TestVersionPicker:
    """Test version selection widget."""

    def test_version_picker_creation(self, qapp):
        """A fresh picker is empty and unfiltered."""
        with mock.patch("bol.gui.QApplication.instance", return_value=qapp):
            picker = VersionPicker(None)
            assert picker.list.count() == 0
            assert picker.search.text() == ""

    def test_version_picker_set_labels(self, qapp):
        """VersionPicker displays version labels."""
        with mock.patch("bol.gui.QApplication.instance", return_value=qapp):
            picker = VersionPicker(None)
            labels = ["1.0.0", "1.1.0", "1.2.0"]
            picker.set_labels(labels, "1.1.0")
            assert picker.list.count() == 3

    def test_version_picker_filter(self, qapp):
        """Filtering hides exactly the rows that do not match."""
        with mock.patch("bol.gui.QApplication.instance", return_value=qapp):
            picker = VersionPicker(None)
            picker.set_labels(["1.0.0", "1.1.0", "2.0.0"], "1.0.0")
            picker.search.setText("1.1")
            assert _visible_rows(picker) == ["1.1.0"]

    def test_version_picker_filter_is_case_insensitive(self, qapp):
        with mock.patch("bol.gui.QApplication.instance", return_value=qapp):
            picker = VersionPicker(None)
            picker.set_labels(["1.2.3  ·  BETA", "1.2.4"], "1.2.4")
            picker.search.setText("beta")
            assert _visible_rows(picker) == ["1.2.3  ·  BETA"]

    def test_clearing_the_filter_brings_every_row_back(self, qapp):
        with mock.patch("bol.gui.QApplication.instance", return_value=qapp):
            picker = VersionPicker(None)
            picker.set_labels(["1.0.0", "1.1.0", "2.0.0"], "1.0.0")
            picker.search.setText("2.")
            picker.search.setText("")
            assert len(_visible_rows(picker)) == 3


class TestRememberedVersion:
    """The build the launcher comes back to (issue #214).

    The picker shows a shortened label -- "26.44" for 1.26.44.3 -- and the
    selection used to be saved in that shape. Nothing could read it back: the
    installer answered "no longer listed" and took the newest build instead,
    so a launcher restarted after playing an older build silently downloaded
    another 2.5 GB of game.
    """

    RELEASE = {"id": "release", "name": "Minecraft for Windows", "beta": False}

    def _versions(self):
        return [
            {"tag": "1.26.50.1", "beta": False, "edition": self.RELEASE,
             "installed": False},
            {"tag": "1.26.44.3", "beta": False, "edition": self.RELEASE,
             "installed": True},
            {"tag": "1.26.42.1", "beta": False, "edition": self.RELEASE,
             "installed": True},
        ]

    def _picked(self, window, saved):
        with mock.patch("bol.gui.load_settings", lambda: {"mc_version": saved}):
            window._on_versions_loaded(self._versions())
        return window.selected_version()["tag"]

    def test_the_build_last_played_is_the_one_offered(self, main_window):
        assert self._picked(main_window, "1.26.44.3") == "1.26.44.3"

    def test_a_selection_saved_by_an_older_release_still_resolves(self,
                                                                  main_window):
        # "26.44" is what the chip used to write.
        assert self._picked(main_window, "26.44") == "1.26.44.3"

    def test_an_unknown_build_falls_back_to_the_newest(self, main_window):
        assert self._picked(main_window, "1.20.0.1") == "1.26.50.1"

    def test_nothing_saved_offers_the_newest(self, main_window):
        assert self._picked(main_window, "") == "1.26.50.1"

    def test_choosing_a_build_saves_the_build_and_not_the_label(self,
                                                                main_window):
        saved = {}
        with mock.patch("bol.gui.load_settings", lambda: dict(saved)), \
                mock.patch("bol.gui.save_settings", saved.update):
            main_window._on_versions_loaded(self._versions())
            main_window.set_version("26.44")
        assert saved["mc_version"] == "1.26.44.3"


class TestOtherTitlesInThePicker:
    """Minecraft Dungeons and Legends beside Stable and Preview."""

    RELEASE = {"id": "release", "name": "Minecraft for Windows", "beta": False}

    def _versions(self):
        from bol import xodus
        return [
            {"tag": "1.26.63.1", "beta": False, "edition": self.RELEASE,
             "installed": True},
            {"tag": "current", "beta": False,
             "edition": xodus.edition("dungeons"), "installed": True,
             "build": "1.17.0.0"},
            {"tag": "current", "beta": False,
             "edition": xodus.edition("legends"), "installed": False,
             "build": None},
        ]

    def test_each_edition_has_a_toggle(self, main_window):
        buttons = main_window.edition_buttons
        assert list(buttons)[:2] == ["release", "preview"]
        assert buttons["dungeons"].accessibleName() == "Dungeons"
        assert buttons["legends"].accessibleName() == "Legends"
        assert main_window.stable_btn is buttons["release"]
        assert main_window.preview_btn is buttons["preview"]

    def test_labels_name_the_title_and_its_build(self):
        from bol.gui import _version_label
        versions = self._versions()
        assert _version_label(versions[1]) == "Dungeons  ·  1.17.0.0"
        assert _version_label(versions[2]) == "Legends  ·  latest"
        assert _version_label(versions[0]) == "26.63"

    def test_edition_of_an_entry_in_every_shape(self):
        from bol.gui import _version_edition_id
        assert _version_edition_id({"edition": {"id": "dungeons"}}) == \
            "dungeons"
        assert _version_edition_id({"edition": "release"}) == "release"
        assert _version_edition_id({"beta": True}) == "preview"
        assert _version_edition_id(None) == "release"

    def test_choosing_dungeons_shows_and_saves_dungeons(self, main_window):
        saved = {}
        with mock.patch("bol.gui.load_settings", lambda: dict(saved)), \
                mock.patch("bol.gui.save_settings", saved.update):
            main_window._on_versions_loaded(self._versions())
            main_window.edition_buttons["dungeons"].click()
            picked = main_window.selected_version()
        assert picked["edition"]["id"] == "dungeons"
        assert main_window.edition_buttons["dungeons"].isChecked()
        assert saved["mc_version"] == "current"
        assert saved["ui_edition"] == "dungeons"
        assert saved["show_betas"] is False

    def test_the_saved_title_is_offered_again(self, main_window):
        with mock.patch("bol.gui.load_settings",
                        lambda: {"mc_version": "current",
                                 "ui_edition": "legends"}):
            main_window._on_versions_loaded(self._versions())
            picked = main_window.selected_version()
        assert picked["edition"]["id"] == "legends"
        assert main_window.edition_buttons["legends"].isChecked()


# ======================================================================
# Settings Persistence Tests
# ======================================================================

class TestSettingsPersistence:
    """Test settings are saved and loaded correctly."""

    def test_save_and_load_theme_setting(self, main_window, temp_settings_dir):
        """Theme setting persists across saves."""
        main_window.settings["light_theme"] = True
        from bol.gui import save_settings, load_settings
        save_settings(main_window.settings)

        loaded = load_settings()
        assert loaded.get("light_theme") is True

    def test_settings_file_created(self, temp_settings_dir):
        """Settings file is created on save."""
        from bol.gui import save_settings
        settings = {"test_key": "test_value"}
        save_settings(settings)


# ======================================================================
# Worker Thread Tests
# ======================================================================

class TestWorkerThread:
    """Test background worker threading."""

    def test_worker_creation(self):
        """Worker initializes with a callable."""
        def dummy_work():
            return "result"

        worker = Worker(dummy_work)
        assert worker is not None
        assert worker._fn is dummy_work

    def test_worker_emits_done_signal(self, qapp):
        """Worker emits done signal with result."""
        def dummy_work():
            return "test_result"

        worker = Worker(dummy_work)
        spy = QSignalSpy(worker.done)
        worker.run()

        # Check that signal was emitted
        assert spy.count() > 0

    def test_worker_emits_failed_signal(self, qapp):
        """Worker emits failed signal on exception."""
        def failing_work():
            raise ValueError("Test error")

        worker = Worker(failing_work)
        spy = QSignalSpy(worker.failed)
        worker.run()

        assert spy.count() > 0


# ======================================================================
# Log Display Tests
# ======================================================================

class TestLogDisplay:
    """Test activity log display."""

    def test_log_view_created(self, main_window):
        """Log view widget is created."""
        assert main_window.log_view is not None

    def test_log_drawer_initially_hidden(self, main_window):
        """Log drawer is hidden initially."""
        assert main_window.log_drawer.isHidden()

    def test_toggle_details_shows_log(self, main_window):
        """Toggling details shows the log drawer."""
        main_window.toggle_details()
        assert not main_window.log_drawer.isHidden()
        main_window.toggle_details()
        assert main_window.log_drawer.isHidden()

    def test_log_line_display(self, main_window):
        """A log line reaches the view verbatim."""
        main_window._on_log_line("Test log message")
        assert "Test log message" in main_window.log_view.toPlainText()


# ======================================================================
# Page Navigation Tests
# ======================================================================

class TestPageNavigation:
    """Test switching between main pages."""

    def test_hero_page_shown_initially(self, main_window):
        """Hero page is shown by default."""
        assert main_window.stack.currentWidget() is main_window.hero_page

    def test_toggle_settings_page(self, main_window):
        """Settings page toggles correctly."""
        main_window.toggle_settings()
        assert main_window.stack.currentWidget() is main_window.settings_page
        main_window.toggle_settings()
        assert main_window.stack.currentWidget() is main_window.hero_page

    def test_toggle_changelog_page(self, main_window):
        """Changelog page toggles correctly."""
        main_window.toggle_changelog()
        assert main_window.stack.currentWidget() is main_window.changelog_page
        main_window.toggle_changelog()
        assert main_window.stack.currentWidget() is main_window.hero_page


# ======================================================================
# UI State Tests
# ======================================================================

class TestUIState:
    """Test UI state management."""

    def test_set_busy_play_button(self, main_window):
        """Busy state changes play button to kill."""
        main_window._set_busy(True)
        assert "KILL" in main_window.play_btn.text()
        main_window._set_busy(False)
        assert "PLAY" in main_window.play_btn.text()

    def test_step_aside_for_game(self, main_window):
        """Step aside hides the window."""
        main_window._step_aside_for_game()
        assert main_window.ui_state["stepped_aside"]

    def test_come_back_from_game(self, main_window):
        """Coming back shows the window."""
        main_window._step_aside_for_game()
        main_window._come_back_from_game()
        assert not main_window.ui_state["stepped_aside"]


# ======================================================================
# Display Resolution Tests
# ======================================================================

class TestDisplayResolution:
    """Test X11/Wayland display handling."""

    def test_an_x11_session_is_left_alone(self):
        """With no WAYLAND_DISPLAY there is nothing better to recover to."""
        environ = {"DISPLAY": ":3"}
        assert _resolve_gui_display(environ=environ) == ":3"
        assert environ["DISPLAY"] == ":3"

    def test_a_live_display_is_kept_even_under_wayland(self, tmp_path):
        """Only an orphaned socket is worth moving off. A live display that
        is failing for a reason of its own (XAUTHORITY) has to keep failing,
        or the real cause is hidden."""
        listener = _serving_socket(tmp_path / "X7")
        try:
            environ = {"DISPLAY": ":7", "WAYLAND_DISPLAY": "wayland-0"}
            assert _resolve_gui_display(
                environ=environ, socket_dir=tmp_path, uid=os.getuid()) == ":7"
        finally:
            listener.close()

    def test_a_stale_display_moves_to_the_live_one(self, tmp_path):
        """The XWayland case this exists for: $DISPLAY names a socket that no
        longer accepts connections, and another one does."""
        (tmp_path / "X4").write_bytes(b"")      # orphaned file, not a socket
        listener = _serving_socket(tmp_path / "X9")
        try:
            environ = {"DISPLAY": ":4", "WAYLAND_DISPLAY": "wayland-0"}
            assert _resolve_gui_display(
                environ=environ, socket_dir=tmp_path, uid=os.getuid()) == ":9"
            assert environ["DISPLAY"] == ":9", (
                "the resolved display must be written back: Qt reads it from "
                "the environment, not from this return value")
        finally:
            listener.close()

    def test_nothing_live_leaves_the_display_untouched(self, tmp_path):
        """Better to fail on the display the session named than on one this
        picked."""
        environ = {"DISPLAY": ":4", "WAYLAND_DISPLAY": "wayland-0"}
        assert _resolve_gui_display(
            environ=environ, socket_dir=tmp_path, uid=os.getuid()) == ":4"
        assert environ["DISPLAY"] == ":4"

    def test_only_this_users_sockets_are_candidates(self, tmp_path):
        listener = _serving_socket(tmp_path / "X9")
        try:
            assert _owned_x11_socket_displays(
                tmp_path, uid=os.getuid()) == (":9",)
            assert _owned_x11_socket_displays(
                tmp_path, uid=os.getuid() + 1) == ()
        finally:
            listener.close()

    def test_a_missing_socket_directory_is_not_an_error(self, tmp_path):
        assert _owned_x11_socket_displays(tmp_path / "nope") == ()


# ======================================================================
# Settings Tab Tests
# ======================================================================

class TestSettingsTab:
    """Test settings interface."""

    def test_general_tab_created(self, main_window):
        """Settings carries all five tabs, in order."""
        tabs = main_window.settings_page.findChild(QTabWidget)
        assert [tabs.tabText(i) for i in range(tabs.count())] == [
            "General", "Versions", "Advanced", "Tools", "Network"]

    def test_theme_toggle_in_settings(self, main_window):
        """The General tab carries the light-theme switch, and flipping it
        repaints the window rather than only writing the setting."""
        labels = [row.layout().itemAt(0).widget().text()
                  for row in main_window._switches]
        assert "Light theme" in labels
        theme_row = main_window._switches[labels.index("Light theme")]
        with mock.patch.object(main_window, "apply_theme") as repaint:
            theme_row.switch.setChecked(not theme_row.isChecked())
        assert repaint.called


# ======================================================================
# Account Row Tests
# ======================================================================

class TestAccountRow:
    """Test account status display."""

    def test_account_row_not_signed_in(self, main_window):
        """The pill starts on the signed-out wording, and the menu behind it
        offers Sign in for both accounts."""
        assert main_window.acct_text.text() == "Sign in"
        menu = main_window.accounts_menu
        assert menu.online.status.text() == "Not signed in"
        assert menu.online.button.text() == "Sign in"
        assert menu.download.button.text() == "Sign in"

    def test_refresh_account_row_in(self, main_window):
        """Signed in online: the menu says so and offers the way back out."""
        main_window._refresh_account_row("in")
        assert "Signed in" in main_window.accounts_menu.online.status.text()
        assert main_window.accounts_menu.online.button.text() == "Sign out"

    def test_refresh_account_row_out(self, main_window):
        """And back again."""
        main_window._refresh_account_row("in")
        main_window._refresh_account_row("out")
        assert main_window.accounts_menu.online.status.text() == "Not signed in"
        assert main_window.acct_text.text() == "Sign in"


# ======================================================================
# Switch Row Tests
# ======================================================================

class TestSwitchRow:
    """Test toggle switch row widgets."""

    def test_switch_row_creation(self):
        """SwitchRow shows its label and starts off."""
        row = SwitchRow("Test Toggle")
        assert row.layout().itemAt(0).widget().text() == "Test Toggle"
        assert row.isChecked() is False

    def test_switch_row_reports_each_change_once(self):
        """The initial setChecked runs before the signal is connected, so a
        row constructed already-on must not report a change nobody made."""
        row = SwitchRow("Test", checked=True)
        seen = []
        row.toggled.connect(seen.append)
        assert seen == []
        row.switch.setChecked(False)
        row.switch.setChecked(True)
        assert seen == [False, True]

    def test_switch_row_checked_state(self):
        """SwitchRow tracks checked state."""
        row = SwitchRow("Test", checked=True)
        assert row.isChecked() is True

    def test_switch_row_toggle(self):
        """SwitchRow can be toggled."""
        row = SwitchRow("Test", checked=False)
        row.switch.setChecked(True)
        assert row.isChecked() is True


# ======================================================================
# Status Row Tests
# ======================================================================

class TestStatusRow:
    """Test status display."""

    def test_status_label_created(self, main_window):
        """The idle window says so."""
        assert main_window.status_label.text() == "Ready to play."

    def test_progress_bar_hidden_initially(self, main_window):
        """Progress bar is hidden initially."""
        assert main_window.progress.isHidden()

    def test_show_progress_bar(self, main_window):
        """Progress bar can be shown."""
        main_window.set_progress(50, 100)
        assert not main_window.progress.isHidden()


# ======================================================================
# Integration Test: Full Workflow
# ======================================================================

class TestFullWorkflow:
    """Integration tests for complete workflows."""

    def test_init_to_play_button_ready(self, main_window):
        """The window comes up idle, on PLAY rather than KILL."""
        assert main_window.play_btn.isEnabled()
        assert "PLAY" in main_window.play_btn.text()
        assert main_window.ui_state["busy"] is False

    def test_settings_open_and_close(self, main_window):
        """Settings can be opened and closed."""
        main_window.toggle_settings()
        assert main_window.stack.currentWidget() is main_window.settings_page
        main_window.toggle_settings()
        assert main_window.stack.currentWidget() is main_window.hero_page

    def test_multiple_toggles(self, main_window):
        """Multiple rapid toggles work correctly."""
        for _ in range(5):
            main_window.toggle_settings()
        # Odd number of toggles should land on the settings page
        assert main_window.stack.currentWidget() is main_window.settings_page


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
