"""Contract checks prove bounded control flow, not general model correctness."""
from __future__ import annotations

import random
import unittest

from hinata.core.answer_contracts import answer_contract, computed_answer
from hinata.core.errors import BudgetTracker, CancellationToken, ClassifiedError, TaskBudget
from hinata.core.reasoning import ThinkingEngine
from tests.test_reasoning_engine import FakeLLM


class AnswerContractTests(unittest.TestCase):
    def solve(self, request):
        contract = answer_contract(request)
        return computed_answer(request, contract) if contract else None

    def test_arithmetic_generated_operands_not_benchmark_answers(self):
        rng = random.Random(4213)
        for _ in range(100):
            a, b = rng.randint(-9999, 9999), rng.randint(-9999, 9999)
            for op, expected in (("plus", a+b), ("minus", a-b), ("multiplied by", a*b)):
                request = f"What is {a} {op} {b}? Return only the integer."
                self.assertEqual(self.solve(request)[0], str(expected))

    def test_only_exact_integer_division(self):
        self.assertEqual(self.solve("Calculate 24 / 6. Return only an integer.")[0], "4")
        for text in ("Calculate 1 / 0.", "Calculate 5 / 2."):
            self.assertIsNone(self.solve(text + " Return only the integer."))

    def test_distinct_count_generated_literal_lists(self):
        rng = random.Random(12)
        for _ in range(100):
            items = [rng.choice(["item-a", "item-b", "delta", "omega", "খ"]) for _ in range(20)]
            request = "List: " + ", ".join(items) + ". How many distinct items are in this list? Return only the integer."
            self.assertEqual(self.solve(request)[0], str(len(set(items))))

    def test_list_tokens_are_case_sensitive(self):
        self.assertEqual(self.solve("List: A, a, A. How many unique items are in the list? Return only the integer.")[0], "2")

    def test_ambiguous_and_executable_inputs_are_not_computed(self):
        for text in (
            "What is 2 + 3 * 4?", "What is 3 ** 999999999999?", "What is __import__('os').system('echo x')?",
            "What is 9999999999999 * 2?", "What is 2 + 3? Actually subtract one.",
            "List: red, blue, green. How many unique warm colors are in this list?",
            "List: A, , B. How many unique items are in this list?",
            "List: 'a,b', c. How many unique items are in this list?",
        ):
            with self.subTest(text=text):
                # Semantic filters such as 'warm' must not be mistaken for a count of all tokens.
                self.assertIsNone(self.solve(text + " Return only the integer."))

    def test_oversize_request_is_not_parsed(self):
        self.assertIsNone(answer_contract("a"*4097 + " Return only the integer."))

    def test_explicit_formats_do_not_claim_truth(self):
        for request, good, bad in (
            ("State it. Return only the year.", "2041", "2041 or 2042"),
            ("State it. Return only YES or NO.", "NO", "Probably no"),
            ("State it. Return only the integer.", "-13", "13.5"),
            ("বছর কত? শুধু ইংরেজি অঙ্কে বছরটি লিখুন।", "2038", "২০৩৮"),
        ):
            contract = answer_contract(request)
            self.assertTrue(contract.accepts(good))
            self.assertFalse(contract.accepts(bad))
        self.assertIsNone(answer_contract("What is 3/2? Return only the number."))
        self.assertIsNone(answer_contract("Quote 'Return only the integer.' then explain."))


