"""Interruptible TTS with soft natural woman voice selection (SAPI5, OneCore, Edge Neural, or Piper)."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from hinata.config import SpeechConfig

StateCallback = Callable[[str, str | None], None]

# Ranked female voice identifiers (highest softness/naturalness first)
SOFT_FEMALE_VOICE_RANK: tuple[tuple[str, int], ...] = (
    ("aria", 100),
    ("jenny", 98),
    ("ava", 96),
    ("neerja", 95),
    ("sonia", 94),
    ("natasha", 93),
    ("emma", 92),
    ("sara", 90),
    ("michelle", 89),
    ("ana", 88),
    ("eva", 86),
    ("kalpana", 84),
    ("veena", 83),
    ("zira", 82),
    ("hazel", 80),
    ("heera", 79),
    ("catherine", 77),
    ("susan", 76),
    ("samantha", 75),
    ("victoria", 74),
    ("karen", 73),
    ("moira", 72),
    ("tessa", 71),
    ("linda", 70),
    ("haruka", 68),
    ("huihui", 67),
    ("hortense", 66),
    ("hedda", 65),
    ("sabina", 64),
    ("paulina", 63),
    ("helena", 62),
    ("elsa", 61),
    ("female", 55),
    ("woman", 55),
)

MALE_VOICE_INDICATORS: tuple[str, ...] = (
    "david",
    "mark",
    "george",
    "james",
    "richard",
    "ravi",
    "hemant",
    "prabhat",
    "sean",
    "daniel",
    "alex",
    "fred",
    "paul",
    "claude",
    "stefan",
    "pablo",
    "ichiro",
    "kangkang",
    "guy",
    "christopher",
    "eric",
    "ryan",
)

LANGUAGE_HINTS: dict[str, tuple[str, ...]] = {
    "en": ("en-", "en_", "english", "united states", "great britain", "india"),
    "hi": ("hi-", "hi_", "hindi", "india", "kalpana", "heera", "neerja"),
    "fr": ("fr-", "fr_", "french", "hortense"),
    "de": ("de-", "de_", "german", "hedda"),
}


class SpeechOutputError(RuntimeError):
    pass


def select_preferred_voice(
    voices: Sequence[Any] | None,
    *,
    configured_voice: str = "",
    preferred_gender: str = "female",
    language: str = "en",
) -> Any | None:
    """Select an explicit configured voice, or auto-select a soft woman voice by default."""
    voice_list = list(voices or [])
    if configured_voice:
        needle = configured_voice.casefold()
        selected = next(
            (
                voice
                for voice in voice_list
                if needle in str(getattr(voice, "id", "")).casefold()
                or needle in str(getattr(voice, "name", "")).casefold()
            ),
            None,
        )
        if selected is None:
            raise SpeechOutputError(f"No installed offline voice matches '{configured_voice}'")
        return selected

    if not voice_list or preferred_gender == "any":
        return None

    lang_tokens = LANGUAGE_HINTS.get(language, ("en-", "en_", "english"))
    best_voice: Any | None = None
    best_score = 0

    for voice in voice_list:
        vid = str(getattr(voice, "id", "")).casefold()
        vname = str(getattr(voice, "name", "")).casefold()
        vgender = str(getattr(voice, "gender", "")).casefold()
        vlangs = " ".join(str(x).casefold() for x in (getattr(voice, "languages", None) or ()))
        combined = f"{vid} {vname} {vgender} {vlangs}"

        is_known_male = (
            vgender in {"male", "voicegendermale", "m"}
            or any(token in combined for token in MALE_VOICE_INDICATORS)
        )
        is_known_female = (
            vgender in {"female", "voicegenderfemale", "f"}
            or any(token in combined for token, _ in SOFT_FEMALE_VOICE_RANK)
        )

        score = 0
        if preferred_gender == "female":
            if is_known_male and not is_known_female:
                continue
            for token, weight in SOFT_FEMALE_VOICE_RANK:
                if token in combined:
                    score = max(score, weight)
            if vgender in {"female", "voicegenderfemale", "f"}:
                score = max(score, 60)
            # If a voice is neither explicitly male nor matched by name, prefer it slightly over David
            if score == 0 and not is_known_male and len(voice_list) > 1:
                score = 10
        elif preferred_gender == "male":
            if is_known_female and not is_known_male:
                continue
            if is_known_male:
                score = 75

        if score > 0 and any(tok in combined for tok in lang_tokens):
            score += 15

        if score > best_score:
            best_score = score
            best_voice = voice

    return best_voice


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
    elif config.tts_backend == "edge-tts":
        has_edge = importlib.util.find_spec("edge_tts") is not None
        has_pyttsx3 = importlib.util.find_spec("pyttsx3") is not None
        configured = bool(has_edge or has_pyttsx3)
        guidance = (
            f"Neural woman voice ({config.edge_tts_voice}) ready with offline SAPI fallback; run voice-output test to verify."
            if has_edge
            else (
                "edge-tts package not installed; falling back to offline female SAPI voice via pyttsx3."
                if has_pyttsx3
                else 'Install voice output: python -m pip install -e ".[tts]"'
            )
        )
    else:
        configured = importlib.util.find_spec("pyttsx3") is not None
        guidance = (
            f"pyttsx3 is installed (preferred voice: {config.tts_voice or config.tts_voice_gender}); run voice-output test to verify the actual audio device."
            if configured else 'Install offline voice output: python -m pip install -e ".[tts]"'
        )
    return {
        "backend": config.tts_backend,
        "voice_preference": config.tts_voice or config.tts_voice_gender,
        "backend_ready": configured,
        "guidance": guidance,
    }


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
        self.active_voice_name: str = ""

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
            elif self.config.tts_backend == "edge-tts":
                try:
                    self._run_edge_tts(text, generation, cancel)
                except Exception:
                    if cancel.is_set():
                        return
                    self._run_pyttsx3(text, generation, cancel)
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

    def _try_apply_onecore_female_voice(self, engine: Any) -> bool:
        """On Windows 11, check OneCore speech tokens (e.g. Aria/Eva/Zira/Heera) when SAPI5 only lists male voices."""
        if os.name != "nt":  # pragma: no cover - Windows OneCore COM path
            return False
        try:
            proxy = getattr(engine, "proxy", None)
            driver = getattr(proxy, "_driver", None)
            tts_com = getattr(driver, "_tts", None)
            if tts_com is None:
                return False
            import comtypes.client  # type: ignore[import-not-found]

            cat = comtypes.client.CreateObject("SAPI.SpObjectTokenCategory")
            cat.SetId(r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech_OneCore\Voices", False)
            tokens = cat.EnumerateTokens()
            candidates: list[tuple[int, Any, str]] = []
            for idx in range(tokens.Count):
                tok = tokens.Item(idx)
                desc = str(tok.GetDescription()).casefold()
                if any(m in desc for m in MALE_VOICE_INDICATORS):
                    continue
                score = 0
                for name_tok, weight in SOFT_FEMALE_VOICE_RANK:
                    if name_tok in desc:
                        score = max(score, weight)
                if score > 0:
                    candidates.append((score, tok, str(tok.GetDescription())))
            if candidates:
                candidates.sort(key=lambda item: item[0], reverse=True)
                _, chosen_tok, chosen_desc = candidates[0]
                tts_com.Voice = chosen_tok
                self.active_voice_name = chosen_desc
                return True
        except Exception:
            return False
        return False

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
            try:
                engine.setProperty("volume", float(self.config.tts_volume))
            except Exception:
                pass
            get_prop = getattr(engine, "getProperty", None)
            voices = get_prop("voices") if callable(get_prop) else None
            selected = select_preferred_voice(
                voices if isinstance(voices, Sequence) else None,
                configured_voice=self.config.tts_voice,
                preferred_gender=self.config.tts_voice_gender,
                language=self.config.language,
            )
            if selected is not None:
                engine.setProperty("voice", selected.id)
                self.active_voice_name = str(getattr(selected, "name", "") or getattr(selected, "id", ""))
            elif not self.config.tts_voice and self.config.tts_voice_gender == "female":
                self._try_apply_onecore_female_voice(engine)
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

    def _run_edge_tts(self, text: str, generation: int, cancel: threading.Event) -> None:
        """Optional ultra-soft Microsoft Neural Woman voice when edge-tts is installed and online."""
        if importlib.util.find_spec("edge_tts") is None:
            raise SpeechOutputError("edge_tts is not installed")
        if os.name != "nt":
            raise SpeechOutputError("Direct WAV/MP3 playback currently uses Windows multimedia")
        voice_name = self.config.edge_tts_voice or "en-US-AriaNeural"
        with tempfile.TemporaryDirectory(prefix="hinata-neural-tts-") as directory:
            out_mp3 = Path(directory) / "speech.mp3"
            cmd = [
                sys.executable,
                "-m",
                "edge_tts",
                "--voice",
                voice_name,
                "--rate=-5%",
                "--text",
                text,
                "--write-media",
                str(out_mp3),
            ]
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
            )
            with self._lock:
                if cancel.is_set() or generation != self._generation:
                    proc.terminate()
                    return
                self._process = proc
            try:
                proc.communicate(timeout=20)
                if cancel.is_set() or generation != self._generation:
                    return
                if proc.returncode != 0 or not out_mp3.is_file():
                    raise SpeechOutputError("edge-tts synthesis failed")
                self.active_voice_name = voice_name
                import ctypes

                winmm = ctypes.windll.winmm  # type: ignore[attr-defined]
                alias = f"hinata_tts_{generation}"
                winmm.mciSendStringW(f'open "{out_mp3}" type mpegvideo alias {alias}', None, 0, None)
                try:
                    winmm.mciSendStringW(f"play {alias} wait", None, 0, None)
                finally:
                    winmm.mciSendStringW(f"close {alias}", None, 0, None)
            finally:
                with self._lock:
                    if self._process is proc:
                        self._process = None

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
