#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp>=2.3,<3",
#   "httpx>=0.27",
#   "trafilatura>=1.12",
#   "pandas>=2.2",
#   "pyarrow>=16",
# ]
# ///
"""
local-agent MCP server
======================

Exposes the Mac Studio's local LLMs (served by LM Studio / mlx_lm / Ollama via an
OpenAI-compatible endpoint) and a self-hosted SearXNG instance to a *driver*
agent such as Claude Code. The driver decides when to delegate; these tools do
the bulk work locally and return compact results, so the expensive model reads
summaries instead of raw files, pages, and logs.

Tools
-----
  local_status          health check: models, search, opencode, config
  local_ask             one-shot prompt to a local model
  local_summarize_files map-reduce over many files/globs for a question
  local_analyze_data    profile a CSV/TSV/JSON(L)/Parquet file (+ optional Q&A)
  local_web_search      SearXNG search, compact results
  local_web_fetch       fetch + extract a page; optionally answer a question
  local_research        search -> fetch -> synthesize with citations
  local_codegen         draft code from a spec + context files
  local_agent_task      run a full local coding agent (OpenCode) in a git worktree
  local_usage_report    tokens processed locally (i.e. not sent to the driver)

Configuration is via environment variables (see config/local-agent.env.example).
Runs over stdio:  claude mcp add --scope user local-agent -- uv run --script <this file>
"""

from __future__ import annotations

import asyncio
import fnmatch
import glob
import json
import logging
import os
import re
import shutil
import signal
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def _load_env_file() -> None:
    """Load KEY=VALUE lines from LOCAL_AGENT_ENV_FILE (default ~/.config/local-agent/local-agent.env).
    Real environment variables win over the file."""
    path = Path(os.path.expanduser(os.environ.get("LOCAL_AGENT_ENV_FILE", "~/.config/local-agent/local-agent.env")))
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        os.environ.setdefault(key, val.strip().strip('"').strip("'"))


_load_env_file()


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _expand_roots(raw: str) -> list[Path]:
    roots = []
    for part in raw.split(":"):
        part = part.strip()
        if part:
            roots.append(Path(os.path.expanduser(part)).resolve())
    return roots


@dataclass(frozen=True)
class Config:
    llm_base_url: str = _env("LOCAL_LLM_BASE_URL", "http://127.0.0.1:1234/v1").rstrip("/")
    llm_api_key: str = _env("LOCAL_LLM_API_KEY", "local")
    model_general: str = _env("LOCAL_MODEL_GENERAL", "openai/gpt-oss-120b")
    model_coder: str = _env("LOCAL_MODEL_CODER", "qwen/qwen3-coder-30b")
    model_fast: str = _env("LOCAL_MODEL_FAST", "") or _env("LOCAL_MODEL_CODER", "qwen/qwen3-coder-30b")
    # Characters of input we are willing to send in one local call. ~4 chars/token.
    ctx_chars: int = int(_env("LOCAL_CTX_CHARS", "120000"))
    chunk_chars: int = int(_env("LOCAL_CHUNK_CHARS", "40000"))
    concurrency: int = int(_env("LOCAL_CONCURRENCY", "2"))
    request_timeout: float = float(_env("LOCAL_REQUEST_TIMEOUT", "900"))
    max_output_tokens: int = int(_env("LOCAL_MAX_OUTPUT_TOKENS", "4096"))
    # "low" | "medium" | "high"; empty leaves the model's default. Reasoning tokens count against
    # max_tokens, so high effort can starve short map calls of an answer.
    reasoning_effort: str = _env("LOCAL_REASONING_EFFORT", "")
    # Override for the coder model ("none" turns Qwen3.5 thinking off; at codegen's low temperature
    # it can otherwise loop until max_tokens).
    reasoning_effort_coder: str = _env("LOCAL_REASONING_EFFORT_CODER", "")
    searxng_url: str = _env("SEARXNG_URL", "http://127.0.0.1:8888").rstrip("/")
    allowed_roots: tuple[Path, ...] = tuple(
        _expand_roots(_env("LOCAL_AGENT_ALLOWED_ROOTS", "~/src:~/data:~/Documents"))
    )
    max_file_bytes: int = int(_env("LOCAL_AGENT_MAX_FILE_BYTES", str(20 * 1024 * 1024)))
    max_files: int = int(_env("LOCAL_AGENT_MAX_FILES", "500"))
    opencode_bin: str = _env("OPENCODE_BIN", "opencode")
    opencode_model: str = _env("OPENCODE_MODEL", "lmstudio/qwen/qwen3-coder-30b")
    # --standalone (OpenCode v2): a private server per task, so a timeout really stops the agent
    # instead of leaving the session running in the shared background service. Set it empty for
    # v1, which rejects the flag (its `run` already starts a private server).
    opencode_run_args: tuple[str, ...] = tuple(_env("OPENCODE_RUN_ARGS", "--standalone").split())
    allow_pandas_eval: bool = _env("LOCAL_AGENT_ALLOW_PANDAS_EVAL", "0") == "1"
    ledger_path: Path = Path(
        os.path.expanduser(_env("LOCAL_AGENT_LEDGER", "~/Library/Logs/local-agent-mcp/usage.jsonl"))
    )


