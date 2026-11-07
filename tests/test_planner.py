import unittest

from hinata.core.planner import Planner, PlanningError
from hinata.security.action_validator import ActionValidator


class PlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = Planner(ActionValidator())

    def test_validates_bounded_plan_and_builds_human_preview(self) -> None:
        plan = self.planner.validate(
            {
                "type": "plan",
                "objective": "Prepare a project folder",
                "steps": [
                    {"tool": "create_folder", "arguments": {"path": "Documents/Project"}},
                    {"tool": "system_info", "arguments": {}},
                ],
            }
        )
        self.assertEqual(len(plan.steps), 2)
        self.assertIn("No steps have run", plan.preview())
        self.assertIn("Documents/Project", plan.preview())

    def test_rejects_confirmation_gated_tools_in_batch_plans(self) -> None:
        with self.assertRaises(PlanningError):
            self.planner.validate(
                {
                    "type": "plan",
                    "objective": "Type something",
                    "steps": [{"tool": "type_text", "arguments": {"text": "hello"}}],
                }
            )

    def test_rejects_oversized_or_malformed_plan(self) -> None:
        too_many = {
            "type": "plan",
            "objective": "Too many steps",
            "steps": [{"tool": "system_info", "arguments": {}} for _ in range(6)],
        }
        with self.assertRaises(PlanningError):
            self.planner.validate(too_many)
        with self.assertRaises(PlanningError):
            self.planner.validate(
                {
                    "type": "plan",
                    "objective": "Run code",
                    "steps": [{"tool": "shell", "arguments": {"command": "whoami"}}],
                }
            )


if __name__ == "__main__":
    unittest.main()
