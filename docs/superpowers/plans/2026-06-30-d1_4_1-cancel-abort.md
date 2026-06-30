# D1.4.1 — Cancel + Abort Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `POST /runs/{id}/cancel`, `GET /runs?active=true`, and a Monitor-view "Active runs" panel with a Cancel button. Canceling a queued run releases its concurrent quota slot; canceling a running run kills the sandbox container and routes the worker's terminal transition to `canceled`. Daily quota is not refunded.

**Architecture:** State machine gains a fourth terminal status (`canceled`). All race-sensitive transitions move to Lua scripts (Valkey is single-threaded — Lua executes atomically against other commands). The runner's worker loop becomes cancel-aware: deterministic container name (`sandbox-{run_id[:12]}`), a kill-watcher coroutine running in parallel with the sandbox task observes `cancel_requested` every 2s, and `mark_completed`/`mark_failed` Lua reroutes the terminal status to `canceled` when the flag is set. UI gets a small "Active runs" card backed by a new tenant-scoped list endpoint.

**Tech Stack:** Python 3.12 (runner, agent-workers, BFF) · FastAPI · valkey-py (async) · Lua (via Valkey eval) · React 18 + Vite + TanStack Query · Tailwind · APISIX gateway · podman REST API (Compat) over UNIX socket · bash smoke tests in `infra/infra.sh`.

**Reference spec:** `docs/superpowers/specs/2026-06-30-sub-project-d1_4_1-cancel-abort-design.md` (commit `02d9c76`).

---

## Conventions used in this plan

- Every task ends with a smoke run + commit. No "wire it up later" steps.
- **TDD adapted to this project's smoke-only test layer:** for each new endpoint or behavior, the failing smoke test is added to `infra/infra.sh` *first*, the smoke runs and the new test fails, the implementation lands, the smoke re-runs and the new test passes.
- All `git commit` messages end with the trailer `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`.
- Working directory: `/Users/jamesnyokabi/QA/AQA`.
- Smoke runs: `./infra/infra.sh smoke` (full suite — ~5min). After this work lands, baseline is 47/48 pass (the pre-existing `agent-workers-d1-suite` flake is documented in commit `92da360`); the 6 new D1.4.1 tests target 53/54 green.
- For interactive Lua testing during Task 4 and Task 5: `podman exec -it runner-service python -c "...inline REPL..."` against the live Valkey.

---

## File structure

**Modify:**
- `services/runner/src/queue.py` — add Lua scripts + Python wrappers for cancel paths, list_active, cancel-aware recover_orphans; convert mark_completed/mark_failed to Lua with cancel_requested reroute.
- `services/runner/src/main.py` — add cancel route + list-active route; rewrite worker loop with deterministic container name, kill-watcher coroutine, hung-container timeout.
- `services/bff/src/main.py` — add two passthrough routes that inject `tenant_id` and `actor_urn` from the JWT.
- `services/agent-workers/src/executor_agent.py` — add `"canceled"` to terminal-status set in the run-polling loop.
- `clients/web/src/lib/queries.ts` — add `RunSummary` type, `useActiveRuns`, `useCancelRun`.
- `clients/web/src/pages/MonitorPage.tsx` — add Active Runs card with Cancel button. *(Engineer: confirm exact file name with `ls clients/web/src/pages/` — D3c shipped this view but the filename may be `Monitor.tsx` or similar.)*
- `infra/infra.sh` — append 6 new smoke tests + register them in the smoke list.

**Create:** none — no new files. Spec already exists.

---

## Task 1: cancel_queued — end-to-end (Lua + route + smoke)

The simplest cancel path: run is still in `runs:queue`, no sandbox to kill.

**Files:**
- Modify: `services/runner/src/queue.py` (add `_LUA_CANCEL_QUEUED`, `cancel_queued`)
- Modify: `services/runner/src/main.py` (add `POST /runs/{run_id}/cancel` route — queued branch only for now)
- Modify: `infra/infra.sh` (add `smoke_runner_cancel_queued` function + register in smoke list)

- [ ] **Step 1: Write failing smoke test**

Append to `infra/infra.sh` near the other `smoke_*` runner tests:

```bash
smoke_runner_cancel_queued() {
  # Saturate quota with long-running fillers so the test run sits in the queue.
  local TENANT="smk-cancel-q-$RANDOM"
  local filler_ids=()
  local i
  for i in 1 2 3; do
    local rid
    rid=$(curl -fsS -X POST http://localhost:8004/runs -H 'Content-Type: application/json' \
      -d "{\"tenant_id\":\"$TENANT\",\"workflow_id\":\"wf\",\"test_case_id\":\"tc-$i\",
           \"test_case\":{\"payload\":{\"title\":\"sleep\",\"steps\":[]}},
           \"timeout_seconds\":120}" | jq -r .run_id) || return 1
    filler_ids+=("$rid")
  done
  # 4th submit must queue (quota_concurrent=3 reached).
  local target
  target=$(curl -fsS -X POST http://localhost:8004/runs -H 'Content-Type: application/json' \
    -d "{\"tenant_id\":\"$TENANT\",\"workflow_id\":\"wf\",\"test_case_id\":\"target\",
         \"test_case\":{\"payload\":{\"title\":\"sleep\",\"steps\":[]}},
         \"timeout_seconds\":120}" | jq -r .run_id) || return 1
  # Cancel the queued one.
  local code
  code=$(curl -s -o /tmp/cancel_q.out -w '%{http_code}' \
    -X POST "http://localhost:8004/runs/$target/cancel" \
    -H 'Content-Type: application/json' \
    -d "{\"actor_urn\":\"urn:qa-aqa:user:smoke\",\"tenant_id\":\"$TENANT\"}")
  [[ "$code" == "200" ]] || { echo "expected 200, got $code body=$(cat /tmp/cancel_q.out)"; return 1; }
  # Status must be canceled.
  local status
  status=$(curl -fsS "http://localhost:8004/runs/$target" | jq -r .status)
  [[ "$status" == "canceled" ]] || { echo "expected status=canceled, got $status"; return 1; }
  # tenant:running must NOT contain the canceled run.
  local in_set
  in_set=$(podman exec valkey valkey-cli SISMEMBER "tenant:$TENANT:running" "$target")
  [[ "$in_set" == "0" ]] || { echo "tenant:running still contains $target"; return 1; }
}
```

Register in the smoke list (find the array near the end of `infra.sh` that contains `runner-d1-4-suite`, add `runner-cancel-queued` after `bff-quota-in-me`).

- [ ] **Step 2: Run smoke to verify the new test fails**

```bash
./infra/infra.sh smoke 2>&1 | grep "runner-cancel-queued"
# Expected: ✘ smoke: runner-cancel-queued FAILED  → smoke_runner_cancel_queued
# (Cancel route returns 404 / not implemented; curl in the test fails.)
```

- [ ] **Step 3: Add `_LUA_CANCEL_QUEUED` script + `cancel_queued` method to queue.py**

Insert after the existing `_LUA_RESERVE` definition (line ~72):

