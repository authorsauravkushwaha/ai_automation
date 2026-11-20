"""Start/stop microphone capture. Audio remains in memory and is never uploaded."""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Any


class MicrophoneError(RuntimeError):
    pass


BLOCK_DURATION_MS = 250
HANDS_FREE_PAUSES_MS = (750, 1250, 1750, 2500)
SPEECH_THRESHOLDS = {"high": 0.006, "normal": 0.012, "low": 0.024}


@dataclass(frozen=True, slots=True)
class HandsFreeSettings:
    pause_ms: int = 1250
    sensitivity: str = "normal"

    def __post_init__(self) -> None:
        if self.pause_ms not in HANDS_FREE_PAUSES_MS or self.sensitivity not in SPEECH_THRESHOLDS:
            raise ValueError("Unsupported hands-free pause or microphone sensitivity")

    @classmethod
    def from_saved(cls, pause: str | None, sensitivity: str | None) -> HandsFreeSettings:
        defaults = cls()
        pause_ms = next((ms for ms in HANDS_FREE_PAUSES_MS if str(ms) == pause), defaults.pause_ms)
        return cls(pause_ms, sensitivity if sensitivity in SPEECH_THRESHOLDS else defaults.sensitivity)

    def make_detector(self) -> UtteranceDetector:
        return UtteranceDetector(
            threshold=SPEECH_THRESHOLDS[self.sensitivity], quiet_blocks_required=self.pause_ms // BLOCK_DURATION_MS,
        )


@dataclass(slots=True)
class UtteranceDetector:
    """A bounded energy gate for 250 ms microphone blocks, not a transcription engine."""

    threshold: float = SPEECH_THRESHOLDS["normal"]
    quiet_blocks_required: int = 5
    heard_speech: bool = False
    quiet_blocks: int = 0
    elapsed_blocks: int = 0

    def update(self, rms: float) -> bool:
        if rms >= self.threshold:
            self.heard_speech = True
            self.quiet_blocks = 0
        elif self.heard_speech:
            self.quiet_blocks += 1
        if self.heard_speech:
            self.elapsed_blocks += 1
        # Submit after the configured pause, or cap the turn at 30 seconds.
        return self.quiet_blocks >= self.quiet_blocks_required or self.elapsed_blocks >= 30_000 // BLOCK_DURATION_MS


class Microphone:
    def __init__(self, device: str | int | None = None, sample_rate: int = 16000) -> None:
        self.device = device
        self.sample_rate = sample_rate
        self._stream: Any = None
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=256)
        self._chunks: list[Any] = []
        self._lock = threading.Lock()
        self.hands_free = HandsFreeSettings()
        self._detector = self.hands_free.make_detector()

    @staticmethod
    def _sounddevice() -> Any:
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise MicrophoneError(
                "Microphone support is not installed. Run: python -m pip install -e \".[speech]\""
            ) from exc
        except OSError as exc:
            raise MicrophoneError(f"Could not load the PortAudio microphone driver: {exc}") from exc
        return sd

    def list_devices(self) -> list[dict[str, Any]]:
        sd = self._sounddevice()
        try:
            devices = sd.query_devices()
            return [
                {"index": index, "name": str(item["name"]), "input_channels": int(item["max_input_channels"])}
                for index, item in enumerate(devices)
                if item["max_input_channels"] > 0
            ]
        except Exception as exc:
            raise MicrophoneError(f"Could not enumerate microphones: {exc}") from exc

    def start(self) -> None:
        with self._lock:
            if self._stream is not None:
                raise MicrophoneError("The microphone is already listening")
            sd = self._sounddevice()
            self._chunks = []
            self._detector = self.hands_free.make_detector()
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break

            def callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
                del frames, time_info
                if status:
                    # Overruns are handled by bounded queueing rather than unbounded memory growth.
                    return
                try:
                    self._queue.put_nowait(indata[:, 0].copy())
                except queue.Full:
                    pass

            try:
                self._stream = sd.InputStream(
                    samplerate=self.sample_rate,
                    channels=1,
                    dtype="float32",
                    device=self.device,
                    blocksize=self.sample_rate * BLOCK_DURATION_MS // 1000,
                    callback=callback,
                )
                self._stream.start()
            except Exception as exc:
                if self._stream is not None:
                    try:
                        self._stream.close()
                    except Exception:
                        pass
                self._stream = None
                raise MicrophoneError(f"Could not start microphone: {exc}") from exc

    def poll_utterance(self) -> bool:
        """Keep a short pre-roll while idle; report once a spoken turn is ready."""
        np = self._numpy_module()
        with self._lock:
            while self._stream is not None:
                try:
                    block = self._queue.get_nowait()
                except queue.Empty:
                    break
                self._chunks.append(block)
                if self._detector.update(float(np.sqrt(np.mean(block * block)))):
                    return True
                if not self._detector.heard_speech:
                    self._chunks = self._chunks[-2:]
            return False

    def stop(self) -> Any:
        with self._lock:
            stream, self._stream = self._stream, None
            if stream is None:
                raise MicrophoneError("The microphone is not listening")
            try:
                try:
                    stream.stop()
                finally:
                    stream.close()
            except Exception as exc:
                raise MicrophoneError(f"Could not stop microphone cleanly: {exc}") from exc
            chunks, self._chunks = self._chunks, []
            while True:
                try:
                    chunks.append(self._queue.get_nowait())
                except queue.Empty:
                    break
        if not chunks:
            raise MicrophoneError("No microphone audio was captured")
        audio = self._numpy_module().concatenate(chunks).astype("float32", copy=False)
        if audio.size < self.sample_rate // 2:
            raise MicrophoneError("The recording is too short; speak for at least half a second")
        return audio

    def cancel(self) -> None:
        with self._lock:
            stream, self._stream = self._stream, None
            if stream is not None:
                try:
                    try:
                        stream.stop()
                    finally:
                        stream.close()
                except Exception:
                    pass
            self._chunks = []
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break

    @property
    def recording(self) -> bool:
        return self._stream is not None

    def record(self, duration_seconds: float = 5.0) -> Any:
        if not 0.5 <= duration_seconds <= 30:
            raise MicrophoneError("Recording duration must be between 0.5 and 30 seconds")
        self.start()
        try:
            time.sleep(duration_seconds)
            return self.stop()
        except Exception:
            self.cancel()
            raise

    @staticmethod
    def _numpy_module() -> Any:
        try:
            import numpy as np
        except ImportError as exc:
            raise MicrophoneError("NumPy is required for microphone audio") from exc
        return np
