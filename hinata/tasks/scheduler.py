"""Durable local offline task queue and bounded scheduler."""

from __future__ import annotations

import json
import logging
import math
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from hinata.core.planner import AUTONOMY_MODES
from hinata.database.database import Database
from hinata.security.redaction import redact_text, redact_value

LOGGER = logging.getLogger(__name__)


def validated_budget(value: Any) -> dict[str, int | float]:
    caps = {"max_iterations": 32, "max_model_calls": 50, "max_tool_calls": 64,
            "max_replans": 5, "max_tokens": 65536, "max_duration_seconds": 3600,
            "per_tool_timeout_seconds": 300}
    if not isinstance(value, dict) or set(value) - set(caps):
        raise ValueError("Unsupported schedule budget fields")
    for key, number in value.items():
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
            raise ValueError("Budget values must be finite numbers")
        if key.endswith("seconds"):
            if not 0 < number <= caps[key]:
                raise ValueError("Budget duration outside supported range")
        elif type(number) is not int or not 0 <= number <= caps[key]:
            raise ValueError("Budget counters must be bounded nonnegative integers")
    return value


class TaskScheduler:
    """Local scheduler for user-requested one-shot or recurring offline tasks.

    Safety rules:
    - Defaults to ``SUGGEST_ONLY`` unless the owner explicitly assigns a bounded
      autonomy mode.
    - Tasks flagged ``requires_online=True`` are skipped cleanly when Offline Mode
      is active.
    - Enforces hard ``max_runs`` and ``expires_epoch`` caps so recurring schedules
      never loop indefinitely without renewal.
    """

    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.ensure_migrated()
        self._stop = threading.Event()
        self._dispatch_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._thread: threading.Thread | None = None
        self.last_background_error: str = ""

    def schedule(
        self,
        *,
        title: str,
        request_text: str,
        run_at_epoch: float | None = None,
        interval_seconds: int = 0,
        max_runs: int = 1,
        expires_epoch: float | None = None,
        autonomy_mode: str = "SUGGEST_ONLY",
        requires_online: bool = False,
        budget: dict[str, Any] | None = None,
    ) -> str:
        if not title.strip() or not request_text.strip():
            raise ValueError("Scheduled task requires non-empty title and request_text")
        if len(title.strip()) > 160 or len(request_text.strip()) > 2000:
            raise ValueError("Schedule title/request exceeds 160/2000 characters; no truncation is allowed")
        if type(requires_online) is not bool:
            raise ValueError("requires_online must be a boolean")
        validated_budget({} if budget is None else budget)
        if autonomy_mode not in AUTONOMY_MODES:
            raise ValueError("Unsupported scheduled autonomy mode")
        if type(max_runs) is not int or max_runs < 1 or max_runs > 100:
            raise ValueError("Scheduled task max_runs must be between 1 and 100")
        if type(interval_seconds) is not int or not 0 <= interval_seconds <= 31_536_000:
            raise ValueError("interval_seconds must be an integer from 0 to 31536000")
        kind = "interval" if interval_seconds > 0 else "once"
        next_epoch = float(run_at_epoch if run_at_epoch is not None else time.time())
        if not math.isfinite(next_epoch):
            raise ValueError("Scheduled time must be finite")
        expires_epoch = next_epoch + 30 * 86400 if expires_epoch is None else expires_epoch
        if expires_epoch is not None and (
            not math.isfinite(expires_epoch) or expires_epoch <= next_epoch
        ):
            raise ValueError("Expiry must be finite and later than the first run")
        sched_id = uuid.uuid4().hex[:12]
        stored_request = request_text.strip()
        if self.database.vault and self.database.vault.enabled:
            stored_request = self.database.vault.protect_text(stored_request, context="schedule:" + sched_id)
        elif redact_text(stored_request) != stored_request:
            raise ValueError("Schedule contains secret-like text; enable encrypted storage or remove the secret")
        self.database.execute(
            """INSERT INTO scheduled_tasks(
                schedule_id, title, request_text, autonomy_mode, schedule_kind,
                next_run_epoch, interval_seconds, expires_epoch, enabled,
                requires_online, max_runs, budget_json
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (
                sched_id,
                redact_text(title.strip())[:160],
                stored_request,
                autonomy_mode,
                kind,
                next_epoch,
                int(interval_seconds),
                expires_epoch,
                int(requires_online),
                int(max_runs),
                json.dumps(budget or {}),
            ),
        )
        return sched_id

    def cancel(self, schedule_id: str) -> bool:
        with self.database.connect() as conn:
            cur = conn.execute(
                "UPDATE scheduled_tasks SET enabled = 0, last_status = 'cancelled', updated_at = datetime('now') WHERE schedule_id = ?",
                (schedule_id,),
            )
            return cur.rowcount == 1

    def get(self, schedule_id: str) -> dict[str, Any] | None:
        row = self.database.fetch_one("SELECT * FROM scheduled_tasks WHERE schedule_id = ?", (schedule_id,))
        return self._display(row) if row else None

    def _display(self, row: dict[str, Any]) -> dict[str, Any]:
        row = dict(row)
        text = str(row["request_text"])
        if text.startswith("enc:"):
            row["request_text"] = "[encrypted request; not displayed]"
        return redact_value(row)

    def attempts(self, schedule_id: str, *, offset: int = 0) -> list[dict[str, Any]]:
        return self.database.fetch_all("SELECT * FROM schedule_attempts WHERE schedule_id=? ORDER BY attempt DESC LIMIT 100 OFFSET ?",
                                       (schedule_id, max(0, offset)))

    def list_schedules(self, *, active_only: bool = False, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        rows = self.database.fetch_all(
            "SELECT * FROM scheduled_tasks " + ("WHERE enabled = 1 " if active_only else "") +
            "ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?", (max(1, min(limit, 100)), max(0, offset)),
        )
        return [self._display(row) for row in rows]

    def _run_with_deadline(self, runner: Callable[[dict[str, Any]], dict[str, Any]], row: dict[str, Any]) -> dict[str, Any]:
        limits = validated_budget(json.loads(str(row.get("budget_json") or "{}")))
        timeout = min(180.0, float(limits.get("max_duration_seconds", 180)))
        done = threading.Event()
        outcomes: list[dict[str, Any]] = []
        interrupted: list[BaseException] = []

        def execute() -> None:
            try:
                outcomes.append(runner(row))
            except Exception as exc:
                outcomes.append({"status": "error: " + type(exc).__name__, "success": False})
            except BaseException as exc:
                interrupted.append(exc)
            finally:
                done.set()

        self._worker = threading.Thread(target=execute, name="hinata-schedule-attempt", daemon=True)
        self._worker.start()
        if not done.wait(timeout):
            return {"status": "unknown_outcome", "success": False,
                    "review": {"message": "Runner exceeded deadline; worker may still be active. Reconcile effects before any retry."}}
        self._worker.join()
        if interrupted:
            raise interrupted[0]
        outcome = outcomes[0]
        if not isinstance(outcome, dict):
            raise ValueError("Schedule runner must return a structured outcome")
        return outcome

    def tick(self, runner: Callable[[dict[str, Any]], dict[str, Any]], *,
             now_epoch: float | None = None, online_mode_enabled: bool = False) -> list[dict[str, Any]]:
        if not self._dispatch_lock.acquire(blocking=False):
            return []
        try:
            if self._worker is not None and self._worker.is_alive():
                return []
            return self._tick(runner, now_epoch=now_epoch, online_mode_enabled=online_mode_enabled)
        finally:
            self._dispatch_lock.release()

    def _tick(
        self,
        runner: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        now_epoch: float | None = None,
        online_mode_enabled: bool = False,
    ) -> list[dict[str, Any]]:
        """Execute due scheduled items synchronously (usable in deterministic tests or background worker)."""
        now = float(now_epoch if now_epoch is not None else time.time())
        if not math.isfinite(now):
            raise ValueError("Scheduler time must be finite")
        due = self.database.fetch_all(
            "SELECT * FROM scheduled_tasks WHERE enabled = 1 AND next_run_epoch <= ? ORDER BY next_run_epoch ASC LIMIT 10",
            (now,),
        )
        dispatched: list[dict[str, Any]] = []
        for row in due:
            if self._worker is not None and self._worker.is_alive():
                break
            now = float(now_epoch if now_epoch is not None else time.time())
            sched_id = str(row["schedule_id"])
            if row["expires_epoch"] is not None and now >= float(row["expires_epoch"]):
                self.database.execute(
                    "UPDATE scheduled_tasks SET enabled = 0, last_status = 'expired', updated_at = datetime('now') WHERE schedule_id = ? AND enabled = 1 AND run_count = ?",
                    (sched_id, row["run_count"]),
                )
                continue
            if row["requires_online"] and not online_mode_enabled:
                # A task that needs the network cannot run while offline, but it
                # must not keep its place at the head of the due queue: the query
                # is LIMIT 10 ordered by next_run_epoch, so a task parked in the
                # past would be re-selected on every tick (forever) and could
                # crowd out tasks that *can* run. Push it forward by one interval
                # so the queue keeps moving; it is still due, just not first.
                resume_at = now + max(1, int(row["interval_seconds"]) if row["interval_seconds"] else 60)
                self.database.execute(
                    "UPDATE scheduled_tasks SET last_status = 'blocked_offline', "
                    "next_run_epoch = ?, updated_at = datetime('now') WHERE schedule_id = ? AND enabled = 1 AND run_count = ?",
                    (resume_at, sched_id, row["run_count"]),
                )
                dispatched.append(
                    {"schedule_id": sched_id, "status": "blocked_offline", "retry_at_epoch": resume_at}
                )
                continue

            # Reserve the attempt durably BEFORE any side effect. A second process
            # that selected the same row loses this compare-and-swap. A crash leaves
            # a disabled `running` record: never blindly replay an uncertain action.
            new_count = int(row["run_count"]) + 1
            with self.database.connect() as conn:
                claimed = conn.execute(
                    """UPDATE scheduled_tasks SET enabled = 0, last_status = 'running',
                       run_count = ?, updated_at = datetime('now')
                       WHERE schedule_id = ? AND enabled = 1 AND run_count = ?
                       AND run_count < max_runs AND next_run_epoch <= ? AND (expires_epoch IS NULL OR expires_epoch > ?)""",
                    (new_count, sched_id, row["run_count"], now, now),
                ).rowcount
                if claimed:
                    conn.execute("INSERT INTO schedule_attempts(schedule_id, attempt, started_epoch) VALUES(?, ?, ?)",
                                 (sched_id, new_count, now))
            if not claimed:
                continue
            try:
                execution_row = dict(row)
                if self.database.vault:
                    execution_row["request_text"] = self.database.vault.unprotect_text(str(row["request_text"]), context="schedule:" + sched_id)
                outcome = self._run_with_deadline(runner, execution_row)
                if not isinstance(outcome, dict) or not isinstance(outcome.get("status"), str):
                    # Missing evidence is not success and must not schedule a replay.
                    outcome = {"status": "unknown_outcome"}
            except Exception as exc:
                # A single failing task must not stop the queue. Record the
                # failure and park this job, while allowing the rest of the
                # queue to proceed. The exception may follow a side effect.
                LOGGER.warning("Scheduled task %s failed: %s", sched_id, type(exc).__name__)
                outcome = {"status": f"error: {type(exc).__name__}"[:60]}
            still_enabled = int(row["schedule_kind"] == "interval" and new_count < int(row["max_runs"]))
            finished = float(now_epoch if now_epoch is not None else time.time())
            next_run = finished + max(1, int(row["interval_seconds"])) if still_enabled else float(row["next_run_epoch"])
            status = str(outcome.get("status", "unknown_outcome"))
            # Only a known successful outcome can renew a recurring job. A
            # failure may have happened after a side effect; don't guess/retry it.
            if status.lower() != "completed" or outcome.get("success") is False:
                still_enabled = 0
            task_id = str(outcome.get("task_id", ""))
            self.database.execute(
                """UPDATE scheduled_tasks SET run_count = ?, enabled = ?, next_run_epoch = ?,
                   last_task_id = ?, last_status = ?, updated_at = datetime('now')
                   WHERE schedule_id = ? AND last_status = 'running' AND run_count = ?""",
                (new_count, still_enabled, next_run, task_id, status[:60], sched_id, new_count),
            )
            saved = self.database.fetch_one("SELECT last_status FROM scheduled_tasks WHERE schedule_id=?", (sched_id,))
            status = str(saved["last_status"]) if saved else "unknown_outcome"
            review_json = json.dumps(redact_value(outcome.get("review", {})), default=str)
            if len(review_json) > 4000:
                review_json = json.dumps({"truncated": True, "preview": review_json[:3000]})
            self.database.execute("UPDATE schedule_attempts SET finished_epoch=?, status=?, task_id=?, review_json=? WHERE schedule_id=? AND attempt=?",
                                  (finished, status, task_id, review_json, sched_id, new_count))
            dispatched.append({"schedule_id": sched_id, "status": status, "task_id": task_id})
        return dispatched

    def start_background(
        self,
        runner: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        poll_seconds: float = 5.0,
        online_mode_getter: Callable[[], bool] | None = None,
    ) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()

        def _loop() -> None:
            while not self._stop.wait(max(0.5, poll_seconds)):
                try:
                    online = online_mode_getter() if online_mode_getter else False
                    self.tick(runner, online_mode_enabled=online)
                except Exception as exc:
                    # A silent background failure leaves scheduled tasks pending
                    # with no explanation. Keep the loop alive but make it visible.
                    self.last_background_error = redact_text(str(exc))[:300]
                    LOGGER.error(
                        "Background scheduler tick failed: %s",
                        self.last_background_error,
                    )

        self._thread = threading.Thread(target=_loop, name="hinata-scheduler", daemon=True)
        self._thread.start()

    def stop_background(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        if self._thread is not None and not self._thread.is_alive():
            self._thread = None