```python
# Atomic cancel of a queued run. Tenant-checked. Returns:
#   {0, previous_status} on success (run is now canceled)
#   {1, stored_tenant}   on cross-tenant attempt (caller -> 403)
#   {2, current_status}  if status is not "queued" (caller -> 409)
#   {3, ""}              if run hash does not exist (caller -> 404)
_LUA_CANCEL_QUEUED = """
local k_run = KEYS[1]
local k_queue = KEYS[2]
local k_processing = KEYS[3]
local k_running = KEYS[4]
local run_id = ARGV[1]
local caller_tenant = ARGV[2]
local actor_urn = ARGV[3]
local now = ARGV[4]

if redis.call('EXISTS', k_run) == 0 then
    return {3, ""}
end
local stored_tenant = redis.call('HGET', k_run, 'tenant_id') or ""
if stored_tenant ~= caller_tenant then
    return {1, stored_tenant}
end
local status = redis.call('HGET', k_run, 'status') or ""
if status ~= 'queued' then
    return {2, status}
end
redis.call('LREM', k_queue, 0, run_id)
redis.call('LREM', k_processing, 0, run_id)
redis.call('SREM', k_running, run_id)
redis.call('HSET', k_run,
    'status', 'canceled',
    'canceled_by', actor_urn,
    'canceled_at', now,
    'completed_at', now)
return {0, status}
"""
```

In the `Queue` class, add the SHA load in `connect()`:

```python
async def connect(self) -> None:
    self._v = Valkey.from_url(self._url, decode_responses=True)
    self._reserve_sha = await self._v.script_load(_LUA_RESERVE)
    self._cancel_queued_sha = await self._v.script_load(_LUA_CANCEL_QUEUED)
```

Add Python wrapper + a typed result class:

```python
class CancelResult:
    def __init__(self, code: int, previous_status: str = "", stored_tenant: str = ""):
        self.code = code  # 0=ok 1=cross-tenant 2=wrong-status 3=not-found
        self.previous_status = previous_status
        self.stored_tenant = stored_tenant

async def cancel_queued(self, run_id: str, actor_urn: str, caller_tenant: str) -> CancelResult:
    assert self._v is not None and self._cancel_queued_sha is not None
    now = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    result = await self._v.evalsha(
        self._cancel_queued_sha,
        4,
        k_run(run_id),
        K_QUEUE,
        K_PROCESSING,
        k_running(caller_tenant),
        run_id, caller_tenant, actor_urn, now,
    )
    code = int(result[0])
    if code == 0:
        return CancelResult(0, previous_status=str(result[1]))
    if code == 1:
        return CancelResult(1, stored_tenant=str(result[1]))
    if code == 2:
        return CancelResult(2, previous_status=str(result[1]))
    return CancelResult(3)
```

- [ ] **Step 4: Add POST /runs/{run_id}/cancel route to main.py (queued path only)**

Insert after the `get_quota` endpoint (around line 117):

```python
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
```

- [ ] **Step 5: Rebuild runner image + run smoke**

```bash
./infra/infra.sh up   # rebuilds changed images, restarts runner-service
./infra/infra.sh smoke 2>&1 | grep -E "runner-cancel-queued|Summary"
# Expected: ✔ smoke: runner-cancel-queued
```

If it fails: read `/tmp/cancel_q.out` (the test wrote the response there) for the actual response body.

- [ ] **Step 6: Commit**

```bash
git add services/runner/src/queue.py services/runner/src/main.py infra/infra.sh
git commit -m "$(cat <<'EOF'
D1.4.1 Task 1: cancel queued runs

- Add _LUA_CANCEL_QUEUED script: tenant-check + status=queued check +
  LREM queue/processing + SREM tenant:running + HSET canceled/canceled_by/at
  in one atomic Lua eval.
- POST /runs/{id}/cancel handles queued-path only for now; running-path
  returns 501 until Task 5.
- New smoke: runner-cancel-queued (saturate quota, queue 4th, cancel,
  assert canceled + tenant:running cleared).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: cross-tenant + already-terminal coverage

Tighten the cancel endpoint against the two error paths the cancel_queued Lua already produces but that aren't exercised by Task 1's smoke.

**Files:**
- Modify: `infra/infra.sh` (add 2 smoke tests + register)

- [ ] **Step 1: Write 2 failing smoke tests**

Append:

```bash
smoke_runner_cancel_cross_tenant() {
  local TA="smk-A-$RANDOM"
  local TB="smk-B-$RANDOM"
  local rid
  rid=$(curl -fsS -X POST http://localhost:8004/runs -H 'Content-Type: application/json' \
    -d "{\"tenant_id\":\"$TA\",\"workflow_id\":\"wf\",\"test_case_id\":\"t\",
         \"test_case\":{\"payload\":{\"title\":\"sleep\",\"steps\":[]}},
         \"timeout_seconds\":60}" | jq -r .run_id) || return 1
  local code
  code=$(curl -s -o /tmp/cancel_xt.out -w '%{http_code}' \
    -X POST "http://localhost:8004/runs/$rid/cancel" \
    -H 'Content-Type: application/json' \
    -d "{\"actor_urn\":\"urn:qa-aqa:user:b\",\"tenant_id\":\"$TB\"}")
  [[ "$code" == "403" ]] || { echo "expected 403 cross-tenant, got $code body=$(cat /tmp/cancel_xt.out)"; return 1; }
  # Cleanup: cancel as the right tenant so the slot frees for other tests.
  curl -s -X POST "http://localhost:8004/runs/$rid/cancel" \
    -H 'Content-Type: application/json' \
    -d "{\"actor_urn\":\"urn:qa-aqa:user:a\",\"tenant_id\":\"$TA\"}" > /dev/null
}

smoke_runner_cancel_already_terminal() {
  # Submit a quick-completing run (sandbox executor returns fast on the trivial payload).
  local TENANT="smk-term-$RANDOM"
  local rid
  rid=$(curl -fsS -X POST http://localhost:8004/runs -H 'Content-Type: application/json' \
    -d "{\"tenant_id\":\"$TENANT\",\"workflow_id\":\"wf\",\"test_case_id\":\"t\",
         \"test_case\":{\"payload\":{\"title\":\"noop\",\"steps\":[]}},
         \"timeout_seconds\":30}" | jq -r .run_id) || return 1
  # Wait up to 60s for terminal.
  local status="" i
  for i in $(seq 1 60); do
    status=$(curl -fsS "http://localhost:8004/runs/$rid" | jq -r .status)
    [[ "$status" == "completed" || "$status" == "failed" ]] && break
    sleep 1
  done
  [[ "$status" == "completed" || "$status" == "failed" ]] || { echo "run did not terminate: $status"; return 1; }
  local code
  code=$(curl -s -o /tmp/cancel_term.out -w '%{http_code}' \
    -X POST "http://localhost:8004/runs/$rid/cancel" \
    -H 'Content-Type: application/json' \
    -d "{\"actor_urn\":\"urn:qa-aqa:user:smoke\",\"tenant_id\":\"$TENANT\"}")
  [[ "$code" == "409" ]] || { echo "expected 409, got $code body=$(cat /tmp/cancel_term.out)"; return 1; }
}
```

Register `runner-cancel-cross-tenant` and `runner-cancel-already-terminal` in the smoke list.

- [ ] **Step 2: Run smoke to verify both pass already**

```bash
./infra/infra.sh smoke 2>&1 | grep -E "runner-cancel-(cross-tenant|already-terminal)"
# Expected: both ✔
# (The Task 1 implementation already handles both cases — these tests just confirm coverage.)
```

If `runner-cancel-already-terminal` is flaky because the noop sandbox is slower-completing than 60s, increase the loop bound to 120s.

- [ ] **Step 3: Commit**

```bash
git add infra/infra.sh
git commit -m "$(cat <<'EOF'
D1.4.1 Task 2: cross-tenant + terminal-state cancel coverage

