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

    async def connect(self) -> None:
        self._v = Valkey.from_url(self._url, decode_responses=True)
        self._reserve_sha = await self._v.script_load(_LUA_RESERVE)
        self._cancel_queued_sha = await self._v.script_load(_LUA_CANCEL_QUEUED)

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

    async def mark_running(self, run_id: str) -> dict[str, Any]:
        assert self._v is not None
        now = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
        await self._v.hset(k_run(run_id), mapping={"status": "running", "started_at": now})
        state = await self._v.hgetall(k_run(run_id))
        return state

    async def mark_completed(self, run_id: str, tenant_id: str, result: dict[str, Any]) -> None:
        assert self._v is not None
        now = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
        await self._v.hset(
            k_run(run_id),
            mapping={"status": "completed", "completed_at": now, "result": json.dumps(result)},
        )
        await self._v.lrem(K_PROCESSING, 0, run_id)
        await self.release(tenant_id, run_id)

    async def mark_failed(self, run_id: str, tenant_id: str, error: str) -> None:
        assert self._v is not None
        now = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
        await self._v.hset(
            k_run(run_id),
            mapping={"status": "failed", "completed_at": now, "error": error},
        )
        await self._v.lrem(K_PROCESSING, 0, run_id)
        await self.release(tenant_id, run_id)

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

    async def recover_orphans(self) -> int:
        """On startup, mark any in-flight runs as failed. Returns count recovered."""
        assert self._v is not None
        ids: list[str] = await self._v.lrange(K_PROCESSING, 0, -1)
        n = 0
        for run_id in ids:
            state = await self._v.hgetall(k_run(run_id))
            tenant_id = state.get("tenant_id", "default")
            await self.mark_failed(run_id, tenant_id, "runner restarted mid-run")
            n += 1
        return n

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
