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

    def test_required_strings_cannot_be_whitespace_after_normalization(self) -> None:
        for tool, args in (
            ("search_files", {"query": "   "}),
            ("remember_preference", {"key": "theme", "value": "  "}),
            ("open_application", {"name": "  "}),
        ):
            with self.subTest(tool=tool), self.assertRaises(ValidationError):
                self.validator.validate(tool, args)
        self.assertEqual(self.validator.validate("type_text", {"text": "  "}).arguments["text"], "  ")

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

    def test_plain_prose_reply_is_used_as_chat(self) -> None:
        kind, payload = self.parser.parse_llm_plan(
            "Hello! How can I help you today?", "Hello"
        )
        self.assertEqual((kind, payload), ("chat", "Hello! How can I help you today?"))

    def test_code_fenced_prose_reply_is_used_as_chat(self) -> None:
        kind, payload = self.parser.parse_llm_plan(
            "```\nHello there! What would you like me to do?\n```", "hi"
        )
        self.assertEqual(kind, "chat")
        self.assertEqual(payload, "Hello there! What would you like me to do?")

    def test_commentary_around_json_is_tolerated(self) -> None:
        kind, payload = self.parser.parse_llm_plan(
            'Sure! Here you go: {"type":"chat","content":"Hi!"}', "hi"
        )
        self.assertEqual((kind, payload), ("chat", "Hi!"))

    def test_json_of_unknown_shape_salvages_readable_text(self) -> None:
        kind, payload = self.parser.parse_llm_plan('{"response":"Hi there!"}', "hi")
        self.assertEqual((kind, payload), ("chat", "Hi there!"))

    def test_broken_action_json_is_still_rejected(self) -> None:
        with self.assertRaises(PlanError):
            self.parser.parse_llm_plan('{"type":"tool","tool":"shell",arguments:{}}', "bad")

    def test_empty_model_output_is_rejected(self) -> None:
        with self.assertRaises(PlanError):
            self.parser.parse_llm_plan("   ", "hi")
        with self.assertRaises(PlanError):
            self.parser.parse_llm_plan("```\n```", "hi")


if __name__ == "__main__":
    unittest.main()
