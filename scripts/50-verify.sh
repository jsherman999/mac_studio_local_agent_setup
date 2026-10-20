#!/usr/bin/env bash
# Phase 6: end-to-end verification of every layer.
source "$(dirname "$0")/lib.sh"
ok=0; bad=0
chk() { if eval "$2" >/dev/null 2>&1; then echo "  [ok]  $1"; ok=$((ok+1)); else echo "  [!!]  $1"; bad=$((bad+1)); fi; }
log "Layer checks"
WL=$(sysctl -n iogpu.wired_limit_mb 2>/dev/null || echo 0)
echo "  [..]  GPU wired limit: $([ "$WL" -gt 0 ] && echo "${WL} MB" || echo 'macOS default (~2/3 of RAM)')"
chk "LM Studio API /v1/models"    "curl -sf localhost:${LLM_PORT}/v1/models"
chk "SearXNG JSON search"         "curl -sf 'localhost:${SEARXNG_PORT}/search?q=test&format=json'"
chk "opencode on PATH"            "command -v opencode"
chk "claude on PATH"              "command -v claude || test -x $HOME/.local/bin/claude"
chk "local-agent registered"      "claude mcp list 2>/dev/null | grep -q local-agent"
log "Offline unit tests"
uv run --script "$REPO_ROOT/tests/test_offline.py" | tail -1
log "Live stdio smoke test (real models)"
uv run --script "$REPO_ROOT/tests/smoke_stdio.py" --ask || bad=$((bad+1))
echo; log "$ok ok, $bad problems"
