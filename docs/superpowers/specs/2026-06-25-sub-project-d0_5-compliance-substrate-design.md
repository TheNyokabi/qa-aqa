# Sub-project D0.5 — compliance & multi-tenancy substrate

**Date:** 2026-06-25
**Scope:** Single new service — `artefact-service` — ships the entire compliance + multi-tenancy substrate that D1 (agent-workers) will sit on top of. No agent code, no LLM calls, no workflow execution.
**Depends on:** Sub-projects 0+A+B+C committed at `6ad7e1c`.

## Why this exists as its own sub-project

The original D1 spec grew from 262 → 419 lines after reviews. The growth concentrated in **substrate concerns** (RLS, URN, attestation, policy lookup, idempotency, audit hygiene) rather than agent concerns. These have subtle failure modes (GUC bleed, RLS bypass, URN regex drift) that benefit from landing standalone and being smoked in isolation. Once green, D1 just calls APIs.

## Locked decisions

- **One service:** `artefact-service` (FastAPI, Postgres). Port `8003`.
- **Multi-tenancy from day 1**: every tenant-scoped table has `tenant_id text NOT NULL DEFAULT 'default'`, RLS enabled, composite indices lead with `tenant_id`.
- **Tenant propagation:** `X-Tenant-ID` header required on all artefact endpoints. Connection pool sets `app.tenant_id` GUC per request before query.
- **URN grammar:** `urn:qa-aqa:<actor_type>:<id>[:v<version>]`, `actor_type ∈ {agent, user, system, service}`. Compiled Pydantic regex validator.
- **Idempotency keys are tenant-scoped:** primary key `(tenant_id, key)` — no cross-tenant collisions possible.
- **Approval policy is itself an artefact** (`type=approval_policy`). State transitions resolve a policy artefact per `(tenant_id, target_artefact_type)`; falls back to a built-in `default_v1` when no override.
- **Critique policy is itself an artefact** (`type=critique_policy`). Stored for D1's critic to consume; D0.5 only ships CRUD.
- **Attestation field is in the schema from day 1**, defaults to `{}`, immutable once written for `compliance_level != 'none'`.
- **State machine config is data, not code:** `default_v1` lives in seed YAML, gets ingested as an artefact on startup.

## Artefact types in D0.5

| Type | Owns it | Purpose |
|---|---|---|
| `approval_policy` | seed YAML on boot | State machine config per `(tenant, target_type)` |
| `critique_policy` | seed YAML on boot | Rubric for D1's critic. D0.5 stores it; doesn't read it. |

(D1 will add: `requirement`, `test_case`. D1.1+: `defect`, `evidence`, `report`.)

## Service: `artefact-service`

Built locally from `services/artefact/`. Same shape as rag-service + policy-svc — FastAPI + uvicorn in a `python:3.12-slim` Containerfile.

### Endpoints

**All `/artefacts*` endpoints require `X-Tenant-ID`** (missing → HTTP 400). `/health` does not.

| Method | Path | Behaviour |
|---|---|---|
| GET | `/health` | `{"status":"ok"}` |
| POST | `/artefacts` | body: `{ id?, type, payload, metadata?, parent_id?, workflow_id?, actor, compliance_level?, attestation? }`. Header `Idempotency-Key: <opaque>` optional. `actor` URN-validated. On duplicate `Idempotency-Key` OR id collision → return existing row unchanged (no version bump, no audit row). Implemented as `INSERT … ON CONFLICT (id) DO NOTHING RETURNING *`, then `SELECT` on no rows. |
| POST | `/artefacts/bulk` | body: `{ items: [...] }`. Single transaction. Same idempotency semantics per item. |
| GET | `/artefacts/{id}` | Returns current version. |
| GET | `/artefacts?type=&state=&workflow_id=&actor_type=` | Filtered list. RLS scopes to caller's tenant. |
| PATCH | `/artefacts/{id}` | body: `{ payload, actor }`. Bumps version, writes history row. State machine NOT touched. |
| POST | `/artefacts/{id}/transition` | body: `{ to_state, actor, attestation? }`. Resolves approval_policy → validates transition + required actor roles → writes audit row with `policy_version`. |
| GET | `/artefacts/{id}/history` | Full version + state history. |
| GET | `/policies/approval/{target_type}` | Active approval_policy for caller's tenant + target type. Falls back to `default_v1`. |

### Pydantic models (shape only)

