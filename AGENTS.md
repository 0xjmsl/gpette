# AGENTS.md

Notes for AI agents that set up, run or extend gpette. Read README.md first for what it does.

## Rules

- Never print, log or echo an API key. Check that one is set with a yes/no test, not by showing it.
- Never put secrets in `gpette.toml`. Keys come from the environment: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`.
- Do not commit `gpette.toml`, `data/`, `workdir/`, `groups/` or `profiles/`. They hold the owner's settings and chats.
- Keep `[server] host = "127.0.0.1"`. The HTTP API and the web UI have no login, and `/api/config` can change what the bot may do.
- Do not edit the core to add a personal feature. Use the plug-in points below.

## Setup

1. Python 3.11 or newer. `pip install -r requirements.txt`. Provider SDKs you do not use can be skipped.
2. `cp gpette.example.toml gpette.toml` and edit it.
3. For `claude-code`: create the `workdir` folder and put a `CLAUDE.md` in it (the bot's personality). Check that `claude --version` works.
4. Run xmtp_daemon with `XMTP_WEBHOOK_URL=http://127.0.0.1:<server.port>`.
5. `python main.py`. It should log `Access gate: ...` and `Listening on http://127.0.0.1:<port>`.
6. Quick check: `curl http://127.0.0.1:<port>/api/` answers `gpette running`.

## Diagnose

| Symptom | Look at |
|---|---|
| No reply at all | Is the daemon's `XMTP_WEBHOOK_URL` the gpette port? Does `data/logs/app.log` show `incoming_message`? |
| Log says `refused non-local caller` | The daemon runs on another machine. Run both on one machine. |
| Message arrives, nothing sent back | The gate. With `password`, a stranger's DM is dropped silently. Check `messages_per_person_per_day`. |
| `Could not connect to LM Studio` at start | LM Studio is not serving at `[llm] lmstudio_host`. |
| `compaction.provider=... needs ..._API_KEY` at start | Set the key, or `[compaction] provider = "disabled"`. |
| Claude Code error, `claude` not found | The CLI is not on PATH for the user running gpette. |
| Reply goes nowhere, `/send` error in the log | `[daemon] url` is wrong, or the conversation id is unknown to the daemon. |
| A tool is missing | The file name and the function name differ, the name starts with `sys_` or `_`, or the profile's `tools` list leaves it out. |
| A profile is not used | Its `members` must hold the exact inbox id or group conversation id (case and `0x` do not matter). Group id wins over sender. |
| A group gets no tools | Expected: groups outside a profile run tool-less. Add the group id to a profile. |
| A timer fired but nothing was sent | The model answered `[NO_REPLY]`. That is by design. |
| Where did a message go? | `http://127.0.0.1:<port>/admin` (this machine only): each conversation's filter rule, profile, gate decision and session; the "who would handle this?" box predicts it without sending. Raw: `<data_dir>/routes.jsonl`. |
| A message is ignored with no gate line in the log | Look for `Filter: ignored … (<rule>)`: a `[filter]` rule (or `default = "ignore"`) dropped it before the gate. Expected when another installation of the same inbox serves that person. |
| Two machines on one inbox both answer, or neither | Their `[filter]` rules overlap or leave a gap. Each person must hit `answer` on exactly one side; groups are matched by group id only. SETUPS.md § 2. |
| The bot can't find an old chat after a reset | `transcripts = true` writes them to `<workdir>/conversations/<person>/`; the bot needs Read/Glob/Grep. Each run sees only its own person's folder. |

Logs: `data/logs/app.log`, and the `server_logs` table in `data/gpette.db`. Each Claude Code run logs one `run summary` line: profile, turns, cost and every tool call the permissions denied.

To check a config without XMTP, run a scenario: `python tests/harness.py tests/scenarios/security.toml --keep`. It starts a fake daemon and gpette in a temp folder, sends each step and checks the reply (see the top of `tests/harness.py` for the format). With `--keep`, the temp folder keeps `gpette.toml`, `data/logs/app.log` and `gpette_stdout.log` for a failed step. Write a scenario for a profile before you trust its fences.

## Configure

Everything is in `gpette.toml`. The example file documents each key. Runtime changes from the web UI land in `data/config_overrides.json` and win over the file. Delete that file to go back to the file's values.

## Where this repo ends

gpette is the engine. Build your own layer on top of it, in your own folder or package, and point the config at it. These are the plug-in points:

- **Gate.** `[gate] kind = "my_pkg.my_gate"`. The module defines `class Gate` with `check_message(message, kind=None) -> dict` returning `{'action': 'allow'}`, `{'action': 'reject'}`, or `{'action': 'auth_success' | 'auth_limit', 'response': <message>}`. Optional: `invalidate_conversation(conversation_id)`. The constructor may take the ConversationDB. See `modules/gates/__init__.py`.
- **Storage.** `[storage] backend = "my_pkg.my_db"`. The module defines `ConversationDB` with the same methods as `models/conversation_db.py`. To move history into or out of it, it also needs the `list_*` and `restore_*` methods that `scripts/history.py` uses; the list is at the end of [docs/history_format.md](docs/history_format.md). `python scripts/history.py export` / `import` copies a store to a JSONL file and back.
- **Sender names.** `[senders] resolver = "my_pkg.names"`. The module defines `resolve(inbox_id, address=None) -> str | None`. Results are cached for a day.
- **Tools.** Add a `.py` file to `tools/` (see README). For a private set, keep the files in your layer and copy or link them in.
- **Profiles.** `[profiles.<name>]` gives people or groups their own folder, `CLAUDE.md`, tools, provider, prompt, session routing and gate.
- **Prompts.** `[llm] system_prompt_file`, or `system_prompt` / `system_prompt_file` in a profile. With Claude Code, the folder's `CLAUDE.md` is the main prompt.

Your module must be importable: install it, or run gpette with it on `PYTHONPATH`.

## How it behaves today

- One session per conversation by default. `sessions.routing = "shared"` puts every DM into one session.
- Claude Code runs with `--permission-mode dontAsk` and only the tools in `allowed_tools`.
- Claude Code lets Read, Glob and Grep see the whole working folder without a rule. So when one of them is allowed only with a scope, like `Read(./images/**)`, gpette also denies it on every other top-level entry of the folder. Images a user sends are saved to `<workdir>/images`.
- Python tools work with API providers only, not with Claude Code.
- Images up to about 1 MB are passed to the model. Larger ones are compressed or replaced by a note.
