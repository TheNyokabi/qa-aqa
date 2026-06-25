# Sub-project D1.1 — executor agent + execute-tests workflow

**Date:** 2026-06-25
**Scope:** Adds the executor agent role + `ExecuteTestsWorkflow` with three execution modes: `simulate`, `scripts`, `playwright_sandbox`. New services: `runner-service` (Playwright sandbox orchestrator) and `minio` (S3-compatible storage for execution artefacts).
**Depends on:** Sub-projects 0+A+B+C+D0.5+D committed at `088f323`. 28 prior smoke tests must pass.

## Locked decisions

### Carried from D1
- Workflow registry pattern, deterministic ids, bulk activity discipline, idempotency contract, URN actor grammar, RLS substrate, cost-attribution metadata, attestation field.
- LangGraph + Temporal + cloud-only critic.

### New for D1.1
- **Three execution modes:** `simulate` (LLM reasons about outcome), `scripts` (LLM emits `.robot` or `.spec.ts` for human/CI run), `playwright_sandbox` (real browser run in ephemeral container).
- **One workflow type — `ExecuteTestsWorkflow`** — input includes `mode` and `target_url` (for sandbox). Workflow routes to the right activity path based on mode.
- **New artefact type:** `execution_result` with mode-specific payload shape (one schema per mode, discriminated by `mode` field).
- **New URN actor:** `urn:qa-aqa:agent:executor:v1`.
- **Storage:** MinIO (Apache 2.0). Bucket `executions` auto-created. Object keys: `executions/<tenant>/<workflow_id>/<test_case_id>/{screenshots,videos,logs}/...`.
- **runner-service** is a thin FastAPI service that owns sandbox lifecycle. It receives a test_case + target_url, spawns a Playwright container, waits, uploads artefacts to MinIO, returns the result. Synchronous v1 (the calling activity blocks).
- **Sandbox image:** `mcr.microsoft.com/playwright:v1.48.0-jammy` (Apache 2.0 license; cross-platform).
- **Network model v1:** sandbox containers join the `qa-aqa` network. Documented as a known limitation; strict egress isolation is D1.2 work.
- **Parallel execution for sandbox mode:** workflow uses `asyncio.gather` with a semaphore cap of 3 (sandbox containers are heavy). Modes `simulate` + `scripts` run sequentially within one activity batch (small, fast LLM calls).
- **Default timeout per sandbox run:** 120s. Configurable via workflow input `sandbox_timeout_seconds`.

## Services (3 new)

| Service | Image | Port (host) | Volume | License |
|---|---|---|---|---|
| minio | `minio/minio:RELEASE.2024-10-13T13-34-11Z` | 9100 (web), 9101 (S3 API) | `minio-data` | Apache 2.0 (`AGPL-3.0` for newer; we pin this stable Apache release) |
| runner-service | built locally from `services/runner/` | 8004 | none (stateless) | (project) |
| (executor lives inside existing **agent-workers**) | — | — | — | — |

## execute-tests workflow shape

Input:
```json
{
  "test_case_ids": ["test_case:<wf>:0", "test_case:<wf>:1", ...],
  "mode": "simulate" | "scripts" | "playwright_sandbox",
  "target_url": "https://staging.example.com",
  "sandbox_timeout_seconds": 120,
  "criticality": "low"
}
```

The workflow:
1. Resolves each `test_case_id` to a test_case artefact via artefact-service (single bulk-ish activity).
2. For each test_case, runs the executor activity in mode-appropriate parallelism:
   - `simulate` / `scripts`: sequential within a single batch activity (per-case LLM call is < 60s)
   - `playwright_sandbox`: parallel with semaphore cap = 3; each delegates to runner-service
3. Bulk persists `execution_result` artefacts via `bulk_create_artefacts_activity` (one activity event regardless of N).

