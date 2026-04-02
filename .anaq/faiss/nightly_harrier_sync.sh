#!/bin/bash
# =============================================================================
# Nightly Harrier-27B FAISS Sync
# =============================================================================
# Spins up 2x Harrier-27B (one per GPU), embeds all pending SQLite docs
# (faiss_id = -1) into FAISS at 5376 dimensions, then shuts down.
#
# Designed to run via cron or systemd timer in the evening when GPUs are free.
#
# Usage:
#   bash ~/.anaq/faiss/nightly_harrier_sync.sh
#
# Prerequisites:
#   - No inference model running (both GPUs must be free)
#   - memory-bridge.service will be stopped during sync
# =============================================================================

set -euo pipefail

PYTHON=~/miniforge3/envs/agent0/bin/python3
LLAMA_SERVER=~/llama/llama.cpp/build-vulkan-v3/bin/llama-server
MODEL=~/vllm_workspace/models/harrier-27b-gguf/harrier-oss-v1-27b-Q4_K_M.gguf
LOGDIR=/tmp
SYNC_SCRIPT=~/.anaq/faiss/nightly_sync_worker.py

echo "=== Nightly Harrier Sync — $(date) ==="

# Check if anything is using the GPUs
if pgrep -f "llama-server|vllm" > /dev/null 2>&1; then
    echo "ERROR: Inference server is running. Stop it first or wait."
    echo "Running processes:"
    pgrep -a "llama-server|vllm" || true
    exit 1
fi

# Check how many pending docs exist
PENDING=$($PYTHON -c "
import sqlite3, os
db = sqlite3.connect(os.path.expanduser('~/.anaq/faiss/metadata.db'))
c = db.cursor()
c.execute('SELECT COUNT(*) FROM documents WHERE faiss_id = -1')
print(c.fetchone()[0])
db.close()
")

echo "Pending docs to embed: $PENDING"

if [ "$PENDING" -eq 0 ]; then
    echo "Nothing to sync. Exiting."
    exit 0
fi

# Stop memory bridge to prevent concurrent writes
echo "Stopping memory-bridge..."
systemctl --user stop memory-bridge.service 2>/dev/null || true
sleep 2

# Launch Harrier on both GPUs
echo "Starting Harrier-27B on GPU 0 (port 9510)..."
VK_DRIVER_FILES=/usr/share/vulkan/icd.d/radeon_icd.x86_64.json \
GGML_VK_VISIBLE_DEVICES=0 \
$LLAMA_SERVER \
    --model "$MODEL" \
    --host 127.0.0.1 --port 9510 \
    -ngl 999 --embeddings \
    --threads 8 --parallel 4 \
    --batch-size 8192 --ubatch-size 8192 \
    --ctx-size 8192 \
    > "$LOGDIR/harrier_sync_gpu0.log" 2>&1 &
GPU0_PID=$!

echo "Starting Harrier-27B on GPU 1 (port 9511)..."
VK_DRIVER_FILES=/usr/share/vulkan/icd.d/radeon_icd.x86_64.json \
GGML_VK_VISIBLE_DEVICES=1 \
$LLAMA_SERVER \
    --model "$MODEL" \
    --host 127.0.0.1 --port 9511 \
    -ngl 999 --embeddings \
    --threads 8 --parallel 4 \
    --batch-size 8192 --ubatch-size 8192 \
    --ctx-size 8192 \
    > "$LOGDIR/harrier_sync_gpu1.log" 2>&1 &
GPU1_PID=$!

echo "Waiting for models to load..."
sleep 90

# Verify both servers
for port in 9510 9511; do
    for attempt in 1 2 3 4 5; do
        if curl -s "http://127.0.0.1:$port/v1/embeddings" \
            -d '{"input":"test","model":"harrier"}' \
            -H "Content-Type: application/json" \
            --max-time 30 > /dev/null 2>&1; then
            echo "  Port $port: OK"
            break
        fi
        echo "  Port $port: attempt $attempt failed, waiting 30s..."
        sleep 30
    done
done

# Run the sync
echo ""
echo "=== Starting sync worker ==="
$PYTHON -u "$SYNC_SCRIPT" 2>&1 | tee "$LOGDIR/harrier_nightly_sync.log"
SYNC_EXIT=$?

# Cleanup: kill servers
echo ""
echo "Stopping Harrier servers..."
kill $GPU0_PID $GPU1_PID 2>/dev/null
wait $GPU0_PID $GPU1_PID 2>/dev/null || true

# Restart memory bridge
echo "Restarting memory-bridge..."
systemctl --user start memory-bridge.service
sleep 2
systemctl --user is-active memory-bridge.service && echo "Memory bridge: OK" || echo "Memory bridge: FAILED TO START"

echo ""
echo "=== Nightly sync complete — $(date) ==="
exit $SYNC_EXIT
