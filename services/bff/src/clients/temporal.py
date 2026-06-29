"""Thin Temporal client wrapper.

BFF uses the temporalio SDK as a read/write client only — it does NOT host
workflow code. Workflow types are referenced by string name, which the
agent-workers worker registers under.
"""
from __future__ import annotations

from typing import Any

from temporalio.client import Client, WorkflowExecutionStatus

from ..config import TEMPORAL_HOST, TEMPORAL_TASK_QUEUE

_client: Client | None = None


async def _get() -> Client:
    global _client
    if _client is None:
        _client = await Client.connect(TEMPORAL_HOST)
    return _client


async def start_design_tests(workflow_id: str, requirement: dict[str, Any]) -> str:
    client = await _get()
    handle = await client.start_workflow(
        "DesignTestsWorkflow",
        requirement,
        id=workflow_id,
        task_queue=TEMPORAL_TASK_QUEUE,
    )
    return handle.id


async def start_execute_tests(workflow_id: str, body: dict[str, Any]) -> str:
    client = await _get()
    handle = await client.start_workflow(
        "ExecuteTestsWorkflow",
        body,
        id=workflow_id,
        task_queue=TEMPORAL_TASK_QUEUE,
    )
    return handle.id


_STATUS_NAMES = {
    WorkflowExecutionStatus.RUNNING: "RUNNING",
    WorkflowExecutionStatus.COMPLETED: "COMPLETED",
    WorkflowExecutionStatus.FAILED: "FAILED",
    WorkflowExecutionStatus.CANCELED: "CANCELED",
    WorkflowExecutionStatus.TERMINATED: "TERMINATED",
    WorkflowExecutionStatus.CONTINUED_AS_NEW: "CONTINUED_AS_NEW",
    WorkflowExecutionStatus.TIMED_OUT: "TIMED_OUT",
}


async def workflow_status(workflow_id: str) -> dict[str, Any]:
    """Return workflow execution status + result (if completed)."""
    client = await _get()
    handle = client.get_workflow_handle(workflow_id)
    try:
        desc = await handle.describe()
    except Exception as e:  # noqa: BLE001
        return {"status": "UNKNOWN", "error": str(e)}
    status_name = _STATUS_NAMES.get(desc.status, str(desc.status))
    out: dict[str, Any] = {
        "workflow_id": workflow_id,
        "status": status_name,
        "start_time": desc.start_time.isoformat() if desc.start_time else None,
        "close_time": desc.close_time.isoformat() if desc.close_time else None,
        "execution_time": desc.execution_time.isoformat() if desc.execution_time else None,
    }
    if desc.status == WorkflowExecutionStatus.COMPLETED:
        try:
            out["result"] = await handle.result()
        except Exception as e:  # noqa: BLE001
            out["result_error"] = str(e)
    elif desc.status == WorkflowExecutionStatus.FAILED:
        out["result_error"] = "workflow failed"
    return out
