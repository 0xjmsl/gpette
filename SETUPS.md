# Setups

Six shapes gpette is built and tested for. Each is one `gpette.toml` (or two) plus one
`xmtp_daemon` (or two). The words:

- **identity**: one XMTP inbox, made from one key. People message an identity.
- **installation**: one daemon running that key with its own database folder. One identity
  can have several (XMTP allows 10); each receives every message.
- **gpette**: the brain behind one installation. It decides who it answers (`[filter]`),
  who may talk (`[gate]`), and where each conversation goes (`[profiles]`, `[sessions]`).

## 1. One bot, one machine

One daemon, one gpette. Start from `gpette.example.toml`. Pick a gate: `open` for a public
bot, `password` for friends, or your own plug-in. This is the default and what most people
need.

## 2. One identity, two machines (owner and public)

You want people to message one bot, but your own chats to run somewhere with your files
and tools, and everyone else's somewhere locked down.

- Run the same key on both machines, each daemon with its **own** `XMTP_DB_DIR`. That makes
  two installations; both receive everything.
- Machine A (yours): `[filter] default = "ignore"` and one rule `action = "answer"` with your
  inbox ids (or your account, with an owner resolver). Full tools, your folder.
- Machine B (public): `[filter] default = "answer"` and one rule `action = "ignore"` with the
  same ids. Read-only tools, `privacy = true`, `transcripts = true`, `sessions.routing =
  "per_account"`, a gate.
- Groups: decide which side answers them. A group is matched by its id only (never by who
  is talking), so the same side always answers a given group. The simplest split: A ignores
  all groups by default, B answers them.

Tested: `tests/scenarios/split_owner_side.toml` and `split_public_side.toml` (offline), and
live on XMTP: each person was answered by exactly one machine. An existing chat picks up a
new installation on its next message.

## 3. Several identities on one machine

