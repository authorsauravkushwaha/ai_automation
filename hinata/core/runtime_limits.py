"""Request-scoped limits shared across inference, plans and compound children.

These are cooperative process limits, not OS memory or thread isolation.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import replace
from functools import wraps
from typing import Any

from hinata.core.errors import BudgetTracker, ClassifiedError
from hinata.core.types import AssistantResponse

CURRENT_BUDGET: ContextVar[BudgetTracker | None] = ContextVar("hinata_budget", default=None)
MODEL_RESERVED: ContextVar[bool] = ContextVar("hinata_model_reserved", default=False)
MODEL_DEADLINE: ContextVar[float | None] = ContextVar("hinata_model_deadline", default=None)
NODE_TIMEOUT: ContextVar[float | None] = ContextVar("hinata_node_timeout", default=None)


def bounded_operation(function: Any) -> Any:
    @wraps(function)
    def run(self: Any, *args: Any, **kwargs: Any) -> AssistantResponse:
        supplied = kwargs.pop("budget_tracker", None)
        inherited = CURRENT_BUDGET.get()
        budget = inherited or supplied or BudgetTracker(replace(self.default_budget))
        acquired = False
        context = CURRENT_BUDGET.set(budget)
        try:
            if inherited is None:
                acquired = self._operation_lock.acquire(timeout=budget.remaining_seconds())
                if not acquired:
                    return AssistantResponse("Another request is still active; retry after it finishes.", False, "blocked")
            budget.check_limits()
            return function(self, *args, **kwargs)
        except ClassifiedError as exc:
            return AssistantResponse(str(exc), False, "cancelled" if exc.code == "ERR_CANCELLED" else "blocked",
                                     data={"error_code": exc.code, "usage": budget.snapshot()})
        finally:
            CURRENT_BUDGET.reset(context)
            if acquired:
                self._operation_lock.release()
    return run


def node_limits(function: Any) -> Any:
    @wraps(function)
    def run(self: Any, node: Any, *args: Any, **kwargs: Any) -> Any:
        context = NODE_TIMEOUT.set(node.timeout_seconds)
        try:
            return function(self, node, *args, **kwargs)
        finally:
            NODE_TIMEOUT.reset(context)
    return run
