#!/usr/bin/env python3
"""
ANAQ Hive Mind — Memory Bridge Service
Unified semantic search across FAISS indices + OpenAI-compatible embedding proxy.

Port: 9600
"""

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.expanduser("~"), ".anaq"))
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import faiss
import httpx
import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FAISS_DIR = Path.home() / ".anaq" / "faiss"
METADATA_DB = FAISS_DIR / "metadata.db"
EMBED_URL = os.environ.get("EMBED_URL", "http://localhost:9500")
LOG_DIR = Path.home() / ".anaq" / "logs"
LOG_FILE = LOG_DIR / "memory_bridge.log"
VECTOR_DIM = 768  # nomic-embed-text-v1.5

INDEX_NAMES = [
    "SYSTEM",
    "SOLUTIONS",
    "BUSINESS",
    "MEDICAL",
    "AGENTS",
    "CODEBASE",
    "CONVERSATIONS",
    "SHARED",        # Generic tools, skills, procedures — not agent-specific
    "OBSERVATIONS",  # Self-observations, behavioral evolution (per-agent + cross-agent)
    "BEHAVIOURS",    # Permanent promoted rules, mandated cross-agent directives
]

# Batch FAISS saves: flush to disk every N writes
FLUSH_INTERVAL = 10

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR.mkdir(parents=True, exist_ok=True)
FAISS_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("memory_bridge")
logger.setLevel(logging.DEBUG)

_fmt = logging.Formatter("[%(asctime)s] %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
_fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(_fmt)
logger.addHandler(_fh)

_sh = logging.StreamHandler(sys.stderr)
_sh.setLevel(logging.INFO)
_sh.setFormatter(_fmt)
logger.addHandler(_sh)


# ---------------------------------------------------------------------------
# Metadata SQLite
# ---------------------------------------------------------------------------

def _init_metadata_db():
    conn = sqlite3.connect(str(METADATA_DB))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            index_name TEXT NOT NULL,
            faiss_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            source TEXT DEFAULT '',
            agent_scope TEXT DEFAULT '["all"]',
            content_hash TEXT NOT NULL,
            metadata_json TEXT DEFAULT '{}',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_docs_index ON documents(index_name)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_docs_hash ON documents(content_hash)
    """)
    # Deduplicate before creating unique index (keep lowest rowid per hash+index)
    try:
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_docs_unique_hash
            ON documents(content_hash, index_name)
        """)
    except sqlite3.IntegrityError:
        logger.warning("Duplicate content_hash+index_name rows found — deduplicating")
        conn.execute("""
            DELETE FROM documents WHERE id NOT IN (
                SELECT MIN(id) FROM documents GROUP BY content_hash, index_name
            )
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_docs_unique_hash
            ON documents(content_hash, index_name)
        """)
    conn.commit()
    conn.close()


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _check_duplicate(content_hash: str, index_name: str) -> bool:
    conn = sqlite3.connect(str(METADATA_DB))
    row = conn.execute(
        "SELECT 1 FROM documents WHERE content_hash = ? AND index_name = ? LIMIT 1",
        (content_hash, index_name),
    ).fetchone()
    conn.close()
    return row is not None


