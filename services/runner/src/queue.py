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


class QuotaExceeded(Exception):
    def __init__(self, kind: str, current: int, maximum: int):
        self.kind = kind
        self.current = current
        self.maximum = maximum
        super().__init__(f"quota_{kind} exceeded: {current}/{maximum}")


class Queue:
    def __init__(self, valkey_url: str = VALKEY_URL):
        self._url = valkey_url
        self._v: Valkey | None = None
        self._reserve_sha: str | None = None

    async def connect(self) -> None:
        self._v = Valkey.from_url(self._url, decode_responses=True)
        self._reserve_sha = await self._v.script_load(_LUA_RESERVE)

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
