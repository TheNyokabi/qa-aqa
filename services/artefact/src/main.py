"""artefact-service: versioned, RLS-isolated artefact store.

Contract: see docs/superpowers/specs/2026-06-25-sub-project-d0_5-compliance-substrate-design.md
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import asyncpg
import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from .policy import DEFAULT_POLICY, TransitionDenied, resolve_policy_for, validate_transition
from .schema import SCHEMA_SQL
from .urn import URN_PATTERN

PGHOST = os.environ.get("PGHOST", "postgres")
PGPORT = int(os.environ.get("PGPORT", "5432"))
PGUSER = os.environ.get("PGUSER", "app")
PGPASSWORD = os.environ.get("PGPASSWORD", "appdevpw")
PGDATABASE = os.environ.get("PGDATABASE", "app")
SEED_DIR = Path(os.environ.get("SEED_DIR", "/app/seed"))

SEED_ACTOR = "urn:qa-aqa:system:seed"


# ── lifespan ─────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pg = await asyncpg.create_pool(
        host=PGHOST, port=PGPORT, user=PGUSER, password=PGPASSWORD, database=PGDATABASE,
        min_size=1, max_size=10,
    )
    # Schema (idempotent). Runs as owner; RLS is FORCED so we still need a tenant
    # context to insert seed rows below.
    async with app.state.pg.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
    await _seed(app.state.pg)
    try:
        yield
    finally:
        await app.state.pg.close()


app = FastAPI(title="artefact-service", version="0.1.0", lifespan=lifespan)


# ── tenancy ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def tenant_conn(pool: asyncpg.Pool, tenant_id: str):
    """Acquire a connection bound to the caller's tenant. RLS enforces it.

    Note: Postgres SET LOCAL does not accept bind parameters; use set_config()
    with is_local=true to get the same effect with a parameterised tenant.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
            yield conn


_TENANT_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def require_tenant(x_tenant_id: str | None = Header(None, alias="X-Tenant-ID")) -> str:
    if not x_tenant_id:
        raise HTTPException(status_code=400, detail="X-Tenant-ID header required")
    if not _TENANT_RE.fullmatch(x_tenant_id):
        raise HTTPException(status_code=400, detail="X-Tenant-ID invalid format")
    return x_tenant_id


# ── pydantic models ──────────────────────────────────────────────────────────
ArtefactType = Literal[
    "requirement", "test_case", "approval_policy", "critique_policy",
    "execution_result",  # D1.1 — payload schema is mode-discriminated
]
ComplianceLevel = Literal["none", "gxp", "iso17025", "sox"]


class CreateArtefactRequest(BaseModel):
    id: str | None = None
    type: ArtefactType
    payload: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)
    parent_id: str | None = None
    workflow_id: str | None = None
    actor: str = Field(..., pattern=URN_PATTERN)
    compliance_level: ComplianceLevel = "none"
    attestation: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None  # body-level mirror of header (workflows use this)


class BulkCreateRequest(BaseModel):
    items: list[CreateArtefactRequest]


class PatchRequest(BaseModel):
    payload: dict[str, Any]
    actor: str = Field(..., pattern=URN_PATTERN)


class TransitionRequest(BaseModel):
    to_state: str
    actor: str = Field(..., pattern=URN_PATTERN)
    attestation: dict[str, Any] = Field(default_factory=dict)


# ── helpers ──────────────────────────────────────────────────────────────────
def _gen_id(artefact_type: str) -> str:
    return f"{artefact_type}:{uuid.uuid4().hex[:12]}"


