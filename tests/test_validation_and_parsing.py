import unittest

from hinata.core.intent_parser import IntentParser, PlanError
from hinata.security.action_validator import ActionValidator, ValidationError


class ValidationAndParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = ActionValidator()
        self.parser = IntentParser(self.validator)

    def test_specific_path_command_precedes_application_command(self) -> None:
        action = self.parser.parse("open folder Downloads")
        self.assertIsNotNone(action)
        self.assertEqual(action.tool, "open_folder")
        self.assertEqual(action.arguments, {"path": "Downloads"})

    def test_open_application_and_preserve_original(self) -> None:
        action = self.parser.parse("open notepad")
        self.assertEqual(action.tool, "open_application")
        self.assertEqual(action.original_text, "open notepad")

    def test_type_text_removes_matching_quotes_only(self) -> None:
        action = self.parser.parse('type "Hello, HINATA"')
        self.assertEqual(action.arguments["text"], "Hello, HINATA")

    def test_volume_validation(self) -> None:
        self.assertEqual(
            self.parser.parse("set volume to 35").arguments,
            {"action": "set", "value": 35},
        )
        with self.assertRaises(ValidationError):
            self.parser.parse("set volume to 900")

    def test_unknown_tool_and_arguments_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.validator.validate("shell", {"command": "whoami"})
        with self.assertRaises(ValidationError):
            self.validator.validate("open_application", {"name": "notepad", "command": "x"})
        with self.assertRaises(ValidationError):
            self.validator.validate("volume", {"action": "set"})

    def test_llm_json_plan_is_still_validated(self) -> None:
        kind, action = self.parser.parse_llm_plan(
            '```json\n{"type":"tool","tool":"open_application","arguments":{"name":"notepad"}}\n```',
            "please",
        )
        self.assertEqual(kind, "tool")
        self.assertEqual(action.original_text, "please")
        with self.assertRaises(PlanError):
            self.parser.parse_llm_plan(
                '{"type":"tool","tool":"shell","arguments":{"command":"calc"}}', "bad"
            )

    def test_llm_can_return_candidate_plan_for_separate_policy_validation(self) -> None:
        kind, candidate = self.parser.parse_llm_plan(
            '{"type":"plan","objective":"Inspect project",'
            '"steps":[{"tool":"system_info","arguments":{}}]}',
            "inspect my system",
        )
        self.assertEqual(kind, "plan")
        self.assertEqual(candidate["objective"], "Inspect project")

    def test_llm_chat_and_clarification(self) -> None:
        self.assertEqual(
            self.parser.parse_llm_plan('{"type":"chat","content":"Hello"}', "hi"),
            ("chat", "Hello"),
        )
        self.assertEqual(
            self.parser.parse_llm_plan('{"type":"clarify","content":"Which file?"}', "read"),
            ("clarify", "Which file?"),
        )


if __name__ == "__main__":
    unittest.main()
