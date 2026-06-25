"""rag-service: hybrid retrieval over pgvector + OpenSearch with RRF fusion.

Endpoints:
    GET  /health
    POST /ingest   { id, text, metadata? }            -> { chunks: int }
    POST /search   { query, k? }                       -> { hits: [{chunk_id, score, doc_id, text}] }
"""
from __future__ import annotations

import os
import re
import uuid
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import httpx
from fastapi import FastAPI, HTTPException
from opensearchpy import AsyncOpenSearch
from pydantic import BaseModel, Field

# ── config ───────────────────────────────────────────────────────────────────
PGHOST = os.environ.get("PGHOST", "postgres")
PGPORT = int(os.environ.get("PGPORT", "5432"))
PGUSER = os.environ.get("PGUSER", "app")
PGPASSWORD = os.environ.get("PGPASSWORD", "appdevpw")
PGDATABASE = os.environ.get("PGDATABASE", "app")
OPENSEARCH_URL = os.environ.get("OPENSEARCH_URL", "http://opensearch:9200")
OPENSEARCH_INDEX = os.environ.get("OPENSEARCH_INDEX", "documents")
MODEL_GATEWAY_URL = os.environ.get("MODEL_GATEWAY_URL", "http://model-gateway:4000")
MODEL_GATEWAY_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "embed-dev")
EMBED_DIM = int(os.environ.get("EMBED_DIM", "768"))

# Fixed-size chunking with 50-token overlap, markdown-aware on H1/H2/H3 boundaries.
CHUNK_SIZE = 512
CHUNK_OVERLAP = 50
RRF_K = 60  # standard RRF constant; rank_i contribution = 1 / (RRF_K + rank_i)


# ── lifespan ─────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pg = await asyncpg.create_pool(
        host=PGHOST,
        port=PGPORT,
        user=PGUSER,
        password=PGPASSWORD,
        database=PGDATABASE,
        min_size=1,
        max_size=5,
    )
    app.state.os = AsyncOpenSearch(hosts=[OPENSEARCH_URL], verify_certs=False)
    app.state.http = httpx.AsyncClient(
        base_url=MODEL_GATEWAY_URL,
        headers={"Authorization": f"Bearer {MODEL_GATEWAY_KEY}"} if MODEL_GATEWAY_KEY else {},
        timeout=httpx.Timeout(60.0),
    )
    await _ensure_pg_schema(app.state.pg)
    await _ensure_os_index(app.state.os)
    try:
        yield
    finally:
        await app.state.pg.close()
        await app.state.os.close()
        await app.state.http.aclose()


app = FastAPI(title="rag-service", version="0.1.0", lifespan=lifespan)


# ── models ───────────────────────────────────────────────────────────────────
class IngestRequest(BaseModel):
    id: str
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class IngestResponse(BaseModel):
    doc_id: str
    chunks: int


class SearchRequest(BaseModel):
    query: str
    k: int = 10


class Hit(BaseModel):
    chunk_id: str
    doc_id: str
    score: float
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    hits: list[Hit]


# ── schema setup (idempotent) ────────────────────────────────────────────────
async def _ensure_pg_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS chunks (
                chunk_id   text PRIMARY KEY,
                doc_id     text NOT NULL,
                text       text NOT NULL,
                metadata   jsonb NOT NULL DEFAULT '{{}}'::jsonb,
                embedding  vector({EMBED_DIM}) NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS chunks_doc_id_idx ON chunks (doc_id)"
        )
        # HNSW index for cosine ANN; created on demand if not present
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw "
            "ON chunks USING hnsw (embedding vector_cosine_ops)"
        )


async def _ensure_os_index(client: AsyncOpenSearch) -> None:
    if await client.indices.exists(index=OPENSEARCH_INDEX):
        return
    await client.indices.create(
        index=OPENSEARCH_INDEX,
        body={
            "settings": {"number_of_shards": 1, "number_of_replicas": 0},
            "mappings": {
                "properties": {
                    "chunk_id": {"type": "keyword"},
                    "doc_id": {"type": "keyword"},
                    "text": {"type": "text"},
                    "metadata": {"type": "object", "enabled": True},
                }
            },
        },
    )


# ── chunking ─────────────────────────────────────────────────────────────────
_HEADER_RE = re.compile(r"^(#{1,3})\s+", re.MULTILINE)


def _chunk(text: str) -> list[str]:
    """Token-approximation chunking: split on markdown headers first, then
    fixed-size windows with overlap measured in whitespace-delimited tokens."""
    # Split at markdown headers; keep header with its body
    parts: list[str] = []
    cursor = 0
    for m in _HEADER_RE.finditer(text):
        if m.start() > cursor:
            parts.append(text[cursor : m.start()])
        cursor = m.start()
    parts.append(text[cursor:])

    chunks: list[str] = []
    for part in parts:
        tokens = part.split()
        if not tokens:
            continue
        if len(tokens) <= CHUNK_SIZE:
            chunks.append(part.strip())
            continue
        i = 0
        while i < len(tokens):
            window = tokens[i : i + CHUNK_SIZE]
            chunks.append(" ".join(window))
            i += CHUNK_SIZE - CHUNK_OVERLAP
    return [c for c in chunks if c]


