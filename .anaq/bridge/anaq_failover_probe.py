#!/usr/bin/env python3
"""
ANAQ Hive Mind — Failover Probe

When ANAQ (orchestration plane) goes down, Pearl can detect it and assume
temporary orchestrator duties by calling Opus 4.6 directly through the
Claude Code Bridge (port 5500).

Pearl normally runs on llama.cpp (unconstrained local inference). This probe
gives her a "seed" — a direct line to Opus — so she can step up as
sub-orchestrator until ANAQ is restored.

Architecture:
    Pearl (llama.cpp) → anaq_failover_probe → Bridge (5500) → Opus 4.6
                                             → writes ~/.anaq/failover/state.json
                                             → ANAQ reads state on recovery

Flow:
    1. Probe checks ANAQ health (memory_bridge:9600 scoring endpoint)
    2. If ANAQ unreachable for 2+ consecutive checks → FAILOVER_ACTIVE
    3. Pearl gets temporary orchestrator prompt injected
    4. Pearl routes through Bridge to Opus for quality-critical decisions
    5. When ANAQ recovers → FAILOVER_RESOLVED, Pearl resumes normal role
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ANAQ_HOME = Path.home() / ".anaq"
FAILOVER_DIR = ANAQ_HOME / "failover"
STATE_FILE = FAILOVER_DIR / "state.json"
MAINTENANCE_FILE = FAILOVER_DIR / "maintenance"
LOG_DIR = ANAQ_HOME / "logs"
LOG_FILE = LOG_DIR / "failover_probe.log"

# Endpoints to probe
# Check BOTH the OC gateway (ANAQ orchestrator) and memory bridge (data plane)
ANAQ_HEALTH_URL = "http://127.0.0.1:18789/health"
MEMORY_HEALTH_URL = "http://127.0.0.1:9600/health"
BRIDGE_HEALTH_URL = "http://127.0.0.1:5500/health"
BRIDGE_CHAT_URL = "http://127.0.0.1:5500/v1/chat/completions"

# Timing
PROBE_INTERVAL = 60  # Check every 60 seconds
FAILURE_THRESHOLD = 2  # 2 consecutive failures = failover
RECOVERY_THRESHOLD = 3  # 3 consecutive successes = recovery

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR.mkdir(parents=True, exist_ok=True)
FAILOVER_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("failover_probe")
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
# State management
# ---------------------------------------------------------------------------

STATES = ("NOMINAL", "DEGRADED", "FAILOVER_ACTIVE", "FAILOVER_RESOLVED", "MAINTENANCE")


def load_state() -> dict:
    """Load current failover state."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "status": "NOMINAL",
        "consecutive_failures": 0,
        "consecutive_recoveries": 0,
        "failover_started": None,
        "failover_ended": None,
        "pearl_is_orchestrator": False,
        "bridge_available": False,
        "last_check": None,
        "history": [],
    }


def save_state(state: dict):
    """Persist failover state."""
    STATE_FILE.write_text(json.dumps(state, indent=2))


