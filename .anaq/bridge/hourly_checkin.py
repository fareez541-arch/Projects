#!/usr/bin/env python3
"""
Hourly Check-In System — Claude Opus Orchestrator

Reads ~/TASKQUEUE.md, generates status report, sends to Fareez via:
  1. Telegram (ANAQ system bot) — primary
  2. WhatsApp (OpenClaw delivery) — fallback

Can be invoked by:
  - OpenClaw cron job
  - Agent Zero programmatically
  - Direct: python3 ~/.anaq/bridge/hourly_checkin.py [--test]

The check-in message includes:
  - Current hour / time window
  - Active tasks and their status
  - Blocked items needing Fareez's input
  - Plan for next hour
  - Permission requests if any
"""

import json
import os
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TASKQUEUE = Path.home() / "TASKQUEUE.md"
CHECKIN_LOG = Path.home() / ".anaq" / "logs" / "checkin.log"
CHECKIN_STATE = Path.home() / ".anaq" / "checkin_state.json"

# Telegram — ANAQ system bot (Main/sysadmin for work comms)
TG_MAIN_TOKEN = os.environ.get("TG_MAIN_TOKEN", "")
TG_CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
if not TG_MAIN_TOKEN:
    raise RuntimeError("TG_MAIN_TOKEN environment variable required")
TG_API = f"https://api.telegram.org/bot{TG_MAIN_TOKEN}"

# Agent Zero
A0_URL = os.environ.get("A0_URL", "http://localhost:5000")
A0_API_KEY = os.environ.get("A0_API_KEY", "")
if not A0_API_KEY:
    raise RuntimeError("A0_API_KEY environment variable required")

# OpenClaw WhatsApp fallback
OC_URL = os.environ.get("OC_URL", "http://localhost:18789")

# Claude CLI for delegating dev work
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", str(Path.home() / ".local" / "bin" / "claude"))


# ---------------------------------------------------------------------------
# Telegram send
# ---------------------------------------------------------------------------

