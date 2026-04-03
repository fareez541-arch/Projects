#!/usr/bin/env bash
# =============================================================================
# SYSTEM BACKUP — Critical data to /media/fareez541/BACKUP
# Run manually: plug in PNY PRO ELITE V2, mount, run this script, unmount, unplug
# =============================================================================
set -uo pipefail

BACKUP_ROOT="/media/fareez541/BACKUP"
TIMESTAMP=$(date +%Y-%m-%d_%H%M)
BACKUP_DIR="${BACKUP_ROOT}/backup_${TIMESTAMP}"
LOG="${BACKUP_ROOT}/backup_${TIMESTAMP}.log"

# --- Preflight ---
if ! mountpoint -q "$BACKUP_ROOT"; then
    echo "ERROR: $BACKUP_ROOT is not mounted. Mount the PNY drive first:"
    echo "  sudo mount -t exfat /dev/sdc1 /media/fareez541/BACKUP"
    exit 1
fi

AVAIL=$(df --output=avail "$BACKUP_ROOT" | tail -1)
if [ "$AVAIL" -lt 52428800 ]; then  # 50GB in KB
    echo "ERROR: Less than 50GB available on backup drive. Clean old backups first."
    exit 1
fi

echo "=== BACKUP STARTED: $(date) ===" | tee "$LOG"
echo "Target: $BACKUP_DIR" | tee -a "$LOG"
mkdir -p "$BACKUP_DIR"

# --- Rsync wrapper ---
sync_dir() {
    local src="$1"
    local label="$2"
    local dest="${BACKUP_DIR}/${label}"

    if [ ! -e "$src" ]; then
        echo "SKIP (not found): $src" | tee -a "$LOG"
        return
    fi

    echo "SYNC: $src → $label" | tee -a "$LOG"
    mkdir -p "$dest"
    rsync -rlt --no-perms --no-owner --no-group --info=progress2 "$src/" "$dest/" 2>&1 | tail -1 | tee -a "$LOG"
    echo "  DONE: $(du -sh "$dest" | cut -f1)" | tee -a "$LOG"
}

sync_file() {
    local src="$1"
    local dest="${BACKUP_DIR}/config"

    if [ ! -e "$src" ]; then
        echo "SKIP (not found): $src" | tee -a "$LOG"
        return
    fi

    mkdir -p "$dest"
    cp --no-preserve=mode,ownership "$src" "$dest/"
    echo "SYNC FILE: $src" | tee -a "$LOG"
}

# =============================================================================
# CRITICAL BUSINESS DATA
# =============================================================================
echo "" | tee -a "$LOG"
echo "--- CRITICAL BUSINESS DATA ---" | tee -a "$LOG"

sync_dir "$HOME/synlearns-core"      "synlearns-core"
sync_dir "$HOME/synlearns-backend"   "synlearns-backend"
sync_dir "$HOME/synlearns-hub"       "synlearns-hub"
sync_dir "$HOME/synlearns-video"     "synlearns-video"
sync_dir "$HOME/synlearns-failover"  "synlearns-failover"
sync_dir "$HOME/ccrn_workspace"      "ccrn_workspace"
sync_dir "$HOME/.anaq"               "anaq"
sync_dir "$HOME/.openclaw"           "openclaw"
sync_dir "$HOME/.antigravity"        "antigravity"

# =============================================================================
# CONFIGURATION & IDENTITY
# =============================================================================
echo "" | tee -a "$LOG"
echo "--- CONFIGURATION & IDENTITY ---" | tee -a "$LOG"

sync_dir "$HOME/.claude"             "claude"
sync_dir "$HOME/.nimah_memories"     "nimah_memories"
sync_dir "$HOME/hardware_control"    "hardware_control"
sync_dir "$HOME/.ssh"                "ssh"
sync_dir "$HOME/.gnupg"              "gnupg"

sync_file "$HOME/.gitconfig"
sync_file "$HOME/.git-credentials"
sync_file "$HOME/.bashrc"
sync_file "$HOME/.profile"
sync_file "$HOME/.tmux.conf"
sync_file "$HOME/.npmrc"

# =============================================================================
# WORKSPACE CONFIGS & SCRIPTS
# =============================================================================
echo "" | tee -a "$LOG"
echo "--- WORKSPACE CONFIGS ---" | tee -a "$LOG"

sync_dir "$HOME/vllm_workspace/config"                   "vllm_workspace/config"
sync_dir "$HOME/vllm_workspace/services"                 "vllm_workspace/services"
sync_dir "$HOME/vllm_workspace/bin"                      "vllm_workspace/bin"
sync_dir "$HOME/vllm_workspace/patches"                  "vllm_workspace/patches"
sync_dir "$HOME/vllm_workspace/ui"                       "vllm_workspace/ui"
sync_dir "$HOME/vllm_workspace/diagnostics_and_knowledge" "vllm_workspace/diagnostics_and_knowledge"
sync_dir "$HOME/vllm_workspace/pearl_training_data"      "vllm_workspace/pearl_training_data"
sync_dir "$HOME/vllm_workspace/replication_protocol"     "vllm_workspace/replication_protocol"

