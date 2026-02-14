#!/bin/bash
# Hardware Environment Detection for CCRN Migration System
# Phase 2: System Prerequisites Check
set -euo pipefail

echo "=== CCRN Workspace Hardware Audit ==="
echo "Timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo ""

# Core System Detection
CPU_CORES=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo "unknown")
TOTAL_MEM_KB=$(grep MemTotal /proc/meminfo 2>/dev/null | awk '{print $2}' || echo "0")
TOTAL_MEM_GB=$((TOTAL_MEM_KB / 1024 / 1024))
DISK_AVAIL=$(df -BG . 2>/dev/null | tail -1 | awk '{print $4}' | tr -d 'G' || echo "0")

echo "[HARDWARE_PROFILE]"
echo "cpu_cores: $CPU_CORES"
echo "memory_gb: $TOTAL_MEM_GB"
echo "disk_available_gb: $DISK_AVAIL"
echo ""

# CCRN Processing Requirements Check
echo "[MIGRATION_READINESS]"
MIN_MEM=4 # GB
MIN_DISK=10 # GB

if [ "$TOTAL_MEM_GB" -ge "$MIN_MEM" ] && [ "$DISK_AVAIL" -ge "$MIN_DISK" ]; then
    echo "status: READY"
    echo "rationale: Sufficient resources for MISSION_MANDATE processing"
else
    echo "status: DEGRADED"
    echo "warning: Resources below optimal for nursing education data migration"
fi

echo ""
echo "[DEPENDENCY_CHECK]"
# Check for unzip capability (needed for migration_upload.zip)
if command -v unzip &> /dev/null; then
    echo "unzip: AVAILABLE"
else
    echo "unzip: MISSING (required for archive_ccrn_output/)"
fi

# Check for text processing (needed for rubrics/)
if command -v awk &> /dev/null; then
    echo "text_processing: AVAILABLE"
else
    echo "text_processing: LIMITED"
fi

echo ""
echo "[AGENT_CONTEXT]"
echo "workspace_root: $(pwd)"
echo "detected_categories: CORE_KNOWLEDGE, SYSTEM_SCRIPTS, AGENT_MEMORY"
echo "next_phase: ARCHIVE_EXTRACTION"

# GFX1100 Environment Configuration (Phase 2)
export HSA_OVERRIDE_GFX_VERSION=11.0.0
export HIP_VISIBLE_DEVICES=0,1
export ROCM_MAX_VRAM_PER_GPU=28000