Different bots (a support bot, a test bot, a friend's bot), each its own key. Give each pair
its own ports and folders:

| bot | daemon `XMTP_HTTP_ADDR` | gpette `[server] port` | `data_dir` |
|---|---|---|---|
| support | 127.0.0.1:3100 | 4849 | ./bots/support/data |
| test | 127.0.0.1:3101 | 4850 | ./bots/test/data |

Each daemon's `XMTP_WEBHOOK_URL` points at its own gpette. Everything listens on localhost;
to look at an admin panel from elsewhere, put a reverse proxy with authentication in front
(one subdomain per bot works well) and keep the panel itself local.

## 4. Several inboxes, one gpette

A team of bots that share one brain: each has its own inbox (its own name and picture in your
chat list), and one gpette answers for all of them, each with its own profile, folder and
prompt. Use this instead of 3 when the bots belong together (a cast of characters, agents
that report to you one chat each); use 3 when they are separate products.

- One daemon per inbox, each its own folder and port, all with `XMTP_WEBHOOK_URL` at the one
  gpette (xmtp_daemon README § Several inboxes has a systemd template).
- In `gpette.toml`, one `[inboxes.<name>]` with the daemon's `url` per inbox, and
  `inbox = "<name>"` on each bot's profile (README § Several inboxes).
- The same person can talk to every bot; each conversation stays with the inbox it was
  started on, and the reply always leaves through that inbox.
- They can share a group: each has its own copy of it inside gpette and answers as itself.
  Give them `group_reply_secs` and tell them about `[NO_REPLY]` (README § Group chats), or
  bots answering bots never stop.

Tested: `tests/test_inboxes.py` (offline, three fake daemons). Live: ten agents on one gpette,
each reporting to its owner from its own inbox.

## 5. Mixed models

Profiles can use different providers on one bot: Claude Code for you, a local LM Studio model
for a group of friends, an API model for everyone else. Set `provider` (and `model`) per
profile; API providers get the profile's `system_prompt` and only the Python tools listed in
`tools`.

## 6. A team that takes turns

Setup 4 plus timers: several bots, each with its own inbox, folder and character, and each
woken on a schedule to do a piece of work and report to you. Nobody has to write to them.
This is how the ten agents mentioned above run.

**One folder per bot.** `workdir = "./agents/<name>"` on its profile (keep each daemon's
folder, with its `secrets/`, somewhere else: a bot can read everything in its workdir). In it:

- `CLAUDE.md`: the brief. Keep one shared part (the task, the rules, what a turn is, how a
  report looks) and one personal part (who this bot is), and build each bot's file from the
  two, so a change to the shared part reaches every bot.
- `notes.md`: the bot's memory, and the only file it may write. Each turn starts by reading
  it and ends by updating it.

**The tightest tools that do the job.** A bot that runs unattended should reach its own
folder and one command, nothing else:

```toml
[profiles.alice]
inbox = "alice"
members = ["<your inbox id>"]        # it reports to you and answers only you
gate = "open"
workdir = "./agents/alice"
allowed_tools = ["Read(./**)", "Glob(./**)", "Grep(./**)", "Edit(./notes.md)", "Write(./notes.md)", "Bash(mytool *)"]
```

A bare `Read` can read any file on the machine; `Read(./**)` keeps it in the folder.
`Bash(mytool *)` allows one command of yours and no other. If the bots hold keys, put a
small wrapper behind that command that loads the key of the bot whose turn it is
(`GPETTE_PROFILE` names it) and runs one of a fixed list of scripts, so no bot ever reads a
key file.

**One recurring timer per bot.** A timer can only be set in a chat that has had a message,
so write one word to each bot first (its address is in its daemon's `/health`). Then
`python scripts/timer.py conversations` lists the chats and their ids, and from your terminal:

```
python scripts/timer.py set "alice's turn" "Your turn. Read notes.md, see what changed since your last turn, do what moves you forward, update notes.md, and end with a short report." --time 200 --type recurring --skip-session --conv <conversation id> --profile alice --owner <your inbox id>
```

- `--time 200 --type recurring` fires every 200 minutes; the first fire comes one interval
  after you set it, so timers set together fire together. To spread the team, run each
  bot's `set` at the moment its slot should start (ten bots, one every 20 minutes, each
  every 200), or use clock times (`--time "08:00" --type recurring` repeats daily; one timer
  per turn of the day). `timer.py edit --time` on a recurring timer changes its interval,
  not only its next fire.
- Try one turn before the schedule: the same `set` with `--time 2` and no `--type`. The
  `run summary` line in `data/logs/app.log` shows the turn, its cost and anything denied.
- `--conv` decides where the report goes; `--owner` is checked against the profile's
  members, not against who is in that chat. Copy the id from `conversations`.
- From a terminal, `timer.py list` needs `--all` (or `--conv`).
- `--skip-session` starts every turn in a fresh session. The brief is read at the start of
  a session, so this is also what makes a changed `CLAUDE.md` take effect on the next turn.
  `notes.md` carries what must survive.
- The report is the bot's last message; it lands in your chat with that bot. A turn that
  answers exactly `[NO_REPLY]` sends nothing (README § Timers).
- gpette runs one turn at a time, for every bot. Keep the slot longer than a turn takes
  (`[claude_code] timeout_secs`, `max_turns`).

**Running it.**

- Cost: every fire is a model run. Slots times bots times days adds up; start slow.
- To change what they do, edit the shared brief and rebuild the `CLAUDE.md` files. To
  change one bot's schedule, `timer.py edit`. To stop one, `timer.py cancel`.
- Pausing without stopping anything: `/menu` from an `admin` profile, Timers, pause. Timers
  keep their schedule and do not fire until resumed.
- A stop switch: start the daemons and gpette from one small script (cron or a task
  scheduler calling it every few minutes is enough) and have it do nothing while a file
  named `PAUSED` exists. Then stopping the team is one `touch` and a kill per process. On
  Windows, a task launcher group you can stop does the same job.
- After gpette has been stopped, a recurring timer fires once per missed turn, a minute
  apart, when it comes back (each fire moves the timer on by one interval: a daily timer
  once per missed day). Before restarting after a long stop, look at `timer.py list --all`.
- `/menu` pause is for the whole gpette. A one-shot timer that comes due while paused is
  marked fired and skipped.
- Test before you hand them a new tool: run every command the way a bot would, the ones
  that must be refused included, and keep the expected refusals in a file to compare.
- Read the reports. The first days show what the brief did not say.

## Checking where messages go

Every setup has the admin panel at `http://127.0.0.1:<port>/admin` (local only): each
conversation's last route, and a "who would handle this?" box. Before going live, run the
harness with your own scenario (`tests/harness.py`, README § Test your config).
