#!/usr/bin/env python3
"""
ANAQ Repair Agent — Opus-Powered System Recovery & Config Manager

When the primary LLM (llama-server/vLLM) goes down and OC falls back to the
bridge, this agent intercepts the fallback call and:

1. Detects it's a system-down scenario (not a normal chat)
2. Sends Fareez a WhatsApp push: "System is down. Debug? YES/NO/config"
3. If YES or specific config: uses launch_manager.sh to restore service
4. Reports specs back to Fareez via WhatsApp
5. Updates OC config if model/context changed

This runs as a webhook on the bridge — when the bridge receives a request
and detects the primary is down, it triggers this repair flow instead of
trying to generate a chat response.
"""

import json
import logging
import os
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ANAQ_HOME = Path.home() / ".anaq"
LAUNCH_MANAGER = Path.home() / "vllm_workspace" / "bin" / "start_llama.sh"
LOG_DIR = ANAQ_HOME / "logs"
LOG_FILE = LOG_DIR / "repair_agent.log"
STATE_FILE = ANAQ_HOME / "repair_state.json"
LAUNCH_STATE = ANAQ_HOME / "launch_state.json"

# OC WhatsApp send endpoint (via exec tool or direct API)
FAREEZ_NUMBER = "+19546817308"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR.mkdir(parents=True, exist_ok=True)
logger = logging.getLogger("repair_agent")
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
# State
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "awaiting_response": False,
        "last_notification": None,
        "auto_repair": True,  # Default: auto-repair unless told otherwise
        "last_config": None,
        "notification_cooldown": 300,  # 5 min between notifications
    }


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# System Checks
# ---------------------------------------------------------------------------

def is_primary_up() -> bool:
    """Check if the primary LLM is responding."""
    try:
        req = urllib.request.Request("http://127.0.0.1:8000/health", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return data.get("status") == "ok"
    except Exception:
        return False


def get_last_config() -> dict | None:
    """Get the last known launch configuration."""
    if LAUNCH_STATE.exists():
        try:
            return json.loads(LAUNCH_STATE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return None


def diagnose() -> str:
    """Run diagnostics and return a report."""
    lines = []

    # GPU status
    try:
        result = subprocess.run(
            ["rocm-smi", "--showuse", "--showtemp", "--showmeminfo", "vram"],
            capture_output=True, text=True, timeout=10,
        )
        lines.append("GPU Status:")
        for line in result.stdout.strip().split("\n"):
            if any(k in line for k in ["Temperature", "busy", "Used", "Total"]):
                lines.append(f"  {line.strip()}")
    except Exception as e:
        lines.append(f"GPU check failed: {e}")

    # Process check
    for name, pattern in [("llama-server", "llama-server"), ("vLLM", "vllm.entrypoints"), ("ComfyUI", "main.py.*818")]:
        try:
            result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
            if result.returncode == 0:
                lines.append(f"{name}: RUNNING (PID {result.stdout.strip()})")
            else:
                lines.append(f"{name}: DOWN")
        except Exception:
            lines.append(f"{name}: CHECK FAILED")

    # Port checks
    for port, svc in [(8000, "LLM"), (8001, "Proxy"), (5500, "Bridge"), (9600, "Memory")]:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/health", method="GET")
            urllib.request.urlopen(req, timeout=3)
            lines.append(f"Port {port} ({svc}): UP")
        except Exception:
            lines.append(f"Port {port} ({svc}): DOWN")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Launch Manager Interface
# ---------------------------------------------------------------------------

def run_launch_script(preset: str = "huihui") -> str:
    """Execute start_llama.sh with a preset. Handles GPU clocks, model launch,
    OpenClaw config update, and gateway restart automatically."""
    cmd = ["bash", str(LAUNCH_MANAGER), preset]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=180,
            env={**os.environ, "HSA_OVERRIDE_GFX_VERSION": "11.0.0"},
        )
        output = result.stdout + result.stderr
        return output.strip()
    except subprocess.TimeoutExpired:
        return "ERROR: Launch timed out (180s)"
    except Exception as e:
        return f"ERROR: {e}"


# Command-to-script dispatch table
_COMMAND_SCRIPTS = {
    "tp-llama":      (Path.home() / "vllm_workspace" / "bin" / "start_llama.sh", None),
    "single-llama":  (Path.home() / "vllm_workspace" / "bin" / "start_llama.sh", None),
    "tp-vllm":       (Path.home() / "vllm_workspace" / "vllm-launch.sh", None),
    "single-vllm":   (Path.home() / "vllm_workspace" / "vllm-launch.sh", None),
    "tp-omni":       (Path.home() / "vllm_workspace" / "bin" / "launch_omni.sh", None),
    "comfy-dual":    (Path.home() / "comfy_repository" / "launch_tandem.sh", None),
    "comfy-single":  (Path.home() / "comfy_repository" / "launch_tandem.sh", None),
    "split":         (Path.home() / "vllm_workspace" / "bin" / "start_llama.sh", None),
}


def run_launch_manager(command: str, model: str = None) -> str:
    """Dispatch a command to the correct launch script with optional model preset."""
    entry = _COMMAND_SCRIPTS.get(command)
    if entry:
        script, _ = entry
        preset = model or "huihui"
        cmd = ["bash", str(script), preset]
    else:
        # Unknown command — fall back to start_llama.sh with model or default
        logger.warning("Unknown command '%s', falling back to start_llama.sh", command)
        cmd = ["bash", str(LAUNCH_MANAGER), model or "huihui"]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=180,
            env={**os.environ, "HSA_OVERRIDE_GFX_VERSION": "11.0.0"},
        )
        output = result.stdout + result.stderr
        return output.strip()
    except subprocess.TimeoutExpired:
        return "ERROR: Launch timed out (180s)"
    except Exception as e:
        return f"ERROR: {e}"