def _row_to_dict(row: asyncpg.Record | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for k in ("payload", "metadata", "attestation"):
        v = d.get(k)
        if isinstance(v, str):
            d[k] = json.loads(v)
    for k in ("created_at", "updated_at"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    return d


async def _fetch_artefact(conn: asyncpg.Connection, aid: str) -> dict[str, Any] | None:
    return _row_to_dict(await conn.fetchrow("SELECT * FROM artefacts WHERE id=$1", aid))


async def _do_create(
    conn: asyncpg.Connection,
    tenant_id: str,
    req: CreateArtefactRequest,
    idempotency_key: str | None,
) -> dict[str, Any]:
    aid = req.id or _gen_id(req.type)
    if idempotency_key:
        existing_id = await conn.fetchval(
            "SELECT artefact_id FROM idempotency_keys WHERE tenant_id=$1 AND key=$2",
            tenant_id, idempotency_key,
        )
        if existing_id:
            return await _fetch_artefact(conn, existing_id)
    row = await conn.fetchrow(
        """INSERT INTO artefacts (id, tenant_id, type, payload, metadata,
                                   parent_id, workflow_id, actor, compliance_level, attestation)
           VALUES ($1,$2,$3,$4::jsonb,$5::jsonb,$6,$7,$8,$9,$10::jsonb)
           ON CONFLICT (id) DO NOTHING
           RETURNING *""",
        aid, tenant_id, req.type, json.dumps(req.payload), json.dumps(req.metadata),
        req.parent_id, req.workflow_id, req.actor, req.compliance_level, json.dumps(req.attestation),
    )
    if row is None:
        # Existing row; don't mutate, return as-is
        existing = await _fetch_artefact(conn, aid)
        if existing is None:
            raise HTTPException(status_code=500, detail="conflict on insert but row missing")
        return existing
    if idempotency_key:
        await conn.execute(
            """INSERT INTO idempotency_keys (tenant_id, key, artefact_id)
               VALUES ($1,$2,$3) ON CONFLICT DO NOTHING""",
            tenant_id, idempotency_key, aid,
        )
    return _row_to_dict(row)


# ── endpoints ────────────────────────────────────────────────────────────────
@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/artefacts", status_code=201)
async def create_artefact(
    req: CreateArtefactRequest,
    tenant_id: str = Depends(require_tenant),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    key = idempotency_key or req.idempotency_key
    async with tenant_conn(app.state.pg, tenant_id) as conn:
        return await _do_create(conn, tenant_id, req, key)


@app.post("/artefacts/bulk", status_code=201)
async def bulk_create(
    body: BulkCreateRequest,
    tenant_id: str = Depends(require_tenant),
) -> list[dict[str, Any]]:
    out = []
    async with tenant_conn(app.state.pg, tenant_id) as conn:
        for item in body.items:
            out.append(await _do_create(conn, tenant_id, item, item.idempotency_key))
    return out


@app.get("/artefacts/{aid}")
async def get_artefact(aid: str, tenant_id: str = Depends(require_tenant)) -> dict[str, Any]:
    async with tenant_conn(app.state.pg, tenant_id) as conn:
        row = await _fetch_artefact(conn, aid)
        if row is None:
            raise HTTPException(status_code=404, detail="artefact not found")
        return row


@app.get("/artefacts")
async def list_artefacts(
    type: str | None = None,
    state: str | None = None,
    workflow_id: str | None = None,
    actor_type: str | None = None,
    tenant_id: str = Depends(require_tenant),
) -> list[dict[str, Any]]:
    clauses = []
    params: list[Any] = []
    if type:
        params.append(type)
        clauses.append(f"type=${len(params)}")
    if state:
        params.append(state)
        clauses.append(f"state=${len(params)}")
    if workflow_id:
        params.append(workflow_id)
        clauses.append(f"workflow_id=${len(params)}")
    if actor_type:
        params.append(actor_type)
        clauses.append(f"actor_type=${len(params)}")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT * FROM artefacts{where} ORDER BY created_at"
    async with tenant_conn(app.state.pg, tenant_id) as conn:
        rows = await conn.fetch(sql, *params)
        return [_row_to_dict(r) for r in rows]


@app.patch("/artefacts/{aid}")
async def patch_artefact(
    aid: str,
    body: PatchRequest,
    tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    async with tenant_conn(app.state.pg, tenant_id) as conn:
        cur = await conn.fetchrow("SELECT * FROM artefacts WHERE id=$1", aid)
        if cur is None:
            raise HTTPException(status_code=404, detail="artefact not found")
        # Write history row capturing the OLD state, then bump
        await conn.execute(
            """INSERT INTO artefact_history (tenant_id, artefact_id, version, state,
                                              payload, metadata, actor, attestation)
               VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8::jsonb)""",
            tenant_id, aid, cur["version"], cur["state"],
            cur["payload"], cur["metadata"], body.actor, cur["attestation"],
        )
        row = await conn.fetchrow(
            """UPDATE artefacts SET version=version+1, payload=$2::jsonb,
                  updated_at=now() WHERE id=$1 RETURNING *""",
            aid, json.dumps(body.payload),
        )
        return _row_to_dict(row)


@app.post("/artefacts/{aid}/transition")
async def transition_artefact(
    aid: str,
    body: TransitionRequest,
    tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    async with tenant_conn(app.state.pg, tenant_id) as conn:
        cur = await conn.fetchrow("SELECT * FROM artefacts WHERE id=$1", aid)
        if cur is None:
            raise HTTPException(status_code=404, detail="artefact not found")
        # Resolve policy: per-tenant override for this artefact type, else default
        policy_rows = await conn.fetch(
            """SELECT payload FROM artefacts
               WHERE type='approval_policy' AND state='approved'
               ORDER BY created_at DESC""",
        )
        applicable = []
        for r in policy_rows:
            p = json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"]
            if cur["type"] in p.get("applies_to", []):
                applicable.append(p)
        policy = resolve_policy_for(applicable)
        try:
            validate_transition(policy, cur["state"], body.to_state, body.actor)
        except TransitionDenied as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        await conn.execute(
            """INSERT INTO artefact_history (tenant_id, artefact_id, version, state,
                                              payload, metadata, actor, policy_version, attestation)
               VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8,$9::jsonb)""",
            tenant_id, aid, cur["version"], cur["state"],
            cur["payload"], cur["metadata"], body.actor,
            policy.get("name", "default_v1"), json.dumps(body.attestation),
        )
        row = await conn.fetchrow(
            "UPDATE artefacts SET state=$2, updated_at=now() WHERE id=$1 RETURNING *",
            aid, body.to_state,
        )
        return _row_to_dict(row)


@app.get("/artefacts/{aid}/history")
async def get_history(aid: str, tenant_id: str = Depends(require_tenant)) -> list[dict[str, Any]]:
    async with tenant_conn(app.state.pg, tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT * FROM artefact_history WHERE artefact_id=$1 ORDER BY id",
            aid,
        )
        out = []
        for r in rows:
            d = dict(r)
            for k in ("payload", "metadata", "attestation"):
                if isinstance(d.get(k), str):
                    d[k] = json.loads(d[k])
            if d.get("changed_at") is not None:
                d["changed_at"] = d["changed_at"].isoformat()
            out.append(d)
        return out


@app.get("/policies/approval/{target_type}")
async def get_approval_policy(target_type: str, tenant_id: str = Depends(require_tenant)) -> dict[str, Any]:
    async with tenant_conn(app.state.pg, tenant_id) as conn:
        rows = await conn.fetch(
            """SELECT payload FROM artefacts
               WHERE type='approval_policy' AND state IN ('draft','approved')
               ORDER BY created_at DESC""",
        )
        for r in rows:
            p = json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"]
            if target_type in p.get("applies_to", []):
                return p
        return DEFAULT_POLICY


# ── seed ─────────────────────────────────────────────────────────────────────
async def _seed(pool: asyncpg.Pool) -> None:
    """Ingest seed YAMLs as artefacts in tenant=default. Idempotent on file sha."""
    if not SEED_DIR.exists():
        return
    files = [
        ("approval_policy", SEED_DIR / "approval_policy.default_v1.yaml"),
        ("critique_policy", SEED_DIR / "critique_policy.test_case.v1.yaml"),
    ]
    async with tenant_conn(pool, "default") as conn:
        for atype, path in files:
            if not path.exists():
                continue
            raw = path.read_text()
            sha = hashlib.sha256(raw.encode()).hexdigest()
            existing = await conn.fetchval(
                "SELECT artefact_id FROM idempotency_keys WHERE tenant_id=$1 AND key=$2",
                "default", f"seed:{sha}",
            )
            if existing:
                continue
            payload = yaml.safe_load(raw)
            aid = f"{atype}:seed:{payload.get('name', sha[:8])}"
            await conn.execute(
                """INSERT INTO artefacts (id, tenant_id, type, payload, actor, state)
                   VALUES ($1,'default',$2,$3::jsonb,$4,'approved')
                   ON CONFLICT (id) DO NOTHING""",
                aid, atype, json.dumps(payload), SEED_ACTOR,
            )
            await conn.execute(
                """INSERT INTO idempotency_keys (tenant_id, key, artefact_id)
                   VALUES ('default',$1,$2) ON CONFLICT DO NOTHING""",
                f"seed:{sha}", aid,
            )
