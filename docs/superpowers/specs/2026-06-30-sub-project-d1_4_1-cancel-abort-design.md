# Sub-project D1.4.1 — Cancel + abort

**Status:** approved-design (2026-06-30)
**Predecessor:** D1.4 — Async run queue + per-tenant quotas (shipped 2026-06-29)
**Scope:** end-to-end (runner + BFF + Monitor UI) so the feature is demoable in the browser.

## 1. Summary

Add a fourth terminal status — `canceled` — to the runner's run-state machine, with an idempotent `POST /runs/{id}/cancel` endpoint and an "Active runs" panel in the web Monitor view. Canceling a **queued** run removes it from the queue and releases its concurrent quota slot. Canceling a **running** run signals the worker via a flag in Valkey, issues `podman kill` against the sandbox container, and routes the worker's terminal transition to `canceled`. Daily quota is **not** refunded (it is intent-based — the submit consumed the slot).

## 2. Motivation

D1.4 made runs asynchronous (submit → queue → process → complete) but offered no way to take a run back. A user who submits a long sandbox by mistake currently waits up to `sandbox_timeout_seconds + 60` (≈10 minutes default) before the slot frees. With cancel, the user reclaims their concurrent slot in seconds and can resubmit immediately.

The feature is also a prerequisite for any future safety-critical or admin-driven kill flows (rate-limit response, tenant suspension, runaway-cost guard).

## 3. State machine

```
                 submit                claim+start_or_abort           container.wait()
              ┌─────────┐            ┌──────────────────┐           ┌──────────────────┐
              │         │            │                  │           │                  │
              ▼         │            ▼                  │           ▼                  │
     ┌────────────┐    │   ┌────────────┐              │   ┌────────────┐              │
─────┤  (none)    ├────┴──▶│   queued   ├──────────────┴──▶│   running  ├──────────────┴──▶ completed | failed
     └────────────┘        └──────┬─────┘                  └──────┬─────┘
                                  │                                │
                                  │ cancel_queued                  │ cancel_running + worker observes flag
                                  ▼                                ▼
                            ┌────────────────────────────────────────────┐
                            │                  canceled                  │
                            └────────────────────────────────────────────┘
```

Terminal states: `completed`, `failed`, `canceled`. All terminal transitions are idempotent against further `cancel`/`mark_*` calls (return 409 / no-op).

## 4. Quota semantics

| Event | `tenant:{id}:running` | `tenant:{id}:daily:DATE` |
|---|---|---|
| submit | SADD (slot reserved) | INCR |
| mark_completed | SREM | unchanged |
| mark_failed | SREM | unchanged |
| **cancel_queued** | **SREM** | **unchanged** |
| **cancel_running → mark_canceled** | **SREM (in worker)** | **unchanged** |
| `start_or_abort` returns ABORT | SREM (already happened in cancel_queued) | unchanged |

Concurrent slot is **reserved at submit, released at any terminal**. Daily counter never decrements — it represents intent and resets at UTC midnight (existing 48h TTL).

## 5. Concurrency & atomicity

Five race windows, each with a Lua-atomic mitigation.

### 5.1 cancel-arrives-between-claim-and-start_or_abort

`claim_next` (`BRPOPLPUSH queue → processing`) has run-id removed from queue but worker hasn't transitioned status to `running` yet.

**Mitigation:** `cancel_queued` Lua does `LREM K_QUEUE 0 run_id` **and** `LREM K_PROCESSING 0 run_id` (idempotent if absent from either). Worker's first action after `claim_next` is `start_or_abort` Lua, which reads `status`: if `canceled`, return `ABORT` (worker skips to next item, no sandbox spawn, no further quota change).

### 5.2 cancel-arrives-between-HSET-container_name-and-podman-run

`container_name` is recorded but the container may not yet exist on the host.

