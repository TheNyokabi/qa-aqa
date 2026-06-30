"""Executor agent — runs in one of three modes.

simulate:           LLM reasons step-by-step about likely outcome.
scripts:            LLM emits a Robot or Playwright script as artefact payload.
playwright_sandbox: Delegates to runner-service; returns its result verbatim.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from . import config

SIMULATE_PROMPT = (
    "You are a Senior Test Engineer simulating a test execution. Given the "
    "test_case and the system docs, reason step-by-step about whether each "
    "step passes. Return ONLY JSON: "
    "{ status: 'pass'|'fail'|'error', reasoning: str, "
    "predicted_failures: [str], confidence: number_between_0_and_1 }."
)

SCRIPTS_PLAYWRIGHT_PROMPT = (
    "Translate the test_case into a Playwright TypeScript test file. Use "
    "`import { test, expect } from '@playwright/test';`. Each step must map "
    "to a single Playwright API call. Output ONLY JSON: "
    "{ language: 'playwright', script_content: str, file_extension: '.spec.ts' }."
)

SCRIPTS_ROBOT_PROMPT = (
    "Translate the test_case into a Robot Framework test file. Use "
    "SeleniumLibrary keywords. Output ONLY JSON: "
    "{ language: 'robot', script_content: str, file_extension: '.robot' }."
)


async def fetch_context(client: httpx.AsyncClient, test_case: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Pull relevant docs from rag-service. Returns (hits, ids)."""
    query = test_case.get("title", "") + " " + (test_case.get("expected_result", "") or "")
    r = await client.post(f"{config.RAG_URL}/search", json={"query": query.strip() or "test", "k": 3})
    r.raise_for_status()
    hits = r.json().get("hits", [])
    return hits, [h.get("chunk_id", "") for h in hits]


async def simulate_via_llm(
    client: httpx.AsyncClient,
    test_case: dict[str, Any],
    docs: list[dict[str, Any]],
    *,
    tenant_id: str,
    workflow_id: str,
    criticality: str,
) -> dict[str, Any]:
    ctx = "\n\n".join(f"[doc] {d.get('text', '')}" for d in docs[:3])
    user = (
        f"Test case:\n{json.dumps(test_case, indent=2)}\n\n"
        f"Relevant docs:\n{ctx or '(none)'}"
    )
    body = {
        "model": config.CHAT_DEV_MODEL,
        "messages": [
            {"role": "system", "content": SIMULATE_PROMPT},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
        "metadata": {
            "tenant_id": tenant_id,
            "workflow_id": workflow_id,
            "agent_role": "executor",
            "mode": "simulate",
            "criticality": criticality,
        },
        "temperature": 0,
    }
    r = await client.post(f"{config.MODEL_GATEWAY_URL}/v1/chat/completions", json=body)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return {
            "mode": "simulate",
            "status": "error",
            "reasoning": "LLM returned non-JSON",
            "predicted_failures": [content[:200]],
            "confidence": 0.0,
        }
    parsed.setdefault("mode", "simulate")
    parsed.setdefault("status", "error")
    parsed.setdefault("reasoning", "")
    parsed.setdefault("predicted_failures", [])
    parsed.setdefault("confidence", 0.0)
    return parsed


async def generate_script(
    client: httpx.AsyncClient,
    test_case: dict[str, Any],
    docs: list[dict[str, Any]],
    language: str,
    *,
    tenant_id: str,
    workflow_id: str,
    criticality: str,
) -> dict[str, Any]:
    prompt = SCRIPTS_PLAYWRIGHT_PROMPT if language == "playwright" else SCRIPTS_ROBOT_PROMPT
    ctx = "\n\n".join(f"[doc] {d.get('text', '')}" for d in docs[:3])
    user = (
        f"Test case:\n{json.dumps(test_case, indent=2)}\n\n"
        f"Relevant docs:\n{ctx or '(none)'}"
    )
    body = {
        "model": config.CHAT_DEV_MODEL,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
        "metadata": {
            "tenant_id": tenant_id,
            "workflow_id": workflow_id,
            "agent_role": "executor",
            "mode": "scripts",
            "criticality": criticality,
        },
        "temperature": 0,
    }
    r = await client.post(f"{config.MODEL_GATEWAY_URL}/v1/chat/completions", json=body)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return {
            "mode": "scripts",
            "language": language,
            "script_content": "",
            "file_extension": ".spec.ts" if language == "playwright" else ".robot",
        }
    parsed.setdefault("mode", "scripts")
    parsed.setdefault("language", language)
    parsed.setdefault("script_content", "")
    parsed.setdefault("file_extension", ".spec.ts" if language == "playwright" else ".robot")
    return parsed


async def run_in_sandbox(
    client: httpx.AsyncClient,
    test_case: dict[str, Any],
    target_url: str | None,
    timeout_seconds: int,
    *,
    tenant_id: str,
    workflow_id: str,
    test_case_id: str,
    allowed_urls: list[str] | None = None,
) -> dict[str, Any]:
    """D1.4b — runner-service /runs is async: POST returns 202+run_id,
    we then poll /runs/{id} until terminal. The Temporal heartbeat ticker
    around the activity keeps things alive during the poll."""
    import asyncio as _asyncio

    body = {
        "test_case": test_case,
        "target_url": target_url,
        "timeout_seconds": timeout_seconds,
        "tenant_id": tenant_id,
        "workflow_id": workflow_id,
        "test_case_id": test_case_id,
        "allowed_urls": allowed_urls or [],
    }
    r = await client.post(f"{config.RUNNER_URL}/runs", json=body, timeout=30.0)
    if r.status_code == 429:
        # Quota exceeded — return as a structured error result the workflow
        # can persist. Don't raise: this is a deterministic outcome, not a
        # transient failure to retry.
        return {
            "mode": "playwright_sandbox",
            "status": "error",
            "error_message": f"quota exceeded: {r.text}",
            "screenshots": [],
            "videos": [],
            "console_log_url": "",
            "duration_ms": 0,
        }
    r.raise_for_status()
    run_id = r.json()["run_id"]

    # Poll. Overall budget is timeout_seconds + 60s (covers cold start, queue wait).
    deadline = timeout_seconds + 60
    elapsed = 0
    poll_interval = 2.0
    while elapsed < deadline:
        await _asyncio.sleep(poll_interval)
        elapsed += poll_interval
        s = await client.get(f"{config.RUNNER_URL}/runs/{run_id}", timeout=30.0)
        if s.status_code == 404:
            return {
                "mode": "playwright_sandbox",
                "status": "error",
                "error_message": f"run {run_id} disappeared (expired or never queued)",
                "screenshots": [],
                "videos": [],
                "console_log_url": "",
                "duration_ms": 0,
            }
        s.raise_for_status()
        state = s.json()
        status = state.get("status")
        if status in ("completed", "failed"):
            result = state.get("result") or {}
            if status == "failed" and not result:
                return {
                    "mode": "playwright_sandbox",
                    "status": "error",
                    "error_message": state.get("error", "run failed"),
                    "screenshots": [],
                    "videos": [],
                    "console_log_url": "",
                    "duration_ms": 0,
                }
            result.setdefault("mode", "playwright_sandbox")
            return result
    # Polling timed out — return a synthetic timeout result. The actual sandbox
    # might still be running; the workflow gets a clear signal.
    return {
        "mode": "playwright_sandbox",
        "status": "timeout",
        "error_message": f"runner-service poll timed out after {deadline}s for run {run_id}",
        "screenshots": [],
        "videos": [],
        "console_log_url": "",
        "duration_ms": deadline * 1000,
    }
