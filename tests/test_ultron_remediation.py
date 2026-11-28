"""Regression tests for the current-tree Ultron audit, using no live model/network."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import tomllib
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from hinata.automation.app_control import AutomationError
from hinata.core.errors import BudgetTracker, ClassifiedError, TaskBudget
from hinata.core.observability import TraceRecorder
from hinata.core.reasoning import keywords, requirement_coverage
from hinata.core.tool_registry import ToolRegistry
from hinata.core.types import ActionRequest, ActionResult, AssistantResponse
from hinata.main import persist_thinking_mode
from hinata.tasks.scheduler import validated_budget
from tests.helpers import make_assistant


class UltronRemediationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.assistant = make_assistant(directory.name)
        self.addCleanup(self.assistant.close)
        self.scheduler = self.assistant.scheduler

    def schedule(self, **options):
        defaults = dict(title="check", request_text="system info", run_at_epoch=1,
                        interval_seconds=10, max_runs=3)
        defaults.update(options)
        return self.scheduler.schedule(**defaults)

    def test_saved_off_and_strict_autonomy_survive_preset(self):
        self.assistant.config.thinking.mode = "off"
        self.assistant.config.agentic.autonomy_mode = "SUGGEST_ONLY"
        status = self.assistant.set_ultron_mode(True)
        self.assertEqual(status["thinking_mode"], "off")
        self.assertEqual(status["autonomy_mode"], "SUGGEST_ONLY")
        self.assertFalse(self.assistant.thinking_status()["enabled"])

    def test_preset_off_preserves_intervening_owner_edit(self):
        self.assistant.set_ultron_mode(True)
        self.assistant.set_thinking_mode("deep")
        self.assertTrue(self.assistant.ultron_status()["modified"])
        self.assistant.set_ultron_mode(False)
        self.assertEqual(self.assistant.thinking.mode, "deep")

    def test_persistence_uses_loaded_path_and_parses_single_quotes(self):
        path = self.root / "custom.toml"
        path.write_text("[thinking]\n  mode = 'balanced' # owner comment\n", encoding="utf-8")
        self.assistant.config.source_path = path
        with patch.dict(os.environ, {"HINATA_CONFIG": str(self.root / "wrong.toml")}):
            persist_thinking_mode(self.assistant.config, "deep")
        self.assertEqual(tomllib.loads(path.read_text())["thinking"]["mode"], "deep")
        self.assertFalse((self.root / "wrong.toml").exists())
        self.assertFalse(list(self.root.glob(".hinata-config-*")))

    def test_schedule_rejects_truncation_and_bad_types(self):
        for options in ({"request_text": "x" * 2001}, {"requires_online": 2},
                        {"interval_seconds": 2**100}, {"budget": []}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.schedule(**options)

    def test_budget_fields_validated(self):
        for budget in ({"made_up": 1}, {"max_tool_calls": True}, {"max_tokens": -1},
                       {"max_duration_seconds": float("inf")}, {"max_replans": 1.5}):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                validated_budget(budget)

    def test_scheduled_zero_tool_budget_blocks_dispatch(self):
        sid = self.schedule(autonomy_mode="LOW_RISK_AUTONOMY", budget={"max_tool_calls": 0})
        with patch.object(self.assistant.orchestrator, "_dispatch_tool") as dispatch:
            result = self.scheduler.tick(self.assistant.run_scheduled_task, now_epoch=2)
        dispatch.assert_not_called()
        self.assertEqual(result[0]["status"], "blocked")
        self.assertFalse(self.scheduler.get(sid)["enabled"])

    def test_false_success_does_not_renew(self):
        sid = self.schedule()
        with patch.object(self.assistant, "process_text", return_value=AssistantResponse("failure", False, "completed")):
            self.scheduler.tick(self.assistant.run_scheduled_task, now_epoch=2)
        self.assertFalse(self.scheduler.get(sid)["enabled"])
        self.assertEqual(self.scheduler.attempts(sid)[0]["status"], "failed")

    def test_cancel_during_run_reports_durable_status(self):
        sid = self.schedule()
        def runner(row):
            self.scheduler.cancel(sid)
            return {"status": "completed", "success": True}
        result = self.scheduler.tick(runner, now_epoch=2)
        self.assertEqual(result[0]["status"], "cancelled")
        self.assertEqual(self.scheduler.attempts(sid)[0]["status"], "cancelled")

    def test_runner_deadline_parks_unknown_and_blocks_while_active(self):
        sid = self.schedule(budget={"max_duration_seconds": 0.02})
        release = threading.Event()
        self.addCleanup(release.set)
        def runner(row):
            release.wait(3)
            return {"status": "completed", "success": True}
        started = time.monotonic()
        result = self.scheduler.tick(runner, now_epoch=2)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(result[0]["status"], "unknown_outcome")
        self.assertFalse(self.scheduler.get(sid)["enabled"])
        self.assertEqual(self.scheduler.tick(runner, now_epoch=20), [])
        release.set()
        self.scheduler._worker.join(2)
        self.assertEqual(self.scheduler.get(sid)["last_status"], "unknown_outcome")

    def test_default_expiry_and_paginated_lookup(self):
        first = self.schedule()
        second = self.schedule()
        self.assertEqual(len(self.scheduler.list_schedules(limit=1, offset=1)), 1)
        self.assertEqual(self.scheduler.get(first)["expires_epoch"], 1 + 30 * 86400)
        self.assertNotEqual(first, second)

    def test_plaintext_secret_schedule_is_rejected(self):
        with self.assertRaises(ValueError):
            self.schedule(request_text="remember api_key=synthetic-secret-123456")

    def test_encrypted_schedule_roundtrip_has_redacted_display(self):
        import importlib.util
        if importlib.util.find_spec("cryptography") is None:
            self.skipTest("secure-storage extra not installed")
        from hinata.security.local_protection import LocalVault
        self.assistant.database.vault = LocalVault(self.root / "data", enabled=True)
        sid = self.schedule(request_text="remember api_key=synthetic-secret-123456")
        raw = self.assistant.database.fetch_one("SELECT request_text FROM scheduled_tasks WHERE schedule_id=?", (sid,))
        self.assertTrue(raw["request_text"].startswith("enc:"))
        self.assertNotIn("synthetic-secret", json.dumps(self.scheduler.get(sid)))
        seen = []
        self.scheduler.tick(lambda row: seen.append(row["request_text"]) or {"status": "completed"}, now_epoch=2)
        self.assertIn("synthetic-secret", seen[0])

    def test_atomic_budget_reservations(self):
        budget = BudgetTracker(TaskBudget(max_tool_calls=1))
        def reserve():
            try:
                budget.record_tool_call()
                return True
            except ClassifiedError:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(lambda _: reserve(), range(20))), 1)
        self.assertEqual(budget.tool_calls, 1)

    def test_explicit_zero_tool_budget(self):
        budget = BudgetTracker(TaskBudget(max_tool_calls=0))
        with patch.object(self.assistant.orchestrator, "_dispatch_tool") as dispatch:
            result = self.assistant.process_text("system info", budget_tracker=budget)
        self.assertEqual(result.status, "blocked")
        dispatch.assert_not_called()

    def test_timeout_reports_unknown_not_failure(self):
        release = threading.Event()
        self.addCleanup(release.set)
        started = threading.Event()
        def work(action):
            started.set()
            release.wait(3)
            return ActionResult(action.tool, True, "late success")
        budget = BudgetTracker(TaskBudget(per_tool_timeout_seconds=0.02))
        with patch.object(self.assistant.orchestrator, "_run_authorized_tool", side_effect=work):
            result = self.assistant.orchestrator._bounded_tool(ActionRequest("system_info"), budget, None)
        self.assertTrue(started.is_set())
        self.assertEqual(result.status, "unknown_outcome")
        release.set()
        for future in list(self.assistant.orchestrator._inflight_tools):
            future.result(timeout=2)

    def test_authorization_is_exact_action_and_thread_local(self):
        action = ActionRequest("clear_memory", {"scope": "all"})
        other = ActionRequest("clear_memory", {"scope": "conversations"})
        with patch.object(self.assistant.orchestrator, "_dispatch_tool") as dispatch:
            with self.assistant.orchestrator._authorized_dispatch(action):
                self.assertFalse(self.assistant.orchestrator._run_tool(other).success)
                with ThreadPoolExecutor(max_workers=1) as pool:
                    self.assertFalse(pool.submit(self.assistant.orchestrator._run_tool, action).result().success)
        dispatch.assert_not_called()

    def test_dynamic_permission_is_checked_by_direct_helper(self):
        action = ActionRequest("write_code", {"language": "Python", "prompt": "hello", "output_path": "Documents/a.py"})
        with patch.object(self.assistant.orchestrator, "_dispatch_tool") as dispatch:
            self.assertFalse(self.assistant.orchestrator._run_tool(action).success)
        dispatch.assert_not_called()

    def test_invalid_autonomy_and_aggregate_input_are_rejected(self):
        self.assertEqual(self.assistant.process_text("system info", autonomy_mode="TYPO").status, "invalid")
        self.assertEqual(self.assistant.process_text("system info and " + "x" * 4001).status, "invalid")

    def test_idempotency_claim_is_atomic(self):
        registry = ToolRegistry(self.assistant.database)
        def claim(_):
            return registry.claim_effect("test-claim", task_id="", step_id="", tool="system_info")
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(claim, range(16))), 1)

    def test_plan_override_revalidated(self):
        plan = self.assistant.planner.validate({"type": "plan", "objective": "check", "steps": [
            {"tool": "system_info", "arguments": {}}]})
        bad = ActionRequest("invented_tool", {})
        nodes = tuple(replace(node, action=bad) for node in plan.nodes)
        invalid = replace(plan, steps=(bad,), nodes=nodes)
        response = self.assistant.orchestrator.execute_plan([], plan_override=invalid)
        self.assertEqual(response.status, "invalid")

    def test_write_verification_checks_content(self):
        (self.root / "Documents/a.txt").write_text("wrong")
        action = ActionRequest("write_text_file", {"path": "Documents/a.txt", "content": "expected"})
        self.assertEqual(self.assistant.orchestrator.reviewer.review((action,))[0].status, "contradicted")
        (self.root / "Documents/a.txt").write_text("expected")
        self.assertEqual(self.assistant.orchestrator.reviewer.review((action,))[0].status, "verified")

    def test_coverage_preserves_numeric_and_bengali_requirements(self):
        self.assertIn("2026", keywords("Keep 2026 records"))
        self.assertTrue(keywords("আমার ফাইল নিরাপদে রাখুন"))
        covered, total, missing = requirement_coverage("security privacy testing", "security")
        self.assertEqual((covered, total), (1, 3))
        self.assertEqual(len(missing), 2)

    def test_code_review_and_manifest_escape_rejected(self):
        outside = self.root / "outside.py"
        outside.write_text("def hidden(): pass\n")
        with self.assertRaises(AutomationError):
            self.assistant.dev_assistant.code_review(str(outside), workspace=str(self.root / "Documents"))
        try:
            (self.root / "Documents/requirements.txt").symlink_to(outside)
        except OSError:
            self.skipTest("symlinks unavailable")
        with self.assertRaises(AutomationError):
            self.assistant.dev_assistant.dependency_audit(str(self.root / "Documents"))

    def test_pytest_flag_is_rejected(self):
        with self.assertRaises(AutomationError):
            self.assistant.dev_assistant.run_tests(str(self.root / "Documents"), "--override-ini=anything")

    def test_unique_no_clobber_file_publication(self):
        def write(value):
            try:
                self.assistant.files.write_text_file("Documents/race.txt", value)
                return True
            except AutomationError:
                return False
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(write, ["a", "b", "c", "d"])), 1)
        self.assertIn((self.root / "Documents/race.txt").read_text(), {"a", "b", "c", "d"})
        self.assertFalse(list((self.root / "Documents").glob("*.hinata-*")))

    def test_multiple_audit_writers_have_one_chain(self):
        recorders = [TraceRecorder(self.assistant.database) for _ in range(4)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = [pool.submit(recorder.record_event, run_id="", event_type="concurrent")
                       for recorder in recorders for _ in range(5)]
            for result in results:
                result.result(timeout=5)
        self.assertTrue(recorders[0].verify_integrity()["intact"])

    def test_audit_metadata_tampering_is_detected(self):
        event = self.assistant.tracer.record_event(run_id="", event_type="test", model_identity="original")
        self.assistant.database.execute("UPDATE agent_trace_events SET model_identity='changed' WHERE id=?", (event,))
        self.assertFalse(self.assistant.tracer.verify_integrity()["intact"])

    def test_long_audit_payload_is_valid_json(self):
        event = self.assistant.tracer.record_event(run_id="", event_type="large", payload={"data": "x" * 10000})
        row = self.assistant.database.fetch_one("SELECT payload_json FROM agent_trace_events WHERE id=?", (event,))
        self.assertTrue(json.loads(row["payload_json"])["truncated"])
        self.assertTrue(self.assistant.tracer.verify_integrity()["intact"])

    def test_prepaid_inference_retry_is_charged(self):
        from unittest.mock import MagicMock

        from hinata.core.runtime_limits import CURRENT_BUDGET, MODEL_RESERVED
        budget = BudgetTracker(TaskBudget(max_model_calls=1))
        budget.record_model_call()
        shared = CURRENT_BUDGET.set(budget)
        reserved = MODEL_RESERVED.set(True)
        self.addCleanup(CURRENT_BUDGET.reset, shared)
        self.addCleanup(MODEL_RESERVED.reset, reserved)
        response = MagicMock()
        response.read.return_value = b'{"message":{"content":"ok"}}'
        with patch.object(self.assistant.llm._opener, "open", return_value=response) as opened:
            self.assistant.llm._request("/api/chat", {})
            with self.assertRaises(ClassifiedError):
                self.assistant.llm._request("/api/chat", {})
        self.assertEqual(opened.call_count, 1)

    def test_inference_queue_obeys_thinking_deadline(self):
        from hinata.core.llm_manager import OllamaError
        from hinata.core.runtime_limits import MODEL_DEADLINE
        token = MODEL_DEADLINE.set(time.monotonic() - 1)
        self.addCleanup(MODEL_DEADLINE.reset, token)
        with patch.object(self.assistant.llm._concurrency_semaphore, "acquire", return_value=False):
            with self.assertRaises(OllamaError):
                with self.assistant.llm._inference_slot(None):
                    self.fail("expired request entered inference")

    def test_numeric_coverage_requires_whole_number(self):
        covered, total, _ = requirement_coverage("2026", "20260")
        self.assertEqual((covered, total), (0, 1))

    def test_minimum_confidence_applies_to_fallback(self):
        from hinata.core.reasoning import ThinkingEngine
        engine = ThinkingEngine(None, mode="off", require_min_confidence=True)
        result = engine.think("Explain the unknown", draft_fn=lambda: "Unverified answer")
        self.assertIn("not have enough verified evidence", result.text)


if __name__ == "__main__":
    unittest.main()