# =============================================================================
# AGENT SYSTEM
# =============================================================================
echo "" | tee -a "$LOG"
echo "--- AGENT SYSTEM ---" | tee -a "$LOG"

sync_dir "$HOME/agent-zero"          "agent-zero"
sync_dir "$HOME/.claude-code-router" "claude-code-router"
sync_dir "$HOME/.clawhub"            "clawhub"
sync_dir "$HOME/.synlearns"          "synlearns"
sync_dir "$HOME/.wacli"              "wacli"

# =============================================================================
# DOCUMENTS & CUSTOM SCRIPTS
# =============================================================================
echo "" | tee -a "$LOG"
echo "--- DOCUMENTS & SCRIPTS ---" | tee -a "$LOG"

sync_dir "$HOME/Documents"           "Documents"
sync_dir "$HOME/Desktop"             "Desktop"
sync_dir "$HOME/bin"                 "bin"

# =============================================================================
# SYSTEMD USER SERVICES
# =============================================================================
echo "" | tee -a "$LOG"
echo "--- SYSTEMD SERVICES ---" | tee -a "$LOG"

sync_dir "$HOME/.config/systemd"     "systemd"
sync_dir "$HOME/.streamlit"          "streamlit"
sync_dir "$HOME/.docker"             "docker"

# =============================================================================
# VLLM SOURCE (43+ unpushed commits of custom gfx1100 work)
# =============================================================================
echo "" | tee -a "$LOG"
echo "--- VLLM SOURCE (custom gfx1100 patches) ---" | tee -a "$LOG"

sync_dir "$HOME/vllm_source"         "vllm_source"

# =============================================================================
# WORKSPACE DOCS (markdown files at vllm_workspace root, not models/logs)
# =============================================================================
echo "" | tee -a "$LOG"
echo "--- WORKSPACE DOCS ---" | tee -a "$LOG"

mkdir -p "${BACKUP_DIR}/vllm_workspace/docs"
find "$HOME/vllm_workspace" -maxdepth 1 -name "*.md" -o -name "*.sh" -o -name "*.py" -o -name "*.env" -o -name "*.txt" | while read -r f; do
    cp --no-preserve=mode,ownership "$f" "${BACKUP_DIR}/vllm_workspace/docs/" 2>/dev/null || true
done
echo "  DONE: $(du -sh "${BACKUP_DIR}/vllm_workspace/docs" | cut -f1)" | tee -a "$LOG"

# =============================================================================
# CONDA ENV EXPORTS (for rebuilding environments)
# =============================================================================
echo "" | tee -a "$LOG"
echo "--- CONDA ENV EXPORTS ---" | tee -a "$LOG"

CONDA_DIR="${BACKUP_DIR}/conda_envs"
mkdir -p "$CONDA_DIR"

if command -v conda &>/dev/null; then
    for env in vllm comfy vllm-omni agent0 vllm-testbench; do
        if conda env list | grep -q "^${env} "; then
            conda env export -n "$env" --no-builds > "${CONDA_DIR}/${env}.yml" 2>/dev/null && \
                echo "  EXPORTED: $env" | tee -a "$LOG" || \
                echo "  FAILED: $env" | tee -a "$LOG"
        fi
    done
else
    echo "  conda not in PATH, skipping env exports" | tee -a "$LOG"
fi

# =============================================================================
# SUMMARY
# =============================================================================
echo "" | tee -a "$LOG"
echo "=== BACKUP COMPLETE: $(date) ===" | tee -a "$LOG"
TOTAL=$(du -sh "$BACKUP_DIR" | cut -f1)
echo "Total backup size: $TOTAL" | tee -a "$LOG"
echo "Location: $BACKUP_DIR" | tee -a "$LOG"
echo "" | tee -a "$LOG"

# List previous backups
echo "--- All backups on drive ---" | tee -a "$LOG"
ls -d "${BACKUP_ROOT}"/backup_* 2>/dev/null | while read -r d; do
    echo "  $(basename "$d"): $(du -sh "$d" | cut -f1)" | tee -a "$LOG"
done

# =============================================================================
# RETENTION — keep only the 5 most recent backups
# =============================================================================
echo "" | tee -a "$LOG"
echo "--- BACKUP RETENTION ---" | tee -a "$LOG"

BACKUP_DIRS=( $(ls -dt "${BACKUP_ROOT}"/backup_* 2>/dev/null) )
KEEP=5
if [ "${#BACKUP_DIRS[@]}" -gt "$KEEP" ]; then
    for old_backup in "${BACKUP_DIRS[@]:$KEEP}"; do
        echo "REMOVING old backup: $(basename "$old_backup")" | tee -a "$LOG"
        rm -rf "$old_backup"
        # Remove matching log file if it exists
        old_log="${old_backup}.log"
        [ -f "$old_log" ] && rm -f "$old_log"
    done
    echo "Retained $KEEP most recent backups, removed $(( ${#BACKUP_DIRS[@]} - KEEP ))" | tee -a "$LOG"
else
    echo "Only ${#BACKUP_DIRS[@]} backup(s) present, no cleanup needed (keep=$KEEP)" | tee -a "$LOG"
fi

echo ""
echo "NEXT: Unmount and disconnect the drive:"
echo "  sudo umount /media/fareez541/BACKUP"
