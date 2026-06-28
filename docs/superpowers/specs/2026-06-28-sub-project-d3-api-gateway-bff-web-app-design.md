# Sub-project D3 — api-gateway + BFF + React/Vite web app

**Date:** 2026-06-28
**Scope:** First user-facing surface for the QA/AQA platform. Three views: **reviewer** (artefact + workflow drill-in + state transitions), **designer** (start design-tests workflows), **monitor** (live executor status + screenshot viewer). Sits behind APISIX gateway + FastAPI BFF.
**Depends on:** Sub-projects 0+A+B+C+D0.5+D1+D1.1+D1.2+D1.3 committed at `b6d0a48`. 37 prior smoke tests must pass.

## Decomposition into shippable tiers

| Tier | Ships | Smoke must pass before next | Effort |
|---|---|---|---|
| **D3a** | APISIX + BFF + auth + React app shell + **reviewer view** | Reviewer can list workflows, view artefacts, transition state | Medium-Large |
| **D3b** | **Designer wizard** view layered on D3a | Designer can submit requirement, watch workflow run, approve cases | Medium |
| **D3c** | **Executor monitor** view + MinIO presigned-URL pipeline for screenshots | Live workflow status visible; sandbox screenshots viewable in browser | Medium |

Each tier commits separately and adds smoke tests cumulatively.

## Locked decisions

