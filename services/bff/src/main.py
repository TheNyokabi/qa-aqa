"""BFF entry — D3a routes only.

D3a:
    POST /api/auth/login
    GET  /api/me
    GET  /api/workflows
    GET  /api/workflows/{id}
    GET  /api/artefacts/{id}
    GET  /api/artefacts/{id}/history
    POST /api/artefacts/{id}/transition
    GET  /api/policies/approval/{type}
    GET  /api/health
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import config
from .auth import User, create_token, load_users, verify_password
from .clients import artefact, temporal as temporal_client
from .deps import current_user, require_role


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_users()
    yield


app = FastAPI(title="bff", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    user: User


class TransitionRequest(BaseModel):
    to_state: str


class DesignTestsRequest(BaseModel):
    requirement: dict[str, Any]
    criticality: str = "low"


class ExecuteTestsRequest(BaseModel):
    test_case_ids: list[str]
    mode: str
    target_url: str | None = None
    allowed_urls: list[str] = []
    sandbox_timeout_seconds: int = 120
    language: str | None = None
    criticality: str = "low"


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/auth/login", response_model=LoginResponse)
async def login(req: LoginRequest) -> LoginResponse:
    user = verify_password(req.email, req.password)
    if user is None:
        raise HTTPException(status_code=401, detail="invalid credentials")
    return LoginResponse(access_token=create_token(user), user=user)


@app.get("/api/me")
async def me(user: User = Depends(current_user)) -> dict[str, Any]:
    """Returns user identity + live quota status for the caller's tenant.

    D1.4a — quota is fetched from runner-service /quota/{tenant_id}. Failure
    to reach runner is non-fatal — we just omit the quota field.
    """
    out: dict[str, Any] = {
        "email": user.email,
        "role": user.role,
        "urn": user.urn,
        "tenant_id": user.tenant_id,
    }
    try:
        runner_url = config.__dict__.get(
            "RUNNER_URL",
            os.environ.get("RUNNER_URL", "http://runner-service:8004"),
        )
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{runner_url}/quota/{user.tenant_id}")
            if r.status_code == 200:
                out["quota"] = r.json()
    except Exception:
        pass
    return out


@app.get("/api/workflows")
async def list_workflows(
    type: str | None = None,
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """List distinct workflow_ids (grouped over artefacts) for the caller's tenant.

    The minimal v1 sources workflows from the artefact-service rather than
    Temporal — it's cheaper and surfaces the *output* the user cares about.
    """
    arts = await artefact.list_artefacts(user.tenant_id, {})
    seen: dict[str, dict[str, Any]] = {}
    for a in arts:
        wf = a.get("workflow_id")
        if not wf:
            continue
        entry = seen.setdefault(wf, {
            "workflow_id": wf,
            "tenant_id": a.get("tenant_id"),
            "artefact_count": 0,
            "types": set(),
            "first_seen": a.get("created_at"),
            "last_seen": a.get("created_at"),
        })
        entry["artefact_count"] += 1
        entry["types"].add(a.get("type"))
        if a.get("created_at") and (entry["first_seen"] or "") > a.get("created_at"):
            entry["first_seen"] = a.get("created_at")
        if a.get("created_at") and (entry["last_seen"] or "") < a.get("created_at"):
            entry["last_seen"] = a.get("created_at")
    out = [
        {**v, "types": sorted(v["types"])}
        for v in seen.values()
    ]
    out.sort(key=lambda x: x.get("last_seen") or "", reverse=True)
    return {"workflows": out}


@app.get("/api/workflows/{wf_id:path}")
async def workflow_detail(
    wf_id: str,
    user: User = Depends(current_user),
) -> dict[str, Any]:
    arts = await artefact.list_artefacts(user.tenant_id, {"workflow_id": wf_id})
    by_type: dict[str, list[dict[str, Any]]] = {}
    for a in arts:
        by_type.setdefault(a.get("type", "unknown"), []).append(a)
    return {"workflow_id": wf_id, "artefacts_by_type": by_type, "total": len(arts)}


@app.get("/api/artefacts/{aid}")
async def get_artefact(aid: str, user: User = Depends(current_user)) -> dict[str, Any]:
    a = await artefact.get_artefact(user.tenant_id, aid)
    if a is None:
        raise HTTPException(status_code=404, detail="not found")
    return a


@app.get("/api/artefacts/{aid}/history")
async def artefact_history(aid: str, user: User = Depends(current_user)) -> list[dict[str, Any]]:
    return await artefact.history(user.tenant_id, aid)


@app.post("/api/artefacts/{aid}/transition")
async def artefact_transition(
    aid: str,
    body: TransitionRequest,
    user: User = Depends(require_role("reviewer")),
) -> dict[str, Any]:
    try:
        return await artefact.transition(user.tenant_id, aid, body.to_state, user.urn)
    except httpx.HTTPStatusError as e:
        # Surface 409 (state machine) and 404 cleanly
        raise HTTPException(status_code=e.response.status_code, detail=str(e.response.text)) from e


@app.get("/api/policies/approval/{target_type}")
async def approval_policy(target_type: str, user: User = Depends(current_user)) -> dict[str, Any]:
    return await artefact.approval_policy(user.tenant_id, target_type)


# ── D3b — Designer wizard ────────────────────────────────────────────────────


@app.post("/api/workflows/design-tests")
async def start_design_tests(
    body: DesignTestsRequest,
    user: User = Depends(require_role("reviewer")),
) -> dict[str, str]:
    import uuid
    wf_id = f"{user.tenant_id}:design-tests:{uuid.uuid4().hex[:12]}"
    req = {
        **body.requirement,
        "criticality": body.criticality,
    }
    actual_id = await temporal_client.start_design_tests(wf_id, req)
    return {"workflow_id": actual_id}


@app.get("/api/workflow-status/{wf_id:path}")
async def workflow_status(
    wf_id: str,
    user: User = Depends(current_user),
) -> dict[str, Any]:
    # NB: path is /workflow-status/ (not /workflows/status/) because FastAPI's
    # /api/workflows/{wf_id:path} from D3a would otherwise shadow this route
    # — it'd match `status/<id>` as the wf_id wildcard.
    if not wf_id.startswith(f"{user.tenant_id}:"):
        raise HTTPException(status_code=403, detail="cross-tenant workflow access denied")
    return await temporal_client.workflow_status(wf_id)


# ── D3c — Executor monitor + media proxy ─────────────────────────────────────


@app.post("/api/workflows/execute-tests")
async def start_execute_tests(
    body: ExecuteTestsRequest,
    user: User = Depends(require_role("reviewer")),
) -> dict[str, str]:
    import uuid
    wf_id = f"{user.tenant_id}:execute-tests:{uuid.uuid4().hex[:12]}"
    payload: dict[str, Any] = {
        "test_case_ids": body.test_case_ids,
        "mode": body.mode,
        "criticality": body.criticality,
    }
    if body.target_url:
        payload["target_url"] = body.target_url
    if body.allowed_urls:
        payload["allowed_urls"] = body.allowed_urls
    if body.sandbox_timeout_seconds:
        payload["sandbox_timeout_seconds"] = body.sandbox_timeout_seconds
    if body.language:
        payload["language"] = body.language
    actual_id = await temporal_client.start_execute_tests(wf_id, payload)
    return {"workflow_id": actual_id}


# Media access: instead of presigned URLs (which would need MinIO CORS config),
# BFF acts as a tenant-scoped proxy. Browser fetches /api/media?key=... and
# BFF streams the object from MinIO after verifying the key belongs to the
# caller's tenant. Same security guarantee, simpler dev story.
import boto3
import re as _re
from botocore.config import Config as _BotoConfig
from fastapi.responses import StreamingResponse

_S3_KEY_RE = _re.compile(r"^executions/([^/]+)/")


def _s3() -> Any:
    return boto3.client(
        "s3",
        endpoint_url=f"http://{config.MINIO_ENDPOINT}",
        aws_access_key_id=config.MINIO_USER,
        aws_secret_access_key=config.MINIO_PASS,
        config=_BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
        region_name="us-east-1",
    )


@app.get("/api/media")
async def media(
    key: str,
    user: User = Depends(current_user),
) -> StreamingResponse:
    """Stream a MinIO object after verifying the key is in the caller's tenant scope.

    Allowed prefix: `executions/<tenant_id>/`. Anything else → 403.
    """
    m = _S3_KEY_RE.match(key)
    if not m:
        raise HTTPException(status_code=403, detail="key must start with executions/<tenant>/...")
    if m.group(1) != user.tenant_id:
        raise HTTPException(status_code=403, detail="cross-tenant media access denied")
    try:
        obj = _s3().get_object(Bucket=config.MINIO_BUCKET, Key=key)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=f"not found: {e}") from e
    content_type = obj.get("ContentType") or "application/octet-stream"
    body = obj["Body"]

    def _iter():
        try:
            while True:
                chunk = body.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            body.close()

    return StreamingResponse(_iter(), media_type=content_type)