Adds two smoke tests confirming Task 1's Lua check-and-return paths
produce 403 (cross-tenant) and 409 (already-terminal) correctly.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: list_active endpoint

Tenant-scoped listing of queued + running runs. Required for the UI to show a Cancel button.

**Files:**
- Modify: `services/runner/src/queue.py` (add `list_active`)
- Modify: `services/runner/src/main.py` (add `GET /runs?active=true&tenant_id=...`)
- Modify: `infra/infra.sh` (add `smoke_runner_list_active`)

- [ ] **Step 1: Write failing smoke test**

```bash
smoke_runner_list_active() {
  local TA="smk-list-A-$RANDOM"
  local TB="smk-list-B-$RANDOM"
  # Submit 2 runs as tenant A.
  local id1 id2
  id1=$(curl -fsS -X POST http://localhost:8004/runs -H 'Content-Type: application/json' \
    -d "{\"tenant_id\":\"$TA\",\"workflow_id\":\"wf\",\"test_case_id\":\"t1\",
         \"test_case\":{\"payload\":{\"title\":\"sleep\",\"steps\":[]}},\"timeout_seconds\":60}" | jq -r .run_id) || return 1
  id2=$(curl -fsS -X POST http://localhost:8004/runs -H 'Content-Type: application/json' \
    -d "{\"tenant_id\":\"$TA\",\"workflow_id\":\"wf\",\"test_case_id\":\"t2\",
         \"test_case\":{\"payload\":{\"title\":\"sleep\",\"steps\":[]}},\"timeout_seconds\":60}" | jq -r .run_id) || return 1
  local count_a count_b
  count_a=$(curl -fsS "http://localhost:8004/runs?active=true&tenant_id=$TA" | jq '.runs | length')
  count_b=$(curl -fsS "http://localhost:8004/runs?active=true&tenant_id=$TB" | jq '.runs | length')
  [[ "$count_a" == "2" ]] || { echo "tenant A: expected 2 active, got $count_a"; return 1; }
  [[ "$count_b" == "0" ]] || { echo "tenant B: expected 0 active, got $count_b"; return 1; }
  # Cleanup.
  for rid in "$id1" "$id2"; do
    curl -s -X POST "http://localhost:8004/runs/$rid/cancel" -H 'Content-Type: application/json' \
      -d "{\"actor_urn\":\"urn:qa-aqa:user:smoke\",\"tenant_id\":\"$TA\"}" > /dev/null
  done
}
```

Register `runner-list-active`.

- [ ] **Step 2: Run smoke to verify failure**

```bash
./infra/infra.sh smoke 2>&1 | grep runner-list-active
# Expected: ✘  (route returns 404 / not implemented; curl exits non-zero on the .runs path)
```

- [ ] **Step 3: Add `list_active` to queue.py**

Append to `Queue` class:

```python
async def list_active(self, tenant_id: str) -> list[dict[str, Any]]:
    """Return queued + running runs for a tenant, sorted by submitted_at desc."""
    assert self._v is not None
    # Queued: scan K_QUEUE, filter by tenant_id from each hash.
    queued_ids: list[str] = await self._v.lrange(K_QUEUE, 0, -1)
    # Running: members of tenant:{id}:running set.
    running_ids: list[str] = list(await self._v.smembers(k_running(tenant_id)))
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rid in queued_ids + running_ids:
        if rid in seen:
            continue
        seen.add(rid)
        h = await self._v.hgetall(k_run(rid))
        if not h:
            continue
        if h.get("tenant_id") != tenant_id:
            continue
        status = h.get("status", "")
        if status not in ("queued", "running"):
            continue
        out.append({
            "run_id": rid,
            "status": status,
            "submitted_at": h.get("submitted_at"),
            "started_at": h.get("started_at"),
            "workflow_id": (json.loads(h["request"]).get("workflow_id") if h.get("request") else None),
            "actor_urn": h.get("actor_urn"),  # may be None until submit records it
        })
    out.sort(key=lambda r: r.get("submitted_at") or "", reverse=True)
    return out
```

- [ ] **Step 4: Add the route to main.py**

Insert after `get_quota`:

```python
@app.get("/runs")
async def list_runs(active: bool = False, tenant_id: str = "") -> dict[str, Any]:
    if not active:
        raise HTTPException(status_code=400, detail="only active=true is supported")
    if not tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id is required")
    runs = await _q.list_active(tenant_id)
    return {"runs": runs}
```

- [ ] **Step 5: Rebuild + smoke**

```bash
./infra/infra.sh up
./infra/infra.sh smoke 2>&1 | grep runner-list-active
# Expected: ✔
```

- [ ] **Step 6: Commit**

```bash
git add services/runner/src/queue.py services/runner/src/main.py infra/infra.sh
git commit -m "$(cat <<'EOF'
D1.4.1 Task 3: list active runs per tenant

GET /runs?active=true&tenant_id=X returns queued + running runs for
the tenant (cross-tenant returns empty, never 404). Required for the
upcoming Monitor view to surface a Cancel button.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: convert mark_completed/mark_failed to Lua + add mark_canceled + start_or_abort

Foundation for the running-cancel path. No new smoke — the existing 47 tests must continue to pass after the conversion.

**Files:**
- Modify: `services/runner/src/queue.py`

- [ ] **Step 1: Add three new Lua scripts after `_LUA_CANCEL_QUEUED`**

```python
# Atomic terminal transition. Reads cancel_requested first; if set, the
# stored terminal status is 'canceled' regardless of how the container exited.
# Returns {final_status}.
_LUA_MARK_TERMINAL = """
local k_run = KEYS[1]
local k_processing = KEYS[2]
local k_running = KEYS[3]
local run_id = ARGV[1]
local intended_status = ARGV[2]   -- 'completed' or 'failed'
local now = ARGV[3]
local result_or_error = ARGV[4]
local field_name = ARGV[5]        -- 'result' or 'error'

local status = redis.call('HGET', k_run, 'status') or ""
if status == 'completed' or status == 'failed' or status == 'canceled' then
    -- idempotent no-op
    return {status}
end
local cancel = redis.call('HGET', k_run, 'cancel_requested')
local final = intended_status
if cancel == '1' then
    final = 'canceled'
end
redis.call('HSET', k_run,
    'status', final,
    'completed_at', now,
    field_name, result_or_error)
redis.call('LREM', k_processing, 0, run_id)
redis.call('SREM', k_running, run_id)
return {final}
"""

