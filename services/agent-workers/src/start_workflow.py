"""CLI helper: start a workflow and wait for the result.

Usage (from inside the agent-workers container):
    python -m src.start_workflow design-tests '<json>' [tenant=default]
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid

from temporalio.client import Client

from . import config
from .registry import lookup


async def main() -> int:
    if len(sys.argv) < 3:
        print("usage: start_workflow <workflow_type> <json> [tenant=default]", file=sys.stderr)
        return 2
    workflow_type = sys.argv[1]
    payload_raw = sys.argv[2]
    tenant = sys.argv[3] if len(sys.argv) >= 4 else config.DEFAULT_TENANT
    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError as e:
        print(f"input is not JSON: {e}", file=sys.stderr)
        return 2

    workflow_cls = lookup(tenant, workflow_type)
    wf_id = f"{tenant}:{workflow_type}:{uuid.uuid4().hex[:12]}"
    client = await Client.connect(config.TEMPORAL_HOST)
    handle = await client.start_workflow(
        workflow_cls.run,
        payload,
        id=wf_id,
        task_queue=config.TASK_QUEUE,
    )
    print(json.dumps({"workflow_id": handle.id}))
    sys.stdout.flush()
    if "--wait" in sys.argv:
        result = await handle.result()
        print(json.dumps({"workflow_id": handle.id, "result": result}))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
