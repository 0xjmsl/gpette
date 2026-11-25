# Changelog

Newest first. gpette has no version numbers yet: entries are dated, and `main` is what runs.

## 2026-11-23

> **Experimental**, and off until a profile asks for it.

### New

- **`<a2ui>` for any catalog.** `a2ui_catalog = "<path>"` on a profile makes it write for that catalog document instead of A2UI's basic catalog, and `a2ui_draws = "<path>"` names a JSON file with the components and functions the app draws when that is not all of them. The lesson and the check follow both; each pair has its own lesson file. README: Another catalog.
- **Interfaces the app draws: `<a2ui>`.** A profile with `a2ui = true` can answer with buttons, a form or a list, written as an [A2UI](https://a2ui.org) block and drawn by the person's app. The A2UI agent SDK (`pip install a2ui-agent-sdk`, optional) writes the lesson the model reads, turns the block into A2UI messages and checks them; a block that fails goes back to the model with the reason. A button press comes back as a message that starts `[A2UI action]`. Needs an xmtp_daemon that knows the `a2ui` content type. README: Interfaces the app draws. Tests: `tests/test_a2ui_block.py`.

## 2026-11-20

> **Experimental.** Everything in this entry is new and has run for hours, not weeks. It is in use on two real setups and covered by the tests named below, but expect the details to move. If you only want a bot that answers, none of it turns on by itself: every feature here is off until your config asks for it. Read "Changes to check in your config" before you update.

### New

- **Writing first: `[SEND_XMTP_MESSAGE]`.** A profile with `send = true` can write to another inbox or to a registered group in the middle of its turn and get the result back. A budget per turn (`[send] per_item`, `max_per_turn`). Claude Code profiles only. README § Writing first.
- **Public profiles.** A bot with tools can have a second profile that holds no power and answers the people it writes to, one fenced folder per person (`per_inbox = true`, `public_profile`, `knowledge`). The first message to an inbox registers that person, so their answer needs no password. README § The people a bot writes to.
- **Asking the main: `[ADMIN_TURN]`.** A public profile can wake the profile named by `admin_profile`, in its DM with the inbox named by `admin_inbox` (opened through the daemon if needed; nothing looked up from history). Nothing the person wrote is passed: the main reads the notes in that person's folder, and a note that copies the person's words is refused. Daily caps. README § Asking the main.
- **`nothing_matched`.** Per inbox (or in `[daemon]`): which profile answers a sender no profile lists, or none. `nothing_matched_groups` extends it to groups.
- **Every profile is told its blocks.** gpette adds a short section to each run's instructions that describes `[NO_REPLY]`, and the send and admin-turn blocks when the profile has them. README § What a profile is told.
- **A timer fire joins the stack.** A person's messages, `[ADMIN_TURN]` wake-ups and a timer fire waiting for the same chat are now one run. README § Stacking.

### Fixed

- **A resumed Claude Code session never received changed instructions.** The CLI keeps the system prompt a session started with and ignores `--append-system-prompt` on `--resume`. gpette now hands a resumed session its instructions inside the next message whenever they differ from the ones it was last given, once per change.
- **`[NO_REPLY]` always sends nothing.** It used to be sent as plain text when a person's message in a one-to-one chat started the turn. Now it is caught in every turn, whatever started it.
- **Naming a block inside a sentence no longer triggers it.** `[SEND_XMTP_MESSAGE]` counts only alone on its own line.

### Changes to check in your config

- **A profile with `admin_profile` now also needs `admin_inbox`** (one of the main's members): gpette refuses to start without it. Before this, the main's chat was guessed from the newest DM a member wrote in, which failed when no member had written yet.
- **`admin = true` changed meaning.** It now means only "other profiles may wake this one with `[ADMIN_TURN]`". The `/menu` and full error text have their own key, `menu = true`. A profile that has `admin = true` and no `menu` key keeps its menu and gets a warning at start; write `menu = true` or `menu = false` to settle it.
- **A group is matched by its own id, never by who writes in it.** A group that is in no profile's `members` no longer falls back to the sender's profile. List the group id in the profile that should answer it.
- **A profile bound to an inbox with no `members`** answers everyone who writes a DM to that inbox. That is how a public profile takes strangers; make sure you do not have such a profile by accident.

### Known limits

- One run at a time for every chat and every inbox: a long timer job delays everyone else's answer.
- Instructions handed to a resumed session inside a message can be summarized away when a very long session is compacted. Keep what matters in the profile's `CLAUDE.md`, or use `sessions = "fresh"`.
- The send and admin-turn blocks need Claude Code. On other engines they are cut out of the answer.
