"""The accounts menu: two sign-ins, one at a time, and questions that expire.

The launcher needs two separate Microsoft sessions -- one to play online, one
to download the game -- and they used to live in two different places with
nothing saying they were different accounts at all. They share a menu now, so
these cover both, plus the two properties the menu has to keep: a device-code
flow that cannot be started twice, and a "Sign out?" that cannot outlive the
menu that asked it.
"""
# SPDX-License-Identifier: MIT

import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

from bol import gui, xodus
from tests.guiharness import headless_window, qt_app


def _menu(window):
    """The accounts menu, refreshed as opening it would."""
    window._refresh_accounts()
    return window.accounts_menu


class SignInIsStartedOnceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        qt_app()

    def test_a_second_click_does_not_start_a_second_flow(self):
        # Every start() is a fresh device code; two in flight means the
        # player is shown one code while the launcher waits on another.
        with headless_window() as window:
            started = []
            window.na.start = lambda *a, **k: started.append(a)
            with mock.patch.object(gui.threading, "Thread") as thread:
                thread.side_effect = lambda target, daemon=None: mock.Mock(
                    start=target)
                window.acct_click()
                window.acct_click()
                window.acct_click()
            self.assertEqual(thread.call_count, 1)

    def test_the_row_says_it_is_working(self):
        with headless_window() as window:
            with mock.patch.object(gui.threading, "Thread"):
                window.acct_click()
            self.assertEqual(window._acct_mode, "loading")
            self.assertEqual(_menu(window).online.button.text(), "Loading…")

    def test_the_code_arriving_makes_the_button_a_cancel(self):
        with headless_window() as window:
            with mock.patch.object(gui.threading, "Thread"):
                window.acct_click()
            window._refresh_account_row("auth")
            self.assertEqual(window._acct_mode, "cancel")
            self.assertEqual(_menu(window).online.button.text(), "Cancel")


class ConfirmationsCanBeWithdrawnTests(unittest.TestCase):
    """Signing out asks first, and a question left armed on screen is one the
    next stray click answers. A Qt::Popup closes on any click outside itself,
    so hiding the menu is what withdraws it -- which is why this no longer
    needs an application-wide mouse filter."""

    @classmethod
    def setUpClass(cls):
        qt_app()

    def test_arming_sign_out_asks_first(self):
        with headless_window() as window:
            window._refresh_account_row("in")
            window.acct_click()
            self.assertTrue(window._acct_confirm)
            self.assertEqual(_menu(window).online.button.text(), "Sign out?")

    def test_confirming_signs_out(self):
        with headless_window() as window:
            window._refresh_account_row("in")
            window.acct_click()
            with mock.patch.object(gui, "msa_logout") as logout, \
                    mock.patch.object(gui, "msa_signed_in", return_value=False):
                window.acct_click()
            self.assertTrue(logout.called)
            self.assertFalse(window._acct_confirm)

    def test_closing_the_menu_withdraws_the_question(self):
        with headless_window() as window:
            window.show()
            window._refresh_account_row("in")
            window.open_accounts_menu()
            window.acct_click()
            self.assertTrue(window._acct_confirm)
            window.accounts_menu.close()
            self.assertFalse(window._acct_confirm)
            self.assertEqual(_menu(window).online.button.text(), "Sign out")

    def test_the_download_account_asks_before_signing_out(self):
        with headless_window() as window:
            with mock.patch.object(xodus, "signed_in", return_value=True):
                window.store_click()
                self.assertTrue(window._store_confirm)
                self.assertEqual(_menu(window).download.button.text(),
                                 "Sign out?")

    def test_the_download_account_signs_out_on_the_second_click(self):
        with headless_window() as window:
            with mock.patch.object(xodus, "signed_in", return_value=True), \
                    mock.patch.object(window, "_unlink_store_account") as out:
                window.store_click()
                window.store_click()
            self.assertTrue(out.called)
            self.assertFalse(window._store_confirm)

    def test_closing_the_menu_withdraws_that_one_too(self):
        with headless_window() as window:
            window.show()
            with mock.patch.object(xodus, "signed_in", return_value=True):
                window.open_accounts_menu()
                window.store_click()
                self.assertTrue(window._store_confirm)
                window.accounts_menu.close()
            self.assertFalse(window._store_confirm)