def _escape_html(text: str) -> str:
    """Escape HTML special characters for Telegram HTML parse_mode."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tg_send(text: str, parse_mode: str = "HTML") -> bool:
    """Send message via Telegram Main bot."""
    MAX_LEN = 4096
    chunks = [text[i:i + MAX_LEN] for i in range(0, len(text), MAX_LEN)]
    success = True
    for chunk in chunks:
        payload = {
            "chat_id": TG_CHAT_ID,
            "text": chunk,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{TG_API}/sendMessage",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                if not result.get("ok"):
                    success = False
        except Exception as e:
            log(f"Telegram send failed: {e}")
            success = False
    return success


# ---------------------------------------------------------------------------
# WhatsApp fallback via OpenClaw
# ---------------------------------------------------------------------------

def whatsapp_send(text: str) -> bool:
    """Send via OpenClaw WhatsApp delivery queue."""
    payload = {
        "channel": "whatsapp",
        "to": "+19546817308",
        "payloads": [{"text": text}],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{OC_URL}/api/v1/deliver",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status == 200
    except Exception as e:
        log(f"WhatsApp send failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Task queue parsing
# ---------------------------------------------------------------------------

def parse_taskqueue() -> dict:
    """Parse TASKQUEUE.md into structured data."""
    if not TASKQUEUE.exists():
        return {"active": [], "pending": [], "blocked": [], "done": []}

    content = TASKQUEUE.read_text()
    tasks = {"active": [], "pending": [], "blocked": [], "done": []}

    for line in content.splitlines():
        line = line.strip()
        if not line.startswith("- ["):
            continue
        if "[ACTIVE]" in line:
            tasks["active"].append(line.replace("- [ACTIVE] ", ""))
        elif "[PENDING]" in line:
            tasks["pending"].append(line.replace("- [PENDING] ", ""))
        elif "[BLOCKED]" in line:
            tasks["blocked"].append(line.replace("- [BLOCKED] ", ""))
        elif "[DONE]" in line:
            tasks["done"].append(line.replace("- [DONE] ", ""))

    return tasks


# ---------------------------------------------------------------------------
# Git status
# ---------------------------------------------------------------------------

def git_summary() -> str:
    """Get recent git activity."""
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-5"],
            capture_output=True, text=True, timeout=10,
            cwd=str(Path.home()),
        )
        return result.stdout.strip() if result.returncode == 0 else "(no git activity)"
    except Exception:
        return "(git unavailable)"


# ---------------------------------------------------------------------------
# Check-in state (track what was sent last)
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if CHECKIN_STATE.exists():
        try:
            return json.loads(CHECKIN_STATE.read_text())
        except Exception:
            pass
    return {"last_checkin": None, "checkin_count": 0, "pending_permissions": []}


def save_state(state: dict):
    CHECKIN_STATE.parent.mkdir(parents=True, exist_ok=True)
    CHECKIN_STATE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Build check-in message
# ---------------------------------------------------------------------------

def build_checkin() -> str:
    now = datetime.now()
    hour = now.strftime("%I:%M %p")
    tasks = parse_taskqueue()
    state = load_state()
    git = git_summary()

    esc = _escape_html

    lines = [
        f"<b>Hourly Check-In — {esc(hour)} ET</b>",
        f"Check-in #{state['checkin_count'] + 1}",
        "",
    ]

    # Active work
    if tasks["active"]:
        lines.append("<b>Active:</b>")
        for t in tasks["active"]:
            lines.append(f"  - {esc(t)}")
        lines.append("")

    # Blocked / needs input
    if tasks["blocked"]:
        lines.append("<b>BLOCKED (need your input):</b>")
        for t in tasks["blocked"]:
            lines.append(f"  - {esc(t)}")
        lines.append("")

    # Pending permissions
    if state.get("pending_permissions"):
        lines.append("<b>Permissions needed:</b>")
        for p in state["pending_permissions"]:
            lines.append(f"  - {esc(p)}")
        lines.append("")

    # Next up
    if tasks["pending"]:
        next_task = tasks["pending"][0]
        lines.append(f"<b>Next up:</b> {esc(next_task)}")
        lines.append("")

    # Recent git
    if git and git != "(no git activity)":
        lines.append("<b>Recent commits:</b>")
        lines.append(f"<pre>{esc(git)}</pre>")
        lines.append("")

    # Stats
    total = len(tasks["active"]) + len(tasks["pending"]) + len(tasks["blocked"])
    done = len(tasks["done"])
    lines.append(f"<i>{done} done / {total} remaining</i>")
    lines.append("")
    lines.append("Reply here to update tasks or give instructions.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Send check-in
# ---------------------------------------------------------------------------

def send_checkin(test_mode: bool = False) -> bool:
    """Send the hourly check-in. Returns True on success, False on total failure."""
    msg = build_checkin()

    if test_mode:
        print("--- CHECK-IN MESSAGE (test mode) ---")
        print(msg)
        print("--- END ---")
        return True

    log(f"Sending hourly check-in...")

    # Primary: Telegram
    tg_ok = tg_send(msg)

    # Fallback: WhatsApp if Telegram fails
    wa_ok = False
    if not tg_ok:
        log("Telegram failed, trying WhatsApp fallback...")
        wa_ok = whatsapp_send(msg)

    delivered = tg_ok or wa_ok

    # Update state
    state = load_state()
    state["last_checkin"] = datetime.now().isoformat()
    state["checkin_count"] = state.get("checkin_count", 0) + 1
    save_state(state)

    log(f"Check-in #{state['checkin_count']} sent (TG={'ok' if tg_ok else 'FAIL'}, WA={'ok' if wa_ok else 'FAIL'})")
    return delivered


# ---------------------------------------------------------------------------
# Agent Zero integration — process replies
# ---------------------------------------------------------------------------

def process_reply(reply_text: str):
    """
    Called when Fareez replies to a check-in via Telegram.
    Routes commands to appropriate handlers:
      - Task updates: "done: <task>", "add: <task>", "block: <task>"
      - Permissions: "allow: <action>"
      - Instructions: forwarded to Agent Zero for processing
    """
    reply = reply_text.strip().lower()

    if reply.startswith("done:"):
        task_name = reply_text[5:].strip()
        mark_task_done(task_name)
        tg_send(f"Marked done: {task_name}")
    elif reply.startswith("add:"):
        task_name = reply_text[4:].strip()
        add_task(task_name)
        tg_send(f"Added: {task_name}")
    elif reply.startswith("block:"):
        task_name = reply_text[6:].strip()
        mark_task_blocked(task_name)
        tg_send(f"Marked blocked: {task_name}")
    elif reply.startswith("priority:"):
        task_name = reply_text[9:].strip()
        add_task(task_name, priority=True)
        tg_send(f"Added as priority: {task_name}")
    elif reply.startswith("allow:"):
        permission = reply_text[6:].strip()
        grant_permission(permission)
        tg_send(f"Permission granted: {permission}")
    else:
        # Forward to Agent Zero for AI processing
        forward_to_a0(reply_text)


def mark_task_done(task_name: str):
    """Move a task to DONE in TASKQUEUE.md."""
    if not TASKQUEUE.exists():
        return
    content = TASKQUEUE.read_text()
    # Find and update the task line
    for status in ["ACTIVE", "PENDING", "BLOCKED"]:
        old = f"[{status}]"
        for line in content.splitlines():
            if old in line and task_name.lower() in line.lower():
                new_line = line.replace(f"[{status}]", "[DONE]")
                content = content.replace(line, new_line)
                break
    TASKQUEUE.write_text(content)


def add_task(task_name: str, priority: bool = False):
    """Add a new task to TASKQUEUE.md."""
    if not TASKQUEUE.exists():
        return
    content = TASKQUEUE.read_text()
    new_line = f"- [PENDING] {task_name} — Fareez — Added via check-in"
    if priority:
        new_line = f"- [ACTIVE] {task_name} — Fareez — Priority, added via check-in"
    # Insert after the first "## Priority" section header
    lines = content.splitlines()
    insert_idx = None
    for i, line in enumerate(lines):
        if line.startswith("## Priority 1"):
            # Find the next empty line after this section
            for j in range(i + 1, len(lines)):
                if lines[j].strip() == "" or lines[j].startswith("## "):
                    insert_idx = j
                    break
            break
    if insert_idx:
        lines.insert(insert_idx, new_line)
    else:
        lines.append(new_line)
    TASKQUEUE.write_text("\n".join(lines))


def mark_task_blocked(task_name: str):
    """Mark a task as blocked."""
    if not TASKQUEUE.exists():
        return
    content = TASKQUEUE.read_text()
    for status in ["ACTIVE", "PENDING"]:
        for line in content.splitlines():
            if f"[{status}]" in line and task_name.lower() in line.lower():
                new_line = line.replace(f"[{status}]", "[BLOCKED]")
                content = content.replace(line, new_line)
                break
    TASKQUEUE.write_text(content)


def grant_permission(permission: str):
    """Record a granted permission in check-in state."""
    state = load_state()
    if "granted_permissions" not in state:
        state["granted_permissions"] = []
    state["granted_permissions"].append({
        "permission": permission,
        "granted_at": datetime.now().isoformat(),
    })
    # Remove from pending if it was there
    state["pending_permissions"] = [
        p for p in state.get("pending_permissions", [])
        if permission.lower() not in p.lower()
    ]
    save_state(state)


def forward_to_a0(message: str):
    """Forward a message to Agent Zero for AI processing."""
    body = {
        "message": f"[CHECKIN REPLY FROM FAREEZ] {message}\n\nProcess this instruction. "
                   f"If it's a task update, modify ~/TASKQUEUE.md. "
                   f"If it's an instruction, execute it or delegate appropriately.",
        "agent_profile": "agent0",
        "lifetime_hours": 24,
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{A0_URL}/api_message",
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-API-KEY": A0_API_KEY,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read())
            reply = result.get("response", "(no response)")
            tg_send(f"<b>A0:</b> {_escape_html(reply[:3000])}")
    except Exception as e:
        log(f"A0 forward failed: {e}")
        tg_send(f"Could not process via A0: {e}")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        CHECKIN_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(CHECKIN_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test = "--test" in sys.argv
    if "--reply" in sys.argv:
        idx = sys.argv.index("--reply")
        if idx + 1 < len(sys.argv):
            process_reply(" ".join(sys.argv[idx + 1:]))
        else:
            print("Usage: hourly_checkin.py --reply <message>")
    else:
        success = send_checkin(test_mode=test)
        if not success:
            sys.exit(1)
