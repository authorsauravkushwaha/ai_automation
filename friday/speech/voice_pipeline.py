"""Shared microphone -> speaker check -> STT path used by CLI and desktop UI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from friday.speech.microphone import Microphone
from friday.speech.speaker_recognition import SpeakerError, SpeakerRecognition
from friday.speech.speech_to_text import SpeechToText


@dataclass(slots=True)
class VoiceResult:
    text: str
    speaker_match: bool | None
    speaker_required: bool
    speaker_status: str
    speaker_score: float | None = None


class VoicePipeline:
    def __init__(self, microphone: Microphone, stt: SpeechToText, speaker: SpeakerRecognition) -> None:
        self.microphone = microphone
        self.stt = stt
        self.speaker = speaker

    def start(self) -> None:
        self.microphone.start()

    def stop_and_process(self) -> VoiceResult:
        return self.process_audio(self.microphone.stop())

    def listen(self, duration_seconds: float = 5.0) -> VoiceResult:
        return self.process_audio(self.microphone.record(duration_seconds))

    def process_audio(self, audio: Any) -> VoiceResult:
        verification = self.speaker.verify(audio)
        if verification.required and verification.match is not True:
            raise SpeakerError(
                f"Speaker verification was {verification.status}"
                + (f" (score {verification.score:.3f})" if verification.score is not None else "")
                + ". The command was not transcribed or executed."
            )
        text = self.stt.transcribe(audio, self.microphone.sample_rate)
        return VoiceResult(
            text=text,
            speaker_match=verification.match,
            speaker_required=verification.required,
            speaker_status=verification.status,
            speaker_score=verification.score,
        )