CFG = Config()

# stdio transport: keep chatty libraries quiet (logs go to stderr, but keep them useful)
logging.getLogger("httpx").setLevel(logging.WARNING)

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
             ".mypy_cache", ".pytest_cache", ".tox", ".next", "target", ".local-agent-worktrees"}

mcp = MCPServer(
    name="local-agent",
    instructions=(
        "Tools that run on the user's local Mac Studio LLMs at zero API cost. Prefer them for "
        "bulk reading (many files, logs, long docs), data profiling, multi-page web research, "
        "and first-draft code. Results are produced by a weaker model than you: verify anything "
        "load-bearing (spot-check cited lines, review diffs, run tests) before relying on it."
    ),
)

# --------------------------------------------------------------------------- #
# Local LLM client + usage ledger
# --------------------------------------------------------------------------- #

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_http: httpx.AsyncClient | None = None
_sem: asyncio.Semaphore | None = None


def _client() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=httpx.Timeout(CFG.request_timeout, connect=10))
    return _http


def _semaphore() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(max(1, CFG.concurrency))
    return _sem


def _ledger(tool: str, model: str, usage: dict[str, Any], seconds: float) -> None:
    try:
        CFG.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": time.time(),
            "tool": tool,
            "model": model,
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "seconds": round(seconds, 2),
        }
        with CFG.ledger_path.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass  # accounting must never break a tool call


class LocalLLMError(RuntimeError):
    pass


async def chat(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    max_tokens: int | None = None,
    temperature: float = 0.2,
    tool: str = "chat",
) -> str:
    """Call the OpenAI-compatible chat endpoint on the local server."""
    model = model or CFG.model_general
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens or CFG.max_output_tokens,
        "stream": False,
    }
    effort = (model == CFG.model_coder and CFG.reasoning_effort_coder) or CFG.reasoning_effort
    if effort:
        payload["reasoning_effort"] = effort
    headers = {"Authorization": f"Bearer {CFG.llm_api_key}"}
    t0 = time.monotonic()
    # When memory only fits one model, a JIT load of another model (e.g. OpenCode on the coder while
    # we use the general one) can cancel our load or drop an in-flight request. Retry that once.
    for attempt in (1, 2):
        async with _semaphore():
            try:
                r = await _client().post(f"{CFG.llm_base_url}/chat/completions", json=payload, headers=headers)
            except httpx.ConnectError as e:
                raise LocalLLMError(
                    f"Cannot reach local LLM server at {CFG.llm_base_url}. Is LM Studio's server running? "
                    f"(`lms server start`). Underlying error: {e}"
                ) from e
            except httpx.TimeoutException as e:
                raise LocalLLMError(
                    f"Local model '{model}' timed out after {CFG.request_timeout}s. Try a smaller input, "
                    f"the fast model, or raise LOCAL_REQUEST_TIMEOUT."
                ) from e
            except httpx.TransportError as e:
                if attempt == 2:
                    raise LocalLLMError(f"Local LLM dropped the request for '{model}' twice: {e}") from e
                r = None
        swapped = r is None or r.status_code >= 500 or (
            r.status_code >= 400 and re.search(r"(?i)failed to load|cancel|unload|does not exist", r.text))
        if not swapped or attempt == 2:
            break
        await asyncio.sleep(3)
    if r.status_code >= 400:
        raise LocalLLMError(
            f"Local LLM returned HTTP {r.status_code} for model '{model}': {r.text[:500]}. "
            f"Check the model id with local_status."
        )
    data = r.json()
    _ledger(tool, model, data.get("usage") or {}, time.monotonic() - t0)
    try:
        choice = data["choices"][0]
        content = choice["message"].get("content") or ""
    except (KeyError, IndexError) as e:
        raise LocalLLMError(f"Unexpected response shape from local server: {str(data)[:500]}") from e
    if not content.strip() and choice.get("finish_reason") == "length":
        # thinking models count hidden reasoning against max_tokens; an empty answer is not "NONE"
        raise LocalLLMError(
            f"Model '{model}' used its whole {payload['max_tokens']}-token budget on reasoning and "
            f"returned no answer. Raise max_tokens or set LOCAL_REASONING_EFFORT=low (or none)."
        )
    # gpt-oss cites with 【...】; normalize so [file:Lx-y] / [n] citations look the same everywhere
    return _THINK_RE.sub("", content).replace("【", "[").replace("】", "]").strip()


def _model_for(kind: str, override: str | None) -> str:
    if override:
        return override
    return {"coder": CFG.model_coder, "fast": CFG.model_fast}.get(kind, CFG.model_general)


