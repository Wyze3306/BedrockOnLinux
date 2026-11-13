"""The list of hosts the launcher names, and the player's update choice."""
# SPDX-License-Identifier: MIT

import fnmatch
import re
import unittest
from pathlib import Path
from unittest import mock

from bol import netpolicy

ROOT = Path(__file__).resolve().parent.parent

# Hosts that appear in the source without ever being connected to: XML
# namespaces, token audiences (relying-party names are http:// identifiers,
# not addresses), links shown to the player, documentation and credits.
NOT_CONTACTED = {
    "www.w3.org", "docs.oasis-open.org", "schemas.microsoft.com",
    "www.microsoft.com", "xboxlive.com", "auth.xboxlive.com",
    "vortex.microsoft.com", "licensing.xboxlive.com", "licensing",
    "wyze3306.github.io", "discord.gg", "www.nayuki.io", "fex-emu.com",
    "example.com", "docs.levimc.org", "snapshot.debian.org",
}


def _declared(host):
    return any(fnmatch.fnmatch(host, pattern)
               for endpoint in netpolicy.ENDPOINTS
               for pattern in endpoint.hosts)


class DisclosureTests(unittest.TestCase):
    def test_every_host_the_source_names_is_declared(self):
        """A new download cannot be added without saying so to the player."""
        missing = {}
        for path in sorted((ROOT / "bol").glob("*.py")):
            for host in re.findall(r"https?://([A-Za-z0-9.-]+)",
                                   path.read_text(encoding="utf-8")):
                host = host.rstrip(".").lower()
                if "." not in host and host not in NOT_CONTACTED:
                    continue
                if host in NOT_CONTACTED or _declared(host):
                    continue
                missing.setdefault(host, path.name)
        self.assertEqual(missing, {})

    def test_every_entry_says_what_is_sent_and_why(self):
        groups = {key for key, _title in netpolicy.GROUPS}
        for endpoint in netpolicy.ENDPOINTS:
            self.assertIn(endpoint.group, groups)
            self.assertTrue(endpoint.hosts)
            self.assertGreater(len(endpoint.sends), 20)
            self.assertGreater(len(endpoint.why), 20)

    def test_the_terminal_text_names_every_host_and_no_telemetry(self):
        text = netpolicy.disclosure_text()
        for endpoint in netpolicy.ENDPOINTS:
            for host in endpoint.hosts:
                self.assertIn(host, text)
        self.assertIn("No telemetry", text)
        html = netpolicy.disclosure_html()
        self.assertIn("raw.githubusercontent.com", html)

    def test_the_readme_names_the_same_hosts(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for endpoint in netpolicy.ENDPOINTS:
            for host in endpoint.hosts:
                self.assertIn(host, readme, f"README.md does not name {host}")

    def test_the_feed_only_uses_declared_update_hosts(self):
        from urllib.parse import urlsplit
        from bol import feed
        update_hosts = [pattern for endpoint in netpolicy.ENDPOINTS
                        if endpoint.group == "updates"
                        for pattern in endpoint.hosts]
        for url in feed.URLS:
            self.assertEqual(urlsplit(url).scheme, "https")
            self.assertIn(urlsplit(url).hostname, update_hosts)


class ModeTests(unittest.TestCase):
    def test_nothing_is_decided_until_the_player_decides(self):
        with mock.patch.dict("os.environ", {netpolicy.ENV: ""}):
            self.assertIsNone(netpolicy.mode({}))
            self.assertFalse(netpolicy.decided({}))
            self.assertFalse(netpolicy.background_allowed({}))
            self.assertFalse(netpolicy.feed_allowed({}))
            self.assertIsNone(netpolicy.mode({"network_updates": "yes"}))

    def test_only_auto_checks_in_the_background(self):
        with mock.patch.dict("os.environ", {netpolicy.ENV: ""}):
            allowed = {m: netpolicy.background_allowed({"network_updates": m})
                       for m in netpolicy.MODES}
            applied = {m: netpolicy.feed_allowed({"network_updates": m})
                       for m in netpolicy.MODES}
        self.assertEqual(allowed, {"auto": True, "ask": False, "off": False})
        self.assertEqual(applied, {"auto": True, "ask": True, "off": False})

    def test_the_environment_decides_for_one_run(self):
        with mock.patch.dict("os.environ", {netpolicy.ENV: "off"}):
            self.assertEqual(netpolicy.mode({"network_updates": "auto"}),
                             "off")

    def test_the_channel_defaults_to_stable(self):
        self.assertEqual(netpolicy.channel({}), "stable")
        self.assertEqual(netpolicy.channel({"update_channel": "nightly"}),
                         "stable")
        self.assertEqual(netpolicy.channel({"update_channel": "prerelease"}),
                         "prerelease")


class TerminalChoiceTests(unittest.TestCase):
    def _choose(self, answers, settings=None, interactive=True):
        settings = {} if settings is None else settings
        said = []
        answers = iter(answers)

        def save(new):
            settings.clear()
            settings.update(new)

        with mock.patch.dict("os.environ", {netpolicy.ENV: ""}), \
                mock.patch("bol.util.load_settings",
                           lambda: dict(settings)), \
                mock.patch("bol.util.save_settings", save):
            result = netpolicy.choose_in_terminal(
                ask=lambda _prompt: next(answers), out=said.append,
                interactive=interactive)
        return result, settings, "\n".join(said)

    def test_the_hosts_are_shown_before_the_question(self):
        result, settings, said = self._choose(["off"])
        self.assertEqual(result, "off")
        self.assertEqual(settings["network_updates"], "off")
        self.assertIn("raw.githubusercontent.com", said)
        self.assertIn("login.live.com", said)

    def test_a_refusal_is_recorded_and_not_asked_again(self):
        _result, settings, _ = self._choose(["off"])
        result, _settings, said = self._choose([], settings)
        self.assertEqual(result, "off")
        self.assertEqual(said, "")

    def test_an_unclear_answer_is_asked_again(self):
        result, _settings, said = self._choose(["a", "maybe", "au"])
        self.assertEqual(result, "auto")
        self.assertEqual(said.count("Type auto, ask or off."), 2)

    def test_without_a_terminal_nothing_is_recorded(self):
        result, settings, said = self._choose([], interactive=False)
        self.assertIsNone(result)
        self.assertEqual(settings, {})
        self.assertIn("network", said)

    def test_a_closed_stdin_decides_nothing(self):
        def closed(_prompt):
            raise EOFError
        with mock.patch.dict("os.environ", {netpolicy.ENV: ""}), \
                mock.patch("bol.util.load_settings", lambda: {}), \
                mock.patch("bol.util.save_settings") as save:
            self.assertIsNone(netpolicy.choose_in_terminal(
                ask=closed, out=lambda _line: None, interactive=True))
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
