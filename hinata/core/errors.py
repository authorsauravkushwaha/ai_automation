"""Structured error taxonomy, budgets, circuit breakers, and request-scoped cancellation."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from hinata.security.redaction import redact_text

LOGGER = logging.getLogger(__name__)


class ErrorCategory(StrEnum):
    VALIDATION = "validation"
    PERMISSION = "permission"
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    CONFLICT = "conflict"
    TIMEOUT = "timeout"
    RESOURCE = "resource"
    CANCELLATION = "cancellation"
    UNKNOWN_SIDE_EFFECT = "unknown_side_effect"


@dataclass(slots=True)
class ClassifiedError(Exception):
    category: ErrorCategory
    code: str
    message: str
    retryable: bool = False
    safe_diagnostics: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": str(self.category),
            "code": self.code,
            "message": self.message[:500],
            "retryable": self.retryable,
            "diagnostics": self.safe_diagnostics,
        }


def classify_exception(exc: Exception) -> ClassifiedError:
    """Map an internal exception to a structured, sanitized ClassifiedError."""
    if isinstance(exc, ClassifiedError):
        return exc
    name = type(exc).__name__
    msg = str(exc)
    lower = msg.lower()
    if name in {"ValidationError", "PlanError", "PlanningError", "ConfigError"}:
        return ClassifiedError(ErrorCategory.VALIDATION, f"ERR_{name.upper()}", msg, retryable=False)
    if "permission" in lower or "access denied" in lower or "pin" in lower or "not allow-listed" in lower:
        return ClassifiedError(ErrorCategory.PERMISSION, "ERR_PERMISSION_DENIED", msg, retryable=False)
    if isinstance(exc, TimeoutError) or "timed out" in lower or "timeout" in lower:
        return ClassifiedError(ErrorCategory.TIMEOUT, "ERR_TIMEOUT", msg, retryable=True)
    if "cancel" in lower or "stopped" in lower:
        return ClassifiedError(ErrorCategory.CANCELLATION, "ERR_CANCELLED", msg, retryable=False)
    if "already exists" in lower or "conflict" in lower or "changed" in lower or "already claimed" in lower:
        return ClassifiedError(ErrorCategory.CONFLICT, "ERR_CONFLICT", msg, retryable=False)
    if "budget" in lower or "quota" in lower or "exceeds" in lower or isinstance(exc, MemoryError):
        return ClassifiedError(ErrorCategory.RESOURCE, "ERR_RESOURCE_LIMIT", msg, retryable=False)
    if "uncertain" in lower or "unknown outcome" in lower:
        return ClassifiedError(ErrorCategory.UNKNOWN_SIDE_EFFECT, "ERR_UNKNOWN_EFFECT", msg, retryable=False)
    if "cannot reach" in lower or "connection" in lower or "temporarily" in lower or "busy" in lower:
        return ClassifiedError(ErrorCategory.TRANSIENT, "ERR_TRANSIENT_RUNTIME", msg, retryable=True)
    return ClassifiedError(ErrorCategory.PERMANENT, f"ERR_{name.upper()}", msg, retryable=False)


class CancellationToken:
    """Request-scoped cooperative cancellation token linked to an optional global epoch."""

    def __init__(self, *, token_id: str | None = None, parent: CancellationToken | None = None) -> None:
        self.token_id = token_id or uuid.uuid4().hex
        self._parent = parent
        self._event = threading.Event()
        self._callbacks: list[Any] = []
        self._lock = threading.Lock()
        self.reason: str = ""

    def cancel(self, reason: str = "User requested stop") -> None:
        callbacks: list[Any] = []
        with self._lock:
            if not self._event.is_set():
                self.reason = reason
                self._event.set()
                callbacks = list(self._callbacks)
        for callback in callbacks:
            try:
                callback()
            except Exception as exc:
                LOGGER.error(
                    "Cancellation callback failed: %s",
                    redact_text(str(exc))[:300],
                )

    @property
    def is_cancelled(self) -> bool:
        if self._event.is_set():
            return True
        if self._parent is not None and self._parent.is_cancelled:
            return True
        return False

    def check(self) -> None:
        if self.is_cancelled:
            raise ClassifiedError(
                ErrorCategory.CANCELLATION,
                "ERR_CANCELLED",
                self.reason or (self._parent.reason if self._parent else "Operation cancelled"),
            )

    def on_cancel(self, callback: Any) -> None:
        call_now = False
        with self._lock:
            if self._event.is_set() or (self._parent is not None and self._parent.is_cancelled):
                call_now = True
            else:
                self._callbacks.append(callback)
        if call_now:
            try:
                callback()
            except Exception as exc:
                LOGGER.error(
                    "Cancellation callback failed: %s",
                    redact_text(str(exc))[:300],
                )


class CancellationRegistry:
    """Manages per-request tokens without allowing a new request to erase another's stop signal."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, CancellationToken] = {}
        self._global_halt = False

    def create_token(self, request_id: str | None = None) -> CancellationToken:
        token = CancellationToken(token_id=request_id)
        with self._lock:
            # Creating a fresh request starts a new token; if emergency stop was pressed
            # while idle, a new explicit request resets idle global_halt while keeping
            # any previously cancelled tokens permanently cancelled.
            self._global_halt = False
            self._active[token.token_id] = token
        return token

    def release_token(self, token: CancellationToken) -> None:
        with self._lock:
            self._active.pop(token.token_id, None)

    def cancel_request(self, token_id: str, reason: str = "Request cancelled") -> bool:
        with self._lock:
            token = self._active.get(token_id)
        if token is None:
            return False
        token.cancel(reason)
        return True

    def cancel_all(self, reason: str = "Emergency stop") -> int:
        with self._lock:
            self._global_halt = True
            tokens = list(self._active.values())
        for token in tokens:
            token.cancel(reason)
        return len(tokens)

    @property
    def global_halt(self) -> bool:
        with self._lock:
            return self._global_halt


