"""
Running summaries for long chats. One module, two ways a chat reaches the model.

Every conversation keeps ONE saved summary plus a marker, the timestamp of the last
message the summary covers. What differs is when the summary is updated, and how it
reaches the model.

API models (Gemini, OpenAI, Claude API, LM Studio, Ollama)
----------------------------------------------------------
gpette sends the history itself on every message, so the trigger is a message count:

- the model sees the summary (appended to the system prompt) plus the messages after
  the marker, word for word;
- when more than `Config.COMPACTION_AFTER` messages sit after the marker, all but the
  latest `Config.COMPACTION_KEEP` are folded into the summary in ONE call, and the
  marker moves past them.

So a long chat costs one extra call per (AFTER - KEEP) messages, never one per message.

Claude Code
-----------
Claude Code keeps the history in its own session, which grows with every turn and
every tool result. The trigger is the size of that session's context, read before each
turn from the previous run (`ClaudeCodeService.context_size`):

- below `COMPACTION_CC_TOPIC_TOKENS`: nothing;
- from there up to `COMPACTION_CC_FORCE_TOKENS`: one short call asks whether the new
  message starts a new topic. A new topic cuts; the same topic goes on;
- from `COMPACTION_CC_FORCE_TOKENS`: the cut is forced;
- `/compact` (the menu's "Compact now") cuts at any size.

A cut: gpette's own chat log after the marker, minus the latest `COMPACTION_KEEP` chat
messages, is folded into the summary (up to `COMPACTION_CC_SUMMARY_WORDS` words), and
the session is dropped. The next session starts with the summary plus those latest
messages, word for word (`ClaudeCodeService._build_context`). The log holds the
messages, not the tool work, so what a session learned by reading files survives a cut
only as far as it was said in the chat.

A timer fire is a message like any other. `/reset` (and the other reset phrases)
starts clean: the summary is emptied and the marker moves to now.

The calls are one-shot `claude` runs with no session and no tools
(`ClaudeCodeService.quick_call`), on `COMPACTION_CC_MODEL`, with the same login as the
chats. Keep the thresholds below the chat model's context window, or Claude Code's own
compaction gets there first.

Both
----
`Config.COMPACTION_PROVIDER`: `same` (default) uses the chat's own service; a provider
name (lm_studio | claude | openai | gemini | ollama) uses that one; `disabled` turns
all of it off (API models then see the plain recent history, a new Claude Code session
starts with the latest messages only).
If a call fails, nothing is saved and nothing is dropped; the next message tries again.
"""

import logging
import time
from typing import Optional

from config import Config
from models.base_llm_service import BaseLLMService
from modules.thread_joins import get_group

logger = logging.getLogger(__name__)

PROVIDERS = ('same', 'lm_studio', 'claude', 'openai', 'gemini', 'ollama', 'disabled')

# What a user can type to act on a Claude Code session's memory, and the fixed
# answers. Neither is conversation: they are left out of summaries and seeds.
COMPACT_COMMAND = "/compact"
RESET_COMMANDS = frozenset({"reset conversation", "fresh start", "new session", "/reset"})
RESET_REPLY = "Session cleared. Next message starts fresh."
COMPACT_OFF_REPLY = "Compaction is off (compaction.provider = disabled)."
_COMMAND_REPLIES = ("Compacted (", "Not compacted:", RESET_REPLY, COMPACT_OFF_REPLY)
API_SUMMARY_WORDS = 250
# How far back a Claude Code cut reads the chat log (rows after the marker).
CUT_READS = 300

_FOLD_PROMPT = """You keep a running summary of a chat between a user and an assistant.
Update the summary with the newer messages below.
Keep: names, facts about the people, decisions, promises, what is being worked on and where it stands, things still open, and anything the assistant will need later.
Drop: greetings, small talk, and anything already settled that won't matter again.
Write plain prose, at most {words} words. Answer with the summary only.

Current summary:
{summary}

Newer messages:
{messages}"""

_TOPIC_PROMPT = """Below are the latest messages of a chat, then a new message.
Does the new message go on with what the chat was about, or does it start a different subject?
Answer with one word: SAME or NEW.

Latest messages:
{recent}

New message:
{new}"""


def summary_key(conversation_id: str) -> str:
    """One summary per history: joined threads (sessions.thread_joins) share it."""
    return "|".join(sorted(get_group(conversation_id)))


def _note(summary: Optional[str]) -> Optional[str]:
    if not summary:
        return None
    return f"\n\n---\n[Summary of the earlier conversation: {summary}]\n---"


