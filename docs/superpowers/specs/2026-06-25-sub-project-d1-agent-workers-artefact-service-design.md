# Sub-project D1 — agent-workers (compliance substrate already shipped)

**Date:** 2026-06-25 (revised after split — D0.5 separated)
**Scope:** Ships one Temporal workflow (`design-tests`) and one agent role (`test_designer`) built with LangGraph. Persistence, RLS, URN validation, idempotency, and approval-policy lookup are already shipped in **Sub-project D0.5** and consumed via HTTP only.
**Depends on:** 0+A+B+C + **D0.5** (compliance substrate). D0.5 must ship and smoke green before D1 begins.

> **Read this first:** [`2026-06-25-sub-project-d0_5-compliance-substrate-design.md`](2026-06-25-sub-project-d0_5-compliance-substrate-design.md). This D1 spec assumes the artefact-service, schema, RLS, URN grammar, idempotency contract, and approval-policy-as-artefact mechanics are already running. D1 is the **agent layer that sits on top**.

## Locked decisions

### Original (carried forward)
- **Anchor workflow:** `design-tests` — input: `requirement`, output: N `test_case` artefacts in state=`draft`
- **Agent framework:** LangGraph (state graph) + LangChain core. Both MIT/Apache 2.0.
- **Workflow engine:** Temporal (existing). Task queue `test-design`. Single Temporal namespace `default`.
- **Artefact types v1:** `requirement`, `test_case`, plus `critique_policy` and `approval_policy` (the system eats its own dogfood — these are stored as artefacts).
- **Agent roles v1:** `test_designer`. `executor`, `reporter` land later.
- **Trigger surface for D1:** agent-workers ships a `start_workflow.py` CLI helper. The api-gateway in D3 will be the proper trigger surface; this is a stopgap.

### Added after review
- **Actor-Critic validation gated on `requirement.criticality`** — `low` skips, `medium` runs OPA rubric only, `high` runs OPA + LLM critic on `chat-prod`, `safety_critical` adds a mandatory human approval step. Critic NEVER runs on the dev model (asymmetric review or no review).
- **Critic rubric stored as `critique_policy` artefact** — versioned YAML + Rego policy, not prompt prose. Critic LLM evaluates against the rubric; OPA enforces deterministic checks.
- **Test case payload schema is Robot Framework / Playwright consumable from day 1** — `steps: [{library, keyword, args}]`. Consumers don't need a translation layer.
- **Multi-tenancy substrate now:** `tenant_id text NOT NULL DEFAULT 'default'` on every tenant-scoped table + RLS enabled in dev as a no-op + tenant propagation pattern documented.
- **Actor URN grammar:** `urn:qa-aqa:<actor_type>:<id>[:v<version>]`. Validated at Pydantic boundary with a compiled regex. `actor_type ∈ {agent, user, system, service}`.
- **RAG retrieval is multi-corpus:** rag-service supports `corpus` parameter. D1 ingests `docs/superpowers/specs/*.md` into `corpus=docs` on first agent-workers start. Test designer retrieves from `corpus=docs` AND `corpus=test_cases` and RRF-merges.
- **Workflow registry** — single in-memory dict in `worker.py` keyed by `<tenant>:<workflow_type>` → `WorkflowClass`. Ships with one entry; the shape is set for D1.1 per-factory overrides.
- **Workflow attestation** — every workflow result includes an `attestation` object: `{model_fingerprints, prompt_hashes, rag_retrieval_ids, seed, policy_version, output_hash}`. Empty when `compliance_level=none`; populated when `compliance_level ∈ {gxp, iso17025, sox}`.
- **Cost-attribution metadata** — every `model-gateway` request includes `metadata: {tenant_id, workflow_id, agent_role, criticality}` (LiteLLM first-class support). Enables per-tenant Prometheus dashboards and future budget enforcement.
- **Approval policy referenced as artefact** — state machine transitions are validated against an `approval_policy` artefact resolved per `(tenant, workflow_type)`. D1 ships a single hardcoded policy named `default_v1` but the lookup mechanism is in place.

## Services (1 new)

| Service | Image | Port | License | Role |
|---|---|---|---|---|
| agent-workers | locally built from `services/agent-workers/` | (no HTTP) | (project) | Temporal worker + LangGraph |

