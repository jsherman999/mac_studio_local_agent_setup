<!-- BEGIN local-agent delegation policy (managed by mac_studio_local_agent_setup) -->
## Local model delegation (Mac Studio)

You have `mcp__local-agent__*` tools backed by local LLMs on this machine. They cost nothing per
token but are slower and less capable than you. Delegate when the work is **large input -> small
output** or **mechanical**, keep judgment for yourself.

**Delegate (default to local):**
- Reading more than ~15 files or more than ~200 KB of text to answer a question -> `local_summarize_files`
- Logs, CSV/Parquet/JSON data, inventories, audit output -> `local_analyze_data` / `local_summarize_files`
- Web questions needing more than 2 pages read -> `local_research`; a single long page -> `local_web_fetch` with `question`
- Boilerplate, test scaffolding, docstrings, simple scripts -> `local_codegen`
- Self-contained multi-file mechanical changes with a test command -> `local_agent_task` (worktree), then review the diff
- Classification, extraction, reformatting of text you already have that is long -> `local_ask`

**Keep for yourself:**
- Architecture, security-sensitive code (auth, sudo, PAM, SSH, secrets), subtle bugs, final review
- Anything where one wrong detail is costly and hard to verify
- Small reads: if you need under ~5 files, just Read them

**Verification rules:**
- Treat local output as a draft from a junior colleague. Spot-check cited `[file:Lx-y]` ranges
  with Read(offset, limit) before acting on them.
- Review every `local_agent_task` diff before merging; run the tests yourself.
- If a local tool errors, call `local_status` once, report the problem, and continue without it.

At the end of long sessions, `local_usage_report` shows how much work was offloaded.
<!-- END local-agent delegation policy -->
