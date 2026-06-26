"""ExecuteTestsWorkflow — runs the executor agent over a batch of test_cases.

Three modes (per D1.1 spec):
  simulate           - LLM reasons about likely outcome (serial within one batch activity)
  scripts            - LLM emits Playwright TS or Robot Framework (serial within one batch activity)
  playwright_sandbox - runner-service runs real browser (parallel via asyncio.gather, cap=3)
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from . import config
    from .activities import (
        bulk_create_artefacts_activity,
        fetch_artefacts_activity,
        run_executor_activity,
        run_executor_batch_activity,
    )


NON_RETRYABLE = [
    "PolicyDeniedError",
    "SchemaValidationError",
    "ArtefactConflictError",
    "TenantNotFoundError",
    "BudgetExceededError",
]
SANDBOX_PARALLEL_CAP = 3


@workflow.defn
class ExecuteTestsWorkflow:
    @workflow.run
    async def run(self, req: dict[str, Any]) -> dict[str, Any]:
        wf_id = workflow.info().workflow_id
        parts = wf_id.split(":", 1)
        tenant_id = parts[0] if len(parts) > 1 else config.DEFAULT_TENANT
        mode = req["mode"]
        compliance_level = req.get("compliance_level", "none")

        # 1) Fetch test_case artefacts
        test_cases = await workflow.execute_activity(
            fetch_artefacts_activity,
            args=[{"tenant_id": tenant_id, "ids": req["test_case_ids"]}],
            start_to_close_timeout=timedelta(seconds=60),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3, non_retryable_error_types=NON_RETRYABLE),
        )
        if not test_cases:
            return {"tenant_id": tenant_id, "execution_result_ids": [], "mode": mode, "warning": "no test cases"}

        # 2) Mode-aware execution
        if mode == "playwright_sandbox":
            # Parallel; semaphore-cap so we don't OOM
            sem = asyncio.Semaphore(SANDBOX_PARALLEL_CAP)

            async def _one(tc: dict[str, Any]) -> dict[str, Any]:
                async with sem:
                    return await workflow.execute_activity(
                        run_executor_activity,
                        args=[{
                            "test_case": tc,
                            "mode": "playwright_sandbox",
                            "target_url": req.get("target_url"),
                            "sandbox_timeout_seconds": req.get("sandbox_timeout_seconds", 120),
                            "tenant_id": tenant_id,
                            "workflow_id": wf_id,
                            "criticality": req.get("criticality", "low"),
                            "allowed_urls": req.get("allowed_urls", []),
                        }],
                        start_to_close_timeout=timedelta(seconds=req.get("sandbox_timeout_seconds", 120) + 60),
                        heartbeat_timeout=timedelta(seconds=30),
                        retry_policy=RetryPolicy(maximum_attempts=2, non_retryable_error_types=NON_RETRYABLE),
                    )

            results = await asyncio.gather(*[_one(tc) for tc in test_cases])
        else:
            results = await workflow.execute_activity(
                run_executor_batch_activity,
                args=[{
                    "test_cases": test_cases,
                    "mode": mode,
                    "language": req.get("language", "playwright"),
                    "tenant_id": tenant_id,
                    "workflow_id": wf_id,
                    "criticality": req.get("criticality", "low"),
                }],
                start_to_close_timeout=timedelta(minutes=30),
                heartbeat_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=2, non_retryable_error_types=NON_RETRYABLE),
            )

        # 3) Bulk persist execution_result artefacts
        items = []
        for i, (tc, result) in enumerate(zip(test_cases, results)):
            items.append({
                "id": f"execution_result:{wf_id}:{i}",
                "tenant_id": tenant_id,
                "type": "execution_result",
                "payload": result,
                "parent_id": tc["id"],
                "workflow_id": wf_id,
                "actor": config.EXECUTOR_URN,
                "compliance_level": compliance_level,
                "idempotency_key": f"er:{wf_id}:{i}",
                "metadata": {
                    "mode": mode,
                    "test_case_id": tc["id"],
                    "language": req.get("language", "") if mode == "scripts" else "",
                },
            })
        persisted = await workflow.execute_activity(
            bulk_create_artefacts_activity,
            args=[items],
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=3, non_retryable_error_types=NON_RETRYABLE),
        )

        return {
            "tenant_id": tenant_id,
            "execution_result_ids": [x["id"] for x in persisted],
            "mode": mode,
            "test_case_count": len(test_cases),
        }
