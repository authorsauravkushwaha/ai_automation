"""HINATA terminal entry point and low-memory interactive shell."""

from __future__ import annotations

import argparse
import getpass
import importlib
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

from hinata import __version__
from hinata.automation.system_info import SystemInfo
from hinata.config import ConfigError, load_config
from hinata.core.assistant import HinataAssistant
from hinata.core.ollama_service import OllamaService
from hinata.core.types import AssistantResponse

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
        prog="hinata", description="Private, offline-first Windows 11 personal assistant"
    )
    parser.add_argument("--config", help="Path to config.toml")
    parser.add_argument("--no-llm", action="store_true", help="Use deterministic tools only")
    parser.add_argument("--speak", action="store_true", help="Speak final responses offline")
    parser.add_argument("--version", action="version", version=f"HINATA {__version__}")
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
    wake = commands.add_parser("wake", help="Explicitly listen for a custom Hey HINATA wake model")
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
    assistant: HinataAssistant | None = None
    try:
        config = load_config(args.config)
        if args.command == "model" and args.operation == "pull":
            return pull_model(config.ollama.model)
        assistant = HinataAssistant(config, llm_enabled=False if args.no_llm else None)
        if args.command == "ask":
            return emit_with_approvals(
                assistant, assistant.process_text(" ".join(args.request)), speak=args.speak, wait_for_speech=True
            )
        if args.command == "listen":
            voice, response = assistant.process_voice(args.duration)
            print(f"You (voice/{voice.speaker_status}): {voice.text}")
            return emit_with_approvals(assistant, response, voice=voice, speak=args.speak, wait_for_speech=True)
        if args.command == "gui":
            try:
                from hinata.interface.main_window import run_gui
            except (ImportError, OSError) as exc:
                print(desktop_import_error(exc), file=sys.stderr)
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
            # status: connect (starting the service if needed) so the report is real.
            report = assistant.llm.ensure_connected()
            print(json.dumps({**report, "tags": assistant.llm.status()}, indent=2))
            return 0
        if args.command == "memory":
            return memory_command(assistant, args)
        return interactive(assistant, speak=args.speak)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130
    except (ConfigError, RuntimeError, OSError, ValueError, sqlite3.Error) as exc:
        print(f"HINATA: {exc}", file=sys.stderr)
        return 1
    finally:
        if assistant is not None:
            assistant.close()


def desktop_import_error(exc: ImportError | OSError) -> str:
    """Distinguish a missing Python package from a missing native Qt dependency."""
    if isinstance(exc, ModuleNotFoundError) and exc.name and (
        exc.name == "PySide6" or exc.name.startswith("PySide6.")
    ):
        guidance = 'Install the GUI package: python -m pip install -e ".[gui]"'
    elif sys.platform.startswith("linux") and "libGL.so.1" in str(exc):
        guidance = (
            "Qt is installed but its system OpenGL library is missing. On Debian or Ubuntu, "
            "install it with: sudo apt-get install libgl1. Other Linux distributions may use "
            "a different package name."
        )
    else:
        guidance = (
            "The GUI package or one of its native dependencies could not be loaded. "
            "Check the details below and your operating system's Qt/runtime libraries. "
            'If PySide6 is not installed, run: python -m pip install -e ".[gui]"'
        )
    return f"Desktop unavailable. {guidance}\nDetails: {exc}"


def desktop_status() -> str:
    """Report import readiness, not just whether package metadata exists."""
    if importlib.util.find_spec("PySide6") is None:
        return "not installed"
    try:
        importlib.import_module("PySide6.QtWidgets")
    except (ImportError, OSError) as exc:
        return f"installed, but QtWidgets could not load: {exc}"
    return "installed (QtWidgets import succeeded; display not tested)"


def emit_with_approvals(
    assistant: HinataAssistant,
    response: AssistantResponse,
    *,
    voice: Any = None,
    speak: bool = False,
    wait_for_speech: bool = False,
) -> int:
    source = "voice" if voice is not None else "text"
    while True:
        print(f"HINATA: {response.text}")
        if response.needs_plan_confirmation:
            try:
                answer = input("Approve this entire plan? Type yes to confirm [y/N]: ").strip().lower()
            except EOFError:
                print("\nHINATA: Plan cancelled; no steps were executed.")
                return 1
            if answer not in {"y", "yes"}:
                print("HINATA: Plan cancelled; no steps were executed.")
                return 1
            response = assistant.execute_plan(
                response.plan_actions,
                source=source,
                speaker_match=getattr(voice, "speaker_match", None),
                speaker_required=getattr(voice, "speaker_required", False),
            )
            continue
        if response.needs_confirmation and response.action is not None:
            print(f"Tool: {response.action.tool}\nArguments: "
                  f"{json.dumps(response.action.arguments, ensure_ascii=True, sort_keys=True)}")
            try:
                answer = input("Proceed? Type yes to confirm [y/N]: ").strip().lower()
            except EOFError:
                print("\nHINATA: Action cancelled; no confirmation input was available.")
                return 1
            if answer not in {"y", "yes"}:
                print("HINATA: Action cancelled; nothing was executed.")
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
                pin = getpass.getpass("HINATA PIN: ")
            except EOFError:
                print("\nHINATA: Action cancelled; no PIN input was available.")
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
            if wait_for_speech:
                assistant.tts.wait()
                if assistant.tts.last_error:
                    print(f"HINATA: Speech output failed: {assistant.tts.last_error}", file=sys.stderr)
                    return 1
        return 0 if response.success else 1


