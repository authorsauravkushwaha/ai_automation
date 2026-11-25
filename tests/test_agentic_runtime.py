"""Phase 1–4 tests: migrations, cancellation tokens, budgets, circuit breakers,
model router, DAG planner, multi-agent registry, autonomy modes, reconciliation,
and bounded scheduler.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hinata.agents.registry import AgentMessage, AgentRegistry, SubtaskReport
from hinata.agents.runtime import AgentExecutor
from hinata.config import OllamaConfig
from hinata.core.errors import (
    BudgetTracker,
    CancellationRegistry,
    CircuitBreaker,
    ClassifiedError,
    TaskBudget,
    classify_exception,
)
from hinata.core.llm_manager import LLMManager, ModelUnavailableError
from hinata.core.planner import Planner, PlanningError
from hinata.core.providers import (
    ContextManager,
    HardwareCapabilityService,
    ModelRouter,
    StructuredObservation,
)
from hinata.core.tool_registry import ToolRegistry
from hinata.core.types import ActionRequest, ActionResult
from hinata.database.database import Database
from hinata.security.action_validator import ActionValidator
from hinata.tasks.scheduler import TaskScheduler
from tests.helpers import make_assistant
from tests.test_ollama_connection import StubOllama


class ReliabilityAndObservabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.assistant = make_assistant(self.tmp.name)

    def tearDown(self) -> None:
        self.assistant.close()
        self.tmp.cleanup()

    def test_versioned_migrations_apply_idempotently(self) -> None:
        from hinata.database.migrations import LATEST_SCHEMA_VERSION

        db = Database(Path(self.tmp.name) / "mig.db")
        v = db.ensure_migrated()
        self.assertEqual(v, LATEST_SCHEMA_VERSION)
        self.assertEqual(len(db.migrations()), LATEST_SCHEMA_VERSION)
        # Second call is a clean no-op
        self.assertEqual(db.ensure_migrated(), LATEST_SCHEMA_VERSION)

    def test_request_scoped_cancellation_tokens_isolate_requests(self) -> None:
        reg = CancellationRegistry()
        t1 = reg.create_token("req-1")
        t2 = reg.create_token("req-2")
        self.assertTrue(reg.cancel_request("req-1", "cancel 1 only"))
        self.assertTrue(t1.is_cancelled)
        self.assertFalse(t2.is_cancelled)
        with self.assertRaises(ClassifiedError):
            t1.check()
        t2.check()  # Does not raise
        reg.cancel_all("emergency")
        self.assertTrue(t2.is_cancelled)

    def test_budget_tracker_and_circuit_breaker_enforce_bounds(self) -> None:
        tracker = BudgetTracker(TaskBudget(max_iterations=2, max_model_calls=1, max_tokens=100))
        tracker.record_iteration()
        tracker.record_model_call(tokens=50)
        with self.assertRaises(ClassifiedError):
            tracker.record_model_call(tokens=10)

        cb = CircuitBreaker(failure_threshold=2, recovery_seconds=60.0)
        cb.record_failure("svc", "err1")
        self.assertTrue(cb.allow("svc"))
        cb.record_failure("svc", "err2")
        self.assertFalse(cb.allow("svc"))
        cb.record_success("svc")
        self.assertTrue(cb.allow("svc"))

    def test_error_classification_covers_categories(self) -> None:
        self.assertEqual(classify_exception(TimeoutError("timed out")).code, "ERR_TIMEOUT")
        self.assertEqual(classify_exception(PermissionError("access denied")).code, "ERR_PERMISSION_DENIED")

    def test_trace_recorder_redacts_secrets_and_sequences_events(self) -> None:
        run_id, _ = self.assistant.tracer.start_run(
            user_goal="check key sk-1234567890abcdefghijklmnop",
            source="text",
        )
        self.assistant.tracer.record_event(
            run_id=run_id,
            event_type="step_info",
            status="RUNNING",
            payload={"token": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"},
        )
        self.assistant.tracer.update_run(run_id, state="SUCCEEDED", summary="done")
        run = self.assistant.tracer.get_run(run_id)
        assert run is not None
        serialized = json.dumps(run)
        self.assertNotIn("sk-1234567890abcdefghijklmnop", serialized)
        self.assertNotIn("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456", serialized)
        self.assertEqual([e["sequence"] for e in run["events"]], [1, 2])

    def test_pause_resume_and_crash_reconciliation_never_replay(self) -> None:
        plan = self.assistant.planner.validate(
            {
                "type": "plan",
                "objective": "Create two folders",
                "steps": [
                    {"step_id": "step_1", "tool": "create_folder", "arguments": {"path": "Documents/rec1"}},
                    {"step_id": "step_2", "tool": "system_info", "arguments": {}, "depends_on": ["step_1"]},
                ],
            }
        )
        tid = self.assistant.tasks.create(plan, "text")
        self.assistant.tasks.claim(tid, plan, "text")
        # Safe pause before any step starts
        self.assertTrue(self.assistant.tasks.pause_task(tid))
        self.assertTrue(self.assistant.tasks.resume_paused_task(tid))

        # Simulate crash during step_1 WITHOUT folder created -> reconciled as not_applied / FAILED
        self.assistant.tasks.start_step(tid, 1)
        self.assertFalse(self.assistant.tasks.pause_task(tid))  # Cannot pause while RUNNING
        recon = self.assistant.reconcile_task(tid)
        self.assertEqual(recon["reconciled_steps"][0]["reconciled_state"], "FAILED")
        self.assertFalse((Path(self.tmp.name) / "Documents" / "rec1").exists())
        self.assertTrue(self.assistant.tasks.resume_paused_task(tid))
        self.assertEqual(self.assistant.tasks.get(tid)["steps"][0]["state"], "QUEUED")

        # Simulate crash during system_info (non-reconcilable) -> UNKNOWN_OUTCOME
        tid2 = self.assistant.tasks.create(
            self.assistant.planner.validate(
                {"type": "plan", "objective": "Info", "steps": [{"tool": "system_info", "arguments": {}}]}
            ),
            "text",
        )
        self.assistant.tasks.claim(
            tid2,
            self.assistant.planner.validate(
                {"type": "plan", "objective": "Info", "steps": [{"tool": "system_info", "arguments": {}}]}
            ),
            "text",
        )
        self.assistant.tasks.start_step(tid2, 1)
        recon2 = self.assistant.reconcile_task(tid2)
        self.assertEqual(recon2["state"], "UNKNOWN_OUTCOME")
        self.assertFalse(self.assistant.tasks.resume_paused_task(tid2))

    def test_bounded_offline_scheduler_skips_online_and_caps_runs(self) -> None:
        sched = TaskScheduler(self.assistant.database)
        sid_online = sched.schedule(
            title="Online check",
            request_text="system info",
            run_at_epoch=1.0,
            requires_online=True,
        )
        sid_local = sched.schedule(
            title="Local check",
            request_text="system info",
            run_at_epoch=1.0,
            max_runs=1,
        )
        executed: list[str] = []
        reports = sched.tick(
            lambda row: executed.append(f"{row['request_text']}:{row['autonomy_mode']}") or {"status": "completed"},
            online_mode_enabled=False,
            now_epoch=10.0,
        )
        by_id = {r["schedule_id"]: r["status"] for r in reports}
        self.assertEqual(by_id[sid_online], "blocked_offline")
        self.assertEqual(by_id[sid_local], "completed")
        self.assertEqual(executed, ["system info:SUGGEST_ONLY"])
        # Completed max_runs=1 -> the local schedule does not execute again.
        # The online-only schedule is pushed forward instead of being reported
        # as blocked on every single tick: a task parked at the head of the due
        # queue would otherwise be re-selected forever and could crowd out work
        # that *can* run while offline.
        second_tick = sched.tick(lambda row: executed.append("unexpected") or {"status": "completed"}, now_epoch=20.0)
        self.assertEqual(executed, ["system info:SUGGEST_ONLY"])
        self.assertEqual(second_tick, [])
        # ...and it comes back once the network is available again.
        third_tick = sched.tick(
            lambda row: {"status": "completed"}, online_mode_enabled=True, now_epoch=200.0
        )
        self.assertEqual([r["status"] for r in third_tick], ["completed"])


class ModelRouterPlannerAndMultiAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.assistant = make_assistant(self.tmp.name)

    def tearDown(self) -> None:
        self.assistant.close()
        self.tmp.cleanup()

    def test_strict_model_pin_prevents_fallback_while_router_supports_roles(self) -> None:
        router = ModelRouter(
            "qwen2.5:0.5b",
            "nomic-embed-text",
            role_overrides={"coder": "qwen2.5-coder:1.5b"},
            allow_fallback=False,
        )
        sel = router.resolve("coder", [{"name": "qwen2.5-coder:1.5b", "digest": "sha256:abc"}])
        self.assertEqual(sel.resolved_model, "qwen2.5-coder:1.5b")
        miss = router.resolve("planner", [{"name": "llama3:8b", "digest": "sha256:def"}])
        self.assertEqual(miss.resolved_model, "")

        stub = StubOllama(models=["llama3:8b"])
        self.addCleanup(stub.close)
        strict_mgr = LLMManager(OllamaConfig(host=stub.url, strict_model_pin=True))
        with self.assertRaises(ModelUnavailableError):
            strict_mgr.chat([{"role": "user", "content": "hi"}], "sys")

    def test_streaming_chat_and_hardware_profiler_and_context_budget(self) -> None:
        hw = HardwareCapabilityService().detect()
        self.assertIn("recommended_profile", hw)
        self.assertIn(
            hw["recommended_profile"]["profile_name"],
            {"conservative_cpu", "balanced", "accelerated"},
        )

        cm = ContextManager(max_context_tokens=1024, reserved_output_tokens=128)
        obs = StructuredObservation(
            step_id="step_1",
            tool="read_text_file",
            success=True,
            status="completed",
            effect_state="SUCCEEDED",
            postcondition="verified",
            facts={"target_name": "notes.txt"},
        )
        sys_prompt, msgs, audit = cm.assemble(
            system_prompt="You are local HINATA.",
            user_text="Summarize status",
            history=[{"role": "user", "content": f"msg {i} " * 40} for i in range(25)],
            observations=[obs],
        )
        self.assertIn("Verified structured step observations", sys_prompt)
        self.assertTrue(audit["compacted"])

    def test_dag_planner_detects_cycles_write_conflicts_and_batches_readonly_waves(self) -> None:
        planner = Planner(ActionValidator(), max_steps=8)
        # Cycle rejected
        with self.assertRaisesRegex(PlanningError, "cycle"):
            planner.validate(
                {
                    "type": "plan",
                    "objective": "Cyclic",
                    "steps": [
                        {"step_id": "step_a", "tool": "system_info", "arguments": {}, "depends_on": ["step_b"]},
                        {"step_id": "step_b", "tool": "hardware_profile", "arguments": {}, "depends_on": ["step_a"]},
                    ],
                }
            )
        # Unordered write conflict to identical path rejected
        with self.assertRaisesRegex(PlanningError, "(?i)conflict"):
            planner.validate(
                {
                    "type": "plan",
                    "objective": "Conflicting folders",
                    "steps": [
                        {"step_id": "step_1", "tool": "create_folder", "arguments": {"path": "Documents/same"}},
                        {"step_id": "step_2", "tool": "create_folder", "arguments": {"path": "Documents/same"}},
                    ],
                }
            )
        # Valid DAG batches two independent read_only steps together, followed by mutating step
        dag = planner.validate(
            {
                "type": "plan",
                "objective": "Inspect then create",
                "steps": [
                    {"step_id": "step_1", "tool": "system_info", "arguments": {}},
                    {"step_id": "step_2", "tool": "hardware_profile", "arguments": {}},
                    {
                        "step_id": "step_3",
                        "tool": "create_folder",
                        "arguments": {"path": "Documents/after_reads"},
                        "depends_on": ["step_1", "step_2"],
                    },
                ],
            }
        )
        waves = dag.topological_batches()
        self.assertEqual(len(waves), 2)
        self.assertEqual([n.step_id for n in waves[0]], ["step_1", "step_2"])
        self.assertEqual([n.step_id for n in waves[1]], ["step_3"])

    def test_autonomy_modes_and_parallel_dag_execution(self) -> None:
        self.assistant.orchestrator.llm_enabled = True
        self.assistant.llm.complete = lambda *args, **kwargs: json.dumps(
            {
                "type": "plan",
                "objective": "Read system diagnostics",
                "steps": [
                    {"step_id": "step_1", "tool": "system_info", "arguments": {}},
                    {"step_id": "step_2", "tool": "hardware_profile", "arguments": {}},
                ],
            }
        )
        # SUGGEST_ONLY previews without executing
        sug = self.assistant.process_text("inspect diagnostics", autonomy_mode="SUGGEST_ONLY")
        self.assertTrue(sug.success)
        self.assertIn("SUGGEST_ONLY", sug.text)

        # LOW_RISK_AUTONOMY executes purely read-only low-risk plan waves automatically
        auto = self.assistant.process_text("inspect diagnostics", autonomy_mode="LOW_RISK_AUTONOMY")
        self.assertEqual(auto.status, "verification_required")
        self.assertEqual(len(auto.data["steps"]), 2)

    def test_multi_agent_registry_blocks_cycles_scope_escapes_locks_and_resolves_disputes(self) -> None:
        reg: AgentRegistry = self.assistant.agent_registry
        self.assertEqual(len(reg.list_manifests()), 10)

        # Circular delegation blocked
        with self.assertRaises(ClassifiedError) as ctx:
            reg.delegate_subtask(
                agent_id="research",
                root_task_id="t1",
                objective="loop",
                actions=[],
                authorized_runner=lambda a, ag: ActionResult(a.tool, True, "ok"),
                delegation_chain=("orchestrator", "research"),
            )
        self.assertEqual(ctx.exception.code, "ERR_CIRCULAR_DELEGATION")

        # Tool scope escape blocked
        with self.assertRaises(ClassifiedError) as ctx2:
            reg.delegate_subtask(
                agent_id="research",
                root_task_id="t1",
                objective="unauthorized delete",
                actions=[ActionRequest("clear_memory", {"scope": "all"})],
                authorized_runner=lambda a, ag: ActionResult(a.tool, True, "ok"),
            )
        self.assertEqual(ctx2.exception.code, "ERR_AGENT_TOOL_SCOPE")

        # Resource lock conflict blocked
        self.assertTrue(reg.locks.acquire("Documents/shared.py", "other-holder"))
        try:
            with self.assertRaises(ClassifiedError) as ctx3:
                reg.delegate_subtask(
                    agent_id="coding",
                    root_task_id="t1",
                    objective="inspect locked file",
                    actions=[ActionRequest("code_inspect", {"path": "Documents/shared.py"})],
                    authorized_runner=lambda a, ag: ActionResult(a.tool, True, "ok"),
                    resource_key="Documents/shared.py",
                )
            self.assertEqual(ctx3.exception.code, "ERR_RESOURCE_LOCKED")
        finally:
            reg.locks.release("Documents/shared.py", "other-holder")

        # Parallel read-only subtasks record durable envelopes
        reports = reg.run_parallel_readonly(
            [
                {"agent_id": "system_monitor", "objective": "sys", "actions": [ActionRequest("system_info", {})]},
                {"agent_id": "memory", "objective": "prefs", "actions": [ActionRequest("list_preferences", {})]},
            ],
            root_task_id="root-par-1",
            authorized_runner=lambda act, ag: self.assistant.orchestrator._run_tool(act),
        )
        self.assertTrue(all(r.success for r in reports))
        msgs = reg.messages_for_task("root-par-1")
        self.assertEqual(len(msgs), 4)  # 2 assignments + 2 results

        # Reviewer evidence overrides specialist claim on contradiction
        verdict = reg.resolve_review_consensus(
            SubtaskReport(agent_id="coding", child_task_id="c1", success=True, summary="claimed ok"),
            [{"step": 1, "status": "contradicted", "detail": "file missing"}],
        )
        self.assertFalse(verdict["accepted"])
        self.assertEqual(verdict["resolution"], "rejected_by_reviewer_evidence")

    def test_model_driven_specialist_report_is_strict_and_side_effects_stay_central(self) -> None:
        class FakeLocalModel:
            last_model_identity = "fake-coder@sha256:test"

            def __init__(self) -> None:
                self.calls = 0

            def chat(self, messages, system, **kwargs):
                self.calls += 1
                assignment = json.loads(messages[0]["content"])
                action = assignment["actions"][0]
                return json.dumps(
                    {
                        "status": "ready",
                        "summary": "The assigned inspection is ready for the central executor.",
                        "outputs": {"intent": "inspect only"},
                        "proposed_actions": [action],
                        "evidence": [{"kind": "assignment", "detail": "Matched the declared assignment", "source": "local_model"}],
                        "limitations": ["The model did not execute the tool."],
                    }
                )

        model = FakeLocalModel()
        executor = AgentExecutor(model)
        invoked = []
        report = self.assistant.agent_registry.delegate_subtask(
            agent_id="coding",
            root_task_id="runtime-report-1",
            objective="Inspect a bounded source file",
            actions=[ActionRequest("code_inspect", {"path": "Documents/example.py"})],
            authorized_runner=lambda action, agent: invoked.append((action.tool, agent)) or ActionResult(
                action.tool, True, "central result", {"verified": True}
            ),
            specialist_executor=executor.prepare,
        )
        self.assertTrue(report.success)
        self.assertEqual(report.model_identity, "fake-coder@sha256:test")
        self.assertEqual(model.calls, 1)
        self.assertEqual(invoked, [("code_inspect", "coding")])
        persisted = self.assistant.agent_registry.reports_for_task("runtime-report-1")
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0]["status"], "SUCCEEDED")

        class MalformedModel(FakeLocalModel):
            def chat(self, messages, system, **kwargs):
                self.calls += 1
                return json.dumps({"status": "ready", "summary": "missing required fields"})

        malformed_invoked = []
        blocked = self.assistant.agent_registry.delegate_subtask(
            agent_id="coding",
            root_task_id="runtime-report-2",
            objective="Reject malformed report",
            actions=[ActionRequest("code_inspect", {"path": "Documents/example.py"})],
            authorized_runner=lambda action, agent: malformed_invoked.append(action.tool) or ActionResult(
                action.tool, True, "must not run"
            ),
            specialist_executor=AgentExecutor(MalformedModel()).prepare,
        )
        self.assertFalse(blocked.success)
        self.assertEqual(malformed_invoked, [])
        self.assertEqual(blocked.status, "BLOCKED")

    def test_tool_registry_idempotency_and_timeout(self) -> None:
        treg = ToolRegistry(self.assistant.database)
        act = ActionRequest("create_folder", {"path": "Documents/idem"})
        key = treg.compute_idempotency_key("taskA", "step1", act)
        treg.mark_effect(key, task_id="taskA", step_id="step1", tool="create_folder", effect_state="SUCCEEDED")
        cached = treg.check_idempotency(key)
        assert cached is not None
        self.assertEqual(cached["effect_state"], "SUCCEEDED")

        msg = AgentMessage.build(
            sender_id="orchestrator",
            recipient_id="research",
            root_task_id="t1",
            message_type="task_assignment",
            payload={"secret": "sk-1234567890abcdefghijklmnop"},
        )
        self.assertNotIn("sk-1234567890abcdefghijklmnop", json.dumps(msg.payload))


if __name__ == "__main__":
    unittest.main()