# --------------------------------------------------------------------------- #
# Filesystem helpers (with an allow-list so the driver can't point us anywhere)
# --------------------------------------------------------------------------- #


def _check_allowed(p: Path) -> Path:
    rp = p.expanduser().resolve()
    if not any(rp == root or rp.is_relative_to(root) for root in CFG.allowed_roots):
        roots = ", ".join(str(r) for r in CFG.allowed_roots)
        raise PermissionError(
            f"{rp} is outside LOCAL_AGENT_ALLOWED_ROOTS ({roots}). "
            f"Add its parent to the allow-list in the MCP server env if this is intended."
        )
    return rp


def _is_text(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            head = fh.read(8192)
        return b"\x00" not in head
    except OSError:
        return False


def _resolve_paths(patterns: list[str], exclude: list[str] | None = None) -> tuple[list[Path], list[str]]:
    """Expand files/dirs/globs into a de-duplicated list of readable text files."""
    exclude = exclude or []
    out: list[Path] = []
    notes: list[str] = []
    seen: set[Path] = set()

    def add(fp: Path) -> None:
        if fp in seen or len(out) >= CFG.max_files:
            return
        if any(fnmatch.fnmatch(str(fp), pat) or fnmatch.fnmatch(fp.name, pat) for pat in exclude):
            return
        try:
            size = fp.stat().st_size
        except OSError:
            return
        if size > CFG.max_file_bytes:
            notes.append(f"skipped (too large, {size} bytes): {fp}")
            return
        if not _is_text(fp):
            return
        seen.add(fp)
        out.append(fp)

    for pat in patterns:
        expanded = os.path.expanduser(pat)
        matches = glob.glob(expanded, recursive=True) or ([expanded] if os.path.exists(expanded) else [])
        if not matches:
            notes.append(f"no match: {pat}")
        for m in matches:
            p = _check_allowed(Path(m))
            if p.is_dir():
                for dirpath, dirnames, filenames in os.walk(p):
                    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                    for f in sorted(filenames):
                        add(Path(dirpath) / f)
            elif p.is_file():
                add(p)
    if len(out) >= CFG.max_files:
        notes.append(f"file cap reached ({CFG.max_files}); narrow the globs or raise LOCAL_AGENT_MAX_FILES")
    return out, notes


def _chunks(text: str, size: int, overlap: int = 500) -> list[str]:
    if len(text) <= size:
        return [text]
    out, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        # prefer to break on a newline near the end of the window
        nl = text.rfind("\n", start + size // 2, end)
        if nl != -1 and end < len(text):
            end = nl + 1
        out.append(text[start:end])
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return out


# --------------------------------------------------------------------------- #
# Map-reduce core: the thing that actually saves driver tokens
# --------------------------------------------------------------------------- #

MAP_SYSTEM = (
    "You extract information for another AI that cannot see this text. Be precise and terse. "
    "Quote exact identifiers, numbers, error strings, and line numbers where useful. "
    "Cite the source label in square brackets for every claim, e.g. [src/app.py:L120-140]. "
    "If the excerpt contains nothing relevant, reply with exactly: NONE"
)
REDUCE_SYSTEM = (
    "You combine extracted notes into one answer for another AI. Keep every citation in square "
    "brackets. Do not invent facts that are not in the notes. Say clearly what was not found. "
    "Be concise: prefer bullet points, no preamble."
)


@dataclass
class Doc:
    label: str
    text: str
    meta: dict[str, Any] = field(default_factory=dict)


def _number_lines(text: str, first: int = 1) -> str:
    return "\n".join(f"{i}: {line}" for i, line in enumerate(text.splitlines(), start=first))


async def map_reduce(docs: list[Doc], question: str, *, model: str, tool: str, max_answer_tokens: int) -> str:
    total = sum(len(d.text) for d in docs)
    if total <= CFG.ctx_chars:
        body = "\n\n".join(f"===== [{d.label}] =====\n{d.text}" for d in docs)
        return await chat(
            [{"role": "system", "content": REDUCE_SYSTEM},
             {"role": "user", "content": f"QUESTION:\n{question}\n\nMATERIAL:\n{body}"}],
            model=model, max_tokens=max_answer_tokens, tool=tool,
        )

    # MAP: per chunk
    jobs: list[tuple[str, str]] = []
    for d in docs:
        for i, ch in enumerate(_chunks(d.text, CFG.chunk_chars)):
            label = d.label if len(d.text) <= CFG.chunk_chars else f"{d.label}#part{i + 1}"
            jobs.append((label, ch))

    async def run(label: str, ch: str) -> str:
        res = await chat(
            [{"role": "system", "content": MAP_SYSTEM},
             {"role": "user", "content": f"QUESTION:\n{question}\n\nEXCERPT [{label}]:\n{ch}"}],
            model=model, max_tokens=1200, tool=f"{tool}:map",
        )
        return "" if res.strip().upper() == "NONE" else f"--- notes from [{label}] ---\n{res}"

    # First chunk alone: if the model is not loaded yet, concurrent requests would each trigger a
    # JIT load, which LM Studio can fail with a 500. Then fan out.
    first = await run(*jobs[0])
    notes = [n for n in [first, *await asyncio.gather(*(run(lbl, ch) for lbl, ch in jobs[1:]))] if n]
    if not notes:
        return f"No relevant information found in {len(docs)} source(s) ({total:,} chars scanned)."

    # REDUCE (recursively if the notes themselves are too long)
    while sum(len(n) for n in notes) > CFG.ctx_chars:
        groups, cur, size = [], [], 0
        for n in notes:
            if size + len(n) > CFG.ctx_chars and cur:
                groups.append(cur)
                cur, size = [], 0
            cur.append(n)
            size += len(n)
        groups.append(cur)
        notes = list(await asyncio.gather(*(
            chat([{"role": "system", "content": REDUCE_SYSTEM},
                  {"role": "user", "content": f"QUESTION:\n{question}\n\nNOTES:\n" + "\n\n".join(g)}],
                 model=model, max_tokens=2000, tool=f"{tool}:reduce")
            for g in groups
        )))

    return await chat(
        [{"role": "system", "content": REDUCE_SYSTEM},
         {"role": "user", "content": f"QUESTION:\n{question}\n\nNOTES:\n" + "\n\n".join(notes)}],
        model=model, max_tokens=max_answer_tokens, tool=tool,
    )


def _footer(**kv: Any) -> str:
    parts = [f"{k}={v}" for k, v in kv.items() if v not in (None, "", [])]
    return "\n\n---\n[local-agent] " + " | ".join(parts)


def _err(e: Exception) -> str:
    return f"ERROR ({type(e).__name__}): {e}"


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
RO_WEB = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)


@mcp.tool(annotations=RO)
async def local_status() -> str:
    """Health check for the local stack: lists models loaded/available on the local LLM server,
    pings SearXNG, checks the OpenCode binary, and shows the active configuration. Call this first
    if any other local_* tool errors."""
    lines = [f"LLM server: {CFG.llm_base_url}"]
    try:
        r = await _client().get(f"{CFG.llm_base_url}/models",
                                headers={"Authorization": f"Bearer {CFG.llm_api_key}"}, timeout=10)
        ids = [m.get("id") for m in r.json().get("data", [])]
        lines.append(f"  models available: {', '.join(ids) or '(none)'}")
        for role, mid in (("general", CFG.model_general), ("coder", CFG.model_coder), ("fast", CFG.model_fast)):
            lines.append(f"  {role:8s}-> {mid} {'OK' if mid in ids else '(NOT LISTED - check id)'}")
    except Exception as e:  # noqa: BLE001
        lines.append(f"  UNREACHABLE: {e}")
    try:
        r = await _client().get(f"{CFG.searxng_url}/search", params={"q": "test", "format": "json"}, timeout=15)
        lines.append(f"SearXNG {CFG.searxng_url}: HTTP {r.status_code}"
                     + ("" if r.status_code == 200 else " (is format=json enabled in settings.yml?)"))
    except Exception as e:  # noqa: BLE001
        lines.append(f"SearXNG {CFG.searxng_url}: UNREACHABLE ({e})")
    oc = shutil.which(CFG.opencode_bin)
    lines.append(f"OpenCode: {oc or 'NOT FOUND'} (model {CFG.opencode_model})")
    lines.append(f"Allowed roots: {', '.join(map(str, CFG.allowed_roots))}")
    lines.append(f"Context budget: {CFG.ctx_chars:,} chars/call, chunk {CFG.chunk_chars:,}, concurrency {CFG.concurrency}")
    return "\n".join(lines)


@mcp.tool(annotations=RO)
async def local_ask(
    prompt: str,
    system: str = "",
    model_role: str = "general",
    model: str = "",
    max_tokens: int = 2048,
    temperature: float = 0.2,
) -> str:
    """Send a single prompt to a local model and return its answer. Use for self-contained
    sub-questions, rewriting, classification, regex/jq/awk drafting, or second opinions where the
    full input is already in the prompt.

    Args:
        prompt: The full prompt (include any text the model needs; it cannot see your context).
        system: Optional system prompt.
        model_role: "general" (strongest reasoning), "coder", or "fast".
        model: Explicit model id; overrides model_role.
        max_tokens: Answer length cap.
        temperature: Sampling temperature.
    """
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    m = _model_for(model_role, model or None)
    try:
        return await chat(msgs, model=m, max_tokens=max_tokens, temperature=temperature, tool="local_ask")
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
async def local_summarize_files(
    paths: list[str],
    question: str,
    exclude: list[str] | None = None,
    model_role: str = "general",
    max_answer_tokens: int = 2500,
) -> str:
    """Read many files locally and answer a question about them, returning a compact answer with
    [file:Lstart-end] citations. Ideal for: "where is X configured", "summarize these logs",
    "which modules call Y", "what changed across these docs". Handles inputs far larger than your
    own context via map-reduce. Spot-check cited lines with Read(offset, limit) before acting.

    Args:
        paths: Files, directories, or globs (e.g. ["~/src/app/**/*.py", "/var/log/app/*.log"]).
            Must be under LOCAL_AGENT_ALLOWED_ROOTS. Common junk dirs are skipped.
        question: What you want to know. Be specific about the output you need.
        exclude: Optional fnmatch patterns to skip (e.g. ["*.min.js", "*/tests/*"]).
        model_role: "general" (default) or "fast" for simple extraction.
        max_answer_tokens: Cap on the final answer length.
    """
    try:
        files, notes = _resolve_paths(paths, exclude)
        if not files:
            return "No readable text files matched. " + "; ".join(notes)
        docs: list[Doc] = []
        for fp in files:
            text = fp.read_text(errors="replace")
            # number lines so the model can cite precise ranges
            first = 1
            for part in _chunks(text, CFG.chunk_chars, overlap=0):
                n_lines = max(1, len(part.splitlines()))
                last = first + n_lines - 1
                docs.append(Doc(label=f"{fp}:L{first}-{last}", text=_number_lines(part, first)))
                first = last + 1
        total = sum(len(d.text) for d in docs)
        ans = await map_reduce(docs, question, model=_model_for(model_role, None),
                               tool="local_summarize_files", max_answer_tokens=max_answer_tokens)
        return ans + _footer(files=len(files), chars_read=f"{total:,}", notes="; ".join(notes[:5]))
    except Exception as e:  # noqa: BLE001
        return _err(e)


def _load_frame(path: Path):
    import pandas as pd

    suf = path.suffix.lower()
    if suf in (".csv", ".txt"):
        return pd.read_csv(path, low_memory=False)
    if suf == ".tsv":
        return pd.read_csv(path, sep="\t", low_memory=False)
    if suf in (".jsonl", ".ndjson"):
        return pd.read_json(path, lines=True)
    if suf == ".json":
        return pd.read_json(path)
    if suf == ".parquet":
        return pd.read_parquet(path)
    raise ValueError(f"Unsupported data file type '{suf}'. Use csv, tsv, json, jsonl, or parquet.")


def _profile(df) -> str:
    import pandas as pd

    buf = [f"shape: {df.shape[0]:,} rows x {df.shape[1]} cols", "", "columns:"]
    for c in df.columns:
        s = df[c]
        nn = int(s.isna().sum())
        line = f"  - {c} ({s.dtype}) nulls={nn:,} unique={s.nunique(dropna=True):,}"
        if pd.api.types.is_numeric_dtype(s):
            d = s.describe()
            line += f" min={d.get('min')} p50={s.median()} max={d.get('max')} mean={round(float(d.get('mean', 0)), 4)}"
        elif s.nunique(dropna=True) <= 25:
            top = s.value_counts(dropna=True).head(8).to_dict()
            line += f" top={top}"
        buf.append(line)
    buf += ["", "head(8):", df.head(8).to_string(max_colwidth=60)]
    return "\n".join(buf)


@mcp.tool(annotations=RO)
async def local_analyze_data(
    path: str,
    question: str = "",
    model_role: str = "general",
) -> str:
    """Profile a tabular data file locally (CSV/TSV/JSON/JSONL/Parquet): shape, dtypes, nulls,
    ranges, top values, sample rows. Optionally answer a question about it. With
    LOCAL_AGENT_ALLOW_PANDAS_EVAL=1 the local model writes and runs pandas expressions to answer
    exact-count questions (the code used is returned so you can verify it); otherwise answers come
    from the profile only.

    Args:
        path: Data file under LOCAL_AGENT_ALLOWED_ROOTS.
        question: Optional question, e.g. "which host has the most failed logins per day?".
        model_role: "general" or "coder".
    """
    try:
        p = _check_allowed(Path(path))
        df = await asyncio.to_thread(_load_frame, p)
        prof = _profile(df)
        if not question:
            return prof
        m = _model_for(model_role, None)
        if not CFG.allow_pandas_eval:
            ans = await chat(
                [{"role": "system", "content": "Answer using only this data profile. If the profile "
                  "cannot answer exactly, say what computation is needed."},
                 {"role": "user", "content": f"PROFILE:\n{prof}\n\nQUESTION: {question}"}],
                model=m, tool="local_analyze_data")
            return f"{ans}\n\n{prof}" + _footer(mode="profile-only")
        return await _pandas_loop(df, prof, question, m)
    except Exception as e:  # noqa: BLE001
        return _err(e)


async def _pandas_loop(df, prof: str, question: str, model: str, max_steps: int = 4) -> str:
    """Let the local model iteratively evaluate pandas expressions. Opt-in only: evaluating
    model-written code is a risk if the data could contain prompt injection."""
    import pandas as pd

    sys_p = ("You answer questions about a pandas DataFrame named df. Reply with EITHER\n"
             "EXPR: <one single-line Python expression using df and pd>\nOR\nANSWER: <final answer>\n"
             "Use EXPR to compute; you will see its result. No imports, no assignments, no I/O.")
    transcript = f"PROFILE:\n{prof}\n\nQUESTION: {question}"
    trail: list[str] = []
    for _ in range(max_steps):
        out = await chat([{"role": "system", "content": sys_p}, {"role": "user", "content": transcript}],
                         model=model, max_tokens=600, tool="local_analyze_data:eval")
        m_ans = re.search(r"ANSWER:\s*(.*)", out, re.DOTALL)
        m_expr = re.search(r"EXPR:\s*(.+)", out)
        if m_ans and not m_expr:
            return m_ans.group(1).strip() + "\n\nCode used:\n" + ("\n".join(trail) or "(none)") + _footer(mode="pandas-eval")
        if not m_expr:
            return out + _footer(mode="pandas-eval", note="model did not follow protocol")
        expr = m_expr.group(1).strip().strip("`")
        if re.search(r"__|import|open\(|exec|eval|os\.|sys\.|subprocess|to_csv|to_parquet|to_json", expr):
            result = "REJECTED: disallowed construct"
        else:
            try:
                val = await asyncio.to_thread(eval, expr, {"__builtins__": {}, "pd": pd}, {"df": df})  # noqa: S307
                result = str(val)[:4000]
            except Exception as e:  # noqa: BLE001
                result = f"error: {e}"
        trail.append(f">>> {expr}\n{result[:400]}")
        transcript += f"\n\nEXPR: {expr}\nRESULT:\n{result}"
    return "No final answer within step limit.\n\nCode tried:\n" + "\n".join(trail)


# ---- web ------------------------------------------------------------------ #


async def _searx(query: str, max_results: int, categories: str, time_range: str) -> list[dict[str, str]]:
    params = {"q": query, "format": "json", "categories": categories}
    if time_range:
        params["time_range"] = time_range
    r = await _client().get(f"{CFG.searxng_url}/search", params=params, timeout=30)
    if r.status_code == 403:
        raise RuntimeError("SearXNG returned 403: enable 'json' under search.formats in settings.yml")
    r.raise_for_status()
    out, seen = [], set()
    for item in r.json().get("results", []):
        url = item.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        out.append({"title": item.get("title", "")[:200], "url": url,
                    "snippet": (item.get("content") or "")[:300],
                    "date": item.get("publishedDate") or ""})
        if len(out) >= max_results:
            break
    return out


async def _fetch_text(url: str, max_chars: int) -> str:
    import trafilatura

    if not re.match(r"^https?://", url):
        raise ValueError("Only http(s) URLs are supported")
    r = await _client().get(url, timeout=30, follow_redirects=True,
                            headers={"User-Agent": "Mozilla/5.0 (Macintosh) local-agent-mcp/1.0"})
    r.raise_for_status()
    ctype = r.headers.get("content-type", "")
    if "html" in ctype or not ctype:
        text = await asyncio.to_thread(trafilatura.extract, r.text, include_tables=True,
                                       include_links=False, favor_recall=True) or ""
    else:
        text = r.text
    return text[:max_chars]


@mcp.tool(annotations=RO_WEB)
async def local_web_search(
    query: str,
    max_results: int = 8,
    categories: str = "general",
    time_range: str = "",
) -> str:
    """Web search via the self-hosted SearXNG metasearch instance. Returns title, URL, snippet.
    Use local_research instead when you need an answer synthesized from several pages.

    Args:
        query: Search query.
        max_results: 1-20.
        categories: SearXNG category: general, news, it, science, files, ...
        time_range: "", "day", "week", "month", or "year".
    """
    try:
        res = await _searx(query, max(1, min(20, max_results)), categories, time_range)
        if not res:
            return "No results."
        return "\n".join(f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet']}" + (f" ({r['date']})" if r['date'] else "")
                         for i, r in enumerate(res, 1))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO_WEB)