# Atomic mark-canceled used by cancel_queued cleanup paths and by
# recover_orphans / hung-container timeouts. Idempotent.
_LUA_MARK_CANCELED = """
local k_run = KEYS[1]
local k_processing = KEYS[2]
local k_running = KEYS[3]
local run_id = ARGV[1]
local now = ARGV[2]
local status = redis.call('HGET', k_run, 'status') or ""
if status == 'completed' or status == 'failed' or status == 'canceled' then
    return {status}
end
redis.call('HSET', k_run, 'status', 'canceled', 'completed_at', now)
redis.call('LREM', k_processing, 0, run_id)
redis.call('SREM', k_running, run_id)
return {'canceled'}
"""

# Decide whether to start the run or abort because it was canceled in-flight
# (between claim_next and worker's first state-write).
# Returns {'running'} or {'abort'}.
_LUA_START_OR_ABORT = """
local k_run = KEYS[1]
local now = ARGV[1]
local status = redis.call('HGET', k_run, 'status') or ""
if status == 'canceled' then
    return {'abort'}
end
redis.call('HSET', k_run, 'status', 'running', 'started_at', now)
return {'running'}
"""
```

- [ ] **Step 2: Load the new SHAs in `connect()`**

```python
async def connect(self) -> None:
    self._v = Valkey.from_url(self._url, decode_responses=True)
    self._reserve_sha = await self._v.script_load(_LUA_RESERVE)
    self._cancel_queued_sha = await self._v.script_load(_LUA_CANCEL_QUEUED)
    self._mark_terminal_sha = await self._v.script_load(_LUA_MARK_TERMINAL)
    self._mark_canceled_sha = await self._v.script_load(_LUA_MARK_CANCELED)
    self._start_or_abort_sha = await self._v.script_load(_LUA_START_OR_ABORT)
```

- [ ] **Step 3: Rewrite mark_completed and mark_failed to call the Lua**

Replace the existing methods:

```python
async def mark_completed(self, run_id: str, tenant_id: str, result: dict[str, Any]) -> str:
    assert self._v is not None and self._mark_terminal_sha is not None
    now = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    r = await self._v.evalsha(
        self._mark_terminal_sha,
        3,
        k_run(run_id), K_PROCESSING, k_running(tenant_id),
        run_id, "completed", now, json.dumps(result), "result",
    )
    return str(r[0])  # 'completed' or 'canceled' (if cancel_requested was set)

async def mark_failed(self, run_id: str, tenant_id: str, error: str) -> str:
    assert self._v is not None and self._mark_terminal_sha is not None
    now = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    r = await self._v.evalsha(
        self._mark_terminal_sha,
        3,
        k_run(run_id), K_PROCESSING, k_running(tenant_id),
        run_id, "failed", now, error, "error",
    )
    return str(r[0])
```

- [ ] **Step 4: Add `mark_canceled` and `start_or_abort` wrappers**

```python
async def mark_canceled(self, run_id: str, tenant_id: str) -> str:
    assert self._v is not None and self._mark_canceled_sha is not None
    now = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    r = await self._v.evalsha(
        self._mark_canceled_sha,
        3,
        k_run(run_id), K_PROCESSING, k_running(tenant_id),
        run_id, now,
    )
    return str(r[0])

async def start_or_abort(self, run_id: str) -> str:
    assert self._v is not None and self._start_or_abort_sha is not None
    now = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    r = await self._v.evalsha(
        self._start_or_abort_sha,
        1,
        k_run(run_id),
        now,
    )
    return str(r[0])  # 'running' or 'abort'

async def set_container_name(self, run_id: str, container_name: str) -> None:
    assert self._v is not None
    await self._v.hset(k_run(run_id), "container_name", container_name)

async def is_cancel_requested(self, run_id: str) -> bool:
    assert self._v is not None
    v = await self._v.hget(k_run(run_id), "cancel_requested")
    return v == "1"
```

- [ ] **Step 5: Wire mark_running to call start_or_abort**

`mark_running` is now obsolete — its single caller (worker loop) should use `start_or_abort` instead. Keep the method but have it delegate, so any other callers don't break:

```python
async def mark_running(self, run_id: str) -> dict[str, Any]:
    """Legacy method — prefer start_or_abort. Returns the run state HASH."""
    await self.start_or_abort(run_id)
    state = await self._v.hgetall(k_run(run_id))
    return state
```

- [ ] **Step 6: Run full smoke; baseline must hold (47/48 green, same flake)**

```bash
./infra/infra.sh up
./infra/infra.sh smoke 2>&1 | tail -10
# Expected: 47 ✔, 1 ✘ (agent-workers-d1-suite — pre-existing flake).
# The 3 D1.4.1 tests added so far must still pass.
```

If any other test now fails, the Lua conversion regressed something — read its smoke output and fix before continuing.

- [ ] **Step 7: Commit**

```bash
git add services/runner/src/queue.py
git commit -m "$(cat <<'EOF'
D1.4.1 Task 4: convert terminal transitions to Lua

- Convert mark_completed/mark_failed to a single _LUA_MARK_TERMINAL
  script that reads cancel_requested first; if set, writes canceled
  regardless of how the container exited (race 5.4 from the spec).
- Add mark_canceled (idempotent, used by cancel_queued / recovery /
  hung-container paths).
- Add start_or_abort: worker's first state-write becomes a Lua check
  so a cancel arriving between claim_next and run-start is honored.
- mark_running kept as a thin compatibility wrapper.

No new smoke; baseline 47/48 holds.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: cancel_running — worker loop rewrite + podman kill + smoke

The biggest task. Adds the cancel_running Lua, rewrites `_execute_run` to use deterministic container name + a parallel kill-watcher coroutine + a hung-container timeout, adds the running branch to the cancel route, and adds the smoke test that exercises the whole path.

**Files:**
- Modify: `services/runner/src/queue.py` (add `cancel_running` + Lua)
- Modify: `services/runner/src/main.py` (rewrite worker; running branch in cancel route)
- Modify: `infra/infra.sh` (add `smoke_runner_cancel_running`)

- [ ] **Step 1: Add `_LUA_CANCEL_RUNNING` + Python wrapper to queue.py**

```python
# Atomic cancel-running. Tenant-checked. Sets cancel_requested=1 only if
# status is still 'running'. Returns:
#   {0, container_name} on success
#   {1, stored_tenant}  on cross-tenant
#   {2, current_status} if status is not 'running'
#   {3, ""}             if run hash does not exist
_LUA_CANCEL_RUNNING = """
local k_run = KEYS[1]
local run_id = ARGV[1]
local caller_tenant = ARGV[2]
local actor_urn = ARGV[3]
local now = ARGV[4]

if redis.call('EXISTS', k_run) == 0 then
    return {3, ""}
end
local stored_tenant = redis.call('HGET', k_run, 'tenant_id') or ""
if stored_tenant ~= caller_tenant then
    return {1, stored_tenant}
end
local status = redis.call('HGET', k_run, 'status') or ""
if status ~= 'running' then
    return {2, status}
end
redis.call('HSET', k_run,
    'cancel_requested', '1',
    'canceled_by', actor_urn,
    'canceled_at', now)
local cname = redis.call('HGET', k_run, 'container_name') or ""
return {0, cname}
"""
```

