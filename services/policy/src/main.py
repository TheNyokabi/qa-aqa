import os
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

OPA_URL = os.environ.get("OPA_URL", "http://opa:8181")
OPA_DECISION_PATH = "/v1/data/qa_aqa/authz/allow"

app = FastAPI(title="policy-svc", version="0.1.0")
_client: httpx.AsyncClient | None = None


@app.on_event("startup")
async def _startup() -> None:
    global _client
    _client = httpx.AsyncClient(base_url=OPA_URL, timeout=httpx.Timeout(5.0))


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _client is not None:
        await _client.aclose()


class AuthorizeRequest(BaseModel):
    subject: dict[str, Any] = Field(..., description="Caller attributes (id, role, tenant)")
    action: str
    resource: dict[str, Any] = Field(default_factory=dict)


class AuthorizeResponse(BaseModel):
    allow: bool
    decision_id: str | None = None


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/authorize", response_model=AuthorizeResponse)
async def authorize(req: AuthorizeRequest) -> AuthorizeResponse:
    assert _client is not None
    try:
        r = await _client.post(OPA_DECISION_PATH, json={"input": req.model_dump()})
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"OPA call failed: {e}") from e
    body = r.json()
    return AuthorizeResponse(
        allow=bool(body.get("result", False)),
        decision_id=body.get("decision_id"),
    )