async def local_web_fetch(url: str, question: str = "", max_chars: int = 200000) -> str:
    """Fetch a web page and extract its main text locally. If `question` is given, the local model
    reads the page and returns only the relevant answer with quotes (cheap for you); otherwise the
    extracted text is returned, truncated to 12k chars.

    Args:
        url: http(s) URL.
        question: Optional: what you need from the page.
        max_chars: Max characters of page text to process.
    """
    try:
        text = await _fetch_text(url, max_chars)
        if not text:
            return "Fetched, but no main text could be extracted (JS-heavy page?)."
        if not question:
            return text[:12000] + (f"\n\n[truncated; {len(text):,} chars total]" if len(text) > 12000 else "")
        ans = await map_reduce([Doc(label=url, text=text)], question, model=CFG.model_general,
                               tool="local_web_fetch", max_answer_tokens=1500)
        return ans + _footer(chars_read=f"{len(text):,}")
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO_WEB)
async def local_research(
    question: str,
    queries: list[str] | None = None,
    max_sources: int = 6,
    time_range: str = "",
) -> str:
    """Multi-page web research done entirely locally: search SearXNG, fetch the top pages, read
    them with the local model, and return a synthesized answer with numbered citations and a source
    list. Saves you from reading whole pages. Verify any critical claim by fetching its source.

    Args:
        question: The research question.
        queries: Optional explicit search queries (default: derived from the question).
        max_sources: Pages to read (1-12).
        time_range: "", "day", "week", "month", or "year".
    """
    try:
        if not queries:
            q_raw = await chat(
                [{"role": "user", "content": "Write 3 short, distinct web search queries (one per line, "
                  f"no numbering) to research:\n{question}"}],
                model=CFG.model_fast, max_tokens=150, tool="local_research:queries")
            queries = [q.strip(" -•\t") for q in q_raw.splitlines() if q.strip()][:3] or [question]
        hits: list[dict[str, str]] = []
        seen: set[str] = set()
        for q in queries:
            for h in await _searx(q, 8, "general", time_range):
                if h["url"] not in seen:
                    seen.add(h["url"])
                    hits.append(h)
        hits = hits[: max(1, min(12, max_sources))]
        if not hits:
            return "Search returned no results for: " + "; ".join(queries)

        async def grab(i: int, h: dict[str, str]) -> Doc | None:
            try:
                t = await _fetch_text(h["url"], 60000)
                return Doc(label=f"{i}", text=f"TITLE: {h['title']}\nURL: {h['url']}\n\n{t}") if t else None
            except Exception:  # noqa: BLE001
                return None

        docs = [d for d in await asyncio.gather(*(grab(i, h) for i, h in enumerate(hits, 1))) if d]
        if not docs:
            return "Found results but could not fetch any page text.\n" + "\n".join(h["url"] for h in hits)
        ans = await map_reduce(docs, question + "\nCite sources as [n] using the bracket labels.",
                               model=CFG.model_general, tool="local_research", max_answer_tokens=2500)
        used = {d.label for d in docs}
        src = "\n".join(f"[{i}] {h['title']} - {h['url']}" for i, h in enumerate(hits, 1) if str(i) in used)
        return f"{ans}\n\nSources:\n{src}" + _footer(queries=" | ".join(queries), pages_read=len(docs))
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ---- code ----------------------------------------------------------------- #


