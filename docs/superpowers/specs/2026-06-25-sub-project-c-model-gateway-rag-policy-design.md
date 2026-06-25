# Sub-project C — model-gateway + RAG + policy

**Date:** 2026-06-25
**Scope:** Adds 5 services to the stack: Ollama, LiteLLM, OPA, rag-service, policy-svc. First two app services with our own code (Python/FastAPI in `services/rag/` and `services/policy/`).
**Depends on:** Sub-projects 0+A+B (committed `728f194`) must be up.

## Locked decisions (from brainstorming)

- **Model routing:** LiteLLM as gateway, both local (Ollama) and cloud (Anthropic/OpenAI) backends. Switch via alias.
- **Code layout:** Monorepo. `services/rag/` and `services/policy/` each contain `Containerfile`, `pyproject.toml`, `src/`, `tests/`.
- **RAG pattern:** Hybrid retrieval = pgvector ANN + OpenSearch BM25 → RRF merge. Skip reranker in v1.
- **Embedding model:** `nomic-embed-text` (Ollama, 768-dim, MIT) for dev. `openai/text-embedding-3-small` alias for prod.
- **Chunking:** 512-token fixed with 50 overlap, markdown-aware on headers.
- **Resource budget:** Bump podman-machine RAM 12 GB → 20 GB.

## Service inventory

### New services (5)

| Service | Image | Port (host) | Volume | License |
|---|---|---|---|---|
| ollama | `ollama/ollama:0.3.14` | 11434 | `ollama-data` | MIT |
| model-gateway (LiteLLM) | `ghcr.io/berriai/litellm:main-stable` | 4000 | (config-only) | MIT |
| opa | `openpolicyagent/opa:0.69.0-rootless` | 8181 | `opa-policies` (read-only bundle dir) | Apache 2.0 |
| rag-service | built locally from `services/rag/` | 8001 | (stateless) | (project) |
| policy-svc | built locally from `services/policy/` | 8002 | (stateless) | (project) |

### Internal-only DNS

Services talk to each other by container name on the `qa-aqa` network. No host port required for service-to-service calls.

- `rag-service` → `postgres:5432`, `opensearch:9200`, `model-gateway:4000`
- `policy-svc` → `opa:8181`
- `model-gateway` → `ollama:11434`, plus public cloud endpoints

## LiteLLM config (`litellm-config.yaml`)

Aliases hide the backend choice from callers:

```yaml
model_list:
  - model_name: chat-dev
    litellm_params:
      model: ollama_chat/llama3.2:3b
      api_base: http://ollama:11434

  - model_name: chat-prod
    litellm_params:
      model: anthropic/claude-opus-4-7
      api_key: os.environ/ANTHROPIC_API_KEY

  - model_name: embed-dev
    litellm_params:
      model: ollama/nomic-embed-text
      api_base: http://ollama:11434

  - model_name: embed-prod
    litellm_params:
      model: openai/text-embedding-3-small
      api_key: os.environ/OPENAI_API_KEY

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
```

Callers send `model: chat-dev` and never know which backend handled it.

## rag-service contract

| Method | Path | Behaviour |
|---|---|---|
| GET | `/health` | returns `{"status":"ok"}` |
| POST | `/ingest` | body: `{ "id": str, "text": str, "metadata": {...} }`. Chunks → embeds via model-gateway → writes pgvector row + opensearch doc. Returns `{"chunks": N}`. |
| POST | `/search` | body: `{ "query": str, "k": int=10 }`. Embeds query → pgvector ANN top-k + opensearch BM25 top-k → RRF merge → returns top-k. |

Implementation: FastAPI + asyncpg + opensearch-py + httpx (to call model-gateway). Single file `src/main.py` for v1; refactor when it crosses ~400 lines.

## policy-svc contract

| Method | Path | Behaviour |
|---|---|---|
| GET | `/health` | returns `{"status":"ok"}` |
| POST | `/authorize` | body: `{ "subject": {...}, "action": str, "resource": {...} }`. Forwards to OPA `/v1/data/qa_aqa/authz/allow` and returns its decision. |