class BothAccountsAreVisibleTests(unittest.TestCase):
    """The whole point of the menu: the download account is no longer only in
    Settings, and the pill never claims everything is fine while one of the
    two is missing."""

    @classmethod
    def setUpClass(cls):
        qt_app()

    def _pill(self, window):
        return window.acct_dot.styleSheet(), window.acct_text.text()

    def test_both_accounts_have_a_row(self):
        with headless_window() as window:
            menu = _menu(window)
            self.assertEqual(menu.online.status.text(), "Not signed in")
            self.assertEqual(menu.download.status.text(), "Not signed in")

    def test_the_signed_in_gamertag_is_named(self):
        with headless_window() as window:
            with mock.patch.object(gui, "msa_gamertag", return_value="Wyze3306"):
                window._refresh_account_row("in")
                self.assertEqual(_menu(window).online.status.text(),
                                 "Signed in as Wyze3306")
                self.assertEqual(window.acct_text.text(), "Wyze3306")

    def test_the_pill_is_green_only_when_both_are_in(self):
        with headless_window() as window:
            with mock.patch.object(gui, "msa_gamertag", return_value="W"), \
                    mock.patch.object(xodus, "signed_in", return_value=True):
                window._refresh_account_row("in")
                self.assertIn(window.theme.green, window.acct_dot.styleSheet())

    def test_one_account_missing_is_not_reported_as_signed_in(self):
        # The half-truth that sent people to PLAY with nothing to click: the
        # window said "Signed in" while the account it downloads with was
        # never linked.
        with headless_window() as window:
            with mock.patch.object(gui, "msa_gamertag", return_value="W"), \
                    mock.patch.object(xodus, "signed_in", return_value=False):
                window._refresh_account_row("in")
                self.assertIn(window.theme.gold, window.acct_dot.styleSheet())
                self.assertNotIn(window.theme.green,
                                 window.acct_dot.styleSheet())

    def test_the_pill_names_both_accounts_in_its_tooltip(self):
        with headless_window() as window:
            window._refresh_accounts()
            tip = window.acct_card.toolTip()
            self.assertIn("Play online:", tip)
            self.assertIn("Download Minecraft:", tip)

    def test_the_menu_says_why_there_are_two(self):
        with headless_window() as window:
            self.assertIn("asks twice", gui.STORE_LINK_EXPLAINER)
            self.assertIn(gui.STORE_LINK_EXPLAINER,
                          [c.text() for c in
                           _menu(window).findChildren(type(window.acct_text))])