def auto_repair() -> str:
    """Attempt to restore the last known configuration via start_llama.sh.
    Reads active_model.env for the last preset, falls back to huihui."""
    active_env = Path.home() / ".openclaw" / "active_model.env"
    preset = "huihui"  # default

    if active_env.exists():
        try:
            for line in active_env.read_text().splitlines():
                if line.startswith("MODEL_NAME="):
                    model_name = line.split("=", 1)[1].strip()
                    # Map known model names to presets
                    name_lower = model_name.lower()
                    if "huihui" in name_lower and "27b" not in name_lower:
                        preset = "huihui"
                    elif "hauhaucs" in name_lower or "aggressive" in name_lower:
                        preset = "huahua"
                    elif "27b" in name_lower:
                        preset = "pearl"
                    elif "savant" in name_lower:
                        preset = "savant"
                    elif "opus" in name_lower:
                        preset = "opus"
                    elif "gemini" in name_lower:
                        preset = "gemini"
                    elif "heretic" in name_lower:
                        preset = "heretic"
                    break
        except OSError:
            pass

    logger.info("Auto-repair: launching preset '%s' via start_llama.sh", preset)
    return run_launch_script(preset)


# ---------------------------------------------------------------------------
# Telegram Notification (primary ANAQ service line)
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TG_SYSTEM_TOKEN", "")
TELEGRAM_CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TG_SYSTEM_TOKEN environment variable required")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"


