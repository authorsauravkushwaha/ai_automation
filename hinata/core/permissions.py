"""Central authorization policy shared by every front end."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from hinata.automation.jarvis_cockpit import JarvisOverrideController
from hinata.core.types import ActionRequest
from hinata.security.authentication import Authentication


class PermissionLevel(StrEnum):
    SAFE = "safe"
    CONFIRM = "confirm"
    SENSITIVE = "sensitive"


POLICY = {
    "code_inspect": PermissionLevel.SAFE,
    "inspect_repository_imports": PermissionLevel.SAFE,
    "knowledge_list": PermissionLevel.SAFE,
    "knowledge_sync": PermissionLevel.CONFIRM,
    "knowledge_index_semantic": PermissionLevel.CONFIRM,
    "knowledge_semantic_search": PermissionLevel.SAFE,
    "knowledge_hybrid_search": PermissionLevel.SAFE,
    "knowledge_answer": PermissionLevel.SAFE,
    "knowledge_index": PermissionLevel.CONFIRM,
    "knowledge_remove": PermissionLevel.CONFIRM,
    "knowledge_search": PermissionLevel.SAFE,
    "open_application": PermissionLevel.SAFE,
    "close_application": PermissionLevel.CONFIRM,
    "open_folder": PermissionLevel.SAFE,
    "open_file": PermissionLevel.CONFIRM,
    "list_directory": PermissionLevel.SAFE,
    "search_files": PermissionLevel.SAFE,
    "search_file_content": PermissionLevel.SAFE,
    "create_folder": PermissionLevel.SAFE,
    "read_text_file": PermissionLevel.SAFE,
    "write_text_file": PermissionLevel.CONFIRM,
    "copy_file": PermissionLevel.CONFIRM,
    "move_file": PermissionLevel.CONFIRM,
    "compare_files": PermissionLevel.SAFE,
    "archive_files": PermissionLevel.CONFIRM,
    "quarantine_delete_file": PermissionLevel.SENSITIVE,
    "type_text": PermissionLevel.CONFIRM,
    "volume": PermissionLevel.SAFE,
    "system_info": PermissionLevel.SAFE,
    "hardware_profile": PermissionLevel.SAFE,
    "ai_fleet_profile": PermissionLevel.SAFE,
    "generate_image": PermissionLevel.CONFIRM,
    "build_project": PermissionLevel.CONFIRM,
    "memory_search": PermissionLevel.SAFE,
    "remember_preference": PermissionLevel.CONFIRM,
    "list_preferences": PermissionLevel.SAFE,
    "set_personality_mode": PermissionLevel.SAFE,
    "clear_memory": PermissionLevel.SENSITIVE,
    "workspace_list": PermissionLevel.SAFE,
    "workspace_read": PermissionLevel.SAFE,
    "workspace_search": PermissionLevel.SAFE,
    "workspace_diff": PermissionLevel.SAFE,
    "workspace_propose_patch": PermissionLevel.SAFE,
    "workspace_apply_patch": PermissionLevel.CONFIRM,
    "workspace_revert_patch": PermissionLevel.CONFIRM,
    "run_project_tests": PermissionLevel.CONFIRM,
    "run_linter": PermissionLevel.SAFE,
    "run_type_checker": PermissionLevel.SAFE,
    "workspace_symbol_index": PermissionLevel.SAFE,
    # J.A.R.V.I.S. desktop cockpit & tactical awareness tools
    "executive_briefing": PermissionLevel.SAFE,
    "list_windows": PermissionLevel.SAFE,
    "focus_window": PermissionLevel.CONFIRM,
    "snap_window": PermissionLevel.CONFIRM,
    "send_hotkey": PermissionLevel.CONFIRM,
    "read_clipboard": PermissionLevel.CONFIRM,
    "write_clipboard": PermissionLevel.CONFIRM,
    "capture_screen": PermissionLevel.CONFIRM,
    "list_processes": PermissionLevel.SAFE,
    "power_control": PermissionLevel.SENSITIVE,
}


@dataclass(slots=True)
class PermissionDecision:
    allowed: bool
    status: str
    message: str
    reason: str = ""
    risk_tier: str = "low"


class PermissionManager:
    def __init__(
        self,
        authentication: Authentication,
        *,
        override_controller: JarvisOverrideController | None = None,
    ) -> None:
        self.authentication = authentication
        self.override = override_controller or JarvisOverrideController()
        self._revoked_tools: set[str] = set()
        self._agent_denials: dict[str, set[str]] = {}

    def level(self, tool: str) -> PermissionLevel:
        return POLICY.get(tool, PermissionLevel.SENSITIVE)

    def revoke_tool(self, tool: str) -> None:
        self._revoked_tools.add(tool)

    def restore_tool(self, tool: str) -> None:
        self._revoked_tools.discard(tool)

    def deny_agent_tool(self, agent_id: str, tool: str) -> None:
        self._agent_denials.setdefault(agent_id, set()).add(tool)

    def status_snapshot(self) -> dict[str, object]:
        return {
            "revoked_tools": sorted(self._revoked_tools),
            "agent_denials": {k: sorted(v) for k, v in sorted(self._agent_denials.items())},
            "tactical_override": self.override.status(),
        }

    def check(
        self,
        action: ActionRequest,
        *,
        source: str,
        speaker_match: bool | None,
        speaker_required: bool,
        confirmed: bool,
        pin: str | None,
        agent_id: str = "execution",
    ) -> PermissionDecision:
        if self.override.is_tool_blocked_by_lockdown(action.tool):
            return PermissionDecision(
                False,
                "denied",
                f"RED ALERT LOCKDOWN is active — '{action.tool}' is frozen until owner PIN unlock.",
                reason="red_alert_lockdown",
                risk_tier="high",
            )
        if action.tool in self._revoked_tools:
            return PermissionDecision(
                False,
                "denied",
                f"Tool '{action.tool}' has been revoked by active security policy.",
                reason="tool_revoked",
                risk_tier="high",
            )
        if action.tool in self._agent_denials.get(agent_id, set()):
            return PermissionDecision(
                False,
                "denied",
                f"Agent '{agent_id}' is denied access to '{action.tool}'.",
                reason="agent_scope_denied",
                risk_tier="high",
            )
        if source == "voice" and (speaker_match is False or (speaker_required and speaker_match is not True)):
            return PermissionDecision(
                False,
                "denied",
                "Speaker verification failed or was uncertain. Use typed input or re-enroll your voice.",
                reason="voice_verification_failed",
                risk_tier="medium",
            )
        level = self.level(action.tool)
        risk = {
            PermissionLevel.SAFE: "low",
            PermissionLevel.CONFIRM: "medium",
            PermissionLevel.SENSITIVE: "high",
        }[level]
        if level is PermissionLevel.SAFE:
            return PermissionDecision(True, "allowed", "Allowed", reason="safe_tier", risk_tier=risk)
        # Active PIN-unlocked Commander Lease streamlines CONFIRM-tier tools (typed or owner-verified voice),
        # while SENSITIVE tools ALWAYS enforce confirmation + PIN below.
        if level is PermissionLevel.CONFIRM and self.override.is_commander_active:
            return PermissionDecision(
                True,
                "allowed",
                "Authorized under active PIN-verified Commander Lease",
                reason="commander_lease",
                risk_tier=risk,
            )
        if not confirmed:
            return PermissionDecision(
                False,
                "confirmation_required",
                f"'{action.tool}' needs explicit confirmation before it can run.",
                reason="confirmation_tier",
                risk_tier=risk,
            )
        if level is PermissionLevel.SENSITIVE:
            if not self.authentication.is_configured:
                return PermissionDecision(
                    False,
                    "denied",
                    "No HINATA PIN is configured. Run 'hinata setup-pin' first.",
                    reason="pin_not_configured",
                    risk_tier=risk,
                )
            if not pin:
                return PermissionDecision(
                    False,
                    "pin_required",
                    "Enter your HINATA PIN to authorize this sensitive action.",
                    reason="pin_required",
                    risk_tier=risk,
                )
            if not self.authentication.verify_pin(pin):
                return PermissionDecision(
                    False,
                    "denied",
                    "The PIN is incorrect or has not been configured.",
                    reason="invalid_pin",
                    risk_tier=risk,
                )
        return PermissionDecision(True, "allowed", "Authorized", reason="owner_authorized", risk_tier=risk)
