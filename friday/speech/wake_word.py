"""Optional, disabled-by-default openWakeWord adapter for a custom Hey FRIDAY model."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from friday.config import WakeWordConfig


class WakeWordError(RuntimeError):
    pass


class WakeWordDetector:
    def __init__(self, config: WakeWordConfig) -> None:
        self.config = config
        self._model: Any = None
        self._last_detection = 0.0

    def load(self) -> None:
        if not self.config.enabled:
            raise WakeWordError("Wake-word support is disabled in configuration")
        model_path = Path(self.config.model_path).expanduser()
        if not model_path.is_file():
            raise WakeWordError(
                "Set wake_word.model_path to a locally trained 'Hey FRIDAY' ONNX model"
            )
        try:
            from openwakeword.model import Model
        except ImportError as exc:
            raise WakeWordError(
                "openWakeWord is optional: python -m pip install -e \".[wake]\""
            ) from exc
        try:
            self._model = Model(wakeword_models=[str(model_path)], inference_framework="onnx")
        except Exception as exc:
            self._model = None
            raise WakeWordError(f"Could not load wake-word model: {exc}") from exc

    def process(self, audio_chunk: Any) -> tuple[bool, float]:
        if self._model is None:
            self.load()
        np = __import__("numpy")
        samples = np.asarray(audio_chunk)
        if samples.dtype != np.int16:
            samples = (np.clip(samples, -1.0, 1.0) * 32767).astype("int16")
        try:
            predictions = self._model.predict(samples.reshape(-1))
            score = max((float(value) for value in predictions.values()), default=0.0)
        except Exception as exc:
            raise WakeWordError(f"Wake-word inference failed: {exc}") from exc
        now = time.monotonic()
        detected = score >= self.config.threshold and now - self._last_detection >= self.config.cooldown_seconds
        if detected:
            self._last_detection = now
        return detected, score

    def close(self) -> None:
        self._model = None


class WakeWordService:
    """Explicit always-on mode with a bounded queue and one inference worker."""

    def __init__(
        self,
        detector: WakeWordDetector,
        device: str | int | None = None,
        sample_rate: int = 16000,
    ) -> None:
        self.detector = detector
        self.device = device
        self.sample_rate = sample_rate
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=100)
        self._stop = threading.Event()
        self._stream: Any = None
        self._thread: threading.Thread | None = None
        self._callback: Callable[[float], None] | None = None
        self.last_error: str | None = None

    @property
    def running(self) -> bool:
        return self._stream is not None and not self._stop.is_set()

    def start(self, on_detection: Callable[[float], None]) -> None:
        if self._stream is not None:
            raise WakeWordError("Stop the previous wake-word stream before restarting")
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise WakeWordError(
                "Wake-word listening needs sounddevice and openWakeWord: python -m pip install -e \".[speech,wake]\""
            ) from exc
        self._stop.clear()
        self.last_error = None
        self._callback = on_detection
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

        def audio_callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
            del frames, time_info, status
            try:
                self._queue.put_nowait(indata[:, 0].copy())
            except queue.Full:
                pass  # Drop stale audio instead of growing RAM or inference latency.

        try:
            # openWakeWord expects 80 ms / 1280-sample frames at 16 kHz.
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="int16",
                device=self.device,
                blocksize=1280,
                callback=audio_callback,
            )
            self._stream.start()
        except Exception as exc:
            if self._stream is not None:
                try:
                    self._stream.close()
                except Exception:
                    pass
            self._stream = None
            raise WakeWordError(f"Could not start wake-word microphone: {exc}") from exc
        self._thread = threading.Thread(target=self._run, name="friday-wake-word", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                chunk = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                detected, score = self.detector.process(chunk)
            except Exception as exc:
                self.last_error = str(exc)
                self._stop.set()
                return
            if detected and self._callback is not None:
                try:
                    self._callback(score)
                except Exception as exc:
                    self.last_error = f"Wake-word callback failed: {exc}"
                    self._stop.set()
                    return

    def stop(self) -> None:
        self._stop.set()
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._callback = None
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