artefact-service already ships in D0.5; D1 only consumes it as an HTTP client.

agent-workers has no HTTP — it connects to Temporal as a worker. Its smoke test is to start a workflow via `start_workflow.py` and verify completion + artefact creation in the existing artefact-service.

## What D1 consumes from D0.5 (no new substrate work)

D0.5 already provides every persistence + governance primitive D1 needs. D1 calls them as a regular HTTP client.

**D0.5 surfaces D1 calls:**
- `POST /artefacts` (create requirement) — with `X-Tenant-ID`, URN actor `urn:qa-aqa:agent:test_designer:v1`, idempotency key derived from workflow_id
- `POST /artefacts/bulk` (create test cases) — same auth/headers
- `POST /artefacts/{id}/transition` (move state forward when criticality demands)
- `GET /artefacts?corpus=docs` (verify seed ingestion picked up project docs)
- `GET /policies/approval/test_case` (resolve transitions during workflow)
- `GET /artefacts?type=critique_policy` (load critique rubric for the critic node)

D1 does **not** touch the database directly. The artefact-service contract from D0.5 is treated as a stable interface; if D1 needs new behaviour (e.g. compliance signing), it requires a D0.x sub-project.

Skipped sections in this spec (now in D0.5): artefact endpoints, Postgres schema, RLS, URN grammar, state transitions, idempotency contract, attestation field shape, approval_policy lookup mechanism. See [D0.5 spec](2026-06-25-sub-project-d0_5-compliance-substrate-design.md).
## agent-workers shape

`services/agent-workers/` contains:

```
src/
    worker.py            # Long-running Temporal worker
    registry.py          # Workflow registry — {(tenant, workflow_type): WorkflowClass}
    workflow.py          # @workflow.defn DesignTestsWorkflow
    activities.py        # create_artefact, bulk_create_artefacts, run_test_designer, ingest_seed_docs
    agent.py             # LangGraph state graph for test_designer
    critic.py            # Actor-Critic node (cloud-only, gated on criticality)
    urn.py               # URN parse/validate helpers
    attestation.py       # Build attestation object from a workflow execution
    start_workflow.py    # CLI helper
seed/
    critique_policy.test_case.v1.yaml    # bootstrap critic rubric
    approval_policy.default_v1.yaml      # bootstrap approval policy
```

On startup, agent-workers performs **one-time idempotent seeding**:
1. POST the two seed YAMLs as `critique_policy` and `approval_policy` artefacts (Idempotency-Key derived from yaml sha).
2. Ingest `docs/superpowers/specs/*.md` into rag-service under `corpus=docs` (skipped if the docs already indexed by hash).

### LangGraph state graph (`agent.py`)

