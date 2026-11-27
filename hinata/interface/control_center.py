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
        self.posture_label = QLabel("Posture: STANDARD")
        top_bar.addWidget(self.posture_label)
        top_bar.addStretch()
        briefing_btn = QPushButton("Tactical Briefing")
        briefing_btn.clicked.connect(self._show_briefing)
        lockdown_btn = QPushButton("Red Alert Lockdown")
        lockdown_btn.clicked.connect(self._toggle_lockdown)
        commander_btn = QPushButton("Commander Lease")
        commander_btn.clicked.connect(self._toggle_commander)
        refresh_btn = QPushButton("Refresh All Panels")
        refresh_btn.clicked.connect(self.refresh_all)
        top_bar.addWidget(briefing_btn)
        top_bar.addWidget(lockdown_btn)
        top_bar.addWidget(commander_btn)
        top_bar.addWidget(refresh_btn)
        outer.addLayout(top_bar)

        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)

        self.dashboard_view = self._add_readonly_tab("Dashboard & Metrics")
        self.tasks_view = self._build_tasks_tab()
        self.agents_view = self._add_readonly_tab("Specialist Agents")
        self.thinking_view = self._add_readonly_tab("Thinking & Reflection")
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

    def _show_briefing(self) -> None:
        resp = self.assistant.process_text("executive briefing")
        QMessageBox.information(self, "J.A.R.V.I.S. Tactical Briefing", resp.text)
        self.refresh_all()

    def _toggle_lockdown(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        if not self.assistant.override_controller.is_lockdown:
            self.assistant.engage_lockdown("Desktop Control Center Red Alert Lockdown")
            QMessageBox.warning(
                self,
                "Red Alert Lockdown Engaged",
                "All mutating, UI, and autonomous tools are frozen until unlocked with your Owner PIN.",
            )
            self.refresh_all()
            return
        pin, ok = QInputDialog.getText(
            self, "Lift Red Alert Lockdown", "Enter HINATA Owner PIN:", QLineEdit.EchoMode.Password
        )
        if not ok or not pin:
            return
        try:
            self.assistant.release_lockdown(pin)
            QMessageBox.information(self, "Lockdown Lifted", "Normal guarded operations restored.")
        except PermissionError as exc:
            QMessageBox.critical(self, "PIN Verification Failed", str(exc))
        self.refresh_all()

    def _toggle_commander(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        if self.assistant.override_controller.is_commander_active:
            self.assistant.stand_down_commander_mode()
            QMessageBox.information(self, "Commander Mode Revoked", "Stood down to standard approval gates.")
            self.refresh_all()
            return
        pin, ok = QInputDialog.getText(
            self,
            "Activate Commander Lease (10 min)",
            "Enter HINATA Owner PIN to unlock timed CONFIRM-tier execution:",
            QLineEdit.EchoMode.Password,
        )
        if not ok or not pin:
            return
        try:
            self.assistant.activate_commander_mode(pin, minutes=10, label="Desktop HUD Session")
            QMessageBox.information(
                self,
                "Commander Lease Active",
                "10-minute Commander Lease unlocked. Destructive SENSITIVE tools still require PIN.",
            )
        except PermissionError as exc:
            QMessageBox.critical(self, "Authorization Failed", str(exc))
        self.refresh_all()

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
        ov = self.assistant.override_controller.status()
        if ov["lockdown_active"]:
            self.posture_label.setText("Posture: RED ALERT LOCKDOWN")
        elif ov["commander_mode_active"]:
            self.posture_label.setText(f"Posture: COMMANDER ({ov['commander_remaining_seconds']:.0f}s)")
        else:
            self.posture_label.setText("Posture: STANDARD")
        dash: dict[str, Any] = {
            "tactical_override": ov,
            "autonomy_mode": self.assistant.orchestrator.autonomy_mode,
            "scheduled_daemons": self.assistant.scheduler.list_schedules(active_only=True),
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
        thinking_rows: list[dict[str, Any]] = []
        try:
            thinking_rows = [
                dict(row) for row in self.assistant.thinking.reflections.recent(10)
            ]
        except Exception:
            thinking_rows = []
        recent_reasoning: list[dict[str, Any]] = []
        try:
            for run in self.assistant.tracer.recent_runs(15):
                payload = run.get("payload") if isinstance(run, dict) else None
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except ValueError:
                        payload = None
                if isinstance(payload, dict) and payload.get("confidence"):
                    recent_reasoning.append(payload)
        except Exception:
            recent_reasoning = []
        self.thinking_view.setPlainText(
            json.dumps(
                {
                    "profile": self.assistant.thinking.status(),
                    "active": self.assistant.orchestrator.thinking_active,
                    "stored_lessons": thinking_rows,
                    "recent_traces": recent_reasoning,
                },
                indent=2,
                ensure_ascii=False,
            )
        )

        from hinata.core.jarvis_models import select_optimal_fleet_pack
        from hinata.core.local_image import is_model_ready, model_directory

        hardware = self.assistant.hardware.detect()
        self.models_view.setPlainText(
            json.dumps(
                {
                    "hardware": hardware,
                    "recommended_lenovo_loq_fleet": select_optimal_fleet_pack(hardware).to_dict(),
                    "local_sd_turbo_ready": is_model_ready(model_directory(self.assistant.config.data_dir)),
                    "configured_model_roles": {
                        "chat": self.assistant.config.ollama.model,
                        "planner": self.assistant.config.ollama.planner_model,
                        "coder": self.assistant.config.ollama.coding_model,
                        "reviewer": self.assistant.config.ollama.reviewer_model,
                        "vision": self.assistant.config.ollama.vision_model,
                        "embedding": self.assistant.config.ollama.embedding_model,
                    },
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