def send_telegram(message: str, parse_mode: str = None) -> bool:
    """Send a Telegram message to Fareez. One HTTP POST, zero dependencies."""
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        req = urllib.request.Request(
            TELEGRAM_API,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            if data.get("ok"):
                logger.info("Telegram delivered to chat %s", TELEGRAM_CHAT_ID)
                return True
            logger.error("Telegram API error: %s", data)
            return False
    except Exception as e:
        logger.error("Telegram send failed: %s", e)
        return False


def notify_fareez(message: str) -> bool:
    """Send a notification to Fareez via Telegram (primary) with WhatsApp fallback."""
    tagged = f"[ANAQ SYSTEM] {message}"

    # Primary: Telegram — instant, no session conflicts, no inference
    if send_telegram(tagged):
        return True

    # Fallback: WhatsApp via OC CLI (unreliable but worth trying)
    logger.warning("Telegram failed — attempting WhatsApp fallback")
    try:
        result = subprocess.run(
            [
                "/home/fareez541/.npm-global/bin/openclaw", "message", "send",
                "--channel", "whatsapp",
                "--target", FAREEZ_NUMBER,
                "--message", tagged,
            ],
            capture_output=True, text=True,
            timeout=30,
        )
        if result.returncode == 0:
            logger.info("WhatsApp fallback delivered")
            return True
    except Exception as e:
        logger.error("WhatsApp fallback failed: %s", e)

    return False


# Keep send_whatsapp as alias for backward compatibility
send_whatsapp = notify_fareez


# ---------------------------------------------------------------------------
# Main Repair Flow (called by bridge or standalone)
# ---------------------------------------------------------------------------

def handle_system_down(user_message: str = None) -> str:
    """
    Called when the primary LLM is detected as down.
    Returns a status message.
    """
    state = load_state()
    now = datetime.now(timezone.utc).isoformat()

    logger.info("System down detected. Diagnosing...")
    diag = diagnose()
    logger.info("Diagnosis:\n%s", diag)

    # Check if user sent a config request
    if user_message:
        msg = user_message.lower().strip()

        if msg in ("no", "later", "not now", "stand down"):
            state["awaiting_response"] = False
            state["auto_repair"] = False
            save_state(state)
            return "Standing down. System remains offline. Message me when ready."

        if msg in ("yes", "fix", "repair", "debug", "launch"):
            logger.info("Auto-repair requested")
            result = auto_repair()
            logger.info("Repair result:\n%s", result)
            state["awaiting_response"] = False
            state["last_config"] = get_last_config()
            save_state(state)
            return f"Repair complete:\n{result}"

        # Parse specific config requests
        for cmd in ["tp-llama", "tp-vllm", "tp-omni", "single-llama", "single-vllm",
                     "comfy-dual", "comfy-single", "split"]:
            if cmd.replace("-", " ") in msg or cmd in msg:
                # Extract model if present
                parts = msg.split()
                model = None
                for p in parts:
                    if resolve_model_alias(p):
                        model = p
                        break
                result = run_launch_manager(cmd, model)
                state["last_config"] = get_last_config()
                save_state(state)
                return f"Config change:\n{result}"

        # Check for model name as config shortcut
        model = resolve_model_alias(msg)
        if model:
            result = run_launch_manager("tp-llama", msg)
            state["last_config"] = get_last_config()
            save_state(state)
            return f"Launched:\n{result}"

    # No user input — auto-repair if enabled
    if state.get("auto_repair", True):
        # Check cooldown
        last_notif = state.get("last_notification")
        if last_notif:
            from datetime import datetime as dt
            try:
                last_dt = dt.fromisoformat(last_notif)
                now_dt = dt.now(timezone.utc)
                if (now_dt - last_dt).total_seconds() < state.get("notification_cooldown", 300):
                    return "System down. Repair already attempted recently. Waiting."
            except (ValueError, TypeError):
                pass

        logger.info("Attempting auto-repair...")
        result = auto_repair()
        logger.info("Auto-repair result:\n%s", result)

        # Notify Fareez
        notification = f"🔧 System auto-repaired:\n{result}"
        send_whatsapp(notification)

        state["last_notification"] = now
        state["last_config"] = get_last_config()
        save_state(state)
        return result

    return f"System is down. Diagnosis:\n{diag}\n\nReply with a config (e.g., 'tp-llama agg') or 'yes' to auto-repair."


def resolve_model_alias(name: str) -> bool:
    """Check if a string is a valid model alias."""
    known = ["savant", "opus", "gemini", "agg", "aggressive", "huihui", "huihui-opus",
             "huihui27", "deckard", "freedom24", "coder", "sonnet", "thinking",
             "erotic", "omni", "vl", "glm", "mistral", "medgemma",
             "awq35", "gptq35", "gptq27", "omni-awq"]
    return name.lower() in known


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = " ".join(sys.argv[1:])
        print(handle_system_down(cmd))
    else:
        print(handle_system_down())
