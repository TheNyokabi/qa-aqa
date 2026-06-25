"""Workflow type registry.

Single entry today (DesignTestsWorkflow). The shape is what matters — D1.1
populates per-tenant overrides through this same dict.
"""
from __future__ import annotations

from typing import Any

from .workflow import DesignTestsWorkflow
from .workflow_execute import ExecuteTestsWorkflow

# (tenant_id or "*"): {workflow_type: workflow_class}
REGISTRY: dict[str, dict[str, Any]] = {
    "*": {
        "design-tests": DesignTestsWorkflow,
        "execute-tests": ExecuteTestsWorkflow,
    },
}


def lookup(tenant_id: str, workflow_type: str):
    """Resolve a workflow class for a tenant + type. Per-tenant overrides first."""
    if tenant_id in REGISTRY and workflow_type in REGISTRY[tenant_id]:
        return REGISTRY[tenant_id][workflow_type]
    if workflow_type in REGISTRY["*"]:
        return REGISTRY["*"][workflow_type]
    raise KeyError(f"no workflow registered for ({tenant_id}, {workflow_type})")


def all_workflows() -> list[Any]:
    seen: set[Any] = set()
    out: list[Any] = []
    for d in REGISTRY.values():
        for w in d.values():
            if w not in seen:
                seen.add(w)
                out.append(w)
    return out
