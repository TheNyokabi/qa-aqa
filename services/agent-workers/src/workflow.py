"""DesignTestsWorkflow — the v1 anchor workflow.

Shape:
  1) Create requirement artefact (deterministic id; idempotent on retry)
  2) Run LangGraph agent (heartbeat, non-retryable on policy/schema errors)
  3) Bulk persist test_case artefacts (one activity event, one tx)

Tenant context is propagated via workflow_id prefix: "<tenant>:design-tests:<uuid>".
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from . import config
    from .activities import (
        bulk_create_artefacts_activity,
        create_artefact_activity,
        run_test_designer_activity,
    )


NON_RETRYABLE = [
    "PolicyDeniedError",
    "SchemaValidationError",
    "ArtefactConflictError",
    "TenantNotFoundError",
    "BudgetExceededError",
]


@workflow.defn
class DesignTestsWorkflow:
    @workflow.run
    async def run(self, req: dict[str, Any]) -> dict[str, Any]:
        wf_id = workflow.info().workflow_id
        # "<tenant>:design-tests:<uuid>"; fall back to "default" if unparseable
        parts = wf_id.split(":", 1)
        tenant_id = parts[0] if len(parts) > 1 else config.DEFAULT_TENANT
        criticality = req.get("criticality", "low")
        compliance_level = req.get("compliance_level", "none")

        # 1) Requirement artefact (idempotent via deterministic id + key)
        requirement_artefact = await workflow.execute_activity(
            create_artefact_activity,
            args=[{
                "id": f"requirement:{wf_id}",
                "tenant_id": tenant_id,
                "type": "requirement",
                "payload": req,
                "workflow_id": wf_id,
                "actor": config.AGENT_URN,
                "compliance_level": compliance_level,
                "idempotency_key": f"req:{wf_id}",
            }],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3, non_retryable_error_types=NON_RETRYABLE),
        )
        req_id = requirement_artefact["id"]

        # 2) LangGraph agent (heartbeated, non-retryable on policy/schema)
        agent_out = await workflow.execute_activity(
            run_test_designer_activity,
            args=[{
                "requirement": req,
                "criticality": criticality,
                "tenant_id": tenant_id,
                "workflow_id": wf_id,
                "parent_id": req_id,
                "compliance_level": compliance_level,
            }],
            start_to_close_timeout=timedelta(minutes=10),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=2, non_retryable_error_types=NON_RETRYABLE),
        )
        cases: list[dict[str, Any]] = agent_out.get("cases", [])
        attestation: dict[str, Any] = agent_out.get("attestation", {})

        # 3) Bulk persist test cases — single activity event regardless of N
        items = [
            {
                "id": f"test_case:{wf_id}:{i}",
                "tenant_id": tenant_id,
                "type": "test_case",
                "payload": case,
                "parent_id": req_id,
                "workflow_id": wf_id,
                "actor": config.AGENT_URN,
                "compliance_level": compliance_level,
                "attestation": attestation,
                "idempotency_key": f"tc:{wf_id}:{i}",
                "metadata": {
                    "critique_findings_count": len(case.get("_critique_findings", []))
                                              if isinstance(case, dict) else 0,
                    "model_used": agent_out.get("model_used", ""),
                    "critic_skipped_reason": agent_out.get("critic_skipped_reason", ""),
                },
            }
            for i, case in enumerate(cases)
        ]
        bulk_result: list[dict[str, Any]] = []
        if items:
            bulk_result = await workflow.execute_activity(
                bulk_create_artefacts_activity,
                args=[items],
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=3, non_retryable_error_types=NON_RETRYABLE),
            )

        return {
            "tenant_id": tenant_id,
            "requirement_id": req_id,
            "test_case_ids": [x["id"] for x in bulk_result],
            "attestation": attestation,
            "critic_skipped_reason": agent_out.get("critic_skipped_reason", ""),
            "model_used": agent_out.get("model_used", ""),
            "schema_attempts": agent_out.get("schema_attempts", 0),
        }
