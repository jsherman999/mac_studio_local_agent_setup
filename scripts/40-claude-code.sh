#!/usr/bin/env bash
# Phase 5: Claude Code (driver, on your Claude subscription) + register the local-agent MCP
# server, delegation policy, subagent, and Read-nudge hook.
source "$(dirname "$0")/lib.sh"
need_macos
have claude || { log "Installing Claude Code"; curl -fsSL https://claude.ai/install.sh | bash; }
export PATH="$HOME/.local/bin:$PATH"

log "Config -> $CONF_DIR"
mkdir -p "$CONF_DIR"
ENV_DEST="$CONF_DIR/local-agent.env"
if [[ ! -f "$ENV_DEST" ]]; then
  cp "$ENV_TEMPLATE" "$ENV_DEST"
elif ! cmp -s "$ENV_TEMPLATE" "$ENV_DEST"; then
  if [[ "${FORCE_ENV:-0}" == 1 ]]; then
    cp "$ENV_DEST" "$ENV_DEST.bak.$(date +%s)"; cp "$ENV_TEMPLATE" "$ENV_DEST"; warn "Replaced $ENV_DEST (backup kept)"
  else
    warn "$ENV_DEST differs from $(basename "$ENV_TEMPLATE"); keeping it (FORCE_ENV=1 to replace)"
  fi
fi

SERVER="$REPO_ROOT/mcp_server/local_agent_mcp.py"
log "Pre-warming uv script environment"
uv sync --script "$SERVER" >/dev/null 2>&1 || uv run --script "$SERVER" </dev/null >/dev/null 2>&1 || true

log "Registering MCP server (user scope)"
claude mcp remove --scope user local-agent >/dev/null 2>&1 || true
claude mcp add --scope user local-agent -- uv run --script "$SERVER"

log "Subagent + hook"
mkdir -p "$HOME/.claude/agents" "$HOME/.claude/hooks"
cp "$REPO_ROOT/claude/agents/local-delegate.md" "$HOME/.claude/agents/"
cp "$REPO_ROOT/claude/hooks/nudge_large_reads.py" "$HOME/.claude/hooks/"
SETTINGS="$HOME/.claude/settings.json"
[[ -f "$SETTINGS" ]] || echo '{}' > "$SETTINGS"
cp "$SETTINGS" "$SETTINGS.bak.$(date +%s)"
# merge hook (dedupe by command string)
jq -s '
  .[0] as $cur | .[1] as $add
  | $cur * {hooks: {PreToolUse: ((($cur.hooks.PreToolUse // []) + $add.hooks.PreToolUse)
                                 | unique_by(.hooks[0].command))}}' \
  "$SETTINGS" "$REPO_ROOT/claude/settings.hooks.json" > "$SETTINGS.tmp" && mv "$SETTINGS.tmp" "$SETTINGS"

log "Delegation policy -> ~/.claude/CLAUDE.md"
CMD="$HOME/.claude/CLAUDE.md"; touch "$CMD"
# replace existing managed block, if any
sed -i '' '/BEGIN local-agent delegation policy/,/END local-agent delegation policy/d' "$CMD"
cat "$REPO_ROOT/claude/CLAUDE.local-delegation.md" >> "$CMD"

cat <<MSG
Done. Next:
  1) claude            # log in with your Claude subscription (/login) if not already
  2) In Claude Code:   /mcp     -> local-agent should show 'connected' with 10 tools
  3) Ask:              "use local_status"   then try the examples in README.md
MSG
