#!/usr/bin/env bash
# Phase 3: self-hosted SearXNG (web search for the local tools) in Podman, bound to localhost.
source "$(dirname "$0")/lib.sh"
need_macos
have podman || die "podman missing (run 00-macos-prep.sh)"

if ! podman machine inspect >/dev/null 2>&1; then
  log "Creating Podman VM"
  podman machine init --cpus 4 --memory 4096 --disk-size 30
fi
podman machine start 2>/dev/null || true

SX="$CONF_DIR/searxng"; mkdir -p "$SX"
if [[ ! -f "$SX/settings.yml" ]]; then
  SECRET=$(openssl rand -hex 32)
  sed "s/__SECRET__/${SECRET}/" "$REPO_ROOT/config/searxng/settings.yml.template" > "$SX/settings.yml"
fi

log "(Re)creating searxng container on 127.0.0.1:${SEARXNG_PORT}"
podman rm -f searxng >/dev/null 2>&1 || true
podman run -d --name searxng --restart=always \
  -p "127.0.0.1:${SEARXNG_PORT}:8080" \
  -v "$SX:/etc/searxng:Z" \
  -e SEARXNG_BASE_URL="http://127.0.0.1:${SEARXNG_PORT}/" \
  docker.io/searxng/searxng:latest

if [[ "$INSTALL_LAUNCH_AGENTS" == 1 ]]; then
  install_agent com.local-agent.searxng
fi

log "Waiting for SearXNG"
for _ in $(seq 1 30); do
  curl -sf "http://127.0.0.1:${SEARXNG_PORT}/search?q=test&format=json" >/dev/null && { log "SearXNG OK"; exit 0; }
  sleep 2
done
die "SearXNG did not answer JSON. Check: podman logs searxng"
