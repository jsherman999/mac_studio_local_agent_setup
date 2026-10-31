#!/bin/bash
# Run at login by com.local-agent.searxng: start the Podman VM and the searxng container, then
# wait for the JSON API. Exits non-zero on failure so launchd retries (KeepAlive/SuccessfulExit).
set -u
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
ENV="$HOME/.config/local-agent/local-agent.env"
[[ -f "$ENV" ]] || ENV="$REPO/config/local-agent.env.example"
URL=$(sed -n 's/^SEARXNG_URL=//p' "$ENV" | tail -1)
echo "$(date '+%F %T') searxng-start: $URL"

for _ in $(seq 1 12); do
  [[ "$(podman machine inspect --format '{{.State}}' 2>/dev/null)" == running ]] && break
  podman machine start 2>&1 | tail -1
  sleep 5
done
podman start searxng || exit 1

for _ in $(seq 1 30); do
  curl -sf "$URL/search?q=test&format=json" >/dev/null && { echo "SearXNG OK"; exit 0; }
  sleep 2
done
echo "SearXNG did not answer JSON; see: podman logs searxng"
exit 1
