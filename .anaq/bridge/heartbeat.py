#!/usr/bin/env python3
"""
ANAQ Hive Mind — Heartbeat Monitor
Lightweight always-on system health monitor. No Claude API usage.
Checks GPU temps, VRAM, disk, service health. Logs + alerts.
"""

import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LOG_DIR = Path.home() / ".anaq" / "logs"
LOG_FILE = LOG_DIR / "heartbeat.log"
ALERTS_DIR = Path.home() / ".anaq" / "alerts"
PENDING_ALERTS = ALERTS_DIR / "pending.json"
CHECK_INTERVAL = 300  # 5 minutes

# Thresholds
GPU_TEMP_WARNING = 85
GPU_TEMP_CRITICAL = 95
VRAM_PCT_WARNING = 90
DISK_PCT_WARNING = 85

# Service health endpoints
SERVICES = {
    "bridge": "http://localhost:5500/health",
    "memory_bridge": "http://localhost:9600/health",
    "embedding": "http://localhost:9500/health",
    "agent_zero": "http://localhost:5000",
    "openclaw": "http://localhost:18789/health",
}

# Alert cooldowns (seconds)
ALERT_COOLDOWNS: dict[str, float] = {}
COOLDOWN_PERIOD = 3600  # 1 hour per alert type

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR.mkdir(parents=True, exist_ok=True)
ALERTS_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("heartbeat")
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
# Checks
# ---------------------------------------------------------------------------

