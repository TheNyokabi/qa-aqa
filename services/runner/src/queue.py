"""Valkey-backed async queue + per-tenant quota for runner-service.

State model:
  runs:queue                    LIST   pending run_id values (FIFO)
  runs:processing               LIST   run_ids the worker is mid-execution
  run:{run_id}                  HASH   per-run state: status, started/finished,
                                       request_json, result_json, error, tenant_id
  tenant:{tenant_id}:running    SET    run_ids currently running for tenant
  tenant:{tenant_id}:daily:DATE STRING count of submits today (TTL 48h)

Status values: queued | running | completed | failed
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import uuid
from typing import Any

from valkey.asyncio import Valkey

VALKEY_URL = os.environ.get("VALKEY_URL", "redis://valkey:6379")
QUOTA_CONCURRENT_DEFAULT = int(os.environ.get("QUOTA_CONCURRENT_DEFAULT", "3"))
QUOTA_DAILY_DEFAULT = int(os.environ.get("QUOTA_DAILY_DEFAULT", "100"))

RUN_TTL_SECONDS = 24 * 3600
DAILY_TTL_SECONDS = 48 * 3600

K_QUEUE = "runs:queue"
K_PROCESSING = "runs:processing"


def k_run(run_id: str) -> str:
    return f"run:{run_id}"


def k_running(tenant_id: str) -> str:
    return f"tenant:{tenant_id}:running"


def k_daily(tenant_id: str, date: _dt.date) -> str:
    return f"tenant:{tenant_id}:daily:{date.isoformat()}"


# Atomic check-and-increment for quotas. Returns:
#   {0, current_concurrent, current_daily}   on success (reserved)
#   {1, current, max}                        on concurrent over-cap
#   {2, current, max}                        on daily over-cap
_LUA_RESERVE = """
local k_running = KEYS[1]
local k_daily = KEYS[2]
local run_id = ARGV[1]
local max_concurrent = tonumber(ARGV[2])
local max_daily = tonumber(ARGV[3])
local daily_ttl = tonumber(ARGV[4])

local concurrent = tonumber(redis.call('SCARD', k_running)) or 0
if concurrent >= max_concurrent then
    return {1, concurrent, max_concurrent}
end

local daily = tonumber(redis.call('GET', k_daily)) or 0
if daily >= max_daily then
    return {2, daily, max_daily}
end

redis.call('SADD', k_running, run_id)
local new_daily = redis.call('INCR', k_daily)
redis.call('EXPIRE', k_daily, daily_ttl)
return {0, concurrent + 1, new_daily}
"""


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


# Atomic terminal transition. Reads cancel_requested first; if set, the stored
# terminal status is 'canceled' regardless of how the container exited (spec
# §5.4 sandbox-completes-just-as-cancel-arrives). Idempotent: a second call on
# an already-terminal run no-ops. Returns {final_status}.
_LUA_MARK_TERMINAL = """
local k_run = KEYS[1]
local k_processing = KEYS[2]
local k_running = KEYS[3]
local run_id = ARGV[1]
local intended_status = ARGV[2]
local now = ARGV[3]
local result_or_error = ARGV[4]
local field_name = ARGV[5]

local status = redis.call('HGET', k_run, 'status') or ""
if status == 'completed' or status == 'failed' or status == 'canceled' then
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


# Atomic mark-canceled for paths where the run has NOT reached the sandbox
# (recover_orphans with cancel_requested=1, hung-container timeout, etc.).
# Idempotent. Returns {final_status}.
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


# Decide whether to start the run or abort because a cancel arrived between
# claim_next and the worker's first state-write (spec §5.1). Returns {'running'}
# or {'abort'}.
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


# Atomic cancel-running. Tenant-checked. Sets cancel_requested=1 only if
# status is still 'running'. Returns:
#   {0, container_name} on success (may be empty if worker hasn't recorded it yet)
#   {1, stored_tenant}  on cross-tenant (caller -> 403)
#   {2, current_status} if status is not 'running' (caller -> 409)
#   {3, ""}             if run hash does not exist (caller -> 404)
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


class QuotaExceeded(Exception):
    def __init__(self, kind: str, current: int, maximum: int):
        self.kind = kind
        self.current = current
        self.maximum = maximum
        super().__init__(f"quota_{kind} exceeded: {current}/{maximum}")


class CancelResult:
    def __init__(self, code: int, previous_status: str = "", stored_tenant: str = ""):
        self.code = code  # 0=ok 1=cross-tenant 2=wrong-status 3=not-found
        self.previous_status = previous_status
        self.stored_tenant = stored_tenant


