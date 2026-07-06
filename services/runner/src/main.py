"""runner-service v0.2 — async runs queue + per-tenant quotas.

D1.4a: per-tenant concurrent + daily quotas via Valkey counters.
D1.4b: POST /runs returns 202 + run_id; a background worker consumes
       runs:queue (Valkey LIST) and executes sandboxes serially.

External contract (callers see):
    POST /runs        -> 202 {run_id, status}
    GET  /runs/{id}   -> 200 {status, result?, error?}
    GET  /quota/{tid} -> 200 {concurrent: {...}, daily: {...}}
    GET  /health      -> 200 {status}
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import queue as queue_mod
from . import sandbox_executor, storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("runner-service")

BUCKET = os.environ.get("MINIO_BUCKET", "executions")
DEFAULT_TIMEOUT = int(os.environ.get("SANDBOX_TIMEOUT_DEFAULT", "120"))

app = FastAPI(title="runner-service", version="0.2.0")
_q = queue_mod.Queue()
_worker_task: asyncio.Task | None = None
_stop_event = asyncio.Event()


# ── lifecycle ────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def _startup() -> None:
    storage.ensure_bucket(BUCKET)
    n = sandbox_executor.reap_orphans_blocking()
    if n:
        log.info("reaped %d orphan sandbox container(s) from prior runs", n)
    await _q.connect()
    recovered = await _q.recover_orphans()
    if recovered:
        log.info("recovered %d orphan run(s) from prior crash", recovered)
    global _worker_task
    _worker_task = asyncio.create_task(_worker_loop())
    log.info("worker loop started; quota concurrent=%d daily=%d",
             queue_mod.QUOTA_CONCURRENT_DEFAULT, queue_mod.QUOTA_DAILY_DEFAULT)


@app.on_event("shutdown")
async def _shutdown() -> None:
    _stop_event.set()
    if _worker_task:
        try:
            await asyncio.wait_for(_worker_task, timeout=10.0)
        except asyncio.TimeoutError:
            _worker_task.cancel()
    await _q.close()


# ── models ───────────────────────────────────────────────────────────────────
class RunRequest(BaseModel):
    test_case: dict[str, Any] = Field(..., description="test_case artefact (full artefact OR just payload)")
    target_url: str | None = None
    timeout_seconds: int = DEFAULT_TIMEOUT
    tenant_id: str
    workflow_id: str
    test_case_id: str
    allowed_urls: list[str] = Field(default_factory=list)


class SubmitResponse(BaseModel):
    run_id: str
    status: str


# ── endpoints ────────────────────────────────────────────────────────────────
@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/runs", status_code=202, response_model=SubmitResponse)
async def submit_run(req: RunRequest) -> SubmitResponse:
    try:
        run_id = await _q.submit(req.tenant_id, req.model_dump())
    except queue_mod.QuotaExceeded as e:
        raise HTTPException(
            status_code=429,
            detail={
                "detail": str(e),
                "kind": f"quota_{e.kind}",
                "current": e.current,
                "max": e.maximum,
            },
        )
    return SubmitResponse(run_id=run_id, status="queued")


@app.get("/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    state = await _q.get(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="run not found or expired")
    return state


@app.get("/quota/{tenant_id}")
async def get_quota(tenant_id: str) -> dict[str, Any]:
    return await _q.quota_status(tenant_id)


class CancelRequest(BaseModel):
    actor_urn: str
    tenant_id: str


@app.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, body: CancelRequest) -> dict[str, Any]:
    state = await _q.get(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="run not found")
    current_status = state.get("status", "")
    if current_status == "queued":
        r = await _q.cancel_queued(run_id, body.actor_urn, body.tenant_id)
        if r.code == 3:
            raise HTTPException(status_code=404, detail="run not found")
        if r.code == 1:
            raise HTTPException(status_code=403, detail="cross-tenant cancel forbidden")
        if r.code == 2:
            raise HTTPException(status_code=409,
                                detail={"detail": "run already terminal", "status": r.previous_status})
        return {"run_id": run_id, "status": "canceled", "previous_status": r.previous_status}
    if current_status in ("completed", "failed", "canceled"):
        raise HTTPException(status_code=409,
                            detail={"detail": "run already terminal", "status": current_status})
    # current_status == "running" — implemented in Task 5
    raise HTTPException(status_code=501, detail="running-cancel not yet implemented")


# ── worker loop ──────────────────────────────────────────────────────────────
async def _worker_loop() -> None:
    """Consume from Valkey queue and execute sandboxes serially.

    Concurrency control is the QUOTA_CONCURRENT_DEFAULT cap (enforced at submit),
    not local parallelism. This loop processes one run at a time per worker; to
    scale, run additional runner-service replicas (D1.4.1).
    """
    log.info("worker: polling queue")
    while not _stop_event.is_set():
        try:
            run_id = await _q.claim_next(timeout=5)
        except Exception as e:  # noqa: BLE001
            log.warning("worker: claim_next failed: %s", e)
            await asyncio.sleep(1)
            continue
        if not run_id:
            continue
        await _execute_run(run_id)


async def _execute_run(run_id: str) -> None:
    state = await _q.get(run_id)
    if not state:
        log.warning("worker: claimed %s but no state", run_id)
        return
    tenant_id = state.get("tenant_id", "default")
    req = state.get("request") or {}
    try:
        await _q.mark_running(run_id)
        sandbox_id = uuid.uuid4().hex[:12]
        result = await sandbox_executor.run_sandbox(
            test_case=req["test_case"],
            target_url=req.get("target_url"),
            timeout_seconds=req.get("timeout_seconds", DEFAULT_TIMEOUT),
            tenant_id=tenant_id,
            workflow_id=req.get("workflow_id", ""),
            test_case_id=req.get("test_case_id", "unknown"),
            sandbox_id=sandbox_id,
            bucket=BUCKET,
            allowed_urls=req.get("allowed_urls", []),
        )
        await _q.mark_completed(run_id, tenant_id, result)
        log.info("worker: %s completed (status=%s)", run_id, result.get("status", "?"))
    except Exception as e:  # noqa: BLE001
        log.exception("worker: %s failed", run_id)
        await _q.mark_failed(run_id, tenant_id, f"{type(e).__name__}: {e}")