# ── embedding ────────────────────────────────────────────────────────────────
async def _embed(client: httpx.AsyncClient, inputs: list[str]) -> list[list[float]]:
    r = await client.post("/v1/embeddings", json={"model": EMBED_MODEL, "input": inputs})
    r.raise_for_status()
    body = r.json()
    return [item["embedding"] for item in body["data"]]


# ── endpoints ────────────────────────────────────────────────────────────────
@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/ingest", response_model=IngestResponse)
async def ingest(req: IngestRequest) -> IngestResponse:
    chunks = _chunk(req.text)
    if not chunks:
        return IngestResponse(doc_id=req.id, chunks=0)

    try:
        embeddings = await _embed(app.state.http, chunks)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"embed failed: {e}") from e

    rows = []
    os_actions = []
    for chunk_text, vec in zip(chunks, embeddings, strict=True):
        chunk_id = f"{req.id}:{uuid.uuid4().hex[:12]}"
        # asyncpg + pgvector: pass vector literal as string "[v1,v2,...]"
        vec_literal = "[" + ",".join(f"{v:.7f}" for v in vec) + "]"
        rows.append((chunk_id, req.id, chunk_text, req.metadata, vec_literal))
        os_actions.append({"index": {"_index": OPENSEARCH_INDEX, "_id": chunk_id}})
        os_actions.append(
            {
                "chunk_id": chunk_id,
                "doc_id": req.id,
                "text": chunk_text,
                "metadata": req.metadata,
            }
        )

    async with app.state.pg.acquire() as conn:
        await conn.executemany(
            "INSERT INTO chunks (chunk_id, doc_id, text, metadata, embedding) "
            "VALUES ($1, $2, $3, $4::jsonb, $5::vector) "
            "ON CONFLICT (chunk_id) DO UPDATE SET text=EXCLUDED.text, "
            "metadata=EXCLUDED.metadata, embedding=EXCLUDED.embedding",
            [(cid, did, txt, _to_jsonb_text(md), vec) for cid, did, txt, md, vec in rows],
        )

    await app.state.os.bulk(body=os_actions, refresh=True)
    return IngestResponse(doc_id=req.id, chunks=len(chunks))


def _to_jsonb_text(d: dict[str, Any]) -> str:
    import json
    return json.dumps(d)


@app.post("/search", response_model=SearchResponse)
async def search(req: SearchRequest) -> SearchResponse:
    try:
        [qvec] = await _embed(app.state.http, [req.query])
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"embed failed: {e}") from e

    # pgvector ANN
    qvec_literal = "[" + ",".join(f"{v:.7f}" for v in qvec) + "]"
    async with app.state.pg.acquire() as conn:
        pg_rows = await conn.fetch(
            "SELECT chunk_id, doc_id, text, metadata, "
            "1 - (embedding <=> $1::vector) AS sim "
            "FROM chunks ORDER BY embedding <=> $1::vector LIMIT $2",
            qvec_literal,
            req.k,
        )

    # OpenSearch BM25
    os_resp = await app.state.os.search(
        index=OPENSEARCH_INDEX,
        body={"size": req.k, "query": {"match": {"text": req.query}}},
    )
    os_hits = os_resp.get("hits", {}).get("hits", [])

    # RRF merge
    rrf: dict[str, float] = {}
    payload: dict[str, dict[str, Any]] = {}
    for rank, row in enumerate(pg_rows):
        cid = row["chunk_id"]
        rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
        payload[cid] = {
            "chunk_id": cid,
            "doc_id": row["doc_id"],
            "text": row["text"],
            "metadata": row["metadata"] if isinstance(row["metadata"], dict) else _parse_jsonb(row["metadata"]),
        }
    for rank, hit in enumerate(os_hits):
        cid = hit["_id"]
        rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
        src = hit.get("_source", {})
        payload.setdefault(
            cid,
            {
                "chunk_id": cid,
                "doc_id": src.get("doc_id", ""),
                "text": src.get("text", ""),
                "metadata": src.get("metadata", {}),
            },
        )

    merged = sorted(rrf.items(), key=lambda kv: kv[1], reverse=True)[: req.k]
    hits = [Hit(score=score, **payload[cid]) for cid, score in merged]
    return SearchResponse(hits=hits)


def _parse_jsonb(value: Any) -> dict[str, Any]:
    import json
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    return {}
