#!/usr/bin/env python3
"""
Per-Agent Telegram Bots — Each agent has their own dedicated Telegram bot.

Architecture:
  Pearl Bot  (tok: 8563406175) ──→ A0 /api_message?agent_profile=pearl
  Nimah Bot  (tok: 8373031049) ──→ A0 /api_message?agent_profile=nimah
  (more bots added via AGENT_BOTS config)

Each bot runs in its own thread with independent long-polling.
Single process, multiple bots, shared A0 connection.

Also supports a system bot (ANAQ) for /status, /switch, cross-agent routing.
"""

import json
import logging
import os
import re
import sys
import time
import threading
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ALLOWED_CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID", "8573771143"))
A0_URL = os.environ.get("A0_URL", "http://localhost:5000")
BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://127.0.0.1:9600")
POLL_TIMEOUT = 30
MAX_TG_MSG = 4096

ANAQ_HOME = Path.home() / ".anaq"
LOG_DIR = ANAQ_HOME / "logs"
LOG_FILE = LOG_DIR / "telegram_bots.log"
STATE_FILE = ANAQ_HOME / "telegram_bots_state.json"

# Per-agent bot tokens — add new agents here
AGENT_BOTS = {
    "pearl": {
        "token": os.environ.get("TG_PEARL_TOKEN", ""),
        "name": "Pearl",
    },
    "nimah": {
        "token": os.environ.get("TG_NIMAH_TOKEN", ""),
        "name": "Nimah",
    },
    "samirah": {
        "token": os.environ.get("TG_SAMIRAH_TOKEN", ""),
        "name": "Samirah",
    },
    "main_sysadmin": {
        "token": os.environ.get("TG_MAIN_TOKEN", ""),
        "name": "Main",
    },
    # "anaq": {"token": "...", "name": "ANAQ"},
}

# Validate all bot tokens are set
for _bot_id, _bot_cfg in AGENT_BOTS.items():
    if not _bot_cfg["token"]:
        raise RuntimeError(f"Missing env var for {_bot_id} bot token (TG_{_bot_id.upper()}_TOKEN)")

# System bot for /status and cross-agent commands (uses ANAQ or first available)
SYSTEM_BOT_TOKEN = os.environ.get("TG_SYSTEM_TOKEN", "")
if not SYSTEM_BOT_TOKEN:
    raise RuntimeError("TG_SYSTEM_TOKEN environment variable required")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR.mkdir(parents=True, exist_ok=True)
logger = logging.getLogger("telegram_bots")
logger.setLevel(logging.DEBUG)
_fmt = logging.Formatter("[%(asctime)s] %(levelname)-7s [%(agent)s] %(message)s", datefmt="%H:%M:%S")
_fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(_fmt)
logger.addHandler(_fh)
_sh = logging.StreamHandler()
_sh.setLevel(logging.INFO)
_sh.setFormatter(_fmt)
logger.addHandler(_sh)


def log(agent: str, level: str, msg: str, *args):
    getattr(logger, level)(msg, *args, extra={"agent": agent})


# ---------------------------------------------------------------------------
# Telegram API
# ---------------------------------------------------------------------------

def tg_request(token: str, method: str, params: dict | None = None, timeout: int = 60) -> dict | None:
    url = f"https://api.telegram.org/bot{token}/{method}"
    if params:
        data = json.dumps(params).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    else:
        req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log("system", "error", "TG API %s error: %s", method, e)
        return None


def send_message(token: str, chat_id: int, text: str):
    chunks = [text[i:i+MAX_TG_MSG] for i in range(0, len(text), MAX_TG_MSG)]
    for chunk in chunks:
        tg_request(token, "sendMessage", {"chat_id": chat_id, "text": chunk})


def send_typing(token: str, chat_id: int):
    tg_request(token, "sendChatAction", {"chat_id": chat_id, "action": "typing"})


# ---------------------------------------------------------------------------
# Agent Zero API
# ---------------------------------------------------------------------------

A0_API_KEY = os.environ.get("A0_API_KEY", "8wuTGkQ_funD8s-3")


# Track A0 context IDs — these are in-memory on A0 and reset on restart
_a0_contexts: dict[str, str] = {}

