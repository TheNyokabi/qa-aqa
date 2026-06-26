# Sub-project D1.2 — ephemeral sandbox containers + egress-isolated network

**Date:** 2026-06-25
**Scope:** Replaces the v1 inline Playwright runner (D1.1) with **per-`/runs` ephemeral container spawning** on a **separate `sandbox-egress` network** that has no route to internal QA/AQA services. Same external HTTP contract — `POST /runs` keeps returning the same JSON shape. Net effect: a malicious or runaway test case can no longer touch artefact-service, model-gateway, postgres, or any internal endpoint.
**Depends on:** Sub-projects 0+A+B+C+D0.5+D1+D1.1 committed at `a888144`. 31 prior smoke tests must pass.

## Locked decisions

### Carried from D1.1
- runner-service is still the entry point. External contract (`POST /runs` → `execution_result` payload) is byte-identical to v1.
- MinIO remains the storage backend; same bucket, same key prefix.
- Same supported Playwright keyword set (`goto`, `click`, `fill`, …).

### New for D1.2
- **Ephemeral sandbox per request.** runner-service spawns a fresh container for each `/runs` call, waits for exit, captures stdout + files, removes container.
- **Sandbox image:** `mcr.microsoft.com/playwright/python:v1.48.0-jammy` (same as runner-service's base; pre-pulled). Sandbox just executes the generated Python script in a clean environment.
- **Network isolation via separate Podman network `sandbox-egress`.**
  - `sandbox-egress` is declared `internal: false` (so the sandbox can reach the public internet for target URLs) but is **not connected to `qa-aqa`** (so the sandbox cannot reach any internal service by name).
  - runner-service itself stays on `qa-aqa` (needs to talk to MinIO). Sandboxes are spawned attached only to `sandbox-egress`.
- **Podman socket bind-mount.** runner-service mounts the in-VM rootless socket at `/run/user/<uid>/podman/podman.sock` → `/run/podman/podman.sock`. This is the trust boundary; documented in the security section.
- **Resource quotas per sandbox container:** `--cpus=2 --memory=2g --pids-limit=200`. Hard-coded for v1.
- **Container-level timeout enforcement.** runner-service kills the sandbox after `timeout_seconds + 10s` (grace period). The container `--rm` flag guarantees cleanup on either normal exit or kill.
- **Per-sandbox tmp dir.** runner-service creates `/tmp/sandbox/<sandbox_id>/` on its own volume; bind-mounts that into the sandbox at `/work`. Sandbox writes screenshots/console there; runner-service reads them out after exit and uploads to MinIO.
- **Sandbox does not inherit MinIO credentials.** All uploads happen *from runner-service*, after the sandbox has exited. Sandbox has no path to leak credentials.
- **Container name pattern:** `qa-aqa-sandbox-<sandbox_id>`. Easy to identify orphans.
- **Orphan reaper on startup.** runner-service deletes any `qa-aqa-sandbox-*` containers found on boot (residual from crashes).
- **Egress proxy / strict allow-list = D1.3.** Deferred. Documented as known gap.

## How the runner-service changes

`services/runner/src/playwright_runner.py` is split into two files:

```
services/runner/
├── src/
│   ├── main.py                   (unchanged — FastAPI surface)
│   ├── storage.py                (unchanged — MinIO upload)
│   ├── script_generator.py       NEW — emits Python script the sandbox runs
│   ├── sandbox_executor.py       NEW — spawns container, waits, captures
│   └── playwright_runner.py      REMOVED (moves into the sandbox image's runtime)
```

### `script_generator.py`

Takes a `test_case` payload + `target_url` + `timeout_seconds`. Emits a self-contained Python file that:
1. Imports `asyncio` + `playwright.async_api`
2. Defines the keyword → Playwright API mapping (subset matching D1.1's supported keywords)
3. Runs all steps, captures screenshots into `/work/screenshots/`, console into `/work/console.log`
4. On any failure, writes a structured result JSON to `/work/result.json`
5. On success, writes the same JSON with `status=pass`

The sandbox container's CMD is `python /work/script.py`. Nothing else. No CLI dispatcher, no FastAPI inside the sandbox.

### `sandbox_executor.py`

Public API:
```python
async def run_sandbox(
    test_case: dict,
    target_url: str | None,
    timeout_seconds: int,
    sandbox_id: str,
) -> dict:
    """Spawn ephemeral container, run, capture, return execution_result payload."""
```

Implementation:
1. Create `/tmp/sandbox/<sandbox_id>/` on runner-service's volume.
2. Generate the Python script via `script_generator.generate_script(...)`. Write to `/tmp/sandbox/<sandbox_id>/script.py`. Create empty `screenshots/` subdir.
3. Shell out:
   ```
   podman --url unix:///run/podman/podman.sock run --rm
       --name qa-aqa-sandbox-<sandbox_id>
       --network sandbox-egress
       --cpus 2 --memory 2g --pids-limit 200
       -v /tmp/sandbox/<sandbox_id>:/work:Z
       -w /work
       mcr.microsoft.com/playwright/python:v1.48.0-jammy
       python /work/script.py
   ```
4. Wait for exit with overall `timeout_seconds + 10` deadline.
5. On timeout: `podman kill qa-aqa-sandbox-<sandbox_id>`; record `status=timeout`.
6. Read `/tmp/sandbox/<sandbox_id>/result.json` if it exists (otherwise synthesize error).
7. Read screenshots/console/video files; upload to MinIO using existing `storage` helpers; collect URLs.
8. Remove `/tmp/sandbox/<sandbox_id>/` recursively.
9. Return the execution_result payload.

## Updates to `infra.sh`

| Section | Change |
|---|---|
| 1 Variables | Add `IMG_PLAYWRIGHT_SANDBOX="mcr.microsoft.com/playwright/python:v1.48.0-jammy"` (same as runner-service base — already pulled, no extra GB). Add `SANDBOX_NETWORK="sandbox-egress"`. |
| 5 compose | (a) Add `sandbox-egress` network definition at top-level networks block. (b) runner-service stays on `qa-aqa` but: bind-mount podman socket, mount `/tmp/sandbox` named volume, set `CONTAINER_HOST=unix:///run/podman/podman.sock` env. (c) Add a new `runner-tmp` named volume. |
| 5b (existing) ollama_preload | Add `podman pull` for the sandbox image once (no-op since it's the runner-service base image, already on disk). |
| 6 wait_healthy | No changes (sandboxes are ephemeral, not waited for). |
| 8 smoke | Replace existing `executor-d1-1-suite` to add an **isolation assertion**: the sandbox-mode run must *fail* to reach `artefact-service:8003` from inside its container. We add one step to the test_case that tries `goto http://artefact-service:8003/health` and expects connection refusal or DNS failure. (Real sandboxes can't resolve internal names.) |

## `sandbox-egress` network definition

```yaml
networks:
  qa-aqa:
    name: qa-aqa
  sandbox-egress:
    name: sandbox-egress
    # internal: false  → can reach public internet for target URLs
    # NOT connected to qa-aqa → cannot reach any internal service by name
```

By default Podman gives every network its own subnet; cross-network reachability requires explicit attachment. Since sandboxes attach only to `sandbox-egress`, they cannot resolve `artefact-service`, `postgres`, `temporal`, etc., and even if they had IPs, no route to them exists.

## runner-service Containerfile update

Add the podman CLI binary. We use the static binary from podman-static to avoid pulling all of apt's dependency tree:

```dockerfile
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends podman uidmap \
 && rm -rf /var/lib/apt/lists/*

# ...rest unchanged...
```

(Trade-off note: adds ~80 MB to the runner-service image. Acceptable for v1.)

## Smoke tests for D1.2

The existing `executor-d1-1-suite` is **extended** rather than replaced:

| # | Test | Verifies |
|---|---|---|
| (existing) | simulate mode produces execution_result with `reasoning`+`status` | unchanged |
| (existing) | scripts/playwright mode produces script_content | unchanged |
| (existing, **modified**) | playwright_sandbox mode produces ≥1 screenshot URL in MinIO + status ∈ {pass, fail} | now backed by a **real ephemeral container** |
| **new** | `podman ps -a --filter name=qa-aqa-sandbox- --format '{{.Names}}'` is empty within 30s after smoke completes | proves `--rm` cleanup |
| **new** | A test_case whose steps include `goto http://artefact-service:8003/health` from playwright_sandbox mode → status ∈ {fail, error} with error_message containing "ERR_NAME_NOT_RESOLVED" or similar | proves egress isolation (sandbox can't see internal services) |
| **new** | `podman network ls` shows `sandbox-egress` exists and is **not connected to qa-aqa** | proves network topology |

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Podman socket bind-mount = full container-runtime privilege | Documented; runner-service code is the trust boundary. D1.3 enforces rootless-only + drops privileges. |
| First-time sandbox container start ~5-10s (image already cached) overhead per test | Acceptable for v1. D1.3 considers warm-pool of pre-spawned sandbox containers. |
| Orphan containers if runner-service crashes mid-run | Orphan reaper on boot (cleans `qa-aqa-sandbox-*`). Plus `--rm` removes container even on crash exit. |
| Sandbox can still reach public internet → could exfiltrate via DNS | True; egress allow-list = D1.3 (forward proxy or DNS filter). |
| Bind-mount path `/run/user/<uid>/podman/podman.sock` differs across podman versions / rootless modes | Probed at runner-service startup; failure → service unhealthy with clear log. |
| Resource quotas (2 CPU, 2 GB) leave little for concurrent sandbox runs on 6 vCPU machine | Workflow semaphore already caps at 3; covers it. |
| Image pull lag for the sandbox image | It's the same image as runner-service base; already on disk after D1.1 install. |
| Network creation might race compose up | infra.sh creates the network with `podman network create` before compose up, idempotent. |
| Sandbox can hit MinIO directly (it's on `qa-aqa` only — but if someone misconfigures…) | MinIO is on `qa-aqa` only. Sandbox on `sandbox-egress` cannot resolve `minio`. |

## Acceptance

- 19 containers running steady-state (same as after D1.1 — sandboxes are ephemeral)
- 34 smoke tests green (was 31; +3 new — cleanup, isolation, network topology)
- A real `podman ps -a` taken anywhere during a sandbox-mode workflow shows a transient `qa-aqa-sandbox-*` container that disappears within 10s of run completion
- A sandbox test_case that tries to reach `http://artefact-service:8003/health` reports `error` (DNS failure)
- D1.1 contract preserved: `runner-service POST /runs` still returns the same payload shape
- All prior 31 smoke tests continue to pass

## Out of scope (D1.3+)

- **Strict egress allow-list** (forward proxy with allowed URLs per workflow)
- **DNS filter** as an alternative to forward proxy
- Warm-pool of pre-spawned sandbox containers (reduce per-test cold-start)
- Multi-browser support (Firefox, WebKit in addition to Chromium)
- Headed mode for visual debugging
- Per-tenant sandbox quotas (concurrent + daily)
- Async runs queue (POST returns 202, GET for status)
- Robot Framework runner (symmetric to Playwright)
- Sandbox-side capability dropping (`--cap-drop=all`, seccomp profile)
