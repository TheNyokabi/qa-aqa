"""Build a per-workflow attestation object for compliance-regulated runs."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def build_attestation(state: dict[str, Any], cases: list[dict[str, Any]], compliance_level: str) -> dict[str, Any]:
    if compliance_level == "none":
        return {}
    output_blob = json.dumps(cases, sort_keys=True)
    return {
        "model_fingerprints": {
            state.get("model_used", "unknown"): "alias_only",  # real fingerprint requires registry lookup
        },
        "prompt_hashes": state.get("prompt_hashes", []),
        "rag_retrieval_ids": state.get("rag_retrieval_ids", []),
        "seed": 0,
        "policy_version": "default_v1",
        "rubric_version": "test_case_rubric_v1",
        "output_hash": "sha256:" + hashlib.sha256(output_blob.encode()).hexdigest(),
        "critic_skipped_reason": state.get("critic_skipped_reason", ""),
    }