Add SHA load in `connect()` (alongside the others):

```python
self._cancel_running_sha = await self._v.script_load(_LUA_CANCEL_RUNNING)
```

Add the Python wrapper:

```python
async def cancel_running(self, run_id: str, actor_urn: str, caller_tenant: str) -> tuple[CancelResult, str]:
    """Returns (CancelResult, container_name). container_name may be '' if cancel
    arrived before worker recorded it; caller should still treat as success and
    let the worker observe cancel_requested on its next poll."""
    assert self._v is not None and self._cancel_running_sha is not None
    now = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    r = await self._v.evalsha(
        self._cancel_running_sha,
        1,
        k_run(run_id),
        run_id, caller_tenant, actor_urn, now,
    )
    code = int(r[0])
    payload = str(r[1])
    if code == 0:
        return CancelResult(0, previous_status="running"), payload
    if code == 1:
        return CancelResult(1, stored_tenant=payload), ""
    if code == 2:
        return CancelResult(2, previous_status=payload), ""
    return CancelResult(3), ""
```

- [ ] **Step 2: Rewrite `_execute_run` in main.py with deterministic name + kill-watcher**

Replace `_execute_run` (lines ~140-165) with:

```python
import httpx  # add to imports if not present

PODMAN_SOCK = "/run/podman/podman.sock"  # bind-mounted by infra.sh
PODMAN_BASE = "http+unix://" + PODMAN_SOCK.replace("/", "%2F")


async def _podman_kill(container_name: str) -> tuple[bool, str]:
    """Returns (success, message). 204/304/404 are success."""
    transport = httpx.AsyncHTTPTransport(uds=PODMAN_SOCK)
    async with httpx.AsyncClient(transport=transport, base_url="http://d") as c:
        try:
            r = await c.post(f"/v4.0.0/libpod/containers/{container_name}/kill", timeout=5.0)
        except Exception as e:  # noqa: BLE001
            return False, f"kill request failed: {e}"
    if r.status_code in (204, 304, 404):
        return True, f"kill ok status={r.status_code}"
    return False, f"kill returned {r.status_code}: {r.text[:200]}"


async def _podman_rm_force(container_name: str) -> None:
    """Best-effort removal; ignore errors (handles 404, already-running, etc.)."""
    transport = httpx.AsyncHTTPTransport(uds=PODMAN_SOCK)
    async with httpx.AsyncClient(transport=transport, base_url="http://d") as c:
        try:
            await c.delete(f"/v4.0.0/libpod/containers/{container_name}?force=true", timeout=5.0)
        except Exception:  # noqa: BLE001
            pass


async def _watch_for_cancel(run_id: str, container_name: str, sandbox_task: asyncio.Task) -> None:
    """Poll Valkey for cancel_requested every 2s; if observed, kill the sandbox.
    Exits when sandbox_task is done."""
    while not sandbox_task.done():
        try:
            await asyncio.wait_for(asyncio.shield(asyncio.sleep(2)), timeout=2.5)
        except asyncio.TimeoutError:
            pass
        if sandbox_task.done():
            return
        if await _q.is_cancel_requested(run_id):
            log.info("worker: cancel_requested for %s; killing %s", run_id, container_name)
            ok, msg = await _podman_kill(container_name)
            log.info("worker: %s podman_kill: %s", run_id, msg)
            return  # sandbox_task will return shortly via container exit


async def _execute_run(run_id: str) -> None:
    state = await _q.get(run_id)
    if not state:
        log.warning("worker: claimed %s but no state", run_id)
        return
    tenant_id = state.get("tenant_id", "default")
    req = state.get("request") or {}

    # start_or_abort: if cancel arrived between claim_next and now, bail.
    decision = await _q.start_or_abort(run_id)
    if decision == "abort":
        log.info("worker: %s aborted before start (canceled while queued)", run_id)
        # Lua already cleared queue/processing, but processing may still hold it
        # if cancel arrived AFTER our claim_next pop and BEFORE start_or_abort.
        # The cancel_queued Lua LREMs processing anyway; this is belt-and-braces.
        return

    # Deterministic container name + record before podman run.
    short = run_id[:12]
    container_name = f"sandbox-{short}"
    await _q.set_container_name(run_id, container_name)
    await _podman_rm_force(container_name)  # idempotent — clears any stale prior container

    timeout_s = req.get("timeout_seconds", DEFAULT_TIMEOUT)
    sandbox_task: asyncio.Task = asyncio.create_task(
        sandbox_executor.run_sandbox(
            test_case=req["test_case"],
            target_url=req.get("target_url"),
            timeout_seconds=timeout_s,
            tenant_id=tenant_id,
            workflow_id=req.get("workflow_id", ""),
            test_case_id=req.get("test_case_id", "unknown"),
            sandbox_id=short,  # pass run-derived id so executor names container sandbox-{short}
            bucket=BUCKET,
            allowed_urls=req.get("allowed_urls", []),
        )
    )
    watcher = asyncio.create_task(_watch_for_cancel(run_id, container_name, sandbox_task))

    try:
        result = await asyncio.wait_for(sandbox_task, timeout=timeout_s + 60)
        final = await _q.mark_completed(run_id, tenant_id, result)
        log.info("worker: %s -> %s (status=%s)", run_id, final, result.get("status", "?"))
    except asyncio.TimeoutError:
        log.error("worker: %s hung past %ds; force-canceling", run_id, timeout_s + 60)
        await _podman_kill(container_name)
        await _q.mark_canceled(run_id, tenant_id)
    except Exception as e:  # noqa: BLE001
        log.exception("worker: %s failed", run_id)
        final = await _q.mark_failed(run_id, tenant_id, f"{type(e).__name__}: {e}")
        log.info("worker: %s -> %s (failed)", run_id, final)
    finally:
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass
```

**Engineer note:** `sandbox_executor.run_sandbox` already accepts `sandbox_id` and (in the existing D1.2 code) names the container `sandbox-{sandbox_id}`. If you find it names it differently, change `container_name` above to match — but the simpler fix is to align `sandbox_executor` to the `sandbox-{sandbox_id}` convention. Verify with `grep -n "sandbox-" services/runner/src/sandbox_executor.py` before running smoke.

- [ ] **Step 3: Add running branch to the cancel route in main.py**

Replace the `running` placeholder in the existing `cancel_run` handler:

```python
    if current_status == "running":
        r, container_name = await _q.cancel_running(run_id, body.actor_urn, body.tenant_id)
        if r.code == 3:
            raise HTTPException(status_code=404, detail="run not found")
        if r.code == 1:
            raise HTTPException(status_code=403, detail="cross-tenant cancel forbidden")
        if r.code == 2:
            raise HTTPException(status_code=409,
                                detail={"detail": "run already terminal", "status": r.previous_status})
        # cancel_requested is set; issue podman kill (the worker will also kill on its next poll).
        if container_name:
            ok, msg = await _podman_kill(container_name)
            if not ok:
                # Leave the flag set; worker will route to canceled when the container eventually exits.
                raise HTTPException(status_code=502, detail={"detail": "kill failed", "error": msg})
        return {"run_id": run_id, "status": "canceled", "previous_status": "running"}
```

