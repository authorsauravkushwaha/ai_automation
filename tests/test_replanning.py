import tempfile
import unittest
from pathlib import Path

from tests.helpers import make_assistant


class ReplanningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.assistant = make_assistant(self.tmp.name)
        self.assistant.orchestrator.llm_enabled = True
        self.root = Path(self.tmp.name) / "Documents"
        (self.root / "exists").mkdir()
        self.calls = []
        self.initial = ('{"type":"plan","objective":"Prepare folder","steps":'
                        '[{"tool":"create_folder","arguments":{"path":"Documents/exists"}}]}')
        self.revised = ('{"type":"plan","objective":"Use alternative folder","steps":'
                        '[{"tool":"create_folder","arguments":{"path":"Documents/alternative"}}]}')
        def model(*args, **kwargs):
            self.calls.append((args, kwargs))
            return self.initial if len(self.calls) == 1 else self.revised
        self.assistant.llm.complete = model

    def tearDown(self):
        self.assistant.close()
        self.tmp.cleanup()

    def test_replan_is_bounded_and_requires_fresh_approval(self):
        initial = self.assistant.process_text("prepare a project folder")
        result = self.assistant.execute_plan(initial.plan_actions, task_id=initial.data["task_id"])
        self.assertTrue(result.needs_plan_confirmation)
        self.assertFalse((self.root / "alternative").exists())
        parent = self.assistant.tasks.get(initial.data["task_id"])
        child = self.assistant.tasks.get(result.data["task_id"])
        self.assertEqual(parent["state"], "FAILED")
        self.assertEqual(child["parent_id"], parent["id"])
        self.assertEqual(child["revision"], 1)
        self.assertEqual(child["state"], "AWAITING_APPROVAL")
        self.assertFalse(self.calls[-1][1]["pull_missing"])
        self.assertEqual(self.calls[-1][0][1], [])
        final = self.assistant.execute_plan(result.plan_actions, task_id=result.data["task_id"])
        self.assertEqual(final.status, "verification_required")
        self.assertEqual(child["user_request"], "prepare a project folder")
        self.assertTrue((self.root / "alternative").is_dir())

    def test_repeated_action_or_unsafe_tool_is_rejected(self):
        for replacement in [self.initial, '{"type":"plan","objective":"bad","steps":[{"tool":"clear_memory","arguments":{"scope":"all"}}]}']:
            with self.subTest(replacement=replacement):
                self.calls.clear()
                self.revised = replacement
                initial = self.assistant.process_text("prepare folder")
                result = self.assistant.execute_plan(initial.plan_actions, task_id=initial.data["task_id"])
                self.assertFalse(result.needs_plan_confirmation)
                self.assertEqual(result.status, "failed")

    def test_second_failure_does_not_propose_another_revision(self):
        (self.root / "alternative").mkdir()
        initial = self.assistant.process_text("prepare folder")
        revised = self.assistant.execute_plan(initial.plan_actions, task_id=initial.data["task_id"])
        self.assertTrue(revised.needs_plan_confirmation)
        final = self.assistant.execute_plan(revised.plan_actions, task_id=revised.data["task_id"])
        self.assertEqual(final.status, "failed")
        self.assertEqual(len(self.calls), 2)


if __name__ == "__main__":
    unittest.main()
