"""Collecting a problem report and getting it out: relay and browser.

The relay tests post to a real relay on loopback that files into a fake
GitHub (tests/reportharness.py). No request leaves the machine.
"""
# SPDX-License-Identifier: MIT

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from bol import report
from tests import reportharness
from tests.test_scrub import KNOWN, SECRETS, TRAPPED

SETTINGS = {
    "proton_source": "winegdk", "renderer": "opengl",
    "limit_frame_rate": False, "mc_version": "1.26.60.5",
    "game_dir": "/data/people/marguerite/Jeux de Marguerite/mc",
    "injector_dll_path": "/data/people/marguerite/Mods/secret.dll",
    "injector_dll_url": "https://mods.example.net/x.dll?key=abc",
    "proton_dir": "", "gamescope": "-f -W 2560",
    "custom_env": ("VKD3D_CONFIG=dxr MANGOHUD=1 MY_API_KEY=s3cr3tvalue "
                   "HTTP_PROXY=http://marguerite:pw@proxy WINEDEBUG=+gdkc"),
}


class CollectionCase(unittest.TestCase):
    """A report collected from a logs folder full of traps."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.logs = Path(self.directory.name) / "logs"
        self.logs.mkdir()
        (self.logs / "minecraft.log").write_text(TRAPPED, encoding="utf-8")
        (self.logs / "proton.log").write_text(TRAPPED, encoding="utf-8")
        (self.logs / "store-login.log").write_text(TRAPPED, encoding="utf-8")
        # Never part of a report, whatever is in the folder.
        (self.logs / "xcurl.log").write_text(
            "GET /users/me -> {\"gamertag\": \"xXDragonSlayer42Xx\"}")
        (self.logs / "notes.txt").write_text("marguerite's notes")
        self.report = report.collect(
            activity_log=TRAPPED, settings=dict(SETTINGS),
            logs_dir=self.logs, identity=list(KNOWN))
        self.report.set_message(
            "Crash for marguerite.duval@courriel.example",
            "It crashed on La Ferme de Mamie.\n" + TRAPPED)


class CollectionTests(CollectionCase):
    def outgoing(self):
        """Every form the report can leave in, joined."""
        payload = json.dumps(self.report.payload(), ensure_ascii=False)
        return "\n".join((payload, self.report.full_text(),
                          report.issue_url(self.report, attached="r.txt")))

    def test_nothing_personal_is_in_anything_that_leaves(self):
        from urllib.parse import unquote
        outgoing = self.outgoing()
        outgoing = (outgoing + unquote(outgoing)).lower()
        leaked = {what: value for what, value in SECRETS.items()
                  if value.lower() in outgoing
                  # Typed by the player, in prose the relay cannot name:
                  # removed on the launcher because the launcher knows them.
                  and what not in ("private folder",)}
        self.assertEqual(leaked, {})
        for secret in ("s3cr3tvalue", "MY_API_KEY", "HTTP_PROXY", "secret.dll",
                       "Jeux de Marguerite", "mods.example.net"):
            self.assertNotIn(secret.lower(), outgoing, secret)

    def test_the_pieces_a_diagnosis_needs_are_there(self):
        keys = [piece.key for piece in self.report.pieces]
        for key in ("launcher", "system", "graphics", "components", "game",
                    "settings", "checks", "activity", "log:minecraft.log",
                    "log:proton.log", "log:store-login.log"):
            self.assertIn(key, keys)
        summary = self.report.summary()
        for needle in (f"version {report.VERSION}", "engine pinned",
                       report.WINEGDK_BUILD_REV, "xodus pinned",
                       report.XODUS_REV, "kernel", "distribution", "session",
                       "cpu", "memory", "fast sync", "renderer",
                       "limit_frame_rate", "gpu", "mesa version",
                       "nvidia driver", "install"):
            self.assertIn(needle, summary)

    def test_only_listed_logs_are_read(self):
        text = self.report.full_text()
        self.assertNotIn("notes", text)
        self.assertFalse(any("xcurl" in piece.key
                             for piece in self.report.pieces))

    def test_only_listed_settings_and_variables_are_reported(self):
        settings = self.report.piece("settings").text
        self.assertIn("renderer", settings)
        self.assertIn("opengl", settings)
        self.assertIn("VKD3D_CONFIG=dxr", settings)
        self.assertIn("WINEDEBUG=+gdkc", settings)
        self.assertIn("2 other variable(s) not reported", settings)
        for hidden in ("game_dir", "injector_dll_path", "injector_dll_url",
                       "MY_API_KEY", "HTTP_PROXY"):
            self.assertNotIn(hidden, settings)

    def test_an_unticked_piece_is_in_nothing(self):
        self.report.piece("log:minecraft.log").included = False
        self.report.piece("settings").included = False
        names = [name for name, _ in self.report.attachments()]
        self.assertNotIn("Game log (minecraft.log)", names)
        self.assertNotIn("Launcher settings", self.report.summary())
        self.assertNotIn("Game log (minecraft.log)", self.report.full_text())
        self.assertNotIn("renderer", report.issue_url(self.report))

    def test_the_message_is_bounded_and_single_lined(self):
        self.report.set_message("a\nb  c" + "x" * 500, "y" * 9000)
        self.assertNotIn("\n", self.report.title)
        self.assertLessEqual(len(self.report.title), report.MAX_TITLE)
        self.assertLessEqual(len(self.report.comment), report.MAX_COMMENT)

    def test_what_was_removed_is_summarised(self):
        self.assertIn("tokens", self.report.removed())
        self.assertIn("lines removed", self.report.removed())

    def test_a_broken_collector_costs_one_piece_not_the_report(self):
        with mock.patch.object(report, "_collect_graphics",
                               side_effect=RuntimeError("no sysfs")):
            broken = report.collect(settings={}, logs_dir=self.logs,
                                    identity=list(KNOWN))
        self.assertIn("unavailable: RuntimeError", broken.piece("graphics").text)
        self.assertIsNotNone(broken.piece("system"))

    def test_the_payload_fits_one_request(self):
        (self.logs / "minecraft.log").write_text(
            "\n".join(f"{i} unique line of a very long log" for i in
                      range(200_000)))
        (self.logs / "proton.log").write_text(
            "\n".join(f"{i} another unique line" for i in range(200_000)))
        big = report.collect(settings={}, logs_dir=self.logs,
                             identity=list(KNOWN))
        big.set_message("big", "c" * 4000)
        self.assertLess(len(json.dumps(big.payload())),
                        report.MAX_PAYLOAD + 20_000)


class LogReadingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "x.log"

    def test_a_short_log_is_read_whole(self):
        self.path.write_text("a\nb\nc\n")
        self.assertEqual(report.read_log(self.path, 1000), "a\nb\nc")

    def test_a_long_log_keeps_its_head_and_its_tail(self):
        self.path.write_text("\n".join(f"line {i}" for i in range(20000)))
        text = report.read_log(self.path, 4000)
        self.assertLess(len(text), 4400)
        self.assertTrue(text.startswith("line 0\nline 1\n"))
        self.assertTrue(text.endswith("line 19999"))
        self.assertIn("line(s) omitted", text)

    def test_a_flood_of_one_message_is_folded(self):
        flood = [f"{i}.000:0118:fixme:d3d:stub unimplemented" for i in range(500)]
        self.path.write_text("start\n" + "\n".join(flood) + "\nend\n")
        self.assertEqual(report.read_log(self.path, 4000).split("\n"), [
            "start", "0.000:0118:fixme:d3d:stub unimplemented",
            "    [repeated 499 more time(s)]", "end"])

    def test_a_huge_log_is_not_read_whole(self):
        with self.path.open("wb") as handle:
            handle.write(b"first line\n")
            handle.seek(50_000_000)
            handle.write(b"\nlast line\n")
        text = report.read_log(self.path, 2000)
        self.assertIn("first line", text)
        self.assertTrue(text.endswith("last line"))

    def test_a_missing_log_is_empty(self):
        self.assertEqual(report.read_log(self.path, 100), "")


class ProbeTests(unittest.TestCase):
    def test_the_session_is_named(self):
        self.assertEqual(report.session_summary(
            {"XDG_CURRENT_DESKTOP": "KDE", "XDG_SESSION_TYPE": "wayland",
             "WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}),
            ("KDE", "wayland (XWayland available)"))
        self.assertEqual(report.session_summary(
            {"XDG_CURRENT_DESKTOP": "gamescope", "SteamDeck": "1",
             "DISPLAY": ":0"}),
            ("gamescope", "x11 (inside Gamescope, SteamOS)"))
        self.assertEqual(report.session_summary({}), ("unknown", "none"))

    def test_the_nvidia_version_is_read_without_loading_the_driver(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory) / "version"
            proc.write_text("NVRM version: NVIDIA UNIX x86_64 Kernel Module  "
                            "550.163.01  Tue Apr  8 12:41:17 UTC 2026\n")
            self.assertEqual(report.nvidia_driver_version(
                proc, Path(directory) / "none"), "550.163.01")
            self.assertIsNone(report.nvidia_driver_version(
                Path(directory) / "none", Path(directory) / "none"))

    def test_the_mesa_version_comes_from_a_file_name(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "libgallium-25.0.7-2.so").write_text("")
            self.assertEqual(report.mesa_version([directory]), "25.0.7-2")
            with mock.patch.object(report.shutil, "which", return_value=None):
                self.assertIsNone(report.mesa_version([directory + "/none"]))

    def test_the_installed_engine_is_read_from_its_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "engine-manifest.json").write_text(json.dumps(
                {"build_rev": "wow64-archs-native23",
                 "components": {"dxvk": "3.0.1"}}))
            self.assertEqual(report.installed_engine(directory),
                             ("wow64-archs-native23", {"dxvk": "3.0.1"}))
            self.assertEqual(report.installed_engine(directory + "/none"),
                             (None, {}))

    def test_identity_names_the_player_without_reporting_them(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            (data / "winegdk-preauth").mkdir()
            (data / "winegdk-preauth" / "device.json").write_text(json.dumps({
                "xbl_gamertag": "xXDragonSlayer42Xx",
                "xbl_xuid": "2535428504219876", "xbl_uhs": "13371337133713",
                "xbl_token": "eyJ0b2tlbi1ib2R5", "xbl_token_expiry": "2026-01-01",
            }))
            game = (data / "pfx/drive_c/users/steamuser/AppData/Roaming/"
                    "Minecraft Bedrock/Users/123/games/com.mojang")
            (game / "minecraftpe").mkdir(parents=True)
            (game / "minecraftpe" / "options.txt").write_text("gfx_vsync:1\n")
            (game / "minecraftpe" / "external_servers.txt").write_text(
                "1:Le Serveur du Lycée:play.lycee-hugo.example:19132:170000\n")
            (game / "minecraftWorlds" / "abc=").mkdir(parents=True)
            (game / "minecraftWorlds" / "abc=" / "levelname.txt").write_text(
                "La Ferme de Mamie")
            with mock.patch("bol.profiles.list_profiles", return_value=[
                    {"name": "Compte de Papa", "slug": "compte-de-papa"}]):
                known = dict(report.local_identity(
                    data=data, prefix=data / "pfx",
                    environ={"USER": "marguerite"}))
        self.assertEqual(known["xXDragonSlayer42Xx"], "<gamertag>")
        self.assertEqual(known["2535428504219876"], "<xuid>")
        self.assertEqual(known["eyJ0b2tlbi1ib2R5"], "<redacted>")
        self.assertNotIn("2026-01-01", known)
        self.assertEqual(known["La Ferme de Mamie"], "<world>")
        self.assertEqual(known["Le Serveur du Lycée"], "<server>")
        self.assertEqual(known["play.lycee-hugo.example"], "<server>")
        self.assertEqual(known["Compte de Papa"], "<profile>")
        self.assertEqual(known["compte-de-papa"], "<profile>")
        self.assertEqual(known["marguerite"], "<user>")
        self.assertEqual(known[str(data)], "<data>")


class RelayChoiceTests(unittest.TestCase):
    def test_no_relay_means_the_browser(self):
        with mock.patch.object(report, "RELAY_URL", ""):
            self.assertIsNone(report.relay_url({}))
            self.assertIn("Nothing is sent by the launcher",
                          report.destination({}))

    def test_a_relay_is_only_ever_reached_over_tls(self):
        env = report.RELAY_ENV
        self.assertEqual(report.relay_url({env: "https://r.example/v1/report"}),
                         "https://r.example/v1/report")
        self.assertIsNone(report.relay_url({env: "http://r.example/v1/report"}))
        self.assertIsNone(report.relay_url({env: "ftp://r.example/x"}))
        self.assertEqual(report.relay_url({env: "http://127.0.0.1:8787/v1/report"}),
                         "http://127.0.0.1:8787/v1/report")
        with mock.patch.object(report, "RELAY_URL", "https://r.example/x"):
            self.assertIsNone(report.relay_url({env: "off"}))

    def test_the_destination_names_the_host(self):
        text = report.destination(
            {report.RELAY_ENV: "https://reports.example/v1/report"})
        self.assertIn("reports.example", text)
        self.assertIn("public issue", text)
        self.assertIn("github.com/Wyze3306/BedrockOnLinux", text)


class DisclosureTests(unittest.TestCase):
    def test_the_browser_route_is_on_the_list_of_hosts(self):
        import importlib
        from bol import netpolicy
        try:
            with mock.patch.object(report, "RELAY_URL", ""):
                importlib.reload(netpolicy)
                entries = [e for e in netpolicy.ENDPOINTS
                           if e.group == "report"]
                self.assertEqual([e.hosts for e in entries],
                                 [("github.com",)])
                self.assertIn("Reporting a problem",
                              netpolicy.disclosure_text())
        finally:
            importlib.reload(netpolicy)

    def test_the_project_relay_is_on_the_list_of_hosts(self):
        from bol import netpolicy
        hosts = [host for e in netpolicy.ENDPOINTS
                 if e.group == "report" for host in e.hosts]
        self.assertEqual(hosts, ["github.com", "reports.linesia.net"])

    def test_a_deployed_relay_is_listed_the_day_it_is_named(self):
        import importlib
        from bol import netpolicy
        try:
            with mock.patch.object(report, "RELAY_URL",
                                   "https://reports.example/v1/report"):
                importlib.reload(netpolicy)
                hosts = [host for e in netpolicy.ENDPOINTS
                         if e.group == "report" for host in e.hosts]
                self.assertEqual(hosts, ["github.com", "reports.example"])
                self.assertIn("reports.example", netpolicy.disclosure_text())
        finally:
            importlib.reload(netpolicy)


class BrowserTests(CollectionCase):
    def test_the_form_is_filled_in_and_the_report_saved(self):
        opened = []
        path, url, did_open = report.open_in_browser(
            self.report, directory=Path(self.directory.name) / "out",
            runner=lambda u: opened.append(u) or True)
        self.assertTrue(did_open)
        self.assertEqual(opened, [url])
        parts = urlsplit(url)
        self.assertEqual((parts.scheme, parts.netloc, parts.path),
                         ("https", "github.com",
                          "/Wyze3306/BedrockOnLinux/issues/new"))
        query = parse_qs(parts.query)
        self.assertEqual(query["title"], ["Crash for <email>"])
        self.assertIn("It crashed on <world>.", query["body"][0])
        self.assertIn(path.name, query["body"][0])
        self.assertNotIn(self.directory.name, query["body"][0])
        self.assertLessEqual(len(url), report.MAX_URL)
        saved = path.read_text(encoding="utf-8")
        self.assertEqual(saved, self.report.full_text())
        self.assertIn("Game log (minecraft.log)", saved)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_a_long_summary_is_cut_to_what_a_url_holds(self):
        self.report.add("extra", "Extra", "\n".join(
            f"row {i} of padding" for i in range(2000)))
        url = report.issue_url(self.report, attached="r.txt")
        self.assertLessEqual(len(url), report.MAX_URL)
        self.assertIn("see the attached report", parse_qs(
            urlsplit(url).query)["body"][0])


@unittest.skipUnless(reportharness.HAVE_OPENSSL, "needs the openssl command")
class SendTests(CollectionCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.keys.cleanup)

    def test_send_files_one_issue_and_returns_its_link(self):
        with reportharness.running_relay(self.keys.name) as (url, _r, github):
            link = report.send(self.report, url)
            self.assertEqual(
                link, "https://github.com/Wyze3306/BedrockOnLinux/issues/401")
            self.assertEqual(len(github.issues), 1)
            filed = json.dumps(github.issues + github.comments,
                               ensure_ascii=False).lower()
            leaked = {what: value for what, value in SECRETS.items()
                      if value.lower() in filed and what != "private folder"}
            self.assertEqual(leaked, {})
            self.assertIn("game log (minecraft.log)", filed)
            self.assertIn("ntsync: up and running.", filed)

    def test_a_refusal_is_a_sentence_for_the_player(self):
        with reportharness.running_relay(
                self.keys.name, BOL_RELAY_PER_SENDER_HOUR="0") as (url, _r, _g):
            with self.assertRaises(report.ReportError) as caught:
                report.send(self.report, url)
            self.assertIn("browser", str(caught.exception))

    def test_a_relay_that_is_down_is_a_sentence_too(self):
        with self.assertRaises(report.ReportError) as caught:
            report.send(self.report, "http://127.0.0.1:9/v1/report", timeout=2)
        self.assertIn("could not be reached", str(caught.exception))

    def test_a_report_without_a_title_is_not_sent(self):
        self.report.set_message("", "x")
        with self.assertRaises(report.ReportError):
            report.send(self.report, "http://127.0.0.1:9/v1/report")

    def test_only_a_link_into_the_projects_issues_is_accepted(self):
        """A relay, or whatever answers in its place, cannot hand the
        launcher an arbitrary link to show and open."""
        class Answer:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self, _limit):
                return json.dumps(self.payload).encode()

        for bad in ({"url": "https://evil.example/issues/1"},
                    {"url": "https://github.com/Wyze3306/BedrockOnLinux/"
                            "issues/1/../../../evil"},
                    {"url": "javascript:alert(1)"}, {}, [1]):
            with self.assertRaises(report.ReportError):
                report.send(self.report, "http://127.0.0.1:9/x",
                            opener=lambda _request, timeout, bad=bad: Answer(bad))


if __name__ == "__main__":
    unittest.main()
