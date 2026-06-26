"""runner-service: per-/runs ephemeral Playwright sandbox (D1.2).

Same external contract as D1.1 — POST /runs returns the same payload shape.
What changed under the hood: each call spawns an ephemeral container on a
network with no route to internal services.
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import sandbox_executor, storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("runner-service")

BUCKET = os.environ.get("MINIO_BUCKET", "executions")
DEFAULT_TIMEOUT = int(os.environ.get("SANDBOX_TIMEOUT_DEFAULT", "120"))

app = FastAPI(title="runner-service", version="0.2.0")


@app.on_event("startup")
async def _startup() -> None:
    storage.ensure_bucket(BUCKET)
    n = sandbox_executor.reap_orphans_blocking()
    if n:
        log.info("reaped %d orphan sandbox container(s) from prior runs", n)


class RunRequest(BaseModel):
    test_case: dict[str, Any] = Field(..., description="test_case artefact (full artefact OR just payload)")
    target_url: str | None = None
    timeout_seconds: int = DEFAULT_TIMEOUT
    tenant_id: str
    workflow_id: str
    test_case_id: str
    # D1.3 — strict-default deny: missing/empty list means the sandbox cannot
    # reach anything through the proxy.
    allowed_urls: list[str] = Field(default_factory=list)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/runs")
async def run(req: RunRequest) -> dict[str, Any]:
    sandbox_id = uuid.uuid4().hex[:12]
    try:
        return await sandbox_executor.run_sandbox(
            test_case=req.test_case,
            target_url=req.target_url,
            timeout_seconds=req.timeout_seconds,
            tenant_id=req.tenant_id,
            workflow_id=req.workflow_id,
            test_case_id=req.test_case_id,
            sandbox_id=sandbox_id,
            bucket=BUCKET,
            allowed_urls=req.allowed_urls,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("sandbox run failed (id=%s)", sandbox_id)
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}") from e
