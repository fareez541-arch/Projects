#!/usr/bin/env python3
"""Nightly sync worker: embed all pending SQLite docs into FAISS via dual Harrier-27B.

Finds all documents with faiss_id = -1 (pending), embeds them using two
Harrier-27B servers on ports 9510/9511, adds vectors to FAISS indices,
and updates the faiss_id in SQLite.

Also re-embeds any docs still at 768d that need upgrading to 5376d.
"""

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import faiss
import numpy as np
import requests

FAISS_DIR = Path.home() / ".anaq" / "faiss"
DB_PATH = FAISS_DIR / "metadata.db"
SERVERS = ["http://localhost:9510"]
NEW_DIM = 5376

CHUNK_SIZE = 20000
CHUNK_OVERLAP = 2000


def get_embedding(text: str, server_url: str) -> list[float] | None:
    try:
        r = requests.post(
            f"{server_url}/v1/embeddings",
            json={"input": text[:32000], "model": "harrier"},
            timeout=600,
        )
        r.raise_for_status()
        return r.json()["data"][0]["embedding"]
    except Exception:
        return None


def chunk_text(text: str) -> list[str]:
    if len(text) <= CHUNK_SIZE:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = start + CHUNK_SIZE
        if end < len(text):
            nl = text.rfind("\n", start + CHUNK_SIZE - 500, end)
            if nl > start:
                end = nl + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start = end - CHUNK_OVERLAP
    return chunks


def embed_with_retry(text: str) -> list[float] | None:
    """Try both servers, then try chunking if text is too long."""
    for server in SERVERS:
        emb = get_embedding(text, server)
        if emb is not None:
            return emb

    # Text might be too long — try first chunk only for the vector
    chunks = chunk_text(text)
    if len(chunks) > 1:
        for server in SERVERS:
            emb = get_embedding(chunks[0], server)
            if emb is not None:
                return emb

    return None


def embed_parallel(texts_with_ids: list[tuple[int, str]]) -> list[tuple[int, list[float] | None]]:
    """Embed multiple texts in parallel across both servers."""
    results = []

    def do_one(item):
        idx, (doc_id, text) = item
        server = SERVERS[idx % len(SERVERS)]
        emb = get_embedding(text, server)
        if emb is None:
            # Retry other server
            emb = get_embedding(text, SERVERS[(idx + 1) % len(SERVERS)])
        if emb is None:
            # Try chunking
            chunks = chunk_text(text)
            if len(chunks) > 1:
                emb = get_embedding(chunks[0], SERVERS[idx % len(SERVERS)])
        return doc_id, emb

    with ThreadPoolExecutor(max_workers=len(SERVERS) * 2) as pool:
        futures = {pool.submit(do_one, (i, item)): i for i, item in enumerate(texts_with_ids)}
        for future in as_completed(futures):
            results.append(future.result())

    return results


def sync_index(index_name: str, pending_docs: list[tuple[int, str]], db: sqlite3.Connection):
    """Embed pending docs and add to the FAISS index."""
    idx_path = FAISS_DIR / f"{index_name}.index"

    # Load or create index at correct dimension
    if idx_path.exists():
        idx = faiss.read_index(str(idx_path))
        if idx.d != NEW_DIM:
            print(f"  WARNING: {index_name} is {idx.d}d, expected {NEW_DIM}d — rebuilding")
            idx = faiss.IndexFlatIP(NEW_DIM)
    else:
        idx = faiss.IndexFlatIP(NEW_DIM)

    cursor = db.cursor()
    added = 0
    failed = 0

    # Process in batches of 8 (parallel across 2 servers × 4 workers)
    batch_size = 8
    for batch_start in range(0, len(pending_docs), batch_size):
        batch = pending_docs[batch_start : batch_start + batch_size]
        results = embed_parallel(batch)

        for doc_id, emb in results:
            if emb is not None:
                vec = np.array([emb], dtype=np.float32)
                faiss.normalize_L2(vec)
                faiss_id = idx.ntotal
                idx.add(vec)
                cursor.execute("UPDATE documents SET faiss_id = ? WHERE id = ?", (faiss_id, doc_id))
                added += 1
            else:
                failed += 1
                print(f"  FAILED doc_id={doc_id}")

        # Progress
        done = batch_start + len(batch)
        if done % 50 == 0 or done == len(pending_docs):
            print(f"  [{index_name}] {done}/{len(pending_docs)} (added={added}, failed={failed})")

    # Save index
    faiss.write_index(idx, str(idx_path))
    db.commit()
    print(f"  {index_name}: +{added} vectors (total={idx.ntotal}), {failed} failed")
    return added, failed


def main():
    start = time.time()
    print(f"=== Nightly Harrier Sync — {datetime.now().isoformat()} ===")
    print(f"Servers: {SERVERS}")
    print(f"Target dimension: {NEW_DIM}")
    print()

    # Verify servers
    for s in SERVERS:
        emb = get_embedding("test", s)
        if emb and len(emb) == NEW_DIM:
            print(f"  {s}: OK ({len(emb)}d)")
        else:
            print(f"  {s}: FAILED — aborting")
            return

    # Find all pending docs
    db = sqlite3.connect(str(DB_PATH))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    cursor = db.cursor()

    cursor.execute(
        "SELECT id, index_name, content FROM documents WHERE faiss_id = -1 ORDER BY index_name, id"
    )
    rows = cursor.fetchall()

    if not rows:
        print("\nNo pending documents. Everything is synced.")
        db.close()
        return

    # Group by index
    by_index: dict[str, list[tuple[int, str]]] = {}
    for doc_id, index_name, content in rows:
        by_index.setdefault(index_name, []).append((doc_id, content))

    print(f"\nPending documents: {len(rows)} across {len(by_index)} indices")
    for idx_name, docs in sorted(by_index.items()):
        print(f"  {idx_name}: {len(docs)}")

    # Sync each index
    total_added = 0
    total_failed = 0
    for index_name, docs in sorted(by_index.items()):
        print(f"\n[{index_name}] Syncing {len(docs)} pending docs...")
        added, failed = sync_index(index_name, docs, db)
        total_added += added
        total_failed += failed

    db.close()

    elapsed = time.time() - start
    print(f"\n=== Sync complete ===")
    print(f"Added: {total_added} vectors")
    print(f"Failed: {total_failed}")
    print(f"Elapsed: {elapsed:.0f}s ({elapsed/60:.1f}m)")


if __name__ == "__main__":
    main()
