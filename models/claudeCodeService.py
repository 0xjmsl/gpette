import re
import subprocess
import json
import os
import logging
import datetime
import base64
import uuid
from typing import Optional

from models.base_llm_service import BaseLLMService
from config import Config
from utils.message_logger import log_message_flow
from modules.thread_joins import get_group
from modules.profiles import base_profile, workdir_for
from modules import transcripts

# Both live under the data folder (server.data_dir in gpette.toml).
SESSIONS_FILE = os.path.join(Config.DATA_DIR, "claude_sessions.json")
IMAGES_DIR = os.path.join(Config.UPLOAD_FOLDER, "claude_images")


class ClaudeCodeService(BaseLLMService):
    """Wraps Claude Code CLI as an LLM backend via subprocess.

    Claude Code manages its own conversation history, tools, and context.
    This service ignores conversation_history, system_prompt, and functions_list
    — it just forwards the user message and parses the response.

    Session IDs are persisted to claude_sessions.json so they survive server
    restarts. Thread-joined conversations share the same session. When starting
    a new session (no resume), the last 4 DB messages are prepended as context.
    """

    MAX_EMPTY_RETRIES = 3

    def __init__(self):
        self._sessions = self._load_sessions()
        self._empty_counters = {}  # conversation_id -> consecutive empty count

    # --- Session persistence ---

    def _load_sessions(self):
        try:
            with open(SESSIONS_FILE, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_sessions(self):
        try:
            with open(SESSIONS_FILE, "w") as f:
                json.dump(self._sessions, f, indent=2)
        except Exception as e:
            logging.error(f"ClaudeCodeService: failed to save sessions: {e}")

    def _find_session(self, conversation_id, profile=None):
        """Find an existing session for this conversation (or its profile, when the
        profile shares one session) or any conversation in its thread group."""
        key = profile.session_key(conversation_id) if profile else conversation_id
        session = self._sessions.get(key)
        if session:
            return session
        if key != conversation_id:
            return None
        for cid in get_group(conversation_id):
            session = self._sessions.get(cid)
            if session:
                return session
        return None

    # --- Context injection for new sessions ---

    def _build_context(self, conversation_history):
        """Format the last 4 messages as context for a new session."""
        if not conversation_history:
            return ""

        recent = conversation_history[-4:]
        lines = ["[Recent conversation context:]"]
        for msg in recent:
            role = msg.get("role", "unknown")
            # Skip tool-call/tool-return noise
            if role == "tool":
                continue
            content = msg.get("content", "")
            if isinstance(content, dict):
                content = content.get("value", "") or content.get("text", "")
            if not content or content in ("PROACTIVE_TRIGGER", "TIMER_TRIGGER"):
                continue
            lines.append(f"[{role}]: {content}")

        if len(lines) <= 1:
            return ""
        return "\n".join(lines) + "\n\n---\n\n"

    # --- Image handling ---

    def _save_image(self, base64_data, mime_type, folder=None):
        """Decode base64 image data, save it, return the absolute path.

        `folder` = the profile's working folder: images go to <workdir>/images,
        so a profile can be allowed to read ONLY them with
        allowed_tools = ["Read(./images/**)"]."""
        target = os.path.join(folder, "images") if folder else IMAGES_DIR
        os.makedirs(target, exist_ok=True)
        ext = {
            "image/jpeg": ".jpg", "image/png": ".png",
            "image/gif": ".gif", "image/webp": ".webp",
        }.get(mime_type, ".jpg")
        filename = f"{uuid.uuid4().hex}{ext}"
        filepath = os.path.join(target, filename)
        with open(filepath, "wb") as f:
            f.write(base64.b64decode(base64_data))
        return filepath

    # --- Reset trigger ---

    _RESET_TRIGGERS = {"reset conversation", "fresh start", "new session", "/reset"}

    def _check_reset_trigger(self, message_data: dict, profile=None) -> bool:
        """Check if the message is a session reset command. If so, clear and return True."""
        content = message_data.get("content")
        if isinstance(content, dict):
            content = content.get("value", "")
        if isinstance(content, str) and content.strip().lower() in self._RESET_TRIGGERS:
            self.clear_session(message_data.get("conversationId", "unknown"), profile)
            return True
        return False

    # --- Core ---

    def send_message(self, message_data: dict, conversation_history: list,
                     system_prompt: str, functions_list: str = None,
                     skip_session: bool = False, is_group: bool = False,
                     profile=None) -> dict:
        conversation_id = message_data.get("conversationId", "unknown")
        now = datetime.datetime.now()
        timestamp = int(now.timestamp() * 1000)
        if profile is None:
            profile = base_profile(is_group)
        # A DM sender's resolved name (chat_controller sets it; groups carry names
        # as message prefixes instead). Claude Code ignores system_prompt, so the
        # name travels in the appended prompt.
        sender_name = (message_data.get("senderName") or "").strip()
        note = (f"The person writing to you is named {sender_name}. If they ask to be "
                f"called something else, use that.") if sender_name and not is_group else ""
        session_key = profile.session_key(conversation_id)

        # Reset session trigger — handle before any processing
        if self._check_reset_trigger(message_data, profile):
            return {
                "role": "assistant",
                "content": {"type": "text", "value": "Session cleared. Next message starts fresh."},
                "timestamp": timestamp,
                "conversationId": conversation_id,
                "tool_calls": None,
                "token_count": 0
            }

        # Check for image attachment
        image_data, mime_type, image_text = self._extract_image_from_content(message_data)
        if image_data and mime_type and mime_type.startswith("image/") and profile is not None and profile.no_tools:
            # Claude Code sees an image only by opening its file (the Read tool).
            # A no-tools profile can't, and asking it to made the CLI stall. Say so
            # plainly instead; give the profile "Read" in allowed_tools to see images.
            caption = (image_text or "").replace("@image", "").strip()
            text = ("[The user sent an image. You cannot view images in this chat. "
                    "Tell them briefly.]" + (f"\nTheir caption: {caption}" if caption else ""))
            logging.info("ClaudeCodeService: image in a no-tools profile, sent as a note")
        elif image_data and mime_type and mime_type.startswith("image/"):
            try:
                image_folder = workdir_for(profile, conversation_id)
                image_path = self._save_image(image_data, mime_type, image_folder)
                text = image_text or ""
                if "@image" in text:
                    text = text.replace("@image", image_path)
                else:
                    text = f"{text}\n{image_path}".strip() if text else image_path
                logging.info(f"ClaudeCodeService: saved image to {image_path}")
                # Gallery: the other images, one path per line (server.py extra_images).
                content = message_data.get("content")
                for extra in (content.get("extra_images") or []) if isinstance(content, dict) else []:
                    extra_path = self._save_image(extra["data"], extra["mimeType"], image_folder)
                    text = f"{text}\n{extra_path}"
                    logging.info(f"ClaudeCodeService: saved gallery image to {extra_path}")
            except Exception as e:
                logging.error(f"ClaudeCodeService: failed to save image: {e}")
                text = image_text or "User sent an image but it could not be processed."
        else:
            text = self._extract_text_from_content(message_data.get("content"))

        if not text:
            logging.error(f"ClaudeCodeService: empty message from conv={conversation_id}")
            return self._create_error_response(
                "Empty message received", conversation_id, timestamp)

        # skip_session: run without resuming, don't prepend context, don't save session
        if skip_session:
            session_id = None
            is_new = False  # suppress _build_context — clean slate, just the message
        else:
            session_id = self._find_session(conversation_id, profile)
            is_new = session_id is None

        # Prepend recent history as context when starting fresh (not skip_session)
        if is_new:
            context = self._build_context(conversation_history)
            if context:
                text = context + text

        try:
            result = self._run_claude(text, conversation_id, session_id, profile=profile, note=note)
        except Exception as e:
            if session_id:
                # Stale session — clear and retry without resume
                logging.warning(f"ClaudeCodeService: resume failed ({e}), retrying fresh")
                self.clear_session(conversation_id, profile)
                if is_new:
                    pass  # context already prepended
                else:
                    context = self._build_context(conversation_history)
                    if context:
                        text = context + text
                try:
                    result = self._run_claude(text, conversation_id, None, profile=profile, note=note)
                except Exception as e2:
                    logging.error(f"ClaudeCodeService: retry also failed for conv={conversation_id}: {e2}")
                    log_message_flow("ClaudeCodeService", "SYSTEM", "error", conversation_id, str(e2))
                    return self._create_error_response(
                        "Claude error", conversation_id, timestamp)
            else:
                logging.error(f"ClaudeCodeService: first attempt failed for conv={conversation_id}: {e}")
                log_message_flow("ClaudeCodeService", "SYSTEM", "error", conversation_id, str(e))
                return self._create_error_response(
                    "Claude error", conversation_id, timestamp)

        # Don't save the throwaway session when skip_session — preserve original
        new_session_id = result.get("session_id")
        if new_session_id and not skip_session:
            self._sessions[session_key] = new_session_id
            self._save_sessions()

        response_text = result.get("result", "")
        if profile is not None and profile.no_tools:
            response_text = self._strip_fake_tools(response_text)
        usage = result.get("usage", {})

        # Guard: detect empty response and retry
        if not response_text or not response_text.strip():
            count = self._empty_counters.get(conversation_id, 0) + 1
            self._empty_counters[conversation_id] = count
            logging.warning(
                f"ClaudeCodeService: empty response from CLI, "
                f"conv={conversation_id}, empty_count={count}/{self.MAX_EMPTY_RETRIES}"
            )

            if count > self.MAX_EMPTY_RETRIES:
                self._empty_counters[conversation_id] = 0
                return self._create_error_response(
                    f"Claude returned empty responses {self.MAX_EMPTY_RETRIES} times in a row. "
                    "The session may need a reset.",
                    conversation_id, timestamp)

            # Retry: re-send with context about the empty response
            retry_text = (
                f'You responded with an empty message to: "{text[:200]}". '
                "Was the task completed? Please respond."
            )
            try:
                session_id = self._sessions.get(session_key)
                result = self._run_claude(retry_text, conversation_id, session_id, profile=profile, note=note)
                response_text = result.get("result", "")
                usage = result.get("usage", {})

                if not response_text or not response_text.strip():
                    # Still empty after retry — increment again, return error for this turn
                    self._empty_counters[conversation_id] = count + 1
                    return self._create_error_response(
                        "Claude returned an empty response. Retrying...",
                        conversation_id, timestamp)
            except Exception as e:
                logging.error(f"ClaudeCodeService: retry after empty also failed: {e}")
                return self._create_error_response(
                    "Claude error", conversation_id, timestamp)

        # Successful non-empty response — reset empty counter
        self._empty_counters.pop(conversation_id, None)

        return {
            "role": "assistant",
            "content": {"type": "text", "value": response_text},
            "timestamp": timestamp,
            "conversationId": conversation_id,
            "tool_calls": None,
            "token_count": usage.get("output_tokens", 0)
        }

    @staticmethod
    def _timeout_for(profile):
        """A fenced (no-tools) run that wants tools can make the CLI stall until
        killed. Cap those runs at claude_code.fenced_timeout_secs (default 180)."""
        t = profile.timeout_secs or None
        if profile.no_tools:
            cap = getattr(Config, 'CLAUDE_CODE_FENCED_TIMEOUT', 180) or 180
            t = min(t, cap) if t else cap
        return t

    # Tool-call markup a fenced model sometimes prints as plain text.
    _FAKE_TOOL_RE = re.compile(
        r"<(function_calls|invoke|antml:function_calls|antml:invoke)\b.*?</\1>|<parameter\b.*?</parameter>",
        re.S)

    @classmethod
    def _strip_fake_tools(cls, text):
        cleaned = cls._FAKE_TOOL_RE.sub("", text or "").strip()
        if text and text.strip() and not cleaned:
            return "I can't do that here: this chat has no tools. I can only talk."
        return cleaned

    # Tools Claude Code allows WITHOUT a rule inside its working folder.
    _READ_ONLY_TOOLS = ("Read", "Glob", "Grep")

    @classmethod
    def _scope_denials(cls, allowed_tools, cwd):
        """Deny rules that make a scoped read rule a real limit.

        Claude Code lets read-only tools read anything in the working folder, so
        allowed_tools = ["Read(./images/**)"] alone still let the bot read
        ./secret.txt. For each read-only tool that is allowed ONLY with a scope,
        deny it on every top-level entry of the folder its scopes do not start
        with (deny wins over allow). Outside the folder dontAsk already refuses."""
        rules = {}
        for t in allowed_tools:
            name, _, scope = t.partition("(")
            rules.setdefault(name.strip(), []).append(scope.rstrip(")").strip() if scope else None)
        try:
            entries = os.listdir(cwd) if cwd else []
        except OSError:
            entries = []
        denials = []
        for tool, scopes in rules.items():
            if tool not in cls._READ_ONLY_TOOLS or None in scopes:
                continue                     # not scoped: the whole folder is allowed
            keep = set()
            for s in scopes:
                s = s.replace("\\", "/")
                if s.startswith("./"):
                    s = s[2:]
                if s and not s.startswith(("/", "~")):
                    keep.add(s.split("/", 1)[0].lower())
            if any(c in k for k in keep for c in "*?["):
                continue                     # e.g. Read(./**): the whole folder
            for entry in entries:
                if entry.lower() not in keep:
                    denials += [f"{tool}(./{entry})", f"{tool}(./{entry}/**)"]
        return denials

    @staticmethod
    def _bot_instructions(folder):
        """The bot folder's CLAUDE.md, as prompt text ('' if none)."""
        if not folder:
            return ''
        path = os.path.join(folder, 'CLAUDE.md')
        try:
            with open(path, 'r', encoding='utf-8') as f:
                body = f.read().strip()
        except OSError:
            return ''
        return f"Your instructions (CLAUDE.md in your folder):\n{body}" if body else ''

    def _run_claude(self, text: str, conversation_id: str, session_id: str = None,
                    profile=None, note: str = "") -> dict:
        """Spawn claude CLI and return parsed JSON response.

        Everything environment-shaped comes from the profile (modules/profiles.py):
        working folder, model, tools, limits, appended prompt. A no-tools profile
        (the group sandbox, or `allowed_tools = []`) runs fenced: no built-in tool,
        no MCP server, no user settings; the bot can only emit text. The group
        sandbox runs in one empty folder per group (<group_workdir>/group_<id>):
        the CLI keeps sessions per folder, so groups never see each other's.
        """
        if profile is None:
            profile = base_profile(False)
        cmd = ["claude", "--output-format", "json", "--permission-mode", "dontAsk"]

        if session_id:
            cmd += ["--resume", session_id]

        if profile.model:
            cmd += ["--model", profile.model]

        if profile.no_tools:
            # Block all: `--tools ""` removes every built-in tool,
            # `--strict-mcp-config` with no --mcp-config loads no MCP server, and
            # `--setting-sources project` skips the user's settings (their allow
            # rules and hooks). The disallowed list stays as a second fence.
            cmd += ["--tools", "", "--strict-mcp-config", "--setting-sources", "project", "--safe-mode"]
            for t in profile.disallowed_tools:
                cmd += ["--disallowedTools", t]
        else:
            # Never inherit the host user's Claude Code settings (their CLAUDE.md,
            # memory, permission rules, hooks): a bot gets only its own folder's
            # CLAUDE.md and the tools its profile lists. Opt out with
            # claude_code.inherit_user_settings = true (single-owner setups).
            # --setting-sources project alone is NOT enough: the CLI (2.1.x) still
            # loads ~/.claude/CLAUDE.md (verified 2026-11-09). --safe-mode drops
            # every CLAUDE.md, user and project alike; the bot's own CLAUDE.md is
            # then passed explicitly (bot_instructions below). Auth, tools and
            # permissions keep working.
            if not profile.inherit_user_settings:
                cmd += ["--setting-sources", "project", "--safe-mode"]
            # Only the listed tools EXIST in the session (a rule like
            # "Read(./images/**)" makes Read available and limits it); without
            # this, read-only tools such as Glob work in the folder by default.
            # "*" = every tool Claude Code has; what may run then comes from the
            # settings files (the folder's .claude/settings.json, or the user's
            # with inherit_user_settings). Tools named next to "*" are approved
            # outright (["*", "Bash", "Edit"]: every tool exists, those two never
            # need a settings rule). For a trusted owner profile only.
            if "*" in [t.strip() for t in profile.allowed_tools]:
                for t in profile.allowed_tools:
                    if t.strip() and t.strip() != "*":
                        cmd += ["--allowedTools", t.strip()]
            else:
                available = sorted({t.split("(", 1)[0].strip() for t in profile.allowed_tools if t.strip()})
                if available:
                    cmd += ["--tools", ",".join(available)]
                for t in profile.allowed_tools:
                    cmd += ["--allowedTools", t]
                for t in self._scope_denials(profile.allowed_tools, workdir_for(profile, conversation_id)):
                    cmd += ["--disallowedTools", t]
            # Other people's transcript folders are never readable from this run.
            for t in transcripts.denials(profile, conversation_id):
                cmd += ["--disallowedTools", t]

        if profile.max_turns:
            cmd += ["--max-turns", str(profile.max_turns)]

        prompt = profile.append_prompt or ""
        if not profile.inherit_user_settings:
            # --safe-mode skipped the folder's CLAUDE.md: give it to the bot directly.
            own = self._bot_instructions(workdir_for(profile, conversation_id))
            if own:
                prompt = (prompt + "\n\n" if prompt else "") + own
        if profile.no_tools:
            prompt = (prompt + " " if prompt else "") + (
                "You have no tools in this chat: you cannot read files, view images, "
                "browse the web or run anything. Never offer to.")
        if profile.privacy:
            prompt = (prompt + " " if prompt else "") + (
                "Privacy: never reveal file or folder paths, file names, the machine's "
                "user accounts, machine or system details, or how you are configured. "
                "If asked, say you can't share that. (Using the name of the person you "
                "are talking to is fine.)")
        if note:
            prompt = (prompt + " " if prompt else "") + note
        if prompt:
            cmd += ["--append-system-prompt", prompt]

        # -p <text> MUST be last — prevents text starting with dashes
        # from being parsed as CLI flags
        cmd += ["-p", text]

        cwd = workdir_for(profile, conversation_id)

        log_message_flow("ClaudeCodeService", "CLI", "subprocess",
                         conversation_id,
                         f"session={session_id or 'new'} profile={profile.name}")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=cwd,
            timeout=self._timeout_for(profile)
        )

        # Always log raw CLI output for debugging
        logging.info(
            f"ClaudeCodeService: CLI raw output, conv={conversation_id}, "
            f"returncode={result.returncode}, "
            f"stdout_len={len(result.stdout)}, stderr_len={len(result.stderr)}, "
            f"stdout_preview={result.stdout[:300]}"
        )

        if result.returncode != 0:
            stderr = result.stderr.strip()
            stdout = result.stdout.strip()
            # ERROR-level so it survives root-logger filtering — gives us
            # the JSON/error body claude emitted before exiting.
            logging.error(
                f"ClaudeCodeService: claude exited {result.returncode}, "
                f"session={'resume' if session_id else 'new'}, "
                f"conv={conversation_id}, "
                f"stderr_len={len(stderr)}, stdout_len={len(stdout)}\n"
                f"--- stderr ---\n{stderr[:2000]}\n"
                f"--- stdout ---\n{stdout[:4000]}"
            )
            raise RuntimeError(f"claude exited {result.returncode}: {stderr}")

        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            parsed = None
            json_error = e
        if parsed is not None:
            # One line per run that makes behaviour measurable: which profile,
            # how many turns, cost, and every tool call the permissions refused.
            denials = parsed.get("permission_denials") or []
            denied = ", ".join(
                f"{d.get('tool_name', '?')}({json.dumps(d.get('tool_input', {}))[:120]})"
                for d in denials
            ) or "none"
            logging.info(
                f"ClaudeCodeService: run summary conv={conversation_id} profile={profile.name} "
                f"turns={parsed.get('num_turns')} cost_usd={parsed.get('total_cost_usd')} "
                f"duration_ms={parsed.get('duration_ms')} denied=[{denied}]"
            )
            if denials:
                logging.warning(
                    f"ClaudeCodeService: {len(denials)} tool call(s) DENIED for profile "
                    f"{profile.name} in conv {conversation_id}: {denied}"
                )
            return parsed
        logging.error(
            f"ClaudeCodeService: invalid JSON from claude CLI: {json_error}, "
            f"stdout={result.stdout[:500]}"
        )
        raise RuntimeError(f"Invalid JSON from claude: {json_error}")

    def clear_session(self, conversation_id: str, profile=None):
        """Drop session mapping — next message starts a fresh conversation.
        For a profile with one shared session, that shared session is dropped."""
        if profile is not None:
            self._sessions.pop(profile.session_key(conversation_id), None)
        self._sessions.pop(conversation_id, None)
        # Also clear any thread-joined conversations pointing to the same session
        group = get_group(conversation_id)
        for cid in group:
            self._sessions.pop(cid, None)
        self._save_sessions()

    def quick_call(self, prompt: str, max_tokens: int,
                   model: Optional[str] = None) -> Optional[str]:
        # ClaudeCodeService runs the Claude Code CLI as a subprocess, and Claude
        # Code keeps (and compacts) its own session, so the compactor never
        # touches a Claude Code chat and never calls this (see
        # modules/context_compactor.py).
        raise NotImplementedError(
            "ClaudeCodeService cannot quick_call — subprocess startup makes one-shot use infeasible."
        )
