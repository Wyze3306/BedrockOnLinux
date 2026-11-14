"""The privacy pass: a booby-trapped log goes in, nothing personal comes out.

Every kind of personal data a report could carry is planted in TRAPPED, next
to the lines a maintainer needs. The tests hold both halves: each planted
value is gone, and the diagnostic lines are still there.
"""
# SPDX-License-Identifier: MIT

import time
import unittest

from bol import scrub
from bol.scrub import Scrubber

# What this "machine" knows about its owner, as bol.report would collect it.
KNOWN = (
    ("/data/people/marguerite", "~"),
    ("marguerite", "<user>"),
    ("Marguerite Duval", "<user>"),
    ("tour-de-guet", "<host>"),
    ("xXDragonSlayer42Xx", "<gamertag>"),
    ("La Ferme de Mamie", "<world>"),
    ("Survie entre potes", "<world>"),
    ("Le Serveur du Lycée", "<server>"),
    ("play.lycee-hugo.example", "<server>"),
    ("Compte de Papa", "<profile>"),
    ("/srv/jeux/bedrock", "<data>"),
)

# Each value below must not survive. Kept apart from the log so the test can
# name what leaked.
SECRETS = {
    "user name": "marguerite",
    "real name": "Duval",
    "home": "/data/people",
    "host name": "tour-de-guet",
    "gamertag": "xXDragonSlayer42Xx",
    "xuid": "2535428504219876",
    "xuid hex": "0009000012AB34CD",
    "e-mail": "marguerite.duval@courriel.example",
    "second e-mail": "papa+xbox@outlook.com",
    "msa refresh token": "M.C544_BAY.0.U.-CqT8fakefakefakefake!x9$pL2wZ*",
    "jwt": "eyJhbGciOiJSUzI1NiJ9.eyJ4dWkiOlt7InhpZCI6IjI1MzUifV19.c2lnbmF0dXJl",
    "xsts header": "XBL3.0 x=13371337133713371337;eyJlbmMiOiJBMTI4Q0JDIn0.abcDEF123",
    "user hash": "13371337133713371337",
    "bearer": "Bearer ya29.a0AfH6SMBx-fake-fake-fake",
    "device id": "{7C9E6679-7425-40DE-944B-E07FC1F90AE7}",
    "device token": "EwAoA8l6BAAUs5fakeFAKEfake0123456789abcdefABCDEFghijklmnop",
    "cookie": "MSPAuth=2fakeCookieValue; MSPProf=another",
    "ipv4": "82.66.142.17",
    "lan ipv4": "192.168.1.42",
    "ipv6": "2a01:e0a:5d1:9c40:be24:11ff:fe8a:1b2c",
    "ipv6 short": "fe80::be24:11ff:fe8a:1b2c",
    "mac": "bc:24:11:8a:1b:2c",
    "mac dashes": "BC-24-11-8A-1B-2D",
    "serial": "PF3K9XQ7",
    "disk serial": "S4EWNX0M123456",
    "machine id": "4f6c1a2b3c4d5e6f708192a3b4c5d6e7",
    "world": "La Ferme de Mamie",
    "world encoded": "La%20Ferme%20de%20Mamie",
    "second world": "Survie entre potes",
    "unknown world": "Base secrète de Timéo",
    "server": "Le Serveur du Lycée",
    "server address": "play.lycee-hugo.example",
    "profile": "Compte de Papa",
    "data root": "/srv/jeux",
    "url query": "code=M.C5_BL2.fakeauthcode",
    "url login": "papa:hunter2",
    "password": "hunter2",
    "volume label": "Disque de Marguerite",
    "other user": "theodore",
    "github token": "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "uid": "/run/user/1000",
    "private key": "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC",
    "session id": "sess-9f8e7d6c5b4a",
    "private folder": "Documents",
    "profile folder": "profiles/papa",
    "client id in a query": "0000000048183522",
}

