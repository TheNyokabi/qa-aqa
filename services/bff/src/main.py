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

from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import config
from .auth import User, create_token, load_users, verify_password
from .clients import artefact
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


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/auth/login", response_model=LoginResponse)
async def login(req: LoginRequest) -> LoginResponse:
    user = verify_password(req.email, req.password)
    if user is None:
        raise HTTPException(status_code=401, detail="invalid credentials")
    return LoginResponse(access_token=create_token(user), user=user)


@app.get("/api/me", response_model=User)
async def me(user: User = Depends(current_user)) -> User:
    return user


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
