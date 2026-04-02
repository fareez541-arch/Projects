#!/usr/bin/env python3
"""Ingest a fix or task record into FAISS via the memory bridge.

Usage:
  # From CLI after a fix:
  python3 ingest_fix.py --agent main --type fix \
    --summary "FAISS race condition in add_vector" \
    --files ".anaq/bridge/memory_bridge.py" \
    --commit e43fa8e

  # From CLI after completing a task:
  python3 ingest_fix.py --agent unicorn --type task \
    --summary "Adversarial code review of ANAQ bridge" \
    --task-id 20260401-010

  # From Python:
  from ingest_fix import ingest_record
  ingest_record(agent="tafakkur", record_type="fix", summary="...", files=["..."])

  # From git post-commit hook (auto-ingest every commit):
  Called automatically if installed via install_hook()
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import requests

BRIDGE_URL = os.environ.get("MEMORY_BRIDGE_URL", "http://127.0.0.1:9600")
INDEX_NAME = "SYSTEM"  # fixes and tasks go to SYSTEM index


def ingest_record(
    agent: str,
    record_type: str,  # "fix", "task", "diagnostic", "config"
    summary: str,
    files: list[str] | None = None,
    commit: str | None = None,
    task_id: str | None = None,
    severity: str | None = None,
    extra: dict | None = None,
) -> bool:
    """Push a structured fix/task record to FAISS via memory bridge."""

    timestamp = datetime.now(timezone.utc).isoformat()

    # Build content string for embedding
    parts = [
        f"[{record_type.upper()}] {summary}",
        f"Agent: {agent}",
        f"Date: {timestamp}",
    ]
    if files:
        parts.append(f"Files: {', '.join(files)}")
    if commit:
        parts.append(f"Commit: {commit}")
    if task_id:
        parts.append(f"Task: {task_id}")
    if severity:
        parts.append(f"Severity: {severity}")
    if extra:
        for k, v in extra.items():
            parts.append(f"{k}: {v}")

    content = "\n".join(parts)

    # Metadata for structured queries
    metadata = {
        "record_type": record_type,
        "agent": agent,
        "timestamp": timestamp,
        "files": files or [],
        "commit": commit,
        "task_id": task_id,
        "severity": severity,
    }

    try:
        r = requests.post(
            f"{BRIDGE_URL}/ingest",
            json={
                "content": content,
                "index": INDEX_NAME,
                "agent_scope": ["all"],
                "source": f"ingest_fix:{agent}",
                "metadata_json": json.dumps(metadata),
            },
            timeout=10,
        )
        if r.status_code == 200:
            return True
        else:
            print(f"Ingest failed: {r.status_code} {r.text}", file=sys.stderr)
            return False
    except requests.ConnectionError:
        print("Memory bridge not reachable — record not ingested", file=sys.stderr)
        return False


def ingest_from_git_commit() -> bool:
    """Auto-ingest the latest git commit as a fix record."""
    try:
        # Get latest commit info
        log = subprocess.run(
            ["git", "log", "-1", "--format=%H|%s|%an|%ai"],
            capture_output=True, text=True, timeout=5,
        )
        if log.returncode != 0:
            return False

        parts = log.stdout.strip().split("|", 3)
        if len(parts) < 4:
            return False

        commit_hash, subject, author, date = parts

        # Get changed files
        diff = subprocess.run(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit_hash],
            capture_output=True, text=True, timeout=5,
        )
        files = [f for f in diff.stdout.strip().split("\n") if f]

        # Determine record type from conventional commit prefix
        record_type = "fix"
        if subject.startswith("feat:"):
            record_type = "feature"
        elif subject.startswith("refactor:"):
            record_type = "refactor"
        elif subject.startswith("docs:"):
            record_type = "docs"
        elif subject.startswith("chore:"):
            record_type = "config"
        elif subject.startswith("perf:"):
            record_type = "perf"

        return ingest_record(
            agent=author,
            record_type=record_type,
            summary=subject,
            files=files,
            commit=commit_hash[:8],
        )
    except Exception as e:
        print(f"Git commit ingest failed: {e}", file=sys.stderr)
        return False


def install_hook():
    """Install a git post-commit hook that auto-ingests every commit."""
    hook_path = os.path.join(
        subprocess.run(["git", "rev-parse", "--git-dir"], capture_output=True, text=True).stdout.strip(),
        "hooks", "post-commit",
    )

    hook_content = f"""#!/bin/bash
# Auto-ingest commit to FAISS memory bridge
{sys.executable} {os.path.abspath(__file__)} --from-commit &
"""

    with open(hook_path, "w") as f:
        f.write(hook_content)
    os.chmod(hook_path, 0o755)
    print(f"Installed post-commit hook at {hook_path}")


def main():
    parser = argparse.ArgumentParser(description="Ingest fix/task record to FAISS")
    parser.add_argument("--agent", default="unknown")
    parser.add_argument("--type", dest="record_type", default="fix",
                        choices=["fix", "task", "feature", "diagnostic", "config", "refactor", "perf", "docs"])
    parser.add_argument("--summary", default="")
    parser.add_argument("--files", nargs="*", default=[])
    parser.add_argument("--commit", default=None)
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--severity", default=None)
    parser.add_argument("--from-commit", action="store_true",
                        help="Auto-extract from latest git commit")
    parser.add_argument("--install-hook", action="store_true",
                        help="Install git post-commit hook")

    args = parser.parse_args()

    if args.install_hook:
        install_hook()
        return

    if args.from_commit:
        ok = ingest_from_git_commit()
        sys.exit(0 if ok else 1)

    if not args.summary:
        parser.error("--summary is required unless using --from-commit")

    ok = ingest_record(
        agent=args.agent,
        record_type=args.record_type,
        summary=args.summary,
        files=args.files,
        commit=args.commit,
        task_id=args.task_id,
        severity=args.severity,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
