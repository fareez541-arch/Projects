#!/usr/bin/env python3
"""
ANAQ Hive Mind — Claude Code Bridge
FastAPI service wrapping the Claude Code CLI as an OpenAI-compatible API.

Port: 5500
Manages stateless claude -p invocations per model with async locking.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

# Add shared lib to path
sys.path.insert(0, str(Path.home() / ".anaq"))
from lib.metrics import MetricsCollector
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

CLAUDE_BIN = os.environ.get(
    "CLAUDE_BIN",
    shutil.which("claude") or "/home/fareez541/.local/bin/claude",
)
LOG_DIR = Path.home() / ".anaq" / "logs"
LOG_FILE = LOG_DIR / "bridge.log"
REQUEST_TIMEOUT = int(os.environ.get("BRIDGE_TIMEOUT", "300"))

# Model aliases -> claude --model flag values
MODEL_MAP: dict[str, str] = {
    "claude-opus-4-6": "opus",
    "claude-opus": "opus",
    "opus": "opus",
    "claude-sonnet-4-6": "sonnet",
    "claude-sonnet-4": "sonnet",
    "claude-sonnet": "sonnet",
    "sonnet": "sonnet",
    "claude-haiku": "haiku",
    "claude-haiku-4-5": "haiku",
    "haiku": "haiku",
}

AVAILABLE_MODELS = [
    {"id": "claude-opus-4-6", "object": "model", "owned_by": "anthropic", "slug": "opus"},
    {"id": "claude-sonnet-4-6", "object": "model", "owned_by": "anthropic", "slug": "sonnet"},
    {"id": "claude-haiku-4-5", "object": "model", "owned_by": "anthropic", "slug": "haiku"},
]

DEFAULT_MODEL = "opus"
DEFAULT_SYSTEM_PROMPT = "You are a helpful AI assistant."

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("bridge")
logger.setLevel(logging.DEBUG)

_fmt = logging.Formatter(
    "[%(asctime)s] %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(_fmt)
logger.addHandler(_fh)

_sh = logging.StreamHandler(sys.stderr)
_sh.setLevel(logging.INFO)
_sh.setFormatter(_fmt)
logger.addHandler(_sh)

# ---------------------------------------------------------------------------
# Per-model state
# ---------------------------------------------------------------------------


class ModelState:
    """Tracks lock and usage stats for a single model slot."""

    def __init__(self, slug: str):
        self.slug = slug
        self.lock = asyncio.Lock()
        self.total_requests: int = 0
        self.total_errors: int = 0
        self.last_request_at: Optional[float] = None
        self.approx_tokens_used: int = 0

    @property
    def context_pressure(self) -> str:
        if self.approx_tokens_used > 800_000:
            return "critical"
        if self.approx_tokens_used > 500_000:
            return "high"
        if self.approx_tokens_used > 200_000:
            return "moderate"
        return "low"

    def record_usage(self, prompt_chars: int, response_chars: int):
        self.approx_tokens_used += (prompt_chars + response_chars) // 4
        self.total_requests += 1
        self.last_request_at = time.time()

    def reset_context(self):
        self.approx_tokens_used = 0

    def to_dict(self) -> dict:
        return {
            "model": self.slug,
            "total_requests": self.total_requests,
            "total_errors": self.total_errors,
            "approx_tokens_used": self.approx_tokens_used,
            "context_pressure": self.context_pressure,
            "last_request_at": self.last_request_at,
        }


_model_states: dict[str, ModelState] = {}


def _get_state(model_slug: str) -> ModelState:
    if model_slug not in _model_states:
        _model_states[model_slug] = ModelState(model_slug)
    return _model_states[model_slug]


# ---------------------------------------------------------------------------
# Claude CLI invocation
# ---------------------------------------------------------------------------


async def _invoke_claude(
    model_slug: str,
    prompt_text: str,
    system_prompt: str,
    max_tokens: Optional[int] = None,
) -> tuple[str, dict]:
    """
    Run `claude -p` as an async subprocess, pipe prompt via stdin,
    parse JSON output, return (assistant_text, usage_dict).

    Uses --output-format json which returns a single JSON object with
    type=result, subtype=success, and result=<assistant text>.
    """

    cmd = [
        CLAUDE_BIN,
        "-p",
        "--output-format", "json",
        "--model", model_slug,
        "--dangerously-skip-permissions",
        "--no-session-persistence",
    ]

    if system_prompt and system_prompt != DEFAULT_SYSTEM_PROMPT:
        cmd.extend(["--system-prompt", system_prompt])

    # Note: claude CLI does not support --max-tokens flag.
    # max_tokens is accepted in the API for compatibility but not passed to CLI.

    logger.debug("CMD: %s", " ".join(cmd))
    logger.debug("PROMPT (%d chars): %.200s...", len(prompt_text), prompt_text)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", ""),
            "LANG": "en_US.UTF-8",
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
        },
    )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(input=prompt_text.encode("utf-8")),
            timeout=REQUEST_TIMEOUT,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise HTTPException(
            status_code=504,
            detail=f"Claude CLI timed out after {REQUEST_TIMEOUT}s",
        )

    if proc.returncode != 0:
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        logger.error("Claude CLI exit %d: %s", proc.returncode, stderr_text)
        raise HTTPException(
            status_code=502,
            detail=f"Claude CLI failed (exit {proc.returncode}): {stderr_text[:500]}",
        )

    # Parse JSON output — single result object
    stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()

    try:
        result_obj = json.loads(stdout_text)
    except json.JSONDecodeError:
        # Might be plain text response
        if stdout_text:
            logger.warning("Non-JSON output from claude (%d chars), using raw", len(stdout_text))
            return stdout_text, {}
        raise HTTPException(status_code=502, detail="Claude CLI returned empty output")

    if result_obj.get("is_error"):
        error_msg = result_obj.get("result", "Unknown error")
        raise HTTPException(status_code=502, detail=f"Claude error: {error_msg}")

    assistant_text = result_obj.get("result", "")
    usage = result_obj.get("usage", {})

    if not assistant_text:
        raise HTTPException(status_code=502, detail="Claude CLI returned empty result")

    return assistant_text, usage


# ---------------------------------------------------------------------------
# Tool support helpers
# ---------------------------------------------------------------------------


def _format_tools_block(tools: list[dict]) -> str:
    """
    Convert an OpenAI-format tools array into a structured text block
    that Claude can understand when injected into the prompt.
    """
    if not tools:
        return ""

    lines = [
        "## Available Tools",
        "",
        "You have the following tools available. To use a tool, output a JSON object "
        'with "tool_call" containing "name" and "arguments". Example:',
        "",
        '```json',
        '{"tool_call": {"name": "tool_name", "arguments": {"arg1": "value1"}}}',
        '```',
        "",
    ]

    for tool in tools:
        if tool.get("type") != "function":
            continue
        func = tool.get("function", {})
        name = func.get("name", "unknown")
        desc = func.get("description", "No description provided.")
        params = func.get("parameters", {})

        lines.append(f"### {name}")
        lines.append(f"Description: {desc}")

        properties = params.get("properties", {})
        required_fields = set(params.get("required", []))

        if properties:
            lines.append("Parameters:")
            for pname, pschema in properties.items():
                ptype = pschema.get("type", "any")
                pdesc = pschema.get("description", "")
                req_label = "required" if pname in required_fields else "optional"
                lines.append(f"- {pname} ({ptype}, {req_label}): {pdesc}")

        lines.append("")

    return "\n".join(lines)


def _format_tool_result_message(msg: dict) -> str:
    """
    Format a tool-role message (tool execution result) into readable text.
    """
    tool_call_id = msg.get("tool_call_id", "unknown")
    content = msg.get("content", "")
    if isinstance(content, list):
        text_parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(block["text"])
            elif isinstance(block, str):
                text_parts.append(block)
        content = "\n".join(text_parts)
    return f"[Tool Result (call_id={tool_call_id})]:\n{content}"


def _format_assistant_tool_calls(msg: dict) -> str:
    """
    Format an assistant message that contains tool_calls into readable text.
    """
    parts = []
    content = msg.get("content", "")
    if content:
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block["text"])

    tool_calls = msg.get("tool_calls", [])
    for tc in tool_calls:
        func = tc.get("function", {})
        tc_id = tc.get("id", "unknown")
        name = func.get("name", "unknown")
        arguments = func.get("arguments", "{}")
        parts.append(
            f'[Tool Call (id={tc_id})]: {{"tool_call": {{"name": "{name}", "arguments": {arguments}}}}}'
        )

    return "\n".join(parts)


def _parse_tool_calls(response_text: str) -> tuple[str, list[dict]]:
    """
    Scan Claude's response for tool_call JSON blocks.
    Returns (cleaned_content, tool_calls_list).

    Looks for patterns like:
      {"tool_call": {"name": "...", "arguments": {...}}}
    in the response text, possibly inside code fences.
    """
    tool_calls = []
    cleaned = response_text

    # Pattern: JSON blocks containing tool_call — try code fences first, then bare JSON
    # Match ```json ... ``` blocks
    code_fence_pattern = re.compile(
        r'```(?:json)?\s*(\{[^`]*?"tool_call"[^`]*?\})\s*```',
        re.DOTALL,
    )
    # Match bare JSON objects with tool_call
    bare_json_pattern = re.compile(
        r'(\{\s*"tool_call"\s*:\s*\{.*?\}\s*\})',
        re.DOTALL,
    )

    found_blocks = []

    for match in code_fence_pattern.finditer(response_text):
        found_blocks.append((match.group(0), match.group(1)))

    if not found_blocks:
        for match in bare_json_pattern.finditer(response_text):
            found_blocks.append((match.group(0), match.group(1)))

    for full_match, json_str in found_blocks:
        try:
            parsed = json.loads(json_str)
            tc = parsed.get("tool_call", {})
            name = tc.get("name")
            arguments = tc.get("arguments", {})

            if not name:
                continue

            call_id = f"call_{uuid.uuid4().hex[:24]}"
            tool_calls.append({
                "id": call_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments) if isinstance(arguments, dict) else str(arguments),
                },
            })

            # Remove the tool call block from content
            cleaned = cleaned.replace(full_match, "").strip()

        except (json.JSONDecodeError, AttributeError):
            logger.debug("Failed to parse potential tool_call block: %.200s", json_str)
            continue

    return cleaned, tool_calls


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------


def _format_messages_as_prompt(messages: list[dict], tools: Optional[list] = None) -> tuple[str, str]:
    """
    Convert OpenAI-style messages array into (system_prompt, prompt_text).
    When tools are provided, injects a structured tool description block.
    Handles role="tool" messages (tool results) and assistant tool_calls.
    """
    system_prompt = DEFAULT_SYSTEM_PROMPT
    parts: list[str] = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # Handle tool result messages
        if role == "tool":
            parts.append(_format_tool_result_message(msg))
            continue

        # Handle assistant messages that contain tool_calls
        if role == "assistant" and msg.get("tool_calls"):
            parts.append(f"[Assistant]: {_format_assistant_tool_calls(msg)}")
            continue

        if isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block["text"])
                elif isinstance(block, str):
                    text_parts.append(block)
            content = "\n".join(text_parts)

        if role == "system":
            system_prompt = content
            continue

        if role == "assistant":
            parts.append(f"[Assistant]: {content}")
        elif role == "user":
            parts.append(f"[User]: {content}")
        else:
            parts.append(f"[{role}]: {content}")

    # Inject tools block at the beginning of the prompt if tools are present
    tools_block = _format_tools_block(tools) if tools else ""

    prompt_text = "\n\n".join(parts)

    # Single user message: strip prefix for cleanliness
    if len(parts) == 1 and messages and messages[-1].get("role") == "user":
        content = messages[-1].get("content", "")
        if isinstance(content, str):
            prompt_text = content

    if tools_block:
        prompt_text = tools_block + "\n\n---\n\n" + prompt_text

    return system_prompt, prompt_text


def _make_completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def _openai_response(
    model_id: str,
    content: str,
    prompt_chars: int,
    response_chars: int,
    usage_data: Optional[dict] = None,
    tool_calls: Optional[list] = None,
) -> dict:
    # Use real token counts from claude CLI when available
    if usage_data:
        prompt_tokens = usage_data.get("input_tokens", 0) + usage_data.get("cache_read_input_tokens", 0)
        completion_tokens = usage_data.get("output_tokens", 0)
    else:
        prompt_tokens = max(1, prompt_chars // 4)
        completion_tokens = max(1, response_chars // 4)

    message: dict = {
        "role": "assistant",
        "content": content if content else None,
    }

    finish_reason = "stop"
    if tool_calls:
        message["tool_calls"] = tool_calls
        finish_reason = "tool_calls"

    return {
        "id": _make_completion_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_id,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class ChatMessage(BaseModel):
    role: str
    content: str | list | None = None
    name: Optional[str] = None
    tool_calls: Optional[list] = None
    tool_call_id: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    model: Optional[str] = None
    messages: list[ChatMessage]
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    stream: Optional[bool] = False
    n: Optional[int] = 1
    tools: Optional[list] = None
    tool_choice: Optional[str | dict] = None


class SessionResetRequest(BaseModel):
    model: Optional[str] = None


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


# Global metrics collector
bridge_metrics = MetricsCollector("bridge")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=== Claude Code Bridge starting on port 5500 ===")
    logger.info("Claude binary: %s", CLAUDE_BIN)
    logger.info("Log file: %s", LOG_FILE)
    logger.info("Request timeout: %ds", REQUEST_TIMEOUT)

    for m in AVAILABLE_MODELS:
        _get_state(m["slug"])

    yield

    logger.info("=== Claude Code Bridge shutting down ===")


app = FastAPI(
    title="ANAQ Claude Code Bridge",
    description="OpenAI-compatible API wrapping the Claude Code CLI",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    claude_exists = os.path.isfile(CLAUDE_BIN) and os.access(CLAUDE_BIN, os.X_OK)
    states = {slug: st.to_dict() for slug, st in _model_states.items()}

    return {
        "status": "healthy" if claude_exists else "degraded",
        "claude_binary": CLAUDE_BIN,
        "claude_binary_found": claude_exists,
        "uptime_models": states,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": m["id"],
                "object": "model",
                "created": 1700000000,
                "owned_by": m["owned_by"],
            }
            for m in AVAILABLE_MODELS
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    wants_stream = request.stream or False

    # Resolve model
    requested_model = (request.model or "opus").strip().lower()
    model_slug = MODEL_MAP.get(requested_model)
    if model_slug is None:
        if requested_model in {m["slug"] for m in AVAILABLE_MODELS}:
            model_slug = requested_model
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown model '{request.model}'. Available: {list(MODEL_MAP.keys())}",
            )

    # Resolve canonical model ID for response
    model_id = requested_model
    for m in AVAILABLE_MODELS:
        if m["slug"] == model_slug:
            model_id = m["id"]
            break

    messages_raw = [m.model_dump() for m in request.messages]
    if not messages_raw:
        raise HTTPException(status_code=400, detail="messages array is empty")

    tools_raw = request.tools
    system_prompt, prompt_text = _format_messages_as_prompt(messages_raw, tools=tools_raw)

    if not prompt_text.strip():
        raise HTTPException(status_code=400, detail="No user content in messages")

    state = _get_state(model_slug)

    if state.context_pressure in ("high", "critical"):
        logger.warning(
            "Context pressure %s for model %s (%d approx tokens). "
            "Consider POST /session/reset.",
            state.context_pressure,
            model_slug,
            state.approx_tokens_used,
        )

    logger.info(
        "REQ model=%s prompt_chars=%d lock_held=%s",
        model_slug,
        len(prompt_text),
        state.lock.locked(),
    )

    t0 = time.time()

    async with state.lock:
        try:
            response_text, usage_data = await _invoke_claude(
                model_slug=model_slug,
                prompt_text=prompt_text,
                system_prompt=system_prompt,
                max_tokens=request.max_tokens,
            )
        except HTTPException as he:
            state.total_errors += 1
            elapsed_err = (time.time() - t0) * 1000
            bridge_metrics.record_error(
                error_type="http_error",
                error_msg=str(he.detail),
                model=model_slug,
                endpoint="/v1/chat/completions",
                latency_ms=elapsed_err,
            )
            raise
        except Exception as exc:
            state.total_errors += 1
            elapsed_err = (time.time() - t0) * 1000
            bridge_metrics.record_error(
                error_type=type(exc).__name__,
                error_msg=str(exc),
                model=model_slug,
                endpoint="/v1/chat/completions",
                latency_ms=elapsed_err,
            )
            logger.exception("Unexpected error invoking claude")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    elapsed = time.time() - t0
    elapsed_ms = elapsed * 1000
    state.record_usage(len(prompt_text), len(response_text))

    # ---------------------------------------------------------------------------
    # A0 format compliance: if response is plain text (no JSON tool call),
    # wrap it in Agent Zero's expected response tool format so A0 can parse it.
    # This handles Claude Opus returning conversational text instead of strict JSON.
    # ---------------------------------------------------------------------------
    stripped = response_text.strip()
    if stripped and not stripped.startswith("{") and not stripped.startswith("```json"):
        import json as _json
        wrapped = _json.dumps({
            "thoughts": ["Processed request and generated response."],
            "headline": "Providing response",
            "tool_name": "response",
            "tool_args": {
                "text": stripped,
            }
        }, ensure_ascii=False)
        logger.info("A0 format wrap: plain text -> JSON response tool (%d chars)", len(stripped))
        response_text = wrapped

    # Record metrics
    usage_tokens_in = usage_data.get("input_tokens", 0) if usage_data else 0
    usage_tokens_out = usage_data.get("output_tokens", 0) if usage_data else 0
    bridge_metrics.record_request(
        model=model_slug,
        endpoint="/v1/chat/completions",
        latency_ms=elapsed_ms,
        tokens_in=usage_tokens_in,
        tokens_out=usage_tokens_out,
    )

    logger.info(
        "RES model=%s response_chars=%d elapsed=%.1fs pressure=%s",
        model_slug,
        len(response_text),
        elapsed,
        state.context_pressure,
    )

    # Parse tool calls from response if tools were provided in the request
    parsed_tool_calls = None
    if tools_raw:
        cleaned_content, parsed_tool_calls = _parse_tool_calls(response_text)
        if parsed_tool_calls:
            logger.info("Parsed %d tool_call(s) from response, cleaned_content=%d chars", len(parsed_tool_calls), len(cleaned_content))
            response_text = cleaned_content
        else:
            logger.debug("No tool calls parsed, keeping original response (%d chars)", len(response_text))

    result = _openai_response(
        model_id, response_text, len(prompt_text), len(response_text),
        usage_data, tool_calls=parsed_tool_calls if parsed_tool_calls else None,
    )

    if state.context_pressure != "low":
        result["_context_pressure"] = state.context_pressure
        result["_approx_tokens_used"] = state.approx_tokens_used

    if wants_stream:
        # Wrap the complete response as an SSE event stream.
        # OC expects: data: {chunk}\n\n ... data: [DONE]\n\n
        async def sse_generator():
            chunk_id = result["id"]
            # Send a single chunk with the full content (role + content)
            chunk = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": result["created"],
                "model": result["model"],
                "choices": [{
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "content": response_text or "",
                    },
                    "finish_reason": "stop",
                }],
                "usage": result.get("usage"),
            }
            # If tool calls, include them in the delta
            if parsed_tool_calls:
                chunk["choices"][0]["delta"]["tool_calls"] = parsed_tool_calls
                chunk["choices"][0]["delta"]["content"] = response_text if response_text else None
                chunk["choices"][0]["finish_reason"] = "tool_calls"
            yield f"data: {json.dumps(chunk)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            sse_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return JSONResponse(content=result)


@app.post("/session/reset")
async def session_reset(request: SessionResetRequest):
    if request.model:
        slug = MODEL_MAP.get(request.model.lower(), request.model.lower())
        state = _get_state(slug)
        old_tokens = state.approx_tokens_used
        state.reset_context()
        logger.info("Session reset for %s (was %d tokens)", slug, old_tokens)
        return {"status": "reset", "model": slug, "previous_tokens": old_tokens}
    else:
        results = {}
        for slug, state in _model_states.items():
            old_tokens = state.approx_tokens_used
            state.reset_context()
            results[slug] = {"previous_tokens": old_tokens}
        return {"status": "reset_all", "models": results}


@app.get("/v1/status")
async def model_status():
    return {
        "models": {slug: st.to_dict() for slug, st in _model_states.items()},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/v1/metrics")
async def metrics():
    """Per-process metrics for the bridge. Deterministic and measurable."""
    from lib.metrics import get_model_usage, get_recent_errors
    return {
        "bridge": bridge_metrics.get_stats(),
        "model_usage_24h": get_model_usage(24),
        "recent_errors": get_recent_errors(10),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Direct run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "claude_code_bridge:app",
        host="127.0.0.1",
        port=5500,
        log_level="info",
        access_log=True,
    )