```python
@workflow.defn
class ExecuteTestsWorkflow:
    @workflow.run
    async def run(self, req: dict) -> dict:
        wf_id = workflow.info().workflow_id
        tenant_id = wf_id.split(":", 1)[0]
        mode = req["mode"]

        # 1) Fetch test_case artefacts (single activity for the batch)
        test_cases = await workflow.execute_activity(
            fetch_artefacts_activity,
            args=[{"tenant_id": tenant_id, "ids": req["test_case_ids"]}],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3, non_retryable_error_types=NON_RETRYABLE),
        )

        # 2) Run executor(s) — branch on mode for parallelism
        if mode == "playwright_sandbox":
            # Parallel; each delegates to runner-service. Heartbeat ticker in activity.
            results = await asyncio.gather(*[
                workflow.execute_activity(
                    run_executor_activity,
                    args=[{**req, "test_case": tc, "tenant_id": tenant_id, "workflow_id": wf_id}],
                    start_to_close_timeout=timedelta(seconds=req.get("sandbox_timeout_seconds", 120) + 60),
                    heartbeat_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=2, non_retryable_error_types=NON_RETRYABLE),
                )
                for tc in test_cases
            ])
        else:
            # Single batch activity that loops cases serially
            results = await workflow.execute_activity(
                run_executor_batch_activity,
                args=[{**req, "test_cases": test_cases, "tenant_id": tenant_id, "workflow_id": wf_id}],
                start_to_close_timeout=timedelta(minutes=30),
                heartbeat_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=2, non_retryable_error_types=NON_RETRYABLE),
            )

        # 3) Bulk persist execution_result artefacts
        items = [_build_artefact_item(wf_id, tenant_id, i, tc, r, req) for i, (tc, r) in enumerate(zip(test_cases, results))]
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
        }
```

## executor LangGraph (in agent-workers)

```python
class ExecutorState(TypedDict, total=False):
    test_case: dict
    mode: str
    target_url: str
    similar_executions: list  # rag corpus=executions, may be empty in v1
    relevant_docs: list       # rag corpus=docs
    output: dict              # mode-specific payload
    errors: list[str]

# nodes:
#   fetch_context  -> rag-service /search (corpus=docs + corpus=executions)
#   policy_check   -> policy-svc /authorize (subject.role=agent, action=execute)
#   dispatch       -> routes to one of:
#       simulate_via_llm     -> model-gateway chat (chat-dev/chat-prod)
#       generate_script      -> model-gateway chat with language-specific prompt
#       run_in_sandbox       -> POST /runs to runner-service
#   validate       -> schema check on mode-specific payload
# edges:
#   fetch_context -> policy_check
#   policy_check  -> dispatch (else PolicyDeniedError)
#   dispatch      -> simulate_via_llm | generate_script | run_in_sandbox (router)
#   *             -> validate -> END | retry (1×)
```

### Mode-specific payload schemas

```json
// simulate
{
  "mode": "simulate",
  "status": "pass" | "fail" | "error",
  "reasoning": "step-by-step analysis...",
  "predicted_failures": ["step 3 likely fails because..."],
  "confidence": 0.0-1.0
}

// scripts
{
  "mode": "scripts",
  "language": "robot" | "playwright",
  "script_content": "...",
  "file_extension": ".robot" | ".spec.ts"
}

// playwright_sandbox
{
  "mode": "playwright_sandbox",
  "status": "pass" | "fail" | "error" | "timeout",
  "duration_ms": 12345,
  "screenshots": ["s3://executions/.../1.png", ...],
  "videos": ["s3://executions/.../trace.webm"],
  "console_log_url": "s3://executions/.../console.log",
  "error_message": "..."  // only for fail/error
}
```

## runner-service contract

Built locally from `services/runner/`. Owns sandbox lifecycle via the Podman socket mounted into its container.

| Method | Path | Behaviour |
|---|---|---|
| GET | `/health` | `{"status":"ok"}` |
| POST | `/runs` | body: `{ test_case, target_url, timeout_seconds, sandbox_id }`. Spawns Playwright container with a generated script derived from the test_case payload, waits up to `timeout_seconds`, uploads screenshots/videos/console to MinIO, tears down the container, returns the full execution_result payload. Synchronous v1. |

### Sandbox lifecycle (per call)
1. Generate Playwright script from `test_case` steps (mechanical translation: `{library:"playwright", keyword:"click", args:[selector]}` → `await page.click(selector)`). For unsupported keywords, write a `test.skip()` block and record a `predicted_failures` entry.
2. Write script + helpers to a tmp dir on the runner-service's volume (which is also bind-mounted into the sandbox).
3. Create sandbox container: `mcr.microsoft.com/playwright:v1.48.0-jammy`, attach to `qa-aqa` network, set timeout, set CPU/RAM limits (2 CPU, 2 GB).
4. Start it; it runs `npx playwright test --reporter=line --output=/results`. Block on container exit or timeout.
5. On exit (or timeout kill): upload `/results/*` to MinIO under `executions/<tenant>/<workflow_id>/<test_case_id>/`.
6. Remove container.
7. Return result.

### How runner-service talks to Podman
- `podman.sock` socket bind-mounted from host into runner-service container (`/run/podman/podman.sock`)
- runner-service uses `podman` Python SDK (`podman==4.9.0`) or just shells out to `podman` CLI bundled into its image
- **v1 simplification:** shell out to `podman` CLI from a script. Cleaner for debugging.

