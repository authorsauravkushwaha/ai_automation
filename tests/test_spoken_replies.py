"""Typed, voice, confirmation and wake replies use the same offline speech path."""

from __future__ import annotations

import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hinata.config import InterfaceConfig
from hinata.core.types import ActionRequest, AssistantResponse
from hinata.main import emit_with_approvals, main
from hinata.speech.replies import spoken_reply


class SpokenReplyTests(unittest.TestCase):
    def test_enabled_by_default_with_explicit_privacy_opt_out(self) -> None:
        self.assertTrue(InterfaceConfig().speak_responses)
        answer = AssistantResponse("Hello there", True, "completed")
        voice = SimpleNamespace(speaker_status="accepted", speaker_match=True, speaker_required=False, text="hello")
        with patch("hinata.main.HinataAssistant") as assistant_cls, patch("hinata.main.emit_with_approvals", return_value=0) as emit:
            assistant = assistant_cls.return_value
            assistant.process_text.return_value = answer
            assistant.process_voice.return_value = voice, answer
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--no-llm", "ask", "hello"]), 0)
            self.assertTrue(emit.call_args.kwargs["speak"])
            self.assertFalse(emit.call_args.kwargs["speech_required"])
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--no-llm", "listen"]), 0)
            self.assertTrue(emit.call_args.kwargs["speak"])
            self.assertIs(emit.call_args.kwargs["voice"], voice)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--no-llm", "--no-speak", "ask", "hello"]), 0)
            self.assertFalse(emit.call_args.kwargs["speak"])
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--no-llm", "--speak", "ask", "hello"]), 0)
            self.assertTrue(emit.call_args.kwargs["speech_required"])

    def test_confirmation_is_spoken_without_exposing_arguments(self) -> None:
        action = ActionRequest("remember_preference", {"key": "private_key", "value": "secret123"})
        pending = AssistantResponse("Approve secret123?", False, "confirmation_required", action)
        assistant = SimpleNamespace(tts=Mock(), resume=Mock(return_value=AssistantResponse("Saved.", True)))
        assistant.tts.last_error = None
        with patch("builtins.input", return_value="yes"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(emit_with_approvals(assistant, pending, speak=True, wait_for_speech=True), 0)
        assistant.resume.assert_called_once()
        spoken = [call.args[0] for call in assistant.tts.speak.call_args_list]
        self.assertEqual(len(spoken), 2)
        self.assertNotIn("secret123", " ".join(spoken))
        self.assertIn("Saved.", spoken)
        assistant.tts.wait.assert_called()

    def test_pin_never_spoken_and_tts_failure_does_not_fake_success(self) -> None:
        pending = AssistantResponse("Enter PIN 1234", False, "pin_required", ActionRequest("clear_memory"))
        self.assertNotIn("1234", spoken_reply(pending))
        self.assertIn("plan", spoken_reply(AssistantResponse("Dangerous paths", False, "plan_confirmation_required")))
        tts = Mock()
        tts.last_error = "no output device"
        assistant = SimpleNamespace(tts=tts)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(emit_with_approvals(assistant, AssistantResponse("Done", True), speak=True,
                                                wait_for_speech=True, speech_required=False), 0)
        self.assertIn("no output device", error.getvalue())
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(emit_with_approvals(assistant, AssistantResponse("Done", True), speak=True,
                                                wait_for_speech=True, speech_required=True), 1)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(emit_with_approvals(assistant, AssistantResponse("Done", True), speak=False), 0)
        tts.speak.assert_called()

    def test_declined_action_reports_spoken_cancellation(self) -> None:
        action = ActionRequest("close_application", {"name": "notepad"})
        assistant = SimpleNamespace(tts=Mock())
        assistant.tts.last_error = None
        with patch("builtins.input", return_value="no"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(emit_with_approvals(
                assistant, AssistantResponse("Approval", False, "confirmation_required", action),
                speak=True, wait_for_speech=True,
            ), 1)
        self.assertIn("cancelled", assistant.tts.speak.call_args_list[-1].args[0])


if __name__ == "__main__":
    unittest.main()
