"""The password gate.

- DM: the sender must already be in (the `authenticated_inboxes` table). A
  stranger gets in by sending exactly `[gate] password` as a DM; the bot answers
  `[gate] welcome` and stops there. At most `[gate] new_people_per_day` new
  people get in per day; past that the bot answers `[gate] limit_reply`.
  Anything else from a stranger is dropped silently.
- Group: answered iff at least one member is already in (vouched for by sharing
  a group with someone known); the speaker need not be. The password never works
  in a group (so nobody slips in through a group invite) and is dropped there.
  The decision is cached per group for GROUP_AUTH_TTL seconds and dropped on
  every member-list sync (`invalidate_conversation`).
- A group whose member list has not synced yet has no known members: dropped
  until the first sync.
"""
import logging
import time

from config import Config


class PasswordGate:
    GROUP_AUTH_TTL = 3600

    def __init__(self, conversation_db):
        self.conversation_db = conversation_db
        self.authenticated_inboxes = set()
        self.group_auth_cache = {}     # conversation_id -> (allowed, expires_at)
        try:
            rows = conversation_db.get_authenticated_inboxes() or []
            self.authenticated_inboxes = {r['inbox_id'] for r in rows}
        except Exception as e:
            logging.error(f"PasswordGate: could not load who is in: {e}")

    @staticmethod
    def _text(message):
        content = message.get('content', {})
        value = content.get('value', '') if isinstance(content, dict) else content
        return value.strip() if isinstance(value, str) else ''

    def _is_password(self, text):
        password = str(Config.GATE_PASSWORD or '')
        return bool(password) and text == password.strip()

    def check_message(self, message, kind=None):
        sender = message.get('senderInboxId')
        conversation_id = message.get('conversationId')
        kind = kind or message.get('kind')
        text = self._text(message)

        if kind == 'group':
            if self._is_password(text):
                logging.warning(f"PasswordGate: password sent in group {conversation_id}, dropped")
                return {'action': 'reject'}
            if self._group_is_allowed(conversation_id):
                return {'action': 'allow'}
            logging.warning(f"PasswordGate: group {conversation_id} has nobody who is in, dropped")
            return {'action': 'reject'}

        if self._is_password(text):
            return self._let_in(sender, conversation_id)
        if self.is_authenticated(sender):
            return {'action': 'allow'}
        logging.warning(f"PasswordGate: DM from a stranger {sender}, dropped")
        return {'action': 'reject'}

    def _group_is_allowed(self, conversation_id):
        now = int(time.time())
        cached = self.group_auth_cache.get(conversation_id)
        if cached and cached[1] > now:
            return cached[0]
        allowed = bool(self.conversation_db.has_authenticated_participant(conversation_id))
        self.group_auth_cache[conversation_id] = (allowed, now + self.GROUP_AUTH_TTL)
        return allowed

    def invalidate_conversation(self, conversation_id):
        self.group_auth_cache.pop(conversation_id, None)

    def _let_in(self, sender, conversation_id):
        if not sender:
            return {'action': 'reject'}
        if not self.conversation_db.can_authenticate_today(sender):
            logging.warning(f"PasswordGate: new-people cap reached, {sender} not let in")
            return {'action': 'auth_limit',
                    'response': self._reply(conversation_id, sender, Config.GATE_LIMIT_REPLY)}
        self.conversation_db.authenticate_inbox(sender)
        self.authenticated_inboxes.add(sender)
        logging.info(f"PasswordGate: let in {sender}")
        return {'action': 'auth_success',
                'response': self._reply(conversation_id, sender, Config.GATE_WELCOME)}

    def is_authenticated(self, inbox_id):
        if not inbox_id:
            return False
        if inbox_id in self.authenticated_inboxes:
            return True
        if self.conversation_db.is_inbox_authenticated(inbox_id):
            self.authenticated_inboxes.add(inbox_id)
            return True
        return False

    @staticmethod
    def _reply(conversation_id, sender, text):
        return {
            'role': 'assistant',
            'content': {'type': 'text', 'value': text},
            'conversationId': conversation_id,
            'senderInboxId': sender,
            'timestamp': int(time.time()),
            'source': 'Gate',
        }
