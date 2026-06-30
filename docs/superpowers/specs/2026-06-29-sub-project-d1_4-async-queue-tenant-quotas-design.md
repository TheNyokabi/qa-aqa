# Sub-project D1.4 — async runs queue + per-tenant quotas

**Date:** 2026-06-29
**Scope:** Two changes shipped together: (1) `runner-service` `/runs` becomes asynchronous — submit returns 202 + `run_id`, a background consumer drains the queue; (2) Valkey-backed concurrent + daily sandbox quotas per tenant. BFF exposes quota status in `/api/me`; UI shows current vs. limit.
**Depends on:** D3c committed at `abe2e20`. 45 prior smoke tests must pass.

## Decomposition

| Tier | Ships | Smoke must pass before next | Effort |
|---|---|---|---|
| **D1.4a — quotas** | Valkey-backed concurrent + daily counters in runner-service; quotas exposed by BFF + UI | Submitting beyond quota → HTTP 429 with clear body | Small-Medium |
| **D1.4b — async queue** | `POST /runs` returns 202 + run_id; background consumer drains Valkey LIST; `GET /runs/{run_id}` returns state | Existing executor flow unchanged externally; new endpoint shape verified | Medium |

Each tier commits separately and adds smoke tests cumulatively.

## Architecture

### Valkey keys

