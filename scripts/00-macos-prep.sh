#!/usr/bin/env bash
# Phase 1: base OS prep - CLT, Homebrew, core tools, GPU wired-memory limit, power settings.
source "$(dirname "$0")/lib.sh"
need_macos

log "Xcode Command Line Tools"
xcode-select -p >/dev/null 2>&1 || { xcode-select --install; die "Re-run after the CLT installer finishes."; }

log "Homebrew"
if ! have brew; then
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi
brew_shellenv
grep -q 'brew shellenv' "$HOME/.zprofile" 2>/dev/null || echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> "$HOME/.zprofile"

log "Core packages"
brew install git jq uv node podman ripgrep fd htop
brew install --cask lm-studio || true

# Leave ~16 GB for macOS; the rest may be wired to the GPU for model weights + KV cache.
# Below 64 GB, total-16 GB would be *lower* than macOS's default (~2/3 of RAM), so keep 8 GB.
TOTAL_MB=$(( $(sysctl -n hw.memsize) / 1024 / 1024 ))
RESERVE_MB=$(( TOTAL_MB >= 65536 ? 16384 : 8192 ))
WIRED_MB=${WIRED_MB:-$(( TOTAL_MB - RESERVE_MB ))}
log "GPU wired limit -> ${WIRED_MB} MB (of ${TOTAL_MB} MB). Needs sudo."
sudo sysctl iogpu.wired_limit_mb="$WIRED_MB"
PL=/Library/LaunchDaemons/com.local-agent.gpu-wired-limit.plist
sed "s/__WIRED_MB__/${WIRED_MB}/" "$REPO_ROOT/config/launchd/com.local-agent.gpu-wired-limit.plist" | sudo tee "$PL" >/dev/null
sudo chown root:wheel "$PL"; sudo chmod 644 "$PL"
sudo launchctl bootstrap system "$PL" 2>/dev/null || true

log "Power: never sleep on AC (server role), wake on network"
sudo pmset -c sleep 0 disksleep 0 womp 1 autorestart 1

log "Done. Open LM Studio once (GUI) to finish first-run, then run 10-models.sh"
