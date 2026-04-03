#!/bin/bash
# Overnight FAISS migration monitor + system documentation ingestion
# Runs via cron every hour. Self-disables after migration + ingestion complete.
set -euo pipefail

LOG=/tmp/harrier_overnight.log
MIGRATION_LOG=/tmp/harrier_migration.log
FAISS_DIR=~/.anaq/faiss
PYTHON=~/miniforge3/envs/agent0/bin/python3
LLAMA_SERVER=~/llama/llama.cpp/build-hip-v3/bin/llama-server
MODEL=~/vllm_workspace/models/harrier-27b-gguf/harrier-oss-v1-27b-Q4_K_M.gguf
HARRIER_PORT=9510

echo "=== $(date) — Overnight Monitor ===" >> "$LOG"

# 1. Check if Harrier server is alive
if ! curl -s "http://127.0.0.1:$HARRIER_PORT/health" --max-time 5 > /dev/null 2>&1; then
    echo "  Harrier DOWN — restarting on HIP GPU 0" >> "$LOG"
    HSA_OVERRIDE_GFX_VERSION=11.0.0 HIP_VISIBLE_DEVICES=0 \
    nohup $LLAMA_SERVER \
        --model "$MODEL" \
        --host 127.0.0.1 --port $HARRIER_PORT \
        -ngl 999 --embeddings \
        --threads 8 --parallel 4 \
        --batch-size 8192 --ubatch-size 8192 \
        --ctx-size 8192 \
        --split-mode none --main-gpu 0 \
        > /tmp/harrier_hip_gpu0.log 2>&1 &
    echo "  Harrier restarted PID: $!" >> "$LOG"
    sleep 60
fi

# 2. Check migration status
if pgrep -f "migrate_harrier" > /dev/null 2>&1; then
    LAST_LINE=$(tail -1 "$MIGRATION_LOG" 2>/dev/null || echo "no log")
    echo "  Migration RUNNING: $LAST_LINE" >> "$LOG"
    exit 0
fi

# 3. Migration not running — check if indices are all at 5376d
RESULT=$($PYTHON -c "
import faiss
from pathlib import Path
FAISS_DIR = Path.home() / '.anaq' / 'faiss'
names = ['SYSTEM','SOLUTIONS','BUSINESS','MEDICAL','AGENTS','CODEBASE','CONVERSATIONS','SHARED','OBSERVATIONS','BEHAVIOURS']
done = 0
for n in names:
    p = FAISS_DIR / f'{n}.index'
    if p.exists():
        idx = faiss.read_index(str(p))
        if idx.d == 5376:
            done += 1
        else:
            print(f'PENDING: {n} at {idx.d}d')
    else:
        print(f'MISSING: {n}')
print(f'MIGRATED: {done}/10')
" 2>&1)

echo "  Index status: $RESULT" >> "$LOG"

if echo "$RESULT" | grep -q "MIGRATED: 10/10"; then
    echo "  All indices migrated to 5376d" >> "$LOG"

    # Restart memory-bridge
    systemctl --user start memory-bridge.service 2>/dev/null || true
    sleep 3
    if systemctl --user is-active memory-bridge.service > /dev/null 2>&1; then
        echo "  memory-bridge: STARTED" >> "$LOG"
    else
        echo "  memory-bridge: FAILED TO START" >> "$LOG"
    fi

    # Restart embedding-service (needs Harrier, not Nomic now)
    # Don't restart — it still serves Nomic for incoming. Harrier is via llama-server.
    echo "  Migration COMPLETE. Services restored." >> "$LOG"

    # Ingest new system documentation
    echo "  Ingesting system documentation..." >> "$LOG"
    $PYTHON ~/.anaq/faiss/ingest_system_docs.py >> "$LOG" 2>&1

    # Now embed the new docs via nightly sync worker
    echo "  Running nightly sync for new docs..." >> "$LOG"

    # Launch Harrier if not running (monitor already checked above)
    # The sync worker expects ports 9510/9511 — update to just 9510
    sed -i 's|SERVERS = \["http://localhost:9510", "http://localhost:9511"\]|SERVERS = ["http://localhost:9510"]|' ~/.anaq/faiss/nightly_sync_worker.py 2>/dev/null || true
    $PYTHON -u ~/.anaq/faiss/nightly_sync_worker.py >> "$LOG" 2>&1

    # Harrier now runs as systemd service (harrier-embed.service) — do not kill
    echo "  Harrier running as systemd service — leaving up" >> "$LOG"

    # Remove this cron entry
    crontab -l 2>/dev/null | grep -v "overnight_monitor" | crontab -
    echo "  Cron self-removed." >> "$LOG"
else
    # Some indices still need migration — rerun
    echo "  Incomplete migration — rerunning..." >> "$LOG"
    cd "$FAISS_DIR" && nohup $PYTHON -u migrate_harrier_gpu1.py >> "$MIGRATION_LOG" 2>&1 &
    echo "  Migration restarted PID: $!" >> "$LOG"
fi

echo "=== Done ===" >> "$LOG"