class Queue:
    def __init__(self, valkey_url: str = VALKEY_URL):
        self._url = valkey_url
        self._v: Valkey | None = None
        self._reserve_sha: str | None = None
        self._cancel_queued_sha: str | None = None
        self._mark_terminal_sha: str | None = None
        self._mark_canceled_sha: str | None = None
        self._start_or_abort_sha: str | None = None
        self._cancel_running_sha: str | None = None

    async def connect(self) -> None:
        self._v = Valkey.from_url(self._url, decode_responses=True)
        self._reserve_sha = await self._v.script_load(_LUA_RESERVE)
        self._cancel_queued_sha = await self._v.script_load(_LUA_CANCEL_QUEUED)
        self._mark_terminal_sha = await self._v.script_load(_LUA_MARK_TERMINAL)
        self._mark_canceled_sha = await self._v.script_load(_LUA_MARK_CANCELED)
        self._start_or_abort_sha = await self._v.script_load(_LUA_START_OR_ABORT)
        self._cancel_running_sha = await self._v.script_load(_LUA_CANCEL_RUNNING)

    async def close(self) -> None:
        if self._v:
            await self._v.aclose()

    async def reserve_or_raise(self, tenant_id: str, run_id: str) -> dict[str, int]:
        """Atomic quota check + increment. Raises QuotaExceeded on cap."""
        assert self._v is not None and self._reserve_sha is not None
        today = _dt.datetime.now(tz=_dt.timezone.utc).date()
        result = await self._v.evalsha(
            self._reserve_sha,
            2,
            k_running(tenant_id),
            k_daily(tenant_id, today),
            run_id,
            str(QUOTA_CONCURRENT_DEFAULT),
            str(QUOTA_DAILY_DEFAULT),
            str(DAILY_TTL_SECONDS),
        )
        code, current, maximum = int(result[0]), int(result[1]), int(result[2])
        if code == 1:
            raise QuotaExceeded("concurrent", current, maximum)
        if code == 2:
            raise QuotaExceeded("daily", current, maximum)
        return {"concurrent": current, "daily": maximum}

    async def release(self, tenant_id: str, run_id: str) -> None:
        assert self._v is not None
        await self._v.srem(k_running(tenant_id), run_id)

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

    async def cancel_running(
        self, run_id: str, actor_urn: str, caller_tenant: str
    ) -> tuple[CancelResult, str]:
        """Set cancel_requested=1 on a running run and return (result, container_name).

        container_name may be '' if the cancel arrived before the worker recorded
        it; the caller should still treat as success and let the worker observe
        cancel_requested on its next poll.
        """
        assert self._v is not None and self._cancel_running_sha is not None
        now = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
        result = await self._v.evalsha(
            self._cancel_running_sha,
            1,
            k_run(run_id),
            run_id, caller_tenant, actor_urn, now,
        )
        code = int(result[0])
        payload = str(result[1])
        if code == 0:
            return CancelResult(0, previous_status="running"), payload
        if code == 1:
            return CancelResult(1, stored_tenant=payload), ""
        if code == 2:
            return CancelResult(2, previous_status=payload), ""
        return CancelResult(3), ""

    async def submit(self, tenant_id: str, request: dict[str, Any]) -> str:
        assert self._v is not None
        run_id = uuid.uuid4().hex
        # Quota check + reserve
        await self.reserve_or_raise(tenant_id, run_id)
        # Persist run state
        now = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
        await self._v.hset(
            k_run(run_id),
            mapping={
                "status": "queued",
                "tenant_id": tenant_id,
                "submitted_at": now,
                "request": json.dumps(request),
            },
        )
        await self._v.expire(k_run(run_id), RUN_TTL_SECONDS)
        # Enqueue
        await self._v.lpush(K_QUEUE, run_id)
        return run_id

    async def claim_next(self, timeout: int = 5) -> str | None:
        """Block up to `timeout` seconds for the next run_id. Atomic move to processing list."""
        assert self._v is not None
        run_id = await self._v.brpoplpush(K_QUEUE, K_PROCESSING, timeout=timeout)
        return run_id

    async def start_or_abort(self, run_id: str) -> str:
        """Atomic first state-write after claim_next.

        Returns 'running' (worker should spawn the sandbox) or 'abort' (a cancel
        arrived between claim_next and here — worker skips to the next item).
        """
        assert self._v is not None and self._start_or_abort_sha is not None
        now = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
        r = await self._v.evalsha(
            self._start_or_abort_sha,
            1,
            k_run(run_id),
            now,
        )
        return str(r[0])

    async def mark_running(self, run_id: str) -> dict[str, Any]:
        """Legacy method — prefer start_or_abort. Kept so any external callers
        (tests, tools) don't break. Returns the run HASH after transition."""
        assert self._v is not None
        await self.start_or_abort(run_id)
        state = await self._v.hgetall(k_run(run_id))
        return state

    async def mark_completed(self, run_id: str, tenant_id: str, result: dict[str, Any]) -> str:
        """Atomic terminal transition to completed. If cancel_requested=1 was
        observed, the stored terminal status is 'canceled' instead — returned
        value reflects what was actually written.
        """
        assert self._v is not None and self._mark_terminal_sha is not None
        now = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
        r = await self._v.evalsha(
            self._mark_terminal_sha,
            3,
            k_run(run_id), K_PROCESSING, k_running(tenant_id),
            run_id, "completed", now, json.dumps(result), "result",
        )
        return str(r[0])

    async def mark_failed(self, run_id: str, tenant_id: str, error: str) -> str:
        """Atomic terminal transition to failed. Same cancel-reroute semantics
        as mark_completed."""
        assert self._v is not None and self._mark_terminal_sha is not None
        now = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
        r = await self._v.evalsha(
            self._mark_terminal_sha,
            3,
            k_run(run_id), K_PROCESSING, k_running(tenant_id),
            run_id, "failed", now, error, "error",
        )
        return str(r[0])

    async def mark_canceled(self, run_id: str, tenant_id: str) -> str:
        """Atomic mark-canceled for paths where no sandbox result exists yet
        (recovery / hung-container timeout). Idempotent."""
        assert self._v is not None and self._mark_canceled_sha is not None
        now = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
        r = await self._v.evalsha(
            self._mark_canceled_sha,
            3,
            k_run(run_id), K_PROCESSING, k_running(tenant_id),
            run_id, now,
        )
        return str(r[0])

    async def set_container_name(self, run_id: str, container_name: str) -> None:
        assert self._v is not None
        await self._v.hset(k_run(run_id), "container_name", container_name)

    async def is_cancel_requested(self, run_id: str) -> bool:
        assert self._v is not None
        v = await self._v.hget(k_run(run_id), "cancel_requested")
        return v == "1"

    async def get(self, run_id: str) -> dict[str, Any] | None:
        assert self._v is not None
        h = await self._v.hgetall(k_run(run_id))
        if not h:
            return None
        out: dict[str, Any] = dict(h)
        for key in ("request", "result"):
            if key in out and out[key]:
                try:
                    out[key] = json.loads(out[key])
                except Exception:
                    pass
        return out

    async def list_active(self, tenant_id: str) -> list[dict[str, Any]]:
        """Return queued + running runs for a tenant, sorted by submitted_at desc.

        Bounded by quota maxima (≤ ~150 runs worst case). Scans K_QUEUE plus the
        tenant's running set; the union deduplicates run_ids that appear in both
        during the brief BRPOPLPUSH → mark_running window.
        """
        assert self._v is not None
        queued_ids: list[str] = await self._v.lrange(K_QUEUE, 0, -1)
        running_ids: list[str] = list(await self._v.smembers(k_running(tenant_id)))
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for rid in queued_ids + running_ids:
            if rid in seen:
                continue
            seen.add(rid)
            h = await self._v.hgetall(k_run(rid))
            if not h or h.get("tenant_id") != tenant_id:
                continue
            status = h.get("status", "")
            if status not in ("queued", "running"):
                continue
            workflow_id = None
            if h.get("request"):
                try:
                    workflow_id = json.loads(h["request"]).get("workflow_id")
                except Exception:
                    pass
            out.append({
                "run_id": rid,
                "status": status,
                "submitted_at": h.get("submitted_at"),
                "started_at": h.get("started_at"),
                "workflow_id": workflow_id,
            })
        out.sort(key=lambda r: r.get("submitted_at") or "", reverse=True)
        return out

    async def recover_orphans(self, on_canceled_cleanup=None) -> dict[str, int]:
        """On startup, reconcile in-flight runs.

        If `cancel_requested=1` was observed before the crash: route to
        mark_canceled and invoke `on_canceled_cleanup(container_name)` if
        provided (to remove the straggler container). Otherwise: mark_failed
        with a "runner restarted mid-run" note.

        Returns {'canceled': N, 'failed': M}.
        """
        assert self._v is not None
        ids: list[str] = await self._v.lrange(K_PROCESSING, 0, -1)
        canceled = 0
        failed = 0
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

    async def quota_status(self, tenant_id: str) -> dict[str, Any]:
        """Live quota counters for the BFF /api/me payload."""
        assert self._v is not None
        today = _dt.datetime.now(tz=_dt.timezone.utc).date()
        concurrent = await self._v.scard(k_running(tenant_id))
        daily_raw = await self._v.get(k_daily(tenant_id, today))
        daily = int(daily_raw or 0)
        # Resets at next UTC midnight
        tomorrow = _dt.datetime.combine(today + _dt.timedelta(days=1), _dt.time.min, tzinfo=_dt.timezone.utc)
        return {
            "concurrent": {"current": int(concurrent), "max": QUOTA_CONCURRENT_DEFAULT},
            "daily": {
                "current": daily,
                "max": QUOTA_DAILY_DEFAULT,
                "resets_at": tomorrow.isoformat(),
            },
        }
