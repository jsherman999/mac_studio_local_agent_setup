#!/usr/bin/env bash
# Phase 2: LM Studio headless server + model downloads (MLX builds where available).
source "$(dirname "$0")/lib.sh"
need_macos
LMS="$HOME/.lmstudio/bin/lms"
[[ -x "$LMS" ]] || die "lms CLI not found. Open LM Studio once, then: ~/.lmstudio/bin/lms bootstrap"
"$LMS" bootstrap >/dev/null 2>&1 || true

# Model ids, context length, and port come from the env template (config/local-agent.env.example,
# or the 24 GB profile under `make poc`), so they match the MCP server and config/opencode.json.
# Check current best MLX builds in LM Studio's model browser first.
GENERAL=${GENERAL:-$(env_get LOCAL_MODEL_GENERAL)}
CODER=${CODER:-$(env_get LOCAL_MODEL_CODER)}
CTX=${CTX:-$(env_get LOCAL_MODEL_CTX)}; CTX=${CTX:-65536}
PRELOAD=${PRELOAD:-$(env_get LOCAL_MODEL_PRELOAD)}; PRELOAD=${PRELOAD:-both}

GENERAL_SRC=$(env_get LOCAL_MODEL_GENERAL_SOURCE); CODER_SRC=$(env_get LOCAL_MODEL_CODER_SOURCE)
for m in $(printf '%s\n' "$GENERAL" "$CODER" | awk '!seen[$0]++'); do
  if "$LMS" ls 2>/dev/null | grep -q "^$m "; then log "Already downloaded: $m"; continue; fi
  # Models outside LM Studio's catalog: `lms get` treats owner/repo as a Hub artifact, so it
  # needs the full Hugging Face URL.
  src=""; [[ "$m" == "$GENERAL" ]] && src=$GENERAL_SRC; [[ "$m" == "$CODER" ]] && src=${CODER_SRC:-$src}
  if [[ -n "$src" ]]; then log "Downloading $m from $src"; "$LMS" get "$src" -y; continue; fi
  # gpt-oss ships natively as MXFP4: the GGUF build is ~63 GB for 120b, while `--mlx` resolves
  # to an 8-bit MLX conversion (~124 GB) that cannot share the GPU with the coder model.
  case "$m" in openai/gpt-oss-*) FMT=--gguf ;; *) FMT=--mlx ;; esac
  log "Downloading $m (${FMT#--} if available)"
  "$LMS" get "$m" "$FMT" -y || "$LMS" get "$m" -y
done

# JIT loads (first request, or after the idle TTL unloads a model) use LM Studio's default
# context length, not the one passed to `lms load`. Set it, or reloaded models silently get 4-8k.
# Also: don't auto-load the bundled starter model, and when both roles fit (PRELOAD=both) don't
# let a JIT load of one role evict the other.
ST="$HOME/.lmstudio/settings.json"
UNLOAD_PREV=$([[ "$PRELOAD" == both ]] && echo false || echo true)
LMS_SETTINGS='.defaultContextLength = {type: "custom", value: $n} | .autoLoadBundledLLM = false
              | .developer.unloadPreviousJITModelOnLoad = $u'
if [[ -f "$ST" ]] && ! jq -e --argjson n "$CTX" --argjson u "$UNLOAD_PREV" ". == ($LMS_SETTINGS)" "$ST" >/dev/null; then
  log "LM Studio settings: JIT context $CTX, no bundled model, unloadPreviousJIT=$UNLOAD_PREV (restarting server)"
  "$LMS" server stop >/dev/null 2>&1 || true
  osascript -e 'quit app "LM Studio"' >/dev/null 2>&1 || true
  sleep 3
  jq --argjson n "$CTX" --argjson u "$UNLOAD_PREV" "$LMS_SETTINGS" "$ST" > "$ST.tmp" && mv "$ST.tmp" "$ST"
fi

log "Starting API server on :$LLM_PORT"
# The first start after LM Studio quit can time out while its background service wakes up.
for _ in 1 2 3; do "$LMS" server start --port "$LLM_PORT" && break; sleep 5; done
"$LMS" server status 2>&1 | grep -q running || die "LM Studio server did not start; check: $LMS server status"

# `lms load` pins a model: JIT never unloads it to make room. That suits the Studio (both fit);
# on small machines use PRELOAD=none so LM Studio swaps the roles in and out on demand.
case "$PRELOAD" in
  both)    LOAD=("$GENERAL" "$CODER") ;;
  general) LOAD=("$GENERAL") ;;
  *)       LOAD=() ;;
esac
log "Pinning: ${LOAD[*]:-(none; all JIT)} at ${CTX} ctx (adjust LOCAL_MODEL_CTX if OOM)"
for m in ${LOAD[@]+"${LOAD[@]}"}; do
  "$LMS" ps 2>/dev/null | grep -q "$m" || "$LMS" load "$m" --context-length "$CTX" -y || warn "load $m failed"
done
if ! "$LMS" ps 2>/dev/null | grep -q "$GENERAL"; then
  log "Warming $GENERAL with a JIT request"
  curl -sf "http://127.0.0.1:${LLM_PORT}/v1/chat/completions" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$GENERAL\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":1}" \
    >/dev/null || warn "warm-up request failed"
fi
"$LMS" ps || true

if [[ "$INSTALL_LAUNCH_AGENTS" == 1 ]]; then
  log "Install login agent for the server (log: ~/Library/Logs/local-agent-mcp/lmstudio-server.log)"
  install_agent com.local-agent.lmstudio-server
else
  log "Skipping login agent (INSTALL_LAUNCH_AGENTS=0); re-run this script after a reboot"
fi

cat <<MSG
In LM Studio > Developer > Server settings, check:
  - Just-in-Time model loading      ON  (models load on first request)
  - Auto unload unused JIT models   ON  (TTL e.g. 60 min)
  - Serve on local network          OFF (unless you deliberately want LAN access)
Verify: curl -s localhost:${LLM_PORT}/v1/models | jq '.data[].id'
MSG