def call_a0(agent: str, message: str, chat_id: int) -> str | None:
    local_key = f"tg_{agent}_{chat_id}"
    context_id = _a0_contexts.get(local_key)

    def _do_call(ctx_id: str | None) -> tuple[dict | None, bool]:
        body: dict = {
            "message": message,
            "agent_profile": agent,
            "lifetime_hours": 168,
        }
        if ctx_id:
            body["context_id"] = ctx_id

        req = urllib.request.Request(
            f"{A0_URL}/api_message",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-API-KEY": A0_API_KEY,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=3600) as resp:
                return json.loads(resp.read()), False
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:200]
            if ctx_id and e.code in (404, 400):
                # 404 = context expired; 400 = context exists with different profile (A0 restarted)
                log(agent, "warning", "A0 HTTP %d (context stale) — will retry fresh: %s", e.code, body)
                return None, True
            log(agent, "error", "A0 HTTP %d: %s", e.code, body)
            return None, False
        except Exception as e:
            log(agent, "error", "A0 call failed: %s", e)
            return None, False

    data, retry = _do_call(context_id)
    if retry:
        log(agent, "info", "A0 context expired for %s — creating new session", local_key)
        _a0_contexts.pop(local_key, None)
        data, _ = _do_call(None)

    if not data or not isinstance(data, dict):
        return None

    # Save the context ID A0 assigned for reuse
    new_ctx = data.get("context_id")
    if new_ctx:
        _a0_contexts[local_key] = new_ctx

    return data.get("response") or data.get("message") or data.get("text") or json.dumps(data)


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