class DownloadSignInIsOfferedWhereItIsMissedTests(unittest.TestCase):
    """PLAY reaching the download with no account for it is an offer, not a
    launch failure: nothing broke and nothing was downloaded, and the account
    it means is not the one the player signed into."""

    @classmethod
    def setUpClass(cls):
        qt_app()

    def test_not_signed_in_is_reported_apart_from_a_failure(self):
        worker = gui.LaunchWorker({"edition": {"id": "release"}, "tag": "1.0"})
        seen = {"failed": [], "offer": []}
        worker.failed.connect(seen["failed"].append)
        worker.needs_store_signin.connect(seen["offer"].append)
        with mock.patch.object(gui, "do_setup",
                               side_effect=xodus.NotSignedIn("no account")):
            worker.run()
        self.assertEqual(seen["failed"], [])
        self.assertEqual(seen["offer"], ["no account"])

    def test_any_other_failure_still_fails(self):
        worker = gui.LaunchWorker({"edition": {"id": "release"}, "tag": "1.0"})
        seen = {"failed": [], "offer": []}
        worker.failed.connect(seen["failed"].append)
        worker.needs_store_signin.connect(seen["offer"].append)
        with mock.patch.object(gui, "do_setup",
                               side_effect=RuntimeError("disk full")):
            worker.run()
        self.assertEqual(seen["failed"], ["disk full"])
        self.assertEqual(seen["offer"], [])

    def test_accepting_the_offer_signs_in_then_resumes_play(self):
        with headless_window() as window:
            with mock.patch.object(window, "_offer_store_account_link",
                                   return_value=True), \
                    mock.patch.object(window, "_link_store_account") as link:
                window._store_signin_needed("no account")
            self.assertTrue(link.called)
            # PLAY resumes on its own rather than making them press it again.
            self.assertEqual(link.call_args.kwargs["then"], window.do_play)

    def test_declining_the_offer_leaves_the_launcher_idle(self):
        with headless_window() as window:
            with mock.patch.object(window, "_offer_store_account_link",
                                   return_value=False), \
                    mock.patch.object(window, "_link_store_account") as link:
                window._store_signin_needed("no account")
            self.assertFalse(link.called)
            self.assertFalse(window.ui_state["busy"])
            self.assertFalse(window.ui_state["launch_active"])

    def test_the_status_names_the_account_that_is_missing(self):
        with headless_window() as window:
            with mock.patch.object(window, "_offer_store_account_link",
                                   return_value=False):
                window._store_signin_needed("no account")
            self.assertIn("download", window.status_label.text().lower())

    def test_signing_in_online_offers_the_download_account_next(self):
        with headless_window() as window:
            with mock.patch.object(xodus, "signed_in", return_value=False), \
                    mock.patch.object(window, "_offer_store_account_link",
                                      return_value=True) as offer, \
                    mock.patch.object(window, "_link_store_account") as link:
                window._offer_download_sign_in()
            self.assertTrue(offer.called)
            self.assertTrue(link.called)

    def test_an_already_linked_download_account_is_not_asked_for_again(self):
        with headless_window() as window:
            with mock.patch.object(xodus, "signed_in", return_value=True), \
                    mock.patch.object(window, "_offer_store_account_link") as offer:
                window._offer_download_sign_in()
            self.assertFalse(offer.called)


