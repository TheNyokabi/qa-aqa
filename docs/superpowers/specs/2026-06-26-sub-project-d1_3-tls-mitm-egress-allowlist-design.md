# Sub-project D1.3 — TLS-intercepting egress proxy with per-workflow URL allow-list

**Date:** 2026-06-26
**Scope:** Adds a TLS-intercepting forward proxy (`proxy-service`) on the `sandbox-egress` network. Every `/runs` call must declare `allowed_urls: [str]` — sandbox traffic is routed through the proxy, which MITMs HTTPS and enforces path-level allow-listing. A sandbox trying to reach a non-allowlisted URL gets HTTP 403 from the proxy; the resulting test result shows status=`fail` with a clear error.
**Depends on:** Sub-projects 0+A+B+C+D0.5+D1+D1.1+D1.2 committed at `f7221a9`. 34 prior smoke tests must pass.

## Locked decisions

### Carried from D1.2
- `sandbox-egress` network exists, no route to `qa-aqa`.
- runner-service spawns ephemeral Playwright containers via the bind-mounted Podman socket.
- All v1 dev-mode security trade-offs (Podman socket as trust boundary, label=disable, etc.) still apply.

### New for D1.3
- **Proxy implementation:** `mitmproxy` (BSD-3 license) with a custom Python addon for per-run allow-list enforcement.
- **Per-run allow-list scope:** array of full URLs at the path level. Wildcards permitted in the path component (e.g. `https://api.example.com/v1/*`).
- **TLS interception:** mitmproxy generates one CA on first start, persisted on a named volume; sandbox image trusts that CA via `SSL_CERT_FILE`. No per-tenant CAs in v1.
- **Allow-list transport:** runner-service writes the per-run config to a path the proxy can read (`/tmp/proxy-allowlists/<sandbox_id>.json`) before spawning the sandbox. The mitmproxy addon reads it on every request via the `X-QA-Sandbox-ID` header that runner-service injects into the sandbox env as `QA_SANDBOX_ID`.
- **Sandbox routing:** sandbox container receives `HTTP_PROXY=http://proxy-service:8080`, `HTTPS_PROXY=http://proxy-service:8080`, `SSL_CERT_FILE=/etc/ssl/certs/qa-aqa-mitmproxy-ca.pem`.
- **CA cert distribution:** baked into the sandbox image at build time (`COPY ca.pem` step). When the CA is regenerated, the sandbox image must be rebuilt — `infra.sh` handles this by hashing `ca.pem` and forcing rebuild on change.
- **Strict default:** if `allowed_urls` is empty/missing on a `/runs` call, ALL outbound traffic is denied. Existing smoke tests are updated to pass `allowed_urls`.
- **Out of scope (D1.4+):** per-tenant CAs, allow-list templates / inheritance, mTLS to the proxy, traffic recording, header-stripping rules.

## Architecture

```
sandbox container               proxy-service                    public internet
─────────────────               ────────────                    ───────────────
HTTPS_PROXY=proxy:8080 ───┐
SSL_CERT_FILE=mitmproxy.crt   │
                              ▼
                         mitmproxy
                            ├── CONNECT api.stripe.com:443
                            │      └── decode + check addon → ALLOW path?
                            │            ├── yes → forward (and re-encode)
                            │            └── no  → return 403 to sandbox
                            └── reads /tmp/proxy-allowlists/<id>.json
                                  keyed by env QA_SANDBOX_ID
```

The mitmproxy container sits on `sandbox-egress` (so sandboxes can reach it) AND has an upstream interface to the public internet. Only the proxy talks to the public internet; sandboxes have no direct route.

## Services (1 new + 1 modified)

| Service | Image | Port | Role |
|---|---|---|---|
| **proxy-service** | `mitmproxy/mitmproxy:11.0.1` + custom addon (built locally as `qa-aqa/proxy-service:dev`) | 8080 (proxy, internal-only) | TLS MITM + per-run allow-list |
| sandbox-image | rebuilt to bake in the mitmproxy CA cert | — | Trusts the QA/AQA CA so MITM'd HTTPS validates |

## proxy-service shape

