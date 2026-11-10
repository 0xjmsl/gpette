# History file format

`scripts/history.py` moves gpette's history between stores. It writes and reads one file format, described here.

```
python scripts/history.py export history.jsonl
python scripts/history.py import history.jsonl --dry-run
python scripts/history.py import history.jsonl
```

Both commands work on the store that `gpette.toml` points at. `GPETTE_CONFIG=/path/to/file.toml` points them at another config, as it does for the server. Stop gpette before you import into its store.

## The file

The file is JSON Lines: UTF-8, one JSON object per line. Every object has a `type` field. Blank lines are ignored. The export writes the lines in this order: `meta`, `conversation`, `participant`, `allowed`, `message`, `timer`. The import does not depend on that order.

Times are integers. Conversation, participant, allowed and timer times are Unix seconds. A message `timestamp` and a conversation `reset_at` are Unix milliseconds, as gpette stores them.

A field marked optional may be left out or set to `null`.

### meta

The first line. It names the format, so a reader can refuse a file it does not know.

| Field | Type | Meaning |
|---|---|---|
| `format` | string | Always `gpette-history`. |
| `version` | integer | `1`. The import refuses a higher version. |

The import also accepts a file without a `meta` line.

### conversation

One per conversation.

| Field | Type | Meaning |
|---|---|---|
| `conversation_id` | string | The XMTP conversation id. |
| `is_group` | boolean | True for a group chat, false for a DM. |
| `kind` | string, optional | `group` or `dm`. The same fact as `is_group`, in the daemon's words. If both are given they must agree. |
| `created_at` | integer | When gpette first saw the conversation. |
| `last_active` | integer | When gpette last saw a message in it. |
| `reset_at` | integer, optional | When someone last reset the chat, in milliseconds. Messages before it are kept but not sent to the model. |

### participant

One per member of a conversation. Groups list their members. A DM lists the allowed person who talked in it.

| Field | Type | Meaning |
|---|---|---|
| `conversation_id` | string | The conversation. |
| `inbox_id` | string | The member's XMTP inbox id. |
| `permission_level` | integer | 0 member, 1 admin, 2 super admin. |
| `joined_at` | integer, optional | When gpette registered the member. The import uses the current time when it is missing. |

### allowed

One per inbox that is let in (the people a `password` gate has admitted).

| Field | Type | Meaning |
|---|---|---|
| `inbox_id` | string | The XMTP inbox id. |
| `eth_address` | string, optional | The address behind the inbox, if known. |
| `first_authenticated_at` | integer | When the person first got in. |
| `last_authenticated_at` | integer | When the person last passed the gate. |

The import does not count these people against today's `new_people_per_day`.

### message

One per stored message, oldest first within each conversation. This includes messages from before a reset and the bot's proactive notes.

| Field | Type | Meaning |
|---|---|---|
| `conversation_id` | string | The conversation. |
| `role` | string | `user`, `assistant`, `tool`, `system` or `Proactive`. |
| `timestamp` | integer | When it was sent, in milliseconds. |
| `sender_inbox_id` | string, optional | Who sent it. Empty for the bot's own messages. |
| `content` | any, optional | The message body, see below. |
| `tool_calls` | list, optional | The tool calls an assistant message made, in the OpenAI shape: `[{"id", "type": "function", "function": {"name", "arguments"}}]`. |
| `tool_call_id` | string, optional | On a `tool` message: the call it answers. |
| `function_call` | object, optional | The older single-call field, kept if present. |
| `token_count` | integer, optional | The token count, if one was stored. |
| `mode` | string, optional | Defaults to `personal`. |
| `source` | string, optional | What produced it, for example `SocketIO` or `ProactiveSystem`. Defaults to `unknown`. |

`content` is what `ConversationDB.store_message` takes. It is one of these:

- text: `{"type": "text", "value": "hello"}`, or a plain string;
- an attachment: `{"type": "attachment", "data": "<base64>", "mimeType": "image/png", "filename": "photo.png", "description": "the caption"}`. `data` is the file's bytes in standard base64. Any other keys in the object are kept as they are.

### timer

One per timer, whatever its status.

| Field | Type | Meaning |
|---|---|---|
| `timer_id` | string | The timer's id, the one the model sees. It is kept as it is. |
| `conversation_id` | string | The conversation it fires in. |
| `description` | string | Its short name. |
| `instructions` | string | What the model is told when it fires. |
| `fire_at` | integer | When it fires next. |
| `created_at` | integer | When it was set. |
| `timer_type` | string, optional | `one_shot` (the default) or `recurring`. |
| `interval_seconds` | integer, optional | For a recurring timer: the time between fires. |
| `last_fired_at` | integer, optional | When it last fired. |
| `status` | string, optional | `active` (the default), `fired` or `cancelled`. |
| `skip_session` | boolean, optional | True if a fire does not resume the conversation's session. |

## How the import behaves

The import reads and checks the whole file first. If any line is bad JSON, has an unknown `type`, lacks a required field or has a field of the wrong type, it prints the lines at fault and writes nothing.

With `--dry-run` it stops there: it prints the count per type and does not open the store.

Otherwise it writes, and prints per type how many lines it read, how many were new and how many were already present.

- A message that is already in the store is skipped. Two messages are the same when they have the same conversation, timestamp and role, and the same content, tool calls, tool call id, function call and sender (compared by a hash). If the file holds the same message twice, the store ends up with it twice, and a second import adds nothing.
- Conversations, participants, allowed inboxes and timers are matched by their ids. One that is already there is overwritten with the file's values.
- Every time and id in the file is kept, so a second export of the imported store matches the first one line for line (the order of lines may differ).

Running the same import twice changes nothing the second time.

## What is not in the file

The file holds history, not settings or caches. These stay behind:

- the daily count of new people let in (the `auth_history` table);
- resolved sender names, which are refetched;
- the server log;
- Claude Code session ids (`claude_sessions.json`), uploads, `memory.db` and `config_overrides.json`, which are files in the data folder. Copy them by hand if you need them.

## Storage backends

The script only calls `ConversationDB` methods, so it works with a storage plug-in that has them. Besides the methods gpette itself uses, it needs:

| Method | Used for |
|---|---|
| `list_conversations()` | every conversation, with `conversation_id`, `is_group`, `created_at`, `last_active`, `reset_at` |
| `list_messages(conversation_id)` | every message of a conversation, oldest first, in the shape `get_conversation_history` returns, with no limit and no reset cut-off |
| `list_timers()` | every timer, any status, with `skip_session` |
| `get_conversation_participants(conversation_id)` | members, with `joined_at` |
| `get_authenticated_inboxes()` | allowed inboxes |
| `get_timer_by_id(timer_id)`, `is_inbox_authenticated(inbox_id)` | telling new lines from present ones |
| `store_message(message)` | writing messages, timestamps kept |
| `restore_conversation(conversation_id, is_group, created_at, last_active, reset_at)` | writing a conversation as it is |
| `restore_participant(conversation_id, inbox_id, permission_level, joined_at)` | writing a member without changing the conversation's kind |
| `restore_authenticated_inbox(inbox_id, eth_address, first_authenticated_at, last_authenticated_at)` | writing an allowed inbox without the daily cap |
| `restore_timer(timer_id, conversation_id, description, instructions, fire_at, created_at, timer_type, interval_seconds, last_fired_at, status, skip_session)` | writing a timer with its own id and status |

The import writes messages before conversations, because storing a message updates its conversation's `last_active`. The conversation lines then put the original values back.
