"""Temporal worker entry. Runs forever; polls task queue 'test-design'.

Boot sequence:
  1) Connect to Temporal (10× retry with backoff)
  2) Ingest seed docs into rag-service via a one-shot client call
  3) Register workflows + activities
  4) Block on Worker.run()
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from temporalio.client import Client
from temporalio.worker import Worker

from . import config
from .activities import (
    bulk_create_artefacts_activity,
    create_artefact_activity,
    ingest_seed_docs_activity,
    run_test_designer_activity,
)
from .registry import all_workflows

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("agent-workers")


async def _connect_with_retry() -> Client:
    delay = 1.0
    last_err: Exception | None = None
    for attempt in range(1, 11):
        try:
            log.info("connecting to temporal at %s (attempt %d)", config.TEMPORAL_HOST, attempt)
            return await Client.connect(config.TEMPORAL_HOST)
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("temporal connect failed: %s; sleeping %.1fs", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15.0)
    assert last_err is not None
    raise last_err


async def _seed_docs() -> None:
    """One-shot, non-fatal: try to ingest spec docs into rag-service."""
    import httpx

    headers = {"Authorization": f"Bearer {config.LITELLM_KEY}"} if config.LITELLM_KEY else {}
    try:
        async with httpx.AsyncClient(headers=headers, timeout=60.0) as client:
            from pathlib import Path
            src = Path(config.DOCS_SOURCE)
            if not src.exists():
                log.info("Seed: source %s missing, skipping", src)
                return
            files = sorted(src.glob("**/*.md"))
            if not files:
                log.info("Seed: no .md files under %s", src)
                return
            n = 0
            for p in files:
                import hashlib
                text = p.read_text()
                doc_id = "doc:" + hashlib.sha256(p.read_bytes()).hexdigest()[:16]
                body = {"id": doc_id, "text": text, "metadata": {"source": p.name, "corpus": "docs"}}
                r = await client.post(f"{config.RAG_URL}/ingest", json=body)
                r.raise_for_status()
                n += 1
            log.info("Seed: %d spec docs ingested into corpus=docs", n)
    except Exception as e:  # noqa: BLE001
        log.warning("Seed: failed (%s) — continuing without seed corpus", e)


async def main() -> None:
    client = await _connect_with_retry()
    await _seed_docs()
    workflows = all_workflows()
    log.info(
        "registering %d workflow(s) and %d activities on queue '%s'",
        len(workflows), 4, config.TASK_QUEUE,
    )
    worker = Worker(
        client,
        task_queue=config.TASK_QUEUE,
        workflows=workflows,
        activities=[
            create_artefact_activity,
            bulk_create_artefacts_activity,
            run_test_designer_activity,
            ingest_seed_docs_activity,
        ],
    )
    log.info("Worker started, polling '%s' queue", config.TASK_QUEUE)
    await worker.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
