"""Ultron controls and durable scheduler boundaries; no model/hardware required."""

from __future__ import annotations

import json
import math
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from hinata.config import AgenticConfig, ConfigError
from hinata.core.assistant import HinataAssistant
from hinata.core.types import AssistantResponse
from hinata.main import build_parser, main
from hinata.tasks.scheduler import TaskScheduler
from tests.helpers import make_assistant


class UltronAutomationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.assistant = make_assistant(self.directory.name)
        self.addCleanup(self.assistant.close)
        self.scheduler = self.assistant.scheduler

    def schedule(self, **kwargs):
        return self.scheduler.schedule(
            title="System check", request_text="system info", run_at_epoch=1,
            interval_seconds=10, max_runs=3, **kwargs,
        )

    def row(self, schedule_id):
        return next(r for r in self.scheduler.list_schedules() if r["schedule_id"] == schedule_id)

    def test_shared_startup_enables_ultron_by_default(self):
        assistant = make_assistant(self.directory.name, ultron_enabled=None)
        self.addCleanup(assistant.close)
        status = assistant.ultron_status()
        self.assertTrue(status["enabled"])
        self.assertEqual(status["thinking_mode"], "ultra")
        self.assertEqual(status["autonomy_mode"], "LOW_RISK_AUTONOMY")
        assistant.set_ultron_mode(False)
        self.assertEqual(assistant.thinking.mode, assistant.config.thinking.mode)
        self.assertEqual(assistant.orchestrator.autonomy_mode, assistant.config.agentic.autonomy_mode)

    def test_startup_config_opt_out_and_explicit_override(self):
        config = self.assistant.config
        config.agentic.ultron_enabled = False
        for override, expected in [(None, False), (True, True), (False, False)]:
            with self.subTest(override=override):
                assistant = HinataAssistant(config, llm_enabled=False, ultron_enabled=override)
                try:
                    self.assertEqual(assistant.ultron_status()["enabled"], expected)
                finally:
                    assistant.close()

    def test_startup_respects_offline_and_disabled_thinking(self):
        config = self.assistant.config
        config.security.strict_offline = True
        config.thinking.enabled = False
        assistant = HinataAssistant(config, llm_enabled=False)
        self.addCleanup(assistant.close)
        self.assertTrue(assistant.ultron_status()["enabled"])
        self.assertEqual(assistant.orchestrator.autonomy_mode, "OFFLINE_AUTONOMY")
        self.assertFalse(assistant.ultron_status()["deliberation_available"])

    def test_startup_flag_defaults_to_config_and_supports_opt_out(self):
        parser = build_parser()
        self.assertIsNone(parser.parse_args([]).ultron)
        self.assertIs(parser.parse_args(["--no-ultron"]).ultron, False)
        self.assertIs(parser.parse_args(["--ultron"]).ultron, True)

    def test_config_rejects_non_boolean_ultron_setting(self):
        with self.assertRaises(ConfigError):
            AgenticConfig(ultron_enabled="false").validate()

    def test_profile_is_reversible_idempotent_and_does_not_raise_budgets(self):
        before = self.assistant.ultron_status()
        self.assistant.set_ultron_mode(True)
        status = self.assistant.set_ultron_mode(True)
        self.assertTrue(status["enabled"])
        self.assertEqual(status["thinking_mode"], "ultra")
        self.assertEqual(status["autonomy_mode"], "LOW_RISK_AUTONOMY")
        self.assertFalse(status["deliberation_available"])  # --no-llm is honest
        self.assertEqual(status["limits"], before["limits"])
        restored = self.assistant.set_ultron_mode(False)
        self.assertEqual(restored, before)
        self.assertEqual(self.assistant.set_ultron_mode(False), before)

    def test_profile_respects_disabled_thinking_and_offline_policy(self):
        self.assistant.config.thinking.enabled = False
        self.assistant.config.security.strict_offline = True
        status = self.assistant.set_ultron_mode(True)
        self.assertFalse(self.assistant.orchestrator.thinking_enabled)
        self.assertFalse(status["deliberation_available"])
        self.assertEqual(status["autonomy_mode"], "OFFLINE_AUTONOMY")

    def test_profile_does_not_approve_sensitive_actions(self):
        self.assistant.set_ultron_mode(True)
        response = self.assistant.process_text("clear memory")
        self.assertFalse(response.success)
        self.assertIn(response.status, {"confirmation_required", "pin_required", "denied"})

    def test_profile_cannot_lift_lockdown(self):
        self.assistant.engage_lockdown("test")
        self.assistant.set_ultron_mode(True)
        response = self.assistant.process_text("create folder Documents/blocked")
        self.assertEqual(response.status, "denied")

    def test_existing_suggest_only_schedule_is_not_upgraded(self):
        sid = self.schedule()
        self.assistant.set_ultron_mode(True)
        with patch.object(self.assistant.orchestrator, "process", return_value=AssistantResponse("suggested")) as process:
            self.scheduler.tick(self.assistant.run_scheduled_task, now_epoch=2)
        self.assertEqual(process.call_args.args, ("system info",))
        self.assertEqual(process.call_args.kwargs["autonomy_mode"], "SUGGEST_ONLY")
        self.assertIn("budget_tracker", process.call_args.kwargs)
        self.assertEqual(self.row(sid)["autonomy_mode"], "SUGGEST_ONLY")

    def test_invalid_saved_policy_is_not_dispatched(self):
        with patch.object(self.assistant, "process_text") as process:
            result = self.assistant.run_scheduled_task({"request_text": "system info", "autonomy_mode": "ADMIN"})
        self.assertEqual(result["status"], "invalid")
        process.assert_not_called()

    def test_only_completed_jobs_recur_and_obey_run_cap(self):
        sid = self.schedule()
        runner = Mock(return_value={"status": "completed"})
        for now in [2, 12, 22, 32]:
            self.scheduler.tick(runner, now_epoch=now)
        self.assertEqual(runner.call_count, 3)
        self.assertEqual(self.row(sid)["run_count"], 3)
        self.assertFalse(self.row(sid)["enabled"])

    def test_review_and_uncertain_outcomes_park_without_replaying(self):
        for status in ["plan_confirmation_required", "confirmation_required", "pin_required",
                       "verification_required", "unknown_outcome", "failed", "cancelled", "unrecognized"]:
            with self.subTest(status=status):
                sid = self.schedule()
                runner = Mock(return_value={"status": status, "task_id": "review-me"})
                self.scheduler.tick(runner, now_epoch=2)
                self.scheduler.tick(runner, now_epoch=100)
                self.assertEqual(runner.call_count, 1)
                self.assertFalse(self.row(sid)["enabled"])
                self.assertEqual(self.row(sid)["last_task_id"], "review-me")

    def test_runner_error_does_not_recur_or_starve_other_jobs(self):
        sid = self.schedule()
        second = self.schedule()
        runner = Mock(side_effect=[RuntimeError("after a possible side effect"), {"status": "completed"}])
        reports = self.scheduler.tick(runner, now_epoch=2)
        self.assertEqual(len(reports), 2)
        self.assertFalse(self.row(sid)["enabled"])
        self.assertTrue(self.row(second)["enabled"])

    def test_malformed_runner_result_is_not_success(self):
        for result in [None, {}, {"status": None}, {"status": ""}]:
            with self.subTest(result=result):
                sid = self.schedule()
                self.scheduler.tick(lambda _, result=result: result, now_epoch=2)
                self.assertFalse(self.row(sid)["enabled"])

    def test_crash_reserves_attempt_and_restart_does_not_replay(self):
        sid = self.schedule()
        with self.assertRaises(KeyboardInterrupt):
            self.scheduler.tick(Mock(side_effect=KeyboardInterrupt), now_epoch=2)
        row = self.row(sid)
        self.assertEqual(row["last_status"], "running")
        self.assertEqual(row["run_count"], 1)
        self.assertFalse(row["enabled"])
        restarted = TaskScheduler(self.assistant.database)
        runner = Mock()
        self.assertEqual(restarted.tick(runner, now_epoch=100), [])
        runner.assert_not_called()

    def test_cancel_during_run_is_not_overwritten_or_reenabled(self):
        sid = self.schedule()

        def run(_):
            self.assertTrue(self.scheduler.cancel(sid))
            return {"status": "completed"}

        self.scheduler.tick(run, now_epoch=2)
        self.assertEqual(self.row(sid)["last_status"], "cancelled")
        self.assertFalse(self.row(sid)["enabled"])

    def test_two_workers_selecting_same_job_dispatch_only_once(self):
        sid = self.schedule()
        other = TaskScheduler(self.assistant.database)
        barrier = threading.Barrier(2)
        original_fetch = self.assistant.database.fetch_all

        def fetch(sql, *args):
            rows = original_fetch(sql, *args)
            if "next_run_epoch <=" in sql:
                barrier.wait(timeout=5)
            return rows

        runner = Mock(return_value={"status": "completed"})
        with patch.object(self.assistant.database, "fetch_all", side_effect=fetch):
            with ThreadPoolExecutor(max_workers=2) as pool:
                jobs = [pool.submit(s.tick, runner, now_epoch=2) for s in (self.scheduler, other)]
                results = [job.result(timeout=10) for job in jobs]
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(sum(map(len, results)), 1)
        self.assertEqual(self.row(sid)["run_count"], 1)

    def test_invalid_schedule_configuration_is_rejected(self):
        for changes in [
            {"autonomy_mode": "ROOT"}, {"max_runs": True}, {"max_runs": 0},
            {"interval_seconds": -1}, {"interval_seconds": 1.5},
            {"run_at_epoch": math.nan}, {"run_at_epoch": math.inf},
            {"expires_epoch": math.nan}, {"expires_epoch": 0},
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.scheduler.schedule(title="test", request_text="system info", **changes)

    def test_cli_options_are_explicit(self):
        args = build_parser().parse_args(["--ultron", "schedule", "--watch", "--poll-seconds", "2"])
        self.assertTrue(args.ultron)
        self.assertTrue(args.watch)
        self.assertEqual(args.autonomy, "SUGGEST_ONLY")
        self.assertEqual(args.poll_seconds, 2)

    def test_watch_runs_ticks_and_closes_cleanly_on_interrupt(self):
        with patch("hinata.main.HinataAssistant", return_value=self.assistant), \
             patch("hinata.main.load_config", return_value=self.assistant.config), \
             patch("hinata.main.time.sleep", side_effect=KeyboardInterrupt), \
             patch("builtins.print"), \
             patch.object(self.assistant.scheduler, "tick", return_value=[]) as tick:
            self.assertEqual(main(["--no-speak", "schedule", "--watch"]), 130)
        tick.assert_called_once()

    def test_status_is_json_serializable(self):
        self.assistant.set_ultron_mode(True)
        self.assertIn('"ultra"', json.dumps(self.assistant.ultron_status()))
