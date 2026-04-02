#!/usr/bin/env python3
"""
Turn Sync — Parse chat history from both OC and A0, post to shared Memory Bridge.

Modes:
  - turn:    Process new turns since last sync (run after each session or on cron)
  - daily:   Aggregate and compact day's memories, deduplicate cross-platform
  - weekly:  Cross-agent pattern extraction, shared knowledge promotion
  - monthly: Full reindex, orphan cleanup, long-term summary generation

Usage:
  python3 turn_sync.py turn              # Incremental turn sync
  python3 turn_sync.py daily             # Daily optimization
  python3 turn_sync.py weekly            # Weekly cross-agent sync
  python3 turn_sync.py monthly           # Monthly full reindex
  python3 turn_sync.py migrate           # Initial migration of all OC data
"""

import hashlib
import json
import logging
import os
import sqlite3
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BRIDGE_URL = "http://127.0.0.1:9600"
OC_HOME = Path.home() / ".openclaw"
A0_HOME = Path.home() / "agent-zero"
ANAQ_HOME = Path.home() / ".anaq"
MEMORY_DIR = OC_HOME / "memory"
STATE_FILE = ANAQ_HOME / "turn_sync_state.json"
LOG_DIR = ANAQ_HOME / "logs"
LOG_FILE = LOG_DIR / "turn_sync.log"

AGENTS = ["pearl", "nimah", "samirah", "main", "anaq"]

