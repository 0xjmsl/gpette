# gpette

gpette is a chat bot server for XMTP. People message the bot from any XMTP app. gpette passes each message to a language model and sends the answer back.

It runs next to [xmtp_daemon](https://github.com/0xjmsl/xmtp_daemon). The daemon holds the bot's XMTP identity and talks to the network. gpette holds the conversations, the rules and the model calls. They talk over plain HTTP on the same machine.

```
XMTP network <-> xmtp_daemon --POST /api/xmtp/incoming--> gpette --> LLM
                             <--------POST /send---------
```

One bot, an owner/public split of one identity, several bots on one machine, several inboxes behind one gpette, mixed models, a team of bots that takes turns: see [SETUPS.md](SETUPS.md).

> **What this is.** This is the small server we run our own XMTP bots on, shared as it is. It does what we need and stays simple: one process, SQLite, a config file. There are far more capable agent frameworks and bot tools out there; if you need scale, orchestration or a polished product, start with those. If you want something you can read in an afternoon and bend to your own setup, this may fit.

## Quick start

You need Python 3.11 or newer and a running xmtp_daemon.

```
pip install -r requirements.txt
cp gpette.example.toml gpette.toml      # then edit it (Windows: copy gpette.example.toml gpette.toml)
python main.py
```

Start the daemon with its webhook pointing at gpette:

```
XMTP_WEBHOOK_URL=http://127.0.0.1:4849 ./xmtp_daemon
```

Use the port from `[server] port`. Set `[daemon] url` to the daemon's own address (default `http://127.0.0.1:3100`). Then send the bot's address a DM.

All settings live in `gpette.toml`. `GPETTE_CONFIG=/path/to/file.toml` points at another file. Relative paths in it are relative to the file. API keys never go in it: they come from environment variables.

## Providers

Pick one with `[llm] provider`. A profile can pick another.

| Provider | What it needs |
|---|---|
| `claude-code` | The Claude Code CLI (`claude`) on PATH, signed in. |
| `claude` | `ANTHROPIC_API_KEY`, `pip install anthropic` |
| `openai` | `OPENAI_API_KEY`, `pip install openai` |
| `gemini` | `GEMINI_API_KEY`, `pip install google-genai` |
| `lmstudio` | LM Studio serving at `[llm] lmstudio_host`. It must be up when gpette starts. |
| `ollama` | Ollama at `[llm] ollama_host`, `pip install ollama` |

Models are set in `[llm.models]`.

API providers get a system prompt. The default is a short built-in text (`modules/prompts.py`). Set `[llm] system_prompt_file`, or `system_prompt` in a profile, to use your own.

## Claude Code and sessions

With `claude-code`, gpette runs the `claude` CLI in a working folder (`[claude_code] workdir`; on Windows write paths in `gpette.toml` with forward slashes, `C:/Users/me/bot`: a backslash is a TOML escape and stops gpette at start). The `CLAUDE.md` in that folder is the bot's personality and rules. `allowed_tools` decides what it may do there (default: read only). ⚠ Scope every tool to the folder: `Read(./**)`, not `Read`. A bare `Read`, `Glob` or `Grep` is allowed on **any path** the user running gpette can read (checked with `--permission-mode dontAsk`, Claude Code 2.1, 2026-11-16), so a bot anyone can talk to could be asked to read files outside its folder.

Each conversation gets its own Claude Code session. gpette saves the session id and passes `--resume` on the next message, so the bot remembers the chat. A new session starts with the summary of the earlier conversation, if there is one, and the latest 10 messages.

A session grows with every message, and a big session makes every turn slower and dearer. So gpette cuts it (`[compaction.claude_code]`): after each run it reads how many tokens the session holds. From 300,000 on, one short call checks each new message, and a new subject cuts. From 400,000 on, the next message cuts. A cut folds gpette's own log of the chat into the running summary (up to 1,000 words) and drops the session. Both calls are one-shot `claude` runs on Sonnet with no session and no tools, on the same login as the bot. If the summary call fails nothing is cut, and the next message tries again. `/compact` cuts at any size and answers with what it did. Timer fires follow the same rules.

`[sessions] routing = "shared"` makes every DM resume one single session instead. A profile can choose this too.

`routing = "per_account"` keeps one session per person instead of per chat: the same person writing from two inboxes (or two chats) continues one conversation. Who a person is comes from `[sessions] owner_resolver`, a module with `owner_for(inbox_id) -> str | None` (for example the account an inbox belongs to). Without one, each inbox is its own person. Groups always keep one session per group.

`sessions = "fresh"` (on a profile, or as `routing`) resumes nothing: every turn is a new session that starts the way any new session does, with the summary and the latest chat messages. Use it for bots whose instructions (the workdir's `CLAUDE.md`) change while they run: Claude Code reads `CLAUDE.md` only when a session starts, so a resumed session keeps following the old one. Memory then lives in the chat history and in whatever files the bot keeps.

A user can start over by sending `/reset`, `reset conversation`, `fresh start` or `new session`: the session and the summary are dropped, and nothing earlier is carried over.

## Profiles

A profile gives some people or groups their own environment: provider, model, folder, tools, prompt, session routing and gate. Declare it as `[profiles.<name>]` with a `members` list of inbox ids and group conversation ids.

A message is matched by its group id first, then by the sender's inbox id. Anything unmatched uses the top-level settings. Every key is optional. See the end of `gpette.example.toml` for the full list.

With [several inboxes](#several-inboxes), `inbox = "<name>"` binds a profile to one of them: it answers only what arrives at that inbox, and speaks through it.

## Several inboxes

One gpette can sit behind several XMTP inboxes, each with its own name and picture in people's chat lists: a team of bots, one brain. Run one xmtp_daemon per inbox (xmtp_daemon README § Several inboxes; each its own folder and port, all with the same `XMTP_WEBHOOK_URL`), then list them and bind a profile to each:

```toml
[inboxes.alice]
url = "http://127.0.0.1:3111"       # alice's daemon

[inboxes.bob]
url = "http://127.0.0.1:3112"

[profiles.alice]
inbox = "alice"                     # answers what arrives at alice, speaks as alice
workdir = "./profiles/alice"

[profiles.bob]
inbox = "bob"
members = ["<your inbox id>"]       # optional: bob answers only you
workdir = "./profiles/bob"
```

That is all. You never write an inbox id: gpette asks each daemon for its own (`/health`) and logs it at start (`inbox 'alice': http://127.0.0.1:3111 = 4f2a…`).

How a message finds its way:

- **It arrives.** The daemon says which inbox received it. Only the profiles bound to that inbox are tried, by group id and then by sender; a bound profile with no `members` answers everyone who writes to that inbox. Profiles with no `inbox` are tried after them, then the default. So you can write to alice and to bob from the same account and get two different bots, in two different chats.
- **It leaves.** A reply goes out through the inbox its chat belongs to. gpette learns that from the first message it gets in the chat, or from the timer that fires there, and keeps it in `<data_dir>/conversation_inboxes.json`. A chat it knows nothing about goes out through `[daemon] url`.
- **Several of your inboxes in one group** is fine: inside gpette each inbox has its own copy of the group, with the id `<group id>@<inbox name>` (its own history, session, profile and timers; list the plain group id in `members`), and each answer leaves through its own inbox. Pace them with `group_reply_secs` ([Group chats](#group-chats)).
- **A bot that reports to you on a timer:** a timer can only be set in a chat gpette has received a message in. Write one word to the bot first, find the chat with `timer.py conversations`, then set the timer there with the bound profile (`timer.py set … --conv <id> --profile alice --owner <your inbox id>`). Each fire goes out as alice. (The daemon's `POST /dm` opens a chat from the bot's side, but gpette does not know that chat until a message arrives in it.)

gpette refuses to start if a profile names an inbox that is not under `[inboxes]`, or an inbox has no `url`. A config with no `[inboxes]` works exactly as before. The admin panel lists each inbox and its id. `tests/test_inboxes.py` checks all of the above with fake daemons, no model and no XMTP.

## The menu and errors

A profile with `admin = true` gets `/menu`: a message of buttons (Status, Model, Session, Timers, Errors, Help). Apps that know the `coinbase.com/actions` content type show buttons; any other app shows a numbered list, and typing the number works the same. It runs before the model, so it answers while the model is over its limit. Session has "Compact now" and "New session", the same as typing `/compact` and `/reset`. Shortcuts: `/status`, `/model <name>` (`/model default` goes back to the config), `/compact`, `/reset`. A model picked here, and paused timers, are kept in `<data_dir>/menu_state.json` and survive a restart. On other profiles `/menu` is ordinary text. If the menu itself fails, that press is dropped and you get a message with the error; the conversation keeps working.

When a Claude Code run fails, the reason is sorted (limit, overloaded, auth, timeout, session, other) and answered per profile with `on_error`: `describe` sends the reason and the model (the default for admin profiles), `generic` sends `error_reply` ("I'm not available right now…" unless you set your own; `[claude_code] error_reply` sets it for every profile). A timer fire reports a failure once, stays quiet while it repeats, and says "back" when a run works again. Only a missing session is dropped and retried fresh; a limit or a login error keeps the session.

A message gpette crashes on is dropped, not retried: the webhook answers the daemon `422` and logs the traceback (`handler failed, DROPPED`), so one bad message never holds the ones behind it.

## Group chats

A group that is in no profile runs in its own empty folder under `[claude_code] group_workdir`, with no tools at all. Set `[claude_code] groups = "default"` to give every such group the top-level folder and tools instead (still one session per group). In groups, each message is prefixed with the sender's name so the model knows who spoke. List a group in a profile to give it more.

Sender names are looked up on [PublicOS](https://publicos.dev) by default: an XMTP inbox that belongs to a PublicOS account shows as that account's display name, read with two calls to the public RPC (no key, no account). Senders without one show as a short inbox id. Point `[senders] resolver` at your own module (`resolve(inbox_id, address=None)` returning a name or `None`) to use another directory, or set it to `""` to turn lookup off; `[senders] rpc` and `mr_anchor` change where the built-in lookup reads.

**A bot does not have to answer in a group.** Every group turn ends with a line telling the bot whether it may post now; if it answers exactly `[NO_REPLY]`, nothing is sent. **`group_reply_secs`** (`[server]`, or per profile) lets a bot post in a group at most once per that many seconds (0, the default, = no limit). A bot that may not post is still run when new messages arrive, so it can read and act with its tools; only its answer is held back, and the turn's line says until when. The time of each post is kept in `<data_dir>/group_posts.json`. A DM always gets its answer.

## Stacking: several messages, one turn

People often send many short messages instead of one long one. gpette answers them together: when a chat's turn comes, it waits until the chat has been quiet for **`stack_secs`** (`[server]`, default 5; a profile may set its own, 0 = no wait), each new message restarting the wait, and then hands every waiting message of that chat to the bot as one turn (in a group, each line starts with its sender's name). A message that arrives while the bot is running waits for the next turn. Files and timer fires are never stacked; a file keeps its place in the chat's order. `tests/test_stacking.py` checks it without a model.

## Gates

A gate decides who gets answered. Set `[gate] kind`:

- `open`: everyone is answered. This is the default.
- `password`: a stranger's first DM must be exactly `[gate] password`. The bot answers `welcome` and lets them in. At most `new_people_per_day` new people get in per day. Other messages from strangers are dropped. A group is answered when one of its members is already in.
- `none`: no one is answered. Useful on a profile whose people another gpette serves.
- a dotted module path: your own gate (see AGENTS.md).

`messages_per_person_per_day` caps every gate. 0 means no cap.

## Transcripts: memory a read-only bot can search

`transcripts = true` (in `[claude_code]` or on a profile) writes every user and bot message to `<workdir>/conversations/<person>/<YYYY-MM>.md`, where `<person>` is the session owner (with `per_account`, the person; else the chat). A bot that can only Read / Glob / Grep can then look up what someone said long ago, even after a session reset. Each run gets deny rules for every other person's folder, so one person's bot cannot read another's transcript (`tests/scenarios/transcripts.toml`: found in her own, not in his, a direct read of his file denied).

## Filter: who this gpette answers

`[filter]` runs before the gate: an ordered list of `answer` / `ignore` rules, first match wins, `default` when none matches. A rule lists `inboxes` and `accounts` (the sender of a DM; accounts need the `owner_resolver`) and `groups` (a group chat, by its id). An ignored message gets no reply and no read receipt.

Its main use: **one inbox, two machines.** XMTP lets one inbox run several installations, and each receives every message. Give each gpette rules for the people it serves (for example the owner's machine answers the owner's inboxes, the public one ignores them), so exactly one replies. `tests/scenarios/filter_rules.toml` and `split_*.toml` show it.

## Admin panel

`http://127.0.0.1:<port>/admin` shows where each conversation went: the filter rule, the profile, the gate's decision and the session it resumes, newest first, with a "who would handle this?" box that answers without sending anything, and the setup in use (never the password). It answers only requests from the same machine. The data is `<data_dir>/routes.jsonl`. JSON at `/api/admin/routes`, `/api/admin/setup`, `/api/admin/explain?sender=…&conversation=…&kind=dm|group`.

## Timers

The model can set reminders that fire later. When a timer fires, the model gets a message that starts with `Timer fired:` plus the instructions it wrote. If it answers exactly `[NO_REPLY]`, nothing is sent. This lets a timer do quiet work. (A group turn may answer `[NO_REPLY]` too; a DM never: a person who writes gets an answer.)

Timers are Python tools (`setTimer`, `listTimers`, `editTimer`, `cancelTimer`), so API providers can set them. (`scripts/timer.py` below needs gpette's packages: run it with the same Python, or virtual environment, that runs `main.py`.) Claude Code cannot call Python tools; a Claude Code profile that may run Bash uses the command line instead:

```
python scripts/timer.py set "Morning check" "Read the notes and tell me what is due today" --time "8 AM" --type recurring
python scripts/timer.py list
python scripts/timer.py cancel <id>
```

gpette sets `GPETTE_CONVERSATION_ID`, `GPETTE_PROFILE` and `GPETTE_TIMER_OWNER` for every Claude Code run, so inside a bot's run the timer lands in the chat being answered, bound to that turn's profile and to the person who wrote; from your own terminal pass `--conv`, `--profile` and, for a DM, `--owner` (`timer.py conversations` lists the ids; `timer.py list` needs `--all` or `--conv` there). `--skip-session` fires in a fresh session, without the chat's history. The store is read every minute, so no restart is needed.

**A timer holds what it runs as.** Three things are written when it is set and never worked out later: the conversation its reply goes to, the profile every fire runs in, and the inbox that set it. Editing a timer keeps all three. The profile has to be the one that chat resolves to. In a DM that is the profile the inbox is a member of, so a timer set from inside a chat only ever messages an inbox linked to its profile (from a terminal, `--owner` is checked against the profile's members but not against who is in the `--conv` chat: pick the chat whose last human sender is that inbox, or the reply goes to whoever is in the chat you named), and a person with two inboxes has one timer per chat, each reporting to its own. In a group it is the group's own profile if the group is listed, else the setter's; the reply goes to the group only. `set` refuses anything else, and every fire checks again: a timer whose profile is missing, gone from the config, or no longer the profile of that chat **does not fire** (the log says `timer_refused` and why). There is no fallback to the default profile. A timer made before this existed shows as `UNBOUND` in `timer.py list --all`; give it its profile with `timer.py bind TIMER_ID --profile NAME --owner INBOX`. `tests/test_timers.py` checks all of it without a model.

A bot that can run `timer.py` can set a timer in any conversation of this gpette, so give Bash only to profiles you trust with that.

## Tools

Every `.py` file at the top of `tools/` is a tool for API providers, unless its name starts with `sys_` or `_`. The file holds a function with the same name. Its docstring is the description the model sees. A `Parameters:` section in the docstring describes each argument. A parameter named `_conversation_id` is filled in by gpette and hidden from the model.

A profile's `tools` list limits which tools it may call.

## Web UI

`ui/` is a small test chat. Run `python ui/serve.py 8080` and open `http://localhost:8080`. Enter the server URL, a conversation id and a sender inbox id. The settings panel changes the running config. Replies to a made-up conversation id also go to the daemon, which logs a send error.

## Data folder

`[server] data_dir` (default `./data`) holds everything gpette writes:

- `gpette.db`: conversations, messages, timers, who is in, and the server log (SQLite)
- `logs/app.log`: a message is logged by its shape (conversation, sender, type, size) when it arrives; its text is logged only after the filter and the gate let it through
- `uploads/`: attachments
- `claude_sessions.json`: Claude Code session ids
- `config_overrides.json`: changes made from the web UI
- `memory.db`: semantic memory, when `[memory] enabled = true`

## Moving your history

`scripts/history.py` copies conversations, members, allowed people, messages (attachments included) and timers to one JSONL file, and back:

```
python scripts/history.py export history.jsonl
python scripts/history.py import history.jsonl --dry-run      # check the file, write nothing
python scripts/history.py import history.jsonl
```

It uses the store in `gpette.toml` (or `GPETTE_CONFIG`), so it also moves history between storage backends. Importing the same file twice adds nothing. Stop gpette before you import. The file format is in [docs/history_format.md](docs/history_format.md).

## Test your config

`tests/harness.py` runs a scenario end to end without XMTP. It writes the scenario's config and files to a temp folder, starts a fake xmtp_daemon and gpette on free local ports, sends each step as a daemon webhook message (text or image with caption), and checks the reply.

```
python tests/harness.py tests/scenarios/security.toml          # --keep keeps the temp folder
```

It prints one row per step: pass or fail, the reply, and the turns, cost and denied tool calls from gpette's run summary. It exits non-zero if any check fails. Checks: `contains`, `contains_any`, `not_contains`, `no_reply`, `denied_tool`, `no_denied`, `max_cost_usd`. The file format is documented at the top of `tests/harness.py`.

`tests/scenarios/security.toml` checks the privacy and tool fences with Claude Code (haiku, a few cents a run): a bot that may read only its images, a bot with no tools, a control profile with privacy off, and the password gate. Copy it to test your own profiles.

## Limitations

- The HTTP API and the web UI have no login. gpette listens on 127.0.0.1 by default. Keep it that way.
- The daemon must run on the same machine: the incoming routes only accept local callers.
- Claude Code profiles cannot use the Python tools. For timers they run `scripts/timer.py` (needs Bash).
- One process, one SQLite file. Other storage needs a plug-in.
- **One run at a time, for every chat and every inbox.** Messages wait in one queue; a message never interrupts a run (a chat's waiting messages are answered together, [Stacking](#stacking-several-messages-one-turn)). With [several inboxes](#several-inboxes) this means a long run of one bot delays every other bot's answers and timers.
- **Bots that talk to each other in a group** each get a turn for every new stack of messages. Use `group_reply_secs` and tell them to answer `[NO_REPLY]` when they have nothing to add, or the conversation never ends.
- XMTP network changes are the daemon's concern. Read its README.

## License

MIT. See LICENSE.