# Diagnostic lines that must come through, exactly.
KEPT = (
    "ntsync: up and running.",
    "vkd3d-proton - applicationVersion: 3.0.1.",
    "Minecraft 1.26.60.5",
    "games/release/1.26.60.5/Minecraft.Windows.exe",
    "NVIDIA driver 580.95.05",
    "Linux 6.16.12+deb13-amd64",
    "(hash: a8cc00c095070613)",
    "0118:fixme:gdkc:XSystem_QueryInterface {67ce4bfc-1e1b-4b4d-9d5a-"
    "6d3e3a1b2c3d} not implemented",
    "largest device local heap is too small (8585740288 bytes)",
    "err:seh:NtRaiseException Unhandled exception code c0000005",
    "exit code: 139",
    "refresh rate: 143.97",
    "d3d12_root_signature_init: Failed to create layout",
    "127.0.0.1",
)

TRAPPED = """\
INFO: umu-launcher version 1.4.3 (3.13.5 (main, Aug 10 2026, 12:06:59))
Proton: /data/people/marguerite/.local/share/bedrock-on-linux/content/Minecraft.Windows.exe
Proton: Upgrading prefix (/home/marguerite/.local/share/bedrock-on-linux/compatdata/pfx/)
ntsync: up and running.
814.933:0118:011c:info:vkd3d-proton:vkd3d_get_vk_version: vkd3d-proton - applicationVersion: 3.0.1.
814.919:0118:011c:info:vkd3d-proton:vkd3d_instance_apply_application_workarounds: Program name: "Minecraft.Windows.exe" (hash: a8cc00c095070613)
815.439:0118:011c:info:vkd3d-proton:vkd3d_memory_info: largest device local heap is too small (8585740288 bytes) for effective ReBAR.
815.500:0118:011c:err:vkd3d-proton:d3d12_root_signature_init: Failed to create layout
0118:fixme:gdkc:XSystem_QueryInterface {67ce4bfc-1e1b-4b4d-9d5a-6d3e3a1b2c3d} not implemented
0118:fixme:gdkc:XUser_GetDeviceId device {7C9E6679-7425-40DE-944B-E07FC1F90AE7}
0118:err:seh:NtRaiseException Unhandled exception code c0000005 at Z:\\data\\people\\marguerite\\x.dll
Launching Minecraft 1.26.60.5 from games/release/1.26.60.5/Minecraft.Windows.exe
NVIDIA driver 580.95.05 on Linux 6.16.12+deb13-amd64, refresh rate: 143.97
preauth: signed in as xXDragonSlayer42Xx (xuid 2535428504219876)
preauth: gamertag=xXDragonSlayer42Xx
preauth: XUID 0009000012AB34CD cached for marguerite.duval@courriel.example
preauth: account papa+xbox@outlook.com on tour-de-guet
POST https://login.live.com/oauth20_token.srf?client_id=0000000048183522&code=M.C5_BL2.fakeauthcode&x=1
refresh_token=M.C544_BAY.0.U.-CqT8fakefakefakefake!x9$pL2wZ*
{"access_token": "eyJhbGciOiJSUzI1NiJ9.eyJ4dWkiOlt7InhpZCI6IjI1MzUifV19.c2lnbmF0dXJl", "expires_in": 3600}
Authorization: XBL3.0 x=13371337133713371337;eyJlbmMiOiJBMTI4Q0JDIn0.abcDEF123
> authorization: Bearer ya29.a0AfH6SMBx-fake-fake-fake
Cookie: MSPAuth=2fakeCookieValue; MSPProf=another
"DeviceToken":"EwAoA8l6BAAUs5fakeFAKEfake0123456789abcdefABCDEFghijklmnop"
device raw EwAoA8l6BAAUs5fakeFAKEfake0123456789abcdefABCDEFghijklmnop end
uhs: 13371337133713371337
session_id=sess-9f8e7d6c5b4a
RakNet: connecting to 82.66.142.17:19132 (lan 192.168.1.42)
RakNet: local 192.168.1.42/24 via 2a01:e0a:5d1:9c40:be24:11ff:fe8a:1b2c and [fe80::be24:11ff:fe8a:1b2c%wlan0]
Joining server "Le Serveur du Lycée" at play.lycee-hugo.example
netdev wlan0 bc:24:11:8a:1b:2c bt BC-24-11-8A-1B-2D
SMBIOS: Serial Number: PF3K9XQ7
nvme0n1 serial=S4EWNX0M123456
/etc/machine-id 4f6c1a2b3c4d5e6f708192a3b4c5d6e7
Loading level La Ferme de Mamie
Opening minecraftWorlds/La%20Ferme%20de%20Mamie/level.dat
LevelName: Base secrète de Timéo
World 'Survie entre potes' saved
profile Compte de Papa selected, data in /srv/jeux/bedrock/profiles/papa
git clone https://papa:hunter2@example.org/repo.git
smb password = hunter2
mounted /run/media/marguerite/Disque de Marguerite/Jeux
socket /run/user/1000/bus, other home /home/theodore/Documents
GITHUB ghp_abcdefghijklmnopqrstuvwxyz0123456789
-----BEGIN PRIVATE KEY----- MIIEvQIBADANBgkqhkiG9w0BAQEFAASC
Marguerite Duval launched the game; exit code: 139
listening on 127.0.0.1:18080
"""


