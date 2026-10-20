# Mac Studio local-agent setup. Run phases in order; each script is idempotent.
# `make poc` runs the same phases with the 24 GB profile (smaller models, no login agents),
# for trying the stack on a smaller Mac first. Skips `prep` (sudo, wired limit, pmset).
.PHONY: all prep models search opencode claude verify test poc
all: prep models search opencode claude verify
prep:     ; ./scripts/00-macos-prep.sh
models:   ; ./scripts/10-models.sh
search:   ; ./scripts/20-searxng.sh
opencode: ; ./scripts/30-opencode.sh
claude:   ; ./scripts/40-claude-code.sh
verify:   ; ./scripts/50-verify.sh
test:     ; uv run --script tests/test_offline.py
poc:
	ENV_TEMPLATE=config/local-agent.mini-24gb.env.example INSTALL_LAUNCH_AGENTS=$${INSTALL_LAUNCH_AGENTS:-0} \
	  $(MAKE) models search opencode claude verify
