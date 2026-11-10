"""Move gpette's history between stores: export to JSONL, import from JSONL.

    python scripts/history.py export <out.jsonl>
    python scripts/history.py import <in.jsonl> [--dry-run]

The store is the one gpette.toml points at (GPETTE_CONFIG works as for the
server). Only public ConversationDB methods are used, so this works with any
storage backend that implements them. The file format is in docs/history_format.md.
"""
import argparse
import hashlib
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FORMAT = 'gpette-history'
VERSION = 1
TYPES = ('meta', 'conversation', 'participant', 'allowed', 'message', 'timer')

# type -> {field: (allowed python types, required)}
_INT = (int,)
_OPT_INT = (int, type(None))
_STR = (str,)
_OPT_STR = (str, type(None))
_ANY = (object,)
SCHEMA = {
    'meta': {'format': (_STR, True), 'version': (_INT, True)},
    'conversation': {
        'conversation_id': (_STR, True), 'is_group': ((bool,), True), 'kind': (_STR, False),
        'created_at': (_INT, True), 'last_active': (_INT, True), 'reset_at': (_OPT_INT, False),
    },
    'participant': {
        'conversation_id': (_STR, True), 'inbox_id': (_STR, True),
        'permission_level': (_INT, True), 'joined_at': (_OPT_INT, False),
    },
    'allowed': {
        'inbox_id': (_STR, True), 'eth_address': (_OPT_STR, False),
        'first_authenticated_at': (_INT, True), 'last_authenticated_at': (_INT, True),
    },
    'message': {
        'conversation_id': (_STR, True), 'role': (_STR, True), 'timestamp': (_INT, True),
        'sender_inbox_id': (_OPT_STR, False), 'content': (_ANY, False),
        'tool_calls': ((list, type(None)), False), 'tool_call_id': (_OPT_STR, False),
        'function_call': ((dict, type(None)), False), 'token_count': (_OPT_INT, False),
        'mode': (_OPT_STR, False), 'source': (_OPT_STR, False),
    },
    'timer': {
        'timer_id': (_STR, True), 'conversation_id': (_STR, True), 'description': (_STR, True),
        'instructions': (_STR, True), 'fire_at': (_INT, True), 'created_at': (_INT, True),
        'timer_type': (_STR, False), 'interval_seconds': (_OPT_INT, False),
        'last_fired_at': (_OPT_INT, False), 'status': (_STR, False), 'skip_session': ((bool,), False),
    },
}


def _db():
    from models.conversation_db import ConversationDB   # imports config: honours GPETTE_CONFIG
    return ConversationDB()


