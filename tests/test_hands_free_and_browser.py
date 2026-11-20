import ctypes
import tempfile
import unittest
from importlib.util import find_spec
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from hinata.automation.app_control import AutomationError
from hinata.automation.browser_search import open_browser_search
from hinata.automation.web_search import WebSearchEngine
from hinata.automation.windows_control import WindowsControl
from hinata.core.intent_parser import IntentParser
from hinata.security.action_validator import ActionValidator, ValidationError
from hinata.security.offline_guard import OfflineGuard
from hinata.speech.microphone import HandsFreeSettings, Microphone, UtteranceDetector
from tests.helpers import make_assistant


class HandsFreeAndBrowserTests(unittest.TestCase):
    def test_pause_submits_one_utterance_only_after_speech(self):
        detector = UtteranceDetector()
        self.assertFalse(any(detector.update(0.001) for _ in range(200)))
        self.assertFalse(detector.update(0.1))
        self.assertFalse(any(detector.update(0.001) for _ in range(4)))
        self.assertFalse(detector.update(0.1))  # brief mid-sentence pause was not a turn
        self.assertFalse(any(detector.update(0.001) for _ in range(4)))
        self.assertTrue(detector.update(0.001))

    def test_long_utterance_is_capped(self):
        detector = UtteranceDetector()
        self.assertFalse(any(detector.update(0.1) for _ in range(119)))
        self.assertTrue(detector.update(0.1))

    def test_tunable_speech_threshold_and_pause(self):
        for sensitivity, detected in (("high", True), ("normal", False), ("low", False)):
            with self.subTest(sensitivity=sensitivity):
                detector = HandsFreeSettings(750, sensitivity).make_detector()
                self.assertFalse(detector.update(0.008))
                self.assertFalse(any(detector.update(0.0) for _ in range(2)))
                self.assertEqual(detector.update(0.0), detected)
        patient = HandsFreeSettings(2500, "low").make_detector()
        self.assertFalse(patient.update(0.008))  # ambient noise stays below the low-sensitivity gate
        self.assertFalse(patient.update(0.1))
        self.assertFalse(any(patient.update(0.008) for _ in range(9)))
        self.assertTrue(patient.update(0.008))

    def test_saved_voice_settings_keep_valid_values_and_default_invalid_ones(self):
        self.assertEqual(HandsFreeSettings.from_saved("1750", "high"), HandsFreeSettings(1750, "high"))
        self.assertEqual(HandsFreeSettings.from_saved("not a duration", "low"), HandsFreeSettings(1250, "low"))
        self.assertEqual(HandsFreeSettings.from_saved("2500", "invalid"), HandsFreeSettings(2500, "normal"))
        with self.assertRaises(ValueError):
            HandsFreeSettings(0, "normal")

    @unittest.skipUnless(find_spec("numpy"), "optional NumPy dependency not installed")
    def test_capture_retains_at_most_two_idle_blocks_and_resets_on_restart(self):
        import numpy as np

        mic = Microphone()
        portaudio = MagicMock()
        with patch.object(mic, "_sounddevice", return_value=portaudio):
            mic.start()
            options = portaudio.InputStream.call_args.kwargs
            callback, frames = options["callback"], options["blocksize"]

            def capture(level):
                callback(np.full((frames, 1), level, dtype="float32"), frames, None, False)
                return mic.poll_utterance()

            for _ in range(300):
                self.assertFalse(capture(0.0))
            self.assertFalse(capture(0.1))
            for _ in range(4):
                self.assertFalse(capture(0.0))
            self.assertTrue(capture(0.0))
            audio = mic.stop()
            self.assertEqual(audio.size, 8 * frames)  # 2 pre-roll, 1 speech, 5 quiet
            np.testing.assert_allclose(audio[2 * frames:3 * frames], 0.1)
            self.assertFalse(mic.recording)
            portaudio.InputStream.return_value.close.assert_called_once()

            mic.start()
            callback = portaudio.InputStream.call_args.kwargs["callback"]
            self.assertFalse(capture(0.05))
            mic.cancel()
            mic.start()
            callback = portaudio.InputStream.call_args.kwargs["callback"]
            for _ in range(6):
                self.assertFalse(capture(0.0))
            self.assertFalse(capture(0.15))
            for _ in range(4):
                self.assertFalse(capture(0.0))
            self.assertTrue(capture(0.0))
            audio = mic.stop()
            self.assertEqual(audio.size, 8 * frames)
            np.testing.assert_allclose(audio[2 * frames:3 * frames], 0.15)

    def test_commands_are_distinct_from_local_search(self):
        parser = IntentParser(ActionValidator())
        self.assertEqual(parser.parse("search train times").tool, "web_search")
        self.assertEqual(parser.parse_multitask("search Linux and open source"), [])
        self.assertEqual(parser.parse("search files for train times").tool, "search_files")
        self.assertEqual(parser.parse("search memory for train times").tool, "memory_search")
        self.assertEqual(parser.parse("Search in Firefox for train times").arguments, {
            "browser": "Firefox", "query": "train times", "engine": "duckduckgo",
        })
        self.assertEqual(parser.parse("Google train times in Chrome.").arguments, {
            "browser": "Chrome", "query": "train times", "engine": "google",
        })
        self.assertEqual(parser.parse("Scroll down two notches.").arguments, {
            "direction": "down", "steps": 2,
        })
        with self.assertRaises(ValidationError):
            parser.parse("scroll down 99")

    def test_browser_search_executes_without_a_model_and_encodes_user_input(self):
        with tempfile.TemporaryDirectory() as directory:
            assistant = make_assistant(directory, llm_enabled=False)
            try:
                query = "C++ & lambda # examples; then open notepad"
                with (
                    patch("hinata.automation.web_search.open_browser_search", return_value="your default browser") as open_search,
                    patch.object(assistant.orchestrator.apps, "open") as open_app,
                ):
                    response = assistant.process_text(f"search in browser for {query}")
                    open_app.assert_not_called()
                self.assertTrue(response.success, response.text)
                url, browser = open_search.call_args.args
                self.assertEqual(browser, "browser")
                self.assertEqual(urlsplit(url).hostname, "duckduckgo.com")
                self.assertEqual(parse_qs(urlsplit(url).query), {"q": [query]})
            finally:
                assistant.close()

    def test_browser_handoff_respects_web_policy(self):
        with patch("hinata.automation.web_search.open_browser_search") as launch:
            for engine in (
                WebSearchEngine(enabled=False),
                WebSearchEngine(strict_offline=True),
                WebSearchEngine(offline_guard=OfflineGuard(strict_offline=True)),
                WebSearchEngine(offline_guard=OfflineGuard(online_mode_enabled=True, allowed_online_domains=["example.com"])),
            ):
                with self.subTest(engine=engine), self.assertRaises(AutomationError):
                    engine.search_in_browser("train times")
            launch.assert_not_called()

    def test_named_browser_must_match_registered_executable_or_label(self):
        exe = Path("registered") / "firefox.exe"
        url = "https://www.google.com/search?q=train"
        with (
            patch("hinata.automation.browser_search.os.name", "nt"),
            patch("hinata.automation.browser_search._registered_browsers", return_value=[("LocalFirefox", "Mozilla Firefox", exe)]),
            patch("hinata.automation.browser_search.subprocess.Popen") as launch,
        ):
            self.assertEqual(open_browser_search(url, "Firefox"), "Mozilla Firefox")
            launch.assert_called_once_with([str(exe), url], shell=False, close_fds=True)
            with self.assertRaisesRegex(AutomationError, "not found"):
                open_browser_search(url, "unregistered")

    def test_scroll_runs_through_the_existing_voice_permission_check(self):
        with tempfile.TemporaryDirectory() as directory:
            assistant = make_assistant(directory, llm_enabled=False)
            try:
                with patch.object(assistant.orchestrator.windows, "scroll", return_value={"direction": "up"}) as scroll:
                    denied = assistant.orchestrator.process("scroll up", source="voice", speaker_required=True)
                    self.assertFalse(denied.success)
                    scroll.assert_not_called()
                    allowed = assistant.orchestrator.process(
                        "Hinata, scroll up.", source="voice", speaker_required=True, speaker_match=True
                    )
                    self.assertTrue(allowed.success, allowed.text)
                    scroll.assert_called_once_with("up", steps=3)
            finally:
                assistant.close()

    def test_scroll_only_targets_the_hovered_child_of_the_foreground_window(self):
        win = MagicMock()
        win.GetForegroundWindow.return_value = 101
        win.WindowFromPoint.return_value = 202
        win.GetAncestor.return_value = 101
        windows = WindowsControl()
        with (
            patch.object(windows, "_require_windows"),
            patch.object(ctypes, "windll", SimpleNamespace(user32=win), create=True),
        ):
            windows.scroll("down", steps=2)
            win.PostMessageW.assert_called_once_with(202, 0x020A, ((-240 & 0xFFFF) << 16), 0)
            win.PostMessageW.reset_mock()
            win.GetAncestor.return_value = 303
            with self.assertRaises(AutomationError):
                windows.scroll("up")
            win.PostMessageW.assert_not_called()