def _text_of(msg) -> str:
    """A stored or incoming message's text ('' for none)."""
    content = (msg or {}).get("content")
    if isinstance(content, dict):
        if content.get("type") == "attachment":
            name = content.get("filename") or "attachment"
            return f"{content.get('value') or ''} (sent a file: {name})".strip()
        content = content.get("value", "")
    if content is None:
        return ""
    return content if isinstance(content, str) else str(content)


def command_of(message) -> Optional[str]:
    """`reset`, `compact` or None: what a user's message asks of a Claude Code
    session. The one definition, shared with ClaudeCodeService."""
    if (message or {}).get("role") != "user":
        return None
    typed = _text_of(message).strip().lower()
    if typed in RESET_COMMANDS:
        return "reset"
    return "compact" if typed == COMPACT_COMMAND else None


def chat_lines(messages: list) -> list:
    """What a person would call the conversation, as (role, text, message): what
    the user and the assistant said, and each timer fire as one `timer` line.
    Tool traffic, the empty assistant message a timer fire stores, and the
    session commands with their answers are left out."""
    lines = []
    for msg in messages or []:
        role = msg.get("role")
        text = _text_of(msg).strip()
        if not text or text in ("PROACTIVE_TRIGGER", "TIMER_TRIGGER"):
            continue
        if role == "tool" and msg.get("source") == "ProactiveSystem":
            lines.append(("timer", text, msg))
        elif command_of(msg):
            continue
        elif role == "assistant" and text.startswith(_COMMAND_REPLIES):
            continue
        elif role in ("user", "assistant"):
            lines.append((role, text, msg))
    return lines


