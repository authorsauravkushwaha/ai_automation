"""Non-destructive evidence probes for docs/ULTRON_WEAKNESS_AUDIT.md.

Run from the checkout: python scripts/audit_ultron_weaknesses.py
Each probe uses a temporary data directory. No real browser, network request,
PIN bypass, or destructive tool is executed. 'observed' means the documented
weakness still reproduces, NOT that the application passed a safety test.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hinata.core.assistant import HinataAssistant  # noqa: E402
from hinata.core.errors import BudgetTracker, TaskBudget  # noqa: E402
from hinata.core.observability import TraceRecorder  # noqa: E402
from hinata.core.reasoning import (  # noqa: E402
    ThinkingEngine,
    _RunBudget,
    keywords,
    requirement_coverage,
    self_consistency,
    thinking_mode,
)
from hinata.core.types import ActionRequest, ActionResult, AssistantResponse  # noqa: E402
from hinata.main import persist_thinking_mode  # noqa: E402
from tests.helpers import make_assistant  # noqa: E402

PROBES = {}


def probe(function):
    PROBES[function.__name__] = function
    return function


@probe
def confidence_counts(a, root):
    request = "python database security testing"
    answer = "python database guidance"
    coverage = requirement_coverage(request, answer)
    verification = ThinkingEngine._verify(request, answer, answer, "chat")
    return coverage[0] < coverage[1] and verification[0] == 1.0, {"coverage": coverage, "verification": verification}


@probe
def negation_consistency(a, root):
    result = self_consistency("The deployment is safe and ready for production today.", "The deployment is not safe and not ready for production today.")
    return result == ("agree", False), result


@probe
def json_plan_confidence(a, root):
    result = ThinkingEngine._verify("Safely back up everything", "{}", "{}", "plan")
    return result[1] == "high", result


@probe
def non_latin_requirements(a, root):
    result = keywords("আমার ফাইল নিরাপদে রাখুন")
    return result == [], result


@probe
def numeric_requirements(a, root):
    result = keywords("Keep 2026 records under 1000 bytes")
    return "2026" not in result and "1000" not in result, result


@probe
def reasoning_budget(a, root):
    budget = BudgetTracker(TaskBudget(max_model_calls=1))
    state = _RunBudget(budget, thinking_mode("ultra"), None, time.monotonic(), 150, 7)
    for _ in range(3):
        state.check()
        state.spend()
    return budget.model_calls == 0, {"thinking_calls": state.calls, "shared_budget_calls": budget.model_calls}


@probe
def preset_drift(a, root):
    a.set_ultron_mode(True)
    a.set_thinking_mode("off")
    result = a.set_ultron_mode(True)
    return result["enabled"] and result["thinking_mode"] == "off", result


@probe
def preset_discards_edits(a, root):
    a.set_ultron_mode(True)
    a.set_thinking_mode("deep")
    result = a.set_ultron_mode(False)
    return result["thinking_mode"] == "balanced", result["thinking_mode"]


@probe
def suggest_only_overridden(a, root):
    a.config.agentic.autonomy_mode = "SUGGEST_ONLY"
    other = HinataAssistant(a.config, llm_enabled=False)
    try:
        return other.orchestrator.autonomy_mode != "SUGGEST_ONLY", other.orchestrator.autonomy_mode
    finally:
        other.close()


@probe
def saved_thinking_overridden(a, root):
    a.config.thinking.mode = "off"
    other = HinataAssistant(a.config, llm_enabled=False)
    try:
        return other.thinking.mode == "ultra", other.thinking.mode
    finally:
        other.close()


@probe
def no_llm_thinking_status(a, root):
    a.set_ultron_mode(True)
    result = {"thinking_enabled": a.thinking_status()["enabled"], "available": a.ultron_status()["deliberation_available"]}
    return result["thinking_enabled"] and not result["available"], result


@probe
def persist_single_quotes(a, root):
    path = root / "config.toml"
    path.write_text("[thinking]\nmode = 'balanced'\n", encoding="utf-8")
    with patch.dict(os.environ, {"HINATA_CONFIG": str(path)}):
        persist_thinking_mode(a.config, "deep")
    saved = tomllib.loads(path.read_text(encoding="utf-8"))["thinking"]["mode"]
    return saved != "deep", saved


@probe
def persist_indentation(a, root):
    path = root / "config.toml"
    path.write_text('[thinking]\n  mode = "balanced"\n', encoding="utf-8")
    with patch.dict(os.environ, {"HINATA_CONFIG": str(path)}):
        persist_thinking_mode(a.config, "deep")
    try:
        tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        return True, str(exc)
    return False, "still valid"


@probe
def schedule_truncation(a, root):
    a.scheduler.schedule(title="probe", request_text="x" * 2000 + " NEVER DELETE ANYTHING")
    row = a.scheduler.list_schedules()[0]
    return "NEVER" not in row["request_text"], {"stored_length": len(row["request_text"])}


@probe
def schedule_budget(a, root):
    a.scheduler.schedule(title="probe", request_text="system info", run_at_epoch=1, budget={"max_tool_calls": 0})
    with patch.object(a, "process_text", return_value=AssistantResponse("mocked")) as process:
        a.scheduler.tick(a.run_scheduled_task, now_epoch=2)
    return process.call_count == 1, str(process.call_args)


@probe
def schedule_expiry_snapshot(a, root):
    for title in ["first", "second"]:
        a.scheduler.schedule(title=title, request_text="system info", run_at_epoch=1, expires_epoch=3)
    clock = [2.0]
    executed = []

    def run(row):
        executed.append({"title": row["title"], "clock": clock[0]})
        clock[0] = 10.0
        return {"status": "completed"}

    with patch("hinata.tasks.scheduler.time.time", side_effect=lambda: clock[0]):
        a.scheduler.tick(run)
    return len(executed) == 2 and executed[1]["clock"] > 3, executed


@probe
def schedule_online_type(a, root):
    a.scheduler.schedule(title="probe", request_text="system info", requires_online=2)
    value = a.scheduler.list_schedules()[0]["requires_online"]
    return value == 2, value


@probe
def schedule_integer_overflow(a, root):
    try:
        a.scheduler.schedule(title="probe", request_text="system info", interval_seconds=2**80)
    except OverflowError as exc:
        return True, str(exc)
    return False, "accepted or rejected differently"


@probe
def schedule_cancel_report(a, root):
    sid = a.scheduler.schedule(title="probe", request_text="system info", run_at_epoch=1)

    def run(row):
        a.scheduler.cancel(sid)
        return {"status": "completed"}

    returned = a.scheduler.tick(run, now_epoch=2)[0]["status"]
    stored = a.scheduler.list_schedules()[0]["last_status"]
    return returned != stored, {"returned": returned, "stored": stored}


@probe
def schedule_false_success(a, root):
    a.scheduler.schedule(title="probe", request_text="system info", run_at_epoch=1, interval_seconds=10, max_runs=2)
    with patch.object(a, "process_text", return_value=AssistantResponse("failed", False, "completed")):
        a.scheduler.tick(a.run_scheduled_task, now_epoch=2)
    enabled = a.scheduler.list_schedules()[0]["enabled"]
    return bool(enabled), {"enabled_after_success_false": enabled}


@probe
def authorization_cross_thread(a, root):
    action = ActionRequest("clear_memory", {"scope": "all"})
    # Replace dispatch entirely: no memory is actually deleted.
    with patch.object(a.orchestrator, "_dispatch_tool", return_value=ActionResult("clear_memory", True, "mock only")) as dispatch:
        with a.orchestrator._authorized_dispatch():
            with ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(a.orchestrator._run_tool, action).result(timeout=5)
    return result.success and dispatch.call_count == 1, "Another thread inherited the authorization depth; dispatch was mocked."


@probe
def direct_dynamic_permission(a, root):
    action = ActionRequest("write_code", {"language": "Python", "prompt": "hello", "output_path": "Documents/x.py"})
    with patch.object(a.orchestrator, "_dispatch_tool", return_value=ActionResult("write_code", True, "mock only")) as dispatch:
        a.orchestrator._run_tool(action)
    return dispatch.call_count == 1, "Direct helper admitted a write_code output_path; no file was written."


@probe
def invalid_autonomy(a, root):
    result = a.process_text("create folder Documents/probe", autonomy_mode="TYPO")
    return result.success and (root / "Documents/probe").is_dir(), result.status


@probe
def reviewer_wrong_content(a, root):
    (root / "Documents/probe.txt").write_text("wrong", encoding="utf-8")
    action = ActionRequest("write_text_file", {"path": "Documents/probe.txt", "content": "expected"})
    check = a.orchestrator.reviewer.review((action,))[0]
    return check.status == "verified", check.detail


@probe
def code_review_escape(a, root):
    target = root / "outside.py"
    target.write_text("def outside():\n    pass\n", encoding="utf-8")
    result = a.dev_assistant.code_review(path=str(target), workspace=str(root / "Documents"))
    return result["files_scanned"] == 1, result


@probe
def dependency_symlink(a, root):
    target = root / "outside.txt"
    target.write_text("synthetic-private-dependency-marker", encoding="utf-8")
    (root / "Documents/requirements.txt").symlink_to(target)
    result = a.dev_assistant.dependency_audit(str(root / "Documents"))
    return "synthetic-private-dependency-marker" in json.dumps(result), result


@probe
def predictable_write_temp(a, root):
    target = root / "outside.txt"
    target.write_text("unchanged", encoding="utf-8")
    temporary_path = root / "Documents" / f".probe.txt.hinata-tmp-{os.getpid()}"
    temporary_path.symlink_to(target)
    a.files.write_text_file("Documents/probe.txt", "changed")
    return target.read_text(encoding="utf-8") == "changed", "Synthetic file outside approved roots was overwritten through the predictable temporary symlink."


@probe
def audit_two_writers(a, root):
    first, second = TraceRecorder(a.database), TraceRecorder(a.database)
    barrier = threading.Barrier(2)
    original = a.database.fetch_one

    def fetch(sql, *args):
        row = original(sql, *args)
        if "SELECT event_hash" in sql:
            barrier.wait(timeout=5)
        return row

    with patch.object(a.database, "fetch_one", side_effect=fetch):
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(t.record_event, run_id="", event_type="probe") for t in [first, second]]
            for job in jobs:
                job.result(timeout=10)
    result = first.verify_integrity()
    return not result["intact"], result


@probe
def audit_metadata_unhashed(a, root):
    event = a.tracer.record_event(run_id="", event_type="probe", model_identity="original")
    a.database.execute("UPDATE agent_trace_events SET model_identity='altered', duration_ms=9999 WHERE id=?", (event,))
    result = a.tracer.verify_integrity()
    return result["intact"], result


@probe
def audit_json_truncation(a, root):
    a.tracer.record_event(run_id="", event_type="probe", payload={"text": "x" * 5000})
    row = a.database.fetch_one("SELECT payload_json FROM agent_trace_events ORDER BY id DESC LIMIT 1")
    try:
        json.loads(row["payload_json"])
    except json.JSONDecodeError:
        return True, {"stored_characters": len(row["payload_json"]), "valid_json": False}
    return False, "valid JSON"


@probe
def metrics_double_count(a, root):
    rid, _ = a.tracer.start_run(user_goal="probe", source="text")
    a.tracer.update_run(rid, state="SUCCEEDED")
    a.tracer.update_run(rid, state="SUCCEEDED")
    snapshot = a.tracer.metrics_snapshot()
    return snapshot["tasks_succeeded"] > snapshot["tasks_started"], snapshot


@probe
def clear_retains_reflections(a, root):
    a.thinking.reflections.add(objective="synthetic-private-objective", confidence="high", lesson="synthetic-private-lesson", mode="ultra")
    a.memory.clear("all")
    row = a.database.fetch_one("SELECT objective FROM reasoning_reflections LIMIT 1")
    return row is not None, row


@probe
def schedule_unredacted(a, root):
    marker = "password=synthetic-only-not-a-real-secret"
    a.scheduler.schedule(title="probe", request_text=marker)
    row = a.scheduler.list_schedules()[0]
    return row["request_text"] == marker, "The synthetic password-like marker was retained verbatim."


@probe
def tool_budget_not_enforced(a, root):
    a.orchestrator.default_budget.max_tool_calls = 0
    with patch.object(a.orchestrator, "_run_tool", return_value=ActionResult("system_info", True, "mock only")) as run:
        result = a.process_text("system info")
    return result.success and run.call_count == 1, {"max_tool_calls": 0, "actual_tool_calls": run.call_count}


@probe
def pytest_target_flag(a, root):
    result = SimpleNamespace(returncode=0, stdout="", stderr="")
    with patch("hinata.automation.dev_assistant.subprocess.run", return_value=result) as run:
        a.dev_assistant.run_tests(workspace=str(root / "Documents"), target="--collect-only")
    argv = run.call_args.args[0]
    return "--collect-only" in argv and "--" not in argv, argv


@probe
def shutdown_omits_scheduler(a, root):
    with patch.object(a.scheduler, "stop_background") as stop:
        a.close()
    return stop.call_count == 0, {"scheduler_stop_calls": stop.call_count}


def main():
    results = []
    for name, function in PROBES.items():
        with tempfile.TemporaryDirectory(prefix="hinata-audit-") as directory:
            assistant = make_assistant(directory)
            try:
                observed, evidence = function(assistant, Path(directory))
                results.append({"probe": name, "observed": bool(observed), "evidence": evidence})
            except Exception as exc:
                results.append({"probe": name, "observed": None, "error": f"{type(exc).__name__}: {exc}"})
            finally:
                assistant.close()
    print(json.dumps({"purpose": "weakness reproduction, not safety-test success", "probes": results}, indent=2, default=str))
    return int(any(row["observed"] is not True for row in results))


if __name__ == "__main__":
    raise SystemExit(main())
