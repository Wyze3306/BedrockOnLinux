"""Shortcuts to one Minecraft each (#392): what their files say, where they
go, what is checked, how they follow the launcher and the profiles, and what
starting one does. No game, no Wine, no desktop and no D-Bus is touched."""
# SPDX-License-Identifier: MIT

import argparse
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import games, profiles, shortcuts
from bol.log import BolError
from tests.guiharness import qt_app


def _build(games_root, edition, version):
    folder = games_root / edition / version
    folder.mkdir(parents=True)
    (folder / "Minecraft.Windows.exe").write_bytes(b"MZ")
    (folder / "AppxManifest.xml").write_text(
        '<Package><Identity Name="Microsoft.MinecraftUWP"/></Package>')
    return folder


class _Isolated(unittest.TestCase):
    """A main installation, an app menu and a desktop of its own."""

    @classmethod
    def setUpClass(cls):
        # Icons are painted with Qt: the suite's one QApplication, made
        # before shortcuts would make a QGuiApplication of its own.
        qt_app()

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.tmp = Path(self._dir.name)
        self.base = self.tmp / "data" / "bedrock-on-linux"
        self.games = self.base / "games"
        self.games.mkdir(parents=True)
        self.apps = self.tmp / "data" / "applications"
        self.desktop = self.tmp / "Bureau"
        self.desktop.mkdir()
        self.argv = ["/opt/My Games/BedrockOnLinux.AppImage"]
        for patcher in (
                mock.patch.object(shortcuts, "XDG_DATA_HOME",
                                  self.tmp / "data"),
                mock.patch.object(shortcuts, "DATA", self.base),
                mock.patch.object(games, "GAMES", self.games),
                mock.patch.object(profiles, "DATA", self.base),
                mock.patch.object(shortcuts, "launcher_argv",
                                  side_effect=lambda: list(self.argv)),
                mock.patch.object(shortcuts, "in_flatpak",
                                  return_value=False),
                mock.patch.object(shortcuts, "_trust"),
                mock.patch.dict(os.environ,
                                {"XDG_DESKTOP_DIR": str(self.desktop)}),
                mock.patch("bol.storeart.logo_bytes", return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        _build(self.games, "release", "1.21.120.4")
        _build(self.games, "release", "1.26.65.1")
        _build(self.games, "preview", "1.26.70.26")

    def make(self, **fields):
        fields.setdefault("edition", "release")
        return shortcuts.create(fields, base_data=self.base)

    def registry(self):
        return json.loads((self.base / "shortcuts" / "shortcuts.json")
                          .read_text(encoding="utf-8"))


class DesktopFileTests(_Isolated):

    def test_menu_entry_runs_the_shortcut_through_the_launcher(self):
        item = self.make(version="1.21.120.4")
        entry = (self.apps / f"bedrock-on-linux-shortcut-{item['id']}"
                             ".desktop")
        text = entry.read_text(encoding="utf-8")
        self.assertIn("Name=Minecraft Bedrock (1.21.120.4)\n", text)
        self.assertIn(f'Exec="/opt/My Games/BedrockOnLinux.AppImage" '
                      f"shortcuts run {item['id']}\n", text)
        self.assertIn(f"X-BedrockOnLinux-Shortcut={item['id']}\n", text)
        icon = self.base / "shortcuts" / "icons" / f"{item['id']}.png"
        self.assertIn(f"Icon={icon}\n", text)
        self.assertTrue(icon.read_bytes().startswith(b"\x89PNG"))
        self.assertFalse(list(self.desktop.iterdir()))

    def test_desktop_file_is_named_after_it_and_executable(self):
        item = self.make(desktop=True, menu=False, name="Old one")
        path = self.desktop / "Old one.desktop"
        self.assertEqual(item["desktop_file"], str(path))
        self.assertTrue(path.stat().st_mode & stat.S_IXUSR)
        self.assertFalse(self.apps.exists() and list(self.apps.iterdir()))

    def test_a_desktop_file_someone_else_owns_is_never_overwritten(self):
        theirs = self.desktop / "Minecraft Bedrock (Latest).desktop"
        theirs.write_text("[Desktop Entry]\nName=theirs\n")
        item = self.make(desktop=True)
        self.assertEqual(theirs.read_text(), "[Desktop Entry]\nName=theirs\n")
        self.assertEqual(Path(item["desktop_file"]).name,
                         "Minecraft Bedrock (Latest) (2).desktop")
        shortcuts.remove(item["id"], base_data=self.base)
        self.assertTrue(theirs.exists())
        self.assertFalse(Path(item["desktop_file"]).exists())

    def test_exec_quoting_follows_the_desktop_entry_specification(self):
        item = {"id": "0123abcd"}
        line = shortcuts.exec_line(item, ['/a b/100% "x" $y `z` \\w'])
        self.assertEqual(
            line,
            '"/a b/100%% \\\\"x\\\\" \\\\$y \\\\`z\\\\` \\\\\\\\w" '
            "shortcuts run 0123abcd")
        self.assertEqual(
            shortcuts.exec_line(item, ["/usr/bin/bedrock-on-linux"]),
            "/usr/bin/bedrock-on-linux shortcuts run 0123abcd")

    def test_strings_are_escaped(self):
        self.assertEqual(shortcuts._string(" a\\b\nc"), "\\sa\\\\b\\nc")

    def test_comment_names_the_build_and_the_profile(self):
        item = self.make(version="1.21.120.4", editor=True)
        text = shortcuts.menu_file(item).read_text(encoding="utf-8")
        self.assertIn("Comment=Minecraft Bedrock 1.21.120.4 · profile "
                      "Default · Bedrock Editor · BedrockOnLinux\n", text)

    def test_portal_entry_leaves_name_icon_and_exec_to_the_portal(self):
        item = self.make()
        text = shortcuts.desktop_entry(item, base_data=self.base,
                                       portal=True)
        self.assertNotIn("Name=", text)
        self.assertNotIn("Icon=", text)
        self.assertIn(f"Exec=bedrock-on-linux shortcuts run {item['id']}\n",
                      text)
        self.assertTrue(shortcuts.portal_file_id(item).startswith(
            "io.github.wyze3306.BedrockOnLinux.shortcut_"))


class _FakeBus:
    """The session bus as the DynamicLauncher portal answers on it: the
    dialog's answer comes back as a Response signal, after the call."""

    def __init__(self, code=0, results=None):
        self.code = code
        self.results = results if results is not None else {}
        self.calls = []
        self.slots = []

    def isConnected(self):  # noqa: N802 - Qt name
        return True

    def baseService(self):  # noqa: N802 - Qt name
        return ":1.42"

    def connect(self, service, path, interface, name, receiver, slot):
        self.slots.append((path, interface, name, slot))
        self.receiver = receiver
        return True

    def disconnect(self, *_args):
        return True

    def call(self, message, timeout=-1):
        from PySide6.QtCore import QTimer
        self.calls.append((message.member(), message.arguments()))
        if message.member() == "PrepareInstall":
            QTimer.singleShot(0, lambda: self.receiver.response(
                self.code, self.results))
        return mock.Mock(**{"type.return_value": None})


class PortalTests(_Isolated):
    """What is sent to the real portal. Checked against
    xdg-desktop-portal 1.20 with its GTK dialog, inside the Flatpak."""

    def install(self, bus, item):
        with mock.patch("PySide6.QtDBus.QDBusConnection") as connection:
            connection.sessionBus.return_value = bus
            return shortcuts.portal_install(item, b"png", timeout=5)

    def test_install_takes_the_name_typed_in_the_dialog(self):
        item = self.make(version="1.21.120.4")
        bus = _FakeBus(results={"token": "t0k", "name": "My build"})
        self.assertEqual(self.install(bus, item), "My build")
        (path, interface, name, slot), = bus.slots
        self.assertTrue(path.startswith(
            "/org/freedesktop/portal/desktop/request/1_42/bol"))
        self.assertEqual((interface, name),
                         ("org.freedesktop.portal.Request", "Response"))
        # A slot typed like this is how the results arrive as a dict.
        self.assertEqual(slot, "1response(uint,QVariantMap)")
        (prepare, args), (install, install_args) = bus.calls
        self.assertEqual(prepare, "PrepareInstall")
        self.assertEqual(args[1], "Minecraft Bedrock (1.21.120.4)")
        # The portal refuses a signed launcher_type, and its default fits.
        self.assertNotIn("launcher_type", args[3])
        self.assertEqual(args[3]["handle_token"], path.rsplit("/", 1)[1])
        self.assertEqual(install, "Install")
        self.assertEqual(install_args[:2],
                         ["t0k", shortcuts.portal_file_id(item)])
        self.assertIn("Exec=bedrock-on-linux shortcuts run ", install_args[2])

    def test_a_declined_dialog_installs_nothing(self):
        item = self.make()
        bus = _FakeBus(code=1)
        with self.assertRaises(shortcuts.PortalError):
            self.install(bus, item)
        self.assertEqual([call[0] for call in bus.calls], ["PrepareInstall"])

    def test_a_declined_shortcut_leaves_nothing_behind(self):
        with mock.patch.object(shortcuts, "in_flatpak", return_value=True), \
                mock.patch.object(shortcuts, "portal_install",
                                  side_effect=shortcuts.PortalError("no")), \
                mock.patch.object(shortcuts, "portal_uninstall"):
            with self.assertRaises(shortcuts.PortalError):
                shortcuts.create({"edition": "release"})
        self.assertEqual(shortcuts.load(), [])
        self.assertEqual(list(shortcuts.icons_dir().glob("*.png")), [])


class ChecksTests(_Isolated):

    def test_defaults(self):
        item = self.make()
        self.assertEqual(item["name"], "Minecraft Bedrock (Latest)")
        self.assertEqual(item["version"], shortcuts.LATEST)
        self.assertEqual(item["start"], shortcuts.START_LAUNCHER)
        self.assertEqual(item["profile"], "")
        self.assertTrue(item["menu"])
        self.assertFalse(item["desktop"])
        self.assertRegex(item["id"], r"^[0-9a-f]{8}$")

    def test_a_title_the_store_serves_once_has_no_version(self):
        item = self.make(edition="dungeons", version="1.17.0.0")
        self.assertEqual(item["version"], shortcuts.LATEST)
        self.assertEqual(item["name"], "Minecraft Dungeons")

    def test_refusals(self):
        for fields, words in (
                ({"edition": "bedrock2"}, "Unknown game"),
                ({"edition": "dungeons", "editor": True}, "Bedrock Editor"),
                ({"env": "MANGOHUD"}, "NAME=value"),
                ({"env": "A='unterminated"}, "cannot be read"),
                ({"menu": False, "desktop": False}, "app menu"),
                ({"start": "teleport"}, "'launcher' or 'direct'"),
                ({"version": "../../etc"}, "not a version"),
                ({"name": "x" * 81}, "at most 80"),
                ({"profile": "Nobody"}, "no profile called")):
            with self.subTest(fields=fields):
                fields.setdefault("edition", "release")
                with self.assertRaises(BolError) as caught:
                    shortcuts.create(fields, base_data=self.base)
                self.assertIn(words, str(caught.exception))
        self.assertFalse((self.base / "shortcuts" / "shortcuts.json")
                         .exists())

    def test_one_build_never_installs_another(self):
        item = self.make(version="1.21.120.4", update=True)
        self.assertFalse(item["update"])
        self.assertTrue(self.make(update=True)["update"])

    def test_flatpak_cannot_reach_the_desktop(self):
        with mock.patch.object(shortcuts, "in_flatpak", return_value=True):
            with self.assertRaises(BolError) as caught:
                shortcuts._check({"edition": "release", "desktop": True})
        self.assertIn("Flatpak", str(caught.exception))

    def test_profile_is_kept_by_its_folder(self):
        profiles.create_profile("Second Account", base_data=self.base)
        item = self.make(profile="second account")
        self.assertEqual(item["profile"], "second-account")
        self.assertEqual(shortcuts.profile_label(item, self.base),
                         "Second Account")


class ResolveTests(_Isolated):

    def test_latest_is_the_newest_build_installed(self):
        item = self.make()
        entry, version, folder = shortcuts.resolve(item)
        self.assertEqual((entry["id"], version), ("release", "1.26.65.1"))
        self.assertEqual(folder, self.games / "release" / "1.26.65.1")

    def test_a_build_that_is_gone_is_reported_not_downloaded(self):
        item = self.make(version="1.20.0.1")
        with self.assertRaises(BolError) as caught:
            shortcuts.resolve(item)
        self.assertIn("1.20.0.1 is not installed any more",
                      str(caught.exception))
        item = self.make(edition="education")
        with self.assertRaises(BolError) as caught:
            shortcuts.resolve(item)
        self.assertIn("Minecraft Education is not installed",
                      str(caught.exception))


class ListTests(_Isolated):

    def test_get_by_id_or_by_name(self):
        item = self.make(name="My Build")
        self.assertEqual(shortcuts.get(item["id"], self.base)["id"],
                         item["id"])
        self.assertEqual(shortcuts.get("my build", self.base)["id"],
                         item["id"])
        with self.assertRaises(BolError):
            shortcuts.get("nope", self.base)

    def test_edit_moves_the_files_with_it(self):
        item = self.make(desktop=True)
        old = Path(item["desktop_file"])
        item = shortcuts.edit(item["id"], {"name": "Renamed", "menu": False},
                              base_data=self.base)
        self.assertFalse(old.exists())
        self.assertTrue((self.desktop / "Renamed.desktop").exists())
        self.assertFalse(shortcuts.menu_file(item).exists())
        self.assertEqual(self.registry()["shortcuts"][0]["name"], "Renamed")

    def test_remove_deletes_its_files_and_icon(self):
        item = self.make(desktop=True)
        icon = shortcuts.icon_file(item, self.base)
        shortcuts.remove(item["id"], base_data=self.base)
        self.assertFalse(icon.exists())
        self.assertFalse(shortcuts.menu_file(item).exists())
        self.assertFalse(Path(item["desktop_file"]).exists())
        self.assertEqual(self.registry()["shortcuts"], [])

    def test_custom_icon_is_kept_and_badged(self):
        from PySide6.QtGui import QColor, QImage
        picture = self.tmp / "pic.jpg"
        image = QImage(600, 300, QImage.Format_RGB32)
        image.fill(QColor("#ff0000"))
        image.save(str(picture), "JPG")
        item = shortcuts.create({"edition": "release",
                                 "version": "1.21.120.4"},
                                icon_picture=picture, base_data=self.base)
        self.assertEqual(item["icon"], shortcuts.ICON_CUSTOM)
        source = QImage(str(self.base / "shortcuts" / "icons"
                            / f"{item['id']}-source.png"))
        self.assertEqual((source.width(), source.height()), (512, 256))
        icon = QImage(str(shortcuts.icon_file(item, self.base)))
        self.assertEqual((icon.width(), icon.height()), (256, 256))

    def test_a_file_that_is_no_picture_is_refused(self):
        bogus = self.tmp / "notes.png"
        bogus.write_text("hello")
        with self.assertRaises(BolError):
            shortcuts.create({"edition": "release"}, icon_picture=bogus,
                             base_data=self.base)


class RefreshTests(_Isolated):

    def test_files_follow_the_launcher_when_it_moves(self):
        item = self.make(desktop=True)
        self.assertEqual(shortcuts.refresh(base_data=self.base), 0)
        self.argv = ["/home/me/Apps/BedrockOnLinux-3.0.1.AppImage"]
        self.assertEqual(shortcuts.refresh(base_data=self.base), 1)
        for path in (shortcuts.menu_file(item), Path(item["desktop_file"])):
            self.assertIn("Exec=/home/me/Apps/BedrockOnLinux-3.0.1.AppImage "
                          "shortcuts run", path.read_text(encoding="utf-8"))
        self.assertEqual(shortcuts.refresh(base_data=self.base), 0)

    def test_a_file_deleted_by_hand_stays_deleted(self):
        item = self.make(desktop=True)
        shortcuts.menu_file(item).unlink()
        self.argv = ["/usr/bin/bedrock-on-linux"]
        shortcuts.refresh(base_data=self.base)
        self.assertFalse(shortcuts.menu_file(item).exists())
        stored = self.registry()["shortcuts"][0]
        self.assertFalse(stored["menu"])
        self.assertTrue(stored["desktop"])
        shortcuts.refresh(force=True, base_data=self.base)
        self.assertFalse(shortcuts.menu_file(item).exists())

    def test_nothing_to_do_without_shortcuts(self):
        self.argv = ["/elsewhere"]
        self.assertEqual(shortcuts.refresh(base_data=self.base), 0)
        self.assertFalse((self.base / "shortcuts" / "shortcuts.json")
                         .exists())


class ProfileFollowTests(_Isolated):

    def setUp(self):
        super().setUp()
        self.apps_dir = self.tmp / "profile-apps"
        profiles.create_profile("Rec", base_data=self.base)

    def test_rename_follows(self):
        item = self.make(profile="Rec")
        profiles.rename_profile("Rec", "Recording", base_data=self.base,
                                applications_dir=self.apps_dir)
        stored = shortcuts.get(item["id"], self.base)
        self.assertEqual(stored["profile"], "recording")
        self.assertIn("profile Recording",
                      shortcuts.menu_file(stored).read_text(encoding="utf-8"))

    def test_delete_removes_its_shortcuts(self):
        mine = self.make(profile="Rec")
        other = self.make()
        profiles.delete_profile("Rec", base_data=self.base,
                                applications_dir=self.apps_dir)
        self.assertEqual([i["id"] for i in shortcuts.load(self.base)],
                         [other["id"]])
        self.assertFalse(shortcuts.menu_file(mine).exists())


class LauncherCommandTests(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.tmp = Path(self._dir.name)

    def file(self, relative):
        path = self.tmp / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/usr/bin/env python3\n")
        return path

    def test_appimage_is_the_file_not_its_mount(self):
        image = self.file("Apps/BoL.AppImage")
        self.assertEqual(
            shortcuts.launcher_argv(environ={"APPIMAGE": str(image)},
                                    argv0="/tmp/.mount_x/usr/bin/x"),
            [str(image)])

    def test_pyz_runs_under_python(self):
        pyz = self.file("bol.pyz")
        self.assertEqual(
            shortcuts.launcher_argv(environ={}, argv0=str(pyz),
                                    executable="/usr/bin/python3",
                                    which=lambda _: "/usr/bin/x"),
            ["/usr/bin/python3", str(pyz)])

    def test_python_m_bol_keeps_its_source_tree(self):
        main = self.file("src/bol/__main__.py")
        self.assertEqual(
            shortcuts.launcher_argv(environ={}, argv0=str(main),
                                    executable="/usr/bin/python3",
                                    which=lambda _: None),
            ["env", f"PYTHONPATH={(self.tmp / 'src').resolve()}",
             "/usr/bin/python3", "-m", "bol"])

    def test_nix_or_guix_wrapper_uses_the_profile_link(self):
        real = self.file("store/abc-bol/bin/.bedrock-on-linux-wrapped")
        wrapper = self.file("store/abc-bol/bin/bedrock-on-linux")
        link = self.tmp / "profile/bin/bedrock-on-linux"
        link.parent.mkdir(parents=True)
        link.symlink_to(wrapper)
        self.assertEqual(
            shortcuts.launcher_argv(environ={}, argv0=str(real),
                                    which=lambda _: str(link)),
            [str(link)])

    def test_a_checkout_beside_an_installed_copy_points_at_itself(self):
        script = self.file("checkout/bedrock-on-linux")
        installed = self.file("usr/bin/bedrock-on-linux")
        self.assertEqual(
            shortcuts.launcher_argv(environ={}, argv0=str(script),
                                    which=lambda _: str(installed)),
            [str(script.resolve())])

    def test_desktop_folder_from_user_dirs(self):
        config = self.tmp / "config"
        config.mkdir()
        (config / "user-dirs.dirs").write_text(
            '# comment\nXDG_DESKTOP_DIR="$HOME/Bureau"\n')
        self.assertEqual(
            shortcuts.desktop_dir(environ={}, config_home=config,
                                  home=self.tmp),
            self.tmp / "Bureau")
        self.assertEqual(
            shortcuts.desktop_dir(environ={}, config_home=self.tmp / "none",
                                  home=self.tmp),
            self.tmp / "Desktop")
        # "$HOME/" alone means no desktop folder of its own.
        (config / "user-dirs.dirs").write_text('XDG_DESKTOP_DIR="$HOME/"\n')
        self.assertEqual(
            shortcuts.desktop_dir(environ={}, config_home=config,
                                  home=self.tmp),
            self.tmp / "Desktop")


class RunTests(_Isolated):

    def test_profile_hop_restarts_in_the_profile(self):
        folder = profiles.create_profile("Rec", base_data=self.base)
        item = self.make(profile="Rec")
        calls = []
        with mock.patch.object(shortcuts.sys, "argv",
                               ["/usr/bin/bedrock-on-linux", "shortcuts",
                                "run", item["id"]]), \
                mock.patch.object(shortcuts.sys, "orig_argv",
                                  ["/usr/bin/python3",
                                   "/usr/bin/bedrock-on-linux", "shortcuts",
                                   "run", item["id"]], create=True), \
                mock.patch.object(shortcuts.sys, "executable",
                                  "/usr/bin/python3"):
            shortcuts.enter_profile(item, base_data=self.base,
                                    execve=lambda *a: calls.append(a))
        (path, cmd, env), = calls
        self.assertEqual(cmd, ["/usr/bin/python3", "/usr/bin/bedrock-on-linux",
                               "shortcuts", "run", item["id"]])
        self.assertEqual(env["BOL_HOME"], str(folder.resolve()))
        self.assertEqual(env[shortcuts._HOP_ENV], item["id"])

    def test_no_hop_in_its_own_profile_and_no_loop(self):
        item = self.make()
        shortcuts.enter_profile(item, base_data=self.base,
                                execve=lambda *a: self.fail("hopped"))
        profiles.create_profile("Rec", base_data=self.base)
        other = self.make(profile="Rec")
        with mock.patch.dict(os.environ, {shortcuts._HOP_ENV: other["id"]}):
            with self.assertRaises(BolError):
                shortcuts.enter_profile(other, base_data=self.base,
                                        execve=lambda *a: None)

    def _play(self, item, previous, running=False, launch_error=None):
        selected = {"game_dir": previous}
        used = []

        def use(folder):
            used.append(Path(folder))
            selected["game_dir"] = str(folder)

        launched = []

        def launch(**options):
            launched.append(options)
            if launch_error:
                raise launch_error
            return 0

        updates = []
        lock = mock.MagicMock()
        with mock.patch("bol.games.use_game_dir", side_effect=use), \
                mock.patch("bol.util.load_settings",
                           side_effect=lambda: dict(selected)), \
                mock.patch("bol.prefix.prefix_operation_lock",
                           return_value=lock), \
                mock.patch("bol.prefix._mc_running", return_value=running), \
                mock.patch("bol.auth.msa_signed_in", return_value=True), \
                mock.patch("bol.supervision.install"), \
                mock.patch.dict(os.environ, {}, clear=False):
            try:
                shortcuts.play(item, launch=launch,
                               update=lambda always: updates.append(always))
            finally:
                env = dict(os.environ)
        return used, launched, updates, selected, env

    def test_play_selects_its_build_then_gives_the_choice_back(self):
        item = self.make(version="1.21.120.4", start="direct", editor=True,
                         hidden=True, env="MANGOHUD=1 FOO='a b'")
        previous = str(self.games / "release" / "1.26.65.1")
        used, launched, updates, selected, env = self._play(item, previous)
        self.assertEqual(used, [self.games / "release" / "1.21.120.4",
                                Path(previous)])
        self.assertEqual(launched, [{"editor": True, "hidden": True}])
        self.assertEqual(updates, [])
        self.assertEqual(selected["game_dir"], previous)
        self.assertEqual((env["MANGOHUD"], env["FOO"]), ("1", "a b"))

    def test_latest_may_update_first(self):
        item = self.make(start="direct", update=True)
        used, launched, updates, *_ = self._play(item, "")
        self.assertEqual(updates, [True])
        self.assertEqual(launched, [{"editor": False}])
        self.assertEqual(used, [self.games / "release" / "1.26.65.1"])

    def test_choice_comes_back_when_the_launch_fails(self):
        item = self.make(version="1.21.120.4", start="direct")
        previous = str(self.games / "release" / "1.26.65.1")
        with self.assertRaises(BolError):
            self._play(item, previous, launch_error=BolError("no engine"))

    def test_a_running_game_is_left_alone(self):
        item = self.make(version="1.21.120.4", start="direct")
        with self.assertRaises(BolError) as caught:
            self._play(item, "", running=True)
        self.assertIn("already running", str(caught.exception))

    def test_run_opens_the_launcher_for_a_launcher_shortcut(self):
        item = self.make()
        with mock.patch.object(shortcuts, "enter_profile"), \
                mock.patch("bol.cli._open_gui") as opened, \
                mock.patch.dict(os.environ, {}):
            with mock.patch.object(shortcuts, "load",
                                   return_value=[item]):
                shortcuts.run(item["id"])
            self.assertEqual(os.environ[shortcuts.START_ENV], item["id"])
        opened.assert_called_once_with()

    def test_run_plays_a_direct_shortcut(self):
        item = self.make(start="direct")
        with mock.patch.object(shortcuts, "enter_profile"), \
                mock.patch.object(shortcuts, "play") as played, \
                mock.patch.object(shortcuts, "load", return_value=[item]):
            shortcuts.run(item["id"])
        played.assert_called_once_with(item)


class CommandLineTests(unittest.TestCase):

    def parse(self, *argv):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="cmd")
        shortcuts.add_arguments(sub)
        return parser.parse_args(list(argv))

    def test_create_fields(self):
        a = self.parse("shortcuts", "create", "--mc", "Preview", "--version",
                       "1.26.70.24", "--profile", "Rec", "--start", "direct",
                       "--no-menu", "--desktop", "--editor", "--env",
                       "MANGOHUD=1", "--icon", "launcher")
        fields, picture = shortcuts._fields(a)
        self.assertIsNone(picture)
        self.assertEqual(fields, {
            "edition": "preview", "version": "1.26.70.24", "profile": "Rec",
            "start": "direct", "menu": False, "desktop": True,
            "editor": True, "env": "MANGOHUD=1", "icon": "launcher"})

    def test_edit_changes_only_what_is_given(self):
        a = self.parse("shortcuts", "edit", "abcd1234", "--icon",
                       "/tmp/x.png")
        fields, picture = shortcuts._fields(a)
        self.assertEqual((fields, picture), ({}, "/tmp/x.png"))

    def test_every_command_parses(self):
        for argv in (("list",), ("run", "x"), ("remove", "x"),
                     ("command", "x"), ("refresh",)):
            self.assertEqual(self.parse("shortcuts", *argv).shortcuts_cmd,
                             argv[0])


if __name__ == "__main__":
    unittest.main()