@mcp.tool(annotations=RO)
async def local_codegen(
    spec: str,
    context_paths: list[str] | None = None,
    language: str = "",
    max_tokens: int = 6000,
) -> str:
    """Draft code with the local coder model from a spec plus optional context files. Returns code
    only; nothing is written to disk. Good for boilerplate, tests, scripts, data transforms, and
    first drafts you will review. For multi-file edits with tool use, use local_agent_task.

    Args:
        spec: What to write: interfaces, constraints, edge cases, style. Be explicit.
        context_paths: Files/globs to include as reference (style, types, call sites).
        language: Target language hint.
        max_tokens: Output cap.
    """
    try:
        ctx = ""
        if context_paths:
            files, _ = _resolve_paths(context_paths)
            budget = CFG.ctx_chars // 2
            for fp in files:
                t = fp.read_text(errors="replace")
                if len(ctx) + len(t) > budget:
                    ctx += f"\n===== {fp} (omitted: budget) =====\n"
                    continue
                ctx += f"\n===== {fp} =====\n{t}\n"
        sys_p = ("You are a senior engineer. Output only the requested code in fenced blocks, with a "
                 "filename comment on the first line of each block. Match the style of the context. "
                 "No explanations unless a design choice is non-obvious (one short note at the end).")
        user = f"LANGUAGE: {language or 'infer'}\n\nSPEC:\n{spec}\n\nCONTEXT FILES:{ctx or ' (none)'}"
        return await chat([{"role": "system", "content": sys_p}, {"role": "user", "content": user}],
                          model=CFG.model_coder, max_tokens=max_tokens, temperature=0.1, tool="local_codegen")
    except Exception as e:  # noqa: BLE001
        return _err(e)


