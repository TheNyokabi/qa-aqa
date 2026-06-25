"""URN grammar for actor identity.

Single source of truth for the URN format. Mirrored by a Postgres CHECK
constraint on `artefacts.actor`. Changes here = data contract bump.
"""
from __future__ import annotations

import re

URN_PATTERN = r"^urn:qa-aqa:(agent|user|system|service):[A-Za-z0-9_\-]+(:v\d+)?$"
URN_RE = re.compile(URN_PATTERN)


def actor_type_of(urn: str) -> str:
    """Extract actor_type from a valid URN. Raises ValueError if invalid."""
    if not URN_RE.match(urn):
        raise ValueError(f"invalid URN: {urn}")
    return urn.split(":")[2]