def connect_local_model(assistant: HinataAssistant) -> None:
    """Best-effort: start the loopback Ollama service before the first request."""
    if not assistant.orchestrator.llm_enabled:
        return
    report = assistant.llm.ensure_connected()
    state = report.get("state")
    if state == "ready":
        print(f"Local model ready: {report.get('model')}")
    elif state == "model_missing":
        print(
            f"Ollama is running but {assistant.config.ollama.model} is not downloaded. "
            "Run 'hinata model pull', or set ollama.auto_pull = true in config.toml."
        )
    elif state == "not_installed":
        print("Ollama is not installed; structured commands still work. See https://ollama.com/download/windows")
    elif state == "disabled":
        print("Local model is disabled in config.toml; structured commands still work.")
    else:
        print("Ollama is not responding; structured commands still work. Run 'hinata doctor'.")


def interactive(assistant: HinataAssistant, *, speak: bool = False) -> int:
    print(f"HINATA {__version__} — local assistant. Type /help for commands, /quit to exit.")
    connect_local_model(assistant)
    if not assistant.authentication.is_configured:
        print("Setup recommended: run 'hinata setup-pin' before sensitive actions or enrollment.")
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
            print("\nShell: /voice /speak /stop /reconnect /status /memory /pin /forget /quit")
            continue
        if text == "/reconnect":
            report = assistant.orchestrator.recheck_model(
                download_missing=assistant.config.ollama.auto_pull
            )
            print(f"Ollama connection: {report.get('state')} {report.get('guidance') or ''}".strip())
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
                print(f"HINATA: Voice request failed safely: {exc}")
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


def wake_loop(assistant: HinataAssistant, duration: float) -> int:
    if not assistant.config.wake_word.enabled:
        print("Enable [wake_word] and configure a custom Hey HINATA ONNX model first.", file=sys.stderr)
        return 1
    service = assistant.wake_service
    detected = threading.Event()

    def on_detection(score: float) -> None:
        print(f"Hey HINATA detected (confidence {score:.2f}).")
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
            print("Listening for Hey HINATA again…")
            service.start(on_detection)
    except KeyboardInterrupt:
        print("\nWake-word mode stopped.")
        return 0
    finally:
        service.stop()


def setup_pin(assistant: HinataAssistant) -> int:
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


def enroll(assistant: HinataAssistant, sample_count: int, duration: float) -> int:
    if not assistant.authentication.is_configured:
        print("Run 'hinata setup-pin' before enrolling a biometric voice profile.", file=sys.stderr)
        return 1
    pin = getpass.getpass("Confirm HINATA PIN for voice enrollment: ")
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


def delete_voice_profile(assistant: HinataAssistant) -> int:
    if input("Type DELETE VOICE to remove the enrolled profile: ").strip() != "DELETE VOICE":
        print("Deletion cancelled.")
        return 1
    if assistant.authentication.is_configured:
        if not assistant.authentication.verify_pin(getpass.getpass("HINATA PIN: ")):
            print("PIN verification failed.", file=sys.stderr)
            return 1
    deleted = assistant.speaker.delete_profile()
    print("Voice profile deleted." if deleted else "No voice profile was enrolled.")
    return 0


def memory_command(assistant: HinataAssistant, args: argparse.Namespace) -> int:
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
    # Start the local service first so a fresh install can pull immediately.
    config = load_config()
    config.ollama.model = model
    OllamaService(config.ollama).ensure_running()
    try:
        result = subprocess.run([executable, "pull", model], shell=False, check=False, timeout=1800)
    except subprocess.TimeoutExpired:
        print("Model download timed out; run 'hinata model pull' again to resume.", file=sys.stderr)
        return 1
    return int(result.returncode)


def doctor(assistant: HinataAssistant) -> int:
    print(f"HINATA {__version__} diagnostic")
    print(f"Platform: {os.name}; Python: {sys.version.split()[0]}")
    if os.name != "nt":
        print("WARNING: Windows app, volume, typing, SAPI, and desktop tests require Windows 11.")
    print("System:", json.dumps(SystemInfo().snapshot()))
    connection = assistant.llm.connect_status()
    print("Ollama connection:", connection.get("state"), "-", connection.get("guidance") or "ok")
    model = assistant.llm.status()
    print("Ollama:", "available" if model["available"] else f"unavailable ({model.get('error', '')})")
    print("Configured model:", assistant.config.ollama.model, "installed:", model["installed"])
    print("Optional PySide6:", desktop_status())
    for package in ("faster_whisper", "sounddevice", "pyttsx3", "resemblyzer", "openwakeword"):
        print(f"Optional {package}:", "installed" if importlib.util.find_spec(package) else "not installed")
    print("Speaker enrolled:", assistant.speaker.is_enrolled)
    print("PIN configured:", assistant.authentication.is_configured)
    print("Permitted roots:", ", ".join(str(path) for path in assistant.config.security.allowed_roots))
    print("Data:", assistant.config.data_dir)
    return 0 if model["available"] and model["installed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
