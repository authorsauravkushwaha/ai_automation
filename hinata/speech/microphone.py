"""Start/stop microphone capture. Audio remains in memory and is never uploaded."""

from __future__ import annotations

import queue
import threading
import time
from typing import Any


class MicrophoneError(RuntimeError):
    pass


class Microphone:
    def __init__(self, device: str | int | None = None, sample_rate: int = 16000) -> None:
        self.device = device
        self.sample_rate = sample_rate
        self._stream: Any = None
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=256)
        self._chunks: list[Any] = []
        self._lock = threading.Lock()

    @staticmethod
    def _sounddevice() -> Any:
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise MicrophoneError(
                "Microphone support is not installed. Run: python -m pip install -e \".[speech]\""
            ) from exc
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
                    blocksize=int(self.sample_rate * 0.25),
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

    def stop(self) -> Any:
        with self._lock:
            stream, self._stream = self._stream, None
        if stream is None:
            raise MicrophoneError("The microphone is not listening")
        try:
            stream.stop()
            stream.close()
        except Exception as exc:
            raise MicrophoneError(f"Could not stop microphone cleanly: {exc}") from exc
        np = self._numpy_module()
        while True:
            try:
                self._chunks.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if not self._chunks:
            raise MicrophoneError("No microphone audio was captured")
        audio = np.concatenate(self._chunks).astype("float32", copy=False)
        self._chunks = []
        if audio.size < self.sample_rate // 2:
            raise MicrophoneError("The recording is too short; speak for at least half a second")
        return audio

    def cancel(self) -> None:
        with self._lock:
            stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        self._chunks = []
        while not self._queue.empty():
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
