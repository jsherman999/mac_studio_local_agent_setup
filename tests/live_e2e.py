#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=2.3,<3"]
# ///
"""Live end-to-end test: start the server over stdio as Claude Code would and call every tool
against the real local stack (models, SearXNG, OpenCode), with small inputs. Prints each
result's head and timing. Slow: minutes, mostly local_research and local_agent_task.

    uv run --script tests/live_e2e.py                 # all tools
    uv run --script tests/live_e2e.py --skip-agent    # skip local_agent_task (OpenCode)

Fixtures are written under the first LOCAL_AGENT_ALLOWED_ROOTS entry that exists (from
~/.config/local-agent/local-agent.env), in a temporary directory that is removed afterwards.
"""
import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

SERVER = Path(__file__).resolve().parents[1] / "mcp_server" / "local_agent_mcp.py"
ENV_FILE = Path(os.path.expanduser(os.environ.get("LOCAL_AGENT_ENV_FILE", "~/.config/local-agent/local-agent.env")))


def allowed_root() -> Path:
    raw = os.environ.get("LOCAL_AGENT_ALLOWED_ROOTS", "")
    if not raw and ENV_FILE.is_file():
        m = re.search(r"^LOCAL_AGENT_ALLOWED_ROOTS=(.*)$", ENV_FILE.read_text(), re.M)
        raw = m.group(1) if m else ""
    for part in (raw or "~/src").split(":"):
        p = Path(os.path.expanduser(part.strip()))
        if part.strip() and p.is_dir():
            return p
    sys.exit(f"No existing directory in LOCAL_AGENT_ALLOWED_ROOTS ({raw!r})")


def make_fixtures(base: Path) -> dict[str, Path]:
    src = base / "src"
    src.mkdir()
    (src / "auth.py").write_text(
        "import logging\n\nlog = logging.getLogger('audit')\n\n\n"
        "def open_session(user, host):\n    log.info('ssh open %s@%s', user, host)\n    return _connect(user, host)\n\n\n"
        "def open_session_fast(user, host):\n    # bypasses the audit logger on purpose\n    return _connect(user, host)\n\n\n"
        "def _connect(user, host):\n    return f'{user}@{host}'\n")
    (src / "util.py").write_text("def slugify(s):\n    return '-'.join(s.lower().split())\n")
    data = base / "logins.csv"
    data.write_text("host,user,failed\nweb1,alice,0\nweb1,bob,7\ndb1,alice,2\ndb1,carol,9\nweb2,bob,1\n")
    repo = base / "repo"
    repo.mkdir()
    (repo / "mathx.py").write_text("def add(a, b):\n    return a + b\n")
    for cmd in (["git", "init", "-q"], ["git", "add", "."],
                ["git", "-c", "user.email=poc@local", "-c", "user.name=poc", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=repo, check=True)
    return {"src": src, "data": data, "repo": repo}


async def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="local-agent-e2e-", dir=allowed_root()))
    fx = make_fixtures(base)
    cases: list[tuple[str, dict, str]] = [
        ("local_status", {}, r"general\s*-> \S+ OK"),
        ("local_ask", {"prompt": "Reply with the single word: ready", "model_role": "fast", "max_tokens": 200}, r"(?i)ready"),
        ("local_ask", {"prompt": "Reply with the single word: ready", "model_role": "coder", "max_tokens": 200}, r"(?i)ready"),
        ("local_summarize_files", {"paths": [str(fx["src"])],
                                   "question": "Which function opens an SSH session without audit logging?"},
         r"open_session_fast"),
        ("local_analyze_data", {"path": str(fx["data"]), "question": "Which host has the most failed logins in total?"},
         r"db1"),
        ("local_web_search", {"query": "MLX Apple machine learning framework github", "max_results": 3}, r"https?://"),
        ("local_web_fetch", {"url": "https://example.com", "question": "What is this domain for?"}, r"(?i)example|documentation|illustrative"),
        ("local_research", {"question": "What is Apple's MLX framework and who develops it?", "max_sources": 3},
         r"Sources:"),
        ("local_codegen", {"spec": "A Python function is_palindrome(s: str) -> bool ignoring case and spaces.",
                           "language": "python", "max_tokens": 4000}, r"def is_palindrome"),
    ]
    if "--skip-agent" not in sys.argv:
        cases.append(("local_agent_task", {
            "task": "Add a function sub(a, b) returning a - b to mathx.py, and a file test_mathx.py with "
                    "plain assert-based tests for add and sub. Then run: python3 test_mathx.py",
            "repo_path": str(fx["repo"]), "timeout_s": 900}, r" M mathx\.py"))
    cases.append(("local_usage_report", {"days": 1}, r"TOTAL"))

    fails = 0
    params = StdioServerParameters(command="uv", args=["run", "--script", str(SERVER)], env=dict(os.environ))
    errlog_path = Path(tempfile.gettempdir()) / f"{base.name}.server-stderr.log"
    errlog = errlog_path.open("w")
    try:
        async with stdio_client(params, errlog=errlog) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                for name, args, expect in cases:
                    t0 = time.monotonic()
                    res = await s.call_tool(name, args)
                    text = "\n".join(getattr(b, "text", "") for b in res.content)
                    ok = bool(re.search(expect, text)) and not text.startswith("ERROR")
                    fails += 0 if ok else 1
                    print(f"\n{'PASS' if ok else 'FAIL'} {name} ({time.monotonic() - t0:.1f}s)")
                    print("  " + text[:1200].replace("\n", "\n  "))
    except BaseException:
        errlog.flush()
        print(f"\nServer died. Last stderr lines ({errlog_path}):\n" + "".join(errlog_path.read_text().splitlines(True)[-40:]))
        raise
    finally:
        errlog.close()
        repo = fx["repo"]
        wts = repo / ".local-agent-worktrees"
        if wts.exists():
            for wt in wts.iterdir():
                subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=repo, capture_output=True)
        shutil.rmtree(base, ignore_errors=True)
    print(f"\n{'ALL PASSED' if not fails else f'{fails} FAILED'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