async def _run(cmd: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
    # stdin must not be inherited: over stdio it is the MCP transport, and `opencode run` reads
    # piped stdin into its prompt. PWD must match cwd: OpenCode picks its project directory from
    # $PWD, so an inherited PWD sends the agent's edits to the server's directory, not the worktree.
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(cwd), stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                                env={**os.environ, "PWD": str(cwd)}, start_new_session=True)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # the whole group, so agent tool subprocesses die too
        except ProcessLookupError:
            pass
        await proc.wait()
        return 124, f"timed out after {timeout}s"
    return proc.returncode or 0, out.decode(errors="replace")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                      idempotentHint=False, openWorldHint=False))
async def local_agent_task(
    task: str,
    repo_path: str,
    use_worktree: bool = True,
    timeout_s: int = 1200,
    model: str = "",
) -> str:
    """Hand a self-contained coding task to a full local agent (OpenCode on the local coder model)
    that can read, edit, and run commands. By default it works in a fresh git worktree on branch
    local-agent/<id>, so your checkout is untouched; you get back the agent's summary and a diff
    stat, then review with `git -C <worktree> diff` and merge or discard. Good for: writing tests,
    mechanical refactors, adding docstrings/types, porting scripts, fixing lint across many files.

    Args:
        task: Complete instructions. Include acceptance criteria and the test command to run.
        repo_path: Git repository root under LOCAL_AGENT_ALLOWED_ROOTS.
        use_worktree: Isolate changes in a new worktree/branch (strongly recommended).
        timeout_s: Hard time limit.
        model: OpenCode model id (provider/model); defaults to OPENCODE_MODEL.
    """
    try:
        repo = _check_allowed(Path(repo_path))
        if not (repo / ".git").exists():
            return f"ERROR: {repo} is not a git repository root."
        oc = shutil.which(CFG.opencode_bin)
        if not oc:
            return "ERROR: opencode not found on PATH. Run scripts/30-opencode.sh."
        workdir = repo
        branch = ""
        if use_worktree:
            tid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
            branch = f"local-agent/{tid}"
            workdir = repo / ".local-agent-worktrees" / tid
            code, out = await _run(["git", "worktree", "add", "-b", branch, str(workdir)], repo, 60)
            if code:
                return f"ERROR creating worktree: {out}"
            # keep worktrees out of `git status` in the main checkout
            excl = repo / ".git" / "info" / "exclude"
            try:
                if ".local-agent-worktrees/" not in excl.read_text():
                    with excl.open("a") as fh:
                        fh.write("\n.local-agent-worktrees/\n")
            except OSError:
                pass
        t0 = time.monotonic()
        code, out = await _run([oc, "run", *CFG.opencode_run_args, "--model", model or CFG.opencode_model, task],
                               workdir, timeout_s)
        _ledger("local_agent_task", model or CFG.opencode_model, {}, time.monotonic() - t0)
        _, stat = await _run(["git", "status", "--short"], workdir, 30)
        _, diffstat = await _run(["git", "diff", "--stat", "HEAD"], workdir, 30)
        tail = out[-6000:]
        return (f"exit={code} elapsed={int(time.monotonic() - t0)}s\nworkdir: {workdir}\n"
                + (f"branch: {branch}\n" if branch else "")
                + f"\n## git status\n{stat or '(clean)'}\n## diff --stat\n{diffstat or '(no changes)'}\n"
                f"## agent output (tail)\n{tail}\n\n"
                f"Review: git -C {workdir} diff   |   Discard: git -C {repo} worktree remove --force {workdir}"
                + (f" && git -C {repo} branch -D {branch}" if branch else ""))
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ---- accounting ----------------------------------------------------------- #


