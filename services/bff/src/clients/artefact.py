"""Thin httpx client for artefact-service. BFF stamps X-Tenant-ID from JWT."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import httpx

from ..config import ARTEFACT_URL


@asynccontextmanager
async def client_for(tenant_id: str):
    async with httpx.AsyncClient(
        base_url=ARTEFACT_URL,
        headers={"X-Tenant-ID": tenant_id, "Content-Type": "application/json"},
        timeout=httpx.Timeout(30.0),
    ) as c:
        yield c


async def get_artefact(tenant_id: str, aid: str) -> dict[str, Any] | None:
    async with client_for(tenant_id) as c:
        r = await c.get(f"/artefacts/{aid}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


async def list_artefacts(tenant_id: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    async with client_for(tenant_id) as c:
        r = await c.get("/artefacts", params=params)
        r.raise_for_status()
        return r.json()


async def history(tenant_id: str, aid: str) -> list[dict[str, Any]]:
    async with client_for(tenant_id) as c:
        r = await c.get(f"/artefacts/{aid}/history")
        r.raise_for_status()
        return r.json()


async def transition(tenant_id: str, aid: str, to_state: str, actor_urn: str) -> dict[str, Any]:
    async with client_for(tenant_id) as c:
        r = await c.post(
            f"/artefacts/{aid}/transition",
            json={"to_state": to_state, "actor": actor_urn},
        )
        if r.status_code in (404, 409):
            r.raise_for_status()
        r.raise_for_status()
        return r.json()


async def approval_policy(tenant_id: str, target_type: str) -> dict[str, Any]:
    async with client_for(tenant_id) as c:
        r = await c.get(f"/policies/approval/{target_type}")
        r.raise_for_status()
        return r.json()
