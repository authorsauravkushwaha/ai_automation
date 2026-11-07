"""Offline CPU speech recognition with lazy faster-whisper or Vosk backends."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hinata.config import SpeechConfig


class SpeechRecognitionError(RuntimeError):
    pass


class SpeechToText:
    def __init__(self, config: SpeechConfig) -> None:
        self.config = config
        self._model: Any = None
        self._recognizer: Any = None

    def prepare(self) -> str:
        """Load/download the configured local model explicitly during setup."""
        if self.config.stt_backend == "faster-whisper":
            self._load_whisper()
            return f"Ready: faster-whisper {self.config.whisper_model} (CPU/{self.config.whisper_compute_type})"
        self._load_vosk(self.config.sample_rate)
        return f"Ready: Vosk model at {self.config.vosk_model_path}"

    def transcribe(self, audio: Any, sample_rate: int = 16000) -> str:
        if sample_rate != 16000:
            raise SpeechRecognitionError("Speech input must use a 16000 Hz sample rate")
        if getattr(audio, "size", 0) < sample_rate // 2:
            raise SpeechRecognitionError("The recording is too short to transcribe")
        if self.config.stt_backend == "faster-whisper":
            return self._transcribe_whisper(audio)
        return self._transcribe_vosk(audio, sample_rate)

    def _load_whisper(self) -> Any:
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise SpeechRecognitionError(
                    "faster-whisper is not installed. Run: python -m pip install -e \".[speech]\""
                ) from exc
            try:
                self._model = WhisperModel(
                    self.config.whisper_model,
                    device="cpu",
                    compute_type=self.config.whisper_compute_type,
                    cpu_threads=2,
                    num_workers=1,
                )
            except Exception as exc:
                raise SpeechRecognitionError(
                    f"Could not load local Whisper model '{self.config.whisper_model}': {exc}. "
                    "The first model load needs internet; later loads use the local cache."
                ) from exc
        return self._model

    def _transcribe_whisper(self, audio: Any) -> str:
        model = self._load_whisper()
        try:
            segments, _ = model.transcribe(
                audio,
                language="en",
                beam_size=1,
                best_of=1,
                vad_filter=True,
                condition_on_previous_text=False,
            )
            text = " ".join(segment.text.strip() for segment in segments).strip()
        except Exception as exc:
            raise SpeechRecognitionError(f"Offline Whisper transcription failed: {exc}") from exc
        if not text:
            raise SpeechRecognitionError("No intelligible speech was detected")
        return text

    def _load_vosk(self, sample_rate: int) -> Any:
        if self._recognizer is not None:
            return self._recognizer
        try:
            from vosk import KaldiRecognizer, Model
        except ImportError as exc:
            raise SpeechRecognitionError(
                "Vosk is not installed. Run: python -m pip install -e \".[light-speech]\""
            ) from exc
        try:
            if self._model is None:
                model_path = Path(self.config.vosk_model_path).expanduser()
                if not model_path.is_dir():
                    raise SpeechRecognitionError(
                        "Set speech.vosk_model_path to an extracted local Vosk model directory"
                    )
                self._model = Model(str(model_path))
            # A fresh recognizer resets words/state without reloading the expensive model.
            self._recognizer = KaldiRecognizer(self._model, sample_rate)
        except SpeechRecognitionError:
            raise
        except Exception as exc:
            raise SpeechRecognitionError(f"Could not load Vosk model: {exc}") from exc
        return self._recognizer

    def _transcribe_vosk(self, audio: Any, sample_rate: int) -> str:
        recognizer = self._load_vosk(sample_rate)
        np = __import__("numpy")
        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("int16").tobytes()
        try:
            recognizer.AcceptWaveform(pcm)
            result = json.loads(recognizer.FinalResult())
            self._recognizer = None  # Do not carry words into the next independent command.
        except Exception as exc:
            self._recognizer = None
            raise SpeechRecognitionError(f"Offline Vosk transcription failed: {exc}") from exc
        text = str(result.get("text", "")).strip()
        if not text:
            raise SpeechRecognitionError("No intelligible speech was detected")
        return text

    def unload(self) -> None:
        self._recognizer = None
        self._model = None
