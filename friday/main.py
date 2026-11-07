"""FRIDAY terminal entry point and low-memory interactive shell."""

from __future__ import annotations

import argparse
import getpass
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from friday import __version__
from friday.automation.system_info import SystemInfo
from friday.config import ConfigError, load_config
from friday.core.assistant import FridayAssistant
from friday.core.types import AssistantResponse

EXAMPLES = """Structured commands (work without an LLM):
  open notepad                         close notepad
  open folder Downloads                open file Documents\\notes.txt
  create folder Projects               read text file Documents\\notes.txt
  find files for report in Downloads   type "Text for the active app"
  volume up / volume down / mute       set volume to 35
  system info                          remember that my editor is VS Code
  list preferences                     search memory for editor
  clear conversations                  clear memory
Anything else is sent to the configured loopback Ollama model, if enabled."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="friday", description="Private, offline-first Windows 11 personal assistant"
    )
    parser.add_argument("--config", help="Path to config.toml")
    parser.add_argument("--no-llm", action="store_true", help="Use deterministic tools only")
    parser.add_argument("--speak", action="store_true", help="Speak final responses offline")
    parser.add_argument("--version", action="version", version=f"FRIDAY {__version__}")
    commands = parser.add_subparsers(dest="command")

    ask = commands.add_parser("ask", help="Process one typed request")
    ask.add_argument("request", nargs="+", help="Natural-language or structured request")

    listen = commands.add_parser("listen", help="Record and process one voice request")
    listen.add_argument("--duration", type=float, default=5.0, help="Seconds to record (0.5..30)")

    enroll = commands.add_parser("enroll", help="Enroll the owner using in-memory voice samples")
    enroll.add_argument("--samples", type=int, default=3, choices=range(3, 9), metavar="3..8")
    enroll.add_argument("--duration", type=float, default=4.0, help="Seconds per sample (2..15)")

    commands.add_parser("delete-voice-profile", help="Delete local owner voice embeddings")
    commands.add_parser("setup-pin", help="Set or change the PIN for sensitive actions")
    commands.add_parser("devices", help="List available input microphones")
    commands.add_parser("setup-speech", help="Prepare the configured offline STT model")
    commands.add_parser("gui", help="Start the PySide6 desktop interface")
    wake = commands.add_parser("wake", help="Explicitly listen for a custom Hey FRIDAY wake model")
    wake.add_argument("--duration", type=float, default=5.0, help="Seconds to record after detection")

    status = commands.add_parser("status", help="Show local model and feature status as JSON")
    status.add_argument("--no-model-check", action="store_true")
    commands.add_parser("doctor", help="Diagnose resources, local runtime, and optional dependencies")

    model = commands.add_parser("model", help="Inspect or download the configured Ollama model")
    model.add_argument("operation", choices=["status", "pull"])

    memory = commands.add_parser("memory", help="Inspect, export, or delete local memory")
    operation = memory.add_mutually_exclusive_group(required=True)
    operation.add_argument("--recent", type=int, metavar="N")
    operation.add_argument("--search", metavar="QUERY")
    operation.add_argument("--export", metavar="PATH")
    operation.add_argument("--clear", choices=["conversations", "commands", "preferences", "all"])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    assistant: FridayAssistant | None = None
    try:
        config = load_config(args.config)
        if args.command == "model" and args.operation == "pull":
            return pull_model(config.ollama.model)
        assistant = FridayAssistant(config, llm_enabled=False if args.no_llm else None)
        if args.command == "ask":
            return emit_with_approvals(
                assistant, assistant.process_text(" ".join(args.request)), speak=args.speak
            )
        if args.command == "listen":
            voice, response = assistant.process_voice(args.duration)
            print(f"You (voice/{voice.speaker_status}): {voice.text}")
            return emit_with_approvals(assistant, response, voice=voice, speak=args.speak)
        if args.command == "gui":
            try:
                from friday.interface.main_window import run_gui
            except ImportError as exc:
                print(
                    "Desktop dependencies are missing. Run: python -m pip install -e \".[gui]\"\n"
                    f"Details: {exc}",
                    file=sys.stderr,
                )
                return 2
            return run_gui(assistant)
        if args.command == "wake":
            return wake_loop(assistant, args.duration)
        if args.command == "status":
            print(json.dumps(assistant.status(check_model=not args.no_model_check), indent=2))
            return 0
        if args.command == "doctor":
            return doctor(assistant)
        if args.command == "devices":
            devices = assistant.microphone.list_devices()
            print(json.dumps(devices, indent=2) if devices else "No input microphones found.")
            return 0
        if args.command == "setup-speech":
            print("Preparing offline speech recognition. The first model download may use internet.")
            print(assistant.stt.prepare())
            return 0
        if args.command == "setup-pin":
            return setup_pin(assistant)
        if args.command == "enroll":
            return enroll(assistant, args.samples, args.duration)
        if args.command == "delete-voice-profile":
            return delete_voice_profile(assistant)
        if args.command == "model":
            print(json.dumps(assistant.llm.status(), indent=2))
            return 0
        if args.command == "memory":
            return memory_command(assistant, args)
        return interactive(assistant, speak=args.speak)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130
    except (ConfigError, RuntimeError, OSError, ValueError, sqlite3.Error) as exc:
        print(f"FRIDAY: {exc}", file=sys.stderr)
        return 1
    finally:
        if assistant is not None:
            assistant.close()


def emit_with_approvals(
    assistant: FridayAssistant,
    response: AssistantResponse,
    *,
    voice: Any = None,
    speak: bool = False,
) -> int:
    source = "voice" if voice is not None else "text"
    while True:
        print(f"FRIDAY: {response.text}")
        if response.needs_confirmation and response.action is not None:
            try:
                answer = input("Proceed? Type yes to confirm [y/N]: ").strip().lower()
            except EOFError:
                print("\nFRIDAY: Action cancelled; no confirmation input was available.")
                return 1
            if answer not in {"y", "yes"}:
                print("FRIDAY: Action cancelled; nothing was executed.")
                return 1
            response = assistant.resume(
                response.action,
                source=source,
                speaker_match=getattr(voice, "speaker_match", None),
                speaker_required=getattr(voice, "speaker_required", False),
            )
            continue
        if response.needs_pin and response.action is not None:
            try:
                pin = getpass.getpass("FRIDAY PIN: ")
            except EOFError:
                print("\nFRIDAY: Action cancelled; no PIN input was available.")
                return 1
            response = assistant.resume(
                response.action,
                source=source,
                speaker_match=getattr(voice, "speaker_match", None),
                speaker_required=getattr(voice, "speaker_required", False),
                pin=pin,
            )
            pin = ""  # Do not retain the PIN in local variables longer than necessary.
            continue
        if speak:
            assistant.tts.speak(response.text)
        return 0 if response.success else 1


def interactive(assistant: FridayAssistant, *, speak: bool = False) -> int:
    print(f"FRIDAY {__version__} — local assistant. Type /help for commands, /quit to exit.")
    if not assistant.authentication.is_configured:
        print("Setup recommended: run 'friday setup-pin' before sensitive actions or enrollment.")
    while True:
        try:
            text = input("\nYou: ").strip()
        except EOFError:
            print()
            return 0
        if not text:
            continue
        if text in {"/quit", "/exit"}:
            return 0
        if text == "/help":
            print(EXAMPLES)
            print("\nShell: /voice /speak /stop /status /memory /pin /forget /quit")
            continue
        if text == "/status":
            print(json.dumps(assistant.status(), indent=2))
            continue
        if text == "/memory":
            for row in assistant.memory.recent_messages(20):
                print(f"{row['created_at']} {row['role']}: {row['content']}")
            continue
        if text == "/pin":
            setup_pin(assistant)
            continue
        if text == "/forget":
            emit_with_approvals(assistant, assistant.process_text("clear memory"))
            continue
        if text == "/voice":
            try:
                voice, response = assistant.process_voice()
                print(f"You (voice/{voice.speaker_status}): {voice.text}")
                emit_with_approvals(assistant, response, voice=voice, speak=speak)
            except (RuntimeError, OSError, ValueError) as exc:
                print(f"FRIDAY: Voice request failed safely: {exc}")
            continue
        if text == "/speak":
            speak = not speak
            print(f"Offline speech output {'enabled' if speak else 'disabled'}.")
            continue
        if text == "/stop":
            assistant.tts.stop()
            if assistant.microphone.recording:
                assistant.microphone.cancel()
            print("Listening and speech stopped.")
            continue
        emit_with_approvals(assistant, assistant.process_text(text), speak=speak)


def wake_loop(assistant: FridayAssistant, duration: float) -> int:
    from friday.speech.wake_word import WakeWordService

    if not assistant.config.wake_word.enabled:
        print("Enable [wake_word] and configure a custom Hey FRIDAY ONNX model first.", file=sys.stderr)
        return 1
    service = WakeWordService(assistant.wake_word, assistant.config.speech.microphone)
    detected = threading.Event()

    def on_detection(score: float) -> None:
        print(f"Hey FRIDAY detected (confidence {score:.2f}).")
        detected.set()

    print("Wake-word mode active. Press Ctrl+C to stop; microphone audio stays local.")
    try:
        service.start(on_detection)
        while True:
            if service.last_error:
                raise RuntimeError(service.last_error)
            if not detected.wait(0.25):
                continue
            detected.clear()
            service.stop()  # Release the microphone before recording/transcribing the command.
            detected.clear()
            voice, response = assistant.process_voice(duration)
            print(f"You (voice/{voice.speaker_status}): {voice.text}")
            emit_with_approvals(assistant, response, voice=voice)
            print("Listening for Hey FRIDAY again…")
            service.start(on_detection)
    except KeyboardInterrupt:
        print("\nWake-word mode stopped.")
        return 0
    finally:
        service.stop()


def setup_pin(assistant: FridayAssistant) -> int:
    if assistant.authentication.is_configured:
        current = getpass.getpass("Current PIN: ")
        if not assistant.authentication.verify_pin(current):
            print("The current PIN is incorrect.", file=sys.stderr)
            return 1
    first = getpass.getpass("New PIN (4-12 digits): ")
    second = getpass.getpass("Repeat new PIN: ")
    if first != second:
        print("PINs do not match; nothing was changed.", file=sys.stderr)
        return 1
    assistant.authentication.set_pin(first)
    print("PIN updated. It is stored only as a salted PBKDF2 hash.")
    return 0


def enroll(assistant: FridayAssistant, sample_count: int, duration: float) -> int:
    if not assistant.authentication.is_configured:
        print("Run 'friday setup-pin' before enrolling a biometric voice profile.", file=sys.stderr)
        return 1
    pin = getpass.getpass("Confirm FRIDAY PIN for voice enrollment: ")
    if not assistant.authentication.verify_pin(pin):
        print("PIN verification failed; enrollment was not changed.", file=sys.stderr)
        return 1
    if not 2 <= duration <= 15:
        print("Enrollment sample duration must be between 2 and 15 seconds.", file=sys.stderr)
        return 1
    if assistant.speaker.is_enrolled and input("Replace the existing voice profile? [y/N]: ").lower() not in {"y", "yes"}:
        print("Enrollment cancelled; the existing profile was retained.")
        return 1
    samples = []
    try:
        for index in range(sample_count):
            input(f"Sample {index + 1}/{sample_count}: press Enter, then speak naturally for {duration:g}s.")
            print("Recording…")
            samples.append(assistant.microphone.record(duration))
            print("Captured. Raw audio will be discarded after embedding.")
        result = assistant.enroll_owner(samples)
    finally:
        samples.clear()
    print(f"Voice profile enrolled from {result['samples']} samples. No raw recordings were saved.")
    print("Voice matching is probabilistic; your PIN is still required for sensitive actions.")
    return 0


def delete_voice_profile(assistant: FridayAssistant) -> int:
    if input("Type DELETE VOICE to remove the enrolled profile: ").strip() != "DELETE VOICE":
        print("Deletion cancelled.")
        return 1
    if assistant.authentication.is_configured:
        if not assistant.authentication.verify_pin(getpass.getpass("FRIDAY PIN: ")):
            print("PIN verification failed.", file=sys.stderr)
            return 1
    deleted = assistant.speaker.delete_profile()
    print("Voice profile deleted." if deleted else "No voice profile was enrolled.")
    return 0


def memory_command(assistant: FridayAssistant, args: argparse.Namespace) -> int:
    if args.recent is not None:
        rows = assistant.memory.recent_messages(args.recent)
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if args.search is not None:
        print(json.dumps(assistant.memory.search(args.search), indent=2, ensure_ascii=False))
        return 0
    if args.export is not None:
        destination = assistant.database.export_json(Path(args.export))
        print(f"Memory exported without settings or PIN material: {destination}")
        return 0
    command = "clear memory" if args.clear == "all" else f"clear {args.clear}"
    return emit_with_approvals(assistant, assistant.process_text(command))


def pull_model(model: str) -> int:
    executable = shutil.which("ollama")
    if executable is None:
        print("Ollama is not installed. See https://ollama.com/download/windows", file=sys.stderr)
        return 1
    print(f"Downloading {model} through local Ollama. Internet is required for this setup step only.")
    try:
        result = subprocess.run([executable, "pull", model], shell=False, check=False, timeout=1800)
    except subprocess.TimeoutExpired:
        print("Model download timed out; run 'friday model pull' again to resume.", file=sys.stderr)
        return 1
    return int(result.returncode)


def doctor(assistant: FridayAssistant) -> int:
    print(f"FRIDAY {__version__} diagnostic")
    print(f"Platform: {os.name}; Python: {sys.version.split()[0]}")
    if os.name != "nt":
        print("WARNING: Windows app, volume, typing, SAPI, and desktop tests require Windows 11.")
    print("System:", json.dumps(SystemInfo().snapshot()))
    model = assistant.llm.status()
    print("Ollama:", "available" if model["available"] else f"unavailable ({model.get('error', '')})")
    print("Configured model:", assistant.config.ollama.model, "installed:", model["installed"])
    for package in ("PySide6", "faster_whisper", "sounddevice", "pyttsx3", "resemblyzer", "openwakeword"):
        print(f"Optional {package}:", "installed" if importlib.util.find_spec(package) else "not installed")
    print("Speaker enrolled:", assistant.speaker.is_enrolled)
    print("PIN configured:", assistant.authentication.is_configured)
    print("Permitted roots:", ", ".join(str(path) for path in assistant.config.security.allowed_roots))
    print("Data:", assistant.config.data_dir)
    return 0 if model["available"] and model["installed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