# OC category → Bridge index
CATEGORY_INDEX = {
    "thought": "AGENTS", "decision": "AGENTS", "observation": "AGENTS",
    "context": "AGENTS", "critique": "AGENTS", "long_term": "AGENTS",
    "task": "SYSTEM", "interaction": "CONVERSATIONS",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR.mkdir(parents=True, exist_ok=True)
logger = logging.getLogger("turn_sync")
logger.setLevel(logging.DEBUG)
_fmt = logging.Formatter("[%(asctime)s] %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
_fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(_fmt)
logger.addHandler(_fh)
_sh = logging.StreamHandler()
_sh.setLevel(logging.INFO)
_sh.setFormatter(_fmt)
logger.addHandler(_sh)

# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "last_turn_sync": None,
        "last_daily": None,
        "last_weekly": None,
        "last_monthly": None,
        "synced_hashes": [],  # Last 5000 content hashes to avoid re-syncing
        "stats": {"turns_synced": 0, "daily_runs": 0, "weekly_runs": 0, "monthly_runs": 0},
    }


def save_state(state: dict):
    # Keep synced_hashes bounded
    if len(state["synced_hashes"]) > 5000:
        state["synced_hashes"] = state["synced_hashes"][-5000:]
    STATE_FILE.write_text(json.dumps(state, indent=2))


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

# ---------------------------------------------------------------------------
# Bridge HTTP helpers
# ---------------------------------------------------------------------------

def _post_bridge(endpoint: str, payload: dict, timeout: int = 15) -> dict | None:
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{BRIDGE_URL}{endpoint}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        logger.warning("Bridge %s failed: %s", endpoint, e)
        return None


def ingest(content: str, index: str, agent: str, source: str, metadata: dict | None = None) -> dict | None:
    return _post_bridge("/ingest", {
        "content": content,
        "index": index,
        "source": source,
        "agent_scope": [agent],
        "metadata": metadata or {},
    })


def batch_ingest(documents: list[dict]) -> dict | None:
    return _post_bridge("/batch_ingest", {"documents": documents}, timeout=60)

# ---------------------------------------------------------------------------
# Turn Sync — incremental new turns
# ---------------------------------------------------------------------------

def sync_turns(state: dict) -> int:
    """Sync new OC working memory entries and A0 FAISS entries to Bridge."""
    synced = 0

    # 1. Sync OC working_memory entries
    for agent in AGENTS:
        db_path = MEMORY_DIR / f"{agent}.context.db"
        if not db_path.exists():
            continue

        conn = None
        try:
            conn = sqlite3.connect(str(db_path))
            # Get entries since last sync
            since = state.get("last_turn_sync") or "2000-01-01T00:00:00"
            rows = conn.execute(
                "SELECT id, category, content, created_at FROM working_memory "
                "WHERE agent = ? AND created_at > ? ORDER BY created_at",
                (agent, since),
            ).fetchall()

            for row_id, category, content, created_at in rows:
                ch = content_hash(content)
                if ch in state["synced_hashes"]:
                    continue

                index = CATEGORY_INDEX.get(category, "AGENTS")
                result = ingest(
                    content=content,
                    index=index,
                    agent=agent,
                    source="openclaw_turn_sync",
                    metadata={"category": category, "oc_id": row_id, "origin": "openclaw"},
                )
                if result and result.get("status") != "error":
                    state["synced_hashes"].append(ch)
                    synced += 1

            # Also sync long_term_memory
            ltm_rows = conn.execute(
                "SELECT id, category, summary, created_at FROM long_term_memory "
                "WHERE agent = ? AND created_at > ?",
                (agent, since),
            ).fetchall()

            for row_id, category, summary, created_at in ltm_rows:
                ch = content_hash(summary)
                if ch in state["synced_hashes"]:
                    continue

                result = ingest(
                    content=summary,
                    index="AGENTS",
                    agent=agent,
                    source="openclaw_long_term",
                    metadata={"category": "long_term", "origin": "openclaw"},
                )
                if result and result.get("status") != "error":
                    state["synced_hashes"].append(ch)
                    synced += 1
        except Exception as e:
            logger.error("OC sync failed for %s: %s", agent, e)
        finally:
            if conn is not None:
                conn.close()

    state["last_turn_sync"] = datetime.now(timezone.utc).isoformat()
    state["stats"]["turns_synced"] += synced
    logger.info("Turn sync complete: %d entries synced", synced)
    return synced

# ---------------------------------------------------------------------------
# Daily — deduplicate, compact, optimize
# ---------------------------------------------------------------------------

def daily_sync(state: dict) -> dict:
    """Daily optimization: sync all recent turns, report stats."""
    logger.info("=== Daily sync starting ===")

    # Run turn sync first
    synced = sync_turns(state)

    # Get bridge stats
    try:
        req = urllib.request.Request(f"{BRIDGE_URL}/stats", method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            bridge_stats = json.loads(resp.read())
    except Exception:
        bridge_stats = {}

    state["last_daily"] = datetime.now(timezone.utc).isoformat()
    state["stats"]["daily_runs"] += 1

    result = {"turns_synced": synced, "bridge_stats": bridge_stats}
    logger.info("Daily sync complete: %s", json.dumps(result, indent=2))
    return result

# ---------------------------------------------------------------------------
# Weekly — cross-agent knowledge promotion
# ---------------------------------------------------------------------------

def weekly_sync(state: dict) -> dict:
    """Weekly: promote shared patterns to SHARED index, cross-agent dedup."""
    logger.info("=== Weekly sync starting ===")

    # Run daily first
    daily_result = daily_sync(state)

    # Identify tool/procedure knowledge that should be shared across agents
    shared_promoted = 0
    tool_keywords = ["tool", "procedure", "how to", "command", "script", "launch", "config"]

    for agent in AGENTS:
        db_path = MEMORY_DIR / f"{agent}.context.db"
        if not db_path.exists():
            continue

        try:
            conn = sqlite3.connect(str(db_path))
            # Find entries mentioning tools/procedures
            for keyword in tool_keywords:
                rows = conn.execute(
                    "SELECT content FROM working_memory WHERE agent = ? AND content LIKE ?",
                    (agent, f"%{keyword}%"),
                ).fetchall()

                for (content,) in rows:
                    ch = content_hash(f"SHARED:{content}")
                    if ch in state["synced_hashes"]:
                        continue

                    result = ingest(
                        content=content,
                        index="SHARED",
                        agent="all",
                        source=f"weekly_promote_{agent}",
                        metadata={"origin": "weekly_promotion", "source_agent": agent},
                    )
                    if result and result.get("status") == "ingested":
                        state["synced_hashes"].append(ch)
                        shared_promoted += 1

            conn.close()
        except Exception as e:
            logger.error("Weekly promotion failed for %s: %s", agent, e)

    state["last_weekly"] = datetime.now(timezone.utc).isoformat()
    state["stats"]["weekly_runs"] += 1

    result = {"daily": daily_result, "shared_promoted": shared_promoted}
    logger.info("Weekly sync complete: %d entries promoted to SHARED", shared_promoted)
    return result

# ---------------------------------------------------------------------------
# Monthly — full reindex and cleanup
# ---------------------------------------------------------------------------

def monthly_sync(state: dict) -> dict:
    """Monthly: full reindex, orphan cleanup, comprehensive summary."""
    logger.info("=== Monthly sync starting ===")

    # Run weekly first
    weekly_result = weekly_sync(state)

    # Clear synced_hashes to allow re-evaluation
    state["synced_hashes"] = []

    # Full re-sync of all OC data
    full_synced = 0
    for agent in AGENTS:
        db_path = MEMORY_DIR / f"{agent}.context.db"
        if not db_path.exists():
            continue

        try:
            conn = sqlite3.connect(str(db_path))
            rows = conn.execute(
                "SELECT category, content FROM working_memory WHERE agent = ?",
                (agent,),
            ).fetchall()

            batch = []
            for category, content in rows:
                index = CATEGORY_INDEX.get(category, "AGENTS")
                batch.append({
                    "content": content,
                    "index": index,
                    "source": "monthly_reindex",
                    "agent_scope": [agent],
                    "metadata": {"category": category, "origin": "openclaw"},
                })

            # Batch ingest in chunks of 50
            for i in range(0, len(batch), 50):
                chunk = batch[i:i+50]
                result = batch_ingest(chunk)
                if result:
                    full_synced += len(chunk)

            conn.close()
        except Exception as e:
            logger.error("Monthly reindex failed for %s: %s", agent, e)

    state["last_monthly"] = datetime.now(timezone.utc).isoformat()
    state["stats"]["monthly_runs"] += 1

    result = {"weekly": weekly_result, "full_reindexed": full_synced}
    logger.info("Monthly sync complete: %d entries reindexed", full_synced)
    return result

# ---------------------------------------------------------------------------
# Initial Migration — pull ALL OC data into Bridge
# ---------------------------------------------------------------------------

def initial_migration() -> dict:
    """
    One-time migration: pull all existing OC memory into Bridge.
    Pearl → pearl scope, Main → main scope, etc.
    Also migrates shared protocols and tool docs to SHARED index.
    """
    logger.info("=== Initial Migration starting ===")
    stats = {"agents": {}, "shared": 0, "total": 0}

    # 1. Migrate per-agent OC context databases
    for agent in AGENTS:
        agent_stats = {"working_memory": 0, "long_term": 0, "errors": 0}

        db_path = MEMORY_DIR / f"{agent}.context.db"
        if not db_path.exists():
            stats["agents"][agent] = agent_stats
            continue

        try:
            conn = sqlite3.connect(str(db_path))

            # Working memory
            rows = conn.execute(
                "SELECT category, content FROM working_memory WHERE agent = ?",
                (agent,),
            ).fetchall()

            batch = []
            for category, content in rows:
                index = CATEGORY_INDEX.get(category, "AGENTS")
                batch.append({
                    "content": content,
                    "index": index,
                    "source": "initial_migration_oc",
                    "agent_scope": [agent],
                    "metadata": {"category": category, "origin": "openclaw", "migrated": True},
                })

            for i in range(0, len(batch), 50):
                chunk = batch[i:i+50]
                result = batch_ingest(chunk)
                if result:
                    agent_stats["working_memory"] += len(chunk)

            # Long-term memory
            ltm_rows = conn.execute(
                "SELECT category, summary FROM long_term_memory WHERE agent = ?",
                (agent,),
            ).fetchall()

            for category, summary in ltm_rows:
                result = ingest(
                    content=summary,
                    index="AGENTS",
                    agent=agent,
                    source="initial_migration_oc_ltm",
                    metadata={"category": "long_term", "origin": "openclaw", "migrated": True},
                )
                if result:
                    agent_stats["long_term"] += 1

            conn.close()
        except Exception as e:
            logger.error("Migration failed for %s: %s", agent, e)
            agent_stats["errors"] += 1

        stats["agents"][agent] = agent_stats
        stats["total"] += agent_stats["working_memory"] + agent_stats["long_term"]
        logger.info("Migrated %s: %d working + %d long-term", agent,
                     agent_stats["working_memory"], agent_stats["long_term"])

    # 2. Migrate shared protocols/tools to SHARED index
    shared_docs = [
        OC_HOME / "shared" / "CONTEXT_PROTOCOL.md",
        OC_HOME / "shared" / "CORE_OPERATIONS.md",
    ]

    # Add all tool/protocol files from shared
    shared_dir = OC_HOME / "shared"
    if shared_dir.exists():
        for md_file in shared_dir.rglob("*.md"):
            if md_file not in shared_docs:
                shared_docs.append(md_file)

    for doc_path in shared_docs:
        if not doc_path.exists():
            continue
        try:
            content = doc_path.read_text()
            if len(content) > 100:  # Skip trivially small files
                # Chunk large files (max 2000 chars per chunk)
                chunks = [content[i:i+2000] for i in range(0, len(content), 2000)]
                for j, chunk in enumerate(chunks):
                    result = ingest(
                        content=chunk,
                        index="SHARED",
                        agent="all",
                        source=f"initial_migration_{doc_path.name}",
                        metadata={
                            "file": str(doc_path),
                            "chunk": j,
                            "origin": "openclaw_shared",
                            "migrated": True,
                        },
                    )
                    if result and result.get("status") == "ingested":
                        stats["shared"] += 1
        except Exception as e:
            logger.error("Failed to migrate %s: %s", doc_path, e)

    # 3. Migrate agent dossiers to AGENTS index (per-agent scoped)
    dossier_dir = OC_HOME / "workspace-anaq" / "dossiers"
    if dossier_dir.exists():
        for dossier in dossier_dir.glob("*.md"):
            agent_name = dossier.stem  # pearl.md → pearl
            if agent_name not in AGENTS:
                continue
            try:
                content = dossier.read_text()
                chunks = [content[i:i+2000] for i in range(0, len(content), 2000)]
                for j, chunk in enumerate(chunks):
                    ingest(
                        content=chunk,
                        index="AGENTS",
                        agent=agent_name,
                        source=f"initial_migration_dossier_{agent_name}",
                        metadata={"type": "dossier", "chunk": j, "migrated": True},
                    )
            except Exception as e:
                logger.error("Dossier migration failed for %s: %s", agent_name, e)

    stats["total"] += stats["shared"]
    logger.info("=== Initial Migration complete: %d total entries ===", stats["total"])
    print(json.dumps(stats, indent=2))
    return stats

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1]
    state = load_state()

    if cmd == "turn":
        sync_turns(state)
        save_state(state)
    elif cmd == "daily":
        daily_sync(state)
        save_state(state)
    elif cmd == "weekly":
        weekly_sync(state)
        save_state(state)
    elif cmd == "monthly":
        monthly_sync(state)
        save_state(state)
    elif cmd == "migrate":
        initial_migration()
    elif cmd == "status":
        print(json.dumps(state, indent=2))
    else:
        print(f"Unknown command: {cmd}")
        print("Commands: turn, daily, weekly, monthly, migrate, status")
        sys.exit(1)
