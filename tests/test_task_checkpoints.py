import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from hinata.core.types import ActionResult
from tests.helpers import make_assistant


class TaskCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.assistant = make_assistant(self.tmp.name)
        self.root = Path(self.tmp.name)
        self.assistant.orchestrator.llm_enabled = True
        self.assistant.llm.complete = lambda *args, **kwargs: (
            '{"type":"plan","objective":"Prepare workspace", "steps":['
            '{"tool":"create_folder","arguments":{"path":"Documents/project"}},'
            '{"tool":"system_info","arguments":{}}]}'
        )

    def tearDown(self):
        self.assistant.close()
        self.tmp.cleanup()

    def test_approval_checkpoints_and_replay_prevention(self):
        pending = self.assistant.process_text("prepare my workspace")
        task_id = pending.data["task_id"]
        self.assertEqual(self.assistant.tasks.get(task_id)["state"], "AWAITING_APPROVAL")
        self.assertFalse((self.root / "Documents/project").exists())
        result = self.assistant.execute_plan(pending.plan_actions, task_id=task_id)
        self.assertFalse(result.success)
        self.assertEqual(result.status, "verification_required")
        task = self.assistant.tasks.get(task_id)
        self.assertEqual(task["objective"], "Prepare workspace")
        self.assertEqual(task["state"], "VERIFYING")
        self.assertEqual(task["user_request"], "prepare my workspace")
        self.assertEqual([step["state"] for step in task["steps"]], ["COMPLETED", "COMPLETED"])
        self.assertEqual(task["steps"][0]["evidence"], "Folder exists after creation")
        replay = self.assistant.execute_plan(pending.plan_actions, task_id=task_id)
        self.assertFalse(replay.success)
        self.assertEqual(replay.status, "invalid")
        self.assistant.close()
        self.assistant = make_assistant(self.tmp.name)
        self.assertEqual(self.assistant.tasks.get(task_id)["state"], "VERIFYING")

    def test_modified_plan_cannot_claim_approval(self):
        pending = self.assistant.process_text("prepare my workspace")
        changed = [self.assistant.validator.validate("create_folder", {"path": "Documents/other"})]
        result = self.assistant.execute_plan(changed, task_id=pending.data["task_id"])
        self.assertFalse(result.success)
        self.assertEqual(self.assistant.tasks.get(pending.data["task_id"])["state"], "AWAITING_APPROVAL")
        self.assertFalse((self.root / "Documents/other").exists())

    def test_reconciled_not_applied_deduplicated_step_is_safely_retried(self):
        plan = self.assistant.planner.validate({
            "type": "plan",
            "objective": "Ensure folder exists",
            "steps": [{"tool": "create_folder", "arguments": {"path": "Documents/retry"}}],
        }, user_request="ensure folder Documents/retry exists")
        task_id = self.assistant.tasks.create(
            plan, "text", user_request="ensure folder Documents/retry exists"
        )
        self.assistant.tasks.claim(task_id, plan, "text")
        self.assistant.tasks.start_step(task_id, 1)
        reconciled = self.assistant.reconcile_task(task_id)
        self.assertEqual(reconciled["state"], "PAUSED")
        self.assertEqual(reconciled["reconciled_steps"][0]["reconciled_state"], "FAILED")
        self.assertEqual(self.assistant.tasks.get(task_id)["steps"][0]["effect_state"], "NOT_STARTED")

        result = self.assistant.resume_task(task_id)
        self.assertTrue(result.success, result.text)
        self.assertEqual(result.status, "completed")
        self.assertTrue((self.root / "Documents" / "retry").is_dir())
        self.assertEqual(self.assistant.tasks.get(task_id)["steps"][0]["retry_count"], 1)

    def test_cli_reconciles_and_resumes_without_replaying_verified_effect(self):
        plan = self.assistant.planner.validate({
            "type": "plan",
            "objective": "Ensure folder exists",
            "steps": [{"tool": "create_folder", "arguments": {"path": "Documents/recovered"}}],
        }, user_request="ensure folder Documents/recovered exists")
        task_id = self.assistant.tasks.create(
            plan, "text", user_request="ensure folder Documents/recovered exists"
        )
        self.assistant.tasks.claim(task_id, plan, "text")
        self.assistant.tasks.start_step(task_id, 1)
        (self.root / "Documents" / "recovered").mkdir()
        reconciled = self.assistant.reconcile_task(task_id)
        self.assertEqual(reconciled["state"], "PAUSED")

        config = self.root / "resume.toml"
        config.write_text(
            f'data_dir = {json.dumps(str(self.root / "data"))}\n'
            f'[security]\nallowed_roots = [{json.dumps(str(self.root / "Documents"))}, '
            f'{json.dumps(str(self.root / "Downloads"))}]\n',
            encoding="utf-8",
        )
        command = [sys.executable, "-m", "hinata", "--config", str(config), "--no-llm",
                   "tasks", task_id, "--resume-paused"]
        resumed = subprocess.run(command, text=True, capture_output=True, timeout=15)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertIn('"status": "completed"', resumed.stdout)
        self.assertEqual(self.assistant.tasks.get(task_id)["state"], "COMPLETED")
        self.assertTrue((self.root / "Documents" / "recovered").is_dir())

    def test_expired_approval_blocks_resume(self):
        plan = self.assistant.planner.validate({
            "type": "plan",
            "objective": "Prepare workspace",
            "steps": [{"tool": "system_info", "arguments": {}}],
        }, user_request="check the system")
        task_id = self.assistant.tasks.create(plan, "text", user_request="check the system")
        self.assistant.tasks.claim(task_id, plan, "text")
        self.assertTrue(self.assistant.tasks.pause_task(task_id))
        approval = json.loads(self.assistant.tasks.get(task_id)["approval_json"])
        approval["expires_at_epoch"] = 0
        self.assistant.database.execute(
            "UPDATE agent_tasks SET approval_json=? WHERE id=?",
            (json.dumps(approval), task_id),
        )
        result = self.assistant.resume_task(task_id)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(self.assistant.tasks.get(task_id)["state"], "PAUSED")

    def test_verified_interrupted_step_is_skipped_and_approved_remainder_runs(self):
        plan = self.assistant.planner.validate({
            "type": "plan",
            "objective": "Prepare workspace",
            "steps": [
                {"step_id": "step_1", "tool": "create_folder", "arguments": {"path": "Documents/project"}},
                {"step_id": "step_2", "tool": "system_info", "arguments": {}, "depends_on": ["step_1"], "condition": "step_1.succeeded"},
            ],
        }, user_request="prepare my workspace")
        task_id = self.assistant.tasks.create(
            plan, "text", user_request="prepare my workspace"
        )
        self.assistant.tasks.claim(task_id, plan, "text")
        self.assistant.tasks.start_step(task_id, 1)
        # Simulate the side effect completing immediately before process termination.
        (self.root / "Documents" / "project").mkdir()
        self.assistant.close()
        self.assistant = make_assistant(self.tmp.name)
        reconciled = self.assistant.reconcile_task(task_id)
        self.assertEqual(reconciled["state"], "PAUSED")
        self.assertEqual(reconciled["reconciled_steps"][0]["reconciled_state"], "COMPLETED")

        executed = []
        original = self.assistant.orchestrator._run_tool
        def record(action):
            executed.append(action.tool)
            return original(action)
        self.assistant.orchestrator._run_tool = record
        result = self.assistant.resume_task(task_id)
        self.assertEqual(executed, ["system_info"])
        self.assertEqual(result.status, "verification_required")
        task = self.assistant.tasks.get(task_id)
        self.assertEqual(task["state"], "VERIFYING")
        self.assertEqual([step["state"] for step in task["steps"]], ["COMPLETED", "COMPLETED"])
        self.assertTrue((self.root / "Documents" / "project").is_dir())

    def test_interrupted_running_step_never_replayed_on_restart(self):
        pending = self.assistant.process_text("prepare my workspace")
        task_id = pending.data["task_id"]
        plan = self.assistant.planner.validate({"type": "plan", "objective": "Prepare workspace", "steps": [
            {"tool": a.tool, "arguments": a.arguments} for a in pending.plan_actions]})
        self.assistant.tasks.claim(task_id, plan, "text")
        self.assistant.tasks.start_step(task_id, 1)
        self.assistant.close()
        self.assistant = make_assistant(self.tmp.name)
        task = self.assistant.tasks.get(task_id)
        self.assertEqual(task["state"], "RUNNING")
        self.assertEqual(task["steps"][0]["state"], "RUNNING")
        self.assertFalse((self.root / "Documents/project").exists())
        self.assertFalse(self.assistant.execute_plan(pending.plan_actions, task_id=task_id).success)
        self.assertFalse((self.root / "Documents/project").exists())
        self.assertFalse(self.assistant.tasks.cancel_pending(task_id))

    def test_process_crash_after_side_effect_keeps_uncertain_checkpoint(self):
        script = r"""
import os, sys
from pathlib import Path
from tests.helpers import make_assistant
assistant = make_assistant(sys.argv[1])
plan = assistant.planner.validate({"type":"plan", "objective":"Create folder", "steps":[
    {"tool":"create_folder", "arguments":{"path":"Documents/crash"}}]})
task_id = assistant.tasks.create(plan, "text")
assistant.tasks.claim(task_id, plan, "text")
assistant.tasks.start_step(task_id, 1)
(Path(sys.argv[1]) / "Documents" / "crash").mkdir()
print(task_id, flush=True)
os._exit(17)
"""
        child = subprocess.run([sys.executable, "-c", script, self.tmp.name], capture_output=True, text=True, timeout=10)
        self.assertEqual(child.returncode, 17, child.stderr)
        task_id = child.stdout.strip()
        task = self.assistant.tasks.get(task_id)
        self.assertEqual(task["steps"][0]["state"], "RUNNING")
        self.assertTrue((self.root / "Documents/crash").is_dir())
        action = self.assistant.validator.validate("create_folder", {"path": "Documents/crash"})
        self.assertFalse(self.assistant.execute_plan([action], task_id=task_id).success)
        self.assertEqual(self.assistant.tasks.get(task_id)["state"], "RUNNING")

    def test_failure_stops_subsequent_steps_and_records_failure(self):
        pending = self.assistant.process_text("prepare my workspace")
        (self.root / "Documents/project").mkdir()
        result = self.assistant.execute_plan(pending.plan_actions, task_id=pending.data["task_id"])
        self.assertFalse(result.success)
        task = self.assistant.tasks.get(pending.data["task_id"])
        self.assertEqual(task["state"], "FAILED")
        self.assertEqual([step["state"] for step in task["steps"]], ["FAILED", "QUEUED"])

    def test_stop_during_final_action_is_cancelled_even_if_action_succeeds(self):
        self.assistant.llm.complete = lambda *args, **kwargs: (
            '{"type":"plan","objective":"Get system info",'
            '"steps":[{"tool":"system_info","arguments":{}}]}'
        )
        pending = self.assistant.process_text("prepare my workspace")
        entered, release = threading.Event(), threading.Event()
        def slow(action):
            entered.set()
            release.wait(3)
            return ActionResult(action.tool, True, "done")
        self.assistant.orchestrator._run_tool = slow
        results = []
        worker = threading.Thread(target=lambda: results.append(self.assistant.execute_plan(
            pending.plan_actions, task_id=pending.data["task_id"])))
        worker.start()
        self.assertTrue(entered.wait(2))
        self.assistant.emergency_stop()
        release.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0].status, "cancelled")
        self.assertEqual(self.assistant.tasks.get(pending.data["task_id"])["state"], "CANCELLED")

    def test_cancel_pending_and_clear_terminal_history(self):
        pending = self.assistant.process_text("prepare my workspace")
        task_id = pending.data["task_id"]
        self.assertTrue(self.assistant.tasks.cancel_pending(task_id))
        self.assertFalse(self.assistant.execute_plan(pending.plan_actions, task_id=task_id).success)
        self.assistant.memory.clear("commands")
        self.assertIsNone(self.assistant.tasks.get(task_id))


if __name__ == "__main__":
    unittest.main()
