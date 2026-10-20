#!/usr/bin/env python3
"""Claude Code PreToolUse hook for Read: block whole-file reads of very large files and point the
model at the local tools instead. Reads with an explicit `limit` (targeted spot-checks) pass.

Threshold: LOCAL_AGENT_LARGE_READ_BYTES (default 262144 = 256 KB).
Bypass for a session: export LOCAL_AGENT_NUDGE=0
"""
import json
import os
import sys

def main() -> None:
    if os.environ.get("LOCAL_AGENT_NUDGE", "1") == "0":
        return
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return
    if event.get("tool_name") != "Read":
        return
    ti = event.get("tool_input") or {}
    path = ti.get("file_path") or ""
    if ti.get("limit"):
        return  # targeted read: allowed
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    limit = int(os.environ.get("LOCAL_AGENT_LARGE_READ_BYTES", "262144"))
    if size <= limit:
        return
    reason = (
        f"{path} is {size/1024:.0f} KB. Reading it whole is expensive. Use "
        f"mcp__local-agent__local_summarize_files (paths=[\"{path}\"], question=...) or "
        f"local_analyze_data for tabular files, then spot-check with Read(offset, limit)."
    )
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))

if __name__ == "__main__":
    main()
