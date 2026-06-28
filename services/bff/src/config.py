"""Runtime config — env-only."""
from __future__ import annotations

import os
import secrets
from pathlib import Path


BFF_PORT = int(os.environ.get("BFF_PORT", "8005"))
ARTEFACT_URL = os.environ.get("ARTEFACT_URL", "http://artefact-service:8003")
TEMPORAL_HOST = os.environ.get("TEMPORAL_HOST", "temporal:7233")
TEMPORAL_TASK_QUEUE = os.environ.get("TEMPORAL_TASK_QUEUE", "test-design")
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "minio:9000")
MINIO_BUCKET = os.environ.get("MINIO_BUCKET", "executions")
MINIO_USER = os.environ.get("MINIO_ROOT_USER", "minio")
MINIO_PASS = os.environ.get("MINIO_ROOT_PASSWORD", "")
SEED_DIR = Path(os.environ.get("SEED_DIR", "/app/seed"))
DEFAULT_TENANT = os.environ.get("DEFAULT_TENANT", "default")

# JWT — random hex if not supplied; persists across restarts only if env is set.
JWT_SECRET = os.environ.get("BFF_JWT_SECRET", secrets.token_hex(32))
JWT_ALG = "HS256"
JWT_TTL_HOURS = 12

# CORS allow-list for browser-direct calls (Vite dev mode + APISIX in stack)
CORS_ALLOW_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "CORS_ALLOW_ORIGINS",
        "http://localhost:9080,http://localhost:5173",
    ).split(",")
    if o.strip()
]