(Remove the `501` branch.)

- [ ] **Step 4: Write the failing smoke test**

Append to infra.sh:

```bash
smoke_runner_cancel_running() {
  local TENANT="smk-cancel-r-$RANDOM"
  local rid
  rid=$(curl -fsS -X POST http://localhost:8004/runs -H 'Content-Type: application/json' \
    -d "{\"tenant_id\":\"$TENANT\",\"workflow_id\":\"wf\",\"test_case_id\":\"t\",
         \"test_case\":{\"payload\":{\"title\":\"long-sleep\",\"steps\":[]}},
         \"timeout_seconds\":120}" | jq -r .run_id) || return 1
  # Wait up to 30s for status=running.
  local status="" i
  for i in $(seq 1 30); do
    status=$(curl -fsS "http://localhost:8004/runs/$rid" | jq -r .status)
    [[ "$status" == "running" ]] && break
    sleep 1
  done
  [[ "$status" == "running" ]] || { echo "run never reached running: $status"; return 1; }
  # container should now be alive
  podman exec valkey valkey-cli HGET "run:$rid" container_name > /tmp/cname.out
  local cname
  cname=$(cat /tmp/cname.out)
  [[ -n "$cname" ]] || { echo "container_name not recorded"; return 1; }
  # cancel
  local code
  code=$(curl -s -o /tmp/cancel_r.out -w '%{http_code}' \
    -X POST "http://localhost:8004/runs/$rid/cancel" \
    -H 'Content-Type: application/json' \
    -d "{\"actor_urn\":\"urn:qa-aqa:user:smoke\",\"tenant_id\":\"$TENANT\"}")
  [[ "$code" == "200" ]] || { echo "cancel returned $code body=$(cat /tmp/cancel_r.out)"; return 1; }
  # within 10s, status must be canceled and tenant:running must not contain it
  for i in $(seq 1 10); do
    status=$(curl -fsS "http://localhost:8004/runs/$rid" | jq -r .status)
    [[ "$status" == "canceled" ]] && break
    sleep 1
  done
  [[ "$status" == "canceled" ]] || { echo "expected canceled within 10s, got $status"; return 1; }
  local in_set
  in_set=$(podman exec valkey valkey-cli SISMEMBER "tenant:$TENANT:running" "$rid")
  [[ "$in_set" == "0" ]] || { echo "tenant:running still contains $rid"; return 1; }
}
```

Register `runner-cancel-running` in the smoke list.

- [ ] **Step 5: Run smoke; verify the new test passes and nothing regressed**

```bash
./infra/infra.sh up
./infra/infra.sh smoke 2>&1 | tail -15
# Expected: ✔ runner-cancel-running; baseline still 47/48 holds (+ 4 new D1.4.1 tests).
```

If `runner-cancel-running` times out: tail `podman logs runner-service | tail -50` and look for the watcher's kill log line.

- [ ] **Step 6: Commit**

```bash
git add services/runner/src/queue.py services/runner/src/main.py infra/infra.sh
git commit -m "$(cat <<'EOF'
D1.4.1 Task 5: cancel running sandboxes

- Add _LUA_CANCEL_RUNNING: tenant-check + status==running check +
  HSET cancel_requested + canceled_by/at, returns container_name.
- Rewrite _execute_run: deterministic container name sandbox-{run_id[:12]},
  HSET container_name before podman run, podman rm -f for clean restart,
  asyncio.wait_for with timeout+60s hung-container guard, parallel
  _watch_for_cancel coroutine polls cancel_requested every 2s and
  issues podman kill via Compat API over the bind-mounted socket.
- Cancel endpoint's running branch reads container_name from Lua return,
  issues podman kill (204/304/404 = success), returns 502 on real error.
- New smoke: runner-cancel-running (submit long-sleep, wait for running,
  cancel, assert canceled within 10s + quota slot released).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: cancel-aware recover_orphans

If the runner crashes mid-cancel, the orphan run has `cancel_requested=1` set. On restart, `recover_orphans` must route to `mark_canceled` and clean up the straggler container, not blindly `mark_failed`.

**Files:**
- Modify: `services/runner/src/queue.py` (rewrite `recover_orphans`)
- Modify: `services/runner/src/main.py` (pass podman-rm callable so queue.py doesn't import httpx)

- [ ] **Step 1: Rewrite recover_orphans in queue.py**

Replace the existing implementation:

```python
async def recover_orphans(self, on_canceled_cleanup=None) -> dict[str, int]:
    """On startup, reconcile in-flight runs.
    - If cancel_requested=1: mark_canceled + call on_canceled_cleanup(container_name) if provided.
    - Else: mark_failed("runner restarted mid-run").
    Returns {'canceled': N, 'failed': M}.
    """
    assert self._v is not None
    ids: list[str] = await self._v.lrange(K_PROCESSING, 0, -1)
    canceled = failed = 0
    for run_id in ids:
        state = await self._v.hgetall(k_run(run_id))
        if not state:
            await self._v.lrem(K_PROCESSING, 0, run_id)
            continue
        tenant_id = state.get("tenant_id", "default")
        if state.get("cancel_requested") == "1":
            await self.mark_canceled(run_id, tenant_id)
            cname = state.get("container_name", "")
            if cname and on_canceled_cleanup is not None:
                try:
                    await on_canceled_cleanup(cname)
                except Exception:  # noqa: BLE001
                    pass
            canceled += 1
        else:
            await self.mark_failed(run_id, tenant_id, "runner restarted mid-run")
            failed += 1
    return {"canceled": canceled, "failed": failed}
```

- [ ] **Step 2: Update the startup hook in main.py to pass the cleanup callback**

In `_startup()` (around line 47), change:

```python
    recovered = await _q.recover_orphans()
    if recovered:
        log.info("recovered %d orphan run(s) from prior crash", recovered)
```

to:

```python
    recovered = await _q.recover_orphans(on_canceled_cleanup=_podman_rm_force)
    if recovered.get("canceled") or recovered.get("failed"):
        log.info("recovered orphans: canceled=%d failed=%d",
                 recovered["canceled"], recovered["failed"])
```

- [ ] **Step 3: Run smoke; existing tests must still pass**

```bash
./infra/infra.sh up
./infra/infra.sh smoke 2>&1 | tail -10
# Expected: same 47/48 + 4 D1.4.1 tests still green.
```

**Manual orphan-recovery check (optional):**
```bash
# Start a long-sleep run, immediately cancel, then before the worker's
# next 2s poll, restart the runner. On startup it should observe
# cancel_requested=1 and mark canceled + remove the container.
# Verified by: podman logs runner-service | grep "recovered orphans"
```

- [ ] **Step 4: Commit**

```bash
git add services/runner/src/queue.py services/runner/src/main.py
git commit -m "$(cat <<'EOF'
D1.4.1 Task 6: cancel-aware orphan recovery

