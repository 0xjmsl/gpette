import hashlib
import threading
import datetime
import logging

from config import Config
from memory import embeddings, chunker, store

logger = logging.getLogger(__name__)


class MemoryManager:
    """Orchestrator for the semantic memory system.

    Two public methods:
    - search(query) -> formatted string for system prompt injection, or None
    - index_conversation(conversation_id, messages) -> None (runs in background)
    """

    def __init__(self):
        self._message_counts = {}  # conversation_id -> messages seen since last index

    def search(self, query: str, conversation_id: str = None, max_results: int = None) -> str | None:
        """Search memory for relevant past context.

        Returns a formatted string ready for system prompt injection, or None.
        """
        if not query or not query.strip():
            return None

        max_results = max_results or Config.MEMORY_MAX_RESULTS
        min_score = Config.MEMORY_MIN_SCORE

        try:
            query_embedding = embeddings.get_embedding(query)
            results = store.search(query_embedding, max_results=max_results, min_score=min_score)

            if not results:
                return None

            # Filter out results from the current conversation
            if conversation_id:
                results = [r for r in results if r["source_id"] != conversation_id]

            if not results:
                return None

            return self._format_results(results)
        except Exception as e:
            logger.warning(f"Memory search failed: {e}")
            return None

    def index_conversation(self, conversation_id: str, messages: list):
        """Index conversation messages for future recall. Runs in background thread.

        Debounces: only indexes every N messages (Config.MEMORY_INDEX_DEBOUNCE).
        """
        # Debounce
        count = self._message_counts.get(conversation_id, 0) + 1
        self._message_counts[conversation_id] = count

        if count % Config.MEMORY_INDEX_DEBOUNCE != 0:
            return

        # Check if content has changed
        text = self._messages_to_text(messages)
        if not text.strip():
            return

        content_hash = hashlib.sha256(text.encode()).hexdigest()[:32]
        sync = store.get_sync_state(conversation_id)
        if sync and sync["last_hash"].startswith(content_hash[:16]):
            return  # Already indexed this version

        # Run in background thread
        thread = threading.Thread(
            target=self._index_worker,
            args=(conversation_id, text),
            daemon=True,
        )
        thread.start()

    def _index_worker(self, conversation_id: str, text: str):
        """Background worker: chunk, embed, store."""
        try:
            chunks = chunker.chunk_text(text, max_tokens=Config.MEMORY_CHUNK_TOKENS)
            if not chunks:
                return

            # Check cache first, embed only uncached chunks
            to_embed = []
            for chunk in chunks:
                cached = store.get_cached_embedding(chunk["hash"])
                if cached:
                    chunk["embedding"] = cached
                else:
                    to_embed.append(chunk)

            if to_embed:
                texts = [c["text"] for c in to_embed]
                embs = embeddings.get_embeddings(texts)
                for chunk, emb in zip(to_embed, embs):
                    chunk["embedding"] = emb
                    store.cache_embedding(
                        chunk["hash"], emb,
                        Config.MEMORY_EMBEDDING_PROVIDER,
                        Config.MEMORY_EMBEDDING_MODEL,
                    )

            store.store_chunks("conversation", conversation_id, chunks)
            logger.info(f"Memory: indexed {len(chunks)} chunks for conversation {conversation_id}")
        except Exception as e:
            logger.warning(f"Memory indexing failed for {conversation_id}: {e}")

    def _messages_to_text(self, messages: list) -> str:
        """Convert conversation messages to plain text for chunking.

        Only includes user and assistant messages (skips tool, system).
        """
        lines = []
        for msg in messages:
            role = msg.get("role", "")
            if role not in ("user", "assistant"):
                continue

            content = msg.get("content", "")
            if isinstance(content, dict):
                content = content.get("value", "")
            if not content or not isinstance(content, str):
                continue

            # Add timestamp if available
            ts = msg.get("timestamp")
            if ts:
                try:
                    if ts > 1e12:  # milliseconds
                        ts = ts / 1000
                    dt = datetime.datetime.fromtimestamp(ts)
                    lines.append(f"[{dt.strftime('%Y-%m-%d %H:%M')}] {role}: {content}")
                except (ValueError, OSError):
                    lines.append(f"{role}: {content}")
            else:
                lines.append(f"{role}: {content}")

        return "\n".join(lines)

    def _format_results(self, results: list[dict]) -> str:
        """Format search results for system prompt injection."""
        items = []
        for r in results:
            # Extract a preview (first ~200 chars)
            text = r["text"].strip()
            if len(text) > 200:
                text = text[:200] + "..."

            # Format timestamp
            ts = r.get("created_at", 0)
            try:
                dt = datetime.datetime.fromtimestamp(ts)
                date_str = dt.strftime("%Y-%m-%d")
            except (ValueError, OSError):
                date_str = "unknown"

            items.append(f'  [{date_str}] "{text}"')

        block = "\n".join(items)
        return (
            f"\n---\nRELEVANT MEMORY (from past conversations):\n"
            f"{block}\n---\n"
        )