OPA loads policies from `policies/` directory bind-mounted into the container. Starter policy: `policies/qa_aqa/authz.rego` with `default allow := false` plus an allow rule for `subject.role == "admin"`.

## Delivery — C1 then C2

Splitting the work for faster iteration:

### C1 — Infra-only additions (no app code)
- Bump podman-machine RAM (rebuild machine OR `podman machine set --memory 20480`)
- Add Ollama, LiteLLM, OPA to compose
- Pre-pull `nomic-embed-text` + `llama3.2:3b` on Ollama start
- Smoke: Ollama version, LiteLLM `/health/liveliness`, LiteLLM embedding call returns 768-d vector, OPA `/health`
- Sub-project C1 spec is THIS doc, sections through "policy-svc contract"

### C2 — Application services (our code)
- Scaffold `services/rag/` and `services/policy/`: Containerfile, pyproject, src/main.py, tests/
- Build images via `podman build` in `infra.sh`
- Add rag-service, policy-svc to compose
- Smoke: rag-service `/health`, ingest a sample doc, search returns it; policy-svc `/health`, authorize sample request

C1 lands when 3 new infra smoke tests pass. C2 lands when the 5-service set is green end-to-end.

## Updates to `infra.sh`

| Section | Change |
|---|---|
| 1 Variables | `MACHINE_MEMORY=20480`, new `IMG_OLLAMA`, `IMG_LITELLM`, `IMG_OPA`, new `PORT_*` for new services, new `OLLAMA_PRELOAD_MODELS=("nomic-embed-text" "llama3.2:3b")` |
| 4 Machine | If existing machine has <20 GB RAM, `podman machine stop && podman machine set --memory 20480 && podman machine start` |
| 5 Configs | Generate `dist/litellm/config.yaml`, `dist/opa/policies/qa_aqa/authz.rego` |
| 5 Compose | Append 3 services (C1) then 2 services (C2) |
| 5 build_custom_images() | C2 only: `podman build -t qa-aqa/rag-service:dev services/rag/`, same for policy |
| 6 .env | Add `LITELLM_MASTER_KEY` (autogenerated random hex), `ANTHROPIC_API_KEY=`, `OPENAI_API_KEY=` |
| 7 up | After compose up, run `podman exec ollama ollama pull <model>` for each preload model |
| 8 Smoke | Add 3 smoke tests for C1, then 2 for C2 |

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Machine RAM bump requires stop/start | Script announces this; data volumes survive `machine set`. Containers must be restarted afterward. |
| Ollama model pull ~3 GB total (slow) | Script reports progress per model; idempotent (skips if already pulled) |
| LiteLLM master key in `.env` | Generated random hex on first run; never overwritten; chmod 600 |
| Custom-image builds slow on Apple Silicon | Multi-stage Containerfiles with pip cache layer; later: add `podman build --layers --cache-from` |
| pgvector dimension mismatch on switch dev↔prod embeddings | Both `embed-dev` (nomic, 768) and `embed-prod` (openai 3-small, 1536) — vector column dimension must be set per index; v1 fixes at 768 (dev) and re-embeds when switching |

## Out of scope (D)

- agent-workers (LangChain + LangGraph)
- connector-svc, output-svc, artefact-service
- api-gateway (Envoy/APISIX) + FastAPI BFF
- web-app (React + Vite)
- reranker (deferred until recall <95%)
- multi-tenant policy (single-tenant `qa_aqa` namespace in OPA for now)

## Acceptance for C1

- `podman machine list` shows 20 GB memory
- `./infra/infra.sh status` lists 13 healthy containers (was 10)
- `./infra/infra.sh smoke` passes 12 tests (was 9)
- `curl -fs http://localhost:11434/api/version` returns Ollama version
- `curl -fs http://localhost:4000/health/liveliness` returns 200
- `curl -s http://localhost:4000/v1/embeddings -H 'Authorization: Bearer $LITELLM_MASTER_KEY' -d '{"model":"embed-dev","input":"hello"}'` returns a 768-d vector
- `curl -fs http://localhost:8181/health` returns 200