class TrappedLogTests(unittest.TestCase):
    def setUp(self):
        self.scrubber = Scrubber(KNOWN)
        self.clean = self.scrubber.text(TRAPPED)

    def test_nothing_personal_survives(self):
        leaked = {what: value for what, value in SECRETS.items()
                  if value.lower() in self.clean.lower()}
        self.assertEqual(leaked, {}, self.clean)

    def test_the_diagnostic_lines_survive(self):
        missing = [line for line in KEPT if line not in self.clean]
        self.assertEqual(missing, [], self.clean)

    def test_a_second_pass_changes_nothing(self):
        """The relay runs the same pass on what the launcher already
        cleaned; it must not eat the placeholders."""
        self.assertEqual(Scrubber(KNOWN).text(self.clean), self.clean)
        self.assertEqual(scrub.scrub(self.clean), self.clean)

    def test_shapes_alone_catch_what_the_relay_can_know(self):
        """With no knowledge of the machine -- the relay's position -- every
        token, address and identifier still goes."""
        clean = scrub.scrub(TRAPPED)
        for what in ("xuid", "xuid hex", "e-mail", "second e-mail",
                     "msa refresh token", "jwt", "xsts header", "bearer",
                     "device id", "device token", "cookie", "ipv4",
                     "lan ipv4", "ipv6", "ipv6 short", "mac", "mac dashes",
                     "serial", "disk serial", "machine id", "url query",
                     "url login", "password", "github token", "other user",
                     "private key", "session id", "unknown world"):
            self.assertNotIn(SECRETS[what].lower(), clean.lower(), what)

    def test_nothing_known_is_left_behind(self):
        self.assertEqual(self.scrubber.residue(self.clean), [])

    def test_what_was_removed_is_counted(self):
        stats = self.scrubber.stats
        for kind in ("personal names or paths", "tokens", "IP addresses", "MAC addresses",
                     "e-mail addresses", "paths", "identifiers",
                     "lines removed"):
            self.assertGreater(stats[kind], 0, kind)