def _insert_metadata(
    index_name: str,
    faiss_id: int,
    content: str,
    source: str,
    agent_scope: list[str],
    content_hash: str,
    metadata: dict,
) -> int:
    conn = sqlite3.connect(str(METADATA_DB))
    cur = conn.execute(
        """INSERT INTO documents
           (index_name, faiss_id, content, source, agent_scope, content_hash, metadata_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            index_name,
            faiss_id,
            content,
            source,
            json.dumps(agent_scope),
            content_hash,
            json.dumps(metadata),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    doc_id = cur.lastrowid
    conn.commit()
    conn.close()
    return doc_id


def _check_and_insert_metadata(
    index_name: str,
    faiss_id: int,
    content: str,
    source: str,
    agent_scope: list[str],
    content_hash: str,
    metadata: dict,
) -> tuple[bool, int]:
    """Atomic duplicate check + insert in a single connection/transaction.

    Returns (is_duplicate, doc_id). doc_id is 0 if duplicate.
    Uses the UNIQUE(content_hash, index_name) constraint as the final guard.
    """
    conn = sqlite3.connect(str(METADATA_DB))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        cur = conn.execute(
            """INSERT INTO documents
               (index_name, faiss_id, content, source, agent_scope, content_hash, metadata_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                index_name,
                faiss_id,
                content,
                source,
                json.dumps(agent_scope),
                content_hash,
                json.dumps(metadata),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        doc_id = cur.lastrowid
        conn.commit()
        return False, doc_id
    except sqlite3.IntegrityError:
        # UNIQUE constraint violation — duplicate
        conn.rollback()
        return True, 0
    finally:
        conn.close()


def _search_metadata(
    index_name: str,
    faiss_ids: list[int],
    agent_scope: Optional[str] = None,
) -> list[dict]:
    conn = sqlite3.connect(str(METADATA_DB))
    placeholders = ",".join("?" * len(faiss_ids))
    rows = conn.execute(
        f"SELECT id, faiss_id, content, source, agent_scope, metadata_json, created_at "
        f"FROM documents WHERE index_name = ? AND faiss_id IN ({placeholders})",
        [index_name] + faiss_ids,
    ).fetchall()
    conn.close()

    results = []
    for row in rows:
        scopes = json.loads(row[4])
        # Filter by agent scope
        if agent_scope and "all" not in scopes and agent_scope not in scopes:
            continue
        results.append({
            "id": row[0],
            "faiss_id": row[1],
            "content": row[2],
            "source": row[3],
            "agent_scope": scopes,
            "metadata": json.loads(row[5]),
            "created_at": row[6],
        })
    return results


def _get_index_stats() -> dict:
    conn = sqlite3.connect(str(METADATA_DB))
    rows = conn.execute(
        "SELECT index_name, COUNT(*) FROM documents GROUP BY index_name"
    ).fetchall()
    conn.close()
    return {row[0]: row[1] for row in rows}


# ---------------------------------------------------------------------------
# FAISS Index Manager
# ---------------------------------------------------------------------------

class FAISSIndexManager:
    def __init__(self):
        self.indices: dict[str, faiss.IndexFlatIP] = {}
        self.pending_writes: dict[str, int] = {}  # index_name -> count since last flush
        self._locks: dict[str, asyncio.Lock] = {}  # per-index locks for add_vector

    def load_or_create(self, name: str) -> faiss.IndexFlatIP:
        if name in self.indices:
            return self.indices[name]

        index_path = FAISS_DIR / f"{name}.index"
        if index_path.exists():
            logger.info("Loading FAISS index: %s (%s)", name, index_path)
            idx = faiss.read_index(str(index_path))
        else:
            logger.info("Creating new FAISS index: %s (dim=%d)", name, VECTOR_DIM)
            idx = faiss.IndexFlatIP(VECTOR_DIM)  # Inner product (cosine on normalized vecs)

        self.indices[name] = idx
        self.pending_writes[name] = 0
        return idx

    def _get_lock(self, name: str) -> asyncio.Lock:
        if name not in self._locks:
            self._locks[name] = asyncio.Lock()
        return self._locks[name]

    async def add_vector_safe(self, name: str, vector: np.ndarray) -> int:
        """Add vector with per-index lock to prevent ntotal race conditions."""
        async with self._get_lock(name):
            return self._add_vector_unlocked(name, vector)

    def _add_vector_unlocked(self, name: str, vector: np.ndarray) -> int:
        idx = self.load_or_create(name)
        faiss_id = idx.ntotal
        idx.add(vector.reshape(1, -1).astype(np.float32))
        self.pending_writes[name] = self.pending_writes.get(name, 0) + 1

        if self.pending_writes[name] >= FLUSH_INTERVAL:
            self.save(name)

        return faiss_id

    def add_vector(self, name: str, vector: np.ndarray) -> int:
        """Synchronous add — use add_vector_safe() from async contexts."""
        return self._add_vector_unlocked(name, vector)

    def search(self, name: str, vector: np.ndarray, top_k: int = 5) -> list[tuple[int, float]]:
        idx = self.load_or_create(name)
        if idx.ntotal == 0:
            return []

        k = min(top_k, idx.ntotal)
        scores, ids = idx.search(vector.reshape(1, -1).astype(np.float32), k)
        return [(int(ids[0][i]), float(scores[0][i])) for i in range(k) if ids[0][i] >= 0]

    def save(self, name: str):
        if name in self.indices:
            index_path = FAISS_DIR / f"{name}.index"
            faiss.write_index(self.indices[name], str(index_path))
            self.pending_writes[name] = 0
            logger.debug("Saved FAISS index: %s (%d vectors)", name, self.indices[name].ntotal)

    def save_all(self):
        for name in list(self.indices.keys()):
            self.save(name)

    def stats(self) -> dict:
        result = {}
        for name in INDEX_NAMES:
            idx = self.load_or_create(name)
            index_path = FAISS_DIR / f"{name}.index"
            size_mb = index_path.stat().st_size / (1024 * 1024) if index_path.exists() else 0
            result[name] = {
                "count": idx.ntotal,
                "size_mb": round(size_mb, 2),
                "pending_writes": self.pending_writes.get(name, 0),
            }
        return result


# Global index manager
index_mgr = FAISSIndexManager()

# ---------------------------------------------------------------------------
# Embedding client
# ---------------------------------------------------------------------------

_http_client: Optional[httpx.AsyncClient] = None
_http_client_lock = asyncio.Lock()


async def _get_client() -> httpx.AsyncClient:
    global _http_client
    async with _http_client_lock:
        if _http_client is None or _http_client.is_closed:
            _http_client = httpx.AsyncClient(timeout=120.0)
        return _http_client


async def _embed_texts(texts: list[str]) -> list[list[float]]:
    """Get embeddings from the embedding service on port 9500."""
    client = await _get_client()
    try:
        resp = await client.post(
            f"{EMBED_URL}/embed",
            json={"texts": texts},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["embeddings"]
    except Exception as e:
        logger.error("Embedding service error: %s", e)
        raise HTTPException(status_code=503, detail=f"Embedding service unavailable: {e}")


async def _embed_single(text: str) -> np.ndarray:
    embeddings = await _embed_texts([text])
    return np.array(embeddings[0], dtype=np.float32)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class SearchRequest(BaseModel):
    query: str
    indices: list[str] = Field(default_factory=lambda: INDEX_NAMES)
    top_k: int = 5
    min_score: float = 0.6  # Filter out low-relevance results
    agent_scope: Optional[str] = None  # e.g., "anaq", "pearl"


class IngestRequest(BaseModel):
    content: str
    index: str
    source: str = ""
    agent_scope: list[str] = Field(default_factory=lambda: ["all"])
    metadata: dict = Field(default_factory=dict)


class BatchIngestRequest(BaseModel):
    documents: list[IngestRequest]


class EmbeddingRequest(BaseModel):
    model: str = "nomic-ai/nomic-embed-text-v1.5"
    input: str | list[str]


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

from lib.metrics import MetricsCollector, get_all_process_stats, get_model_usage, get_agent_usage, get_recent_errors

memory_metrics = MetricsCollector("memory_bridge")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=== Memory Bridge starting on port 9600 ===")
    _init_metadata_db()
    obs_init_db()

    # Pre-load all indices
    for name in INDEX_NAMES:
        index_mgr.load_or_create(name)

    logger.info("All FAISS indices loaded")
    yield

    # Save on shutdown
    index_mgr.save_all()
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
    logger.info("=== Memory Bridge shutting down ===")


app = FastAPI(
    title="ANAQ Memory Bridge",
    description="Unified semantic search + FAISS management + embedding proxy",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    # Check embedding service
    embed_ok = False
    try:
        client = await _get_client()
        resp = await client.get(f"{EMBED_URL}/health", timeout=5.0)
        embed_ok = resp.status_code == 200
    except Exception:
        pass

    return {
        "status": "healthy" if embed_ok else "degraded",
        "embedding_service": "up" if embed_ok else "down",
        "indices": index_mgr.stats(),
        "metadata_db": str(METADATA_DB),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/search")
async def search(request: SearchRequest):
    query_vec = await _embed_single(request.query)

    all_results = []
    for idx_name in request.indices:
        if idx_name not in INDEX_NAMES:
            continue
        hits = index_mgr.search(idx_name, query_vec, request.top_k)
        if not hits:
            continue

        faiss_ids = [h[0] for h in hits]
        score_map = {h[0]: h[1] for h in hits}

        docs = _search_metadata(idx_name, faiss_ids, request.agent_scope)
        for doc in docs:
            doc["score"] = score_map.get(doc["faiss_id"], 0.0)
            doc["index"] = idx_name
            all_results.append(doc)

    # Filter by min_score, sort by score descending, take top_k
    all_results = [r for r in all_results if r["score"] >= request.min_score]
    all_results.sort(key=lambda x: x["score"], reverse=True)
    return all_results[: request.top_k]


@app.post("/ingest")
async def ingest(request: IngestRequest):
    if request.index not in INDEX_NAMES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid index '{request.index}'. Valid: {INDEX_NAMES}",
        )

    ch = _content_hash(request.content)

    # Check duplicate BEFORE embedding to avoid FAISS orphans (P2-006 TOCTOU fix)
    if _check_duplicate(ch, request.index):
        return {"status": "duplicate", "content_hash": ch}

    # Try to embed and add to FAISS; if embedding service is down, store in
    # SQLite with faiss_id=-1 (pending). Nightly Harrier sync will backfill.
    faiss_id = -1
    try:
        vec = await _embed_single(request.content)
        faiss_id = await index_mgr.add_vector_safe(request.index, vec)
    except Exception as e:
        logger.warning("Embedding unavailable, storing to SQLite only (pending sync): %s", e)

    is_dup, doc_id = _check_and_insert_metadata(
        index_name=request.index,
        faiss_id=faiss_id,
        content=request.content,
        source=request.source,
        agent_scope=request.agent_scope,
        content_hash=ch,
        metadata=request.metadata,
    )

    if is_dup:
        # Rare race: concurrent request inserted between our check and insert.
        # FAISS vector is orphaned but nightly cleanup handles this.
        logger.debug("Race-condition duplicate detected for hash=%s", ch[:16])
        return {"status": "duplicate", "content_hash": ch}

    status = "ingested" if faiss_id >= 0 else "pending_sync"
    if status == "pending_sync":
        logger.info("FAISS pending: doc_id=%d index=%s — awaiting nightly Harrier backfill", doc_id, request.index)
    else:
        logger.debug("Ingested: doc_id=%d faiss_id=%d index=%s", doc_id, faiss_id, request.index)
    return {"status": status, "id": doc_id, "faiss_id": faiss_id, "index": request.index}


@app.post("/batch_ingest")
async def batch_ingest(request: BatchIngestRequest):
    results = []
    for doc in request.documents:
        if doc.index not in INDEX_NAMES:
            results.append({"status": "error", "detail": f"Invalid index: {doc.index}"})
            continue

        ch = _content_hash(doc.content)

        faiss_id = -1
        try:
            vec = await _embed_single(doc.content)
            faiss_id = await index_mgr.add_vector_safe(doc.index, vec)
        except Exception as e:
            logger.warning("Batch embed failed for doc, storing pending: %s", e)

        is_dup, doc_id = _check_and_insert_metadata(
            index_name=doc.index,
            faiss_id=faiss_id,
            content=doc.content,
            source=doc.source,
            agent_scope=doc.agent_scope,
            content_hash=ch,
            metadata=doc.metadata,
        )
        if is_dup:
            results.append({"status": "duplicate", "content_hash": ch})
        else:
            results.append({"status": "ingested", "id": doc_id, "faiss_id": faiss_id})

    # Flush all indices after batch
    index_mgr.save_all()
    return {"results": results, "total": len(results)}


@app.delete("/memory/{doc_id}")
async def delete_memory(doc_id: int):
    """Delete a document from metadata (FAISS vectors are not removed — they become orphans).
    A full rebuild via /rebuild would clean orphaned vectors."""
    conn = sqlite3.connect(str(METADATA_DB))
    row = conn.execute("SELECT index_name FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")
    conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    conn.commit()
    conn.close()
    logger.info("Deleted document %d from index %s", doc_id, row[0])
    return {"status": "deleted", "id": doc_id, "index": row[0]}


@app.get("/stats")
async def stats():
    db_stats = _get_index_stats()
    faiss_stats = index_mgr.stats()

    return {
        "indices": {
            name: {
                "faiss_vectors": faiss_stats.get(name, {}).get("count", 0),
                "metadata_docs": db_stats.get(name, 0),
                "size_mb": faiss_stats.get(name, {}).get("size_mb", 0),
            }
            for name in INDEX_NAMES
        },
        "total_vectors": sum(s.get("count", 0) for s in faiss_stats.values()),
        "total_docs": sum(db_stats.values()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# OpenAI-compatible embedding proxy (for Agent Zero / LiteLLM)
# ---------------------------------------------------------------------------

@app.post("/v1/embeddings")
async def openai_embeddings(request: EmbeddingRequest):
    """Proxy embedding requests in OpenAI format to the local embedding service."""
    texts = request.input if isinstance(request.input, list) else [request.input]
    embeddings = await _embed_texts(texts)

    return {
        "object": "list",
        "data": [
            {"object": "embedding", "embedding": emb, "index": i}
            for i, emb in enumerate(embeddings)
        ],
        "model": request.model,
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
    }


@app.get("/v1/models")
async def list_models():
    return {
        "data": [
            {"id": "nomic-ai/nomic-embed-text-v1.5", "object": "model", "owned_by": "nomic-ai"}
        ]
    }


# ---------------------------------------------------------------------------
# Context Loader endpoint — non-agentic, ungated context injection
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Observation Engine endpoints (tiered behavioral evolution)
# ---------------------------------------------------------------------------

from observation_engine import (
    _init_db as obs_init_db,
    record_observation as obs_record,
    get_agent_observations as obs_get,
    get_mandated_observations as obs_mandated,
    approve_observation as obs_approve,
    reject_observation as obs_reject,
    delete_observation as obs_delete,
    get_observation_history as obs_history,
    compile_system_prompt as obs_compile,
    run_decay as obs_decay,
    migrate_from_oc as obs_migrate,
)


class ObsRecordRequest(BaseModel):
    agent: str
    observation_type: str
    observation: str
    confidence: float = 0.5
    source: str = "unknown"


@app.post("/obs/record")
async def api_obs_record(request: ObsRecordRequest):
    """Record or reinforce an observation. Auto-promotes, auto-propagates cross-agent."""
    import asyncio
    result = await asyncio.to_thread(
        obs_record, request.agent, request.observation_type,
        request.observation, request.confidence, request.source,
    )

    # Also ingest into OBSERVATIONS FAISS index for semantic search
    if result.get("action") in ("created", "reinforced"):
        obs_content = f"[{request.observation_type}] {request.observation}"
        obs_hash = _content_hash(f"obs:{request.agent}:{request.observation}")
        obs_meta = {
            "type": "observation",
            "observation_type": request.observation_type,
            "tier": result.get("tier", "MEDIUM"),
            "confidence": request.confidence,
            "agent": request.agent,
        }
        faiss_id = -1
        try:
            vec = await _embed_single(obs_content)
            faiss_id = await index_mgr.add_vector_safe("OBSERVATIONS", vec)
        except Exception as e:
            logger.warning("Embedding unavailable for observation, storing pending: %s", e)

        _insert_metadata(
            index_name="OBSERVATIONS",
            faiss_id=faiss_id,
            content=obs_content,
            source=request.source,
            agent_scope=[request.agent],
            content_hash=obs_hash,
            metadata=obs_meta,
        )

    return result


@app.get("/obs/mandated")
async def api_obs_mandated(limit: int = 20):
    """Get all mandated cross-agent observations."""
    import asyncio
    return await asyncio.to_thread(obs_mandated, limit)


@app.get("/obs/{agent_name}")
async def api_obs_get(agent_name: str, tier: Optional[str] = None, limit: int = 20):
    """Get active observations for an agent, optionally filtered by tier."""
    import asyncio
    observations = await asyncio.to_thread(obs_get, agent_name, tier, limit)
    return {"agent": agent_name, "observations": observations, "count": len(observations)}


@app.post("/obs/approve/{obs_id}")
async def api_obs_approve(obs_id: int):
    """ANAQ approves an observation. May trigger promotion."""
    import asyncio
    return await asyncio.to_thread(obs_approve, obs_id)


@app.post("/obs/reject/{obs_id}")
async def api_obs_reject(obs_id: int):
    """ANAQ rejects an observation."""
    import asyncio
    return await asyncio.to_thread(obs_reject, obs_id)


@app.delete("/obs/{obs_id}")
async def api_obs_delete(obs_id: int):
    """Delete an observation with full history trail."""
    import asyncio
    return await asyncio.to_thread(obs_delete, obs_id)


@app.get("/obs/history/{obs_id}")
async def api_obs_history(obs_id: int):
    """Get the full version history of an observation."""
    import asyncio
    versions = await asyncio.to_thread(obs_history, obs_id)
    return {"observation_id": obs_id, "versions": versions, "count": len(versions)}


@app.post("/obs/decay")
async def api_obs_decay():
    """Run tier-appropriate decay on all observations."""
    import asyncio
    return await asyncio.to_thread(obs_decay)


@app.post("/obs/migrate")
async def api_obs_migrate():
    """One-time: import existing OC self_observations into observation engine."""
    import asyncio
    return await asyncio.to_thread(obs_migrate)


# ---------------------------------------------------------------------------
# System Prompt Compiler
# ---------------------------------------------------------------------------

@app.get("/compile/{agent_name}")
async def api_compile_prompt(agent_name: str):
    """
    Compile a single injection block for an agent's system prompt.
    Returns tiered behavioral context: PERMANENT rules, HIGH observations,
    MEDIUM notes, MANDATED cross-agent directives.
    Both OC and A0 call this at spawn.
    """
    import asyncio
    compiled = await asyncio.to_thread(obs_compile, agent_name)
    return {
        "agent": agent_name,
        "compiled_prompt": compiled,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Delete Propagation
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Training Data API
# ---------------------------------------------------------------------------

try:
    from training_collector import (
        query_responses as tc_query_responses,
        query_dpo_pairs as tc_query_dpo,
        get_stats as tc_stats,
        export_sft as tc_export_sft,
        export_dpo as tc_export_dpo,
    )
    _TC_AVAILABLE = True
except ImportError:
    _TC_AVAILABLE = False


@app.get("/training/stats")
async def api_training_stats():
    """Training data statistics — counts by agent, verdict, score range."""
    if not _TC_AVAILABLE:
        raise HTTPException(status_code=503, detail="Training collector not available")
    import asyncio
    return await asyncio.to_thread(tc_stats)


@app.get("/training/responses")
async def api_training_responses(
    agent: Optional[str] = None,
    approved: Optional[bool] = None,
    verdict: Optional[str] = None,
    min_score: Optional[int] = None,
    max_score: Optional[int] = None,
    grader: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 50,
):
    """Query graded responses. Filter by agent, verdict, score range, grader, date."""
    if not _TC_AVAILABLE:
        raise HTTPException(status_code=503, detail="Training collector not available")
    import asyncio
    return await asyncio.to_thread(
        tc_query_responses,
        agent=agent, approved=approved, verdict=verdict,
        min_score=min_score, max_score=max_score,
        grader_model=grader, since=since, until=until, limit=limit,
    )


@app.get("/training/dpo")
async def api_training_dpo(
    agent: Optional[str] = None,
    min_gap: Optional[int] = None,
    since: Optional[str] = None,
    limit: int = 50,
):
    """Query DPO preference pairs. Filter by agent, minimum score gap, date."""
    if not _TC_AVAILABLE:
        raise HTTPException(status_code=503, detail="Training collector not available")
    import asyncio
    return await asyncio.to_thread(tc_query_dpo, agent=agent, min_score_gap=min_gap, since=since, limit=limit)


@app.get("/training/export/sft")
async def api_export_sft(agent: Optional[str] = None, min_score: int = 75):
    """Export approved responses as SFT training format (messages array)."""
    if not _TC_AVAILABLE:
        raise HTTPException(status_code=503, detail="Training collector not available")
    import asyncio
    return await asyncio.to_thread(tc_export_sft, agent=agent, min_score=min_score)


@app.get("/training/export/dpo")
async def api_export_dpo(agent: Optional[str] = None, min_gap: int = 10):
    """Export DPO pairs in training format (prompt/chosen/rejected)."""
    if not _TC_AVAILABLE:
        raise HTTPException(status_code=503, detail="Training collector not available")
    import asyncio
    return await asyncio.to_thread(tc_export_dpo, agent=agent, min_gap=min_gap)


class PropagateDeleteRequest(BaseModel):
    doc_id: int = 0
    content_hash: str = ""


@app.post("/propagate_delete")
async def api_propagate_delete(request: PropagateDeleteRequest):
    """
    Propagate a deletion from local store to Bridge.
    Accepts either doc_id or content_hash (JSON body).
    Deletes metadata rows and logs removed FAISS IDs (orphaned vectors
    are cleaned on next /rebuild).
    """
    conn_meta = sqlite3.connect(str(METADATA_DB))
    conn_meta.execute("PRAGMA busy_timeout=5000")
    deleted = 0
    removed_faiss_ids: list[tuple[str, int]] = []

    if request.doc_id:
        row = conn_meta.execute(
            "SELECT id, index_name, faiss_id FROM documents WHERE id = ?",
            (request.doc_id,),
        ).fetchone()
        if row:
            conn_meta.execute("DELETE FROM documents WHERE id = ?", (row[0],))
            removed_faiss_ids.append((row[1], row[2]))
            deleted += 1

    if request.content_hash:
        rows = conn_meta.execute(
            "SELECT id, index_name, faiss_id FROM documents WHERE content_hash = ?",
            (request.content_hash,),
        ).fetchall()
        for row in rows:
            conn_meta.execute("DELETE FROM documents WHERE id = ?", (row[0],))
            removed_faiss_ids.append((row[1], row[2]))
            deleted += 1

    conn_meta.commit()
    conn_meta.close()

    if deleted > 0:
        logger.info(
            "propagate_delete: removed %d doc(s) [doc_id=%s, hash=%s, faiss_ids=%s]",
            deleted, request.doc_id, request.content_hash[:16] if request.content_hash else "",
            removed_faiss_ids,
        )

    return {
        "deleted": deleted,
        "doc_id": request.doc_id,
        "content_hash": request.content_hash,
        "orphaned_faiss_ids": removed_faiss_ids,
    }


@app.get("/context/{agent_name}")
async def get_context(agent_name: str, query: str = "", limit: int = 15, shared_limit: int = 10):
    """
    Direct context injection endpoint. Returns formatted memories for an agent.
    Called by OC spawn hooks and A0 init extensions independently.
    No AI, no grading, no gating — just data retrieval.
    """
    from datetime import datetime, timezone

    # Agent-scoped memories
    if query:
        q_vec = await _embed_single(query)
    else:
        q_vec = await _embed_single(f"{agent_name} recent context memories decisions observations")

    agent_results = []
    for idx_name in ["AGENTS", "CONVERSATIONS"]:
        hits = index_mgr.search(idx_name, q_vec, min(limit, 20))
        if hits:
            faiss_ids = [h[0] for h in hits]
            score_map = {h[0]: h[1] for h in hits}
            docs = _search_metadata(idx_name, faiss_ids, agent_name)
            for doc in docs:
                doc["score"] = score_map.get(doc["faiss_id"], 0.0)
                doc["index"] = idx_name
                agent_results.append(doc)

    agent_results = [r for r in agent_results if r["score"] >= 0.3]
    agent_results.sort(key=lambda x: x["score"], reverse=True)
    agent_results = agent_results[:limit]

    # Shared memories (tools, procedures — not agent-specific)
    shared_vec = await _embed_single("tools procedures protocols skills shared operations")
    shared_results = []
    for idx_name in ["SHARED", "SYSTEM"]:
        hits = index_mgr.search(idx_name, shared_vec, shared_limit)
        if hits:
            faiss_ids = [h[0] for h in hits]
            score_map = {h[0]: h[1] for h in hits}
            docs = _search_metadata(idx_name, faiss_ids)
            for doc in docs:
                doc["score"] = score_map.get(doc["faiss_id"], 0.0)
                shared_results.append(doc)

    shared_results = [r for r in shared_results if r["score"] >= 0.3]
    shared_results.sort(key=lambda x: x["score"], reverse=True)
    shared_results = shared_results[:shared_limit]

    # Solutions
    sol_vec = q_vec if query else await _embed_single(f"{agent_name} solutions fixes")
    sol_results = []
    hits = index_mgr.search("SOLUTIONS", sol_vec, 5)
    if hits:
        faiss_ids = [h[0] for h in hits]
        score_map = {h[0]: h[1] for h in hits}
        docs = _search_metadata("SOLUTIONS", faiss_ids, agent_name)
        for doc in docs:
            doc["score"] = score_map.get(doc["faiss_id"], 0.0)
            sol_results.append(doc)
    sol_results = [r for r in sol_results if r["score"] >= 0.4]

    return {
        "agent": agent_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "memories": agent_results,
        "shared": shared_results,
        "solutions": sol_results,
        "counts": {
            "memories": len(agent_results),
            "shared": len(shared_results),
            "solutions": len(sol_results),
        },
    }


# ---------------------------------------------------------------------------
# System-wide metrics dashboard
# ---------------------------------------------------------------------------

@app.get("/metrics")
async def metrics():
    """Per-process metrics for this memory bridge."""
    return memory_metrics.get_stats()


@app.get("/dashboard")
async def dashboard():
    """System-wide metrics dashboard — all processes, models, agents."""
    return {
        "processes": get_all_process_stats(),
        "model_usage_24h": get_model_usage(24),
        "agent_usage_24h": get_agent_usage(24),
        "recent_errors": get_recent_errors(10),
        "memory_indices": index_mgr.stats(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Scoring API (ALL SCORE ANAQ. ANAQ SCORES ALL.)
# ---------------------------------------------------------------------------

from lib.scoring import ScoringEngine

_scoring_engine = None

def _get_scoring() -> ScoringEngine:
    global _scoring_engine
    if _scoring_engine is None:
        _scoring_engine = ScoringEngine()
    return _scoring_engine


class ScoreAgentRequest(BaseModel):
    target: str
    dimension_scores: dict[str, int]
    mandate_adherence: int = 100
    auto_reject_trigger: Optional[str] = None
    revision_count: int = 0
    flaws: str = ""
    critique: str = ""


class ScoreAnaqRequest(BaseModel):
    scorer: str
    dimension_scores: dict[str, int]
    was_fair: bool = True
    critique: str = ""


@app.post("/scoring/score_agent")
async def api_score_agent(request: ScoreAgentRequest):
    """ANAQ scores an agent. Records assessment, updates points, tracks patterns."""
    engine = _get_scoring()
    return engine.score_agent(
        target=request.target,
        dimension_scores=request.dimension_scores,
        mandate_adherence=request.mandate_adherence,
        auto_reject_trigger=request.auto_reject_trigger,
        revision_count=request.revision_count,
        flaws=request.flaws,
        critique=request.critique,
    )


@app.post("/scoring/score_anaq")
async def api_score_anaq(request: ScoreAnaqRequest):
    """ANY agent scores ANAQ. ALL SCORE ANAQ. Bidirectional accountability."""
    engine = _get_scoring()
    return engine.score_anaq(
        scorer=request.scorer,
        dimension_scores=request.dimension_scores,
        was_fair=request.was_fair,
        critique=request.critique,
    )


@app.get("/scoring/leaderboard")
async def api_leaderboard():
    """Ranked leaderboard of all agents by total points."""
    return _get_scoring().get_leaderboard()


@app.get("/scoring/agent/{agent_name}")
async def api_agent_status(agent_name: str):
    """Get scoring status for a specific agent."""
    engine = _get_scoring()
    return {
        "agent": engine.get_agent_status(agent_name),
        "patterns": engine.get_patterns(agent_name),
        "recent": engine.get_recent_assessments(agent_name, limit=10),
        "dimensions": engine.get_dimensions(agent_name),
        "auto_reject_triggers": engine.get_auto_reject_triggers(agent_name),
    }


@app.get("/scoring/anaq_global")
async def api_anaq_global():
    """ANAQ's global score — the system health metric."""
    return _get_scoring().get_anaq_global()


@app.get("/scoring/dashboard")
async def api_scoring_dashboard():
    """Full scoring dashboard — leaderboard + ANAQ global + recent assessments."""
    return _get_scoring().get_dashboard()


# ---------------------------------------------------------------------------
# Direct run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "memory_bridge:app",
        host="127.0.0.1",
        port=9600,
        log_level="info",
        access_log=True,
    )