class PlayingOfflineIsWarnedAboutTests(unittest.TestCase):
    """PLAY with no account for online play (#240).

    The sign-in players remember doing is the download's -- PLAY asks for
    that one on its own when it is missing. The online one never asks, so its
    absence used to surface only inside the game, as Realms, servers and
    friends quietly missing, with nothing in the launcher having said a word.
    """

    @classmethod
    def setUpClass(cls):
        qt_app()

    @staticmethod
    @contextmanager
    def _armed(window, answer="play"):
        """PLAY with a version picked, the launch stubbed out, and the
        warning answering without a modal anyone has to click."""
        stubs = {
            "selected_version": mock.Mock(
                return_value={"edition": {"id": "release"}, "tag": "1.0"}),
            "_start_worker": mock.Mock(return_value=True),
            "_offer_online_sign_in": mock.Mock(return_value=answer),
            "acct_click": mock.Mock(),
        }
        with mock.patch.multiple(window, **stubs):
            yield SimpleNamespace(**stubs)

    def test_play_warns_before_starting_the_game(self):
        with headless_window() as window, self._armed(window) as patched:
            window.do_play()
            self.assertTrue(patched._offer_online_sign_in.called)

    def test_playing_offline_still_plays(self):
        with headless_window() as window, self._armed(window, "play") as patched:
            window.do_play()
            self.assertTrue(patched._start_worker.called)
            self.assertTrue(window.ui_state["launch_active"])

    def test_choosing_to_sign_in_holds_the_launch_back(self):
        with headless_window() as window, self._armed(window, "signin") as patched:
            window.do_play()
            self.assertFalse(patched._start_worker.called)
            self.assertTrue(patched.acct_click.called)
            # ...and PLAY resumes on its own, rather than making them press
            # it again once the sign-in lands.
            self.assertTrue(window.ui_state["play_after_signin"])
            self.assertFalse(window.ui_state["busy"])

    def test_a_signed_in_player_is_not_warned(self):
        with headless_window() as window, self._armed(window) as patched:
            with mock.patch.object(gui, "msa_signed_in", return_value=True):
                window.do_play()
            self.assertFalse(patched._offer_online_sign_in.called)
            self.assertTrue(patched._start_worker.called)

    def test_the_warning_can_be_turned_off(self):
        with headless_window(warn_offline_play=False) as window, \
                self._armed(window) as patched:
            window.do_play()
            self.assertFalse(patched._offer_online_sign_in.called)
            self.assertTrue(patched._start_worker.called)

    def test_a_sign_in_already_on_screen_is_not_warned_about(self):
        # Warning about the sign-in the player is in the middle of doing
        # helps nobody, and the device code is already the thing to act on.
        with headless_window() as window, self._armed(window) as patched:
            window._refresh_account_row("auth")
            window.do_play()
            self.assertFalse(patched._offer_online_sign_in.called)
            self.assertTrue(patched._start_worker.called)

    def test_signing_in_resumes_the_play_that_asked_for_it(self):
        with headless_window() as window:
            window.ui_state["play_after_signin"] = True
            with mock.patch.object(window, "_warm_xbox_preauth"), \
                    mock.patch.object(window, "_offer_download_sign_in",
                                      return_value=False), \
                    mock.patch.object(window, "do_play") as play:
                window._on_online()
            self.assertTrue(play.called)
            self.assertNotIn("play_after_signin", window.ui_state)

    def test_the_download_sign_in_takes_the_resume_over(self):
        # Both accounts were missing: launching while the download's browser
        # sign-in is still open would start the game out from under it.
        with headless_window() as window:
            window.ui_state["play_after_signin"] = True
            with mock.patch.object(window, "_warm_xbox_preauth"), \
                    mock.patch.object(window, "_offer_store_account_link",
                                      return_value=True), \
                    mock.patch.object(window, "_link_store_account") as link, \
                    mock.patch.object(window, "do_play") as play:
                window._on_online()
            self.assertFalse(play.called)
            self.assertEqual(link.call_args.kwargs["then"], play)

    def test_signing_in_without_a_pending_play_starts_nothing(self):
        with headless_window() as window:
            with mock.patch.object(window, "_warm_xbox_preauth"), \
                    mock.patch.object(window, "_offer_download_sign_in",
                                      return_value=False), \
                    mock.patch.object(window, "do_play") as play:
                window._on_online()
            self.assertFalse(play.called)

    def test_cancelling_the_sign_in_withdraws_the_pending_play(self):
        with headless_window() as window:
            window._refresh_account_row("auth")
            window.ui_state["play_after_signin"] = True
            window.acct_click()          # arms the confirmation
            window.acct_click()          # confirms the cancel
            self.assertNotIn("play_after_signin", window.ui_state)

    def test_the_warning_says_what_offline_costs(self):
        for missing in ("Realms", "servers", "friends"):
            self.assertIn(missing, gui.OFFLINE_PLAY_EXPLAINER)

    def _dialog(self, window, click=None):
        """The real warning, answered by picking one of its own buttons
        instead of running a modal no one is there to click."""
        seen = {}

        def answer(box):
            seen["buttons"] = [b.text() for b in box.buttons()]
            seen["checkbox"] = box.checkBox()
            chosen = next((b for b in box.buttons() if b.text() == click), None)
            if chosen is not None:
                chosen.click()
            return 0

        with mock.patch.object(gui.QMessageBox, "exec", answer):
            seen["choice"] = window._offer_online_sign_in()
        return seen

    def test_the_warning_offers_both_ways_out(self):
        with headless_window() as window:
            seen = self._dialog(window)
            self.assertIn("Play offline", seen["buttons"])
            self.assertIn("Sign in", seen["buttons"])

    def test_the_warning_can_be_silenced_from_itself(self):
        with headless_window() as window:
            with mock.patch.object(window, "_save_setting") as saved:
                def answer(box):
                    box.checkBox().setChecked(True)
                    return 0
                with mock.patch.object(gui.QMessageBox, "exec", answer):
                    window._offer_online_sign_in()
            saved.assert_called_once_with("warn_offline_play", False)

    def test_dismissing_the_warning_plays_offline(self):
        # A dialog closed by the window manager clicks nothing. Treating that
        # as "Sign in" would hijack PLAY into a sign-in nobody asked for.
        with headless_window() as window:
            self.assertEqual(self._dialog(window)["choice"], "play")

    def test_each_button_answers_as_itself(self):
        with headless_window() as window:
            self.assertEqual(self._dialog(window, "Sign in")["choice"], "signin")
            self.assertEqual(self._dialog(window, "Play offline")["choice"],
                             "play")

    def test_settings_can_put_the_warning_back(self):
        # "Don't warn me again" is only fair if there is somewhere to undo it.
        with headless_window(warn_offline_play=False) as window:
            row = next(r for r in window._switches
                       if any("offline" in c.text().lower()
                              for c in r.findChildren(gui.QLabel)))
            self.assertFalse(row.switch.isChecked())
            with mock.patch.object(window, "_save_setting") as saved:
                row.switch.click()
            saved.assert_called_once_with("warn_offline_play", True)