recover_orphans now reads cancel_requested per orphan; routes to
mark_canceled (with podman rm -f of the straggler container) instead
of mark_failed when set. Returns {canceled, failed} counts for the
startup log line.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: agent-workers — accept canceled as a terminal status

Currently the executor's run-polling loop treats only `completed` / `failed` as terminal. After D1.4.1, the polled `GET /runs/{id}` may report `canceled` and the executor must surface that to the workflow.

**Files:**
- Modify: `services/agent-workers/src/executor_agent.py`

- [ ] **Step 1: Find the poll loop**

```bash
grep -n "status.*completed.*failed" services/agent-workers/src/executor_agent.py
# Expect a line like: if state.get("status") in ("completed", "failed"):
```

- [ ] **Step 2: Add "canceled" to the terminal set + synthesize a canceled return**

Replace the terminal check:

```python
if state.get("status") in ("completed", "failed", "canceled"):
    if state.get("status") == "canceled":
        return {
            "mode": "playwright_sandbox",
            "status": "canceled",
            "error_message": f"canceled by {state.get('canceled_by','unknown')}",
            "run_id": run_id,
            "canceled_at": state.get("canceled_at"),
        }
    return state.get("result") or {
        "mode": "playwright_sandbox",
        "status": "error",
        "error_message": state.get("error", "no result"),
        "run_id": run_id,
    }
```

- [ ] **Step 3: Rebuild agent-workers, run smoke; baseline holds**

```bash
./infra/infra.sh up
./infra/infra.sh smoke 2>&1 | tail -10
# Expected: same baseline (47/48 + 4 D1.4.1 tests).
```

- [ ] **Step 4: Commit**

```bash
git add services/agent-workers/src/executor_agent.py
git commit -m "$(cat <<'EOF'
D1.4.1 Task 7: agent-workers treats 'canceled' as terminal

The executor's run-polling loop now exits on status='canceled' and
synthesizes a canceled return shape so the Temporal workflow can
distinguish a user-canceled run from a true failure.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: BFF passthrough + bff-cancel-passthrough smoke

End-to-end through APISIX + JWT. The BFF injects `actor_urn` and `tenant_id` from the verified JWT so the browser never has to send them.

**Files:**
- Modify: `services/bff/src/main.py` (add two routes)
- Modify: `infra/infra.sh` (add `smoke_bff_cancel_passthrough`)

- [ ] **Step 1: Write the failing smoke test**

```bash
smoke_bff_cancel_passthrough() {
  # Login as the seeded reviewer user to get a JWT.
  local token
  token=$(curl -fsS -X POST http://localhost:9080/api/auth/login \
    -H 'Content-Type: application/json' \
    -d '{"email":"reviewer@example.com","password":"reviewer123"}' | jq -r .access_token) || return 1
  [[ -n "$token" && "$token" != "null" ]] || { echo "no token"; return 1; }
  # Submit a long-sleep run directly to the runner (BFF doesn't expose submit yet
  # — that's covered by the executor flow). We just need a run to cancel.
  local rid
  rid=$(curl -fsS -X POST http://localhost:8004/runs -H 'Content-Type: application/json' \
    -d '{"tenant_id":"default","workflow_id":"wf","test_case_id":"t",
         "test_case":{"payload":{"title":"sleep","steps":[]}},"timeout_seconds":60}' | jq -r .run_id) || return 1
  # Cancel via BFF — JWT carries tenant_id=default + the user's URN.
  local code
  code=$(curl -s -o /tmp/bff_cancel.out -w '%{http_code}' \
    -X POST "http://localhost:9080/api/runs/$rid/cancel" \
    -H "Authorization: Bearer $token")
  [[ "$code" == "200" ]] || { echo "expected 200, got $code body=$(cat /tmp/bff_cancel.out)"; return 1; }
  # Verify the cancel reached the runner (status=canceled).
  local status
  status=$(curl -fsS "http://localhost:8004/runs/$rid" | jq -r .status)
  [[ "$status" == "canceled" ]] || { echo "expected canceled, got $status"; return 1; }
  # And the listing endpoint via BFF works with JWT-only.
  local count
  count=$(curl -fsS "http://localhost:9080/api/runs?active=true" \
    -H "Authorization: Bearer $token" | jq '.runs | length')
  [[ "$count" =~ ^[0-9]+$ ]] || { echo "list endpoint failed via BFF"; return 1; }
}
```

Register `bff-cancel-passthrough`.

- [ ] **Step 2: Run smoke; expect failure**

```bash
./infra/infra.sh smoke 2>&1 | grep bff-cancel-passthrough
# Expected: ✘  (BFF routes don't exist yet — 404 from APISIX or 405 from BFF)
```

- [ ] **Step 3: Add the BFF routes**

Find the existing `/api/me` endpoint in `services/bff/src/main.py` (which already calls the runner — same pattern). Add:

```python
@app.post("/api/runs/{run_id}/cancel")
async def cancel_run_passthrough(run_id: str, user: User = Depends(current_user)) -> dict[str, Any]:
    runner_url = os.environ.get("RUNNER_URL", "http://runner-service:8004")
    body = {"actor_urn": user.urn, "tenant_id": user.tenant_id}
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.post(f"{runner_url}/runs/{run_id}/cancel", json=body)
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.json().get("detail", r.text))
    return r.json()


@app.get("/api/runs")
async def list_runs_passthrough(active: bool = False, user: User = Depends(current_user)) -> dict[str, Any]:
    if not active:
        raise HTTPException(status_code=400, detail="only active=true is supported")
    runner_url = os.environ.get("RUNNER_URL", "http://runner-service:8004")
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.get(f"{runner_url}/runs", params={"active": "true", "tenant_id": user.tenant_id})
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.json().get("detail", r.text))
    return r.json()
```

Confirm APISIX is configured to proxy `/api/runs*` to the BFF. APISIX uses a path-prefix match `/api/` from D3a, so no APISIX config change should be needed — but verify with:
```bash
grep -A3 "/api" infra/apisix/config.yaml
# Expect a single upstream rule that matches /api/* and forwards to bff:8000.
```

- [ ] **Step 4: Rebuild BFF, run smoke**

```bash
./infra/infra.sh up
./infra/infra.sh smoke 2>&1 | grep -E "bff-cancel-passthrough|Summary"
# Expected: ✔ bff-cancel-passthrough
```

- [ ] **Step 5: Commit**

```bash
git add services/bff/src/main.py infra/infra.sh
git commit -m "$(cat <<'EOF'
D1.4.1 Task 8: BFF passthrough for cancel + list-active

- POST /api/runs/{id}/cancel: requires JWT, injects actor_urn + tenant_id
  from the verified user, forwards verbatim status to runner.
- GET /api/runs?active=true: injects tenant_id from JWT.
- New smoke: bff-cancel-passthrough (end-to-end through APISIX + JWT).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Web — Active runs panel + Cancel button

End-to-end demoable in the browser.

**Files:**
- Modify: `clients/web/src/lib/queries.ts`
- Modify: `clients/web/src/pages/MonitorPage.tsx` (or whatever D3c shipped — verify with `ls clients/web/src/pages/`)

