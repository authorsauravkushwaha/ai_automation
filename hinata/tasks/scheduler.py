"""Durable local offline task queue and bounded scheduler."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from hinata.database.database import Database

LOGGER = logging.getLogger(__name__)


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
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

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
        if max_runs < 1 or max_runs > 100:
            raise ValueError("Scheduled task max_runs must be between 1 and 100")
        if interval_seconds < 0:
            raise ValueError("interval_seconds cannot be negative")
        kind = "interval" if interval_seconds > 0 else "once"
        next_epoch = float(run_at_epoch if run_at_epoch is not None else time.time())
        sched_id = uuid.uuid4().hex[:12]
        self.database.execute(
            """INSERT INTO scheduled_tasks(
                schedule_id, title, request_text, autonomy_mode, schedule_kind,
                next_run_epoch, interval_seconds, expires_epoch, enabled,
                requires_online, max_runs, budget_json
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (
                sched_id,
                title.strip()[:160],
                request_text.strip()[:2000],
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

    def list_schedules(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        if active_only:
            return self.database.fetch_all(
                "SELECT * FROM scheduled_tasks WHERE enabled = 1 ORDER BY next_run_epoch ASC"
            )
        return self.database.fetch_all("SELECT * FROM scheduled_tasks ORDER BY created_at DESC LIMIT 50")

    def tick(
        self,
        runner: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        now_epoch: float | None = None,
        online_mode_enabled: bool = False,
    ) -> list[dict[str, Any]]:
        """Execute due scheduled items synchronously (usable in deterministic tests or background worker)."""
        now = float(now_epoch if now_epoch is not None else time.time())
        due = self.database.fetch_all(
            "SELECT * FROM scheduled_tasks WHERE enabled = 1 AND next_run_epoch <= ? ORDER BY next_run_epoch ASC LIMIT 10",
            (now,),
        )
        dispatched: list[dict[str, Any]] = []
        for row in due:
            sched_id = str(row["schedule_id"])
            if row["expires_epoch"] is not None and now >= float(row["expires_epoch"]):
                self.database.execute(
                    "UPDATE scheduled_tasks SET enabled = 0, last_status = 'expired', updated_at = datetime('now') WHERE schedule_id = ?",
                    (sched_id,),
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
                    "next_run_epoch = ?, updated_at = datetime('now') WHERE schedule_id = ?",
                    (resume_at, sched_id),
                )
                dispatched.append(
                    {"schedule_id": sched_id, "status": "blocked_offline", "retry_at_epoch": resume_at}
                )
                continue

            try:
                outcome = runner(row)
            except Exception as exc:
                # A single failing task must not stop the queue. Record the
                # failure, advance the schedule like any other attempt, and keep
                # going: otherwise the row stays due, re-raises on every tick,
                # and every task behind it starves.
                LOGGER.warning("Scheduled task %s failed: %s", sched_id, exc)
                outcome = {"status": f"error: {type(exc).__name__}"[:60]}
            new_count = int(row["run_count"]) + 1
            still_enabled = int(row["schedule_kind"] == "interval" and new_count < int(row["max_runs"]))
            next_run = now + max(1, int(row["interval_seconds"])) if still_enabled else float(row["next_run_epoch"])
            status = str(outcome.get("status", "completed"))
            task_id = str(outcome.get("task_id", ""))
            self.database.execute(
                """UPDATE scheduled_tasks SET run_count = ?, enabled = ?, next_run_epoch = ?,
                   last_task_id = ?, last_status = ?, updated_at = datetime('now')
                   WHERE schedule_id = ?""",
                (new_count, still_enabled, next_run, task_id, status[:60], sched_id),
            )
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
                except Exception:
                    pass

        self._thread = threading.Thread(target=_loop, name="hinata-scheduler", daemon=True)
        self._thread.start()

    def stop_background(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self._thread = None