def _dump(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


# ---------------------------------------------------------------- messages

def _clean_content(content):
    """Content as stored: empty means none; drop the placeholder text the
    SQLite backend puts on attachments (store_message makes it again)."""
    if not content:
        return None
    if isinstance(content, dict) and content.get('type') == 'attachment':
        content = dict(content)
        if content.get('value') == f"Attachment: {content.get('filename', 'file')}":
            content.pop('value')
    return content


def _message_record(m):
    """A ConversationDB message dict -> a `message` line."""
    return {
        'type': 'message',
        'conversation_id': m.get('conversationId'),
        'sender_inbox_id': m.get('senderInboxId'),
        'role': m.get('role'),
        'timestamp': m.get('timestamp'),
        'content': _clean_content(m.get('content')),
        'tool_calls': m.get('tool_calls') or None,
        'tool_call_id': m.get('tool_call_id'),
        'function_call': m.get('function_call') or None,
        'token_count': m.get('token_count'),
        'mode': m.get('mode'),
        'source': m.get('source'),
    }


def _message_key(rec):
    """Dedupe key: conversation, timestamp, role, and a hash of everything the message says."""
    body = {k: rec.get(k) for k in ('sender_inbox_id', 'tool_calls', 'tool_call_id', 'function_call')}
    body['content'] = _clean_content(rec.get('content'))
    body['tool_calls'] = body['tool_calls'] or None
    body['function_call'] = body['function_call'] or None
    digest = hashlib.sha256(_dump(body).encode('utf-8')).hexdigest()
    return (rec['conversation_id'], rec['timestamp'], rec['role'], digest)


# ---------------------------------------------------------------- export

def export(path):
    db = _db()
    counts = Counter()
    lines = [{'type': 'meta', 'format': FORMAT, 'version': VERSION}]

    conversations = db.list_conversations()
    for c in conversations:
        is_group = bool(c.get('is_group'))
        lines.append({
            'type': 'conversation', 'conversation_id': c['conversation_id'],
            'is_group': is_group, 'kind': 'group' if is_group else 'dm',
            'created_at': c.get('created_at'), 'last_active': c.get('last_active'),
            'reset_at': c.get('reset_at'),
        })
    for c in conversations:
        for p in sorted(db.get_conversation_participants(c['conversation_id']), key=lambda p: p['inbox_id']):
            lines.append({
                'type': 'participant', 'conversation_id': c['conversation_id'],
                'inbox_id': p['inbox_id'], 'permission_level': p.get('permission_level') or 0,
                'joined_at': p.get('joined_at'),
            })
    for a in sorted(db.get_authenticated_inboxes(), key=lambda a: a['inbox_id']):
        lines.append({
            'type': 'allowed', 'inbox_id': a['inbox_id'], 'eth_address': a.get('eth_address'),
            'first_authenticated_at': a.get('first_authenticated_at'),
            'last_authenticated_at': a.get('last_authenticated_at'),
        })
    for c in conversations:
        for m in db.list_messages(c['conversation_id']):
            lines.append(_message_record(m))
    for t in db.list_timers():
        lines.append({
            'type': 'timer', 'timer_id': t['timer_id'], 'conversation_id': t['conversation_id'],
            'description': t['description'], 'instructions': t['instructions'],
            'timer_type': t.get('timer_type') or 'one_shot', 'interval_seconds': t.get('interval_seconds'),
            'fire_at': t['fire_at'], 'created_at': t['created_at'], 'last_fired_at': t.get('last_fired_at'),
            'status': t.get('status') or 'active', 'skip_session': bool(t.get('skip_session')),
        })

    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        for rec in lines:
            f.write(_dump(rec) + '\n')
            counts[rec['type']] += 1
    counts.pop('meta', None)
    print(f"exported to {path}")
    for t in TYPES[1:]:
        print(f"  {t}: {counts[t]}")
    return 0


# ---------------------------------------------------------------- import

def _validate(rec, lineno):
    if not isinstance(rec, dict):
        return f"line {lineno}: not a JSON object"
    rtype = rec.get('type')
    if rtype not in SCHEMA:
        return f"line {lineno}: unknown type {rtype!r}"
    for field, (types, required) in SCHEMA[rtype].items():
        if field not in rec or (rec[field] is None and required):
            if required:
                return f"line {lineno}: {rtype} needs '{field}'"
            continue
        val = rec[field]
        if types is not _ANY and (not isinstance(val, types) or (isinstance(val, bool) and bool not in types)):
            return f"line {lineno}: {rtype}.{field} has the wrong type"
    if rtype == 'meta' and (rec['format'] != FORMAT or rec['version'] > VERSION):
        return f"line {lineno}: not a {FORMAT} file of version {VERSION} or older"
    if rtype == 'conversation' and rec.get('kind') not in (None, 'dm', 'group'):
        return f"line {lineno}: conversation.kind must be 'dm' or 'group'"
    if rtype == 'conversation' and rec.get('kind') and (rec['kind'] == 'group') != rec['is_group']:
        return f"line {lineno}: conversation.kind and is_group disagree"
    if rtype == 'message' and isinstance(rec.get('content'), dict) and rec['content'].get('type') == 'attachment':
        import base64, binascii
        data = rec['content'].get('data')
        if data is not None:
            try:
                base64.b64decode(data, validate=True)
            except (binascii.Error, ValueError, TypeError):
                return f"line {lineno}: attachment data is not valid base64"
    return None


def _read(path):
    records, errors = [], []
    with open(path, 'r', encoding='utf-8') as f:
        for lineno, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                errors.append(f"line {lineno}: bad JSON ({e.msg})")
                continue
            err = _validate(rec, lineno)
            if err:
                errors.append(err)
            else:
                records.append(rec)
    return records, errors


def do_import(path, dry_run=False):
    records, errors = _read(path)
    if errors:
        for e in errors[:20]:
            print(e, file=sys.stderr)
        if len(errors) > 20:
            print(f"... and {len(errors) - 20} more", file=sys.stderr)
        print(f"{len(errors)} invalid line(s); nothing imported", file=sys.stderr)
        return 1

    by_type = {t: [r for r in records if r['type'] == t] for t in TYPES}
    if dry_run:
        print(f"dry run: {path} is valid, nothing written")
        for t in TYPES[1:]:
            print(f"  {t}: {len(by_type[t])}")
        return 0

    db = _db()
    stats = {t: Counter() for t in TYPES[1:]}
    known = {c['conversation_id'] for c in db.list_conversations()}

    # Messages first: storing one touches its conversation's last_active, and the
    # conversation lines below then put the original values back.
    existing = {}
    for rec in by_type['message']:
        cid = rec['conversation_id']
        if cid not in existing:
            existing[cid] = Counter(_message_key(_message_record(m)) for m in db.list_messages(cid))
        key = _message_key(rec)
        if existing[cid][key] > 0:
            existing[cid][key] -= 1          # already there: one stored copy per copy in the file
            stats['message']['skipped'] += 1
            continue
        db.store_message({
            'conversationId': cid,
            'senderInboxId': rec.get('sender_inbox_id'),
            'role': rec['role'],
            'timestamp': rec['timestamp'],
            'content': rec.get('content'),
            'tool_calls': rec.get('tool_calls'),
            'tool_call_id': rec.get('tool_call_id'),
            'function_call': rec.get('function_call'),
            'token_count': rec.get('token_count'),
            'mode': rec.get('mode') or 'personal',
            'source': rec.get('source') or 'unknown',
        })
        stats['message']['inserted'] += 1

    for rec in by_type['conversation']:
        db.restore_conversation(rec['conversation_id'], rec['is_group'], rec['created_at'],
                                rec['last_active'], rec.get('reset_at'))
        stats['conversation']['updated' if rec['conversation_id'] in known else 'inserted'] += 1

    for rec in by_type['participant']:
        had = {p['inbox_id'] for p in db.get_conversation_participants(rec['conversation_id'])}
        db.restore_participant(rec['conversation_id'], rec['inbox_id'], rec['permission_level'], rec.get('joined_at'))
        stats['participant']['updated' if rec['inbox_id'] in had else 'inserted'] += 1

    for rec in by_type['allowed']:
        had = db.is_inbox_authenticated(rec['inbox_id'])
        db.restore_authenticated_inbox(rec['inbox_id'], rec.get('eth_address'),
                                       rec['first_authenticated_at'], rec['last_authenticated_at'])
        stats['allowed']['updated' if had else 'inserted'] += 1

    for rec in by_type['timer']:
        had = db.get_timer_by_id(rec['timer_id']) is not None
        db.restore_timer(rec['timer_id'], rec['conversation_id'], rec['description'], rec['instructions'],
                         rec['fire_at'], rec['created_at'], timer_type=rec.get('timer_type') or 'one_shot',
                         interval_seconds=rec.get('interval_seconds'), last_fired_at=rec.get('last_fired_at'),
                         status=rec.get('status') or 'active', skip_session=bool(rec.get('skip_session')))
        stats['timer']['updated' if had else 'inserted'] += 1

    print(f"imported {path}")
    for t in TYPES[1:]:
        s = stats[t]
        if t == 'message':
            extra = f", {s['skipped']} already present (skipped)"
        else:
            extra = f", {s['updated']} already present (overwritten)"
        print(f"  {t}: {len(by_type[t])} read, {s['inserted']} new{extra}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Export or import gpette history as JSONL.")
    sub = ap.add_subparsers(dest='cmd', required=True)
    ex = sub.add_parser('export', help="write every conversation, message and timer to a file")
    ex.add_argument('path')
    im = sub.add_parser('import', help="load a file written by export")
    im.add_argument('path')
    im.add_argument('--dry-run', action='store_true', help="validate and count, write nothing")
    args = ap.parse_args(argv)
    if args.cmd == 'export':
        return export(args.path)
    return do_import(args.path, args.dry_run)


if __name__ == '__main__':
    sys.exit(main())