### Architecture
- **Gateway:** APISIX 3.10 LTS (Apache 2.0). Brings its own admin UI + Etcd, declared as the front door for all browser traffic.
- **BFF:** FastAPI (matches the rest of the platform). Owns: auth, session, MinIO presigned URL generation, Temporal SDK queries, aggregation/shape for the UI.
- **Web app:** React 18 + Vite 5 + TypeScript + TanStack Query + React Router + Tailwind + shadcn/ui (Radix UI + Tailwind components). All MIT.
- **Auth model (v1):** dev JWT issued by BFF. Bcrypt-hashed user list seeded from `services/bff/seed/users.yaml`. JWT carries `sub`, `email`, `role`. Tokens last 12h. Refresh tokens land in D3.1 alongside real OIDC (deferred to a later sub-project; Keycloak integration is the natural follow-up).
- **Roles (v1):** `viewer`, `reviewer`, `admin`. URN form: `urn:qa-aqa:user:<email-slug>` (matches D0.5 actor grammar).
- **MinIO access from browser:** BFF generates presigned GET URLs server-side (S3 SDK signs locally; no MinIO admin creds in the browser). URLs expire in 5 min.
- **Temporal access from BFF:** read-only via Temporal Python SDK client. Workflow status + activity history are surfaced; starting workflows uses BFF wrappers (which write the input artefact + then `client.start_workflow(...)` — same as agent-workers' CLI helper but driven by HTTP).

### Service inventory (3 new + 1 frontend bundle)

| Service | Image | Port (host) | License | Role |
|---|---|---|---|---|
| **apisix** | `apache/apisix:3.10.0-debian` | 9180 (admin), 9080 (proxy) | Apache 2.0 | Front gateway |
| **apisix-etcd** | `bitnami/etcd:3.5.16` | (internal) | Apache 2.0 | APISIX config store |
| **bff** | locally built from `services/bff/` | 8005 | (project) | FastAPI BFF |
| **web-app** | locally built from `clients/web/` (Vite build → nginx static serve) | served via APISIX route | (project) | React static bundle |

All routing through APISIX:
- `/api/*` → `bff:8005`
- `/` → `web-app` (static)
- `/temporal-ui/*` → existing Temporal UI (admin only)
- `/minio-console/*` → existing MinIO web (admin only)

## D3a — foundation + reviewer view

### BFF endpoints (D3a)

**All `/api/*` paths except `/api/auth/login` require a Bearer JWT.** Failed auth = 401. Forbidden role = 403.

| Method | Path | Body / params | Behaviour | Role |
|---|---|---|---|---|
| POST | `/api/auth/login` | `{email, password}` | Returns `{access_token, user: {email, role}}` if creds match | any |
| GET | `/api/me` | — | Returns the JWT's claims | viewer+ |
| GET | `/api/workflows` | `?type=&status=&limit=&offset=` | Lists Temporal workflows (or proxy to artefact-service grouped by workflow_id). Pagination. | viewer+ |
| GET | `/api/workflows/{id}` | — | Returns workflow + its artefacts (requirement → test_cases → execution_results, joined) | viewer+ |
| GET | `/api/artefacts/{id}` | — | Returns the artefact (with `X-Tenant-ID` set by BFF from JWT) | viewer+ |
| GET | `/api/artefacts/{id}/history` | — | Returns history | viewer+ |
| POST | `/api/artefacts/{id}/transition` | `{to_state}` | BFF stamps `actor` from JWT, forwards to artefact-service `/artefacts/{id}/transition` | reviewer+ |
| GET | `/api/policies/approval/{type}` | — | Proxies artefact-service | viewer+ |

### Web app (D3a)

```
clients/web/
├── Containerfile           multi-stage: node:20 → nginx:1.27-alpine
├── package.json            React 18, Vite 5, TS, tanstack/react-query, react-router-dom, tailwindcss, shadcn/ui deps
├── vite.config.ts          dev proxy points /api → bff:8005 for local dev outside the stack
├── nginx.conf              serves built bundle on :80; SPA fallback to /index.html
├── tailwind.config.ts
├── postcss.config.js
├── tsconfig.json
└── src/
    ├── main.tsx
    ├── App.tsx             routes: /login, /workflows, /workflows/:id, /
    ├── lib/
    │   ├── api.ts          fetch wrapper + token storage
    │   ├── auth.tsx        AuthProvider context + useAuth()
    │   └── queries.ts      TanStack Query hooks (useWorkflows, useArtefact, useHistory)
    ├── pages/
    │   ├── LoginPage.tsx
    │   ├── WorkflowsListPage.tsx       table of recent workflows
    │   └── WorkflowDetailPage.tsx      requirement card + test_cases list + transitions
    ├── components/
    │   ├── Layout.tsx                  top bar with user, sidebar
    │   ├── ArtefactCard.tsx            renders one artefact (any type)
    │   ├── StateTransitionMenu.tsx     transition picker per artefact
    │   └── ui/                         shadcn/ui generated components
    └── styles/index.css
```

**Login flow:** `POST /api/auth/login` → store JWT in `localStorage` + memory → set Authorization header on all subsequent fetches → redirect to `/workflows`.

**Reviewer flow:** Dashboard shows recent workflows (poll every 10s via TanStack). Click row → detail page shows requirement + test_cases + executions (folded by mode). Each test_case has a transition menu (`draft → in_review → approved`) that's gated by role.

### Smoke tests for D3a (5 new)

| # | Test | Verifies |
|---|---|---|
| 1 | `curl http://localhost:9080/api/me` → HTTP 401 (no auth) | Gateway route up + BFF enforces auth |
| 2 | `POST /api/auth/login {seed_user, seed_password}` → 200 + token | Login works |
| 3 | `GET /api/workflows` with token → 200, has at least the smoke-default-tenant workflow ids from D1 | Wiring through APISIX → BFF → artefact-service holds |
| 4 | `POST /api/artefacts/{id}/transition` to `in_review` → 200, then GET history shows the transition with actor = JWT email URN | Auth-driven actor stamping works |
| 5 | `curl http://localhost:9080/` returns HTML containing `<title>QA/AQA</title>` | Static web bundle served by APISIX |

## D3b — designer wizard

Layered on top of D3a's shell.

### BFF endpoints (D3b)

| Method | Path | Body | Behaviour | Role |
|---|---|---|---|---|
| POST | `/api/workflows/design-tests` | `{requirement: {id, title, acceptance_criteria, ...}, criticality}` | Validates input, calls Temporal SDK `start_workflow(DesignTestsWorkflow, ...)`, returns `{workflow_id}` (does not wait) | reviewer+ |
| GET | `/api/workflows/{id}/status` | — | Returns Temporal workflow execution status (RUNNING / COMPLETED / FAILED) + result if done | viewer+ |

### Web app additions (D3b)

```
src/pages/
├── DesignerPage.tsx        form: title, acceptance_criteria array, criticality
└── WorkflowRunPage.tsx     polls /api/workflows/{id}/status, renders live progress + final test_cases
```

### Smoke tests for D3b (3 new)

| # | Test | Verifies |
|---|---|---|
| 1 | `POST /api/workflows/design-tests {...}` with reviewer JWT → 200 + workflow_id starts with `default:design-tests:` | BFF connects to Temporal + workflow started |
| 2 | Poll `GET /api/workflows/{id}/status` until status=COMPLETED (timeout 4 min) | Live status query works |
| 3 | After completion: `GET /api/workflows/{id}` returns the test_cases with parent_id pointing at the requirement | Same data as design-tests CLI suite produces |

## D3c — executor monitor + media

Layered on D3a + D3b.

### BFF endpoints (D3c)

| Method | Path | Body | Behaviour | Role |
|---|---|---|---|---|
| POST | `/api/workflows/execute-tests` | `{test_case_ids, mode, target_url?, allowed_urls?, sandbox_timeout_seconds?}` | Starts ExecuteTestsWorkflow | reviewer+ |
| GET | `/api/media/presign` | `?key=executions/...` | Returns a presigned MinIO GET URL valid 5 min, with tenant scope check (key path must start with `executions/<jwt_tenant>/`) | viewer+ |
| GET | `/api/workflows/{id}/timeline` | — | Aggregated activity events from Temporal history (event id, type, timestamp, optional error) | viewer+ |

### Web app additions (D3c)

```
src/pages/
├── MonitorPage.tsx                 list of running + recent execute-tests workflows; row = workflow with status
└── ExecutionDetailPage.tsx         tab-bar per execution_result: status, screenshots (presigned), console log
```

Screenshots render via `<img src={presignedUrl}>` (URL fetched on render); video same.

### Smoke tests for D3c (4 new)

| # | Test | Verifies |
|---|---|---|
| 1 | `POST /api/workflows/execute-tests` with `mode=playwright_sandbox`, `allowed_urls=["https://example.com/*"]` → 200 + workflow_id starts with `default:execute-tests:` | BFF starts ExecuteTestsWorkflow with allowlist |
| 2 | After workflow completes: `GET /api/workflows/{id}` returns execution_result artefacts with `payload.screenshots` populated | End-to-end |
| 3 | `GET /api/media/presign?key=executions/default/...png` (key from #2) → 200 + URL; HEAD on returned URL → 200 (object exists) | Presigning + MinIO accessibility |
| 4 | `GET /api/media/presign?key=executions/another-tenant/...` → 403 (tenant scope check) | Cross-tenant media leak prevented |

## Architecture diagram

```
Browser (React+Vite bundle)
    │
    │  https://localhost:9080
    ▼
┌─────────────────┐
│  APISIX 9080    │  Routes:  /         -> web-app static
│                 │           /api/*    -> bff:8005
│                 │           /temporal-ui/*    admin-only
│                 │           /minio-console/*  admin-only
└────┬──────┬─────┘
     │      │
     ▼      ▼
┌──────────┐  ┌──────────┐
│ web-app  │  │  BFF     │  JWT issuance + verification
│ (static) │  │  :8005   │  Temporal SDK reads/writes
│          │  │          │  S3 SDK for MinIO presigns
└──────────┘  └─┬──┬──┬──┘
                │  │  └────▶ artefact-service (X-Tenant-ID from JWT)
                │  └───────▶ Temporal :7233 (read history, start workflows)
                └──────────▶ MinIO (presigned URL signing)
```

## Updates to `infra.sh`

| Section | Change |
|---|---|
| 1 Variables | New: `IMG_APISIX`, `IMG_ETCD`, `IMG_BFF`, `IMG_WEB`, `PORT_APISIX_PROXY=9080`, `PORT_APISIX_ADMIN=9180`, `PORT_BFF=8005`. |
| 5 compose | Adds `apisix-etcd`, `apisix`, `bff`, `web-app`. Volume for APISIX config and an `etcd-data` volume. `bff` depends on `artefact-service` healthy + `temporal` healthy + `minio` healthy. |
| 5a build_custom_images | Loop covers BFF + web-app (and unchanged: rag, policy, artefact, agent-workers, runner, sandbox-image, proxy). 9 services total. |
| 5b configure_apisix | New step: after APISIX starts, POST routes config via the admin API. Idempotent on hash. |
| 6 wait_healthy | Add apisix, bff, web-app. |
| 8 smoke | 5 (D3a) + 3 (D3b) + 4 (D3c) = 12 new tests across the three tiers. Each tier's smoke runs only after that tier is built. |
| Endpoint summary | APISIX URL + BFF + Web URL. |

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| APISIX + etcd are 2 new long-running services with their own quirks | Pin to LTS versions. apisix admin API has stable docs. |
| JWT secret stored in env → leaks if dev env compromised | Generated random hex on first run; persisted in `.env`. Real prod story uses a KMS. |
| Temporal SDK in BFF + agent-workers competing → connection limits | Both are read/write clients; Temporal handles concurrency. Connection pool size configurable. |
| MinIO presigned URLs let browser fetch any object if path-traversal | BFF validates `key.startswith("executions/")` + key contains the JWT's tenant. Otherwise 403. |
| Vite dev mode vs prod build are different (CORS, hot reload) | Inside-stack: only the prod bundle served by nginx. `vite dev` is for laptop work outside the stack. |
| Web-app build is slow (npm install + vite build) | Cached layers in Containerfile. First-time build ~3-5 min. |
| APISIX config drift between manual changes and infra.sh's bootstrap | Bootstrap step writes config; admin UI is read-only by convention. Documented. |

## Acceptance per tier

### D3a
- 23 containers running (+apisix, +apisix-etcd, +bff, +web-app)
- 42 smoke tests green (was 37; +5 D3a tests)
- `https://localhost:9080/login` page loads; login with seeded user works
- Reviewer can transition a test_case state from the UI; history shows the actor URN

### D3b
- Same containers as D3a
- 45 smoke tests green (+3 D3b tests)
- Designer flow runs a real design-tests workflow from the UI; results appear in the same UI

### D3c
- Same containers as D3b
- 49 smoke tests green (+4 D3c tests)
- Monitor flow displays a running execute-tests workflow; screenshot from MinIO renders in the browser via presigned URL

## Out of scope (D3.1+ and beyond)

- Real OIDC (Keycloak / Auth0 / WorkOS) — dev JWT only in D3
- TLS on the APISIX listener — port 9080 stays HTTP for the dev demo
- Role-based granular permissions beyond viewer/reviewer/admin
- Multi-tenant UI (tenant switcher) — implicit `default` tenant only
- Workflow cancellation / signal sending from UI
- Real-time updates via WebSocket — TanStack polling only in D3
- Mobile responsiveness — desktop-first; mobile pass later
- Accessibility audit pass beyond shadcn/ui defaults
- Internationalization
- Server-side rendering / static generation
- Analytics / telemetry instrumentation in the web app (will appear on the Grafana dashboard when added)
