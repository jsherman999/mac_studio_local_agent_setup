#!/usr/bin/env bash
# Phase 4: OpenCode as the *local worker* harness (local models only, no Anthropic login).
source "$(dirname "$0")/lib.sh"
need_macos
if ! have opencode; then
  log "Installing OpenCode"
  brew install sst/tap/opencode || curl -fsSL https://opencode.ai/install | bash
fi
mkdir -p "$HOME/.config/opencode"
DEST="$HOME/.config/opencode/opencode.json"
# Point the default model, provider URL, and model list at the env template's values.
OC_MODEL=${OPENCODE_MODEL:-$(env_get OPENCODE_MODEL)}
jq --arg m "$OC_MODEL" --arg url "$(env_get LOCAL_LLM_BASE_URL)" \
   --arg g "$(env_get LOCAL_MODEL_GENERAL)" --arg c "$(env_get LOCAL_MODEL_CODER)" '
  .model = $m | .small_model = $m
  | .provider.lmstudio.options.baseURL = $url
  | .provider.lmstudio.models = ({($g): {name: "\($g) (local)"}, ($c): {name: "\($c) (local)"}}
                                 + .provider.lmstudio.models)' \
  "$REPO_ROOT/config/opencode.json" > "$DEST.tmp"
if [[ -f "$DEST" ]] && ! cmp -s "$DEST" "$DEST.tmp"; then
  cp "$DEST" "$DEST.bak.$(date +%s)"; warn "Backed up existing $DEST"
fi
mv "$DEST.tmp" "$DEST"
log "OpenCode configured: $(opencode --version 2>/dev/null || echo installed), model $OC_MODEL"
log "Try: cd some-repo && opencode run 'list the top-level modules and what they do'"
