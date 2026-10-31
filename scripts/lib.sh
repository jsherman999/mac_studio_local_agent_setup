# shellcheck shell=bash
# Shared helpers for setup scripts.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF_DIR="$HOME/.config/local-agent"
log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }
need_macos() { [[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "arm64" ]] || die "Run on Apple silicon macOS."; }
have() { command -v "$1" >/dev/null 2>&1; }
brew_shellenv() { [[ -x /opt/homebrew/bin/brew ]] && eval "$(/opt/homebrew/bin/brew shellenv)"; }

# Env template for the MCP server (copied to $CONF_DIR/local-agent.env). Scripts read model ids,
# ports, and context length from it so every layer stays in sync. `make poc` points it at the
# 24 GB profile.
ENV_TEMPLATE="${ENV_TEMPLATE:-$REPO_ROOT/config/local-agent.env.example}"
[[ "$ENV_TEMPLATE" = /* ]] || ENV_TEMPLATE="$REPO_ROOT/$ENV_TEMPLATE"
env_get() { sed -n "s/^$1=//p" "$ENV_TEMPLATE" | tail -1; }
url_port() { sed -E 's#^[a-z]+://[^:/]+:([0-9]+).*#\1#' <<<"$1"; }
LLM_PORT="$(url_port "$(env_get LOCAL_LLM_BASE_URL)")"
SEARXNG_PORT="$(url_port "$(env_get SEARXNG_URL)")"
INSTALL_LAUNCH_AGENTS="${INSTALL_LAUNCH_AGENTS:-1}"

# Install config/launchd/<label>.plist as a user LaunchAgent (filling __REPO__/__HOME__) and
# (re)load it. bootout first so a changed plist actually replaces the loaded one. With a second
# argument "next-login", only write the plist (loading it would run it now).
install_agent() {
  local label="$1" dest="$HOME/Library/LaunchAgents/$1.plist"
  mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs/local-agent-mcp"
  sed -e "s#__REPO__#${REPO_ROOT}#g" -e "s#__HOME__#${HOME}#g" \
    "$REPO_ROOT/config/launchd/$label.plist" > "$dest"
  plutil -lint -s "$dest" || die "bad plist: $dest"
  [[ "${2:-}" == next-login ]] && return 0
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$dest" || warn "launchctl bootstrap $label failed"
}
