"""Regression tests for the Part B structural fixes in docs/WEAKNESS_AUDIT.md.

* B2 — ``Orchestrator._run_tool`` split into domain handlers without behaviour change
* B3 — silent ``except: pass`` sites now log
* B4 — expiry/lease timestamps are normalised to one comparable format
* B5 — duplicated Qt animation lifecycle collapsed into one mixin
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import re
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from hinata.agents.registry import AgentMessage, AgentRegistry
from hinata.core.memory import Memory
from hinata.core.orchestrator import Orchestrator
from hinata.core.planner import ExecutionPlan
from hinata.core.tool_registry import DEFAULT_MANIFESTS
from hinata.core.types import ActionRequest
from hinata.database.database import Database
from hinata.database.timestamps import (
    SQLITE_DATETIME,
    sqlite_epoch_compare,
    to_sqlite_utc,
    to_sqlite_utc_or_raise,
    utc_now_sqlite,
)
from hinata.tasks.store import TaskStore

REPO = Path(__file__).resolve().parent.parent


class ToolDispatchSplitTests(unittest.TestCase):
    """B2: the 860-line ladder must stay a thin router forever."""

    def test_run_tool_is_now_a_thin_router(self) -> None:
        source = inspect.getsource(Orchestrator._run_tool)
        size = len(source.splitlines())
        self.assertLess(size, 300, f"_run_tool grew back to {size} lines")

    def _all_dispatch_source(self) -> str:
        """The dispatch router plus every domain handler."""
        parts = [inspect.getsource(Orchestrator._dispatch_tool)]
        for name in dir(Orchestrator):
            if name.startswith("_handle_"):
                parts.append(inspect.getsource(getattr(Orchestrator, name)))
        return "\n".join(parts)

    def test_every_manifest_tool_dispatches(self) -> None:
        """A tool is dispatched if it appears in a `tool ==` or `tool in {...}` test."""
        text = self._all_dispatch_source()
        dispatched: set[str] = set(re.findall(r'tool\s*==\s*"([a-z0-9_]+)"', text))
        for group in re.findall(r"tool\s+in\s*\{([^}]*)\}", text, re.S):
            dispatched.update(
                token.strip().strip("\"'") for token in group.replace("\n", " ").split(",")
                if token.strip()
            )
        missing = {m.name for m in DEFAULT_MANIFESTS} - dispatched
        self.assertEqual(missing, set(), f"manifest tools with no dispatch: {sorted(missing)}")

    def test_router_targets_real_handlers(self) -> None:
        router = inspect.getsource(Orchestrator._dispatch_tool)
        targets = set(re.findall(r"self\.(_handle_\w+)\(", router))
        self.assertTrue(targets)
        for target in targets:
            self.assertTrue(hasattr(Orchestrator, target), target)

    def test_handlers_exist_for_every_domain_and_are_bounded(self) -> None:
        expected = {
            "_handle_automation", "_handle_files", "_handle_web", "_handle_identity",
            "_handle_knowledge", "_handle_memory", "_handle_system", "_handle_media",
            "_handle_workspace", "_handle_checks", "_handle_research",
            "_handle_communications", "_handle_devtools",
        }
        found = {name for name in dir(Orchestrator) if name.startswith("_handle_")}
        self.assertEqual(found, expected)
        for name in expected:
            size = len(inspect.getsource(getattr(Orchestrator, name)).splitlines())
            self.assertLess(size, 400, f"{name} is {size} lines")

    def test_unknown_tool_still_raises(self) -> None:
        from hinata.core.types import ActionRequest

        source = inspect.getsource(Orchestrator._dispatch_tool)
        self.assertIn("No executor is registered", source)
        self.assertIn("raise AutomationError", source)
        self.assertIsNotNone(ActionRequest)


class TimestampNormalisationTests(unittest.TestCase):
    """B4: one comparable timestamp format for every stored date."""

    def test_isoformat_is_converted_to_sqlite_form(self) -> None:
        value = "2026-11-25T17:33:59.001668+00:00"
        self.assertEqual(to_sqlite_utc(value), "2026-11-25 17:33:59")

    def test_t_separator_no_longer_leaks_into_storage(self) -> None:
        normalized = to_sqlite_utc("2030-01-01T00:00:00")
        self.assertNotIn("T", normalized or "")
        dt.datetime.strptime(normalized or "", SQLITE_DATETIME)  # exact format

    def test_offsets_are_converted_to_utc(self) -> None:
        # 23:50 at -05:00 is 04:50 UTC the next day.
        self.assertEqual(
            to_sqlite_utc("2026-11-30T23:50:00-05:00"), "2026-12-01 04:50:00"
        )

    def test_z_suffix_is_accepted(self) -> None:
        self.assertEqual(to_sqlite_utc("2026-07-01T12:00:00Z"), "2026-07-01 12:00:00")

    def test_naive_input_is_treated_as_utc_not_local(self) -> None:
        # Same text in, same text out regardless of the machine timezone.
        self.assertEqual(to_sqlite_utc("2026-05-05 05:05:05"), "2026-05-05 05:05:05")

    def test_already_canonical_is_idempotent(self) -> None:
        canonical = "2026-03-04 05:06:07"
        self.assertEqual(to_sqlite_utc(canonical), canonical)

    def test_invalid_and_empty_input(self) -> None:
        self.assertIsNone(to_sqlite_utc("tomorrow"))
        self.assertIsNone(to_sqlite_utc(""))
        self.assertIsNone(to_sqlite_utc(None))
        with self.assertRaises(ValueError):
            to_sqlite_utc_or_raise("next tuesday", field="expires_at")

    def test_utc_now_is_sqlite_formatted(self) -> None:
        parsed = dt.datetime.strptime(utc_now_sqlite(), SQLITE_DATETIME)
        self.assertLess(abs((dt.datetime.utcnow() - parsed).total_seconds()), 5)

    def test_epoch_columns_compare_numerically(self) -> None:
        expression = sqlite_epoch_compare("expires_epoch")
        self.assertIn("strftime('%s','now')", expression)
        self.assertIn("CAST(", expression)


class MemoryExpiryTests(unittest.TestCase):
    """B4, end to end: an expired memory must never be served."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temporary.name) / "memory.db")
        self.memory = Memory(self.db)
        self.now = dt.datetime.now(dt.UTC)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _visible(self, index: int, label: str, expires_at: str) -> bool:
        token = f"zqx{index}wvu"
        record = self.memory.propose_record(
            memory_type="semantic",
            content=f"{token} {label}",
            normalized_key=f"key{index}",
            approval_status="approved",
            expires_at=expires_at,
        )
        self.db.execute(
            "UPDATE memory_records SET approval_status='approved' WHERE memory_id=?",
            (record["memory_id"],),
        )
        visible = {r["memory_id"] for r in self.memory.retrieve_relevant(f"{token} {label}")}
        return record["memory_id"] in visible

    def test_expired_isoformat_memory_is_hidden(self) -> None:
        expires = (self.now - dt.timedelta(minutes=5)).isoformat()
        self.assertFalse(self._visible(0, "expired isoformat", expires))

    def test_expired_canonical_memory_is_hidden(self) -> None:
        expires = (self.now - dt.timedelta(days=1)).strftime(SQLITE_DATETIME)
        self.assertFalse(self._visible(1, "expired canonical", expires))

    def test_expired_offset_memory_is_hidden(self) -> None:
        expires = (self.now - dt.timedelta(hours=2)).astimezone(
            dt.timezone(dt.timedelta(hours=5, minutes=30))
        ).isoformat()
        self.assertFalse(self._visible(2, "expired offset", expires))

    def test_future_memories_remain_visible(self) -> None:
        cases = [
            (self.now + dt.timedelta(days=30)).isoformat(),
            (self.now + dt.timedelta(days=1)).astimezone(
                dt.timezone(dt.timedelta(hours=2))
            ).isoformat(),
            (self.now + dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            (self.now + dt.timedelta(hours=2)).strftime(SQLITE_DATETIME),
        ]
        for index, expires in enumerate(cases, start=10):
            with self.subTest(expires=expires):
                self.assertTrue(self._visible(index, f"future {index}", expires))

    def test_unparseable_expiry_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.memory.propose_record(
                memory_type="semantic", content="bad expiry", expires_at="next tuesday"
            )

    def test_blank_expiry_means_no_expiry(self) -> None:
        record = self.memory.propose_record(
            memory_type="semantic", content="no expiry probe", expires_at="   "
        )
        self.assertTrue(record["memory_id"])


class AnimationLifecycleMixinTests(unittest.TestCase):
    """B5: one mixin owns the start/stop lifecycle for every animated widget."""

    def _mixin(self):
        source = (REPO / "hinata/interface/canvas.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        node = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.ClassDef) and n.name == "_AnimationLifecycleMixin"
        )
        namespace: dict[str, object] = {"Any": object}
        exec(compile(textwrap.dedent(ast.get_source_segment(source, node) or ""),
                     "<mixin>", "exec"), namespace)
        return namespace["_AnimationLifecycleMixin"], tree

    def test_timer_stops_when_hidden_and_restarts_on_show(self) -> None:
        mixin, _ = self._mixin()

        class Timer:
            def __init__(self) -> None:
                self.active = False
                self.starts = 0

            def isActive(self) -> bool:
                return self.active

            def start(self) -> None:
                self.active = True
                self.starts += 1

            def stop(self) -> None:
                self.active = False

        class WidgetBase:
            """Stands in for QWidget: the mixin must call through to it."""

            def __init__(self) -> None:
                self.super_calls: list[str] = []

            def showEvent(self, event: object) -> None:
                self.super_calls.append("show")

            def hideEvent(self, event: object) -> None:
                self.super_calls.append("hide")

        class Widget(mixin, WidgetBase):  # type: ignore[misc,valid-type]
            def __init__(self) -> None:
                super().__init__()
                self._timer = Timer()

        widget = Widget()
        widget._timer.start()
        widget.hideEvent(object())
        self.assertFalse(widget._timer.isActive())
        widget.showEvent(object())
        self.assertTrue(widget._timer.isActive())
        widget.showEvent(object())
        self.assertEqual(widget._timer.starts, 2, "must not double-start")
        self.assertEqual(widget.super_calls, ["hide", "show", "show"])

    def test_both_widgets_inherit_the_mixin_and_define_no_local_copy(self) -> None:
        _, tree = self._mixin()
        for name in ("SceneCanvas", "OrbLogo"):
            node = next(
                n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == name
            )
            bases = [b.id if isinstance(b, ast.Name) else getattr(b, "attr", "") for b in node.bases]
            methods = {n.name for n in node.body if isinstance(n, ast.FunctionDef)}
            self.assertIn("_AnimationLifecycleMixin", bases, name)
            self.assertNotIn("showEvent", methods, name)
            self.assertNotIn("hideEvent", methods, name)


class SilentFailureLoggingTests(unittest.TestCase):
    """B3: previously invisible best-effort failures now emit a log record."""

    def test_registry_and_jarvis_modules_have_loggers(self) -> None:
        for relative in ("hinata/agents/registry.py", "hinata/core/jarvis_models.py"):
            source = (REPO / relative).read_text(encoding="utf-8")
            self.assertIn("LOGGER = logging.getLogger(__name__)", source, relative)
            self.assertIn("import logging", source, relative)

    def test_report_persistence_failure_is_logged(self) -> None:
        source = (REPO / "hinata/agents/registry.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            body = node.body
            if len(body) == 1 and isinstance(body[0], ast.Pass):
                continue
            segment = ast.get_source_segment(source, node) or ""
            if "record_report" in (ast.get_source_segment(source, node.parent) or "") \
                    if hasattr(node, "parent") else False:
                found = True
            if "LOGGER.warning" in segment or "LOGGER.info" in segment:
                found = True
        self.assertTrue(found)
        self.assertIn("Could not persist failure report", source)

    def test_identity_vault_sync_failure_is_logged(self) -> None:
        source = (REPO / "hinata/core/orchestrator.py").read_text(encoding="utf-8")
        self.assertIn("identity-vault sync failed", source)
        self.assertIn("personality name is authoritative", source)


if __name__ == "__main__":
    unittest.main()


class WallClockEnforcementTests(unittest.TestCase):
    """B4: float-epoch columns must never be compared with text 'now'.

    ``CAST(numeric_column AS REAL) > CAST(datetime('now') AS REAL)`` is always
    false because datetime() yields text. Every one of these checks used that
    shape, so the deadline/lease was written and then never enforced.
    """

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.database = Database(self.root / "task.db")
        self.database.initialize()

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _plan(self):
        return ExecutionPlan(
            objective="probe",
            steps=(ActionRequest(tool="system_info", arguments={}, original_text="sysinfo"),),
        )

    def _running_task_with_lease(self, store, lease_epoch: float | None) -> str:
        plan = self._plan()
        task_id = store.create(plan, source="test")
        store.claim(task_id, source="test", plan=plan, lease_seconds=60)
        if lease_epoch is not None:
            self.database.execute(
                "UPDATE agent_tasks SET lease_expires_at = ? WHERE id = ?",
                (str(lease_epoch), task_id),
            )
        return task_id

    def test_epoch_column_compared_numerically_not_as_text(self) -> None:
        """SQLite coerces datetime('now') to its leading digits (the year).

        So ``epoch > datetime('now')`` really means ``1.76e9 > 2026.0``, which
        is always true: every lease and deadline looked permanently live.
        """
        expired_epoch = str(round(time.time() - 300, 3))  # 5 minutes ago
        row = self.database.fetch_one(
            "SELECT CAST(? AS REAL) > CAST(strftime('%s','now') AS REAL) AS numeric_cmp,"
            " CAST(? AS REAL) > CAST(datetime('now') AS REAL) AS text_cmp",
            (expired_epoch, expired_epoch),
        )
        assert row is not None
        self.assertEqual(row["numeric_cmp"], 0, "numeric compare sees the lease as expired")
        self.assertEqual(row["text_cmp"], 1, "text compare wrongly reports it as still live")

    def test_live_lease_is_not_expired(self) -> None:
        store = TaskStore(self.database)
        task_id = self._running_task_with_lease(store, time.time() + 120)
        self.assertFalse(store.lease_expired(task_id))
        self.assertEqual(store.reclaim_expired_leases(), [])

    def test_expired_lease_is_reclaimed_to_paused(self) -> None:
        store = TaskStore(self.database)
        task_id = self._running_task_with_lease(store, time.time() - 5)
        self.assertTrue(store.lease_expired(task_id))
        self.assertEqual(store.reclaim_expired_leases(), [task_id])
        row = store.get(task_id)
        assert row is not None
        self.assertEqual(row["state"], "PAUSED")
        self.assertEqual(row["error_code"], "LEASE_EXPIRED")

    def test_dead_worker_cannot_renew_an_expired_lease(self) -> None:
        store = TaskStore(self.database)
        task_id = self._running_task_with_lease(store, time.time() - 5)
        self.assertFalse(store.heartbeat(task_id, lease_seconds=60))

    def test_live_worker_can_renew_its_lease(self) -> None:
        store = TaskStore(self.database)
        task_id = self._running_task_with_lease(store, time.time() + 120)
        self.assertTrue(store.heartbeat(task_id, lease_seconds=60))

    def test_unreadable_lease_is_treated_as_expired(self) -> None:
        store = TaskStore(self.database)
        task_id = self._running_task_with_lease(store, None)
        self.database.execute(
            "UPDATE agent_tasks SET lease_expires_at = 'not-a-number' WHERE id = ?", (task_id,)
        )
        self.assertTrue(store.lease_expired(task_id))

    def test_empty_lease_never_expires(self) -> None:
        store = TaskStore(self.database)
        task_id = self._running_task_with_lease(store, None)
        self.database.execute(
            "UPDATE agent_tasks SET lease_expires_at = '' WHERE id = ?", (task_id,)
        )
        self.assertFalse(store.lease_expired(task_id))

    def _record(self, registry, message_id: str, deadline: float | None) -> None:
        registry.record_message(
            AgentMessage(
                message_id=message_id,
                sender_id="planner",
                recipient_id="executor",
                root_task_id="root-1",
                child_task_id="child-1",
                correlation_id="corr-1",
                message_type="result",
                trust_label="trusted_runtime_metadata",
                payload={"text": message_id},
                run_id="run-1",
                deadline_epoch=deadline,
            )
        )

    def test_expired_agent_reply_is_withheld(self) -> None:
        registry = AgentRegistry(self.database)
        self._record(registry, "fresh", time.time() + 300)
        self._record(registry, "stale", time.time() - 300)
        self._record(registry, "no-deadline", None)
        visible = {m["message_id"] for m in registry.messages_for_task("root-1")}
        self.assertEqual(visible, {"fresh", "no-deadline"})
        expired = {m["message_id"] for m in registry.expired_messages_for_task("root-1")}
        self.assertEqual(expired, {"stale"})
        everything = {
            m["message_id"] for m in registry.messages_for_task("root-1", include_expired=True)
        }
        self.assertEqual(everything, {"fresh", "stale", "no-deadline"})