**Mitigation:** worker uses a **deterministic** container name `sandbox-{run_id}` chosen up front and `HSET` *before* the `podman run` call. Before `podman run`, worker calls `podman rm -f sandbox-{run_id}` (idempotent — handles restart-after-crash too). `cancel_running` reads `container_name`; if the kill returns "no such container", that's success (the run is either still pre-spawn or already gone), `cancel_requested=1` remains set, and the worker observes the flag on its next poll.

### 5.3 cancel-arrives-while-sandbox-is-running

Standard happy path. `cancel_running` Lua atomically sets `cancel_requested=1`, `canceled_by`, `canceled_at` (only if `status==running`; otherwise returns 409). Cancel handler then calls `podman kill sandbox-{run_id}`. Worker's poll loop sees container exit + `cancel_requested=1` and calls `mark_canceled`.

### 5.4 sandbox-completes-legitimately-just-as-cancel-arrives

The dangerous race: cancel API's Lua sets `cancel_requested=1` while worker is already in `mark_completed` (sandbox returned exit 0 a microsecond earlier).

**Mitigation:** `mark_completed`/`mark_failed` are converted to Lua. Each script reads `cancel_requested` first; if set, the terminal status it writes is `canceled` regardless of how the container exited. This makes the worker's "what did the sandbox return" question subordinate to "did anyone cancel" — the answer is always consistent with what the cancel API told the caller. Plus this Lua also enforces `status==running` so that a second `mark_*` call no-ops (idempotency on restart).

### 5.5 runner-restart-mid-cancel

Worker dies after `cancel_requested=1` was set but before `mark_canceled` ran. On restart, `recover_orphans` finds the run in `K_PROCESSING`.

**Mitigation:** `recover_orphans` is updated to read `cancel_requested` and route to `mark_canceled` instead of `mark_failed`. Also kills any straggler `sandbox-{run_id}` container with `podman rm -f` before completing recovery.

### 5.6 hung-container (kill-failed-for-real)

`podman kill` returns success but the container never actually exits, or kill itself fails with a non-404 error.