```python
URN_RE = re.compile(r"^urn:qa-aqa:(agent|user|system|service):[A-Za-z0-9_\-]+(:v\d+)?$")

class CreateArtefactRequest(BaseModel):
    id: str | None = None
    type: Literal["approval_policy", "critique_policy", "requirement", "test_case"]
    payload: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)
    parent_id: str | None = None
    workflow_id: str | None = None
    actor: str = Field(..., pattern=URN_RE.pattern)
    compliance_level: Literal["none", "gxp", "iso17025", "sox"] = "none"
    attestation: dict[str, Any] = Field(default_factory=dict)

class TransitionRequest(BaseModel):
    to_state: str
    actor: str = Field(..., pattern=URN_RE.pattern)
    attestation: dict[str, Any] = Field(default_factory=dict)
```

### Postgres schema (created on startup, idempotent)

```sql
CREATE TABLE IF NOT EXISTS artefacts (
    id              text PRIMARY KEY,
    tenant_id       text NOT NULL DEFAULT 'default',
    type            text NOT NULL,
    version         int  NOT NULL DEFAULT 1,
    state           text NOT NULL DEFAULT 'draft',
    payload         jsonb NOT NULL,
    metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
    parent_id       text REFERENCES artefacts(id),
    workflow_id     text,
    actor           text NOT NULL CHECK (actor ~ '^urn:qa-aqa:(agent|user|system|service):[A-Za-z0-9_\-]+(:v[0-9]+)?$'),
    actor_type      text GENERATED ALWAYS AS (split_part(actor, ':', 3)) STORED,
    attestation     jsonb NOT NULL DEFAULT '{}'::jsonb,
    compliance_level text NOT NULL DEFAULT 'none'
                    CHECK (compliance_level IN ('none','gxp','iso17025','sox')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS artefacts_tenant_type_state_idx ON artefacts(tenant_id, type, state);
CREATE INDEX IF NOT EXISTS artefacts_tenant_workflow_idx ON artefacts(tenant_id, workflow_id);
CREATE INDEX IF NOT EXISTS artefacts_tenant_actor_type_idx ON artefacts(tenant_id, actor_type);

ALTER TABLE artefacts ENABLE ROW LEVEL SECURITY;
CREATE POLICY artefacts_tenant_isolation ON artefacts
    USING (tenant_id = current_setting('app.tenant_id', true));

CREATE TABLE IF NOT EXISTS idempotency_keys (
    tenant_id   text NOT NULL,
    key         text NOT NULL,
    artefact_id text NOT NULL REFERENCES artefacts(id) ON DELETE CASCADE,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, key)
);
ALTER TABLE idempotency_keys ENABLE ROW LEVEL SECURITY;
CREATE POLICY idempotency_keys_tenant_isolation ON idempotency_keys
    USING (tenant_id = current_setting('app.tenant_id', true));

CREATE TABLE IF NOT EXISTS artefact_history (
    id             bigserial PRIMARY KEY,
    tenant_id      text NOT NULL DEFAULT 'default',
    artefact_id    text NOT NULL REFERENCES artefacts(id) ON DELETE CASCADE,
    version        int  NOT NULL,
    state          text NOT NULL,
    payload        jsonb NOT NULL,
    metadata       jsonb NOT NULL,
    actor          text NOT NULL,
    policy_version text,
    attestation    jsonb NOT NULL DEFAULT '{}'::jsonb,
    changed_at     timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE artefact_history ENABLE ROW LEVEL SECURITY;
CREATE POLICY artefact_history_tenant_isolation ON artefact_history
    USING (tenant_id = current_setting('app.tenant_id', true));
```

### Connection pool wiring (critical for RLS correctness)

The Postgres `app.tenant_id` GUC must be **set before every query AND reset before the connection returns to the pool** — otherwise tenant context bleeds across requests.

Pattern in artefact-service:

```python
@asynccontextmanager
async def tenant_connection(pool: asyncpg.Pool, tenant_id: str) -> asyncpg.Connection:
    async with pool.acquire() as conn:
        # SET LOCAL so it auto-resets at transaction end
        async with conn.transaction():
            await conn.execute("SET LOCAL app.tenant_id = $1", tenant_id)
            yield conn
        # Outside the transaction, the GUC is reset by Postgres itself
```

Every endpoint that touches `artefacts*` uses this context manager. Endpoints that don't get a connection without a tenant header → fail before opening one.

### Seed-on-boot (idempotent)

artefact-service startup:
1. Run schema migrations
2. Ingest `services/artefact/seed/approval_policy.default_v1.yaml` as an `approval_policy` artefact in tenant `default`, actor `urn:qa-aqa:system:seed`, idempotency key = sha of yaml
3. Ingest `services/artefact/seed/critique_policy.test_case.v1.yaml` similarly

