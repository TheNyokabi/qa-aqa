"""Temporal activities — the side-effecting boundaries the workflow uses."""
from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from temporalio import activity

from . import config
from .agent import (
    DesignerState,
    PolicyDeniedError,
    SchemaValidationError,
    build_graph,
    finalize_or_raise,
)
from .attestation import build_attestation


def _http_client() -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {config.LITELLM_KEY}"} if config.LITELLM_KEY else {}
    # 300s read timeout: llama3.2:3b structured-JSON generation on CPU can take
    # 2-4 min for an exhaustive test-case set. The Temporal heartbeat ticker keeps
    # the activity alive independently; this timeout just bounds the inner call.
    return httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(300.0, connect=10.0))


def _artefact_client(tenant_id: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=config.ARTEFACT_URL,
        headers={"X-Tenant-ID": tenant_id, "Content-Type": "application/json"},
        timeout=httpx.Timeout(30.0),
    )


@activity.defn
async def create_artefact_activity(item: dict[str, Any]) -> dict[str, Any]:
    """POST /artefacts. `item.tenant_id` controls the tenant context."""
    tenant_id = item.pop("tenant_id", config.DEFAULT_TENANT)
    idempotency_key = item.pop("idempotency_key", None)
    async with _artefact_client(tenant_id) as client:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        r = await client.post("/artefacts", json=item, headers=headers)
        r.raise_for_status()
        return r.json()


@activity.defn
async def bulk_create_artefacts_activity(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """POST /artefacts/bulk. All items share the tenant of items[0]."""
    if not items:
        return []
    tenant_id = items[0].get("tenant_id", config.DEFAULT_TENANT)
    # The bulk endpoint takes idempotency keys in the body per-item.
    cleaned = [{**it, "tenant_id": tenant_id} for it in items]
    for it in cleaned:
        it.pop("tenant_id", None)
    async with _artefact_client(tenant_id) as client:
        r = await client.post("/artefacts/bulk", json={"items": cleaned})
        r.raise_for_status()
        return r.json()


@asynccontextmanager
async def _heartbeat_ticker(interval_seconds: float = 10.0):
    """Background task that calls activity.heartbeat() every N seconds.

    Decouples heartbeat cadence from in-node work (LLM calls can take 60-90s
    on llama3.2:3b — explicit per-node heartbeats are not enough).
    """
    stop = asyncio.Event()

    async def _loop() -> None:
        while not stop.is_set():
            try:
                activity.heartbeat()
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                continue

    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        stop.set()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.TimeoutError:
            task.cancel()


@activity.defn
async def run_test_designer_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Run the LangGraph agent under a background heartbeat ticker.

    Returns { "cases": [...], "attestation": {...} }.
    Raises PolicyDeniedError / SchemaValidationError (non-retryable in workflow policy).
    """
    async with _heartbeat_ticker(interval_seconds=10.0):
        async with _http_client() as client:
            graph = build_graph(client)
            initial: DesignerState = {
                "requirement": payload["requirement"],
                "criticality": payload.get("criticality", "low"),
                "tenant_id": payload.get("tenant_id", config.DEFAULT_TENANT),
                "workflow_id": payload.get("workflow_id", ""),
                "parent_id": payload.get("parent_id", ""),
                "schema_attempts": 0,
                "errors": [],
                "rag_retrieval_ids": [],
                "prompt_hashes": [],
            }
            try:
                final_state: DesignerState = await graph.ainvoke(initial)
            except PolicyDeniedError:
                raise
            cases = await finalize_or_raise(final_state)
    compliance = payload.get("compliance_level", "none")
    attestation = build_attestation(final_state, cases, compliance)
    return {
        "cases": cases,
        "attestation": attestation,
        "critic_skipped_reason": final_state.get("critic_skipped_reason", ""),
        "model_used": final_state.get("model_used", ""),
        "schema_attempts": final_state.get("schema_attempts", 0),
    }


@activity.defn
async def ingest_seed_docs_activity(tenant_id: str = config.DEFAULT_TENANT) -> dict[str, Any]:
    """Ingest the project spec docs into rag-service as a seed corpus.

    Idempotent — each doc is keyed by file sha; rag-service /ingest upserts.
    """
    activity.heartbeat()
    src = Path(config.DOCS_SOURCE)
    if not src.exists():
        return {"ingested": 0, "reason": f"source {src} not present"}
    md_files = sorted(src.glob("**/*.md"))
    if not md_files:
        return {"ingested": 0, "reason": "no markdown files"}
    headers = {"Authorization": f"Bearer {config.LITELLM_KEY}"} if config.LITELLM_KEY else {}
    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(120.0)) as client:
        n = 0
        for p in md_files:
            activity.heartbeat()
            text = p.read_text()
            doc_id = "doc:" + hashlib.sha256(p.read_bytes()).hexdigest()[:16]
            body = {
                "id": doc_id,
                "text": text,
                "metadata": {"source": str(p.name), "corpus": "docs"},
            }
            r = await client.post(f"{config.RAG_URL}/ingest", json=body)
            r.raise_for_status()
            n += 1
        return {"ingested": n, "source": str(src)}