**Mitigation:** worker's poll loop has an upper bound = `request.sandbox_timeout_seconds + 60s grace`. If exceeded while `cancel_requested=1`, the worker force-calls `mark_canceled` regardless of container state, logs `runner.cancel.hung_container` at ERROR, and proceeds. The orphan container will be picked up by the next worker restart's `recover_orphans` cleanup. (This matches the pre-existing timeout semantics in D1.4; it's just made cancel-aware.)

## 6. API contract

### 6.1 Runner

#### `POST /runs/{run_id}/cancel`

Request body:
```json
{ "actor_urn": "urn:qa-aqa:user:alice", "tenant_id": "default" }
```

Responses:

| Status | Body | When |
|---|---|---|
| 200 | `{ "run_id", "status": "canceled", "previous_status": "queued"\|"running" }` | success |
| 404 | `{ "detail": "run not found" }` | `run:{id}` HASH does not exist |
| 403 | `{ "detail": "cross-tenant cancel forbidden" }` | body `tenant_id` ≠ stored `tenant_id` |
| 409 | `{ "detail": "run already terminal", "status": "completed"\|"failed"\|"canceled" }` | another cancel/terminal beat us |
| 502 | `{ "detail": "kill failed", "error": "..." }` | `podman kill` returned a non-404 error and `cancel_requested` was set; orphan recovery will reconcile |

#### `GET /runs?active=true&tenant_id={id}`

Returns queued + running runs for the tenant, sorted by `submitted_at` descending.

Response:
```json
{ "runs": [
    { "run_id": "abc123", "status": "running",
      "submitted_at": "...", "started_at": "...",
      "workflow_id": "default:execute-tests:wf-xyz",
      "actor_urn": "urn:qa-aqa:user:alice" },
    ...
] }
```

`tenant_id` query param is **required**. The endpoint never crosses tenants (returns empty if the tenant has no active runs, never 404).

### 6.2 BFF

#### `POST /api/runs/{run_id}/cancel`

No body required. BFF injects `actor_urn` and `tenant_id` from the JWT. Forwards verbatim status code from runner.

#### `GET /api/runs?active=true`

No query params required. BFF injects `tenant_id` from the JWT. Returns the runner's response verbatim.

## 7. Data model changes

`run:{run_id}` HASH gains four optional fields. None require migration — they materialize when first written.

| Field | Set by | Set when |
|---|---|---|
| `container_name` | worker (`main.py`) | before `podman run` — deterministic `sandbox-{run_id}` |
| `cancel_requested` | `cancel_running` Lua | atomically with running→cancel_requested |
| `canceled_by` | `cancel_queued` or `cancel_running` Lua | atomically with status transition |
| `canceled_at` | `cancel_queued` or `cancel_running` Lua | atomically with status transition |

Existing HASH TTL (24h) covers the new fields. No new keys in Valkey.

## 8. Components

### 8.1 `services/runner/src/queue.py`

New methods:

- `cancel_queued(run_id, actor_urn, tenant_id) -> dict` — Lua: tenant-check, status==queued check, LREM queue, LREM processing, SREM tenant:running, HSET status=canceled + canceled_by + canceled_at.
- `cancel_running(run_id, actor_urn, tenant_id) -> dict` — Lua: tenant-check, status==running check, HSET cancel_requested=1 + canceled_by + canceled_at. Does **not** call `podman kill`; caller does (separation of concerns — kill is I/O, Lua is state).
- `mark_canceled(run_id, tenant_id) -> None` — Lua: HSET status=canceled + completed_at, LREM processing, SREM tenant:running. Idempotent (no-op if already terminal). **Called directly only from** `cancel_queued` flow, `recover_orphans` (when `cancel_requested=1`), and the hung-container timeout in §5.6. The normal running→canceled path goes through `mark_completed`/`mark_failed`, whose Lua reroutes the terminal status to `canceled` when `cancel_requested=1` is observed (§5.4).
- `start_or_abort(run_id) -> "running" | "abort"` — Lua: if status==canceled, return "abort"; else HSET status=running + started_at, return "running".
- `list_active(tenant_id) -> list[dict]` — non-Lua: scan members of `tenant:{tenant_id}:running`, plus iterate `K_QUEUE` filtering by tenant via `HGET run:{id} tenant_id`. Bounded by quota maxima (≤ ~150 runs in the worst case).

Modified methods:

- `mark_completed` / `mark_failed` — converted to Lua. Each script reads `cancel_requested` first; if set, status is written as `canceled` instead of `completed`/`failed`.
- `recover_orphans` — reads `cancel_requested` per orphan; routes to `mark_canceled` (with `podman rm -f` of stale container) if set, otherwise existing `mark_failed` path.

### 8.2 `services/runner/src/main.py`

- New route `POST /runs/{run_id}/cancel` — body validation, tenant check (delegated to Lua), dispatch to `cancel_queued` or `cancel_running` based on current status. On `cancel_running` success: HTTPX POST to `/containers/sandbox-{run_id}/kill` on the podman socket; treat **204 (killed), 304 (already stopped), and 404 (gone)** as success; anything else → 502 (but leave `cancel_requested=1` set so worker still routes to `canceled` if the container later exits). On inspect 404 mid-poll: worker treats as exited with unknown code (log warning, proceed via terminal Lua).
- New route `GET /runs` with `active=true&tenant_id={id}` — calls `queue.list_active(tenant_id)`.
- Worker loop `_worker_loop()` changes:
  - After `claim_next` returns `run_id`, call `start_or_abort`. If `"abort"`, `LREM K_PROCESSING 0 run_id` and continue.
  - Choose deterministic `container_name = f"sandbox-{run_id}"`; `HSET run:{id} container_name`; `podman rm -f` (ignore 404); `podman run --name {container_name} ...`.
  - Replace blocking wait with 2-second poll loop:
    - On each tick: read container state (`GET /containers/{name}/json`) **and** `HGET run:{id} cancel_requested`.
    - If `cancel_requested==1` and container still running: `podman kill`; continue polling.
    - If container exited: capture exit code + collect logs/artifacts, then call `mark_completed` (exit 0) or `mark_failed` (non-zero). The Lua sees `cancel_requested` and writes `canceled` if set — so the worker doesn't need a separate `mark_canceled` branch here. (5.4 mitigation in action.)
    - If poll loop exceeds `sandbox_timeout_seconds + 60`: log `runner.cancel.hung_container`, call `mark_canceled`, give up.

### 8.3 `services/bff/src/main.py`

- `POST /api/runs/{run_id}/cancel` — requires `current_user` dep, forwards with `{actor_urn: user.urn, tenant_id: user.tenant_id}`.
- `GET /api/runs?active=true` — requires `current_user` dep, forwards `tenant_id` from JWT.
- No new role checks. Anyone authenticated within the tenant may cancel (as agreed in brainstorming).

### 8.4 `services/agent-workers/src/executor_agent.py`

In the `run_in_sandbox` poll loop:
- Terminal-status set becomes `{"completed", "failed", "canceled"}`.
- Synthesized return for `canceled`:
  ```python
  return {"mode": "playwright_sandbox", "status": "canceled",
          "error_message": f"canceled by {state.get('canceled_by','unknown')}",
          "run_id": run_id, "canceled_at": state.get("canceled_at"), ...}
  ```

**Workflow-boundary note:** canceling a *run* cancels the *sandbox activity*. The Temporal workflow itself continues; the activity returns a `canceled` result and the workflow decides what to do (the existing `execute-tests` workflow already tolerates per-test failures, so a canceled run looks like a failed result to it). Workflow cancellation is a future feature.

### 8.5 `clients/web/src/`

- `lib/queries.ts`:
  - New type `RunSummary = { run_id, status, submitted_at, started_at?, workflow_id?, actor_urn }`.
  - `useActiveRuns()` — `useQuery({ queryKey: ["active-runs"], queryFn: () => api<{runs: RunSummary[]}>("/api/runs?active=true"), refetchInterval: 3_000 })`.
  - `useCancelRun()` — `useMutation` over `POST /api/runs/{id}/cancel`. On success, invalidates `["active-runs"]`, `["me-quota"]`, `["workflow"]`, and `["wf-status"]`.
- `pages/MonitorPage.tsx` (or equivalent in the existing Monitor view) gains an "Active runs" card above the screenshots grid:
  - Empty state: "No active runs."
  - Each row: `run_id` (short), status badge (queued / running), submitted-at relative time, workflow link if present, Cancel button (disabled when status is terminal — though terminal runs shouldn't appear in this list).
  - Cancel button calls `window.confirm("Cancel run {short_id}? This will terminate the sandbox immediately.")` before firing the mutation.
  - Status badge styling: `canceled` renders gray (Tailwind `bg-slate-700 text-slate-300`).

### 8.6 `infra/infra.sh`

- No new services. The runner already mounts `/run/podman/podman.sock` for sandbox spawning; the same socket is reused for `kill` and `rm`.
- New smoke tests: see §9.
- No env changes.

## 9. Smoke tests

Added to `infra.sh`:

1. **`runner-cancel-queued`** — Submit run while quota_concurrent is saturated by long-running fillers so the new run sits in `runs:queue`. POST cancel. Assert: status=`canceled`, run absent from `runs:queue`, run absent from `tenant:running`, daily counter unchanged from pre-submit + 1.

2. **`runner-cancel-running`** — Submit a long-sleep sandbox payload (60s). Poll until status=`running` and `container_name` is set (≤10s). POST cancel. Assert within 5s: status=`canceled`, `cancel_requested=1`, container gone (`podman inspect sandbox-{id}` → 404), tenant:running does not contain it.

3. **`runner-cancel-cross-tenant`** — Submit as tenant A. POST cancel with body `tenant_id=tenant-B` → 403. Run remains queued/running. (Direct runner test; BFF JWT path is tested separately.)

4. **`runner-cancel-already-terminal`** — Submit a fast-completing sandbox (echo + exit). Poll until status=`completed`. POST cancel → 409.

5. **`runner-list-active`** — Submit two runs as tenant A. GET `/runs?active=true&tenant_id=tenant-A` → array length 2 with both run_ids. GET with `tenant_id=tenant-B` → array length 0.

6. **`bff-cancel-passthrough`** — Login as user in tenant A via BFF, submit a long-sleep run, then `POST /api/runs/{id}/cancel` through APISIX with the JWT → 200. Confirms JWT-injected `actor_urn` + `tenant_id` reach the runner correctly.

All six are appended to the existing 48-test suite. No prior tests should regress.

## 10. Risks & mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Worker hangs forever on a container that won't die after kill | low | Poll-loop timeout in §5.6 force-routes to `mark_canceled`; orphan container cleaned up on next restart. |
| Audit data lost if Valkey is wiped | medium | Acceptable for now — same volatility as `runs:queue` itself. Durable audit shipped in a later sub-project. |
| Sub-millisecond race not covered by Lua atomicity | very low | All transitions touching `status` are inside Lua scripts; Valkey is single-threaded so Lua executes atomically with respect to all other commands. |
| BFF JWT bug passes wrong `tenant_id` | low | Runner's Lua tenant-check is the authoritative gate; cross-tenant bug surfaces as 403, not as silent leakage. |
| Web cache shows stale "running" after cancel | low | `useCancelRun` invalidates four query keys (`active-runs`, `me-quota`, `workflow`, `wf-status`) on success; 3s background poll catches anything missed. |
| Container-name collision on restart | low | `podman rm -f sandbox-{run_id}` is unconditional before `podman run`. |

## 11. Out of scope

- **Bulk cancel** (e.g., "cancel everything for this workflow"). Add later if a user request materializes.
- **Cancel reason field** (free-text). Audit currently captures `canceled_by` + `canceled_at` only.
- **Auto-cancel on tenant suspension** — needs a tenant lifecycle subsystem; not built.
- **Daily-quota refund on cancel** — explicitly rejected: daily is intent-based.
- **Prometheus/Grafana observability** (queue depth, cancel latency, 429 rate). Deferred to D1.4.2 (observability sub-project).
- **Role-based cancel restrictions** (viewer can't cancel). Current rule: anyone in the tenant. Add roles later if needed.
- **Workflow-level cancel** (kill the Temporal workflow, not just the sandbox). Out of D1.4.1.
- **Cancel via admin API key** (no JWT). Future admin endpoint.

## 12. Implementation order

1. `queue.py`: write the four new Lua scripts (cancel_queued, cancel_running, start_or_abort, mark_canceled) plus convert mark_completed/mark_failed to Lua. Unit-callable from a Python REPL against a dev Valkey to confirm atomicity before wiring up.
2. `queue.py`: update `recover_orphans` for cancel-aware recovery.
3. `queue.py`: implement `list_active`.
4. `main.py`: rewrite worker loop to use deterministic container_name + 2s poll + `start_or_abort`.
5. `main.py`: add `POST /runs/{id}/cancel` and `GET /runs?active=true` routes.
6. `bff/main.py`: add the two passthrough routes.
7. `agent-workers/executor_agent.py`: add `"canceled"` to terminal-status set.
8. `infra.sh`: add the six smoke tests; run the full suite.
9. `web/queries.ts` + Monitor view: `useActiveRuns`, `useCancelRun`, "Active runs" card with Cancel button.
10. Manual browser smoke: submit a long sandbox, click Cancel, confirm UI updates, badge goes gray, quota slot returns.

Estimated diff: ~11 files (4 runner, 1 BFF, 1 agent-workers, 3 web, 1 infra, 1 spec — already this file). ~600 LOC added/changed.