def check_health() -> dict:
    health = {}
    for name, url in [("a0", A0_URL), ("bridge", BRIDGE_URL), ("llm", "http://127.0.0.1:8000")]:
        try:
            endpoint = "/" if name == "a0" else "/health"
            req = urllib.request.Request(f"{url}{endpoint}", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                health[name] = resp.status == 200
        except Exception:
            health[name] = False
    return health


# ---------------------------------------------------------------------------
# Per-Agent Bot Thread
# ---------------------------------------------------------------------------

class AgentBot(threading.Thread):
    """Dedicated long-polling thread for a single agent's Telegram bot."""

    def __init__(self, agent_name: str, token: str, display_name: str):
        super().__init__(daemon=True, name=f"bot-{agent_name}")
        self.agent = agent_name
        self.token = token
        self.display_name = display_name
        self.offset = 0
        self.running = True

    _BACKOFF_MIN = 5    # seconds
    _BACKOFF_MAX = 60   # seconds

    def run(self):
        log(self.agent, "info", "%s bot starting (token ...%s)", self.display_name, self.token[-8:])

        # Set bot commands
        tg_request(self.token, "setMyCommands", {
            "commands": [
                {"command": "status", "description": "System health status"},
                {"command": "forget", "description": "Clear conversation context"},
            ]
        })

        backoff = self._BACKOFF_MIN
        while self.running:
            try:
                result = tg_request(self.token, "getUpdates", {
                    "offset": self.offset,
                    "timeout": POLL_TIMEOUT,
                    "allowed_updates": ["message"],
                }, timeout=POLL_TIMEOUT + 10)

                if not result or not result.get("ok"):
                    log(self.agent, "warning", "getUpdates failed, backing off %ds", backoff)
                    time.sleep(backoff)
                    backoff = min(backoff * 2, self._BACKOFF_MAX)
                    continue

                # Success — reset backoff
                backoff = self._BACKOFF_MIN

                for update in result.get("result", []):
                    self.offset = update["update_id"] + 1
                    try:
                        self._handle_update(update)
                    except Exception as e:
                        log(self.agent, "exception", "Error handling update: %s", e)

            except KeyboardInterrupt:
                break
            except Exception as e:
                log(self.agent, "error", "Poll error: %s (backoff %ds)", e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, self._BACKOFF_MAX)

    def _handle_update(self, update: dict):
        message = update.get("message")
        if not message:
            return

        chat_id = message.get("chat", {}).get("id")
        text = message.get("text", "").strip()
        if not chat_id or not text:
            return

        # Security
        if chat_id != ALLOWED_CHAT_ID:
            send_message(self.token, chat_id, f"[{self.display_name}] Unauthorized.")
            return

        log(self.agent, "info", "Message: %.80s", text)

        # Handle /status
        if text.lower().startswith("/status"):
            health = check_health()
            lines = [
                f"[{self.display_name} STATUS]",
                f"  Agent Zero: {'UP' if health.get('a0') else 'DOWN'}",
                f"  Memory Bridge: {'UP' if health.get('bridge') else 'DOWN'}",
                f"  LLM Server: {'UP' if health.get('llm') else 'DOWN'}",
                f"  Time: {datetime.now(timezone.utc).strftime('%H:%M UTC')}",
            ]
            send_message(self.token, chat_id, "\n".join(lines))
            return

        # Handle /forget
        if text.lower().startswith("/forget"):
            send_message(self.token, chat_id, f"[{self.display_name}] Context cleared. Next message starts fresh.")
            return

        # Immediate acknowledgment — guarantees Fareez knows the message landed
        send_message(self.token, chat_id, f"[{self.display_name}] Received — processing now.")

        # Route to Agent Zero asynchronously — no timeout possible
        worker = threading.Thread(
            target=self._process_a0_async,
            args=(text, chat_id),
            daemon=True,
            name=f"a0-{self.agent}-{int(time.time())}",
        )
        worker.start()

    def _process_a0_async(self, text: str, chat_id: int):
        """Handle A0 call in background thread — sends response when ready."""
        response = call_a0(self.agent, text, chat_id)

        if response:
            send_message(self.token, chat_id, response)
            log(self.agent, "info", "Response: %d chars", len(response))
        else:
            # First call failed — clear stale context and retry once
            local_key = f"tg_{self.agent}_{chat_id}"
            _a0_contexts.pop(local_key, None)
            log(self.agent, "warning", "First A0 call failed — retrying with fresh context")
            response = call_a0(self.agent, text, chat_id)
            if response:
                send_message(self.token, chat_id, response)
                log(self.agent, "info", "Response (retry): %d chars", len(response))
            else:
                send_message(
                    self.token, chat_id,
                    f"[{self.display_name}] Agent Zero encountered an error processing this request. Check system logs.",
                )
                log(self.agent, "error", "Both A0 attempts failed for message: %.80s", text)

    def stop(self):
        self.running = False


# ---------------------------------------------------------------------------
# System Bot (ANAQ — cross-agent routing + status)
# ---------------------------------------------------------------------------

class SystemBot(AgentBot):
    """The ANAQ system bot — handles /status, /switch, and routes to any agent."""

    def __init__(self):
        super().__init__("system", SYSTEM_BOT_TOKEN, "ANAQ System")

    def _handle_update(self, update: dict):
        message = update.get("message")
        if not message:
            return

        chat_id = message.get("chat", {}).get("id")
        text = message.get("text", "").strip()
        if not chat_id or not text:
            return

        if chat_id != ALLOWED_CHAT_ID:
            send_message(self.token, chat_id, "[ANAQ] Unauthorized.")
            return

        # /status
        if text.lower().startswith("/status"):
            health = check_health()
            bot_status = "\n".join(
                f"  {name}: {'RUNNING' if name in _active_bots else 'NOT CONFIGURED'}"
                for name in ["pearl", "nimah", "main", "samirah", "anaq"]
            )
            lines = [
                "[ANAQ SYSTEM STATUS]",
                f"  Agent Zero: {'UP' if health.get('a0') else 'DOWN'}",
                f"  Memory Bridge: {'UP' if health.get('bridge') else 'DOWN'}",
                f"  LLM Server: {'UP' if health.get('llm') else 'DOWN'}",
                f"\n[AGENT BOTS]",
                bot_status,
                f"\n  Time: {datetime.now(timezone.utc).strftime('%H:%M UTC')}",
            ]
            send_message(self.token, chat_id, "\n".join(lines))
            return

        # Route to specific agent: /pearl <msg>, /nimah <msg>, etc.
        if text.startswith("/"):
            parts = text.split(None, 1)
            agent = parts[0][1:].lower()
            msg = parts[1] if len(parts) > 1 else ""

            valid_agents = set(AGENT_BOTS.keys()) | {"main", "samirah", "anaq"}
            if agent in valid_agents and msg:
                send_typing(self.token, chat_id)
                response = call_a0(agent, msg, chat_id)
                if response:
                    send_message(self.token, chat_id, f"[{agent.upper()}] {response}")
                else:
                    send_message(self.token, chat_id, f"[ANAQ] {agent} is offline.")
                return

        # Default: route to ANAQ
        send_typing(self.token, chat_id)
        response = call_a0("anaq", text, chat_id)
        if response:
            send_message(self.token, chat_id, response)
        else:
            send_message(self.token, chat_id, "[ANAQ] System offline.")


# ---------------------------------------------------------------------------
# Task Completion Watcher
# ---------------------------------------------------------------------------

TASKQUEUE_PATH = Path.home() / "TASKQUEUE.md"
TASK_POLL_INTERVAL = 30  # seconds
_TASK_LINE_RE = re.compile(r"^- \[(\w+)\]\s+(.+)$")


class TaskWatcher(threading.Thread):
    """Watches TASKQUEUE.md for [DONE] transitions and sends Telegram notifications."""

    def __init__(self, notify_token: str, notify_chat_id: int):
        super().__init__(daemon=True, name="task-watcher")
        self.token = notify_token
        self.chat_id = notify_chat_id
        self.known_tasks: dict[str, str] = {}  # task_text -> status
        self.running = True
        # Seed initial state so we don't fire on startup
        self._load_tasks(seed=True)

    def _load_tasks(self, seed: bool = False) -> list[tuple[str, str, str]]:
        """Parse TASKQUEUE.md, return list of (old_status, new_status, task_text) transitions."""
        transitions = []
        try:
            content = TASKQUEUE_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            return transitions

        for line in content.splitlines():
            m = _TASK_LINE_RE.match(line.strip())
            if not m:
                continue
            status, task_text = m.group(1), m.group(2).strip()
            # Normalize task text for matching (strip owner/notes after " — ")
            task_key = task_text.split(" — ")[0].strip()
            old_status = self.known_tasks.get(task_key)

            if not seed and old_status and old_status != status:
                transitions.append((old_status, status, task_text))

            self.known_tasks[task_key] = status

        return transitions

    def run(self):
        log("system", "info", "Task watcher started — monitoring %s", TASKQUEUE_PATH)
        while self.running:
            time.sleep(TASK_POLL_INTERVAL)
            try:
                transitions = self._load_tasks()
                for old_status, new_status, task_text in transitions:
                    if new_status == "DONE":
                        msg = f"[TASK COMPLETE] {task_text}\n\n(was {old_status})"
                        send_message(self.token, self.chat_id, msg)
                        log("system", "info", "Task completion notification: %s", task_text[:80])
                    elif new_status == "BLOCKED":
                        msg = f"[TASK BLOCKED] {task_text}\n\n(was {old_status})"
                        send_message(self.token, self.chat_id, msg)
                        log("system", "info", "Task blocked notification: %s", task_text[:80])
                    elif new_status == "ACTIVE" and old_status == "PENDING":
                        msg = f"[TASK STARTED] {task_text}"
                        send_message(self.token, self.chat_id, msg)
                        log("system", "info", "Task started notification: %s", task_text[:80])
            except Exception as e:
                log("system", "error", "Task watcher error: %s", e)

    def stop(self):
        self.running = False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_active_bots: dict[str, AgentBot] = {}


def main():
    log("system", "info", "=== Telegram Multi-Bot Bridge starting ===")
    log("system", "info", "Configured agents: %s", ", ".join(AGENT_BOTS.keys()))

    # Start per-agent bots
    for agent_name, config in AGENT_BOTS.items():
        bot = AgentBot(agent_name, config["token"], config["name"])
        bot.start()
        _active_bots[agent_name] = bot

    # Start system bot
    system_bot = SystemBot()
    system_bot.start()
    _active_bots["system"] = system_bot

    log("system", "info", "%d bots running", len(_active_bots))

    # Start task completion watcher — notifies via Main's bot
    main_bot_token = AGENT_BOTS.get("main_sysadmin", {}).get("token", "")
    if main_bot_token:
        task_watcher = TaskWatcher(main_bot_token, ALLOWED_CHAT_ID)
        task_watcher.start()
        log("system", "info", "Task watcher active — polling %s every %ds", TASKQUEUE_PATH, TASK_POLL_INTERVAL)

    # Keep main thread alive
    try:
        while True:
            time.sleep(60)
            # Health check — log any dead threads
            for name, bot in _active_bots.items():
                if not bot.is_alive():
                    log("system", "warning", "Bot %s died — restarting", name)
                    if name == "system":
                        new_bot = SystemBot()
                    else:
                        cfg = AGENT_BOTS.get(name, {})
                        new_bot = AgentBot(name, cfg.get("token", ""), cfg.get("name", name))
                    new_bot.start()
                    _active_bots[name] = new_bot
    except KeyboardInterrupt:
        log("system", "info", "Shutting down...")
        for bot in _active_bots.values():
            bot.stop()


if __name__ == "__main__":
    main()
