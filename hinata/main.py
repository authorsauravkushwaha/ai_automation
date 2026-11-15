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
from urllib.parse import urlsplit

from hinata import __version__
from hinata.automation.system_info import SystemInfo
from hinata.config import ConfigError, OllamaConfig, load_config
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
  inspect code Documents\\script.py
  index document Documents\\notes.txt  search knowledge for project
  sync knowledge folder Documents      list knowledge
  semantic index document Documents\\notes.txt
  semantic search for project          ask knowledge what is the project?
  remove document from index Documents\\notes.txt
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

    model = commands.add_parser("model", help="Inspect, profile, or download configured Ollama models")
    model.add_argument("operation", choices=["status", "pull", "hardware", "benchmark", "fleet"])
    model.add_argument("--fleet", action="store_true", help="Pull all models in the detected Lenovo LOQ i5 fleet (large setup download)")

    commands.add_parser("agents", help="Show registered specialist agents, manifests, and recent activity")
    tasks = commands.add_parser("tasks", help="Inspect saved plan checkpoints (never auto-replays execution)")
    tasks.add_argument("id", nargs="?", help="Task ID; omit to list recent tasks")
    task_action = tasks.add_mutually_exclusive_group()
    task_action.add_argument("--cancel", action="store_true", help="Cancel an unapproved plan by ID")
    task_action.add_argument("--verify", action="store_true", help="Review an executed task and record observed evidence")
    task_action.add_argument("--reject", action="store_true", help="Record that an executed task did not meet its objective")
    task_action.add_argument("--pause", action="store_true", help="Pause a task at a safe non-RUNNING boundary")
    task_action.add_argument("--resume-paused", action="store_true", help="Resume a PAUSED task if all prior outcomes are known")
    task_action.add_argument("--reconcile", action="store_true", help="Run read-only crash reconciliation without replaying actions")
    task_action.add_argument("--timeline", action="store_true", help="Inspect durable trace timeline for a task")

    workspaces = commands.add_parser("workspaces", help="Manage approved coding workspaces and inspect patches")
    ws_op = workspaces.add_mutually_exclusive_group()
    ws_op.add_argument("--register", metavar="PATH", help="Approve and register a local coding workspace directory")
    ws_op.add_argument("--patches", action="store_true", help="List proposed, applied, and reverted coding patches")
    ws_op.add_argument("--symbols", action="store_true", help="Build Python AST symbol & call-reference map")
    ws_op.add_argument("--probe-sandbox", action="store_true", help="Verify sandbox network-disabled isolation")
    workspaces.add_argument("--workspace", default="", help="Workspace ID or name filter")
    workspaces.add_argument("--query", default="", help="Optional symbol search filter")

    assets = commands.add_parser("assets", help="Inspect, verify, export, or import air-gapped offline bundles")
    asset_op = assets.add_mutually_exclusive_group()
    asset_op.add_argument("--verify", action="store_true", help="Verify SHA-256 digests of registered local assets")
    asset_op.add_argument("--export-bundle", metavar="TAR_GZ", help="Export registered local assets to an offline bundle")
    asset_op.add_argument("--import-bundle", metavar="TAR_GZ", help="Import and SHA-256 verify an offline bundle")

    audit = commands.add_parser("audit", help="Inspect, cryptographically verify, or export audit traces")
    audit_op = audit.add_mutually_exclusive_group()
    audit_op.add_argument("--export", metavar="PATH", help="Write redacted audit JSON to file")
    audit_op.add_argument("--verify-chain", action="store_true", help="Verify SHA-256 tamper-evident hash chain")

    playbooks = commands.add_parser("playbooks", help="Manage reusable procedural workflow playbooks")
    pb_op = playbooks.add_mutually_exclusive_group()
    pb_op.add_argument("--promote", metavar="TASK_ID", help="Promote a verified COMPLETED task into a playbook")
    pb_op.add_argument("--instantiate", metavar="NAME_OR_ID", help="Stage an AWAITING_APPROVAL task from a playbook")
    pb_op.add_argument("--delete", metavar="NAME_OR_ID", help="Delete a saved playbook")
    playbooks.add_argument("--name", default="", help="Playbook name when using --promote")
    playbooks.add_argument("--description", default="", help="Optional playbook description")
    playbooks.add_argument("--param", action="append", default=[], metavar="KEY=VALUE", help="Parameter binding")

    commands.add_parser("briefing", help="J.A.R.V.I.S. tactical situational awareness briefing")

    override = commands.add_parser(
        "override",
        help="Owner tactical protocols: Red Alert Lockdown & PIN-unlocked Commander Lease",
    )
    ov_op = override.add_mutually_exclusive_group()
    ov_op.add_argument("--status", action="store_true", help="Show current tactical posture")
    ov_op.add_argument("--lockdown", action="store_true", help="Immediately freeze all non-status tools")
    ov_op.add_argument("--unlock", action="store_true", help="Lift Red Alert Lockdown (requires PIN)")
    ov_op.add_argument("--commander", type=int, metavar="MINUTES", help="Activate timed Commander Lease (1..30 min, requires PIN)")
    ov_op.add_argument("--stand-down", action="store_true", help="Revoke active Commander Lease immediately")
    override.add_argument("--reason", default="Owner engaged Red Alert Lockdown", help="Lockdown reason note")

    schedule = commands.add_parser("schedule", help="Manage bounded offline-only autonomous background jobs")
    sc_op = schedule.add_mutually_exclusive_group()
    sc_op.add_argument("--add", metavar="NAME", help="Schedule a recurring bounded offline job")
    sc_op.add_argument("--tick", action="store_true", help="Execute one bounded scheduler tick now")
    sc_op.add_argument("--disable", metavar="JOB_ID", help="Disable a scheduled job")
    schedule.add_argument("--request", default="executive briefing", help="Command/request for --add")
    schedule.add_argument("--interval", type=int, default=300, help="Interval in seconds for --add")
    schedule.add_argument("--max-runs", type=int, default=10, help="Bounded run cap for --add")

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
            if args.fleet:
                from hinata.core.jarvis_models import select_optimal_fleet_pack
                from hinata.core.providers import HardwareCapabilityService
                pack = select_optimal_fleet_pack(HardwareCapabilityService().detect())
                for name in pack.all_ollama_models():
                    if pull_model(name, config.ollama) != 0:
                        return 1
                return 0
            return pull_model(config.ollama.model, config.ollama)
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
            if args.operation == "fleet":
                from hinata.core.jarvis_models import (
                    render_fleet_toml_snippet,
                    select_optimal_fleet_pack,
                )
                pack = select_optimal_fleet_pack(assistant.hardware.detect())
                print(json.dumps(pack.to_dict(), indent=2))
                print("\n# Paste these [ollama] values into config.toml (replace existing [ollama] section):")
                print(render_fleet_toml_snippet(pack))
                return 0
            if args.operation == "hardware":
                print(json.dumps(assistant.hardware.detect(), indent=2))
                return 0
            if args.operation == "benchmark":
                print(json.dumps(assistant.llm.benchmark(), indent=2))
                return 0
            # status: connect (starting the service if needed) so the report is real.
            report = assistant.llm.ensure_connected()
            print(json.dumps({**report, "tags": assistant.llm.status()}, indent=2))
            return 0
        if args.command == "agents":
            from hinata.agents.workers import WORKERS
            print(
                json.dumps(
                    {
                        "workers": {name: sorted(worker.allowed_tools) for name, worker in WORKERS.items()},
                        "specialist_manifests": assistant.agent_registry.list_manifests(),
                        "agent_health": assistant.agent_registry.health_snapshot(),
                        "planning": "proposal-only bounded replanner & DAG graph validator",
                        "review": "read-only postcondition checks and human attestation",
                        "recent_events": assistant.orchestrator.workers.recent(),
                    },
                    indent=2,
                )
            )
            return 0
        if args.command == "workspaces":
            if args.register:
                print(json.dumps(assistant.workspaces.register_workspace(args.register), indent=2))
                return 0
            if args.probe_sandbox:
                print(
                    json.dumps(
                        assistant.workspaces.sandbox.verify_network_blocked(assistant.config.data_dir),
                        indent=2,
                    )
                )
                return 0
            if args.patches:
                rows = assistant.database.fetch_all(
                    "SELECT patch_id, workspace_id, title, state, created_at FROM coding_patches ORDER BY created_at DESC LIMIT 50"
                )
                print(json.dumps(rows, indent=2))
                return 0
            if args.symbols:
                print(
                    json.dumps(
                        assistant.workspaces.ast_symbol_index(args.workspace, query=args.query),
                        indent=2,
                        ensure_ascii=False,
                    )
                )
                return 0
            print(json.dumps(assistant.workspaces.list_workspaces(), indent=2))
            return 0
        if args.command == "assets":
            if args.verify:
                report = assistant.assets.verify_all()
                print(json.dumps(report, indent=2))
                return 0 if report["all_valid"] else 1
            if args.export_bundle:
                print(json.dumps(assistant.assets.export_bundle(args.export_bundle), indent=2))
                return 0
            if args.import_bundle:
                print(json.dumps(assistant.assets.import_bundle(args.import_bundle), indent=2))
                return 0
            print(json.dumps(assistant.assets.readiness_inventory(), indent=2))
            return 0
        if args.command == "audit":
            if args.verify_chain:
                report = assistant.tracer.verify_integrity()
                print(json.dumps(report, indent=2, ensure_ascii=False))
                return 0 if report["intact"] else 1
            payload = assistant.tracer.export_audit()
            if args.export:
                out_p = Path(args.export).expanduser().resolve()
                out_p.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
                print(f"Redacted audit report saved to: {out_p}")
                return 0
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 0
        if args.command == "playbooks":
            kv_params: dict[str, str] = {}
            for pair in args.param:
                if "=" not in pair:
                    print(f"Invalid --param '{pair}'; expected KEY=VALUE.", file=sys.stderr)
                    return 1
                k, v = pair.split("=", 1)
                kv_params[k.strip()] = v
            if args.promote:
                if not args.name:
                    print("--name is required when promoting a task to a playbook.", file=sys.stderr)
                    return 1
                created = assistant.playbooks.promote_from_task(
                    args.promote,
                    name=args.name,
                    description=args.description,
                    parameter_bindings=kv_params or None,
                )
                print(json.dumps(created, indent=2, ensure_ascii=False))
                return 0
            if args.instantiate:
                task_id, plan = assistant.playbooks.instantiate(args.instantiate, params=kv_params)
                print(
                    json.dumps(
                        {
                            "task_id": task_id,
                            "state": "AWAITING_APPROVAL",
                            "plan_hash": plan.plan_hash(),
                            "preview": plan.preview(),
                        },
                        indent=2,
                        ensure_ascii=False,
                    )
                )
                return 0
            if args.delete:
                ok = assistant.playbooks.delete_playbook(args.delete)
                print("Playbook deleted." if ok else "Playbook not found.")
                return 0 if ok else 1
            print(json.dumps(assistant.playbooks.list_playbooks(), indent=2, ensure_ascii=False))
            return 0
        if args.command == "briefing":
            resp = assistant.process_text("executive briefing")
            print(resp.text)
            if args.speak:
                assistant.tts.speak(resp.text)
                assistant.tts.wait()
            return 0 if resp.success else 1
        if args.command == "override":
            if args.lockdown:
                print(json.dumps(assistant.engage_lockdown(args.reason), indent=2))
                return 0
            if args.stand_down:
                print(json.dumps(assistant.stand_down_commander_mode(), indent=2))
                return 0
            if args.unlock:
                pin = getpass.getpass("HINATA Owner PIN to lift Red Alert Lockdown: ")
                print(json.dumps(assistant.release_lockdown(pin), indent=2))
                return 0
            if args.commander is not None:
                pin = getpass.getpass("HINATA Owner PIN to activate Commander Mode: ")
                print(
                    json.dumps(
                        assistant.activate_commander_mode(pin, minutes=args.commander),
                        indent=2,
                    )
                )
                return 0
            print(json.dumps(assistant.override_controller.status(), indent=2))
            return 0
        if args.command == "schedule":
            if args.add:
                sched_id = assistant.scheduler.schedule(
                    title=args.add,
                    request_text=args.request,
                    interval_seconds=args.interval,
                    max_runs=args.max_runs,
                    requires_online=False,
                )
                print(json.dumps({"schedule_id": sched_id, "title": args.add}, indent=2, ensure_ascii=False))
                return 0
            if args.disable:
                ok = assistant.scheduler.cancel(args.disable)
                print(f"Scheduled job {args.disable} disabled." if ok else "Schedule ID not found.")
                return 0 if ok else 1
            if args.tick:
                def _run_sched(row: dict[str, Any]) -> dict[str, Any]:
                    res = assistant.process_text(
                        str(row["request_text"]),
                        autonomy_mode=str(row.get("autonomy_mode") or "SUGGEST_ONLY"),
                    )
                    return {"status": res.status, "task_id": str(res.data.get("task_id", ""))}

                ran = assistant.scheduler.tick(
                    _run_sched,
                    online_mode_enabled=assistant.config.security.online_mode_enabled,
                )
                print(json.dumps({"executed_jobs": ran}, indent=2, ensure_ascii=False))
                return 0
            print(json.dumps(assistant.scheduler.list_schedules(), indent=2, ensure_ascii=False))
            return 0
        if args.command == "tasks":
            if args.pause:
                if not args.id or not assistant.tasks.pause_task(args.id):
                    print("Task could not be paused (missing, inactive, or step currently RUNNING).", file=sys.stderr)
                    return 1
                print(f"Task {args.id} paused at safe boundary.")
                return 0
            if args.resume_paused:
                if not args.id or not assistant.tasks.resume_paused_task(args.id):
                    print("Task could not be resumed (not PAUSED or contains uncertain step outcomes).", file=sys.stderr)
                    return 1
                print(f"Task {args.id} transitioned to RUNNING.")
                return 0
            if args.reconcile:
                if not args.id:
                    print("A task ID is required.", file=sys.stderr)
                    return 1
                print(json.dumps(assistant.reconcile_task(args.id), indent=2, ensure_ascii=False))
                return 0
            if args.timeline:
                if not args.id:
                    print("A task ID is required.", file=sys.stderr)
                    return 1
                print(json.dumps(assistant.tracer.task_timeline(args.id), indent=2, ensure_ascii=False))
                return 0
            if args.verify or args.reject:
                if not args.id:
                    print("A task ID is required.", file=sys.stderr)
                    return 1
                task, checks = assistant.review_task(args.id)
                print(json.dumps({"task": task, "current_checks": checks}, indent=2, ensure_ascii=False))
                try:
                    evidence = input("Describe the evidence you personally observed (10-1000 characters): ").strip()
                    answer = input("Record this review? Type yes to confirm [y/N]: ").strip().lower()
                except EOFError:
                    print("Review cancelled.")
                    return 1
                if answer not in {"y", "yes"}:
                    print("Review cancelled.")
                    return 1
                if not assistant.attest_task(args.id, evidence, satisfied=args.verify):
                    print("Task state changed; review not recorded.", file=sys.stderr)
                    return 1
                print("Human review recorded; task " + ("marked complete." if args.verify else "marked failed."))
                return 0
            if args.cancel:
                if not args.id or not assistant.tasks.cancel_pending(args.id):
                    print("Task is not awaiting approval; it was not changed.", file=sys.stderr)
                    return 1
                print("Unapproved task cancelled. No steps were run.")
                return 0
            result = assistant.tasks.get(args.id) if args.id else assistant.tasks.recent()
            if result is None:
                print("Task not found.", file=sys.stderr)
                return 1
            print(json.dumps(result, indent=2, ensure_ascii=False))
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
                if response.data.get("task_id"):
                    assistant.tasks.cancel_pending(response.data["task_id"])
                print("\nHINATA: Plan cancelled; no steps were executed.")
                return 1
            if answer not in {"y", "yes"}:
                if response.data.get("task_id"):
                    assistant.tasks.cancel_pending(response.data["task_id"])
                print("HINATA: Plan cancelled; no steps were executed.")
                return 1
            response = assistant.execute_plan(
                response.plan_actions,
                task_id=response.data.get("task_id"),
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
            print(
                "\nShell: /briefing /lockdown /unlock /commander /standdown /voice /speak /stop /reconnect /status /memory /pin /forget /quit"
            )
            continue
        if text == "/briefing":
            emit_with_approvals(assistant, assistant.process_text("executive briefing"), speak=speak)
            continue
        if text == "/lockdown":
            print(json.dumps(assistant.engage_lockdown("Interactive shell Red Alert Lockdown"), indent=2))
            continue
        if text == "/unlock":
            try:
                pin = getpass.getpass("HINATA Owner PIN: ")
                print(json.dumps(assistant.release_lockdown(pin), indent=2))
            except (EOFError, PermissionError) as exc:
                print(f"HINATA: {exc}")
            continue
        if text.startswith("/commander"):
            parts = text.split()
            mins = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10
            try:
                pin = getpass.getpass("HINATA Owner PIN: ")
                print(json.dumps(assistant.activate_commander_mode(pin, minutes=mins), indent=2))
            except (EOFError, PermissionError) as exc:
                print(f"HINATA: {exc}")
            continue
        if text == "/standdown":
            print(json.dumps(assistant.stand_down_commander_mode(), indent=2))
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


def pull_model(model: str, ollama_config: OllamaConfig | None = None) -> int:
    executable = shutil.which("ollama")
    if executable is None:
        print("Ollama is not installed. See https://ollama.com/download/windows", file=sys.stderr)
        return 1
    print(f"Downloading {model} through local Ollama. Internet is required for this setup step only.")
    # Start the local service first so a fresh install can pull immediately.
    cfg = ollama_config or load_config().ollama
    cfg.model = model
    OllamaService(cfg).ensure_running()
    environment = os.environ.copy()
    environment["OLLAMA_HOST"] = urlsplit(cfg.host).netloc
    try:
        result = subprocess.run(
            [executable, "pull", model], env=environment, shell=False, check=False, timeout=1800
        )
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
