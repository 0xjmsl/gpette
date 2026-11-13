"""Timers from the command line: set, list, edit, cancel.

    python scripts/timer.py list [--all] [--conv ID]
    python scripts/timer.py set "description" "instructions" --time TIME [--type recurring] [--skip-session] [--conv ID]
    python scripts/timer.py edit TIMER_ID [--desc ..] [--instructions ..] [--time TIME] [--type ..] [--skip-session | --no-skip-session]
    python scripts/timer.py cancel TIMER_ID
    python scripts/timer.py conversations

TIME is minutes from now ("30") or a clock time ("19:30", "7:30 PM", "7PM").
With --type recurring, minutes repeat at that interval and a clock time repeats daily.

Why this exists: the timer tools (setTimer, ...) are Python tools, and a Claude Code
bot cannot call those. A Claude Code profile that may run Bash calls this instead.
gpette reads due timers from the store every minute, so a row written here fires;
no restart is needed.

Which conversation: --conv, or GPETTE_CONVERSATION_ID, which gpette sets for every
Claude Code run to the chat being answered. So inside a bot's run, "set" needs no id.
A fire runs in the profile of the person the conversation belongs to, and its reply
goes to that conversation.

The store is the one gpette.toml points at (GPETTE_CONFIG works as for the server).
Only public ConversationDB methods are used, so any storage backend works.

A bot that can run this can set a timer in ANY conversation of this gpette. Give
Bash only to profiles you trust with that (the owner's).
"""
import argparse
import os
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Timer text can hold emoji; a Windows pipe is cp1252 and print() would raise.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _db():
    from models.conversation_db import ConversationDB   # imports config: honours GPETTE_CONFIG
    return ConversationDB()


def parse_time(text):
    """-> (minutes_from_now, label, is_clock_time), or (None, error, False)."""
    text = text.strip()
    try:
        minutes = int(float(text))
        if minutes < 1:
            return None, "A timer must be at least 1 minute away.", False
        return minutes, f"in {minutes}min", False
    except (ValueError, TypeError):
        pass
    now = datetime.now()
    when = None
    for fmt in ("%H:%M", "%I:%M %p", "%I:%M%p", "%I %p", "%I%p"):
        try:
            p = datetime.strptime(text.upper(), fmt)
            when = now.replace(hour=p.hour, minute=p.minute, second=0, microsecond=0)
            break
        except ValueError:
            continue
    if when is None:
        return None, f"Could not read '{text}'. Use '19:30', '7:30 PM', or '30' (minutes).", False
    note = ""
    if when <= now:
        when += timedelta(days=1)
        note = " (tomorrow)"
    minutes = max(1, int((when - now).total_seconds() / 60))
    return minutes, "at " + when.strftime("%H:%M") + note, True


def _conversation(args, required=True):
    conv = getattr(args, "conv", None) or os.environ.get("GPETTE_CONVERSATION_ID", "")
    if not conv and required:
        sys.exit("No conversation: pass --conv ID (see `timer.py conversations`). "
                 "Inside a bot's run GPETTE_CONVERSATION_ID is set for you.")
    return conv


def _line(t, now):
    left = int(t.get("fire_at") or 0) - now
    when = f"in {left // 60}m {left % 60}s" if left > 0 else "due"
    kind = ""
    if t.get("timer_type") == "recurring":
        every = int(t.get("interval_seconds") or 0)
        kind = " (daily)" if every == 86400 else f" (every {every // 60}min)"
    skip = " [skip-session]" if t.get("skip_session") else ""
    return f"[{t['timer_id']}] {t['description']}: fires {when}{kind}{skip}  conv={str(t['conversation_id'])[:8]}\n         instructions: {t['instructions']}"


def cmd_list(args):
    db = _db()
    timers = [t for t in db.list_timers() if t.get("status") == "active"]
    if not args.all:
        conv = _conversation(args)
        timers = [t for t in timers if t["conversation_id"] == conv]
    if not timers:
        print("No active timers.")
        return
    now = int(time.time())
    for t in sorted(timers, key=lambda t: t.get("fire_at") or 0):
        print(_line(t, now))


