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

    def test_llm_plain_prose_is_used_as_a_chat_answer(self) -> None:
        self.assertEqual(
            self.parser.parse_llm_plan("Hello! I am HINATA, running locally.", "hi"),
            ("chat", "Hello! I am HINATA, running locally."),
        )

    def test_llm_unquoted_chat_content_and_plan_arguments_are_recovered(self) -> None:
        kind, text = self.parser.parse_llm_plan(
            '{"type": "chat", "content": Hello, this is local.}', "hi"
        )
        self.assertEqual(kind, "chat")
        self.assertEqual(text, "Hello, this is local.")
        kind, plan = self.parser.parse_llm_plan(
            '{"type":"plan","objective":"Check disk","steps":[{"tool":"system_info"}]}',
            "check disk",
        )
        self.assertEqual(kind, "plan")
        self.assertEqual(plan["steps"][0]["arguments"], {})

    def test_llm_json_inside_prose_or_fence_is_found(self) -> None:
        for reply in (
            'Sure, here you go: {"type":"chat","content":"Hi"}',
            '```json\n{"type":"chat","content":"Hi"}\n```',
            "{'type': 'chat', 'content': 'Hi'}",
        ):
            with self.subTest(reply=reply):
                self.assertEqual(self.parser.parse_llm_plan(reply, "hi"), ("chat", "Hi"))

    def test_empty_model_output_still_raises(self) -> None:
        with self.assertRaises(PlanError):
            self.parser.parse_llm_plan("   ", "hi")

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
