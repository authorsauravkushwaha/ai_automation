"""The raw dispatcher must never be a way around the permission gate.

Before this guard, ``Orchestrator._run_tool`` performed no authorization at all:
the only check lived in ``_execute``. Any future internal caller — a plugin, an
agent helper, a refactor — could therefore wipe memory, delete files or power
off the machine by calling the dispatcher directly. That is exactly what
happened while auditing ``clear_memory``: the confirmed path refused it, and a
direct dispatch deleted the record with no confirmation and no PIN.
"""

from __future__ import annotations

import tempfile
import unittest

from hinata.core.permissions import POLICY, PermissionLevel
from hinata.core.tool_registry import DEFAULT_MANIFESTS
from hinata.core.types import ActionRequest
from tests.helpers import make_assistant


class DispatchAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.assistant = make_assistant(self.temporary.name)
        self.orchestrator = self.assistant.orchestrator

    def tearDown(self) -> None:
        self.assistant.close()
        self.temporary.cleanup()

    def _run(self, tool: str, arguments: dict | None = None):
        return self.orchestrator._run_tool(
            ActionRequest(tool=tool, arguments=arguments or {}, original_text=tool)
        )

    def test_sensitive_tool_is_refused_without_authorization(self) -> None:
        self.assistant.memory.add_message("user", "private medical note", "text")
        result = self._run("clear_memory", {"scope": "all"})
        self.assertFalse(result.success)
        self.assertIn("authorisation", result.message.lower())
        self.assertTrue(
            self.assistant.memory.search("private medical note"),
            "a direct dispatch must not delete memory",
        )

    def test_confirm_tier_tool_is_refused_without_authorization(self) -> None:
        result = self._run("save_identity", {"profile": "x", "fields_json": '{"email":"a@b.com"}'})
        self.assertFalse(result.success)

    def test_safe_tool_still_dispatches(self) -> None:
        result = self._run("system_info")
        self.assertTrue(result.success, result.message)

    def test_authorized_path_dispatches_a_sensitive_tool(self) -> None:
        self.assistant.memory.add_message("user", "note to delete", "text")
        result = self.orchestrator._run_authorized_tool(
            ActionRequest(tool="clear_memory", arguments={"scope": "all"}, original_text="x")
        )
        self.assertTrue(result.success, result.message)
        self.assertFalse(self.assistant.memory.search("note to delete"))

    def test_authorization_does_not_leak_after_the_call(self) -> None:
        self.orchestrator._run_authorized_tool(
            ActionRequest(tool="system_info", arguments={}, original_text="x")
        )
        self.assertEqual(self.orchestrator._authorization_depth, 0)
        self.assertFalse(self._run("clear_memory", {"scope": "all"}).success)

    def test_authorization_is_cleared_even_when_dispatch_raises(self) -> None:
        original = self.orchestrator._dispatch_tool

        def boom(_action):
            raise RuntimeError("handler exploded")

        self.orchestrator._dispatch_tool = boom
        try:
            with self.assertRaises(RuntimeError):
                self.orchestrator._run_authorized_tool(
                    ActionRequest(tool="system_info", arguments={}, original_text="x")
                )
        finally:
            self.orchestrator._dispatch_tool = original
        self.assertEqual(
            self.orchestrator._authorization_depth, 0,
            "a failed dispatch must not leave the stack armed for later calls",
        )

    def test_the_confirmed_path_still_works_end_to_end(self) -> None:
        self.assistant.memory.add_message("user", "keep this note", "text")
        response = self.assistant.process_text("clear memory")
        self.assertEqual(response.status, "confirmation_required")
        self.assertTrue(self.assistant.memory.search("keep this note"))

    def test_unknown_tool_is_not_treated_as_safe(self) -> None:
        self.assertFalse(self.orchestrator._is_safe_tool("a_tool_that_does_not_exist"))

    def test_guard_covers_the_whole_policy_table(self) -> None:
        """Every tool the POLICY calls confirm/sensitive must be guarded."""
        guarded = [
            name for name, level in POLICY.items() if level is not PermissionLevel.SAFE
        ]
        self.assertTrue(guarded)
        for name in guarded:
            with self.subTest(tool=name):
                self.assertFalse(
                    self.orchestrator._is_safe_tool(name),
                    f"{name} is {POLICY[name].value}-tier but the raw dispatcher allows it",
                )

    def test_every_manifest_tool_is_classified(self) -> None:
        missing = [m.name for m in DEFAULT_MANIFESTS if m.name not in POLICY]
        self.assertEqual(missing, [], "unclassified tools silently fall back to SENSITIVE")


if __name__ == "__main__":
    unittest.main()
