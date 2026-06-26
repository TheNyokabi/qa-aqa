"""mitmproxy addon: enforce per-sandbox URL allow-list.

Every request must carry header `X-QA-Sandbox-ID`. We read the corresponding
file at `/tmp/proxy-allowlists/<sandbox_id>.json` (bind-mounted from
runner-service) and match the request URL against the allow-list patterns.
On mismatch: 403 with a clear error message.

The allow-list file shape:
    { "allowed_urls": ["https://example.com/*", "https://api.x.com/v1/*"] }

Patterns use shell-glob semantics via fnmatch.fnmatch.
"""
from __future__ import annotations

import fnmatch
import json
import time
from pathlib import Path

from mitmproxy import http

ALLOWLIST_DIR = Path("/tmp/proxy-allowlists")
CACHE_TTL_SECONDS = 5.0
_cache: dict[str, dict] = {}


def _load(sandbox_id: str) -> dict:
    now = time.time()
    entry = _cache.get(sandbox_id)
    if entry and (now - entry["loaded_at"]) < CACHE_TTL_SECONDS:
        return entry
    path = ALLOWLIST_DIR / f"{sandbox_id}.json"
    if not path.exists():
        return {"loaded_at": now, "allow": [], "missing": True}
    try:
        cfg = json.loads(path.read_text())
        allow = cfg.get("allowed_urls", [])
        if not isinstance(allow, list):
            allow = []
    except Exception:
        allow = []
    entry = {"loaded_at": now, "allow": [str(p) for p in allow], "missing": False}
    _cache[sandbox_id] = entry
    return entry


def _matches(url: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if fnmatch.fnmatch(url, pattern):
            return True
        # Also try the "no-trailing-slash" variant (common surprise)
        if not pattern.endswith("/") and fnmatch.fnmatch(url + "/", pattern + "/"):
            return True
    return False


def request(flow: http.HTTPFlow) -> None:
    sid = flow.request.headers.get("X-QA-Sandbox-ID", "")
    if not sid:
        flow.response = http.Response.make(
            403,
            b"egress denied: missing X-QA-Sandbox-ID header\n",
            {"Content-Type": "text/plain"},
        )
        return
    cfg = _load(sid)
    if cfg.get("missing"):
        flow.response = http.Response.make(
            403,
            b"egress denied: no allowlist file for this sandbox (strict-default deny)\n",
            {"Content-Type": "text/plain"},
        )
        return
    url = flow.request.pretty_url
    if not _matches(url, cfg["allow"]):
        flow.response = http.Response.make(
            403,
            f"egress denied: {url} not in allowlist\n".encode(),
            {"Content-Type": "text/plain"},
        )
        return
    # Allowed — pass through
