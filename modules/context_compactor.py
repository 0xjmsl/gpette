"""
Running summaries for long chats with API models.

Claude Code keeps its own session (and compacts it itself), so a Claude Code chat is
never touched here. For the API models (Gemini, OpenAI, Claude API, LM Studio, Ollama)
gpette sends the history itself on every message, so a long chat is folded:

- each conversation keeps ONE saved summary plus a marker, the timestamp of the last
  message the summary covers;
- the model sees that summary (appended to the system prompt) plus the messages after
  the marker, word for word;
- when more than `Config.COMPACTION_AFTER` messages sit after the marker, all but the
  latest `Config.COMPACTION_KEEP` are folded into the summary in ONE call, and the
  marker moves past them.

So a long chat costs one extra call per (AFTER - KEEP) messages, never one per message.

`Config.COMPACTION_PROVIDER`: `same` (default) folds with the model the chat is using;
a provider name (lm_studio | claude | openai | gemini | ollama) folds with that one;
`disabled` turns folding off (the model then sees the plain recent history).
If the fold call fails, nothing is saved and the chat goes on with the summary it had;
the next message tries again.
"""

import logging
import time
from typing import Optional

from config import Config
from models.base_llm_service import BaseLLMService
from modules.thread_joins import get_group

logger = logging.getLogger(__name__)

PROVIDERS = ('same', 'lm_studio', 'claude', 'openai', 'gemini', 'ollama', 'disabled')

_FOLD_PROMPT = """You keep a running summary of a chat between a user and an assistant.
Update the summary with the newer messages below.
Keep: names, facts about the people, decisions, promises, things still open, and anything the assistant will need later.
Drop: greetings, small talk, and anything already settled that won't matter again.
Write plain prose, at most 250 words. Answer with the summary only.

Current summary:
{summary}

Newer messages:
{messages}"""


def summary_key(conversation_id: str) -> str:
    """One summary per history: joined threads (sessions.thread_joins) share it."""
    return "|".join(sorted(get_group(conversation_id)))


def _note(summary: Optional[str]) -> Optional[str]:
    if not summary:
        return None
    return f"\n\n---\n[Summary of the earlier conversation: {summary}]\n---"


class ContextCompactor:
    def __init__(self):
        # The most recent outcome, for the admin routes:
        # never_run | ok | waiting | skipped_claude_code | disabled | provider_unavailable | fold_failed
        self._last_status: dict = {"outcome": "never_run", "at": None, "error": None, "provider": None}
        self._provider_cache: dict = {}            # provider name -> service, or False after a failed start
        self._cached_provider_setting: Optional[str] = None

    def get_status(self) -> dict:
        return dict(self._last_status)

    def _set_status(self, outcome: str, error: str = None, provider: str = None):
        self._last_status = {"outcome": outcome, "at": time.time(), "error": error, "provider": provider}

    def compact(self, conversation_history: list, service=None, conversation_id: str = None,
                db=None, fold: bool = True) -> tuple:
        """Return (messages to send, note for the system prompt or None).

        fold=False applies the saved summary without ever calling a model (used while a
        tool loop is mid-flight, so a fold never lands between a tool call and its result).
        """
        from models.claudeCodeService import ClaudeCodeService
        if isinstance(service, ClaudeCodeService):
            self._set_status("skipped_claude_code")
            return conversation_history, None
        if Config.COMPACTION_PROVIDER == "disabled" or not conversation_id or db is None:
            self._set_status("disabled", provider=Config.COMPACTION_PROVIDER)
            return conversation_history, None

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

        caller, provider_name = self._resolve_caller(service)
        if caller is None:
            self._set_status("provider_unavailable", provider=provider_name,
                             error=f"COMPACTION_PROVIDER={Config.COMPACTION_PROVIDER}")
            return pending, _note(summary)

        new_summary = self._fold(summary, to_fold, caller, provider_name)
        if not new_summary:
            return pending, _note(summary)

        db.save_summary(key, new_summary, to_fold[-1].get("timestamp") or 0)
        self._set_status("ok", provider=provider_name)
        logger.info(f"[ContextCompactor] folded {len(to_fold)} messages of {key} with {provider_name}")
        return keep, _note(new_summary)

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

    # --- the fold -----------------------------------------------------------------

    def _fold(self, summary: Optional[str], messages: list, caller: BaseLLMService,
              provider: str) -> Optional[str]:
        prompt = _FOLD_PROMPT.format(summary=summary or "(none yet)",
                                     messages=self._format(messages))
        try:
            result = caller.quick_call(prompt, Config.COMPACTION_SUMMARY_TOKENS,
                                       model=Config.COMPACTION_MODEL or None)
        except Exception as e:
            logger.warning(f"[ContextCompactor] fold failed ({provider}): {e}")
            self._set_status("fold_failed", error=str(e), provider=provider)
            return None
        result = (result or "").strip()
        if not result:
            self._set_status("fold_failed", error="empty answer", provider=provider)
            return None
        return result

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
            db=None, fold: bool = True) -> tuple:
    """messages, note = compact(history, service, conversation_id, db)"""
    return get_compactor().compact(conversation_history, service, conversation_id, db, fold)


def get_status() -> dict:
    """Most recent compaction outcome, for the admin routes."""
    return get_compactor().get_status()
