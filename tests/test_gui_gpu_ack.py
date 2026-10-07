"""Clear a GPU safety block where it is met, not three menus away.

PLAY refuses the launch, so the failure dialog is where the acknowledgement
belongs. It used to only tell the player to open Settings ▸ Tools and find
the action there, which is the whole reason an ordinary reboot with the game
still open became a recurring chore. None of the safety decision moves here:
eligibility is still re-checked under the launch lock before anything is
written, and these tests pin that the confirmation is never skipped.
"""
# SPDX-License-Identifier: MIT

import unittest
from unittest import mock

from bol import gui
from bol.gpu_safety import GpuSafetyAcknowledgementStatus
from tests.guiharness import headless_window, qt_app


def _status(previous_boot_fault=False, message="an interrupted launch"):
    return GpuSafetyAcknowledgementStatus(
        code="previous-boot",
        can_acknowledge=True,
        message=message,
        marker_present=True,
        previous_boot_fault=previous_boot_fault,
    )


class FakeMessageBox:
    """Records what the dialogs were asked, and answers yes or no."""

    def __init__(self, answer=True):
        self.answer = answer
        self.asked = []
        self.info = []
        self.errors = []

    def askyesno(self, title, message, parent=None):
        self.asked.append((title, message, parent))
        return self.answer

    def showinfo(self, title, message, parent=None):
        self.info.append((title, message, parent))

    def showerror(self, title, message, parent=None):
        self.errors.append((title, message, parent))


class AcknowledgementOfferTests(unittest.TestCase):
    def _offer(self, answer=True, acknowledged=True, status=None, **kwargs):
        box = FakeMessageBox(answer)
        ack = mock.Mock(return_value=acknowledged)
        with mock.patch.object(gui, "acknowledge_gpu_crash", ack), \
                mock.patch.object(gui, "gpu_crash_acknowledgement_status",
                                  return_value=_status(
                                      message="still refused")):
            result = gui._offer_gpu_incident_acknowledgement(
                box, "parent", status or _status(), **kwargs)
        return result, box, ack

    def test_declining_the_confirmation_writes_nothing(self):
        result, box, ack = self._offer(answer=False)
        self.assertFalse(result)
        self.assertFalse(ack.called)
        self.assertEqual(box.info, [])

    def test_accepting_acknowledges_once_and_confirms(self):
        result, box, ack = self._offer()
        self.assertTrue(result)
        self.assertEqual(ack.call_count, 1)
        self.assertIn("acknowledged", box.info[0][1])

    def test_a_refused_acknowledgement_reports_why_and_fails(self):
        result, box, ack = self._offer(acknowledged=False)
        self.assertFalse(result)
        self.assertTrue(ack.called)
        self.assertEqual(box.info, [])
        # The live status is what explains the refusal, not the stale one.
        self.assertEqual(box.errors[0][1], "still refused")

    def test_the_block_itself_is_still_shown_in_full(self):
        # The launch path passes the failure text as the prefix; losing it
        # would trade one annoyance for a dialog that explains nothing.
        _, box, _ = self._offer(prefix="Unsafe graphics session: …\n\n",
                                title="Minecraft could not start")
        title, message, parent = box.asked[0]
        self.assertEqual(title, "Minecraft could not start")
        self.assertTrue(message.startswith("Unsafe graphics session: …"))
        self.assertIn("an interrupted launch", message)
        self.assertEqual(parent, "parent")

    def test_the_confirmation_names_what_must_be_checked_first(self):
        _, box, _ = self._offer(status=_status(previous_boot_fault=True))
        self.assertIn("repairing/updating the graphics driver",
                      box.asked[0][1])
        _, box, _ = self._offer(status=_status(previous_boot_fault=False))
        self.assertIn("No fatal driver event was detected", box.asked[0][1])

    def test_the_default_title_is_kept_for_the_settings_entry(self):
        _, box, _ = self._offer()
        self.assertEqual(box.asked[0][0], "Acknowledge previous GPU incident")

    def test_play_carries_on_instead_of_a_note_saying_it_could(self):
        resumed = []
        result, box, ack = self._offer(then=lambda: resumed.append(True))
        self.assertTrue(result)
        self.assertEqual(resumed, [True])
        self.assertEqual(box.info, [])

    def test_nothing_carries_on_after_no_or_a_refusal(self):
        resumed = []
        self._offer(answer=False, then=lambda: resumed.append(True))
        self._offer(acknowledged=False, then=lambda: resumed.append(True))
        self.assertEqual(resumed, [])


class PlayFailureResumeTests(unittest.TestCase):
    """Yes in the "Minecraft could not start" dialog starts Minecraft.

    It used to end on a note that PLAY would run its checks again, with the
    player left to press PLAY a second time after every reboot that found
    the game had been open at shutdown.
    """

    def setUp(self):
        qt_app()

    def _fail(self, window, offer):
        with mock.patch.object(gui, "gpu_crash_acknowledgement_status",
                               return_value=_status()), \
                mock.patch.object(window, "_offer_gpu_ack",
                                  side_effect=offer) as offered, \
                mock.patch.object(window, "error_box") as error_box:
            window._play_failed("Unsafe graphics session: …")
        return offered, error_box

    def test_yes_resumes_play_once(self):
        def yes(_status, prefix="", title="", then=None):
            then()
            return True

        with headless_window() as window, \
                mock.patch.object(window, "do_play") as do_play:
            offered, error_box = self._fail(window, yes)
            self.assertEqual(do_play.call_count, 1)
            self.assertEqual(offered.call_args.kwargs["title"],
                             "Minecraft could not start")
            error_box.assert_not_called()
            # Refused again on the resumed attempt: shown as it is, rather
            # than the same question in a loop.
            offered, error_box = self._fail(window, yes)
            offered.assert_not_called()
            error_box.assert_called_once()
            self.assertEqual(do_play.call_count, 1)

    def test_no_leaves_play_alone(self):
        with headless_window() as window, \
                mock.patch.object(window, "do_play") as do_play:
            self._fail(window, lambda *_a, **_kw: False)
            do_play.assert_not_called()
            self.assertNotIn("gpu_ack_resumed", window.ui_state)


class SafetyInstructionTests(unittest.TestCase):
    def test_a_verified_driver_fault_demands_a_repair_and_a_reboot(self):
        self.assertIn("rebooting", gui._gpu_incident_safety_instruction(
            _status(previous_boot_fault=True)))

    def test_an_unexplained_stop_demands_an_inspection(self):
        instruction = gui._gpu_incident_safety_instruction(_status())
        self.assertIn("why the previous session or machine stopped",
                      instruction)


if __name__ == "__main__":
    unittest.main()