@mcp.tool(annotations=RO)
async def local_usage_report(days: float = 7.0) -> str:
    """Summarize tokens processed by local models (work the driver did not pay for) over the last
    N days, grouped by tool. Useful for judging whether delegation is paying off."""
    try:
        cutoff = time.time() - days * 86400
        agg: dict[str, list[float]] = {}
        if CFG.ledger_path.exists():
            for line in CFG.ledger_path.read_text().splitlines():
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("ts", 0) < cutoff:
                    continue
                key = r["tool"].split(":")[0]
                a = agg.setdefault(key, [0, 0, 0, 0.0])
                a[0] += 1
                a[1] += r.get("prompt_tokens", 0)
                a[2] += r.get("completion_tokens", 0)
                a[3] += r.get("seconds", 0.0)
        if not agg:
            return f"No local usage recorded in the last {days:g} days."
        rows = [f"{'tool':24s} {'calls':>6s} {'in_tok':>12s} {'out_tok':>10s} {'gpu_min':>8s}"]
        tot = [0, 0, 0, 0.0]
        for k, (c, pi, po, s) in sorted(agg.items(), key=lambda kv: -kv[1][1]):
            rows.append(f"{k:24s} {int(c):6d} {int(pi):12,d} {int(po):10,d} {s / 60:8.1f}")
            tot = [tot[0] + c, tot[1] + pi, tot[2] + po, tot[3] + s]
        rows.append(f"{'TOTAL':24s} {int(tot[0]):6d} {int(tot[1]):12,d} {int(tot[2]):10,d} {tot[3] / 60:8.1f}")
        return "\n".join(rows)
    except Exception as e:  # noqa: BLE001
        return _err(e)


if __name__ == "__main__":
    mcp.run()  # stdio