class XboxPreauthWarmUpTests(unittest.TestCase):
    """launch.py runs xbl_preauth again at PLAY, so this is a warm-up, not a
    dependency -- but dropping it moves the whole SISU/XSTS round trip onto
    the first launch instead of the sign-in the player is already waiting on."""

    @classmethod
    def setUpClass(cls):
        qt_app()

    def _run_warm_up(self, window):
        with mock.patch.object(gui.threading, "Thread") as thread:
            # Execute the target immediately when the mock thread starts
            def run_target(*args, **kwargs):
                target = args[0] if args else kwargs.get('target')
                if target:
                    target()
                return mock.Mock()
            thread.side_effect = run_target
            window._warm_xbox_preauth()

    def test_coming_online_warms_the_token_chain(self):
        with headless_window() as window:
            with mock.patch.object(window, "_warm_xbox_preauth") as warm, \
                    mock.patch.object(window, "_offer_store_account_link",
                                      return_value=False):
                # _offer_store_account_link is mocked because _on_online
                # falls through to _offer_download_sign_in(), which would
                # otherwise open a real QMessageBox.exec() -- a blocking
                # modal with no one to click it under offscreen QPA, which
                # hangs the test rather than failing it.
                window._on_online()
            self.assertTrue(warm.called)

    def test_the_refreshed_token_is_handed_to_xbl_preauth(self):
        from bol import auth
        with headless_window() as window:
            with mock.patch.object(auth, "msa_load",
                                   return_value={"refresh_token": "r"}), \
                    mock.patch.object(auth, "msa_refresh",
                                      return_value={"access_token": "a"}), \
                    mock.patch.object(auth, "_account_cache_epoch",
                                      return_value=7), \
                    mock.patch.object(auth, "xbl_preauth",
                                      return_value=True) as preauth:
                self._run_warm_up(window)
            preauth.assert_called_once_with("a", 7)

    def test_no_stored_account_is_not_an_error(self):
        from bol import auth
        with headless_window() as window:
            with mock.patch.object(auth, "msa_load", return_value=None), \
                    mock.patch.object(auth, "xbl_preauth") as preauth:
                self._run_warm_up(window)
            self.assertFalse(preauth.called)

    def test_a_failing_warm_up_is_swallowed(self):
        # PLAY re-runs the chain and is where a real failure gets reported,
        # with its diagnostic attached. A warm-up must never surface as a
        # crash in a background thread.
        from bol import auth
        with headless_window() as window:
            with mock.patch.object(auth, "msa_load",
                                   side_effect=RuntimeError("offline")):
                self._run_warm_up(window)


