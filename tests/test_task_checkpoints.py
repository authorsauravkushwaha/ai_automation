import tempfile
import subprocess
import sys
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