```python
class DesignerState(TypedDict, total=False):
    requirement: dict          # input
    criticality: str           # low|medium|high|safety_critical
    similar_cases: list        # from rag-service corpus=test_cases
    relevant_docs: list        # from rag-service corpus=docs (specs/ADRs/contracts)
    draft_cases: list          # from LLM
    errors: list[str]          # schema validation errors
    critique_findings: list    # actor-critic findings
    retry_with_cloud: bool
    final_cases: list          # validated + critiqued, ready to persist
    rag_retrieval_ids: list    # captured for attestation
    prompt_hashes: list        # captured for attestation

# Output schema: test_case.payload is Robot Framework / Playwright consumable:
#   { id, title, steps: [{library, keyword, args}], expected_result,
#     traceability_to_requirement, priority, tags }

# Format adherence (defense in depth):
#  1) Ollama JSON mode via LiteLLM response_format={"type":"json_object"}
#  2) On schema failure: retry once on same model
#  3) On second failure: retry on chat-prod (cloud). If no cloud key, raise
#     SchemaValidationError (non-retryable).

# Activity heartbeat: every node calls activity.heartbeat() on entry.
# heartbeat_timeout=30s, so a dead worker fails fast (not at 10-min cap).

# nodes:
#   fetch_context  -> rag-service /search (corpus=docs)  -- system constraints
#                  -> rag-service /search (corpus=test_cases) -- similar past cases
#                  -> RRF merge -> append doc + case ids to rag_retrieval_ids
#                  Query payload: title + acceptance_criteria + tags + parent_context
#   policy_check   -> policy-svc /authorize with actor=urn:qa-aqa:agent:test_designer:v1
#                  -> Raises PolicyDeniedError (non-retryable) on deny
#   generate       -> model-gateway POST /v1/chat/completions
#                  -> metadata={tenant_id, workflow_id, agent_role, criticality}
#                  -> model=chat-dev or chat-prod (retry_with_cloud)
#                  -> append prompt sha to prompt_hashes
#   validate       -> JSON schema check (Pydantic). Catches shape errors.
#   critique       -> CONDITIONAL on criticality:
#                       low            -> SKIP critique entirely
#                       medium         -> OPA rubric only (deterministic, no LLM)
#                       high           -> OPA + LLM critic on chat-prod (cloud forced)
#                       safety_critical -> OPA + LLM critic + mandatory human-review
#                                          (sets state=in_review_safety on persist)
#                  -> Uses critique_policy artefact for the rubric (versioned, not prompt prose)
#                  -> Appends critique_findings (revision suggestions per case)
# edges:
#   fetch_context -> policy_check
#   policy_check  -> generate          (if allow; else END with PolicyDeniedError)
#   generate      -> validate
#   validate      -> generate          (1st schema failure, same model)
#                 -> generate          (2nd schema failure, retry_with_cloud=True)
#                 -> END               (3rd schema failure: SchemaValidationError)
#                 -> critique          (no errors AND criticality > low)
#                 -> END (final_cases) (no errors AND criticality == low)
#   critique      -> END (final_cases) (no findings)
#                 -> generate          (findings: amend prompt with revisions, single retry)
```

### Temporal workflow shape (`workflow.py`)

```python
NON_RETRYABLE = [
    "PolicyDeniedError",
    "SchemaValidationError",
    "ArtefactConflictError",
    "TenantNotFoundError",
    "BudgetExceededError",       # cost-attribution guard (D1.1 enforces)
]

# Workflow registry — keyed by (tenant_id, workflow_type). registry.py owns it.
# Lookup: registry.lookup(tenant_id, workflow_type) -> WorkflowClass

@workflow.defn
class DesignTestsWorkflow:
    @workflow.run
    async def run(self, req: dict) -> dict:
        wf_id = workflow.info().workflow_id
        # Tenant is propagated via workflow_id prefix: "<tenant>:design-tests:<uuid>"
        tenant_id = wf_id.split(":", 1)[0]
        criticality = req.get("criticality", "low")
        compliance_level = req.get("compliance_level", "none")
        agent_urn = "urn:qa-aqa:agent:test_designer:v1"

        # 1) Create the requirement artefact — deterministic id keyed on workflow_id.
        req_id = await workflow.execute_activity(
            create_artefact_activity,
            args=[{"id": f"requirement:{wf_id}",
                   "tenant_id": tenant_id,
                   "type": "requirement",
                   "payload": req,
                   "workflow_id": wf_id,
                   "actor": agent_urn,
                   "compliance_level": compliance_level,
                   "idempotency_key": f"req:{wf_id}"}],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                maximum_attempts=3,
                non_retryable_error_types=NON_RETRYABLE,
            ),
        )

        # 2) Run LangGraph agent with heartbeat. Returns cases + attestation fragments.
        agent_result = await workflow.execute_activity(
            run_test_designer_activity,
            args=[{
                "requirement": req,
                "criticality": criticality,
                "tenant_id": tenant_id,
                "workflow_id": wf_id,
                "agent_urn": agent_urn,
                "parent_id": req_id,
            }],
            start_to_close_timeout=timedelta(minutes=10),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                maximum_attempts=2,
                non_retryable_error_types=NON_RETRYABLE,
            ),
        )
        cases = agent_result["cases"]
        attestation = agent_result["attestation"] if compliance_level != "none" else {}

        # 3) Bulk persist — single activity event, single network call, single DB tx.
        items = [
            {"id": f"test_case:{wf_id}:{i}",
             "tenant_id": tenant_id,
             "type": "test_case",
             "payload": case,
             "parent_id": req_id,
             "workflow_id": wf_id,
             "actor": agent_urn,
             "compliance_level": compliance_level,
             "attestation": attestation,
             "idempotency_key": f"tc:{wf_id}:{i}"}
            for i, case in enumerate(cases)
        ]
        bulk_result = await workflow.execute_activity(
            bulk_create_artefacts_activity,
            args=[items],
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(
                maximum_attempts=3,
                non_retryable_error_types=NON_RETRYABLE,
            ),
        )
        return {
            "tenant_id": tenant_id,
            "requirement_id": req_id,
            "test_case_ids": [x["id"] for x in bulk_result],
            "attestation": attestation,
        }
```