- [ ] **Step 1: Add type, query, and mutation to queries.ts**

Append:

```typescript
// D1.4.1 — Active runs + cancel

export type RunSummary = {
  run_id: string;
  status: "queued" | "running" | "canceled" | "completed" | "failed";
  submitted_at: string | null;
  started_at: string | null;
  workflow_id: string | null;
  actor_urn: string | null;
};

export function useActiveRuns() {
  return useQuery({
    queryKey: ["active-runs"],
    queryFn: () => api<{ runs: RunSummary[] }>("/api/runs?active=true"),
    refetchInterval: 3_000,
  });
}

export function useCancelRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (runId: string) =>
      api<{ run_id: string; status: string; previous_status: string }>(
        `/api/runs/${encodeURIComponent(runId)}/cancel`,
        { method: "POST" }
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["active-runs"] });
      qc.invalidateQueries({ queryKey: ["me-quota"] });
      qc.invalidateQueries({ queryKey: ["workflow"] });
      qc.invalidateQueries({ queryKey: ["wf-status"] });
    },
  });
}
```

- [ ] **Step 2: Add the Active Runs card to the Monitor page**

Open the Monitor page (likely `clients/web/src/pages/MonitorPage.tsx`). Above the existing screenshots / workflow grid, insert:

```tsx
import { useActiveRuns, useCancelRun, type RunSummary } from "../lib/queries";

function ActiveRunsCard() {
  const { data, isLoading } = useActiveRuns();
  const cancel = useCancelRun();
  const runs = data?.runs ?? [];
  if (isLoading && runs.length === 0) return null;
  return (
    <section className="mb-6 rounded border border-slate-800 bg-slate-900/40 p-4">
      <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-slate-300">
        Active runs
      </h2>
      {runs.length === 0 ? (
        <p className="text-sm text-slate-500">No active runs.</p>
      ) : (
        <ul className="space-y-2">
          {runs.map((r) => (
            <li key={r.run_id} className="flex items-center justify-between gap-3 text-sm">
              <div className="flex items-center gap-3">
                <code className="font-mono text-slate-300">{r.run_id.slice(0, 12)}</code>
                <span
                  className={
                    "rounded px-2 py-0.5 text-xs uppercase tracking-wide " +
                    (r.status === "running"
                      ? "bg-emerald-900 text-emerald-200"
                      : "bg-amber-900 text-amber-200")
                  }
                >
                  {r.status}
                </span>
                {r.workflow_id && (
                  <span className="text-xs text-slate-500">workflow: {r.workflow_id}</span>
                )}
              </div>
              <button
                onClick={() => {
                  if (window.confirm(`Cancel run ${r.run_id.slice(0, 12)}? This will terminate the sandbox immediately.`)) {
                    cancel.mutate(r.run_id);
                  }
                }}
                disabled={cancel.isPending}
                className="rounded border border-slate-700 px-3 py-1 text-xs text-slate-200 hover:border-rose-700 hover:text-rose-200 disabled:opacity-50"
              >
                Cancel
              </button>
            </li>
          ))}
        </ul>
      )}
      {cancel.isError && (
        <p className="mt-2 text-xs text-rose-300">
          Cancel failed: {(cancel.error as Error)?.message ?? "unknown error"}
        </p>
      )}
    </section>
  );
}
```

Render `<ActiveRunsCard />` at the top of the page's main column.

- [ ] **Step 3: Manual browser verification**

```bash
# Web is rebuilt + served by APISIX on http://localhost:9080
./infra/infra.sh up
# Open http://localhost:9080 in the browser, log in (reviewer@example.com / reviewer123),
# navigate to /monitor.
# In a separate terminal, submit a long-sleep run:
curl -X POST http://localhost:8004/runs -H 'Content-Type: application/json' \
  -d '{"tenant_id":"default","workflow_id":"wf-demo","test_case_id":"t","
       test_case":{"payload":{"title":"long","steps":[]}},"timeout_seconds":120}'
# Confirm: it appears under Active runs within 3s.
# Click Cancel, confirm the dialog, watch it disappear, watch QuotaBadge "running" counter drop.
```

- [ ] **Step 4: Commit**

```bash
git add clients/web/src/lib/queries.ts clients/web/src/pages/MonitorPage.tsx
git commit -m "$(cat <<'EOF'
D1.4.1 Task 9: Active runs panel + Cancel button

- Add RunSummary type + useActiveRuns (3s poll) + useCancelRun hooks.
- ActiveRunsCard at the top of MonitorPage shows queued+running runs
  with a Cancel button per row. window.confirm gates destructive action.
- useCancelRun invalidates active-runs, me-quota, workflow, and
  wf-status query keys so all surfaces update on success.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: Final smoke + push

Sanity pass on the whole suite and publish.

- [ ] **Step 1: Full smoke run**

```bash
./infra/infra.sh smoke 2>&1 | tail -20
# Expected:
#   53 ✔ (47 baseline + 6 new D1.4.1)
#   1  ✘ agent-workers-d1-suite  (known pre-existing LLM flake)
# Total: 53/54
```

If a different test fails, investigate the relevant `services/<svc>/` logs before pushing. Do NOT push a regression beyond the known flake.

- [ ] **Step 2: Push**

```bash
git push origin main
```

Verify on GitHub that the spec commit (`02d9c76`) and the 9 implementation commits appear in order.

---

## Self-review checklist (already performed inline by the spec author; included here for executor reference)

**Spec coverage** — every numbered section in the spec maps to one or more tasks:
- §3 state machine → Task 4 (start_or_abort, mark_canceled, mark_terminal Lua)
- §4 quota semantics → all transitions in Tasks 1, 4, 5, 6 use the Lua that SREMs tenant:running
- §5 races (5.1–5.6) → Tasks 1 (5.1 partially), 4 (5.4), 5 (5.2, 5.3, 5.6), 6 (5.5)
- §6 API contract → Tasks 1, 3, 5 (runner), Task 8 (BFF)
- §7 data model → Tasks 1, 4, 5 (HSET fields)
- §8 components → Tasks 1–9 cover all five subsystems
- §9 smoke tests → 6 tests across Tasks 1, 2 (×2), 3, 5, 8
- §10 risks → all mitigations are present in implementation tasks
- §11 out-of-scope → no tasks (correct)
- §12 implementation order → matches the task order in this plan

**Placeholder scan** — no TBD / TODO / "implement later" strings in the plan.

**Type/name consistency** — `CancelResult`, `RunSummary`, `_LUA_CANCEL_QUEUED`, `_LUA_CANCEL_RUNNING`, `_LUA_MARK_TERMINAL`, `_LUA_MARK_CANCELED`, `_LUA_START_OR_ABORT`, `cancel_queued`, `cancel_running`, `mark_canceled`, `start_or_abort`, `is_cancel_requested`, `set_container_name`, `list_active`, `_podman_kill`, `_podman_rm_force`, `_watch_for_cancel`, `useActiveRuns`, `useCancelRun`, `ActiveRunsCard` all used consistently across tasks.
