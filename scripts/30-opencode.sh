#!/usr/bin/env bash
# Phase 4: OpenCode as the *local worker* harness (local models only, no Anthropic login).
source "$(dirname "$0")/lib.sh"
need_macos
# OpenCode v2 (the old sst/tap formula is the v1 line; both install an `opencode` binary).
if ! opencode --version 2>/dev/null | grep -q 'v2\.'; then
  log "Installing OpenCode v2"
  brew list opencode >/dev/null 2>&1 && brew uninstall opencode   # v1 from sst/tap
  brew install anomalyco/tap/opencode-v2 || curl -fsSL https://opencode.ai/v2/install | bash
fi
mkdir -p "$HOME/.config/opencode"
DEST="$HOME/.config/opencode/opencode.json"
# Native V2 config. Point the default model, provider URL, and model list at the env template's
# values. Permissions are an ordered list where the LAST match wins, so denies follow the allow.
OC_MODEL=${OPENCODE_MODEL:-$(env_get OPENCODE_MODEL)}
jq --arg m "$OC_MODEL" --arg url "$(env_get LOCAL_LLM_BASE_URL)" \
   --arg g "$(env_get LOCAL_MODEL_GENERAL)" --arg c "$(env_get LOCAL_MODEL_CODER)" '
  .model = $m | .agents.title.model = $m
  | .providers.lmstudio.settings.baseURL = $url
  | .providers.lmstudio.models = ({($g): {name: "\($g) (local)"}, ($c): {name: "\($c) (local)"}}
                                  + .providers.lmstudio.models)' \
  "$REPO_ROOT/config/opencode.json" > "$DEST.tmp"
if [[ -f "$DEST" ]] && ! cmp -s "$DEST" "$DEST.tmp"; then
  cp "$DEST" "$DEST.bak.$(date +%s)"; warn "Backed up existing $DEST"
fi
mv "$DEST.tmp" "$DEST"
log "OpenCode configured: $(opencode --version 2>/dev/null || echo installed), model $OC_MODEL"
log "Try: cd some-repo && opencode run 'list the top-level modules and what they do'"
