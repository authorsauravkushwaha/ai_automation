"""Repair a half-installed HINATA before start.bat opens the desktop window.

start.bat calls this script on every launch after the first run. It verifies
the runtime pieces the app needs (GUI, offline speech, offline TTS, volume
control) and the cached Whisper/Vosk voice model, installs or downloads
whatever is missing, and only then reports success. Internet is used
exclusively for the parts that are actually missing.
"""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hinata.config import load_config  # noqa: E402  (needs ROOT on sys.path first)

# Importable modules that map to the extras setup.ps1 installs on first run.
CORE_MODULES = ("PySide6", "sounddevice", "pyttsx3", "pycaw")
BASE_INSTALL_EXTRAS = ("gui", "tts", "windows-audio")


def module_installed(name: str) -> bool:
    """True when an importable distribution for *name* is present."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        # find_spec can raise when a parent package exists but the child does
        # not; treat that exactly like a missing module.
        return False


def missing_components(config: Any = None) -> list[str]:
    """Names of runtime pieces required by the configured speech backend."""
    missing = [name for name in CORE_MODULES if not module_installed(name)]
    backend = getattr(getattr(config, "speech", None), "stt_backend", "faster-whisper")
    speech_module = "vosk" if backend == "vosk" else "faster_whisper"
    if not module_installed(speech_module):
        missing.append(f"{speech_module} (offline speech)")
    return missing


def speech_model_ready(config: Any) -> bool:
    """True when the configured offline STT model is fully cached locally."""
    speech = config.speech
    if speech.stt_backend == "vosk":
        return Path(speech.vosk_model_path or "").expanduser().is_dir()
    model = speech.whisper_model
    if not model:
        return True
    if Path(model).expanduser().is_dir():
        return True  # A local model directory in config needs no download.
    if not module_installed("huggingface_hub"):
        return False
    from huggingface_hub import try_to_load_from_cache

    repo = model if "/" in model else f"Systran/faster-whisper-{model}"
    # Require both the config and the weights so a half-finished download is
    # retried on the next launch instead of silently accepted.
    for filename in ("config.json", "model.bin"):
        cached = try_to_load_from_cache(repo, filename)
        if not cached or cached == "_no_exist":
            return False
    return True


def install_components(config: Any = None) -> int:
    """Install the configured GUI/speech/TTS/audio extras into this environment."""
    backend = getattr(getattr(config, "speech", None), "stt_backend", "faster-whisper")
    speech_extra = "light-speech" if backend == "vosk" else "speech"
    extras = (*BASE_INSTALL_EXTRAS, speech_extra)
    spec = ".[" + ",".join(extras) + "]"
    print(f"Installing the missing HINATA components: {spec}", flush=True)
    print("Internet is needed for this step; keep this window open.", flush=True)
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", spec],
        cwd=ROOT,
        check=False,
    )
    if result.returncode != 0:
        return int(result.returncode) or 1
    still_missing = missing_components(config)
    if still_missing:
        print(
            "Still missing after installation: " + ", ".join(still_missing),
            file=sys.stderr,
        )
        return 1
    return 0


def prepare_speech_model() -> int:
    """Download the offline voice model; non-zero only when the user opts out."""
    print(
        "The offline voice model is not cached yet; downloading it now "
        "(one-time setup download).",
        flush=True,
    )
    result = subprocess.run(
        [sys.executable, "-m", "hinata", "setup-speech"],
        cwd=ROOT,
        check=False,
    )
    if result.returncode == 0:
        return 0
    print(
        "The offline voice model could not be prepared (see the messages above).",
        file=sys.stderr,
    )
    try:
        answer = input("Open HINATA anyway without voice input? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "y"
    if answer in {"n", "no"}:
        return 1
    print("Continuing; run 'hinata setup-speech' later to enable voice input.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify and repair HINATA before the desktop window opens"
    )
    parser.add_argument(
        "--skip-model",
        action="store_true",
        help="Only verify/install Python components; leave the voice model alone",
    )
    args = parser.parse_args(argv)
    try:
        config: Any = load_config()
    except Exception:
        # A broken config.toml is reported by the app itself; never loop here.
        config = None
    missing = missing_components(config)
    if missing:
        print("Missing components detected: " + ", ".join(missing), flush=True)
        code = install_components(config)
        if code != 0:
            print("Component installation failed; HINATA was not opened.", file=sys.stderr)
            return code
        print("All components are installed.", flush=True)
    if config is not None and not args.skip_model and not speech_model_ready(config):
        code = prepare_speech_model()
        if code != 0:
            return code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
