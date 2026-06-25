"""runner-service: FastAPI wrapper around the inline Playwright runner."""
from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import playwright_runner, storage

BUCKET = os.environ.get("MINIO_BUCKET", "executions")
DEFAULT_TIMEOUT = int(os.environ.get("SANDBOX_TIMEOUT_DEFAULT", "120"))

app = FastAPI(title="runner-service", version="0.1.0")


@app.on_event("startup")
async def _startup() -> None:
    # Make sure the bucket exists before any /runs request lands.
    storage.ensure_bucket(BUCKET)


class RunRequest(BaseModel):
    test_case: dict[str, Any] = Field(..., description="test_case artefact payload")
    target_url: str | None = None
    timeout_seconds: int = DEFAULT_TIMEOUT
    tenant_id: str
    workflow_id: str
    test_case_id: str


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/runs")
async def run(req: RunRequest) -> dict[str, Any]:
    try:
        return await playwright_runner.run_test_case(
            test_case=req.test_case,
            target_url=req.target_url,
            timeout_seconds=req.timeout_seconds,
            tenant_id=req.tenant_id,
            workflow_id=req.workflow_id,
            test_case_id=req.test_case_id,
            bucket=BUCKET,
        )
    except Exception as e:  # noqa: BLE001 — surface a clean 500 to callers
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}") from e