## MinIO config
- Auto-create bucket `executions` on startup via mc CLI in compose `entrypoint` extension
- Default credentials in `.env`: `MINIO_ROOT_USER=minio`, `MINIO_ROOT_PASSWORD=<random hex>`
- runner-service reads creds from env, uses `boto3` client

## Updates to `infra.sh`

| Section | Change |
|---|---|
| 1 Variables | `IMG_MINIO`, `IMG_RUNNER`, `IMG_PLAYWRIGHT` (pulled but not run by compose; used by runner). `PORT_MINIO_S3=9101`, `PORT_MINIO_WEB=9100`, `PORT_RUNNER=8004` |
| 5a build_custom_images | Loop covers existing 4 + runner |
| 5 compose | Add `minio` (bucket-init via init container or runner-service startup), `runner-service` (with podman.sock mount) |
| 5b ollama_preload | Also `podman pull mcr.microsoft.com/playwright:v1.48.0-jammy` once (~1.5 GB) |
| 6 wait_healthy | Add `minio`, `runner-service` |
| .env | Append `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD` (random hex on first run) |
| 8 smoke | 5 new tests covering all three executor modes + runner-service health + MinIO health |
| Endpoint summary | MinIO web UI URL + runner-service URL |

## Smoke tests for D1.1

| # | Test | Verifies |
|---|---|---|
| 1 | `minio /minio/health/live` → 200 | minio up |
| 2 | `runner-service /health` → 200 | runner up |
| 3 | Run `execute-tests` workflow with mode=`simulate`, using test_cases from a prior `design-tests` run → returns ≥1 `execution_result` artefact, mode=`simulate`, has reasoning + status | simulate mode e2e |
| 4 | Run `execute-tests` workflow with mode=`scripts`, language=`playwright` → returns ≥1 artefact with `script_content` containing `await page.` | scripts mode e2e |
| 5 | Run `execute-tests` workflow with mode=`playwright_sandbox`, target_url=`https://example.com` (simple, no auth) → returns ≥1 artefact with `screenshots` (≥1 url) and `status ∈ {pass, fail}` (not error/timeout) | sandbox mode e2e — proves runner-service spawned a real sandbox + uploaded artefacts |
| 6 | Workflow history for sandbox mode shows N parallel `run_executor_activity` events (one per test_case), capped at semaphore=3 in concurrent execution | parallelism discipline |

Tests 3, 4, 5 are the headline e2e tests — they prove the full chain from test_case artefact → executor → execution_result artefact across all three modes.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Spawning containers from a container needs Podman socket access — security risk | Document the trade-off. v1 dev only. Production deployment would use a dedicated runner pool with strict RBAC. |
| Playwright image (~1.5 GB) slow to pull first time | Pre-pulled by `infra.sh` after the existing Ollama models step; idempotent |
| Sandbox container takes 30-60s to cold-start | Heartbeat ticker keeps activity alive; total budget is `timeout_seconds + 60` |
| MinIO Apache vs AGPL licensing | Pin to stable Apache release date; document the rationale in compose |
| Sandbox can reach internal services (no egress isolation) | v1 limitation documented; D1.2 adds Podman network policy |
| Parallel sandbox runs OOM the machine | Semaphore cap=3, CPU 2/RAM 2GB per container = 6 vCPU / 6 GB peak. Machine has 20 GB. |
| Generated Playwright script invalid for complex test cases | Validate node + `test.skip` for unsupported keywords; record `predicted_failures` |
| Cross-mode payload shape conflicts | Discriminated union by `mode` field; Pydantic validates per mode at artefact-service boundary (extend D0.5's CreateArtefactRequest later) |

## Acceptance

- 20 containers running (was 17; +minio, +runner-service, +the executor-emitted sandbox containers are ephemeral and not in steady state)
- 34 smoke tests green (was 28; +6 new D1.1 tests)
- All three execution modes produce visible `execution_result` artefacts with mode-correct payload shapes
- Sandbox mode demonstrably uploads at least one screenshot to MinIO
- Workflow history shows parallel activities for sandbox mode (asyncio.gather)
- `executor` URN actor (`urn:qa-aqa:agent:executor:v1`) recorded in artefacts
- Cost-attribution metadata visible in model-gateway logs for simulate + scripts modes
- D0.5 substrate guarantees continue to pass

## Out of scope (D1.2+)

- **Egress isolation for sandbox** (separate Podman network with strict allow-list) — biggest known v1 limitation
- Async runner queue (multiple workers consuming a runs queue)
- Video transcoding, screenshot diff vs golden
- Failure auto-replay (sandbox flake mitigation)
- Robot Framework runner (analogous to Playwright runner)
- Multi-browser support (Firefox, WebKit)
- Headed mode for visual debugging
- Per-tenant sandbox quotas
