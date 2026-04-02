#!/usr/bin/env python3
"""Migrate ALL FAISS indices from Nomic (768d) to Harrier-27B (5376d).

Single GPU (port 9511) version. Designed to run while memory bridge is STOPPED
to prevent the bridge from overwriting migrated indices with 768d vectors.

Steps:
  1. Stop memory-bridge.service
  2. Run this script
  3. Update embedding-service to serve Harrier instead of Nomic
  4. Restart memory-bridge.service

Handles resume: checks each index dimension. Skips indices already at 5376d.
After main pass, runs chunking for any docs that failed (oversized).
"""

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import faiss
import numpy as np
import requests

FAISS_DIR = Path.home() / ".anaq" / "faiss"
BACKUP_DIR = FAISS_DIR / "backup_nomic_768d"
DB_PATH = FAISS_DIR / "metadata.db"

# Single server on GPU 1
SERVER = "http://localhost:9511"

INDEX_NAMES = [
    "SYSTEM", "SOLUTIONS", "BUSINESS", "MEDICAL", "AGENTS",
    "CODEBASE", "CONVERSATIONS", "SHARED", "OBSERVATIONS", "BEHAVIOURS",
]

CHUNK_SIZE = 20000   # chars — ~5000 tokens with margin
CHUNK_OVERLAP = 2000  # chars

NEW_DIM = 5376  # Harrier-27B


def get_embedding(text: str) -> list[float] | None:
    """Get embedding from Harrier server. Returns None on failure."""
    try:
        r = requests.post(
            f"{SERVER}/v1/embeddings",
            json={"input": text[:32000], "model": "harrier"},
            timeout=600,
        )
        r.raise_for_status()
        return r.json()["data"][0]["embedding"]
    except Exception as e:
        return None


def chunk_text(text: str) -> list[str]:
    """Split text into overlapping chunks for oversized docs."""
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


def needs_migration(index_name: str) -> bool:
    """Check if this index still needs migration."""
    idx_path = FAISS_DIR / f"{index_name}.index"
    if not idx_path.exists():
        return True
    idx = faiss.read_index(str(idx_path))
    if idx.d == NEW_DIM:
        print(f"  {index_name}: already at {NEW_DIM}d ({idx.ntotal} vectors) — SKIP")
        return False
    return True


def migrate_index(index_name: str, docs: list[tuple[int, str]], db: sqlite3.Connection):
    """Re-embed all docs for one index and write new FAISS index."""
    if not docs:
        idx = faiss.IndexFlatIP(NEW_DIM)
        faiss.write_index(idx, str(FAISS_DIR / f"{index_name}.index"))
        print(f"  {index_name}: empty index (0 vectors)")
        return 0, 0

    total = len(docs)
    vectors = []
    doc_ids = []
    failed_docs = []  # (doc_id, content) for chunking pass

    for i, (doc_id, content) in enumerate(docs):
        emb = get_embedding(content)

        if emb is None:
            # Try chunking
            chunks = chunk_text(content)
            if len(chunks) > 1:
                chunk_success = False
                for j, chunk in enumerate(chunks):
                    cemb = get_embedding(chunk)
                    if cemb is not None:
                        vectors.append(cemb)
                        doc_ids.append(doc_id)
                        chunk_success = True
                if not chunk_success:
                    failed_docs.append((doc_id, content))
                    print(f"  FAILED doc {doc_id} ({len(content)} chars, {len(chunks)} chunks all failed)")
            else:
                failed_docs.append((doc_id, content))
                print(f"  FAILED doc {doc_id} ({len(content)} chars)")
        else:
            vectors.append(emb)
            doc_ids.append(doc_id)

        # Progress every 50 docs
        if (i + 1) % 50 == 0 or i == total - 1:
            elapsed = time.time() - migrate_index._start
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            eta = (total - i - 1) / rate if rate > 0 else 0
            print(f"  [{index_name}] {i+1}/{total} ({rate:.1f} docs/sec, ETA {eta/60:.0f}m)")

    # Build FAISS index
    if vectors:
        vecs = np.array(vectors, dtype=np.float32)
        faiss.normalize_L2(vecs)
        idx = faiss.IndexFlatIP(NEW_DIM)
        idx.add(vecs)
    else:
        idx = faiss.IndexFlatIP(NEW_DIM)

    faiss.write_index(idx, str(FAISS_DIR / f"{index_name}.index"))

    # Update faiss_id mapping — each doc_id gets the position of its first vector
    cursor = db.cursor()
    seen_docs = set()
    for faiss_id, did in enumerate(doc_ids):
        if did not in seen_docs:
            cursor.execute("UPDATE documents SET faiss_id = ? WHERE id = ?", (faiss_id, did))
            seen_docs.add(did)
    db.commit()

    print(f"  {index_name}: {idx.ntotal} vectors at {NEW_DIM}d ({len(failed_docs)} failed)")
    return len(vectors), len(failed_docs)