if __name__ == "__main__":
    unittest.main()


class ASignInThatNeverFinishesTests(unittest.TestCase):
    """Issue #214: the window that loads for ever.

    xodus-cli opens Microsoft's own page in a webview, and a page that stops
    making progress there prints nothing and never exits. What the launcher
    did with that was the actual report: PLAY kept starting downloads the
    missing account would refuse, and every "Sign in" after the first was a
    button that did nothing at all, because the flag saying one was in flight
    was never going to be cleared.
    """

    @classmethod
    def setUpClass(cls):
        qt_app()

    @contextmanager
    def _signing_in(self):
        with headless_window() as window:
            window.ui_state["store_login_active"] = True
            window._refresh_store_row()
            yield window

    def test_the_row_offers_to_close_the_window(self):
        with self._signing_in() as window:
            self.assertEqual(_menu(window).download.button.text(), "Cancel")
            self.assertEqual(window.store_btn.text(), "Cancel")

    def test_cancelling_closes_the_sign_in(self):
        with self._signing_in() as window:
            with mock.patch.object(window, "_cancel_store_login") as cancel:
                window.store_click()          # arms the confirmation
                self.assertFalse(cancel.called)
                window.store_click()          # confirms it
            self.assertTrue(cancel.called)

    def test_play_does_not_start_a_download_the_sign_in_would_refuse(self):
        with self._signing_in() as window:
            with mock.patch.object(
                    window, "selected_version",
                    return_value={"edition": {"id": "release"}, "tag": "1.0"}), \
                    mock.patch.object(window, "_store_login_already_open") as said, \
                    mock.patch.object(window, "_start_worker") as started:
                window.do_play()
            # One certain failure otherwise: setup gets as far as the
            # download, finds no account and stops. Three of those in a row
            # is what #214 was reported with.
            self.assertTrue(said.called)
            self.assertFalse(started.called)

    def test_asking_for_a_second_sign_in_says_where_the_first_one_went(self):
        with self._signing_in() as window:
            with mock.patch.object(window, "_store_login_already_open") as said:
                window._link_store_account()
            # Returning quietly here is what made every later attempt look
            # like a dead button.
            self.assertTrue(said.called)

    def test_a_flag_with_no_sign_in_behind_it_is_cleared(self):
        with self._signing_in() as window:
            window._store_login_cancel_done(False)
            self.assertFalse(window.ui_state["store_login_active"])
            self.assertEqual(_menu(window).download.button.text(), "Sign in")

    def test_signing_in_twice_for_one_play_is_not_offered_a_third_time(self):
        with headless_window() as window:
            with mock.patch.object(window, "_offer_store_account_link",
                                   return_value=True), \
                    mock.patch.object(window, "_link_store_account"):
                window._store_signin_needed("no account")
            self.assertTrue(window.ui_state["store_signin_offered"])
            # The sign-in went through and the download still says there is
            # no account: asking for the same window again is the loop.
            with mock.patch.object(window, "_offer_store_account_link") as offer, \
                    mock.patch.object(window, "error_box") as box:
                window._store_signin_needed("no account")
            self.assertFalse(offer.called)
            self.assertTrue(box.called)

    def test_a_cancelled_sign_in_is_not_reported_as_a_failure(self):
        with headless_window() as window:
            failures = []
            with mock.patch.object(gui, "Worker") as worker, \
                    mock.patch.object(window, "error_box",
                                      side_effect=failures.append):
                window._link_store_account()
                failed = worker.return_value.failed.connect.call_args[0][0]
                failed(xodus.LOGIN_CANCELLED_MESSAGE)
            self.assertEqual(failures, [])
            self.assertFalse(window.ui_state["store_login_active"])
            self.assertIn("cancel", window.status_label.text().lower())

    def test_any_other_sign_in_failure_is_still_reported(self):
        with headless_window() as window:
            failures = []
            with mock.patch.object(gui, "Worker") as worker, \
                    mock.patch.object(window, "error_box",
                                      side_effect=lambda *a: failures.append(a)):
                window._link_store_account()
                failed = worker.return_value.failed.connect.call_args[0][0]
                failed("Microsoft would not")
            self.assertTrue(failures)


