"""Interruptible offline TTS using Windows SAPI (pyttsx3) or a Piper executable."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hinata.config import SpeechConfig

StateCallback = Callable[[str, str | None], None]


class SpeechOutputError(RuntimeError):
    pass


def output_readiness(config: SpeechConfig) -> dict[str, Any]:
    """Non-speaking dependency check. A real device can only be verified by test."""
    if config.tts_backend == "piper":
        executable = Path(config.piper_executable).expanduser().is_file() if config.piper_executable else False
        model = Path(config.piper_model).expanduser().is_file() if config.piper_model else False
        configured = bool(executable and model and os.name == "nt")
        guidance = (
            "Piper executable and local voice model are configured; run voice-output test to verify playback."
            if configured else "Configure existing speech.piper_executable and speech.piper_model paths on Windows."
        )
    else:
        configured = importlib.util.find_spec("pyttsx3") is not None
        guidance = (
            "pyttsx3 is installed; run voice-output test to verify the actual audio device."
            if configured else 'Install offline voice output: python -m pip install -e ".[tts]"'
        )
    return {"backend": config.tts_backend, "backend_ready": configured, "guidance": guidance}


class TextToSpeech:
    def __init__(self, config: SpeechConfig) -> None:
        self.config = config
        self._cancel_event = threading.Event()
        self._speaking = threading.Event()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._engine: Any = None
        self._process: subprocess.Popen[str] | None = None
        self._generation = 0
        self.last_error: str | None = None

    @property
    def speaking(self) -> bool:
        return self._speaking.is_set()

    def speak(self, text: str, on_state: StateCallback | None = None) -> None:
        cleaned = text.strip()
        if not cleaned:
            return
        if len(cleaned) > 4000:
            cleaned = cleaned[:4000]
        self.stop()
        cancel = threading.Event()
        self.last_error = None
        with self._lock:
            generation = self._generation
            self._cancel_event = cancel
            self._speaking.set()
            self._thread = threading.Thread(
                target=self._worker,
                args=(cleaned[:4000], generation, cancel, on_state),
                name="hinata-tts",
                daemon=True,
            )
            self._thread.start()
        self._notify(on_state, "speaking", None)

    def _worker(
        self,
        text: str,
        generation: int,
        cancel: threading.Event,
        callback: StateCallback | None,
    ) -> None:
        try:
            if self.config.tts_backend == "piper":
                self._run_piper(text, generation, cancel)
            else:
                self._run_pyttsx3(text, generation, cancel)
            if not cancel.is_set():
                self._notify(callback, "idle", None)
        except Exception as exc:
            if not cancel.is_set():
                self.last_error = str(exc)
                self._notify(callback, "error", str(exc))
        finally:
            with self._lock:
                if generation == self._generation:
                    self._engine = None
                    self._process = None
                    self._speaking.clear()

    def _run_pyttsx3(self, text: str, generation: int, cancel: threading.Event) -> None:
        try:
            import pyttsx3
        except ImportError as exc:
            raise SpeechOutputError(
                "Offline SAPI output needs pyttsx3. Run: python -m pip install -e \".[tts]\""
            ) from exc
        engine: Any = None
        # SAPI runs in this dedicated thread. Initialize its COM apartment here,
        # not on the GUI/CLI caller thread; leave other platforms unchanged.
        ole32: Any = None
        com_initialized = False
        try:
            if os.name == "nt":  # pragma: no cover - requires Windows SAPI
                import ctypes

                windll = getattr(ctypes, "windll", None)
                if windll is not None:
                    ole32 = windll.ole32
                    status = int(ole32.CoInitializeEx(None, 2))  # COINIT_APARTMENTTHREADED
                    if status not in (0, 1):
                        raise SpeechOutputError(f"Windows SAPI COM initialization failed: {status}")
                    com_initialized = True
            engine = pyttsx3.init("sapi5") if os.name == "nt" else pyttsx3.init()
            with self._lock:
                if cancel.is_set() or generation != self._generation:
                    return
                self._engine = engine
            engine.setProperty("rate", self.config.tts_rate)
            if self.config.tts_voice:
                needle = self.config.tts_voice.casefold()
                voices = engine.getProperty("voices")
                selected = next(
                    (
                        voice
                        for voice in voices
                        if needle in str(getattr(voice, "id", "")).casefold()
                        or needle in str(getattr(voice, "name", "")).casefold()
                    ),
                    None,
                )
                if selected is None:
                    raise SpeechOutputError(f"No installed offline voice matches '{self.config.tts_voice}'")
                engine.setProperty("voice", selected.id)
            if cancel.is_set() or generation != self._generation:
                return
            engine.say(text)
            if cancel.is_set() or generation != self._generation:
                return
            engine.runAndWait()
        except SpeechOutputError:
            raise
        except Exception as exc:
            if not cancel.is_set():
                raise SpeechOutputError(f"Offline speech output failed: {exc}") from exc
        finally:
            if engine is not None:
                try:
                    engine.stop()
                except Exception:
                    pass
            with self._lock:
                if self._engine is engine:
                    self._engine = None
            if com_initialized and ole32 is not None:
                ole32.CoUninitialize()

    def _run_piper(self, text: str, generation: int, cancel: threading.Event) -> None:
        executable = Path(self.config.piper_executable).expanduser()
        model = Path(self.config.piper_model).expanduser()
        if not executable.is_file() or not model.is_file():
            raise SpeechOutputError(
                "Configure existing speech.piper_executable and speech.piper_model paths"
            )
        if os.name != "nt":
            raise SpeechOutputError("This lightweight Piper player currently supports Windows 11")
        command = [str(executable), "--model", str(model)]
        if self.config.piper_config:
            config_path = Path(self.config.piper_config).expanduser()
            if not config_path.is_file():
                raise SpeechOutputError("The configured Piper JSON config does not exist")
            command.extend(["--config", str(config_path)])
        process: subprocess.Popen[str] | None = None
        with tempfile.TemporaryDirectory(prefix="hinata-tts-") as directory:
            output = Path(directory) / "speech.wav"
            command.extend(["--output_file", str(output)])
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    shell=False,
                )
                with self._lock:
                    if cancel.is_set() or generation != self._generation:
                        process.terminate()
                        return
                    self._process = process
                stdout, stderr = process.communicate(text, timeout=120)
                if cancel.is_set() or generation != self._generation:
                    return
                if process.returncode != 0:
                    raise SpeechOutputError(
                        f"Piper exited with {process.returncode}: {(stderr or stdout).strip()[:1000]}"
                    )
                if not output.is_file():
                    raise SpeechOutputError("Piper did not create an audio file")
                import winsound

                winsound.PlaySound(str(output), winsound.SND_FILENAME)  # type: ignore[attr-defined]
            except subprocess.TimeoutExpired as exc:
                if process is not None:
                    process.kill()
                raise SpeechOutputError("Piper timed out after 120 seconds") from exc
            except OSError as exc:
                raise SpeechOutputError(f"Could not run Piper: {exc}") from exc
            finally:
                with self._lock:
                    if self._process is process:
                        self._process = None

    def stop(self) -> None:
        with self._lock:
            self._generation += 1
            self._cancel_event.set()
            engine = self._engine
            process = self._process
        if engine is not None:
            try:
                engine.stop()
            except Exception:
                pass
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        if os.name == "nt":
            try:
                import winsound

                winsound.PlaySound(None, 0)  # type: ignore[attr-defined]
            except (ImportError, RuntimeError):
                pass
        self._speaking.clear()

    def wait(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    @staticmethod
    def _notify(callback: StateCallback | None, state: str, error: str | None) -> None:
        if callback is not None:
            try:
                callback(state, error)
            except Exception:
                pass
