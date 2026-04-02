#!/usr/bin/env python3
"""
SynLearns D1 Sync — Export Postgres to SQLite for Cloudflare D1 failover.

Usage:
    python sync_d1.py export    # Postgres → SQLite file
    python sync_d1.py push      # SQLite → D1 (requires wrangler)
    python sync_d1.py full      # export + push

Content stays AES-256-GCM encrypted in D1 — the Worker decrypts at runtime.
UUIDs stored as TEXT. JSONB stored as JSON TEXT. BYTEA stored as BLOB (hex-encoded).
"""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

# ── Config ──────────────────────────────────────────────────────────
DB_HOST = "127.0.0.1"
DB_PORT = 5432
DB_NAME = "synlearns"
DB_USER = "sls"
DB_PASSWORD = os.environ.get("SLS_DB_PASSWORD", "")

EXPORT_DIR = Path("/tmp/sls-d1-sync")
SQLITE_PATH = EXPORT_DIR / "sls_failover.db"

D1_DATABASE_NAME = "sls-failover"

WRANGLER_DIR = Path(__file__).parent.parent.parent / "synlearns-failover"


def get_pg_conn():
    """Connect to local Postgres (exposed on 127.0.0.1:5432)."""
    if not DB_PASSWORD:
        # Try reading from .env
        env_path = Path(__file__).parent.parent / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("DB_PASSWORD="):
                    pw = line.split("=", 1)[1].strip().strip('"').strip("'")
                    return psycopg2.connect(
                        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
                        user=DB_USER, password=pw
                    )
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASSWORD
    )