```
services/proxy/
├── Containerfile
├── pyproject.toml          (mitmproxy + minimal)
└── src/
    ├── addon.py             custom mitmproxy addon: per-request allow-list check
    └── start.sh             wraps `mitmdump -s src/addon.py --listen-port 8080 ...`
```

### `addon.py` shape

```python
"""mitmproxy addon: enforce per-run URL allow-list.

Each incoming HTTP/HTTPS request includes header X-QA-Sandbox-ID set by the
sandbox (we inject it via env that the generated script writes into a default
header for every request). The addon reads /tmp/proxy-allowlists/<id>.json
and matches the request URL against the allow-list patterns. Mismatch → 403.
"""
import fnmatch, json, os, time
from pathlib import Path
from mitmproxy import http

ALLOWLIST_DIR = Path("/tmp/proxy-allowlists")
CACHE: dict[str, dict] = {}
CACHE_TTL = 5.0

def _load(sandbox_id: str) -> dict:
    entry = CACHE.get(sandbox_id)
    now = time.time()
    if entry and now - entry["loaded_at"] < CACHE_TTL:
        return entry
    path = ALLOWLIST_DIR / f"{sandbox_id}.json"
    if not path.exists():
        return {"loaded_at": now, "allow": [], "missing": True}
    cfg = json.loads(path.read_text())
    CACHE[sandbox_id] = {"loaded_at": now, "allow": cfg.get("allowed_urls", []), "missing": False}
    return CACHE[sandbox_id]

def request(flow: http.HTTPFlow) -> None:
    sid = flow.request.headers.get("X-QA-Sandbox-ID", "")
    if not sid:
        flow.response = http.Response.make(403, b"missing sandbox id\n")
        return
    cfg = _load(sid)
    if cfg.get("missing"):
        flow.response = http.Response.make(403, b"no allowlist for sandbox\n")
        return
    url = flow.request.pretty_url
    for pattern in cfg["allow"]:
        if fnmatch.fnmatch(url, pattern):
            return
    flow.response = http.Response.make(403, f"egress denied: {url}\n".encode())
```

### Container layout

