"""Agent Control Center PySide6 dialog providing unified visibility across all
agentic subsystems: Dashboard & Metrics, Task Board & Plan Graph, Specialist Agents,
Model & Hardware Profiler, Structured Memory Explorer, Knowledge Library,
Coding Workspaces & Patch Diffs, and Offline Security Center.
"""

from __future__ import annotations

import json
from typing import Any

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from hinata.core.assistant import HinataAssistant
from hinata.core.planner import AUTONOMY_MODES


class AgentControlCenterDialog(QDialog):
    """Unified multi-tab inspection and governance center for HINATA's agentic runtime."""

    def __init__(self, assistant: HinataAssistant, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.assistant = assistant
        self.setWindowTitle("HINATA Agent Control Center")
        self.resize(880, 620)

        outer = QVBoxLayout(self)
        top_bar = QHBoxLayout()
        top_bar.addWidget(QLabel("Autonomy Mode:"))
        self.autonomy_combo = QComboBox()
        for mode in sorted(AUTONOMY_MODES):
            self.autonomy_combo.addItem(mode, mode)
        idx = self.autonomy_combo.findData(self.assistant.orchestrator.autonomy_mode)
        if idx >= 0:
            self.autonomy_combo.setCurrentIndex(idx)
        self.autonomy_combo.currentIndexChanged.connect(self._change_autonomy)
        top_bar.addWidget(self.autonomy_combo)
        top_bar.addStretch()
        refresh_btn = QPushButton("Refresh All Panels")
        refresh_btn.clicked.connect(self.refresh_all)
        top_bar.addWidget(refresh_btn)
        outer.addLayout(top_bar)

        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)

        self.dashboard_view = self._add_readonly_tab("Dashboard & Metrics")
        self.tasks_view = self._build_tasks_tab()
        self.agents_view = self._add_readonly_tab("Specialist Agents")
        self.models_view = self._add_readonly_tab("Models & Hardware")
        self.memory_view = self._add_readonly_tab("Structured Memory")
        self.knowledge_view = self._add_readonly_tab("Knowledge & RAG")
        self.workspace_view = self._add_readonly_tab("Coding Workspaces")
        self.security_view = self._add_readonly_tab("Security & Offline Assets")

        close_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_box.rejected.connect(self.reject)
        outer.addWidget(close_box)
        self.refresh_all()

    def _add_readonly_tab(self, title: str) -> QPlainTextEdit:
        page = QWidget()
        layout = QVBoxLayout(page)
        editor = QPlainTextEdit()
        editor.setReadOnly(True)
        layout.addWidget(editor)
        self.tabs.addTab(page, title)
        return editor

    def _build_tasks_tab(self) -> QPlainTextEdit:
        page = QWidget()
        layout = QVBoxLayout(page)
        controls = QHBoxLayout()
        self.task_id_input = QLineEdit()
        self.task_id_input.setPlaceholderText("Task ID for inspect / pause / resume / reconcile…")
        inspect_btn = QPushButton("Inspect Task")
        inspect_btn.clicked.connect(self._inspect_task)
        pause_btn = QPushButton("Pause Safe")
        pause_btn.clicked.connect(self._pause_task)
        reconcile_btn = QPushButton("Reconcile Interrupted")
        reconcile_btn.clicked.connect(self._reconcile_task)
        controls.addWidget(self.task_id_input, 1)
        controls.addWidget(inspect_btn)
        controls.addWidget(pause_btn)
        controls.addWidget(reconcile_btn)
        layout.addLayout(controls)
        editor = QPlainTextEdit()
        editor.setReadOnly(True)
        layout.addWidget(editor, 1)
        self.tabs.addTab(page, "Task Board & Plan Graph")
        return editor

    def _change_autonomy(self) -> None:
        mode = str(self.autonomy_combo.currentData() or "APPROVED_PLAN")
        self.assistant.orchestrator.set_autonomy_mode(mode)

    def _inspect_task(self) -> None:
        tid = self.task_id_input.text().strip()
        if not tid:
            self.refresh_all()
            return
        task = self.assistant.tasks.get(tid)
        timeline = self.assistant.tracer.task_timeline(tid)
        self.tasks_view.setPlainText(
            json.dumps({"task": task, "timeline": timeline}, indent=2, ensure_ascii=False)
        )

    def _pause_task(self) -> None:
        tid = self.task_id_input.text().strip()
        if not tid:
            return
        ok = self.assistant.tasks.pause_task(tid)
        QMessageBox.information(
            self,
            "Pause Task",
            f"Task {tid} paused cleanly." if ok else "Task could not be paused (not active or step in flight).",
        )
        self._inspect_task()

    def _reconcile_task(self) -> None:
        tid = self.task_id_input.text().strip()
        if not tid:
            return
        try:
            report = self.assistant.reconcile_task(tid)
            self.tasks_view.setPlainText(json.dumps(report, indent=2, ensure_ascii=False))
        except Exception as exc:
            QMessageBox.warning(self, "Reconcile Task", str(exc))

    def refresh_all(self) -> None:
        dash: dict[str, Any] = {
            "autonomy_mode": self.assistant.orchestrator.autonomy_mode,
            "metrics": self.assistant.tracer.metrics_snapshot(),
            "recent_runs": self.assistant.tracer.recent_runs(10),
        }
        self.dashboard_view.setPlainText(json.dumps(dash, indent=2, ensure_ascii=False))
        self.tasks_view.setPlainText(
            json.dumps(
                {
                    "tasks": self.assistant.tasks.dashboard_summary(),
                    "playbooks": self.assistant.playbooks.list_playbooks(),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        self.agents_view.setPlainText(
            json.dumps(
                {
                    "manifests": self.assistant.agent_registry.list_manifests(),
                    "health": self.assistant.agent_registry.health_snapshot(),
                    "active_locks": self.assistant.agent_registry.locks.active_locks(),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        self.models_view.setPlainText(
            json.dumps(
                {
                    "hardware": self.assistant.hardware.detect(),
                    "configured_chat_model": self.assistant.config.ollama.model,
                    "configured_embedding_model": self.assistant.config.ollama.embedding_model,
                    "fallback_events": self.assistant.llm.fallback_events,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        self.memory_view.setPlainText(
            json.dumps(
                {
                    "structured_items": self.assistant.memory.list_records(limit=30),
                    "preferences": self.assistant.memory.preferences(),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        self.knowledge_view.setPlainText(
            json.dumps(
                {
                    "index_stats": self.assistant.knowledge.index_stats(),
                    "documents": self.assistant.knowledge.list_documents(),
                    "reembed_queue": self.assistant.knowledge.reembed_queue(),
                    "watched_folders": self.assistant.knowledge_watcher.watched_folders(),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        patches = self.assistant.database.fetch_all(
            "SELECT patch_id, workspace_id, title, state, created_at FROM coding_patches ORDER BY created_at DESC LIMIT 20"
        )
        self.workspace_view.setPlainText(
            json.dumps(
                {
                    "approved_workspaces": self.assistant.workspaces.list_workspaces(),
                    "isolation_backend": self.assistant.workspaces.sandbox.isolation_backend,
                    "recent_patches": patches,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        self.security_view.setPlainText(
            json.dumps(
                {
                    "offline_policy": self.assistant.offline_guard.status(),
                    "encryption_at_rest": self.assistant.vault is not None,
                    "database_encryption": self.assistant.database.encryption_status(),
                    "audit_chain_integrity": self.assistant.tracer.verify_integrity(),
                    "pin_configured": self.assistant.authentication.is_configured,
                    "allowed_roots": [str(r) for r in self.assistant.config.security.allowed_roots],
                    "revoked_tools": self.assistant.permissions.status_snapshot(),
                    "offline_assets": self.assistant.assets.readiness_inventory(),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
