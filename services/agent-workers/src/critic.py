"""Actor-Critic: cloud-only review of generated test cases.

Hard rule: critic ALWAYS runs on chat-prod (cloud). If no cloud key is
configured, critic is skipped with a recorded reason. Two weak models do
not a senior reviewer make.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from . import config

CRITIC_PROMPT = (
    "You are a Senior QA Reviewer. Evaluate each test case against these criteria:\n"
    "  1) Covers at least one negative path (invalid input / error condition / failure mode)\n"
    "  2) If the requirement involves ranges/dates/lengths, probes boundary values\n"
    "  3) Steps are unambiguous (two engineers would execute identically)\n"
    "Return ONLY JSON: { findings: [ {case_index, criterion, severity: 'must_fix'|'should_fix', suggestion: str} ] }.\n"
    "Return an empty findings array if all cases pass."
)


async def critique_cases(
    client: httpx.AsyncClient,
    cases: list[dict[str, Any]],
    criticality: str,
) -> tuple[list[dict[str, Any]], str | None]:
    """Returns (findings, skip_reason).

    findings is empty when cases pass or critic is skipped.
    skip_reason is None on success, otherwise a human-readable string.
    """
    if criticality in ("low",):
        return [], "skipped:low_criticality"
    if not config.ANTHROPIC_KEY:
        return [], "skipped:no_cloud_key"
    # criticality is medium/high/safety_critical AND cloud is available
    body = {
        "model": config.CHAT_PROD_MODEL,
        "messages": [
            {"role": "system", "content": CRITIC_PROMPT},
            {"role": "user", "content": json.dumps({"cases": cases})},
        ],
        "response_format": {"type": "json_object"},
        "metadata": {
            "agent_role": "test_designer_critic",
            "criticality": criticality,
        },
        "temperature": 0,
    }
    try:
        r = await client.post(f"{config.MODEL_GATEWAY_URL}/v1/chat/completions", json=body, timeout=120)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        findings = parsed.get("findings", [])
        return (findings if isinstance(findings, list) else []), None
    except Exception as e:  # noqa: BLE001 — critic must not break the workflow
        return [], f"skipped:critic_error:{type(e).__name__}"