def check_gpu() -> list[dict]:
    """Check GPU temps and VRAM via rocm-smi."""
    alerts = []
    try:
        result = subprocess.run(
            ["rocm-smi", "--showtemp", "--showmeminfo", "vram", "--json"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            alerts.append({"level": "WARNING", "msg": f"rocm-smi failed: {result.stderr[:200]}"})
            return alerts

        data = json.loads(result.stdout)
        for card_key, card_data in data.items():
            if not card_key.startswith("card"):
                continue

            # Temperature
            temp = None
            for key, val in card_data.items():
                if "temperature" in key.lower() and "edge" in key.lower():
                    try:
                        temp = float(val)
                    except (ValueError, TypeError):
                        pass

            if temp is not None:
                if temp >= GPU_TEMP_CRITICAL:
                    alerts.append({
                        "level": "CRITICAL",
                        "msg": f"{card_key} temp {temp}°C >= {GPU_TEMP_CRITICAL}°C CRITICAL",
                    })
                elif temp >= GPU_TEMP_WARNING:
                    alerts.append({
                        "level": "WARNING",
                        "msg": f"{card_key} temp {temp}°C >= {GPU_TEMP_WARNING}°C",
                    })
                else:
                    logger.debug("%s temp: %.0f°C", card_key, temp)

            # VRAM
            vram_used = card_data.get("VRAM Total Used Memory (B)")
            vram_total = card_data.get("VRAM Total Memory (B)")
            if vram_used and vram_total:
                try:
                    used = int(vram_used)
                    total = int(vram_total)
                    pct = (used / total) * 100 if total > 0 else 0
                    if pct >= VRAM_PCT_WARNING:
                        alerts.append({
                            "level": "WARNING",
                            "msg": f"{card_key} VRAM {pct:.0f}% used ({used // (1024**2)}MB / {total // (1024**2)}MB)",
                        })
                    else:
                        logger.debug("%s VRAM: %.0f%%", card_key, pct)
                except (ValueError, TypeError):
                    pass

    except FileNotFoundError:
        alerts.append({"level": "WARNING", "msg": "rocm-smi not found"})
    except subprocess.TimeoutExpired:
        alerts.append({"level": "WARNING", "msg": "rocm-smi timed out"})
    except json.JSONDecodeError:
        # Try non-JSON fallback
        logger.debug("rocm-smi JSON parse failed, skipping GPU check")

    return alerts


def check_disk() -> list[dict]:
    """Check disk usage."""
    alerts = []
    try:
        result = subprocess.run(
            ["df", "-h", "/"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 5:
                pct_str = parts[4].rstrip("%")
                try:
                    pct = int(pct_str)
                except ValueError:
                    logger.debug("Skipping non-numeric disk usage: %s", pct_str)
                    continue
                if pct >= DISK_PCT_WARNING:
                    alerts.append({
                        "level": "WARNING",
                        "msg": f"Disk usage {pct}% on / ({parts[3]} available)",
                    })
                else:
                    logger.debug("Disk usage: %d%%", pct)
    except Exception as e:
        alerts.append({"level": "WARNING", "msg": f"Disk check failed: {e}"})

    return alerts


def check_services() -> list[dict]:
    """Check service health endpoints."""
    alerts = []
    for name, url in SERVICES.items():
        try:
            resp = httpx.get(url, timeout=5.0)
            if resp.status_code == 200:
                logger.debug("Service %s: OK", name)
            else:
                alerts.append({
                    "level": "WARNING",
                    "msg": f"Service {name} returned HTTP {resp.status_code}",
                })
        except httpx.ConnectError:
            logger.debug("Service %s: not running", name)
            # Only alert for critical services
            if name in ("bridge", "embedding"):
                alerts.append({
                    "level": "WARNING",
                    "msg": f"Service {name} not reachable at {url}",
                })
        except Exception as e:
            logger.debug("Service %s check error: %s", name, e)

    return alerts


# ---------------------------------------------------------------------------
# Alert handling
# ---------------------------------------------------------------------------

def should_alert(alert_key: str) -> bool:
    """Check cooldown for this alert type."""
    last = ALERT_COOLDOWNS.get(alert_key, 0)
    if time.time() - last < COOLDOWN_PERIOD:
        return False
    ALERT_COOLDOWNS[alert_key] = time.time()
    return True


def write_alert_queue(alerts: list[dict]):
    """
    Write alerts to ~/.anaq/alerts/pending.json for downstream consumers.

    Flow: Heartbeat (sensor) → pending.json → Valkyrie (reads + acts)
                                             → ANAQ (reads + scores Valkyrie)

    Heartbeat NEVER calls LLM or OC directly. It only observes and writes.
    """
    now = datetime.now(timezone.utc).isoformat()

    # Load existing pending alerts
    pending = []
    if PENDING_ALERTS.exists():
        try:
            pending = json.loads(PENDING_ALERTS.read_text())
        except (json.JSONDecodeError, OSError):
            pending = []

    # Append new alerts with metadata — skip if still within cooldown window
    for alert in alerts:
        if not should_alert(alert["msg"]):
            logger.debug("Alert suppressed (cooldown): %s", alert["msg"])
            continue
        pending.append({
            "id": f"{now}_{alert['level']}_{hash(alert['msg']) & 0xFFFF:04x}",
            "level": alert["level"],
            "msg": alert["msg"],
            "source": "heartbeat",
            "timestamp": now,
            "acknowledged": False,
            "acted_on": False,
        })

    # Prune old acknowledged alerts (keep last 200, drop acknowledged older than 24h)
    cutoff = time.time() - 86400
    pruned = []
    for a in pending:
        try:
            ts = datetime.fromisoformat(a["timestamp"]).timestamp()
        except (ValueError, KeyError):
            ts = time.time()
        if not a.get("acknowledged") or ts > cutoff:
            pruned.append(a)
    pruned = pruned[-200:]  # Hard cap

    PENDING_ALERTS.write_text(json.dumps(pruned, indent=2))
    logger.debug("Wrote %d alerts to pending queue (%d new)", len(pruned), len(alerts))


def process_alerts(alerts: list[dict]):
    """Log alerts and write to alert queue for Valkyrie consumption."""
    for alert in alerts:
        level = alert["level"]
        msg = alert["msg"]

        if level == "CRITICAL":
            logger.critical(msg)
        elif level == "WARNING":
            logger.warning(msg)
        else:
            logger.info(msg)

    # Write ALL alerts to the queue — Valkyrie decides what to act on
    if alerts:
        write_alert_queue(alerts)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_check():
    """Run all health checks once."""
    alerts = []
    alerts.extend(check_gpu())
    alerts.extend(check_disk())
    alerts.extend(check_services())

    if alerts:
        process_alerts(alerts)
    else:
        logger.info("All systems nominal")

    return alerts


def main():
    logger.info("=== ANAQ Heartbeat starting (interval: %ds) ===", CHECK_INTERVAL)

    while True:
        try:
            run_check()
        except Exception as e:
            logger.exception("Heartbeat check failed: %s", e)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