| Key | Type | Purpose | TTL |
|---|---|---|---|
| `runs:queue` | LIST | Pending run job specs (JSON) | none |
| `runs:processing` | LIST | Jobs in-flight (for crash recovery) | none |
| `run:{id}` | HASH | Per-run state: status, started_at, completed_at, sandbox_id, result_json, error | 24h |
| `tenant:{id}:running` | SET | run_ids currently running for tenant | cleared on completion |
| `tenant:{id}:daily:YYYY-MM-DD` | STRING (INCR'd) | Count of runs submitted today by tenant | 48h |

### Quota gates (D1.4a)

- **Concurrent**: `SCARD tenant:{id}:running` must be `< MAX_CONCURRENT` (default 3) at submit time.
- **Daily**: `GET tenant:{id}:daily:<today>` must be `< MAX_DAILY` (default 100) at submit time.
- Both limits configurable via env: `QUOTA_CONCURRENT_DEFAULT`, `QUOTA_DAILY_DEFAULT`. Future D1.4.1 supports per-tenant overrides; v1 uses one global value for all tenants.
- Atomic check-and-increment via a Lua script: prevents racing past the cap when many requests land at once.
- Submit beyond quota → **HTTP 429** with body `{detail, kind: "quota_concurrent"|"quota_daily", current, max}`.

### Async submit lifecycle (D1.4b)

```
HTTP POST /runs
   │
   ▼
[runner-service]  ── quota check (D1.4a) ──┐
   │                                       │  ok
   │  generate run_id                      ▼
   │  HSET run:{id} status=queued, request=...
   │  SADD tenant:{id}:running {run_id}
   │  INCR tenant:{id}:daily:<today>
   │  LPUSH runs:queue {run_id}
   │
   └──> 202 { run_id, status: "queued" }

[runner-service-worker (asyncio task)]
   │
   ▼  loop:
   BRPOPLPUSH runs:queue runs:processing  (block 5s)
   HSET run:{id} status=running, started_at=...
   spawn sandbox container (existing logic)
   wait for exit + capture artefacts
   HSET run:{id} status=completed|failed, result=..., completed_at=...
   SREM tenant:{id}:running {run_id}
   LREM runs:processing 0 {run_id}

HTTP GET /runs/{run_id}
   │
   ▼
HGETALL run:{id}
   → { status, started_at, completed_at, result?, error? }
```

### Crash recovery

If runner-service dies mid-run, the entry stays in `runs:processing` LIST and `run:{id}` stays at `status=running`. On startup, runner-service reads `runs:processing` and either:
- Moves entries back to `runs:queue` (re-enqueue, for runs we don't know completed)
- Marks them as `failed` if we can't tell

For v1: simple — on startup, mark any `status=running` runs in `runs:processing` as `failed` with `error="runner restarted"`, remove from processing list, remove from tenant running sets.

### agent-workers contract change (transparent to callers)

`executor_agent.run_in_sandbox` currently does:
```python
r = await client.post("/runs", json=body)
return r.json()
```

Becomes:
```python
r = await client.post("/runs", json=body)
run_id = r.json()["run_id"]
# Poll until terminal
while True:
    s = await client.get(f"/runs/{run_id}")
    state = s.json()
    if state["status"] in ("completed", "failed"):
        return state.get("result", {})
    await asyncio.sleep(2.0)
```

The Temporal heartbeat ticker around the activity keeps things alive through the polling.

## BFF changes (D1.4a-side)

| Method | Path | Behaviour | Role |
|---|---|---|---|
| GET | `/api/me` | Now includes `quota: {concurrent: {current, max}, daily: {current, max, resets_at}}` for the caller's tenant | viewer+ |

BFF queries runner-service over the network: `GET http://runner-service:8004/quota/{tenant_id}` (new endpoint). runner-service returns the live counters.

## Web app changes (D1.4a-side)

- `Layout.tsx` shows a tiny quota badge: `Runs today 5/100 · running 1/3`
- `useAuth()` hook hydrates the badge from `/api/me` (already polled)

## Updates to `infra.sh`

| Section | Change |
|---|---|
| 1 Variables | New env defaults: `QUOTA_CONCURRENT_DEFAULT=3`, `QUOTA_DAILY_DEFAULT=100` |
| 5 compose | runner-service: add `VALKEY_URL=redis://valkey:6379`, `QUOTA_CONCURRENT_DEFAULT`, `QUOTA_DAILY_DEFAULT` envs |
| `services/runner/pyproject.toml` + Containerfile | Add `valkey-py` (a.k.a. `valkey==6.0.0` — the official Valkey Python client, BSD-3) |
| 8 smoke | 4 new tests covering quota + async |

## Smoke tests for D1.4

| # | Test | Verifies |
|---|---|---|
| 1 | `POST /runs` returns 202 with `{run_id, status: "queued"}` | async submit |
| 2 | `GET /runs/{id}` initial → `queued` or `running`; eventually `completed` | state lifecycle |
| 3 | Submit `QUOTA_CONCURRENT_DEFAULT + 1` runs back-to-back; the last one returns 429 with `kind: "quota_concurrent"` | concurrent cap |
| 4 | Force daily counter to limit via Valkey CLI, submit one more → 429 with `kind: "quota_daily"` | daily cap |
| 5 | BFF `/api/me` returns `quota: {...}` with current values | BFF exposure |

The existing sandbox-cleanup + sandbox-isolation + proxy-d1-3-suite + executor-d1-1-suite tests continue to pass — the async refactor is internal to runner-service from agent-workers' perspective.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Async refactor breaks existing executor smoke tests | Agent-workers polls until terminal; Temporal heartbeat keeps activity alive. The contract observed by callers is identical (eventually returns the result dict). |
| Crash mid-run leaves stuck state | Startup recovery marks any `status=running` runs as `failed`; tenant counters cleared. v1 doesn't try to resume in-flight sandboxes. |
| Valkey down → all submits fail | Acceptable for v1; Valkey is already in the critical path for other services. |
| Daily quota TTL boundary at midnight UTC | TTL=48h gives a generous window; quota status surfaces `resets_at` so the UI can show "in 8h 23m". |
| BFF /api/me polling load on runner-service | The quota endpoint reads Valkey only; cheap. BFF could cache 5s if needed (v1 doesn't). |
| Concurrent submit race (two requests both see < max) | Atomic Lua script does check-and-increment in one Valkey round-trip. |

## Acceptance per tier

### D1.4a (quotas)
- 22 containers (unchanged)
- 48 smoke tests green (was 45; +3 D1.4a tests — quota concurrent, quota daily, quota in /api/me)
- UI shows quota badge in top nav for logged-in user
- A 4th concurrent submit returns 429 with `kind: "quota_concurrent"`

### D1.4b (async)
- Same containers
- 50 smoke tests green (was 48; +2 D1.4b tests — submit returns 202, lifecycle reaches `completed`)
- Existing executor smoke tests continue to pass
- runner-service worker loop visible in logs

## Out of scope (D1.4.1+)

- Multiple runner-service replicas consuming the same queue (compose change only, but warrants a smoke test)
- Per-tenant quota overrides stored as an artefact (currently global env)
- Cancellation via `DELETE /runs/{run_id}`
- Quota reset endpoint (`POST /admin/quotas/{tenant}/reset`)
- Workflow-level quota (vs per-/runs-call)
- Backpressure to model-gateway / Ollama
- Job priority within the queue
- Dead-letter queue for permanent failures