- `proxy-service` runs `mitmdump` (mitmproxy's headless mode) with the addon, listening on port 8080.
- CA generated on first start lives at `~/.mitmproxy/mitmproxy-ca-cert.pem` (persisted via the `mitm-ca` named volume).
- runner-service mounts the same volume read-only at `/mitmproxy-ca/` and copies the cert into the sandbox tmp dir each run.

## Sandbox image rebuild

`services/sandbox-image/Containerfile` adds:

```dockerfile
# D1.3: trust the QA/AQA mitmproxy CA so TLS MITM works for sandbox-egress traffic.
# The CA is generated at proxy-service first-start, fetched by infra.sh, and
# saved to services/sandbox-image/qa-aqa-mitmproxy-ca.pem before the build.
COPY qa-aqa-mitmproxy-ca.pem /usr/local/share/ca-certificates/qa-aqa-mitmproxy.crt
RUN update-ca-certificates
```

`infra.sh` orchestrates this:
1. Start proxy-service (creates CA on first start).
2. Copy CA from the `mitm-ca` volume to `services/sandbox-image/qa-aqa-mitmproxy-ca.pem`.
3. Rebuild `qa-aqa/sandbox:dev` only if the CA file's sha changed since the previous build.

## Workflow + runner contract changes

| Component | Change |
|---|---|
| `ExecuteTestsWorkflow` request schema | New optional `allowed_urls: list[str]` field. Empty/missing → strict deny default. |
| Activity → `/runs` body | Includes `allowed_urls`. |
| `runner-service POST /runs` | Validates `allowed_urls` is a list of strings. Writes `/tmp/proxy-allowlists/<sandbox_id>.json`. Injects `HTTPS_PROXY`, `HTTP_PROXY`, `SSL_CERT_FILE`, `X-QA-Sandbox-ID` env / mount into the sandbox container. |
| `sandbox_executor.py` | New env vars in the `podman run` command. New volume mount for the allow-list config (read-only). |
| `script_generator.py` | The generated script injects `X-QA-Sandbox-ID` header into every Playwright request via Playwright's `set_extra_http_headers`. |
| Existing smoke tests | Updated to pass `allowed_urls=["https://example.com/*"]` for the example.com tests. |

## Updates to `infra.sh`

| Section | Change |
|---|---|
| 1 Variables | `IMG_PROXY="qa-aqa/proxy-service:dev"`, `PORT_PROXY=8083` (host-side admin/healthcheck only — sandboxes use container port 8080 internally). |
| 5a build_custom_images | Loop covers 6 services now (adds proxy). |
| 5 compose | Add `proxy-service` (on **both** `sandbox-egress` AND a new `proxy-upstream` network so it can reach the internet). Add `mitm-ca` and `proxy-allowlists` named volumes. runner-service mounts `proxy-allowlists` writable. |
| 5b new step `prepare_mitm_ca` | After compose up, copy `mitm-ca/mitmproxy-ca-cert.pem` to `services/sandbox-image/qa-aqa-mitmproxy-ca.pem` if changed; force a rebuild of `qa-aqa/sandbox:dev` when changed. |
| 8 smoke | 4 new tests covering allow + deny + missing-allowlist + path-level. |
| Endpoint summary | proxy-service (admin/health only). |

## Smoke tests for D1.3

| # | Test | Verifies |
|---|---|---|
| 1 | `proxy-service /health` (mitmproxy has a status endpoint) → 200 | proxy up |
| 2 | Sandbox-mode workflow with `allowed_urls=["https://example.com/*"]` reaching `https://example.com` → `status=pass` + ≥1 screenshot | allow path works |
| 3 | Sandbox-mode workflow with `allowed_urls=["https://example.com/*"]` reaching `https://example.org` → `status=fail/error` + error message contains `"egress denied"` or `403` | deny path works |
| 4 | Sandbox-mode workflow with NO `allowed_urls` field → strict-default deny → error contains `"no allowlist"` | strict default |
| 5 | Sandbox-mode workflow with `allowed_urls=["https://example.com/foo*"]` reaching `https://example.com/bar` → denied | path-level enforcement |

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| TLS MITM breaks cert-pinned apps (some banks, mobile apps) | v1 dev only; documented. Production tenants who need pinned access opt out per-workflow with `bypass_proxy=true` (not implemented in D1.3 — D1.4 work). |
| CA cert rotation requires sandbox image rebuild | `infra.sh` detects CA change via sha and rebuilds; documented for ops. |
| mitmproxy slow under load (Python-based) | Acceptable for v1. D1.5 considers envoy with SDS for high-throughput. |
| Sandbox needs to know its own `X-QA-Sandbox-ID` for the addon to enforce per-run | Injected as env var + the generated script sets it via Playwright's default headers. |
| `X-QA-Sandbox-ID` is forge-able if attacker controls the sandbox | True. Real defense is the per-run allow-list FILE PATH being unguessable: sandbox-id is UUID. v1 trust boundary: runner-service writes only its own ids. |
| Default-deny breaks existing tests | All existing sandbox-mode tests updated to pass `allowed_urls` explicitly. |
| CA file ends up in sandbox image layer (visible in image history) | Acceptable — the CA is for a local QA/AQA mitmproxy only. Not used outside this stack. |
| Proxy on `sandbox-egress` and `proxy-upstream` networks may leak between them | proxy-service is the controlled bridge; this is the intended trust boundary. |

## Acceptance

- 21 containers running steady-state (+1: proxy-service)
- 38 smoke tests green (was 34; +4 D1.3 tests)
- Sandbox with valid `allowed_urls` for example.com → real screenshot uploaded
- Sandbox attempting non-allowlisted host → status=fail/error, MinIO has the console log showing the 403
- Sandbox with no `allowed_urls` → strict-deny default; cannot reach anything
- Existing D1.2 smoke tests continue to pass (updated to pass `allowed_urls`)

## Out of scope (D1.4+)

- Per-tenant CAs (one mitmproxy CA shared across all tenants in v1)
- Allow-list templates / inheritance / per-tenant defaults
- mTLS between sandbox and proxy
- Traffic recording / replay
- Per-run rate limiting on egress (no per-URL request budgets)
- Sandbox bypass for cert-pinned scenarios
- Granular Robots-style header stripping rules
- envoy-based scaling (mitmproxy at scale is the bottleneck)