def cmd_set(args):
    conv = _conversation(args)
    minutes, info, clock = parse_time(args.time)
    if minutes is None:
        sys.exit("Error: " + info)
    every = (86400 if clock else minutes * 60) if args.type == "recurring" else None
    db = _db()
    known = {c["conversation_id"] for c in db.list_conversations()}
    if conv not in known:
        sys.exit(f"Error: no conversation '{conv}' in this store. A timer needs a chat that has "
                 "had at least one message (see `timer.py conversations`).")
    timer_id = db.create_timer(conv, args.description, args.instructions, int(time.time()) + minutes * 60,
                               timer_type=args.type, interval_seconds=every, skip_session=args.skip_session)
    print(f"Timer '{args.description}' set ({info}{' (recurring)' if every else ''}"
          f"{' [skip-session]' if args.skip_session else ''}). ID: {timer_id}")


def cmd_edit(args):
    db = _db()
    timer = db.get_timer_by_id(args.timer_id)
    if not timer:
        sys.exit(f"Error: timer '{args.timer_id}' not found.")
    if timer.get("status") != "active":
        sys.exit(f"Error: timer '{args.timer_id}' is {timer.get('status')}, cannot edit.")
    changes = {}
    if args.desc is not None:
        changes["description"] = args.desc
    if args.instructions is not None:
        changes["instructions"] = args.instructions
    kind = args.type or timer.get("timer_type")
    if args.type is not None:
        changes["timer_type"] = args.type
    if args.time is not None:
        minutes, info, clock = parse_time(args.time)
        if minutes is None:
            sys.exit("Error: " + info)
        changes["fire_at"] = int(time.time()) + minutes * 60
        if kind == "recurring":
            changes["interval_seconds"] = 86400 if clock else minutes * 60
    if args.skip_session:
        changes["skip_session"] = 1
    elif args.no_skip_session:
        changes["skip_session"] = 0
    if not changes:
        print("Nothing to update.")
        return
    db.update_timer(args.timer_id, **changes)
    print(f"Timer '{args.timer_id}' updated.")


def cmd_cancel(args):
    db = _db()
    timer = db.get_timer_by_id(args.timer_id)
    if not timer:
        sys.exit(f"Error: timer '{args.timer_id}' not found.")
    if timer.get("status") != "active":
        print(f"Timer '{args.timer_id}' is already {timer.get('status')}.")
        return
    db.cancel_timer(args.timer_id)
    print(f"Timer '{args.timer_id}' ({timer.get('description', '')}) cancelled.")


def cmd_conversations(args):
    """The chats this gpette knows, newest first, with the person a timer there would run as."""
    db = _db()
    rows = sorted(db.list_conversations(), key=lambda c: c.get("last_active") or 0, reverse=True)
    if not rows:
        print("No conversations yet. Send the bot a message first.")
        return
    for c in rows:
        ts = c.get("last_active") or 0
        ts = ts / 1000 if ts > 1e11 else ts
        last = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "never"
        kind = "group" if c.get("is_group") else "dm"
        who = "" if kind == "group" else f"  last human sender: {db.last_human_sender(c['conversation_id']) or 'none'}"
        print(f"{c['conversation_id']}  {kind:5}  last active {last}{who}")


def main():
    parser = argparse.ArgumentParser(description="Manage gpette timers")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="Active timers of one conversation")
    p.add_argument("--all", action="store_true", help="Every conversation")
    p.add_argument("--conv", help="Conversation id (default: GPETTE_CONVERSATION_ID)")

    p = sub.add_parser("set", help="Create a timer")
    p.add_argument("description", help="Short label")
    p.add_argument("instructions", help="What the bot should do when it fires")
    p.add_argument("--time", required=True, help="'30' (minutes) or '19:30' / '7:30 PM'")
    p.add_argument("--type", choices=["one_shot", "recurring"], default="one_shot")
    p.add_argument("--skip-session", action="store_true", help="Fire in a fresh session, without the chat's history")
    p.add_argument("--conv", help="Conversation id (default: GPETTE_CONVERSATION_ID)")

    p = sub.add_parser("edit", help="Change a timer")
    p.add_argument("timer_id")
    p.add_argument("--desc")
    p.add_argument("--instructions")
    p.add_argument("--time")
    p.add_argument("--type", choices=["one_shot", "recurring"])
    p.add_argument("--skip-session", action="store_true")
    p.add_argument("--no-skip-session", action="store_true")

    p = sub.add_parser("cancel", help="Cancel a timer")
    p.add_argument("timer_id")

    sub.add_parser("conversations", help="List the chats a timer can be set in")

    args = parser.parse_args()
    {"list": cmd_list, "set": cmd_set, "edit": cmd_edit, "cancel": cmd_cancel,
     "conversations": cmd_conversations}[args.command](args)


if __name__ == "__main__":
    main()