migrate_index._start = time.time()


def main():
    start = time.time()
    print(f"=== FAISS Migration: Nomic (768d) → Harrier-27B ({NEW_DIM}d) ===")
    print(f"Server: {SERVER} (GPU 1 only)")
    print(f"Started: {datetime.now().isoformat()}")
    print()

    # Verify server
    try:
        emb = get_embedding("test")
        assert emb is not None and len(emb) == NEW_DIM
        print(f"Server OK — dimension confirmed: {len(emb)}")
    except Exception as e:
        print(f"Server FAILED: {e}")
        print("Start Harrier on GPU 1 first:")
        print("  GGML_VK_VISIBLE_DEVICES=1 llama-server --model harrier-27b.gguf --port 9511 --embeddings ...")
        return

    # Verify memory bridge is stopped
    import subprocess
    result = subprocess.run(
        ["systemctl", "--user", "is-active", "memory-bridge.service"],
        capture_output=True, text=True,
    )
    if result.stdout.strip() == "active":
        print("\nWARNING: memory-bridge.service is ACTIVE!")
        print("It will overwrite migrated indices with 768d vectors.")
        print("Stop it first: systemctl --user stop memory-bridge.service")
        print("Continuing anyway — but indices may get clobbered.")
        print()

    # Load all documents
    print("Loading documents from metadata.db...")
    db = sqlite3.connect(str(DB_PATH))
    cursor = db.cursor()

    total_migrated = 0
    total_failed = 0

    for index_name in INDEX_NAMES:
        if not needs_migration(index_name):
            continue

        cursor.execute(
            "SELECT id, content FROM documents WHERE index_name = ? ORDER BY id",
            (index_name,),
        )
        docs = cursor.fetchall()

        if not docs:
            # Still create empty 5376d index
            idx = faiss.IndexFlatIP(NEW_DIM)
            faiss.write_index(idx, str(FAISS_DIR / f"{index_name}.index"))
            print(f"  {index_name}: empty (0 docs)")
            continue

        print(f"\n[{index_name}] Migrating {len(docs)} documents...")
        migrate_index._start = time.time()
        migrated, failed = migrate_index(index_name, docs, db)
        total_migrated += migrated
        total_failed += failed

    db.close()

    elapsed = time.time() - start
    print(f"\n=== Migration complete ===")
    print(f"Total: {total_migrated} vectors created, {total_failed} failed")
    print(f"Dimension: {NEW_DIM}")
    print(f"Elapsed: {elapsed/3600:.1f} hours")
    print(f"\nNext steps:")
    print(f"  1. Update embedding-service to serve Harrier (or keep Nomic for search, Harrier for storage)")
    print(f"  2. Restart memory-bridge: systemctl --user restart memory-bridge.service")
    print(f"  3. Verify: curl http://127.0.0.1:9600/health")


if __name__ == "__main__":
    main()