class AdaptiveReasoningTests(unittest.TestCase):
    def test_correct_short_answer_needs_no_extra_calls(self):
        for request, answer in (
            ("Is this approved? Return only YES or NO.", "NO"),
            ("What room? Return only the room name.", "Oak"),
            ("The record doesn't say. Return only the value.", "UNKNOWN"),
        ):
            llm = FakeLLM({"_default": "invented"})
            result = ThinkingEngine(llm, mode="ultra").think(request, draft_fn=lambda answer=answer: answer)
            self.assertEqual(result.text, answer)
            self.assertEqual(llm.calls, 0)
            self.assertEqual(result.trace.confidence, "unknown")
            self.assertIn("unverified", result.trace.verification_basis)

    def test_short_corrected_result_is_independently_verified(self):
        llm = FakeLLM({})
        result = ThinkingEngine(llm, mode="ultra").think(
            "Calculate 9 * 14. Return only the integer.", draft_fn=lambda: "121")
        self.assertEqual(result.text, "126")
        self.assertTrue(result.trace.revised)
        self.assertEqual(llm.calls, 0)
        self.assertEqual(result.trace.confidence, "high")
        self.assertIn("arithmetic", result.trace.verification_basis)

    def test_bad_format_gets_one_small_repair_not_recon_critique_loop(self):
        llm = FakeLLM({"format_repair": "2034"})
        result = ThinkingEngine(llm, mode="ultra").think(
            "The year is 2034. Return only the year.", draft_fn=lambda: "It is 2034.")
        self.assertEqual(result.text, "2034")
        self.assertEqual(llm.calls, 1)
        self.assertEqual(result.trace.model_calls, 1)
        self.assertEqual(result.trace.confidence, "unknown")

    def test_repair_cannot_invent_new_number(self):
        llm = FakeLLM({"format_repair": "2099"})
        result = ThinkingEngine(llm, mode="ultra").think(
            "The year is 2034. Return only the year.", draft_fn=lambda: "It is 2034.")
        self.assertEqual(result.text, "It is 2034.")
        self.assertTrue(result.trace.limits)

    def test_abstention_not_replaced_by_format_repair(self):
        llm = FakeLLM({"format_repair": "2034"})
        result = ThinkingEngine(llm, mode="ultra").think(
            "We asked in 2034. What year was it built? Return only the year.", draft_fn=lambda: "UNKNOWN")
        self.assertEqual(result.text, "UNKNOWN")

    def test_hallucinated_action_not_allowed_through_contract(self):
        result = ThinkingEngine(FakeLLM({}), mode="ultra").think(
            "What happened? Return only the value.", draft_fn=lambda: "I have already deleted the file.")
        self.assertNotIn("already deleted", result.text)

    def test_cancellation_before_fast_path_is_not_bypassed(self):
        token = CancellationToken()
        token.cancel("stop")
        with self.assertRaises(ClassifiedError):
            ThinkingEngine(FakeLLM({}), mode="ultra").think(
                "Calculate 2 + 2. Return only the integer.", draft_fn=lambda: "4", cancel_token=token)

    def test_repair_respects_zero_shared_model_budget(self):
        llm = FakeLLM({"format_repair": "2034"})
        result = ThinkingEngine(llm, mode="ultra").think(
            "The year is 2034. Return only the year.", draft_fn=lambda: "Year: 2034",
            budget=BudgetTracker(TaskBudget(max_model_calls=0)))
        self.assertEqual(llm.calls, 0)
        self.assertEqual(result.text, "Year: 2034")
        self.assertTrue(result.trace.limits)

    def test_off_and_quick_do_not_gain_unrequested_rewriting(self):
        for mode in ("off", "quick"):
            result = ThinkingEngine(FakeLLM({"critique": '{"critical_findings":[]}'}), mode=mode).think(
                "Calculate 2 + 2. Return only the integer.", draft_fn=lambda: "5")
            self.assertEqual(result.text, "5")

    def test_clean_general_answer_no_mandatory_revision_or_second_opinion(self):
        llm = FakeLLM({"recon": "An explanation", "critique": '{"critical_findings":[]}'})
        result = ThinkingEngine(llm, mode="ultra").think(
            "Explain what a calendar does for someone new to it", draft_fn=lambda: "It tracks dates.")
        self.assertEqual(result.text, "It tracks dates.")
        self.assertEqual(llm.calls, 2)
        self.assertFalse(result.trace.second_opinion_used)

    def test_unknown_cannot_be_overwritten_by_word_overlap(self):
        llm = FakeLLM({"recon": "room", "critique": '{"critical_findings":[{"title":"Room","severity":"gap","detail":"Add conference room to the answer."}]}',
                       "revise": "The meeting room is the conference room.", "second_opinion": "The conference room."})
        result = ThinkingEngine(llm, mode="ultra").think(
            "The meeting is Tuesday. Which room is the meeting in?", draft_fn=lambda: "UNKNOWN")
        self.assertEqual(result.text, "UNKNOWN")
        self.assertFalse(result.trace.revised)

    def test_strict_confidence_does_not_treat_valid_format_as_factual_proof(self):
        result = ThinkingEngine(FakeLLM({}), mode="ultra", require_min_confidence=True).think(
            "Who? Return only the name.", draft_fn=lambda: "Ada")
        self.assertIn("not have enough verified evidence", result.text)


class EvaluationGateTests(unittest.TestCase):
    def test_answer_regression_fails_even_without_runtime_error(self):
        from scripts.evaluate_ultron_local import quality_failures, summarize
        rows = [dict(case="test", repeat=0, mode=mode, passed=passed, seconds=1,
                     usage={"model_calls": 1}, trace={}) for mode, passed in (("off", True), ("ultra", False))]
        summary = summarize(rows)
        self.assertEqual(summary["runtime_errors"], 0)
        self.assertEqual(len(summary["regressions"]), 1)
        self.assertTrue(quality_failures(summary, max_regressions=0))

    def test_contained_stage_failure_does_not_count_as_clean_execution(self):
        from scripts.evaluate_ultron_local import quality_failures, summarize
        rows = [dict(case="test", repeat=0, mode="ultra", passed=True, seconds=1,
                     usage={"model_calls": 1}, trace={"stage_errors": ["format_repair"]})]
        self.assertTrue(quality_failures(summarize(rows)))

    def test_improvement_and_absolute_quality_gate(self):
        from scripts.evaluate_ultron_local import quality_failures, summarize
        rows = [dict(case="test", repeat=0, mode=mode, passed=passed, seconds=1,
                     usage={"model_calls": 1}, trace={}) for mode, passed in (("off", False), ("ultra", True))]
        summary = summarize(rows)
        self.assertEqual(len(summary["improvements"]), 1)
        self.assertFalse(quality_failures(summary, max_regressions=0, min_accuracy=1.0))