class ShapeTests(unittest.TestCase):
    def assertScrubbed(self, text, expected):
        self.assertEqual(scrub.scrub(text), expected)

    def test_home_directories(self):
        self.assertScrubbed("/home/alice/.config/x", "/home/<user>/.config/x")
        self.assertScrubbed("/var/home/alice/.x", "/var/home/<user>/.x")
        self.assertScrubbed(r"C:\users\alice\AppData", r"C:\users\<user>\AppData")
        self.assertScrubbed(r"Z:\home\alice\x.dll", r"Z:\home\<user>\x.dll")
        self.assertScrubbed("/mnt/Jeux de Bob/x", "/mnt/<volume>/x")
        self.assertScrubbed("/run/media/bob/Mes Jeux/x",
                            "/run/media/<volume>/x")
        self.assertScrubbed("~/.local/share/bol/profiles/kids/logs",
                            "~/.local/share/bol/profiles/<profile>/logs")
        self.assertScrubbed("GET https://a.example/b?c=d#e",
                            "GET https://a.example/b?<query>")

    def test_private_folders_under_home_are_not_named(self):
        self.assertScrubbed(
            "--- inject /home/alice/Bureau/Velyx/build/Velyx.dll ---",
            "--- inject /home/<user>/<path>/Velyx.dll ---")
        self.assertScrubbed(
            r"OK injected Z:\home\alice\Mes Mods\Borion.dll into "
            r"Minecraft.Windows.exe (pid 32)",
            r"OK injected Z:\home\<user>\<path>\Borion.dll into "
            r"Minecraft.Windows.exe (pid 32)")
        # No end can be found for a name with spaces: the line ends there.
        self.assertScrubbed(
            "opening /home/alice/Documents/Dupont taxes 2026/notes and "
            "Minecraft.Windows.exe", "opening /home/<user>/<path>")
        self.assertScrubbed(
            "copy /home/alice/Jeux/a.mcpack to /home/alice/Autre Dossier/b",
            "copy /home/<user>/<path>/a.mcpack to /home/<user>/<path>")
        # A program's own hidden folder is not the player's naming.
        keep = "/home/<user>/.local/share/bedrock-on-linux/logs/minecraft.log"
        self.assertScrubbed(keep.replace("<user>", "alice"), keep)
        scrubber = Scrubber((("/home/alice", "~"),))
        self.assertEqual(scrubber.text("x /home/alice/Bureau/y/z.txt"),
                         "x ~/<path>")
        self.assertEqual(scrubber.text("x ~/<path>"), "x ~/<path>")

    def test_a_minecraft_build_is_not_an_ip_address(self):
        for line in ("Minecraft 1.26.60.5", "version: 1.21.120.4",
                     "games/preview/1.26.70.24",
                     "Microsoft.MinecraftWindowsBeta_1.26.7024.0_x64",
                     "build v1.26.44.3"):
            self.assertScrubbed(line, line)

    def test_an_address_stays_an_address_whatever_precedes_it(self):
        self.assertScrubbed("Minecraft server 1.26.60.5:19132",
                            "Minecraft server <ip>:19132")
        self.assertScrubbed("connected to 8.8.8.8", "connected to <ip>")
        self.assertScrubbed("gateway 10.0.0.1", "gateway <lan-ip>")
        self.assertScrubbed("route 192.168.0.0/24", "route <lan-ip>/24")

    def test_clocks_and_scopes_are_not_ipv6(self):
        for line in ("12:34:56 started", "std::vector<int>::iterator",
                     "0118:011c:info:vkd3d", "Bedrock::Threading::Async"):
            self.assertScrubbed(line, line)

    def test_wine_interface_guids_stay_and_owned_ones_go(self):
        keep = ("0124:fixme:combase:RoGetActivationFactory "
                "{94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90}")
        self.assertScrubbed(keep, keep)
        self.assertScrubbed(
            "0124:trace:gdkc:device {94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90}",
            "0124:trace:gdkc:device <guid>")
        self.assertScrubbed("id 94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90",
                            "id <guid>")

    def test_sensitive_keys_lose_their_value(self):
        self.assertScrubbed('"RpsTicket": "t=abc"', '"RpsTicket": "<redacted>"')
        self.assertScrubbed("client_secret=abc123 next",
                            "client_secret=<redacted> next")
        self.assertScrubbed("device_code: ABCD-1234",
                            "device_code: <redacted>")

    def test_ordinary_codes_and_rates_are_left_alone(self):
        for line in ("exit code: 1", "error code=0x80070005",
                     "status code 403", "refresh rate: 60",
                     "key=VK_F5 pressed"):
            self.assertScrubbed(line, line)

    def test_escape_sequences_are_stripped(self):
        self.assertScrubbed("\x1b[38;5;78mOK\x1b[0m done\x07", "OK done")

    def test_a_very_long_line_is_cut(self):
        clean = scrub.scrub("x " * 5000)
        self.assertLess(len(clean), scrub.MAX_LINE + 40)
        self.assertTrue(clean.endswith("[line cut]"))

    def test_consecutive_dropped_lines_become_one_marker(self):
        clean = scrub.scrub("a\ngamertag: A\nxuid: 1\nLevelName: B\nb")
        self.assertEqual(clean.split("\n"),
                         ["a", "[3 lines removed: could contain personal "
                               "data]", "b"])

    def test_prose_keeps_its_lines_and_loses_its_secrets(self):
        scrubber = Scrubber((("xXDragonSlayer42Xx", "<gamertag>"),))
        comment = ("My gamertag is xXDragonSlayer42Xx and my world name "
                   "disappeared.\nMail me: bob@example.org")
        self.assertEqual(
            scrubber.text(comment, prose=True),
            "My gamertag is <gamertag> and my world name disappeared.\n"
            "Mail me: <email>")

    def test_a_value_pasted_into_prose_is_still_redacted(self):
        self.assertEqual(
            scrub.scrub("dmidecode says Serial Number: PF3K9XQ7 and "
                        "gamertag=Bob, password: hunter2", prose=True),
            "dmidecode says Serial Number: <redacted> and "
            "gamertag=<redacted>, password: <redacted>")
        once = scrub.scrub("xuid = 0009000012AB34CD", prose=True)
        self.assertEqual(once, "xuid = <redacted>")
        self.assertEqual(scrub.scrub(once, prose=True), once)

    def test_generic_names_are_not_replaced_everywhere(self):
        """A login called "user" must not turn user32.dll into noise; its
        home directory still goes."""
        scrubber = Scrubber((("user", "<user>"), ("/home/user", "~")))
        self.assertEqual(
            scrubber.text("user.auth.xboxlive.com for /home/user/.x"),
            "user.auth.xboxlive.com for ~/.x")

    def test_the_longest_known_value_wins(self):
        scrubber = Scrubber((("alice", "<user>"), ("alice-pc", "<host>")))
        self.assertEqual(scrubber.text("on alice-pc as alice"),
                         "on <host> as <user>")

    def test_many_known_names_do_not_slow_the_pass(self):
        known = [(f"Monde numero {i} de Marguerite", "<world>")
                 for i in range(800)] + list(KNOWN)
        log = TRAPPED * 40 + "Loading level Monde numero 799 de Marguerite\n"
        started = time.monotonic()
        clean = Scrubber(known).text(log)
        self.assertLess(time.monotonic() - started, 10)
        self.assertTrue(clean.endswith("Loading level <world>"))

    def test_a_large_log_is_scrubbed_quickly(self):
        log = (TRAPPED + "A" * 1900 + "\n" + "a_b-c." * 300 + "\n") * 60
        started = time.monotonic()
        Scrubber(KNOWN).text(log)
        self.assertLess(time.monotonic() - started, 10)


class EnvironmentAllowListTests(unittest.TestCase):
    def test_only_known_tuning_variables_are_reported(self):
        kept, hidden = scrub.filter_env([
            ("PROTON_LOG", "1"), ("WINEDEBUG", "+gdkc"),
            ("VKD3D_CONFIG", "dxr"), ("MESA_VK_DEVICE_SELECT", "10de:25a2"),
            ("__GL_SYNC_TO_VBLANK", "0"), ("MANGOHUD", "1"),
            ("MY_API_KEY", "s3cret"), ("HTTP_PROXY", "http://bob:pw@proxy"),
            ("GITHUB_TOKEN", "ghp_x"), ("FOO", "bar"),
            ("WINE_AUTH_TOKEN", "x"), ("PROTON_USER_SECRET", "x"),
        ])
        self.assertEqual([name for name, _ in kept],
                         ["PROTON_LOG", "WINEDEBUG", "VKD3D_CONFIG",
                          "MESA_VK_DEVICE_SELECT", "__GL_SYNC_TO_VBLANK",
                          "MANGOHUD"])
        self.assertEqual(hidden, 6)


if __name__ == "__main__":
    unittest.main()
