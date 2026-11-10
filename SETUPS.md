# Setups

Four shapes gpette is built and tested for. Each is one `gpette.toml` (or two) plus one
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

## 4. Mixed models

Profiles can use different providers on one bot: Claude Code for you, a local LM Studio model
for a group of friends, an API model for everyone else. Set `provider` (and `model`) per
profile; API providers get the profile's `system_prompt` and only the Python tools listed in
`tools`.

## Checking where messages go

Every setup has the admin panel at `http://127.0.0.1:<port>/admin` (local only): each
conversation's last route, and a "who would handle this?" box. Before going live, run the
harness with your own scenario (`tests/harness.py`, README § Test your config).