Seeding is gated on the sha — if the yaml hasn't changed, no new artefact is created.

### `default_v1` approval policy (seed YAML)

```yaml
# approval_policy.default_v1
name: default_v1
applies_to: [requirement, test_case, approval_policy, critique_policy]
states: [draft, in_review, approved, archived]
transitions:
  draft -> in_review:    { roles_any: [agent, user] }
  draft -> archived:     { roles_any: [user] }
  in_review -> approved: { roles_any: [user] }
  in_review -> draft:    { roles_any: [user] }
  in_review -> archived: { roles_any: [user] }
  approved -> archived:  { roles_any: [user] }
```

D1.1 will introduce per-tenant overrides. D0.5 only ships the lookup mechanism + this fallback.

## Updates to `infra.sh`

| Section | Change |
|---|---|
| 1 Variables | `IMG_ARTEFACT="qa-aqa/artefact-service:dev"`, `PORT_ARTEFACT=8003` |
| 5a build_custom_images | Loop covers `rag`, `policy`, `artefact` |
| 5 compose | Append `artefact-service` (depends on postgres healthy) |
| 6 wait_healthy | Add `artefact-service` |
| 8 smoke | 10 new tests (below) |
| Endpoint summary | Add `artefact-service http://localhost:8003` |

## Smoke tests for D0.5

| # | Test | Verifies |
|---|---|---|
| 1 | `GET /health` → 200 `{"status":"ok"}` | service up |
| 2 | `POST /artefacts` without `X-Tenant-ID` → 400 | RLS gate enforced before DB touched |
| 3 | `POST /artefacts` with malformed URN actor (`"alice"`) → 422 | Pydantic URN validation |
| 4 | `POST /artefacts` with valid URN + tenant header → 201, payload echoed, `state=draft`, `version=1` | happy path create |
| 5 | Same `POST` repeated with same `Idempotency-Key` → returns identical row, no version bump | idempotency contract |
| 6 | `POST /artefacts/bulk` with 3 items in one call → returns 3 rows, all `state=draft`, single transaction (verified by `actor_history` count = 0) | bulk + audit hygiene |
| 7 | `PATCH /artefacts/{id}` → version=2, history has 1 row, state unchanged | versioning without state-touch |
| 8 | `POST /artefacts/{id}/transition` to disallowed state (`approved` from `draft`) → 409 + reason | approval_policy lookup |
| 9 | `POST /artefacts/{id}/transition` to allowed state (`in_review` from `draft`) → 200, history row contains `policy_version: default_v1` | policy_version captured in audit |
| 10 | `GET /policies/approval/test_case` → returns the seed `default_v1` body | seed-on-boot ran |

**Headline test = #6 + #5** together: prove that the audit log only sees real creates (not retry duplicates) AND that bulk doesn't shortcut the audit pattern.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| GUC bleed across requests in connection pool | Use `SET LOCAL` inside an explicit transaction; smoke test 2 verifies missing tenant fails before query |
| RLS bypass via raw SQL (e.g. in migrations) | Migration runs as a user with `BYPASSRLS`; runtime user does not. Documented but not enforced yet (later: separate `app_runtime` role). |
| `CHECK (actor ~ urn_regex)` and Pydantic regex drift | Single source of truth in `urn.py`; both layers import the same regex string |
| Generated column `actor_type` brittle to URN grammar change | URN grammar is now part of the data contract; changing it = major version bump |
| Seed YAML changes silently overwrite policy | Seed insert uses sha-based idempotency key + `INSERT ON CONFLICT DO NOTHING` — a real policy update goes through PATCH + new version, not seed |
| Multi-table RLS = N RLS policies to maintain | All three tables use the same predicate. Worth extracting to a function later. |

## Acceptance

- 16 containers running (was 15; +artefact-service)
- 28 smoke tests green (was 18; +10 in D0.5)
- All RLS policies active; smoke 2 proves enforcement
- Seed policy artefact present: `GET /artefacts?type=approval_policy` returns ≥1 row
- Audit log captures policy_version on every transition
- Idempotency: a workflow can replay create-or-update activities without polluting `artefact_history`

## Out of scope (returns to D1)

- agent-workers service
- LangGraph / Critic / RAG ingestion / cost-attribution wiring
- `requirement` and `test_case` artefact types (the data they hold is D0.5-shaped, but creation happens in D1)
- Per-tenant policy overrides (registry shape is here; population is later)
- Quorum approvals, escalation timers, human-review surfaces
- Cryptographic signing of attestation
