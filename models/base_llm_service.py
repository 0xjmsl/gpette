from abc import ABC, abstractmethod
from typing import Optional


class BaseLLMService(ABC):

    @abstractmethod
    def send_message(self, message_data: dict, conversation_history: list,
                     system_prompt: str, functions_list: str = None,
                     skip_session: bool = False, is_group: bool = False,
                     profile=None) -> dict:
        """
        Returns dict with keys:
          role, content, timestamp, conversationId,
          tool_calls, token_count

        skip_session: If True, process this message without any prior context.
          Each model decides what this means:
          - Claude Code: don't resume existing session, don't save the new one
          - API models: ignore conversation_history (keep system_prompt)

        is_group: If True, the conversation is a group XMTP chat. Currently
          only ClaudeCodeService acts on this — it switches to an isolated
          sandbox cwd with no tools available. Other services ignore the flag.

        profile: the modules.profiles.Profile this message resolved to. Claude
          Code takes its whole environment from it (workdir, tools, model,
          limits, prompt, session key); API services get their system prompt and
          tool list already shaped by ChatController and ignore it.
        """
        pass

    @abstractmethod
    def quick_call(self, prompt: str, max_tokens: int,
                   model: Optional[str] = None) -> Optional[str]:
        """
        One-shot text completion for lightweight internal tasks (compaction,
        topic detection, summarization). No conversation history, no tools,
        single user prompt + a fixed terse system prompt. Each provider
        picks its own cheapest sensible model when `model` is None.

        Returns the response text on success, or None on any failure (network,
        auth, empty response). Implementations should not raise — log and
        return None instead. Callers (e.g. ContextCompactor) react to None
        with their own fallback policy.

        Implementations that fundamentally cannot quick_call (e.g. subprocess
        CLI wrappers like ClaudeCodeService) should raise NotImplementedError.
        Resolvers must avoid routing quick_call to such services.
        """
        pass

    def _create_error_response(self, error_value, conversation_id, timestamp, token_count=0) -> dict:
        """Shared error response builder."""
        return {
            "role": "assistant",
            "content": {"type": "text", "value": error_value},
            "timestamp": timestamp,
            "conversationId": conversation_id or "unknown",
            "tool_calls": None,
            "token_count": token_count
        }

    def _extract_text_from_content(self, content) -> str:
        """Shared helper to get plain text from message content."""
        if isinstance(content, dict):
            return str(content.get("value", ""))
        elif isinstance(content, str):
            return content
        return str(content) if content else ""

    def _extract_image_from_content(self, message_data) -> tuple:
        """Shared helper to extract (base64_data, mime_type, text) from attachment messages."""
        content = message_data.get("content")
        content_type = message_data.get("contentType")
        if content_type == "attachment" or (isinstance(content, dict) and content.get("type") == "attachment"):
            data = content.get("data") if isinstance(content, dict) else None
            mime = content.get("mimeType") or message_data.get("mimeType") or "image/jpeg"
            text = content.get("value") if isinstance(content, dict) else None
            return data, mime, text
        return None, None, None
