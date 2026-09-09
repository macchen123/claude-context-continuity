<div align="center">

# Claude Context Continuity

<img src="docs/images/hero.svg" alt="Concept illustration of a native Claude Code context window rotating into a fresh context while the workspace remains connected" width="760" />

**Codex-style context continuity for native Claude Code.**

Keep long-running work moving with a context budget, a fresh start at the right moment, access to the original conversation, and short working notes.

[简体中文](README.zh-CN.md) · [Repository](https://github.com/macchen123/claude-context-continuity) · [License](LICENSE)

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![MIT License](https://img.shields.io/badge/License-MIT-2ea44f)](LICENSE)
[![Version 0.1.1](https://img.shields.io/badge/version-0.1.1-6f42c1)](https://github.com/macchen123/claude-context-continuity/releases/tag/v0.1.1)
[![Platform macOS and Linux](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-555555)](#platform)

<sub>Concept illustration — not a terminal screenshot.</sub>

</div>

[Why](#why) · [A better rhythm](#rhythm) · [Start in three steps](#quick-start) · [How it works](#how-it-works) · [Everyday use](#everyday-use) · [Privacy](#privacy) · [FAQ](#faq) · [Contributing](#contributing)

<a id="why"></a>
## Long tasks deserve more than repeated `/compact`

Claude Code provides `/compact` to make room as a conversation grows. It is useful, but repeatedly continuing from summaries can come with tradeoffs:

- **Details may get left out.** An exact constraint or the reason behind a decision may not survive in full.
- **Background may need explaining again.** Missing details can lead to corrections or repeated work.
- **The work has to pause.** Condensing the conversation takes time, and rebuilding the background can take more.

We built Claude Context Continuity to bring a **Codex-style alternative to native Claude Code: watch the budget, start a fresh context when it is time, and continue with working notes and access to the original history—not just another round of summaries.**

You keep the native Claude Code you already use, rather than move to a replacement coding assistant. The design is inspired by Codex's [new-context mechanism](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/core/src/tools/handlers/new_context_window_spec.rs) and [History/Notes tools](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/ext/history-notes/src/tools.rs). This is an independent community project, not an official product or a full Codex port.

<a id="rhythm"></a>
## A better rhythm for a long session

| With `/compact` | With context continuity |
| --- | --- |
| Make room by summarizing the active conversation. | Make room by starting with a fresh context. |
| Keep a shortened account in the active context. | Carry a short handoff and references to the original history. |
| An omitted detail may need to be explained again. | Give the new context a direct way to look up the earlier detail. |

<a id="quick-start"></a>
## Start in three steps

### 1. Prepare your environment and install

**What do you need first?**

| Prerequisite | How to prepare |
| --- | --- |
| Python 3.10+ and pip | Install Python or use an existing Python environment |
| Git | Needed for the Git URL below; not needed when installing the Release wheel |
| Native Claude Code | Install it separately, sign in or configure authentication, and confirm `claude` works |
| tmux | Install it through your system package manager; this package manages the session for you |
| Terminal and PATH | Use an interactive macOS/Linux terminal with `claude`, `tmux`, and your Python environment's command directory on PATH |

```sh
python3 -m pip install "git+https://github.com/macchen123/claude-context-continuity.git@v0.1.1"
```

**What does pip install?**

| Installed component | Purpose |
| --- | --- |
| `claude-context-continuity` Python package | Automatic context continuity, budget observation, History, and Notes |
| `apsw==3.53.4.0` | Supplies SQLite 3.53.4 and FTS5 without replacing system SQLite |
| `cclaude` command | Starts native Claude Code with context continuity |
| `claude-context` command | Searches History, manages Notes, and requests context switches |
| The `/renew` implementation | Ships with the package and loads in managed cclaude sessions; no separate skill installation |

Installation leaves Claude Code's global configuration unchanged; `/renew` and the related hooks load through a session-local plugin.

### 2. Set your context budget

```sh
# Example only: choose a positive estimate for your own native host.
export CLAUDE_CODE_MAX_CONTEXT_TOKENS=200000
```

`200000` is an example; choose a suitable estimate for your host's context capacity. A smaller detected native window takes precedence. Until a usable budget is available, the native session runs normally with automatic switching off.

### 3. Start as usual

```sh
cclaude
```

<details>
<summary>Optional: choose a no-compact invocation</summary>

If your native host supports the setting and you deliberately want that workflow for this invocation:

```sh
DISABLE_COMPACT=1 cclaude
```

This is your choice. The package reads no-compact configuration but never edits global settings or guarantees how another host will handle compaction. When available, it also considers `autoCompactEnabled: false`.

</details>

<a id="how-it-works"></a>
## How it works

<img src="docs/images/context-flow.svg" alt="Concept diagram: Session 1 leaves a short handoff, a fresh context continues in the same workspace, and the original conversation and Notes remain available on demand" width="100%" />

<p align="center"><em>Concept flow, not a terminal screenshot.</em></p>

1. **Choose room to work.** Set a context estimate before the session gets crowded.
2. **Finish, then switch.** The tool waits until current activity is finished and the prompt is empty before asking Claude Code for a fresh context. If it cannot tell, it leaves the session alone.
3. **Continue with the work intact.** The new context gets a short handoff; the same workspace, original conversation, and working Notes remain available.

<a id="everyday-use"></a>
## Everyday use

Use `cclaude` where you would normally start an interactive `claude` session. Keep working in the same project directory and use the same native tools and settings.

As the chosen budget gets close, the tool keeps room for a clean switch after current work is done. The next context receives a brief handoff rather than a full transcript, while the workspace stays where it is.

You do not need to handle session IDs for the ordinary path. If an exact earlier detail matters later, the original conversation is still available to read, and Notes can hold a few useful reminders.

### Switch early with `/renew`

In a managed `cclaude` session, type:

```text
/renew
```

Use this when a work stage has finished or you want a clean context before the budget threshold. The model prepares a concise handoff and calls the same `context-request` used by the existing controller. It waits for current work to settle and does not force `/clear`, kill tasks, or overwrite typed input. A request being accepted is not proof that the switch has already completed.

This one command is loaded only through the session-local `cclaude` plugin, with no global installation or extra setup. The `cclaude` plugin namespace is reserved for this package. `/renew` is the short alias supported by the verified native CLI; if another command already uses that name, use `/cclaude:renew` without overwriting the other command. Manual invocation keeps native tool permissions and can require a normal model turn; automatic budget observation does not depend on invoking this command.

### Durable scheduled tasks and opting out

Claude Code `2.1.263` and `2.1.266` reproduce a same-process issue: existing durable tasks continue after `/clear`, while a newly created durable task can remain on disk without firing. Version 0.1.1 enables a reversible binding workaround by default. Only after a successful native `CronCreate(durable=true)` in the managed process, it aligns the new task's scheduler-session binding with that process's startup session. The startup binding is retained through `resume` as well.

The native `.claude/scheduled_tasks.json` remains the only task store; the native scheduler still fires tasks and `CronList` / `CronDelete` work normally. The workaround does not change prompts, cron expressions, firing timestamps, or creator processes. It creates no temporary duplicates or extra timers and does not patch the official binary. Original session attribution is kept in a private undo receipt; other processes' tasks are left alone. Unsafe paths, concurrent changes, unsupported records, or native JSON larger than 4 MiB leave the task untouched and surface a compatibility diagnostic. Neither task creation nor a repaired binding proves an actual firing.

Start a new session with the workaround disabled:

```sh
CCLAUDE_DURABLE_CRON_COMPAT=off cclaude
```

Inspect its status in a managed session:

```sh
claude-context cron-compat status --context-id "$CLAUDE_CONTINUITY_ID"
```

To undo existing repairs, exit the corresponding native Claude process and other schedulers using that project directory first, then run from a normal terminal:

```sh
claude-context cron-compat restore --context-id "<context-id>"
```

Undo restores attribution only for tasks still matching their receipts. It never resurrects cancelled tasks, overwrites later edits, or changes native locks; a live owner process blocks restoration. Once upstream fixes the issue, disable the workaround and verify initial creation, an existing task across `/clear`, and a new task after `/clear` before retiring it. Do not infer a fix from a version number. Upgrading this package does not hot-update a running controller; start a new `cclaude` session.

<details>
<summary>Advanced commands: inspect a conversation, use Notes, or request a handoff</summary>

These commands are optional. `history` always requires both `--source` and `--session-id`, and the selected source filename must match that session ID.

```sh
# Read the human instructions from one selected conversation.
claude-context history \
  --source "<session-jsonl-path>" \
  --session-id "<session-id>" \
  --instructions

# Search that conversation, or list source conversations known to a context.
claude-context history \
  --source "<session-jsonl-path>" \
  --session-id "<session-id>" \
  --search "decision" \
  --page-size 5
claude-context history-windows --context-id "<context-id>"

# Read or create a short working Note.
claude-context notes list --context-id "<context-id>"
claude-context notes read handoff-1 --context-id "<context-id>"
printf '%s' 'Short working note.' | \
  claude-context notes write plan --context-id "<context-id>"

# Request a handoff for an already managed session after current work is done.
claude-context context-request \
  --context-id "$CLAUDE_CONTINUITY_ID" \
  --handoff "goal, completed work, remaining work, constraints, and important file locations"
```

**Cross-window History in v0.1.0:** `history-search` searches or browses the known windows of one continuity session using a local incremental FTS5 cache. It can be called in any later turn, not just during handoff. It does not scan every project or guarantee automatic recall of the right detail.

```sh
# Search all known windows; the context ID defaults to CLAUDE_CONTINUITY_ID.
claude-context history-search --query "decision" --role user --recent-first --page-size 5
# Browse one window's tool results, or continue a search with the same options.
claude-context history-search --context-id "<context-id>" --window 0 --tool Bash
claude-context history-search --query "decision" --role user --recent-first --page-size 5 --cursor "<next_cursor>"
# Verify the exact original record returned by search.
claude-context history --source "<source_path>" --session-id "<session_id>" \
  --message-id "<message_id>" --expected-sha256 "<sha256>"
```

Additional filters are `--session` and `--source-kind`; omit `--query` for bounded browsing. For an unmanaged session, use `history-search --source "<session-jsonl-path>" --session-id "<session-id>" --query "decision"`. Source changes invalidate pagination cursors explicitly. Updating an existing Note still requires its current `--expected-sha256` value.

Version `0.1.0` pins `apsw==3.53.4.0`, which supplies SQLite **3.53.4** and FTS5 without replacing system SQLite. Long literal queries use a trigram index; one- and two-character queries remain correct through a scan of selected cached text inside SQLite. No embedding service or retrieval-specific model is required. Unchanged sources reuse the cache; changed sources are parsed by the existing History reader and update only changed index records.

</details>

<a id="native-behavior"></a>
## Fits around native Claude Code

The normal path is simply `cclaude` in an interactive terminal. It adds context continuity around native Claude Code rather than replacing its prompts, models, tools, permissions, or settings.

<details>
<summary>Technical compatibility and direct-native behavior</summary>

`cclaude` checks the local `claude --help` shape before it decides to manage a session. This routing is based on observed Claude Code CLI `2.1.263`; it is not a promise that future native CLI versions expose the same flags or tools.

Only a normal interactive `cclaude` TTY is automated. Print modes, `--bg`/`--background`, `--cloud`, native subcommands, `--bare`, `--safe-mode`, non-TTY invocations, and unrecognized native arguments go straight to `claude` unchanged. Use `claude-context context-run` to start the same interactive route explicitly from a TTY.

</details>

<a id="privacy"></a>
## Local data and privacy

By default, continuity data lives separately from package source under:

```text
${CLAUDE_CONFIG_DIR:-$HOME/.claude}/session-continuity/
```

This local directory holds continuity data, working Notes, and the rebuildable History search cache. Original conversations remain where native Claude Code keeps them. The cache stores permitted, redacted searchable text and exact locators—not raw transcripts, hidden thinking, or tool-call inputs—and must still be protected as private conversation data.

Continuity files stay local, and this package does not run an extra upload service. But when you or native Claude Code retrieve a conversation or Note into a prompt, that text may be sent to the normal model provider used by the native session. Handle local conversation history with the same care as any other Claude Code transcript; this is not a promise that data never leaves your machine.

<details>
<summary>Choose the local data directory</summary>

Choose another local state directory before starting a command:

```sh
export CLAUDE_CONTEXT_CONTINUITY_DIR="<chosen-state-directory>"
cclaude
```

No additional skills or template-copying steps are required. A managed `cclaude` session loads `/renew` from its own local plugin. Plain `claude` sessions do not receive this command. For an unmanaged session, you can still ask the model for a concise handoff and use the same `claude-context` History/Notes commands.

</details>

<a id="platform"></a>
## Platform

| Environment | Status |
| --- | --- |
| Python | 3.10+ required; 3.12 tested |
| macOS and Linux | Supported POSIX baseline with native `claude` and `tmux` available |
| WSL | Not verified |
| Native Windows | Unsupported |

<a id="faq"></a>
## FAQ

### Can this replace a compaction-centered workflow?

Yes. You can use “leave room early → switch to a fresh context → continue from notes and original history” instead of repeatedly relying on `/compact`.

The tool does not patch or remove the native command, and it does not guarantee better results or perfect recall for every task. `/compact` summarizes the active context; it does not delete the original conversation records.

### What if the tool cannot tell that work is finished or cannot find a usable budget?

Only automatic switching pauses. Keep using the native Claude Code session normally; the tool does not force a clear, take over another terminal, or replay uncertain input.

### Can it search every previous context at once?

Yes. In `v0.1.0`, `history-search` searches the registered windows of one continuity session, with filtering, ordering, and pagination. It remains callable throughout the task. It does not search unrelated projects or ensure the model will always choose the right query. Single-source `history --search` and the window catalogue remain available.

### Does it replace Claude Code or fully reproduce Codex?

No. You still use native Claude Code. This tool adds context continuity rather than rebuilding the coding assistant or changing its models, tools, and permissions.

<a id="contributing"></a>
## Contributing and testing

Contributions are welcome through forks and pull requests. Only the repository owner can update or merge into `main`; contributors do not need write access to run tests locally. Release notes are maintained on [GitHub Releases](https://github.com/macchen123/claude-context-continuity/releases).

From your source checkout, install the package and its dependencies using your trusted package mirror, then run the isolated test suite:

```sh
python3 -m pip install -e .
CLAUDE_CONFIG_DIR="$PWD/.test-claude-config" \
PYTHONPATH=src \
python3 -m unittest discover -s tests -v
```

## License

Claude Context Continuity `0.1.1` is released under the [MIT License](LICENSE).
