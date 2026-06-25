"""Runtime configuration. All values come from env; defaults match compose."""
from __future__ import annotations

import os

TEMPORAL_HOST = os.environ.get("TEMPORAL_HOST", "temporal:7233")
TASK_QUEUE = os.environ.get("TEMPORAL_TASK_QUEUE", "test-design")

ARTEFACT_URL = os.environ.get("ARTEFACT_URL", "http://artefact-service:8003")
RAG_URL = os.environ.get("RAG_URL", "http://rag-service:8001")
POLICY_URL = os.environ.get("POLICY_URL", "http://policy-svc:8002")
MODEL_GATEWAY_URL = os.environ.get("MODEL_GATEWAY_URL", "http://model-gateway:4000")
RUNNER_URL = os.environ.get("RUNNER_URL", "http://runner-service:8004")

LITELLM_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

DOCS_SOURCE = os.environ.get("DOCS_SOURCE", "/specs")
DEFAULT_TENANT = "default"

AGENT_URN = "urn:qa-aqa:agent:test_designer:v1"
EXECUTOR_URN = "urn:qa-aqa:agent:executor:v1"

CHAT_DEV_MODEL = "chat-dev"
CHAT_PROD_MODEL = "chat-prod"
EMBED_DEV_MODEL = "embed-dev"