class DeviceLimitOpensThePageThatFixesItTests(unittest.TestCase):
    """The download refused because the account holds its ten Microsoft Store
    devices. Nothing in the launcher gives one back -- that happens on a
    Microsoft page -- so this is the failure the window has to hand over
    rather than print, and pick back up once they return."""

    @classmethod
    def setUpClass(cls):
        qt_app()

    def test_the_device_limit_is_reported_apart_from_a_failure(self):
        worker = gui.LaunchWorker({"edition": {"id": "release"}, "tag": "1.0"})
        seen = {"failed": [], "limit": []}
        worker.failed.connect(seen["failed"].append)
        worker.store_device_limit.connect(seen["limit"].append)
        with mock.patch.object(
                gui, "do_setup",
                side_effect=xodus.DeviceLimitReached("ten devices")):
            worker.run()
        self.assertEqual(seen["failed"], [])
        self.assertEqual(seen["limit"], ["ten devices"])

    def test_taking_the_offer_opens_the_microsoft_device_list(self):
        with headless_window() as window:
            with mock.patch.object(window, "_offer_device_page",
                                   return_value=True), \
                    mock.patch.object(window, "question_box",
                                      return_value=False), \
                    mock.patch.object(gui, "open_path") as opened:
                window._store_device_limit("ten devices")
            self.assertEqual(opened.call_args[0][0], xodus.DEVICE_PAGE)

    def test_coming_back_from_the_page_starts_the_download_again(self):
        # Nothing about the download changed while they were away; the account
        # it was refused for did.
        with headless_window() as window:
            with mock.patch.object(window, "_offer_device_page",
                                   return_value=True), \
                    mock.patch.object(window, "question_box",
                                      return_value=True), \
                    mock.patch.object(gui, "open_path"), \
                    mock.patch.object(window, "do_play") as play:
                window._store_device_limit("ten devices")
            self.assertTrue(play.called)

    def test_declining_leaves_the_launcher_idle(self):
        with headless_window() as window:
            with mock.patch.object(window, "_offer_device_page",
                                   return_value=False), \
                    mock.patch.object(gui, "open_path") as opened:
                window._store_device_limit("ten devices")
            self.assertFalse(opened.called)
            self.assertFalse(window.ui_state["busy"])
            self.assertFalse(window.ui_state["launch_active"])
            self.assertIn("devices", window.status_label.text().lower())

    def test_the_page_is_not_offered_twice_for_one_launch(self):
        with headless_window() as window:
            with mock.patch.object(window, "_offer_device_page",
                                   return_value=True), \
                    mock.patch.object(window, "question_box",
                                      return_value=False), \
                    mock.patch.object(gui, "open_path"):
                window._store_device_limit("ten devices")
            self.assertTrue(window.ui_state["device_page_offered"])
            # The devices were already opened once for this launch and the
            # answer has not changed: offering the same page again is a loop.
            with mock.patch.object(window, "_offer_device_page") as offer, \
                    mock.patch.object(window, "error_box") as box:
                window._store_device_limit("ten devices")
            self.assertFalse(offer.called)
            self.assertTrue(box.called)
