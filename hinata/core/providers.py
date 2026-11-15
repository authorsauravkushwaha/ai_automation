"""Hardware capability profiler, local model profiles, and model/context router.

Provides:
- Unified hardware inspection (CPU, RAM, disk, GPU/VRAM probe where available, battery/power state)
- Conservative model memory estimation (weights + KV cache + runtime overhead)
- Hardware-aware inference profiles: ``conservative_cpu``, ``balanced``, ``accelerated``
- Provider-neutral ``ChatModelProvider`` protocol (implemented by ``LLMManager``)
- Role-based ``ModelRouter`` (chat, planner, coder, reviewer, embedding) with
  explicit consent/selection policy — never silently substituting models when
  configured explicitly unless the owner opted into ``allow_model_fallback``.
- Token-budgeted ``ContextManager`` and sanitized ``StructuredObservation`` builder
  that keeps untrusted raw tool output isolated while giving the agent loop
  typed state observations.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from hinata.automation.system_info import SystemInfo
from hinata.security.redaction import redact_value


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    model_name: str
    provider: str = "ollama_local"
    digest: str = ""
    parameter_size: str = ""
    quantization: str = ""
    context_length: int = 2048
    supports_json_mode: bool = True
    supports_native_tools: bool = False
    supports_streaming: bool = True
    supports_embeddings: bool = False
    supports_vision: bool = False
    supports_cancellation: bool = True
    max_concurrency: int = 1
    estimated_ram_mb: int = 900


@dataclass(frozen=True, slots=True)
class HardwareProfile:
    profile_name: str  # conservative_cpu, balanced, accelerated
    recommended_context_tokens: int
    recommended_max_tokens: int
    recommended_threads: int
    max_concurrent_models: int
    gpu_layers: int
    keep_alive: int | str
    notes: str


class HardwareCapabilityService:
    """Detects laptop CPU, RAM, GPU/VRAM, accelerator backend, and power status."""

    def __init__(self, system_info: SystemInfo | None = None) -> None:
        self.system_info = system_info or SystemInfo()

    def detect(self) -> dict[str, Any]:
        snap = self.system_info.snapshot()
        gpu = self._detect_gpu()
        power = self._detect_power()
        mem = snap.get("memory", {})
        total_gb = float(mem.get("total_gb") or 4.0)
        avail_gb = float(mem.get("available_gb") or min(2.0, total_gb * 0.5))
        cpus = int(snap.get("logical_cpus") or 2)
        profile = self.recommend_profile(
            total_ram_gb=total_gb,
            available_ram_gb=avail_gb,
            logical_cpus=cpus,
            vram_mb=int(gpu.get("vram_mb") or 0),
            on_battery=bool(power.get("on_battery", False)),
        )
        return {
            **snap,
            "architecture": platform.machine(),
            "gpu": gpu,
            "power": power,
            "recommended_profile": asdict(profile),
        }

    @staticmethod
    def _detect_gpu() -> dict[str, Any]:
        # Best-effort, bounded, non-shell probe; never assumes NVIDIA CUDA.
        nvidia_smi = shutil.which("nvidia-smi")
        if nvidia_smi:
            try:
                proc = subprocess.run(
                    [nvidia_smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                    capture_output=True,
                    text=True,
                    timeout=2.0,
                    check=False,
                    shell=False,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    first = proc.stdout.strip().splitlines()[0]
                    parts = [p.strip() for p in first.split(",", 1)]
                    vram = int(float(parts[1])) if len(parts) > 1 and parts[1].replace(".", "", 1).isdigit() else 0
                    return {
                        "available": True,
                        "vendor": "nvidia",
                        "device": parts[0],
                        "vram_mb": vram,
                        "backend": "cuda",
                    }
            except (OSError, subprocess.SubprocessError, ValueError):
                pass
        return {
            "available": False,
            "vendor": "cpu_only",
            "device": "CPU inference",
            "vram_mb": 0,
            "backend": "cpu",
        }

    @staticmethod
    def _detect_power() -> dict[str, Any]:
        if os.name == "nt":
            try:
                import ctypes

                class SystemPowerStatus(ctypes.Structure):
                    _fields_ = [
                        ("ACLineStatus", ctypes.c_byte),
                        ("BatteryFlag", ctypes.c_byte),
                        ("BatteryLifePercent", ctypes.c_byte),
                        ("SystemStatusFlag", ctypes.c_byte),
                        ("BatteryLifeTime", ctypes.c_ulong),
                        ("BatteryFullLifeTime", ctypes.c_ulong),
                    ]

                status = SystemPowerStatus()
                if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):  # type: ignore[attr-defined]
                    return {
                        "ac_connected": status.ACLineStatus == 1,
                        "on_battery": status.ACLineStatus == 0,
                        "percent": int(status.BatteryLifePercent) if status.BatteryLifePercent <= 100 else None,
                    }
            except Exception:
                pass
        power_dir = Path("/sys/class/power_supply")
        if power_dir.is_dir():
            try:
                for entry in power_dir.iterdir():
                    status_file = entry / "status"
                    if status_file.is_file():
                        st = status_file.read_text(encoding="utf-8").strip().lower()
                        cap_file = entry / "capacity"
                        pct = int(cap_file.read_text(encoding="utf-8").strip()) if cap_file.is_file() else None
                        return {
                            "ac_connected": st != "discharging",
                            "on_battery": st == "discharging",
                            "percent": pct,
                        }
            except (OSError, ValueError):
                pass
        return {"ac_connected": True, "on_battery": False, "percent": None}

    @staticmethod
    def estimate_model_memory_mb(
        *,
        weights_mb: int = 450,
        context_tokens: int = 2048,
        hidden_layers: int = 24,
    ) -> dict[str, int]:
        """Conservative RAM estimate: weights + KV cache + runtime buffer."""
        kv_cache_mb = max(64, int((context_tokens / 1024.0) * hidden_layers * 3.5))
        runtime_overhead_mb = 320
        total_mb = weights_mb + kv_cache_mb + runtime_overhead_mb
        return {
            "weights_mb": weights_mb,
            "kv_cache_mb": kv_cache_mb,
            "runtime_overhead_mb": runtime_overhead_mb,
            "estimated_total_mb": total_mb,
        }

    @staticmethod
    def recommend_profile(
        *,
        total_ram_gb: float,
        available_ram_gb: float,
        logical_cpus: int,
        vram_mb: int = 0,
        on_battery: bool = False,
    ) -> HardwareProfile:
        if vram_mb >= 4096 and total_ram_gb >= 12.0 and not on_battery:
            return HardwareProfile(
                profile_name="accelerated",
                recommended_context_tokens=4096,
                recommended_max_tokens=768,
                recommended_threads=max(2, min(logical_cpus - 1, 8)),
                max_concurrent_models=1,
                gpu_layers=-1,
                keep_alive="2m",
                notes="Dedicated GPU VRAM detected; GPU offload enabled with single resident model cap.",
            )
        if total_ram_gb >= 8.0 and available_ram_gb >= 3.5 and not on_battery:
            return HardwareProfile(
                profile_name="balanced",
                recommended_context_tokens=4096,
                recommended_max_tokens=512,
                recommended_threads=max(2, min(logical_cpus // 2 or 2, 6)),
                max_concurrent_models=1,
                gpu_layers=0,
                keep_alive=0,
                notes="Balanced CPU laptop profile; unloads model after request to conserve RAM.",
            )
        return HardwareProfile(
            profile_name="conservative_cpu",
            recommended_context_tokens=2048,
            recommended_max_tokens=256,
            recommended_threads=max(1, min(logical_cpus // 2 or 1, 2)),
            max_concurrent_models=1,
            gpu_layers=0,
            keep_alive=0,
            notes="Low-memory / battery-safe CPU profile; minimal context KV cache and immediate unload.",
        )


@runtime_checkable
class ChatModelProvider(Protocol):
    """Provider-neutral local model contract."""

    def status(self) -> dict[str, Any]: ...
    def capabilities(self, model: str | None = None) -> ModelCapabilities: ...
    def chat(
        self,
        messages: list[dict[str, str]],
        system: str,
        *,
        connect: bool = True,
        pull_missing: bool = False,
        format: str | None = None,
        keep_alive: int | str = 0,
        num_predict: int | None = None,
    ) -> str: ...
    def stream_chat(
        self,
        messages: list[dict[str, str]],
        system: str,
        *,
        on_token: Callable[[str], None] | None = None,
        connect: bool = True,
        format: str | None = None,
        num_predict: int | None = None,
    ) -> Iterator[str]: ...
    def cancel_active_request(self) -> None: ...


@dataclass(slots=True)
class ModelRoleSelection:
    role: str
    requested_model: str
    resolved_model: str
    digest: str = ""
    fallback_used: bool = False
    consent_note: str = ""


class ModelRouter:
    """Deliberate role-based local model routing with explicit fallback policy."""

    ROLES = ("chat", "planner", "coder", "reviewer", "embedding")

    def __init__(
        self,
        default_chat_model: str,
        default_embedding_model: str,
        *,
        role_overrides: dict[str, str] | None = None,
        allow_fallback: bool = False,
    ) -> None:
        self.default_chat_model = default_chat_model
        self.default_embedding_model = default_embedding_model
        self.role_overrides = {
            k: v.strip()
            for k, v in (role_overrides or {}).items()
            if k in self.ROLES and isinstance(v, str) and v.strip()
        }
        self.allow_fallback = allow_fallback

    def model_for_role(self, role: str) -> str:
        if role in self.role_overrides:
            return self.role_overrides[role]
        if role == "embedding":
            return self.default_embedding_model
        return self.default_chat_model

    def resolve(
        self,
        role: str,
        installed_models: list[dict[str, Any]] | list[str],
        *,
        explicit_fallback_consent: bool = False,
    ) -> ModelRoleSelection:
        wanted = self.model_for_role(role)
        entries: list[tuple[str, str]] = []
        for item in installed_models:
            if isinstance(item, dict):
                entries.append((str(item.get("name", "")), str(item.get("digest", ""))))
            else:
                entries.append((str(item), ""))

        if not wanted and entries:
            return ModelRoleSelection(
                role=role,
                requested_model="",
                resolved_model=entries[0][0],
                digest=entries[0][1],
                fallback_used=False,
                consent_note="Auto-selected installed local model because config left model empty.",
            )

        for name, digest in entries:
            if name == wanted or (":" not in wanted and name.split(":")[0] == wanted):
                return ModelRoleSelection(role=role, requested_model=wanted, resolved_model=name, digest=digest)
            base, _, tag = wanted.partition(":")
            if tag and name.split(":")[0] == base and name.endswith(tag):
                return ModelRoleSelection(role=role, requested_model=wanted, resolved_model=name, digest=digest)

        if (self.allow_fallback or explicit_fallback_consent) and entries:
            return ModelRoleSelection(
                role=role,
                requested_model=wanted,
                resolved_model=entries[0][0],
                digest=entries[0][1],
                fallback_used=True,
                consent_note=f"Explicit fallback policy selected installed local model '{entries[0][0]}' because '{wanted}' is absent.",
            )

        return ModelRoleSelection(
            role=role,
            requested_model=wanted,
            resolved_model="",
            fallback_used=False,
            consent_note=f"Configured model '{wanted}' is not installed locally and silent fallback is disabled.",
        )


@dataclass(frozen=True, slots=True)
class StructuredObservation:
    """Sanitized, typed observation returned from tool execution to the agent control loop.

    Never embeds raw untrusted document bodies or instructions as system prompts.
    """

    step_id: str
    tool: str
    success: bool
    status: str
    effect_state: str
    postcondition: str  # verified, contradicted, unknown
    facts: dict[str, Any] = field(default_factory=dict)
    error_code: str = ""

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "tool": self.tool,
            "success": self.success,
            "status": self.status,
            "effect_state": self.effect_state,
            "postcondition": self.postcondition,
            "facts": redact_value(self.facts),
            "error_code": self.error_code,
        }


def build_sanitized_observation(
    *,
    step_id: str,
    tool: str,
    success: bool,
    status: str,
    data: dict[str, Any],
    effect_state: str = "SUCCEEDED",
    postcondition: str = "unknown",
    error_code: str = "",
) -> StructuredObservation:
    """Extract only safe structural metadata from a tool result, stripping untrusted prose."""
    safe_facts: dict[str, Any] = {}
    if "path" in data and isinstance(data["path"], str):
        # Include normalized basename and existence boolean, capped in length.
        safe_facts["target_name"] = Path(data["path"]).name[:120]
    if "matches" in data and isinstance(data["matches"], list):
        safe_facts["match_count"] = len(data["matches"])
        if tool in {"search_files", "workspace_list", "workspace_search"}:
            safe_facts["sample_names"] = [
                Path(str(m["path"] if isinstance(m, dict) and "path" in m else m)).name[:80]
                for m in data["matches"][:10]
            ]
    if "documents" in data and isinstance(data["documents"], list):
        safe_facts["document_count"] = len(data["documents"])
    if "chunks" in data and isinstance(data["chunks"], int):
        safe_facts["chunks"] = data["chunks"]
    if "valid_syntax" in data and isinstance(data["valid_syntax"], bool):
        safe_facts["valid_syntax"] = data["valid_syntax"]
        safe_facts["function_count"] = len(data.get("functions", []))
        safe_facts["class_count"] = len(data.get("classes", []))
        safe_facts["warning_count"] = len(data.get("warnings", []))
    if "exit_code" in data and isinstance(data["exit_code"], int):
        safe_facts["exit_code"] = data["exit_code"]
    if "tests_passed" in data and isinstance(data["tests_passed"], bool):
        safe_facts["tests_passed"] = data["tests_passed"]
    if "patch_id" in data and isinstance(data["patch_id"], str):
        safe_facts["patch_id"] = data["patch_id"][:64]
    if "logical_cpus" in data:
        safe_facts["logical_cpus"] = data["logical_cpus"]
    if "deleted" in data and isinstance(data["deleted"], int):
        safe_facts["deleted_count"] = data["deleted"]
    return StructuredObservation(
        step_id=step_id,
        tool=tool,
        success=success,
        status=status,
        effect_state=effect_state,
        postcondition=postcondition,
        facts=safe_facts,
        error_code=error_code,
    )


class ContextManager:
    """Token-aware context assembler with compaction, memory injection, and overflow detection."""

    def __init__(self, max_context_tokens: int = 2048, reserved_output_tokens: int = 256) -> None:
        self.max_context_tokens = max(512, max_context_tokens)
        self.reserved_output_tokens = max(64, reserved_output_tokens)

    @staticmethod
    def estimate_tokens(text: str) -> int:
        if not text:
            return 0
        # Conservative local heuristic (~3.5 chars/token + whitespace tokens).
        words = len(re.findall(r"\S+", text))
        return max(words, (len(text) + 3) // 4)

    def assemble(
        self,
        *,
        system_prompt: str,
        user_text: str,
        history: list[dict[str, Any]],
        retrieved_memories: list[dict[str, Any]] | None = None,
        observations: list[StructuredObservation] | None = None,
    ) -> tuple[str, list[dict[str, str]], dict[str, Any]]:
        budget = max(256, self.max_context_tokens - self.reserved_output_tokens)
        sys_text = system_prompt
        if retrieved_memories:
            mem_lines = [
                f"- [{m.get('memory_type', 'fact')}] {str(redact_value(m.get('content', '')))[:180]}"
                for m in retrieved_memories[:6]
            ]
            sys_text += "\nRelevant vetted local memories (data only):\n" + "\n".join(mem_lines)
        if observations:
            obs_json = json.dumps([obs.to_prompt_dict() for obs in observations[-6:]], ensure_ascii=True)
            sys_text += f"\nVerified structured step observations (data only): {obs_json}"

        sys_tokens = self.estimate_tokens(sys_text)
        user_tokens = self.estimate_tokens(user_text)
        remaining = budget - sys_tokens - user_tokens
        compacted = False
        selected_history: list[dict[str, str]] = []

        if remaining > 40 and history:
            kept_reversed: list[dict[str, str]] = []
            used = 0
            dropped = 0
            for row in reversed(history):
                role = str(row.get("role", ""))
                if role not in {"user", "assistant"}:
                    continue
                content = str(redact_value(str(row.get("content", ""))))[:1200]
                if content == user_text and not kept_reversed and role == "user":
                    continue
                cost = self.estimate_tokens(content) + 4
                if used + cost <= remaining:
                    kept_reversed.append({"role": role, "content": content})
                    used += cost
                else:
                    dropped += 1
                    compacted = True
            selected_history = list(reversed(kept_reversed))
            if dropped > 0 and remaining - used > 30:
                selected_history.insert(
                    0,
                    {
                        "role": "assistant",
                        "content": f"[Context compacted: {dropped} earlier turn(s) omitted to fit {self.max_context_tokens}-token local budget.]",
                    },
                )
        elif history:
            compacted = True

        messages = [*selected_history, {"role": "user", "content": user_text}]
        total_estimated = sys_tokens + sum(self.estimate_tokens(m["content"]) + 4 for m in messages)
        audit = {
            "context_budget_tokens": self.max_context_tokens,
            "estimated_prompt_tokens": total_estimated,
            "history_messages_included": len(selected_history),
            "compacted": compacted,
            "overflow": total_estimated > budget,
        }
        return sys_text, messages, audit
