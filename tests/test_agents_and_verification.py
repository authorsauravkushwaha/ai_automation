import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from hinata.agents.workers import WORKERS, DelegationError
from hinata.core.types import ActionRequest, ActionResult
from hinata.tasks.store import TaskStateError
from tests.helpers import make_assistant


class AgentAndVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.assistant = make_assistant(self.tmp.name)
        self.root = Path(self.tmp.name) / "Documents"

    def tearDown(self):
        self.assistant.close()
        self.tmp.cleanup()

    def make_plan(self):
        self.assistant.orchestrator.llm_enabled = True
        self.assistant.llm.complete = lambda *args, **kwargs: (
            '{"type":"plan","objective":"Prepare a work area","steps":['
            '{"tool":"create_folder","arguments":{"path":"Documents/work"}},'
            '{"tool":"system_info","arguments":{}}]}'
        )
        return self.assistant.process_text("set up my work area")

    def test_tools_route_to_limited_workers_and_record_history(self):
        (self.root / "project.py").write_text("print(1)", encoding="utf-8")
        (self.root / "notes.txt").write_text("local notes", encoding="utf-8")
        self.assertTrue(self.assistant.process_text("read text file Documents/project.py").success)
        self.assertTrue(self.assistant.process_text("read text file Documents/notes.txt").success)
        self.assertTrue(self.assistant.process_text("system info").success)
        self.assertTrue(self.assistant.process_text("list preferences").success)
        events = self.assistant.orchestrator.workers.recent()
        self.assertEqual([event["worker"] for event in events[:4]], ["memory", "execution", "research", "coding"])
        self.assertTrue(all(event["status"] == "COMPLETED" for event in events[:4]))
        self.assertTrue(all("local notes" not in json.dumps(event) for event in events))
        self.assistant.close()
        self.assistant = make_assistant(self.tmp.name)
        self.assertGreaterEqual(len(self.assistant.orchestrator.workers.recent()), 4)

    def test_worker_cannot_bypass_role_or_user_approval(self):
        action = ActionRequest("clear_memory", {"scope": "all"})
        called = []
        with self.assertRaises(DelegationError):
            WORKERS["coding"].execute(action, lambda a: called.append(a))
        self.assertEqual(called, [])
        with self.assertRaises(DelegationError):
            WORKERS["execution"].execute(ActionRequest("system_info", {}), lambda action: ActionResult("volume", True, "wrong tool"))
        pending = self.assistant.process_text("remember that my editor is vim")
        self.assertTrue(pending.needs_confirmation)
        self.assertEqual(self.assistant.orchestrator.workers.recent(), [])
        self.assertTrue(self.assistant.resume(pending.action).success)
        self.assertEqual(self.assistant.orchestrator.workers.recent()[0]["worker"], "memory")

    def test_objective_requires_human_evidence_and_can_be_reviewed_after_restart(self):
        proposal = self.make_plan()
        result = self.assistant.execute_plan(proposal.plan_actions, task_id=proposal.data["task_id"])
        self.assertFalse(result.success)
        self.assertEqual(result.status, "verification_required")
        task_id = result.data["task_id"]
        task = self.assistant.tasks.get(task_id)
        self.assertEqual(task["state"], "VERIFYING")
        self.assertEqual(task["user_request"], "set up my work area")
        checks = json.loads(task["verification_json"])
        self.assertEqual([c["status"] for c in checks], ["verified", "unknown"])
        with self.assertRaises(ValueError):
            self.assistant.attest_task(task_id, "yes", satisfied=True)
        self.assistant.close()
        self.assistant = make_assistant(self.tmp.name)
        self.assertTrue(self.assistant.attest_task(task_id, "I inspected the created folder and system status.", satisfied=True))
        task = self.assistant.tasks.get(task_id)
        self.assertEqual(task["state"], "COMPLETED")
        self.assertIn("inspected", task["attestation"])
        with self.assertRaises(ValueError):
            self.assistant.attest_task(task_id, "I inspected everything again.", satisfied=True)

    def test_exact_folder_objective_is_automatically_verified_with_evidence(self):
        self.assistant.orchestrator.llm_enabled = True
        self.assistant.llm.complete = lambda *args, **kwargs: (
            '{"type":"plan","objective":"Make folder","steps":['
            '{"tool":"create_folder","arguments":{"path":"Documents/work"}}]}'
        )
        pending = self.assistant.process_text("ensure folder Documents/work exists")
        result = self.assistant.execute_plan(pending.plan_actions, task_id=pending.data["task_id"])
        self.assertTrue(result.success, result.text)
        self.assertEqual(result.status, "completed")
        task = self.assistant.tasks.get(result.data["task_id"])
        self.assertEqual(task["state"], "COMPLETED")
        self.assertEqual(task["verification_method"], "folder_postcondition")
        evidence = json.loads(task["objective_evidence"])
        self.assertEqual(evidence["path"], str(self.root / "work"))
        self.assistant.close()
        self.assistant = make_assistant(self.tmp.name)
        self.assertEqual(self.assistant.tasks.get(result.data["task_id"])["state"], "COMPLETED")

    def test_wrong_target_and_extra_steps_never_auto_complete(self):
        reviewer = self.assistant.orchestrator.reviewer
        (self.root / "work").mkdir()
        (self.root / "other").mkdir()
        work = self.assistant.validator.validate("create_folder", {"path": "Documents/work"})
        other = self.assistant.validator.validate("create_folder", {"path": "Documents/other"})
        self.assertIsNone(reviewer.verify_objective("ensure folder Documents/work exists", (other,)))
        self.assertIsNone(reviewer.verify_objective("ensure folder Documents/work exists", (work, other)))
        self.assertIsNone(reviewer.verify_objective("ensure folder Documents/work exists and email me", (work,)))
        self.assertIsNone(reviewer.verify_objective("ensure folder Documents/work exists and folder Documents/other exists", (work,)))
        self.assertIsNone(reviewer.verify_objective("set up my work area", (work,)))
        self.assertIsNone(reviewer.verify_objective("ensure folder ../outside exists", (work,)))

    def test_exact_multi_folder_objective_requires_matching_unique_existing_targets(self):
        reviewer = self.assistant.orchestrator.reviewer
        (self.root / "a").mkdir()
        (self.root / "b").mkdir()
        a = self.assistant.validator.validate("create_folder", {"path": "Documents/a"})
        b = self.assistant.validator.validate("create_folder", {"path": "Documents/b"})
        request = "ensure folders Documents/a, Documents/b exist"
        proof = reviewer.verify_objective(request, (b, a))
        self.assertEqual(proof["kind"], "folders_exist")
        self.assertEqual(proof["paths"], [str(self.root / "a"), str(self.root / "b")])
        self.assertIsNone(reviewer.verify_objective(request, (a, a)))
        self.assertIsNone(reviewer.verify_objective(request + " and send email", (a, b)))
        self.assertIsNone(reviewer.verify_objective("ensure folders Documents/a, Documents/a exist", (a, a)))
        self.assertIsNone(reviewer.verify_objective("ensure folders Documents/a, Documents/b exist", (a,)))
        (self.root / "b").rmdir()
        self.assertIsNone(reviewer.verify_objective(request, (a, b)))

    def test_multi_folder_objective_records_evidence(self):
        self.assistant.orchestrator.llm_enabled = True
        self.assistant.llm.complete = lambda *args, **kwargs: json.dumps({
            "type": "plan", "objective": "Prepare folders", "steps": [
                {"tool": "create_folder", "arguments": {"path": "Documents/a"}},
                {"tool": "create_folder", "arguments": {"path": "Documents/b"}}]})
        proposal = self.assistant.process_text("ensure folders Documents/a, Documents/b exist")
        result = self.assistant.execute_plan(proposal.plan_actions, task_id=proposal.data["task_id"])
        self.assertTrue(result.success, result.text)
        task = self.assistant.tasks.get(result.data["task_id"])
        self.assertEqual(task["state"], "COMPLETED")
        self.assertEqual(json.loads(task["objective_evidence"])["kind"], "folders_exist")

    def test_human_attestation_is_labelled_and_not_automatic(self):
        proposal = self.make_plan()
        result = self.assistant.execute_plan(proposal.plan_actions, task_id=proposal.data["task_id"])
        self.assertEqual(result.status, "verification_required")
        self.assertTrue(self.assistant.attest_task(result.data["task_id"], "I personally checked all the requested work.", satisfied=True))
        task = self.assistant.tasks.get(result.data["task_id"])
        self.assertEqual(task["verification_method"], "human")
        self.assertEqual(task["objective_evidence"], "")

    def test_removed_folder_blocks_attestation(self):
        proposal = self.make_plan()
        result = self.assistant.execute_plan(proposal.plan_actions, task_id=proposal.data["task_id"])
        self.assertEqual(result.status, "verification_required")
        (self.root / "work").rmdir()
        with self.assertRaisesRegex(ValueError, "postcondition"):
            self.assistant.attest_task(result.data["task_id"], "I inspected the result carefully.", satisfied=True)
        self.assertEqual(self.assistant.tasks.get(result.data["task_id"])["state"], "VERIFYING")
        self.assertTrue(self.assistant.attest_task(result.data["task_id"], "The expected folder is no longer present.", satisfied=False))
        self.assertEqual(self.assistant.tasks.get(result.data["task_id"])["state"], "FAILED")

    def test_review_detects_disappearing_folder_before_completion(self):
        proposal = self.make_plan()
        original = self.assistant.orchestrator._run_tool
        def tool(action):
            if action.tool == "system_info":
                (self.root / "work").rmdir()
            return original(action)
        self.assistant.orchestrator._run_tool = tool
        result = self.assistant.execute_plan(proposal.plan_actions, task_id=proposal.data["task_id"])
        self.assertFalse(result.success)
        self.assertEqual(result.status, "failed")
        self.assertEqual(self.assistant.tasks.get(result.data["task_id"])["state"], "FAILED")

    def test_cli_requires_explicit_evidence_and_confirmation(self):
        proposal = self.make_plan()
        result = self.assistant.execute_plan(proposal.plan_actions, task_id=proposal.data["task_id"])
        task_id = result.data["task_id"]
        config = Path(self.tmp.name) / "config.toml"
        config.write_text(f'data_dir = "{Path(self.tmp.name) / "data"}"\n[security]\nallowed_roots = ["{self.root}"]\n', encoding="utf-8")
        command = [sys.executable, "-m", "hinata", "--config", str(config), "--no-llm", "tasks", task_id, "--verify"]
        cancelled = subprocess.run(command, input="checked files and logs\nno\n", text=True, capture_output=True, timeout=10)
        self.assertEqual(cancelled.returncode, 1)
        self.assertEqual(self.assistant.tasks.get(task_id)["state"], "VERIFYING")
        accepted = subprocess.run(command, input="checked the created folder and system status\nyes\n", text=True, capture_output=True, timeout=10)
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertEqual(self.assistant.tasks.get(task_id)["state"], "COMPLETED")

    def test_legacy_completed_tasks_are_not_treated_as_verified(self):
        from hinata.database.database import Database
        from hinata.tasks.store import TaskStore
        path = Path(self.tmp.name) / "legacy.db"
        db = Database(path)
        with db.connect() as conn:
            conn.executescript("""
                CREATE TABLE agent_tasks(id TEXT PRIMARY KEY, objective TEXT NOT NULL, source TEXT NOT NULL,
                    state TEXT NOT NULL, created_at TEXT DEFAULT '', updated_at TEXT DEFAULT '',
                    parent_id TEXT, revision INTEGER NOT NULL DEFAULT 0);
                INSERT INTO agent_tasks(id, objective, source, state) VALUES('old', 'Old task', 'text', 'COMPLETED');
            """)
        store = TaskStore(db)
        self.assertEqual(store.get("old")["state"], "VERIFYING")
        self.assertEqual(store.get("old")["attestation"], "")
        self.assertEqual(TaskStore(db).get("old")["state"], "VERIFYING")

    def test_attestation_cannot_be_used_to_forge_mismatched_evidence(self):
        proposal = self.make_plan()
        result = self.assistant.execute_plan(proposal.plan_actions, task_id=proposal.data["task_id"])
        with self.assertRaises(TaskStateError):
            self.assistant.tasks.attest(result.data["task_id"], "I checked the final result.", satisfied=True,
                                        checks=[{"step": 1, "tool": "other", "status": "verified"}])
        self.assertEqual(self.assistant.tasks.get(result.data["task_id"])["state"], "VERIFYING")


if __name__ == "__main__":
    unittest.main()