@dataclass(slots=True)
class TaskBudget:
    max_iterations: int = 8
    max_steps: int = 12
    max_duration_seconds: float = 180.0
    max_model_calls: int = 10
    max_tool_calls: int = 16
    max_tokens: int = 16384
    max_replans: int = 2
    per_tool_timeout_seconds: float = 30.0
    max_memory_mb: int = 2048

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class BudgetTracker:
    budget: TaskBudget
    started_monotonic: float = field(default_factory=time.monotonic)
    iterations: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    tokens_used: int = 0
    replans: int = 0
    _lock: Any = field(default_factory=threading.RLock, repr=False)

    def elapsed_seconds(self) -> float:
        return round(time.monotonic() - self.started_monotonic, 3)

    def check_limits(self) -> None:
        if self.elapsed_seconds() > self.budget.max_duration_seconds:
            raise ClassifiedError(
                ErrorCategory.TIMEOUT,
                "ERR_TASK_DURATION_EXCEEDED",
                f"Task exceeded maximum duration ({self.budget.max_duration_seconds}s)",
            )
        if self.iterations > self.budget.max_iterations:
            raise ClassifiedError(
                ErrorCategory.RESOURCE,
                "ERR_ITERATION_BUDGET_EXCEEDED",
                f"Agent loop exceeded max_iterations ({self.budget.max_iterations})",
            )
        if self.model_calls > self.budget.max_model_calls:
            raise ClassifiedError(
                ErrorCategory.RESOURCE,
                "ERR_MODEL_BUDGET_EXCEEDED",
                f"Task exceeded max_model_calls ({self.budget.max_model_calls})",
            )
        if self.tool_calls > self.budget.max_tool_calls:
            raise ClassifiedError(
                ErrorCategory.RESOURCE,
                "ERR_TOOL_BUDGET_EXCEEDED",
                f"Task exceeded max_tool_calls ({self.budget.max_tool_calls})",
            )
        if self.tokens_used > self.budget.max_tokens:
            raise ClassifiedError(
                ErrorCategory.RESOURCE,
                "ERR_TOKEN_BUDGET_EXCEEDED",
                f"Task exceeded max_tokens ({self.budget.max_tokens})",
            )

    def reserve(self, counter: str, maximum: int, amount: int = 1) -> None:
        with self._lock:
            self.check_limits()
            if getattr(self, counter) + amount > maximum:
                raise ClassifiedError(ErrorCategory.RESOURCE, "ERR_BUDGET_EXCEEDED",
                                      f"Task {counter} budget exhausted ({maximum})")
            setattr(self, counter, getattr(self, counter) + amount)

    def remaining_seconds(self) -> float:
        self.check_limits()
        return max(0.001, self.budget.max_duration_seconds - self.elapsed_seconds())

    def record_iteration(self) -> None:
        self.reserve("iterations", self.budget.max_iterations)

    def record_model_call(self, tokens: int = 0) -> None:
        with self._lock:
            self.reserve("model_calls", self.budget.max_model_calls)
            self.reserve("tokens_used", self.budget.max_tokens, max(0, tokens))

    def record_tool_call(self) -> None:
        self.reserve("tool_calls", self.budget.max_tool_calls)

    def record_replan(self) -> None:
        self.reserve("replans", self.budget.max_replans)

    def snapshot(self) -> dict[str, Any]:
        return {
            "elapsed_seconds": self.elapsed_seconds(),
            "iterations": self.iterations,
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "tokens_used": self.tokens_used,
            "replans": self.replans,
        }


class CircuitBreaker:
    """Component health tracker that trips open after repeated failures."""

    def __init__(self, *, failure_threshold: int = 4, recovery_seconds: float = 30.0) -> None:
        self.failure_threshold = max(1, failure_threshold)
        self.recovery_seconds = max(1.0, recovery_seconds)
        self._lock = threading.Lock()
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}
        self._last_error: dict[str, str] = {}

    def allow(self, component: str) -> bool:
        with self._lock:
            opened = self._opened_at.get(component)
            if opened is None:
                return True
            if time.monotonic() - opened >= self.recovery_seconds:
                # Half-open probe permitted.
                return True
            return False

    def record_success(self, component: str) -> None:
        with self._lock:
            self._failures.pop(component, None)
            self._opened_at.pop(component, None)
            self._last_error.pop(component, None)

    def record_failure(self, component: str, error: str = "") -> None:
        with self._lock:
            count = self._failures.get(component, 0) + 1
            self._failures[component] = count
            if error:
                self._last_error[component] = error[:200]
            if count >= self.failure_threshold:
                self._opened_at[component] = time.monotonic()

    def reset(self, component: str | None = None) -> None:
        with self._lock:
            if component is None:
                self._failures.clear()
                self._opened_at.clear()
                self._last_error.clear()
            else:
                self._failures.pop(component, None)
                self._opened_at.pop(component, None)
                self._last_error.pop(component, None)

    def health_snapshot(self) -> dict[str, dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            keys = set(self._failures) | set(self._opened_at)
            report: dict[str, dict[str, Any]] = {}
            for key in sorted(keys):
                opened = self._opened_at.get(key)
                state = "closed"
                if opened is not None:
                    state = "half_open" if now - opened >= self.recovery_seconds else "open"
                report[key] = {
                    "state": state,
                    "failures": self._failures.get(key, 0),
                    "last_error": self._last_error.get(key, ""),
                }
            return report
