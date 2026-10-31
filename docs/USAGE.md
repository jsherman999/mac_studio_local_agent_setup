# Using the Mac Studio local-agent stack

What runs on the Studio after `make all`, and four ways to use the local models: through
Claude Code, from the command line, with OpenCode alone, and through the raw API.

## What is running

**At boot (LaunchDaemon, root)**

- `com.local-agent.gpu-wired-limit` sets `iogpu.wired_limit_mb` to RAM minus 16 GB (114688 on a
  128 GB Studio), so the GPU can hold both models.
- Remote Login (Apple's launchd-managed `sshd`) listens on port 22, if you turned it on in
  System Settings > General > Sharing.

**After auto-login (LaunchAgents, your user)**

Each agent runs a start script, retries until its service is up, and exits 0. `launchctl print`
then shows the agent as "not running, last exit code = 0", which is expected; the service it
started keeps running.

| Agent | Brings up | Listens on |
|---|---|---|
| `com.local-agent.lmstudio-server` | LM Studio API server, with **gpt-oss-120b** (63 GB, MXFP4 GGUF) and **qwen3-coder-30b** (17 GB, 4-bit MLX) pinned at 64k context | `127.0.0.1:1234`, localhost only |
| `com.local-agent.searxng` | Podman VM and the SearXNG container | `127.0.0.1:8888`, localhost only |
| `com.local-agent.lock-at-login` | Locks the session 15 s after auto-login | — |

With both models pinned, about half of RAM is still free. Logs are in
`~/Library/Logs/local-agent-mcp/`.

**On demand**

- The **`local-agent` MCP server** (`mcp_server/local_agent_mcp.py`). Claude Code, or
  `tools/local-tool`, starts it over stdio with `uv`, and it exits with the session. It reads
  `~/.config/local-agent/local-agent.env`.
- **OpenCode** runs only when `local_agent_task` calls it or when you run it yourself.

**Checking and restarting**

```bash
~/.lmstudio/bin/lms ps                                   # loaded models, context, TTL
curl -s localhost:1234/v1/models | jq -r '.data[].id'    # model ids the API serves
podman ps                                                # searxng container
launchctl print gui/$(id -u)/com.local-agent.searxng | grep -E 'state|last exit'
launchctl kickstart -k gui/$(id -u)/com.local-agent.lmstudio-server   # restart a service
make verify                                              # check every layer
```

## The tools

| Tool | Parameters (`[optional]`) | Use it for |
|---|---|---|
| `local_status` | | Health check of models, search, OpenCode, and config |
| `local_ask` | `prompt, [system], [model_role], [model], [max_tokens], [temperature]` | One-shot prompts where the input fits in the prompt |
| `local_summarize_files` | `paths, question, [exclude], [model_role], [max_answer_tokens]` | Questions over many files, directories, or globs; cites `[file:Lx-y]` |
| `local_analyze_data` | `path, [question], [model_role]` | Profiling CSV/TSV/JSON/JSONL/Parquet files, plus optional Q&A |
| `local_web_search` | `query, [max_results], [categories], [time_range]` | SearXNG results (title, URL, snippet) |
| `local_web_fetch` | `url, [question], [max_chars]` | Extracting a page's text, or answering a question about it |
| `local_research` | `question, [queries], [max_sources], [time_range]` | Search, fetch N pages, answer with `[n]` sources |
| `local_codegen` | `spec, [context_paths], [language], [max_tokens]` | Drafting code from a spec plus context files (writes nothing) |
| `local_agent_task` | `task, repo_path, [use_worktree], [timeout_s], [model]` | Running OpenCode in a new git worktree and branch |
| `local_usage_report` | `[days]` | Tokens processed locally, grouped by tool |

- `model_role` is `general` (gpt-oss-120b, the default), `coder`, or `fast` (both qwen3-coder).
- `time_range` is `day`, `week`, `month`, or `year`.
- File tools only read under `LOCAL_AGENT_ALLOWED_ROOTS` (here `~/src:~/data:~/Documents:~/ccode`).

## 1. With Claude Code (the main way)

Run `claude` as usual, logged in with your subscription. `/mcp` should list `local-agent` as
connected with 10 tools. Three things steer Opus toward the tools:

- The **tool descriptions**, which are always in context.
- The **delegation policy** in `~/.claude/CLAUDE.md`. It says what to delegate and requires
  spot-checking citations and reviewing diffs.
- The **`local-delegate` subagent**, which runs an entire investigation locally and returns
  only a short brief.

A hook also blocks whole-file `Read` calls over 256 KB and points Opus to the local tools.

Opus delegates on its own, and you can also ask for it:

```text
Use local_summarize_files on ~/src/myapp/**/*.py: where is auth handled? Verify the top 3 citations.
Profile ~/data/auth_logs/2026-10.parquet and tell me which hosts had failed-sudo spikes.
local_research: current state of FIDO2 for SSH on RHEL 9, last 6 months.
Have the local agent add pytest coverage for utils/parsing.py in ~/src/myapp, then review its diff.
Use the local-delegate subagent to survey ~/ccode/bigproject and give me a one-page brief.
```

Run `local_usage_report` at the end of a session to see how much work stayed local.

## 2. From the command line

### a) `tools/local-tool`: call any tool directly, no Claude involved

`tools/local-tool` is a small MCP client. It starts the server the same way Claude Code does,
so it uses the same config and the same usage ledger. Arguments are `key=value`. Values that
parse as JSON (lists, numbers, `true`/`false`) are passed as JSON, and everything else is passed
as a string. To pass the whole argument object at once, use `--json '{...}'`. If the first word
is not a tool name (`local_*`) or `list`, all the words are sent as the prompt to `local_ask`.

```bash
# in ~/.zshrc. noglob keeps zsh from treating ? and * in a free-text prompt as file patterns
alias lt="noglob $HOME/src/mac_studio_local_agent_setup/tools/local-tool"

lt list                                   # every tool and its parameters
lt local_status
```

**Ask a model**

```bash
lt what date is it?                       # free text = local_ask prompt="what date is it?"
lt explain launchd KeepAlive vs RunAtLoad in 3 bullets
lt local_ask prompt="Explain launchd KeepAlive vs RunAtLoad in 3 bullets"
lt local_ask prompt="Write a regex for ISO-8601 dates" model_role=coder max_tokens=400
lt local_ask system="You are terse." prompt="$(cat notes.txt) -- summarize in 5 bullets"
```

**Read many files**

```bash
lt local_summarize_files paths='["~/src/myapp/**/*.py"]' \
   question="Where are database connections opened, and are they always closed?"

lt local_summarize_files paths='["~/src/myapp"]' exclude='["*/tests/*","*.min.js"]' \
   question="List every HTTP endpoint with its handler"

lt local_summarize_files paths='["/var/log/system.log"]' model_role=fast \
   question="Which processes crashed today and how often?"
```

**Data files**

```bash
lt local_analyze_data path=~/data/sales.csv
lt local_analyze_data path=~/data/auth.parquet question="Which hosts have the most failed logins?"
```

**Web**

```bash
lt local_web_search query="mlx-lm server continuous batching" max_results=5
lt local_web_search query="macOS 27 release notes" categories=news time_range=week
lt local_web_fetch url=https://lmstudio.ai/docs/cli question="How do I set a model's TTL?"
lt local_research question="What changed in OpenCode 1.19?" max_sources=4 time_range=month
lt local_research question="Compare SearXNG and Whoogle for self-hosting" \
   queries='["searxng vs whoogle","self-hosted metasearch 2026"]'
```

**Code**

```bash
lt local_codegen spec="A bash function that retries a command N times with backoff" language=bash
lt local_codegen spec="Add a --dry-run flag to this script" \
   context_paths='["~/src/myapp/deploy.py"]' language=python

lt local_agent_task repo_path=~/src/myapp timeout_s=600 \
   task="Add type hints to utils/parsing.py and run: python -m pytest -q"
# -> prints exit, worktree path, branch local-agent/<id>, git status, diff stat. Then:
git -C ~/src/myapp/.local-agent-worktrees/<id> diff     # review
git -C ~/src/myapp merge local-agent/<id>               # keep it, or:
git -C ~/src/myapp worktree remove --force .local-agent-worktrees/<id> && git -C ~/src/myapp branch -D local-agent/<id>
```

**Usage**

```bash
lt local_usage_report days=1
lt local_usage_report --json '{"days": 30}'    # --json right after the tool name replaces key=value
```

Output is plain text, so it pipes:

```bash
lt local_summarize_files paths='["~/src/myapp"]' question="TODOs and FIXMEs, grouped by file" > todo.md
```

### b) Headless Claude Code (`claude -p`): Opus decides, local models do the work

Opus runs the tools and writes the answer. You grant only the tools it may use:

```bash
claude -p "Use local_summarize_files on ~/src/myapp/**/*.py and tell me where auth is handled. \
Spot-check two citations with Read." \
  --allowedTools "mcp__local-agent__local_summarize_files" "Read"

claude -p "local_research: best practices for launchd agents that start VMs. 5 bullets with sources." \
  --allowedTools "mcp__local-agent__local_research"

# Every local tool:
claude -p "..." --allowedTools "mcp__local-agent__*"
```

This route is useful in cron jobs and scripts, where you want Opus to judge and verify the
local output. Use `tools/local-tool` when you just want the local result.

### c) Long-running jobs: `tools/long-job`

Give it a goal and it works until the goal is met. Each run is a fresh headless Claude session,
and the job's state lives in files, so context never bloats and the job survives crashes,
reboots, and usage limits.

```bash
tmux new -s job   # so it keeps running after you disconnect (detach: Ctrl-b d; reattach: tmux attach -t job)
NOTIFY_EMAIL=you@example.com tools/long-job fda-recalls "Collect all FDA medical-device recalls
from 2025-2026, classify by failure type and manufacturer, and find trends. Done = REPORT.md
with tables and sources."

tools/long-job status                 # all jobs: running/stopped/done, plan progress, runs
tools/long-job status fda-recalls     # goal, plan checklist, recent progress
tools/long-job fda-recalls            # resume a stopped job (picks up from PLAN.md)
```

Each job has a directory, `~/data/jobs/<name>/` (set `JOBS_DIR` to change it):

| File | Written by | Purpose |
|---|---|---|
| `GOAL.md` | you (first run) | The goal and what "done" means. You can edit it mid-job to steer. |
| `PLAN.md` | Opus | Checklist of `- [ ]` items, checked off as they are done |
| `PROGRESS.md` | Opus | Dated log: what was done, what failed, what's next |
| `raw/`, `notes/` | Opus and scripts | Downloaded data; local-model summaries and analyses |
| `REPORT.md`, `DONE` | Opus | The final answer; `DONE` ends the loop |
| `runs.log` | `long-job` | Output of each run |

Within each run, Opus does the next unchecked plan items. It writes small curl or Python
scripts for bulk downloads, delegates reading and analysis to the `local_*` tools, spot-checks
claims the report relies on, and updates the plan and log before it stops. Settings, all
optional:

| Variable | Default | Meaning |
|---|---|---|
| `MAX_RUNS` | 40 | Runs before stopping without `DONE` |
| `MAX_TURNS` | 60 | Turns per run |
| `RETRY_SLEEP` | 1200 | Seconds to wait after a failed run (for example, a usage limit) |
| `MAX_FAILS` | 5 | Consecutive failed runs before giving up |
| `NOTIFY_EMAIL` | (none) | Email `REPORT.md` here through the Gmail connector when done |

Notes:
- **Permissions:** the allowlist permits file edits, the local tools, `curl`, `python3`, `uv`,
  `jq`, `ls`, `wc`, `head`, and `mkdir`. There is no `rm`, `sudo`, or `git`. Edit `ALLOW` in the
  script if a job needs more.
- **Search rate limits:** SearXNG's upstream engines rate-limit heavy use. Goals that point at
  APIs or bulk downloads do better than goals that need hundreds of searches.
- **Usage:** jobs still use your Claude subscription. The local tools are what keep Opus's share
  small, and failed runs (such as hitting the usage limit) wait and retry.
- **Surviving reboots:** to restart a job after a reboot, start `long-job <name>` from a
  one-shot LaunchAgent.

## 3. With OpenCode alone

OpenCode is configured in `~/.config/opencode/opencode.json`. It uses the LM Studio provider,
defaults to `lmstudio/qwen/qwen3-coder-30b`, and never sees Anthropic credentials.

```bash
cd ~/src/some-repo
opencode                                     # TUI; /models switches model
opencode run "explain the top-level modules"     # one-shot, then exits
opencode run -m lmstudio/openai/gpt-oss-120b "review the error handling in server.py"
```

- **Which model:** **qwen3-coder** for editing and agent work (fast, reliable with tools);
  **gpt-oss-120b** for reasoning and review. In the POC, the smaller gpt-oss-20b stopped early
  when used as an agent. The 120B has not been tested as an agent.
- **Permissions** come from the `permission` block. Edits and most shell commands are allowed.
  `git push`, `git remote`, `git reset --hard`, `sudo`, `rm -rf /`, and `curl | sh` are denied.
- **Commit first.** OpenCode edits the working copy directly. Commit before you start, or work
  in a throwaway worktree (`git worktree add ../try -b try`). `local_agent_task` does this for
  you.
- It works over SSH from a phone or laptop, either with `opencode run` or the TUI.

## 4. The raw OpenAI-compatible API

```bash
curl -s localhost:1234/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "openai/gpt-oss-120b",
  "messages": [{"role": "user", "content": "hi"}]
}' | jq -r '.choices[0].message.content'
```

Any OpenAI-compatible client works with base URL `http://127.0.0.1:1234/v1` and any API key.
The server is bound to localhost on purpose. To use it from another machine, tunnel over SSH
instead of turning on LAN serving in LM Studio:

```bash
ssh -N -L 1234:127.0.0.1:1234 jay@<studio-ip>      # then use http://127.0.0.1:1234/v1 locally
```
