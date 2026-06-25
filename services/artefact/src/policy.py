"""Approval policy lookup + transition validation.

Lookup order:
  1) Per-tenant override artefact (type=approval_policy, payload.applies_to includes target)
  2) Built-in DEFAULT_POLICY (also seeded as an artefact on startup; this code
     fallback exists so the first transition can succeed before seed completes)
"""
from __future__ import annotations

from typing import Any

from .urn import actor_type_of

DEFAULT_POLICY: dict[str, Any] = {
    "name": "default_v1",
    "applies_to": ["requirement", "test_case", "approval_policy", "critique_policy"],
    "states": ["draft", "in_review", "approved", "archived"],
    "transitions": {
        "draft -> in_review":    {"roles_any": ["agent", "user"]},
        "draft -> archived":     {"roles_any": ["user"]},
        "in_review -> approved": {"roles_any": ["user"]},
        "in_review -> draft":    {"roles_any": ["user"]},
        "in_review -> archived": {"roles_any": ["user"]},
        "approved -> archived":  {"roles_any": ["user"]},
    },
}


class TransitionDenied(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def resolve_policy_for(policies_for_type: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the first tenant override that applies; otherwise DEFAULT_POLICY."""
    for p in policies_for_type:
        return p
    return DEFAULT_POLICY


def validate_transition(policy: dict[str, Any], from_state: str, to_state: str, actor: str) -> None:
    """Raise TransitionDenied if the transition is not allowed."""
    transitions = policy.get("transitions", {})
    key = f"{from_state} -> {to_state}"
    if key not in transitions:
        raise TransitionDenied(f"transition not in policy: {key}")
    allowed_roles = transitions[key].get("roles_any", [])
    if not allowed_roles:
        return  # no role gating
    actor_role = actor_type_of(actor)
    if actor_role not in allowed_roles:
        raise TransitionDenied(f"actor role '{actor_role}' not in {allowed_roles}")