def export_to_sqlite():
    """Export all Postgres tables to SQLite."""
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    if SQLITE_PATH.exists():
        SQLITE_PATH.unlink()

    pg = get_pg_conn()
    pg_cur = pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    sq = sqlite3.connect(str(SQLITE_PATH))
    sq.execute("PRAGMA journal_mode=WAL")
    sq.execute("PRAGMA foreign_keys=OFF")  # Bulk import

    print("[sync] Exporting Postgres → SQLite...")

    # ── Users ───────────────────────────────────────────────────────
    sq.execute("""
        CREATE TABLE users (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT,
            first_name TEXT,
            last_name TEXT,
            stripe_customer_id TEXT,
            tier INTEGER DEFAULT 0,
            account_status TEXT DEFAULT 'pending',
            activated_at TEXT,
            expires_at TEXT,
            extension_used INTEGER DEFAULT 0,
            device_slots TEXT DEFAULT '[]',
            fm_profile TEXT DEFAULT '{}',
            is_admin INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    pg_cur.execute("SELECT * FROM users")
    users = pg_cur.fetchall()
    for u in users:
        sq.execute(
            "INSERT INTO users VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(u["id"]), u["email"], u["password_hash"],
                u["first_name"], u["last_name"], u["stripe_customer_id"],
                u["tier"], u["account_status"],
                u["activated_at"].isoformat() if u["activated_at"] else None,
                u["expires_at"].isoformat() if u["expires_at"] else None,
                1 if u["extension_used"] else 0,
                json.dumps(u["device_slots"] or []),
                json.dumps(u["fm_profile"] or {}),
                1 if u["is_admin"] else 0,
                u["created_at"].isoformat() if u["created_at"] else None,
                u["updated_at"].isoformat() if u["updated_at"] else None,
            )
        )
    print(f"  users: {len(users)} rows")

    # ── Sessions ────────────────────────────────────────────────────
    sq.execute("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            device_fingerprint TEXT NOT NULL,
            access_token_jti TEXT UNIQUE NOT NULL,
            refresh_token_hash TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            expires_at TEXT NOT NULL,
            created_at TEXT
        )
    """)
    sq.execute("CREATE INDEX idx_sessions_jti ON sessions(access_token_jti)")
    sq.execute("CREATE INDEX idx_sessions_user ON sessions(user_id)")

    pg_cur.execute("SELECT * FROM sessions WHERE is_active = true")
    sessions = pg_cur.fetchall()
    for s in sessions:
        sq.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
            (
                str(s["id"]), str(s["user_id"]), s["device_fingerprint"],
                s["access_token_jti"], s["refresh_token_hash"],
                1 if s["is_active"] else 0,
                s["expires_at"].isoformat(),
                s["created_at"].isoformat() if s["created_at"] else None,
            )
        )
    print(f"  sessions: {len(sessions)} rows (active only)")

    # ── Questions ───────────────────────────────────────────────────
    sq.execute("""
        CREATE TABLE questions (
            id TEXT PRIMARY KEY,
            question_id TEXT UNIQUE NOT NULL,
            formula_id TEXT NOT NULL,
            domain TEXT NOT NULL,
            subdomain TEXT NOT NULL,
            difficulty TEXT NOT NULL,
            module_number INTEGER,
            stem TEXT NOT NULL,
            correct_answer TEXT NOT NULL,
            correct_rationale TEXT NOT NULL,
            gates_tested TEXT DEFAULT '[]',
            clinical_vignette INTEGER DEFAULT 1,
            distractors TEXT NOT NULL,
            fm_tags TEXT DEFAULT '[]'
        )
    """)
    sq.execute("CREATE INDEX idx_questions_qid ON questions(question_id)")
    sq.execute("CREATE INDEX idx_questions_domain ON questions(domain)")
    sq.execute("CREATE INDEX idx_questions_difficulty ON questions(difficulty)")
    sq.execute("CREATE INDEX idx_questions_module ON questions(module_number)")

    pg_cur.execute("SELECT * FROM questions")
    questions = pg_cur.fetchall()
    for q in questions:
        sq.execute(
            "INSERT INTO questions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(q["id"]), q["question_id"], q["formula_id"],
                q["domain"], q["subdomain"], q["difficulty"],
                q["module_number"], q["stem"], q["correct_answer"],
                q["correct_rationale"],
                json.dumps(q["gates_tested"] or []),
                1 if q["clinical_vignette"] else 0,
                json.dumps(q["distractors"]),
                json.dumps(q["fm_tags"] or []),
            )
        )
    print(f"  questions: {len(questions)} rows")

    # ── Course Modules ──────────────────────────────────────────────
    sq.execute("""
        CREATE TABLE course_modules (
            id TEXT PRIMARY KEY,
            module_number INTEGER UNIQUE NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            duration_hours REAL,
            section_count INTEGER DEFAULT 0,
            tier_required INTEGER DEFAULT 1,
            is_mandatory INTEGER DEFAULT 1,
            syllabus TEXT DEFAULT '[]'
        )
    """)
    sq.execute("CREATE INDEX idx_modules_num ON course_modules(module_number)")

    pg_cur.execute("SELECT * FROM course_modules")
    modules = pg_cur.fetchall()
    for m in modules:
        sq.execute(
            "INSERT INTO course_modules VALUES (?,?,?,?,?,?,?,?,?)",
            (
                str(m["id"]), m["module_number"], m["title"],
                m["description"], m["duration_hours"], m["section_count"],
                m["tier_required"], 1 if m["is_mandatory"] else 0,
                json.dumps(m["syllabus"] or []),
            )
        )
    print(f"  course_modules: {len(modules)} rows")

    # ── Content Chunks (encrypted blobs) ────────────────────────────
    sq.execute("""
        CREATE TABLE content_chunks (
            id TEXT PRIMARY KEY,
            module_id TEXT NOT NULL,
            module_number INTEGER NOT NULL,
            section_number INTEGER NOT NULL,
            subsection_number INTEGER DEFAULT 1,
            chunk_order INTEGER NOT NULL,
            title TEXT,
            encrypted_content BLOB NOT NULL,
            content_hash TEXT NOT NULL
        )
    """)
    sq.execute("CREATE INDEX idx_chunks_module ON content_chunks(module_number)")

    pg_cur.execute("SELECT * FROM content_chunks")
    chunks = pg_cur.fetchall()
    for c in chunks:
        # BYTEA comes as memoryview from psycopg2
        enc_bytes = bytes(c["encrypted_content"]) if c["encrypted_content"] else b""
        sq.execute(
            "INSERT INTO content_chunks VALUES (?,?,?,?,?,?,?,?,?)",
            (
                str(c["id"]), str(c["module_id"]), c["module_number"],
                c["section_number"], c["subsection_number"], c["chunk_order"],
                c["title"], enc_bytes, c["content_hash"],
            )
        )
    print(f"  content_chunks: {len(chunks)} rows")

    # ── Content Assets (encrypted blobs) ────────────────────────────
    sq.execute("""
        CREATE TABLE content_assets (
            id TEXT PRIMARY KEY,
            module_id TEXT,
            module_number INTEGER NOT NULL,
            section_number INTEGER,
            asset_type TEXT NOT NULL,
            display_name TEXT,
            display_order INTEGER DEFAULT 0,
            encrypted_content BLOB,
            asset_url TEXT,
            status TEXT DEFAULT 'available',
            content_hash TEXT
        )
    """)
    sq.execute("CREATE INDEX idx_assets_module ON content_assets(module_number)")

    pg_cur.execute("SELECT * FROM content_assets")
    assets = pg_cur.fetchall()
    for a in assets:
        enc_bytes = bytes(a["encrypted_content"]) if a["encrypted_content"] else None
        sq.execute(
            "INSERT INTO content_assets VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(a["id"]), str(a["module_id"]) if a["module_id"] else None,
                a["module_number"], a["section_number"], a["asset_type"],
                a["display_name"], a["display_order"],
                enc_bytes, a["asset_url"], a["status"], a["content_hash"],
            )
        )
    print(f"  content_assets: {len(assets)} rows")

    # ── Assessment Sessions ─────────────────────────────────────────
    sq.execute("""
        CREATE TABLE assessment_sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            assessment_type TEXT NOT NULL,
            module_number INTEGER,
            status TEXT DEFAULT 'in_progress',
            question_ids TEXT DEFAULT '[]',
            current_index INTEGER DEFAULT 0,
            current_band TEXT DEFAULT 'medium',
            answers TEXT DEFAULT '[]',
            fm_profile TEXT DEFAULT '{}',
            band_history TEXT DEFAULT '[]',
            score INTEGER DEFAULT 0,
            total_questions INTEGER DEFAULT 0,
            score_by_difficulty TEXT DEFAULT '{}',
            score_by_domain TEXT DEFAULT '{}',
            tier_assigned INTEGER,
            benchmark_report TEXT,
            started_at TEXT,
            completed_at TEXT
        )
    """)
    sq.execute("CREATE INDEX idx_assess_user ON assessment_sessions(user_id)")

    pg_cur.execute("SELECT * FROM assessment_sessions")
    assess = pg_cur.fetchall()
    for a in assess:
        sq.execute(
            "INSERT INTO assessment_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(a["id"]), str(a["user_id"]), a["assessment_type"],
                a["module_number"], a["status"],
                json.dumps(a["question_ids"] or []),
                a["current_index"], a["current_band"],
                json.dumps(a["answers"] or []),
                json.dumps(a["fm_profile"] or {}),
                json.dumps(a["band_history"] or []),
                a["score"], a["total_questions"],
                json.dumps(a["score_by_difficulty"] or {}),
                json.dumps(a["score_by_domain"] or {}),
                a["tier_assigned"],
                json.dumps(a["benchmark_report"]) if a["benchmark_report"] else None,
                a["started_at"].isoformat() if a["started_at"] else None,
                a["completed_at"].isoformat() if a["completed_at"] else None,
            )
        )
    print(f"  assessment_sessions: {len(assess)} rows")

    # ── User Progress ───────────────────────────────────────────────
    sq.execute("""
        CREATE TABLE user_progress (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            module_number INTEGER NOT NULL,
            status TEXT DEFAULT 'locked',
            completed_sections TEXT DEFAULT '{}',
            quiz_score INTEGER,
            quiz_total INTEGER,
            quiz_passed INTEGER,
            fm_weaknesses TEXT DEFAULT '[]',
            updated_at TEXT
        )
    """)
    sq.execute("CREATE INDEX idx_progress_user ON user_progress(user_id)")
    sq.execute("CREATE INDEX idx_progress_module ON user_progress(module_number)")

    pg_cur.execute("SELECT * FROM user_progress")
    progress = pg_cur.fetchall()
    for p in progress:
        sq.execute(
            "INSERT INTO user_progress VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                str(p["id"]), str(p["user_id"]), p["module_number"],
                p["status"], json.dumps(p["completed_sections"] or {}),
                p["quiz_score"], p["quiz_total"],
                1 if p["quiz_passed"] else (0 if p["quiz_passed"] is not None else None),
                json.dumps(p["fm_weaknesses"] or []),
                p["updated_at"].isoformat() if p["updated_at"] else None,
            )
        )
    print(f"  user_progress: {len(progress)} rows")

    # ── Metadata table for sync tracking ────────────────────────────
    sq.execute("""
        CREATE TABLE _sync_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    from datetime import datetime, timezone
    sq.execute(
        "INSERT INTO _sync_meta VALUES (?, ?)",
        ("last_sync", datetime.now(timezone.utc).isoformat())
    )
    sq.execute(
        "INSERT INTO _sync_meta VALUES (?, ?)",
        ("source", "postgres:synlearns@127.0.0.1:5432")
    )

    sq.execute("PRAGMA foreign_keys=ON")
    sq.commit()
    sq.close()
    pg.close()

    size = SQLITE_PATH.stat().st_size / (1024 * 1024)
    print(f"[sync] SQLite export complete: {size:.1f} MB at {SQLITE_PATH}")
    return SQLITE_PATH


def push_to_d1():
    """Push SQLite to D1 via wrangler."""
    if not SQLITE_PATH.exists():
        print("[sync] ERROR: No SQLite file. Run 'export' first.")
        sys.exit(1)

    cf_token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    cf_account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    if not cf_token or not cf_account:
        print("[sync] ERROR: Set CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID")
        sys.exit(1)

    env = {
        **os.environ,
        "CLOUDFLARE_API_TOKEN": cf_token,
        "CLOUDFLARE_ACCOUNT_ID": cf_account,
    }

    # Read the SQLite and generate SQL statements
    sq = sqlite3.connect(str(SQLITE_PATH))
    cursor = sq.cursor()

    # Get all table names
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row[0] for row in cursor.fetchall()]

    # Export as SQL
    sql_path = EXPORT_DIR / "d1_import.sql"
    with open(sql_path, "w") as f:
        for table in tables:
            # Schema
            cursor.execute(f"SELECT sql FROM sqlite_master WHERE type='table' AND name='{table}'")
            schema = cursor.fetchone()[0]
            f.write(f"DROP TABLE IF EXISTS {table};\n")
            f.write(f"{schema};\n\n")

        # Indexes
        cursor.execute("SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")
        for row in cursor.fetchall():
            f.write(f"{row[0]};\n")
        f.write("\n")

        # Data as INSERT statements
        for table in tables:
            cursor.execute(f"SELECT * FROM {table}")
            rows = cursor.fetchall()
            if not rows:
                continue
            cursor.execute(f"PRAGMA table_info({table})")
            cols = [col[1] for col in cursor.fetchall()]
            col_str = ", ".join(cols)
            for row in rows:
                vals = []
                for v in row:
                    if v is None:
                        vals.append("NULL")
                    elif isinstance(v, (int, float)):
                        vals.append(str(v))
                    elif isinstance(v, bytes):
                        vals.append(f"X'{v.hex()}'")
                    else:
                        escaped = str(v).replace("'", "''")
                        vals.append(f"'{escaped}'")
                f.write(f"INSERT INTO {table} ({col_str}) VALUES ({', '.join(vals)});\n")
            f.write("\n")

    sq.close()

    sql_size = sql_path.stat().st_size / (1024 * 1024)
    print(f"[sync] Generated D1 import SQL: {sql_size:.1f} MB")

    # Push to D1
    print(f"[sync] Pushing to D1 database '{D1_DATABASE_NAME}'...")

    # D1 has a 100KB limit per execute call, so we batch
    # Use wrangler d1 execute with --file
    result = subprocess.run(
        [
            "npx", "wrangler", "d1", "execute", D1_DATABASE_NAME,
            "--file", str(sql_path),
            "--remote",
        ],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(WRANGLER_DIR) if WRANGLER_DIR.exists() else None,
    )

    if result.returncode != 0:
        print(f"[sync] ERROR pushing to D1:")
        print(result.stderr)
        sys.exit(1)

    print(f"[sync] D1 push complete")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "full"

    if cmd == "export":
        export_to_sqlite()
    elif cmd == "push":
        push_to_d1()
    elif cmd == "full":
        export_to_sqlite()
        push_to_d1()
    else:
        print(f"Usage: {sys.argv[0]} [export|push|full]")
        sys.exit(1)
