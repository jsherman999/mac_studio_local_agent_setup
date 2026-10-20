#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=2.3,<3", "httpx>=0.27", "trafilatura>=1.12", "pandas>=2.2", "pyarrow>=16"]
# ///
"""Offline tests: mock the LLM server and SearXNG with httpx.MockTransport, then call the tools
through the MCP server object. No models or network needed.

    uv run --script tests/test_offline.py
"""
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import httpx

TMP = Path(tempfile.mkdtemp())
os.environ["LOCAL_AGENT_ALLOWED_ROOTS"] = str(TMP)
os.environ["LOCAL_AGENT_LEDGER"] = str(TMP / "usage.jsonl")
os.environ["LOCAL_CTX_CHARS"] = "3000"      # force the map-reduce path
os.environ["LOCAL_CHUNK_CHARS"] = "1500"
os.environ["LOCAL_AGENT_ALLOW_PANDAS_EVAL"] = "1"
os.environ["LOCAL_REASONING_EFFORT"] = "low"
os.environ["LOCAL_REASONING_EFFORT_CODER"] = "none"

spec = importlib.util.spec_from_file_location(
    "lam", Path(__file__).resolve().parents[1] / "mcp_server" / "local_agent_mcp.py")
lam = importlib.util.module_from_spec(spec)
sys.modules['lam'] = lam
spec.loader.exec_module(lam)

calls: list[str] = []
efforts: dict[str, str | None] = {}


def handler(req: httpx.Request) -> httpx.Response:
    url = str(req.url)
    if url.endswith("/models"):
        return httpx.Response(200, json={"data": [{"id": lam.CFG.model_general}, {"id": lam.CFG.model_coder}]})
    if url.endswith("/chat/completions"):
        body = json.loads(req.content)
        efforts[body["model"]] = body.get("reasoning_effort")
        sys_msg = next((m["content"] for m in body["messages"] if m["role"] == "system"), "")
        user = body["messages"][-1]["content"]
        if user == "swap race" and calls.count("swap") == 0:
            calls.append("swap")
            return httpx.Response(400, json={"error": {"message": 'Failed to load model "x". Error: Operation canceled.'}})
        if user == "think forever":
            return httpx.Response(200, json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                                             "usage": {"prompt_tokens": 3, "completion_tokens": 20}})
        if "DataFrame named df" in sys_msg:
            reply = "ANSWER: 3 rows" if "RESULT:" in user else "EXPR: len(df)"
        elif sys_msg.startswith("You extract"):
            reply = "found NEEDLE [x]" if "NEEDLE" in user else "NONE"
            calls.append("map")
        else:
            reply = "<think>hidden</think>final answer about NEEDLE 【1】"
            calls.append("reduce")
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}],
                                         "usage": {"prompt_tokens": len(user) // 4, "completion_tokens": 10}})
    if "/search" in url:
        return httpx.Response(200, json={"results": [
            {"title": "Page A", "url": "https://a.example/x", "content": "about needle"},
            {"title": "Page B", "url": "https://b.example/y", "content": "more"}]})
    if "a.example" in url or "b.example" in url:
        html = "<html><body><article><h1>T</h1><p>" + ("filler text. " * 50) + " NEEDLE fact.</p></article></body></html>"
        return httpx.Response(200, html=html)
    return httpx.Response(404)


lam._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))


def text_of(result) -> str:
    # MCPServer.call_tool may return content blocks or (blocks, structured)
    if isinstance(result, tuple):
        result = result[0]
    if hasattr(result, "content"):
        result = result.content
    if isinstance(result, list):
        return "\n".join(getattr(b, "text", str(b)) for b in result)
    return getattr(result, "text", str(result))


async def main() -> int:
    fails = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal fails
        print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"\n    {detail[:800]}"))
        fails += 0 if cond else 1

    tools = {t.name for t in await lam.mcp.list_tools()}
    expected = {"local_status", "local_ask", "local_summarize_files", "local_analyze_data", "local_web_search",
                "local_web_fetch", "local_research", "local_codegen", "local_agent_task", "local_usage_report"}
    check("all tools registered", expected <= tools, str(tools))

    out = text_of(await lam.mcp.call_tool("local_status", {}))
    check("status lists models", "OK" in out, out)

    out = text_of(await lam.mcp.call_tool("local_ask", {"prompt": "hi"}))
    check("ask strips <think>", out.startswith("final answer") and "hidden" not in out, out)
    check("ask normalizes 【n】 citations", out.endswith("NEEDLE [1]"), out)
    await lam.mcp.call_tool("local_ask", {"prompt": "hi", "model_role": "coder"})
    check("reasoning effort per role", efforts == {lam.CFG.model_general: "low", lam.CFG.model_coder: "none"},
          str(efforts))

    calls.clear()
    out = text_of(await lam.mcp.call_tool("local_ask", {"prompt": "swap race"}))
    check("ask retries a cancelled model load", calls.count("swap") == 1 and out.startswith("final answer"), out)
    out = text_of(await lam.mcp.call_tool("local_ask", {"prompt": "think forever", "max_tokens": 20}))
    check("ask reports reasoning-starved answer", "budget on reasoning" in out, out)

    (TMP / "src").mkdir()
    for i in range(6):
        (TMP / "src" / f"f{i}.py").write_text("x = 1\n" * 400 + ("NEEDLE = 42\n" if i == 3 else ""))
    (TMP / "src" / "blob.bin").write_bytes(b"\x00\x01binary")
    calls.clear()
    out = text_of(await lam.mcp.call_tool("local_summarize_files",
                                          {"paths": [str(TMP / "src")], "question": "where is NEEDLE?"}))
    check("summarize map-reduce", "map" in calls and "reduce" in calls and "files=6" in out, out + str(calls))

    out = text_of(await lam.mcp.call_tool("local_summarize_files", {"paths": ["/etc/passwd"], "question": "q"}))
    check("allow-list enforced", "outside LOCAL_AGENT_ALLOWED_ROOTS" in out, out)

    (TMP / "d.csv").write_text("host,fails\na,1\nb,5\nc,2\n")
    out = text_of(await lam.mcp.call_tool("local_analyze_data", {"path": str(TMP / "d.csv")}))
    check("data profile", "3 rows x 2 cols" in out, out)
    out = text_of(await lam.mcp.call_tool("local_analyze_data", {"path": str(TMP / "d.csv"), "question": "rows?"}))
    check("pandas eval loop", "3 rows" in out and ">>> len(df)" in out, out)

    out = text_of(await lam.mcp.call_tool("local_web_search", {"query": "needle"}))
    check("web search", "https://a.example/x" in out, out)

    out = text_of(await lam.mcp.call_tool("local_research", {"question": "what is the needle?",
                                                              "queries": ["needle"]}))
    check("research w/ sources", "Sources:" in out and "[1] Page A" in out, out)

    # agent subprocesses: PWD must follow cwd (OpenCode uses it as the project dir), stdin must not
    # be the MCP transport (cat would block on it)
    code, out = await lam._run(["sh", "-c", 'echo "$PWD"; cat'], TMP / "src", 10)
    check("subprocess PWD=cwd, stdin closed", code == 0 and out.strip() == str(TMP / "src"), f"{code} {out}")

    out = text_of(await lam.mcp.call_tool("local_usage_report", {}))
    check("usage ledger", "TOTAL" in out, out)

    await lam._http.aclose()
    print(f"\n{'ALL PASSED' if not fails else f'{fails} FAILED'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
