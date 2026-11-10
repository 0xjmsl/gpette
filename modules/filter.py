"""Who this gpette answers: an ordered list of answer / ignore rules, checked before the gate.

Use it when several gpettes share one inbox (two installations of one key, e.g. the
owner's machine and a public one): each answers its own people and stays silent for
the rest. Also handy to mute a noisy group or a person without touching profiles.

    [filter]
    default = "answer"                    # what happens when no rule matches: answer | ignore

    [[filter.rules]]
    action = "ignore"                     # answer | ignore
    inboxes = ["<inbox id>", ...]         # the sender's inbox
    accounts = ["0x<account>", ...]       # the sender's account (needs [sessions] owner_resolver)
    groups = ["<group conversation id>"]  # the conversation, for group chats
    name = "the owner's machine answers these"   # optional, shown in logs and the admin panel

The first rule with a match wins. An empty rule matches nothing. `inboxes` and `accounts`
match the sender of a DM; `groups` match a group chat by its id (never by who is talking,
so one group is always answered by the same gpette). An ignored message is
dropped like a gate refusal: no reply, no read receipt, nothing stored.
"""
import logging

from config import Config


def _norm(value):
    return str(value or '').strip().lower().removeprefix('0x')


def _rules():
    raw = getattr(Config, 'FILTER', None) or {}
    return raw.get('rules') or []


def _default():
    raw = getattr(Config, 'FILTER', None) or {}
    value = str(raw.get('default') or 'answer').strip().lower()
    return value if value in ('answer', 'ignore') else 'answer'


def _account_of(inbox_id):
    """The sender's account address (normalized), via the owner resolver; '' if none."""
    from modules.profiles import owner_for
    owner = owner_for(inbox_id) if inbox_id else ''
    if owner.startswith('account:'):
        return _norm(owner[len('account:'):])
    return ''


def decide(conversation_id, sender_inbox_id, kind):
    """('answer' | 'ignore', rule label)."""
    rules = _rules()
    if not rules:
        return _default(), 'default'
    sender = _norm(sender_inbox_id)
    conv = _norm(conversation_id)
    account = None                      # resolved lazily: a chain read, only if a rule needs it
    for i, rule in enumerate(rules):
        label = rule.get('name') or f'rule {i + 1}'
        if kind == 'group':
            # A group is matched by its id only: matching its speaker would make the
            # answer depend on who happened to talk last.
            if conv and conv in {_norm(g) for g in rule.get('groups') or []}:
                return _action(rule), label
            continue
        if sender and sender in {_norm(x) for x in rule.get('inboxes') or []}:
            return _action(rule), label
        wanted = {_norm(a) for a in rule.get('accounts') or []}
        if wanted and sender:
            if account is None:
                try:
                    account = _account_of(sender_inbox_id)
                except Exception as e:
                    logging.warning(f"filter: account lookup failed ({e}); rule '{label}' skipped")
                    account = ''
            if account and account in wanted:
                return _action(rule), label
    return _default(), 'default'


def _action(rule):
    value = str(rule.get('action') or 'answer').strip().lower()
    return value if value in ('answer', 'ignore') else 'answer'