def add_history(state: dict, event: str):
    """Append event to state history (keep last 50)."""
    state["history"].append({
        "event": event,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    state["history"] = state["history"][-50:]


# ---------------------------------------------------------------------------
# Maintenance mode — suppresses failure counting when no LLM is expected
# ---------------------------------------------------------------------------

def is_maintenance_mode() -> bool:
    """Check if maintenance flag file exists."""
    return MAINTENANCE_FILE.exists()


def exit_maintenance_mode(state: dict):
    """Remove maintenance flag and log the transition."""
    try:
        MAINTENANCE_FILE.unlink()
        logger.info("MAINTENANCE MODE exited — LLM detected, resuming normal monitoring")
        add_history(state, "Maintenance mode auto-exited — LLM came online")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

def check_anaq() -> bool:
    """Check if ANAQ orchestration plane is responsive.
    Checks OpenClaw gateway (where ANAQ actually runs) as primary,
    and memory bridge as secondary signal.
    """
    oc_ok = False
    mem_ok = False
    try:
        resp = httpx.get(ANAQ_HEALTH_URL, timeout=5.0)
        oc_ok = resp.status_code == 200
    except Exception:
        pass
    try:
        resp = httpx.get(MEMORY_HEALTH_URL, timeout=5.0)
        mem_ok = resp.status_code == 200
    except Exception:
        pass
    # ANAQ is "up" if OpenClaw is responding. Memory bridge is secondary.
    return oc_ok


def check_bridge() -> bool:
    """Check if Claude Code Bridge (Opus access) is available."""
    try:
        resp = httpx.get(BRIDGE_HEALTH_URL, timeout=5.0)
        return resp.status_code == 200
    except Exception:
        return False


def check_llm() -> bool:
    """Check if the primary LLM (llama-server/vLLM) is responding."""
    try:
        resp = httpx.get("http://127.0.0.1:8000/health", timeout=5.0)
        return resp.status_code == 200
    except Exception:
        return False


def trigger_repair(user_message: str = None) -> str:
    """Trigger the repair agent to diagnose and fix the system."""
    import subprocess
    try:
        cmd = ["/home/fareez541/miniforge3/envs/agent0/bin/python", str(Path.home() / ".anaq" / "bridge" / "repair_agent.py")]
        if user_message:
            cmd.append(user_message)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=200)
        return result.stdout.strip()
    except Exception as e:
        logger.error("Repair agent failed: %s", e)
        return f"Repair agent error: {e}"


# ---------------------------------------------------------------------------
# Pearl orchestrator seed
# ---------------------------------------------------------------------------

PEARL_ORCHESTRATOR_SEED = """
## EMERGENCY: Pearl Acting as Sub-Orchestrator

ANAQ is currently offline. You (Pearl) are temporarily assuming orchestrator
duties until ANAQ recovers. This is a FAILOVER state, not a promotion.

### Your temporary responsibilities:
1. **Route messages** to the correct shard/agent based on content
2. **Basic quality check** — reread your output before sending. Would ANAQ pass it?
3. **Log everything** — write to memory bridge so ANAQ can review when it recovers
4. **Do NOT score agents** — you don't have ANAQ's scoring authority
5. **Do NOT change system config** — that's Valkyrie/Main territory

### For quality-critical decisions, call Opus directly:
- Bridge endpoint: http://127.0.0.1:5500/v1/chat/completions
- Model: claude-opus-4-6
- Use this for: routing ambiguity, safety questions, anything you'd normally defer to ANAQ

### When ANAQ recovers:
- You will see status change to FAILOVER_RESOLVED
- Immediately relinquish orchestrator duties
- Resume normal Pearl operations
- ANAQ will review your failover decisions

Remember: You are a GUEST in ANAQ's chair. Keep it warm, don't redecorate.
""".strip()


def get_pearl_orchestrator_prompt() -> str:
    """Return the emergency orchestrator prompt for Pearl."""
    return PEARL_ORCHESTRATOR_SEED


def call_opus_for_pearl(message: str, system_prompt: str = None) -> str | None:
    """
    Pearl's direct line to Opus 4.6 via the Bridge.
    Used during failover for quality-critical routing decisions.
    Returns the response text, or None if bridge is down too.
    """
    payload = {
        "model": "claude-opus-4-6",
        "messages": [{"role": "user", "content": message}],
        "max_tokens": 2048,
    }
    if system_prompt:
        payload["messages"].insert(0, {"role": "system", "content": system_prompt})

    try:
        resp = httpx.post(BRIDGE_CHAT_URL, json=payload, timeout=120.0)
        if resp.status_code == 200:
            data = resp.json()
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error("Bridge call failed: %s", e)
    return None


# ---------------------------------------------------------------------------
# Main probe loop
# ---------------------------------------------------------------------------

def probe_cycle(state: dict) -> dict:
    """Run one probe cycle and update state."""
    now = datetime.now(timezone.utc).isoformat()
    state["last_check"] = now

    anaq_ok = check_anaq()
    bridge_ok = check_bridge()
    llm_ok = check_llm()
    state["bridge_available"] = bridge_ok
    state["llm_available"] = llm_ok

    maintenance = is_maintenance_mode()
    state["maintenance_mode"] = maintenance

    # --- LLM health check ---
    if llm_ok:
        # LLM is up — if we were in maintenance mode, auto-exit
        if maintenance:
            exit_maintenance_mode(state)
            maintenance = False
            state["maintenance_mode"] = False
        if state.get("consecutive_llm_failures", 0) > 0:
            logger.info("LLM recovered after %d failed checks", state.get("consecutive_llm_failures", 0))
            add_history(state, "LLM recovered")
        state["consecutive_llm_failures"] = 0
    elif maintenance:
        # LLM down but maintenance mode — no counting, no repair
        logger.debug("LLM offline (maintenance mode — no action)")
        state["consecutive_llm_failures"] = 0
    else:
        # LLM down, NOT maintenance — normal failure handling
        consecutive_llm_failures = state.get("consecutive_llm_failures", 0) + 1
        state["consecutive_llm_failures"] = consecutive_llm_failures

        if consecutive_llm_failures == 2:  # 2 consecutive failures = trigger repair
            logger.critical("LLM DOWN for %d checks — triggering repair agent", consecutive_llm_failures)
            add_history(state, f"LLM DOWN — triggering auto-repair (bridge={'UP' if bridge_ok else 'DOWN'})")
            repair_result = trigger_repair()
            logger.info("Repair result: %s", repair_result[:200])
            add_history(state, f"Repair result: {repair_result[:100]}")
        elif consecutive_llm_failures > 2:
            logger.warning("LLM still down after repair attempt (%d checks)", consecutive_llm_failures)

    # --- ANAQ orchestration health check ---
    current_status = state["status"]

    if anaq_ok:
        # ANAQ is up
        state["consecutive_failures"] = 0
        state["consecutive_recoveries"] = state.get("consecutive_recoveries", 0) + 1

        if current_status == "FAILOVER_ACTIVE":
            if state["consecutive_recoveries"] >= RECOVERY_THRESHOLD:
                # ANAQ recovered — end failover
                state["status"] = "FAILOVER_RESOLVED"
                state["pearl_is_orchestrator"] = False
                state["failover_ended"] = now
                add_history(state, "ANAQ recovered — failover resolved")
                logger.info("ANAQ RECOVERED — failover resolved after %d checks",
                           state["consecutive_recoveries"])
            else:
                logger.info("ANAQ responding (%d/%d for recovery)",
                           state["consecutive_recoveries"], RECOVERY_THRESHOLD)
        elif current_status == "FAILOVER_RESOLVED":
            # Back to nominal after one more clean cycle
            state["status"] = "NOMINAL"
            add_history(state, "System returned to NOMINAL")
            logger.info("System NOMINAL")
        else:
            state["status"] = "NOMINAL"
            logger.debug("ANAQ nominal")

    elif maintenance:
        # ANAQ down but maintenance mode — hold at NOMINAL, no counting
        logger.debug("ANAQ offline (maintenance mode — no action)")
        state["consecutive_failures"] = 0
        state["consecutive_recoveries"] = 0
        if current_status in ("FAILOVER_ACTIVE", "DEGRADED"):
            state["status"] = "MAINTENANCE"
            state["pearl_is_orchestrator"] = False
            state["failover_ended"] = now
            add_history(state, "Entered maintenance mode — suppressing failover")
            logger.info("Maintenance mode active — failover suppressed")
        elif current_status != "MAINTENANCE":
            state["status"] = "MAINTENANCE"

    else:
        # ANAQ is down — normal failure handling
        state["consecutive_recoveries"] = 0
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1

        if state["consecutive_failures"] >= FAILURE_THRESHOLD:
            if current_status != "FAILOVER_ACTIVE":
                # Enter failover
                state["status"] = "FAILOVER_ACTIVE"
                state["pearl_is_orchestrator"] = True
                state["failover_started"] = now
                state["failover_ended"] = None
                add_history(state, f"ANAQ DOWN — Pearl assuming orchestrator (bridge={'UP' if bridge_ok else 'DOWN'})")
                logger.critical(
                    "ANAQ FAILOVER ACTIVE — Pearl is now sub-orchestrator (bridge=%s)",
                    "available" if bridge_ok else "UNAVAILABLE"
                )
        else:
            state["status"] = "DEGRADED"
            add_history(state, f"ANAQ unreachable ({state['consecutive_failures']}/{FAILURE_THRESHOLD})")
            logger.warning("ANAQ unreachable (%d/%d for failover)",
                          state["consecutive_failures"], FAILURE_THRESHOLD)

    return state


def main():
    logger.info("=== ANAQ Failover Probe starting (interval: %ds) ===", PROBE_INTERVAL)
    state = load_state()

    while True:
        try:
            state = probe_cycle(state)
            save_state(state)
        except Exception as e:
            logger.exception("Probe cycle failed: %s", e)

        time.sleep(PROBE_INTERVAL)


if __name__ == "__main__":
    main()