**Why bulk?** Sequential per-case activities would produce 2N history events and N round-trips. Bulk = exactly 2 events and 1 round-trip regardless of N.

**Why deterministic ids + per-tenant idempotency keys?** Temporal replays activities (worker crash, timeout, retry). Deterministic ids + `INSERT ON CONFLICT DO NOTHING` make retries safe — existing row returned, no version bump, no audit row. Per-tenant key namespace prevents cross-tenant collision.

**Why tenant prefix in workflow_id?** Lets Temporal-level metrics, the workflow registry, and downstream activities all derive tenancy from a single source without needing a side-channel. Validated by registry on workflow start.

**Activity inventory** (`activities.py`):
- `create_artefact_activity` — POST `/artefacts`, includes `X-Tenant-ID` header
- `bulk_create_artefacts_activity` — POST `/artefacts/bulk`
- `run_test_designer_activity` — runs LangGraph; emits `activity.heartbeat()` per node; builds attestation; raises `PolicyDeniedError` / `SchemaValidationError` non-retryable
- `ingest_seed_docs_activity` — one-shot on worker boot; ingests `docs/superpowers/specs/*.md` into rag-service `corpus=docs`

## Updates to `infra.sh` (D1 — agent-workers only)

artefact-service was added to `infra.sh` by D0.5. D1 only appends agent-workers.

| Section | Change |
|---|---|
| 1 Variables | New `IMG_AGENT_WORKERS="qa-aqa/agent-workers:dev"`. No new ports. |
| 5a build_custom_images | Loop covers `rag`, `policy`, `artefact` (D0.5), and `agent-workers` (D1) |
| 5 compose | Append `agent-workers` (no port, depends on temporal + model-gateway + rag-service + policy-svc + artefact-service all healthy) |
| 6 wait_healthy | Add `agent-workers` |
| 8 smoke | 12 new tests (agent-workers + workflow + critic + bulk + idempotency + cost + attestation) |
| Endpoint summary | No change — agent-workers has no HTTP |

## Smoke tests for D1 (agent layer only — D0.5 covers substrate)

