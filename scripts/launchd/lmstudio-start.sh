#!/bin/bash
# Run at login by com.local-agent.lmstudio-server: start LM Studio's API server, then pin the
# models named by LOCAL_MODEL_PRELOAD (both on the Studio) so the first MCP call after a reboot
# doesn't pay a JIT load. Exits non-zero on failure so launchd retries (KeepAlive/SuccessfulExit).
set -u
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
ENV="$HOME/.config/local-agent/local-agent.env"
[[ -f "$ENV" ]] || ENV="$REPO/config/local-agent.env.example"
get() { sed -n "s/^$1=//p" "$ENV" | tail -1; }
LMS="$HOME/.lmstudio/bin/lms"
PORT=$(get LOCAL_LLM_BASE_URL | sed -E 's#^[a-z]+://[^:/]+:([0-9]+).*#\1#')
GENERAL=$(get LOCAL_MODEL_GENERAL); CODER=$(get LOCAL_MODEL_CODER)
CTX=$(get LOCAL_MODEL_CTX); CTX=${CTX:-65536}
PRELOAD=$(get LOCAL_MODEL_PRELOAD); PRELOAD=${PRELOAD:-both}
echo "$(date '+%F %T') lmstudio-start: port=$PORT preload=$PRELOAD ctx=$CTX"

# LM Studio's background service can take a while to come up after login.
for _ in $(seq 1 18); do
  "$LMS" server start --port "$PORT" && break
  sleep 10
done
"$LMS" server status 2>&1 | grep -q running || { echo "server did not start"; exit 1; }

# LM Studio does not always recreate its temp dir when its service starts fresh, and GGUF loads
# then fail with "ENOENT ... mkdtemp ~/.lmstudio/.internal/temp/lmstudio-chat-template-*".
mkdir -p "$HOME/.lmstudio/.internal/temp"

case "$PRELOAD" in
  both)    LOAD="$GENERAL $CODER" ;;
  general) LOAD="$GENERAL" ;;
  *)       LOAD="" ;;
esac
rc=0
for m in $LOAD; do
  "$LMS" ps 2>/dev/null | grep -q "$m" && continue
  "$LMS" load "$m" --context-length "$CTX" -y || { echo "load $m failed"; rc=1; }
done
"$LMS" ps
exit $rc