class ContextCompactor:
    def __init__(self):
        # The most recent outcome, for the admin routes:
        # never_run | ok | waiting | disabled | provider_unavailable | fold_failed
        # `report` is the same in words (the answer to /compact).
        self._last_status: dict = {"outcome": "never_run", "at": None, "error": None,
                                   "provider": None, "report": None}
        self._provider_cache: dict = {}            # provider name -> service, or False after a failed start
        self._cached_provider_setting: Optional[str] = None

    def get_status(self) -> dict:
        return dict(self._last_status)

    def _set_status(self, outcome: str, error: str = None, provider: str = None, report: str = None):
        self._last_status = {"outcome": outcome, "at": time.time(), "error": error,
                             "provider": provider, "report": report}

    def compact(self, conversation_history: list, service=None, conversation_id: str = None,
                db=None, fold: bool = True, profile=None, incoming: dict = None) -> tuple:
        """Return (messages to send, note or None).

        For an API model the note goes on the system prompt and the messages are the
        history it is sent. For Claude Code both are what a NEW session starts with.

        fold=False applies the saved summary without ever calling a model (used while a
        tool loop is mid-flight, so a fold never lands between a tool call and its result).
        `incoming` is the message being answered (already stored, the last of the log).
        """
        if Config.COMPACTION_PROVIDER == "disabled" or not conversation_id or db is None:
            self._set_status("disabled", provider=Config.COMPACTION_PROVIDER,
                             report="Compaction is off (compaction.provider = disabled).")
            return conversation_history, None
        from models.claudeCodeService import ClaudeCodeService
        if isinstance(service, ClaudeCodeService):
            return self._compact_claude_code(conversation_history, service, conversation_id,
                                             db, fold, profile, incoming)

        key = summary_key(conversation_id)
        saved = db.get_summary(key)
        summary = saved["summary"] if saved else None
        through = saved["through_ts"] if saved else None
        pending = [m for m in conversation_history
                   if through is None or (m.get("timestamp") or 0) > through]

        if not fold or len(pending) <= Config.COMPACTION_AFTER:
            if fold:
                self._set_status("waiting", provider=Config.COMPACTION_PROVIDER)
            return pending, _note(summary)

        keep_n = max(1, Config.COMPACTION_KEEP)
        to_fold, keep = pending[:-keep_n], pending[-keep_n:]
        # Never split a tool call from its result: a kept tool result needs its call.
        while keep and keep[0].get("role") == "tool":
            to_fold.append(keep.pop(0))
        if not to_fold:
            return pending, _note(summary)

        prompt = _FOLD_PROMPT.format(summary=summary or "(none yet)", words=API_SUMMARY_WORDS,
                                     messages=self._format(to_fold))
        new_summary, provider_name = self._ask(service, prompt, Config.COMPACTION_SUMMARY_TOKENS)
        if not new_summary:
            return pending, _note(summary)

        db.save_summary(key, new_summary, to_fold[-1].get("timestamp") or 0)
        self._set_status("ok", provider=provider_name)
        logger.info(f"[ContextCompactor] folded {len(to_fold)} messages of {key} with {provider_name}")
        return keep, _note(new_summary)

    # --- Claude Code ------------------------------------------------------------------

    def _compact_claude_code(self, conversation_history, service, conversation_id, db,
                             fold, profile, incoming) -> tuple:
        key = summary_key(conversation_id)
        saved = db.get_summary(key)
        summary = saved["summary"] if saved else None
        through = saved["through_ts"] if saved else None
        text = _text_of(incoming).strip()
        command = command_of(incoming)

        # A reset starts clean: no summary, and nothing before now is carried over.
        # (ClaudeCodeService drops the session and answers.)
        if command == "reset":
            db.save_summary(key, "", int(time.time() * 1000))
            self._set_status("ok", report="Session and summary cleared.")
            return [], None

        # None = no session on file (the next run starts one), 0 = size unknown.
        context = service.context_size(conversation_id, profile)
        if fold:
            self._set_status("waiting", provider=Config.COMPACTION_PROVIDER)
        reason = None
        if command == "compact":
            reason = "asked"
        elif fold and context:
            force, topic = Config.COMPACTION_CC_FORCE_TOKENS, Config.COMPACTION_CC_TOPIC_TOKENS
            if force and context >= force:
                reason = "forced"
            elif topic and context >= topic and text and self._new_topic(service, conversation_history, text):
                reason = "new topic"

        if reason:
            done, summary, through, report = self._cut(
                service, conversation_id, profile, db, key, summary, through,
                self._log(db, conversation_id, through, incoming), context, reason)
            if done:
                context = None
            if command == "compact":
                # ClaudeCodeService answers /compact with this, without a model turn.
                incoming["compactReport"] = report

        if context is None:
            # A new session starts on this message: the latest chat messages, read
            # from the log itself (the history passed in is a short window that
            # silent timer fires can fill).
            lines = [l for l in chat_lines(self._log(db, conversation_id, through, incoming))
                     if l[0] != "timer"]
            seed = [l[2] for l in lines[-max(1, Config.COMPACTION_KEEP):]]
        else:
            # The session goes on and already holds the chat. This is only used if
            # the resume fails and the run falls back to a new session.
            seed = [m for m in conversation_history
                    if through is None or (m.get("timestamp") or 0) > through]
        return seed, (f"[Summary of the earlier conversation: {summary}]" if summary else None)

    @staticmethod
    def _log(db, conversation_id, through, incoming=None) -> list:
        """The stored chat after the marker, without the message being answered
        (it is already stored, and the last one)."""
        rows = db.get_conversation_history(get_group(conversation_id), limit=CUT_READS)
        if incoming is not None:
            rows = rows[:-1]
        return [m for m in rows if through is None or (m.get("timestamp") or 0) > through]

    def _new_topic(self, service, conversation_history, text) -> bool:
        """One short call. Only a clear NEW cuts: a failed or unclear answer is SAME."""
        recent = [l for l in chat_lines(conversation_history) if l[0] != "timer"][-6:]
        if not recent:
            return False
        prompt = _TOPIC_PROMPT.format(
            recent="\n".join(f"{role}: {t[:600]}" for role, t, _ in recent), new=text[:1500])
        answer, _ = self._ask(service, prompt, 10)
        return bool(answer) and answer.strip().strip("*.\"'`").upper().startswith("NEW")

    def _cut(self, service, conversation_id, profile, db, key, summary, through, log,
             context, reason) -> tuple:
        """Fold the chat log (the rows after the marker) into the summary and drop
        the session. Returns (done, summary, marker, report): done is False when the
        summary call failed, and then nothing changed."""
        lines = chat_lines(log)
        chat = [i for i, l in enumerate(lines) if l[0] != "timer"]
        keep_n = max(1, Config.COMPACTION_KEEP)
        # Everything before the latest `keep` chat messages is folded.
        to_fold = lines[:chat[-keep_n]] if len(chat) > keep_n else []
        size = f"{context:,} tokens" if context else "no session on file"
        if to_fold:
            words = Config.COMPACTION_CC_SUMMARY_WORDS
            prompt = _FOLD_PROMPT.format(
                summary=summary or "(none yet)", words=words,
                messages="\n".join(f"{role}: {t[:300] if role == 'timer' else t[:1500]}"
                                   for role, t, _ in to_fold))
            new_summary, provider_name = self._ask(service, prompt, words * 2)
            if not new_summary:
                error = self._last_status.get("error")
                report = (f"Not compacted: the summary call failed ({error}). "
                          "The session is as it was.")
                self._set_status("fold_failed", error=error, provider=provider_name, report=report)
                logger.warning(f"[ContextCompactor] cut of {key} ({reason}, {size}) failed: {error}")
                return False, summary, through, report
            summary, through = new_summary, to_fold[-1][2].get("timestamp") or 0
            db.save_summary(key, summary, through)
        service.clear_session(conversation_id, profile)
        report = (f"Compacted ({reason}, {size}): {len(to_fold)} messages folded into a summary of "
                  f"{len((summary or '').split())} words. The next message starts a new session "
                  f"with the summary and the latest {keep_n} messages.")
        self._set_status("ok", provider=Config.COMPACTION_PROVIDER, report=report)
        logger.info(f"[ContextCompactor] {key}: {report}")
        return True, summary, through, report

    # --- provider ---------------------------------------------------------------

    def _resolve_caller(self, service) -> tuple:
        configured = Config.COMPACTION_PROVIDER
        if configured != self._cached_provider_setting:
            self._provider_cache = {}
            self._cached_provider_setting = configured
        if configured == "same":
            if service is None:
                return None, "same (no chat service)"
            return service, type(service).__name__
        cached = self._provider_cache.get(configured, "missing")
        if cached is False:
            return None, configured
        if cached != "missing":
            return cached, configured
        try:
            instance = self._instantiate_provider(configured)
        except Exception as e:
            logger.warning(f"[ContextCompactor] could not start compaction provider '{configured}': {e}")
            self._provider_cache[configured] = False
            return None, configured
        self._provider_cache[configured] = instance
        return instance, configured

    @staticmethod
    def _instantiate_provider(provider: str) -> BaseLLMService:
        if provider == "lm_studio":
            from models.lmStudioService import LMStudioService
            return LMStudioService()
        if provider == "claude":
            from models.claudeService import ClaudeService
            return ClaudeService()
        if provider == "openai":
            from models.openAIService import OpenAIService
            return OpenAIService()
        if provider == "gemini":
            from models.geminiService import GeminiService
            return GeminiService()
        if provider == "ollama":
            from models.ollamaService import OllamaService
            return OllamaService()
        raise ValueError(f"Unknown COMPACTION_PROVIDER '{provider}'. Valid: {' | '.join(PROVIDERS)}")

    # --- the call ---------------------------------------------------------------------

    def _ask(self, service, prompt: str, max_tokens: int) -> tuple:
        """One call to the compaction provider. Returns (text or None, provider name);
        a failure sets the status and never raises."""
        caller, provider = self._resolve_caller(service)
        if caller is None:
            self._set_status("provider_unavailable", provider=provider,
                             error=f"COMPACTION_PROVIDER={Config.COMPACTION_PROVIDER}")
            return None, provider
        from models.claudeCodeService import ClaudeCodeService
        model = (Config.COMPACTION_CC_MODEL if isinstance(caller, ClaudeCodeService)
                 else Config.COMPACTION_MODEL)
        try:
            result = caller.quick_call(prompt, max_tokens, model=model or None)
        except Exception as e:
            logger.warning(f"[ContextCompactor] call failed ({provider}): {e}")
            self._set_status("fold_failed", error=str(e), provider=provider)
            return None, provider
        result = (result or "").strip()
        if not result:
            self._set_status("fold_failed", error="empty answer", provider=provider)
            return None, provider
        return result, provider

    @staticmethod
    def _format(messages: list) -> str:
        lines = []
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, dict):
                content = content.get("value", "")
            elif content is None:
                content = ""
            elif not isinstance(content, str):
                content = str(content)
            if not content and msg.get("tool_calls"):
                content = "(called a tool)"
            if len(content) > 1500:
                content = content[:1500] + "..."
            lines.append(f"{msg.get('role', 'unknown')}: {content}")
        return "\n".join(lines)


_compactor = None


def get_compactor() -> ContextCompactor:
    global _compactor
    if _compactor is None:
        _compactor = ContextCompactor()
    return _compactor


def compact(conversation_history: list, service=None, conversation_id: str = None,
            db=None, fold: bool = True, profile=None, incoming: dict = None) -> tuple:
    """messages, note = compact(history, service, conversation_id, db)"""
    return get_compactor().compact(conversation_history, service, conversation_id, db, fold,
                                   profile, incoming)


def get_status() -> dict:
    """Most recent compaction outcome, for the admin routes."""
    return get_compactor().get_status()