| # | Test | Verifies |
|---|---|---|
| 1 | `podman logs agent-workers` shows "Worker started, polling 'test-design' queue" within 30s of compose up | worker boots + Temporal connection |
| 2 | `podman logs agent-workers` shows "Seed: N spec docs ingested into corpus=docs" once | startup seed step is idempotent and runs |
| 3 | `curl '/artefacts?corpus=docs'` (D0.5) returns ≥3 rows after seed | seed populated rag corpus + the artefacts trail recorded |
| 4 | `podman exec agent-workers python -m src.start_workflow design-tests '{"id":"R-001","title":"login","acceptance_criteria":["auth ok","auth fail returns 401"],"criticality":"low"}'` → returns `workflow_id` with `default:design-tests:` prefix | workflow registry lookup + tenant propagation via workflow_id |
| 5 | After workflow #4 completion: `curl '/artefacts?type=test_case&workflow_id=<id>'` returns ≥1 case; `payload.steps[0]` has `{library, keyword, args}` keys | Robot-Framework-consumable shape; full e2e RAG + policy + LLM + persistence |
| 6 | Same workflow_id triggered twice (deliberate retry) → artefacts unchanged, `artefact_history` count unchanged | idempotency-on-retry holds at the workflow level |
| 7 | `temporal workflow show -w <id>` shows exactly **3 activity scheduled events** for any N>0 cases | bulk activity discipline holds |
| 8 | `temporal workflow show -w <id>` shows heartbeats from the agent activity (≥1 every ~10s) | heartbeat instrumentation present |
| 9 | Workflow with `criticality=high` and **no** `ANTHROPIC_API_KEY` → result includes `warning: critic_skipped_no_cloud_key` | critic cloud-only constraint enforced |
| 10 | Workflow with `criticality=high` and key set → artefacts include `metadata.critique_findings_count` | critic ran and recorded findings |
| 11 | `podman logs model-gateway` (within 60s of workflow #4) shows recent request with `metadata.tenant_id=default` + `metadata.workflow_id` + `metadata.agent_role=test_designer` | cost-attribution wiring active |
| 12 | Workflow with `compliance_level=gxp` produces test_case artefacts whose `attestation` JSON is non-empty (`model_fingerprints`, `prompt_hashes`, `rag_retrieval_ids`, `output_hash` all present) | attestation populated only when compliance demands |

Tests 5, 6, 7 are the **headline wiring tests**: they prove the full agent loop, the bulk discipline, and idempotency-on-retry all hold simultaneously.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| llama3.2:3b too weak to produce valid JSON test cases | Three-layer defense: (1) Ollama JSON mode via `response_format=json_object`; (2) one retry on dev model; (3) one retry on cloud (`chat-prod`, requires ANTHROPIC_API_KEY). If still failing → `SchemaValidationError` (non-retryable, workflow fails fast). |
| Temporal worker connection flakes on first start | worker.py retries connect 10× with exponential backoff |
| LangGraph + langchain version conflicts | Pin: `langgraph==0.2.50`, `langchain-core==0.3.15` |
| Long agent activity looks dead to Temporal | `activity.heartbeat()` at each LangGraph node entry; `heartbeat_timeout=30s` so dead workers fail fast (not at 10-min start-to-close) |
| Retry policy wastes compute on deterministic failures | `non_retryable_error_types=["PolicyDeniedError","SchemaValidationError","ArtefactConflictError"]` on every activity |
| Workflow retry creates duplicate artefacts + corrupts audit log | Deterministic ids keyed on `workflow_id` + `Idempotency-Key` header + `INSERT ON CONFLICT DO NOTHING` (returns existing row, no version bump, no audit row) |
| N sequential `create_artefact` activities bloat history | Single `bulk_create_artefacts_activity` → 2 events + 1 RTT regardless of N |

## Acceptance

- 17 containers running (was 16 after D0.5; +agent-workers)
- 12 new smoke tests green (D0.5's 10 + D1's 12 = 22 net new across the D-tier)
- `podman logs agent-workers` shows "Worker started, polling 'test-design' queue" and "Seed: N spec docs ingested into corpus=docs"
- A workflow can be started and produces ≥1 `test_case` artefact, persisted via artefact-service (D0.5)
- **Workflow retry**: replays activity but produces no duplicate artefacts and no spurious audit-log rows (proven against D0.5's idempotency contract)
- **Bulk activity discipline**: workflow history shows exactly 3 activity events for any N>0 cases
- **Heartbeats visible** in `temporal workflow show` output
- **Robot Framework consumable**: test_case payload has `steps[].{library,keyword,args}` from day 1
- **Cost-attribution wired**: model-gateway request logs include `tenant_id`, `workflow_id`, `agent_role`, `criticality`
- **Attestation populated** when `compliance_level != "none"`; empty otherwise
- **Critic is asymmetric**: never runs on dev model; either chat-prod or skipped with warning
- D0.5 substrate guarantees (RLS, URN, idempotency, policy lookup) continue to pass — D1 does not regress them

## Out of scope (D1.1+)

- More agent roles (executor, reporter, defect_triager)
- More artefact types (defect, evidence, report, system_contract)
- Per-tenant prompt packs and per-factory workflow overrides (registry shape is in D1; population is in D1.1)
- Budget enforcement (LiteLLM accepts the metadata in D1; budget rejection in D1.1)
- Configurable approval policy beyond `default_v1` (lookup mechanism in D1; tenant-override registry in D1.1)
- Human approval UI for `safety_critical` (the workflow sets state correctly in D1; the UI is D3)
- Quorum-based approvals + escalation timers
- OpenAPI / API-contract corpus ingestion (the multi-corpus retrieval mechanism is in D1; this specific corpus is later)
- gRPC clients
- Cross-tenant federation for shared corpus (e.g., industry-standard test patterns)
- Compliance attestation cryptographic signing (D1 captures the hashes; signing happens in D1.1)
