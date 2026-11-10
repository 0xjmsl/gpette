import sqlite3
import struct
import os
import time

import numpy as np

from config import Config

_DB_PATH = os.path.join(Config.DATA_DIR, "memory.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    text TEXT NOT NULL,
    embedding BLOB NOT NULL,
    start_line INTEGER,
    end_line INTEGER,
    content_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS embedding_cache (
    content_hash TEXT PRIMARY KEY,
    embedding BLOB NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_state (
    source_id TEXT PRIMARY KEY,
    last_hash TEXT NOT NULL,
    last_indexed INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_source_id ON chunks(source_id);
CREATE INDEX IF NOT EXISTS idx_chunks_content_hash ON chunks(content_hash);
"""


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    with _get_conn() as conn:
        conn.executescript(_SCHEMA)


# Initialize on import
_init_db()


def _embedding_to_blob(embedding: list[float]) -> bytes:
    """Pack float list into compact binary blob."""
    return struct.pack(f"{len(embedding)}f", *embedding)


def _blob_to_embedding(blob: bytes) -> np.ndarray:
    """Unpack binary blob to numpy array."""
    n = len(blob) // 4  # 4 bytes per float32
    return np.array(struct.unpack(f"{n}f", blob), dtype=np.float32)


def search(query_embedding: list[float], max_results: int = 5, min_score: float = 0.35) -> list[dict]:
    """Search chunks by cosine similarity to query embedding."""
    query_vec = np.array(query_embedding, dtype=np.float32)
    query_norm = np.linalg.norm(query_vec)
    if query_norm == 0:
        return []
    query_vec = query_vec / query_norm

    with _get_conn() as conn:
        rows = conn.execute("SELECT id, source, source_id, text, embedding, start_line, end_line, created_at FROM chunks").fetchall()

    if not rows:
        return []

    # Vectorized cosine similarity
    embeddings = np.array([_blob_to_embedding(row["embedding"]) for row in rows])
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    norms[norms == 0] = 1  # avoid div-by-zero
    embeddings = embeddings / norms

    scores = embeddings @ query_vec  # dot product with unit vectors = cosine sim

    # Filter and sort
    results = []
    for i, score in enumerate(scores):
        if score >= min_score:
            row = rows[i]
            results.append({
                "id": row["id"],
                "source": row["source"],
                "source_id": row["source_id"],
                "text": row["text"],
                "score": float(score),
                "start_line": row["start_line"],
                "end_line": row["end_line"],
                "created_at": row["created_at"],
            })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:max_results]


def store_chunks(source: str, source_id: str, chunks_with_embeddings: list[dict]):
    """Store chunks with their embeddings. Replaces existing chunks for this source_id."""
    now = int(time.time())
    with _get_conn() as conn:
        # Remove old chunks for this source
        conn.execute("DELETE FROM chunks WHERE source_id = ?", (source_id,))

        for chunk in chunks_with_embeddings:
            chunk_id = f"{source_id}:{chunk['hash']}"
            conn.execute(
                "INSERT OR REPLACE INTO chunks (id, source, source_id, text, embedding, start_line, end_line, content_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (chunk_id, source, source_id, chunk["text"], _embedding_to_blob(chunk["embedding"]), chunk.get("start_line"), chunk.get("end_line"), chunk["hash"], now),
            )

        # Update sync state
        combined_hash = ":".join(c["hash"] for c in chunks_with_embeddings)
        conn.execute(
            "INSERT OR REPLACE INTO sync_state (source_id, last_hash, last_indexed) VALUES (?, ?, ?)",
            (source_id, combined_hash, now),
        )


def get_sync_state(source_id: str) -> dict | None:
    """Get the sync state for a source_id."""
    with _get_conn() as conn:
        row = conn.execute("SELECT last_hash, last_indexed FROM sync_state WHERE source_id = ?", (source_id,)).fetchone()
    if row:
        return {"last_hash": row["last_hash"], "last_indexed": row["last_indexed"]}
    return None


def get_cached_embedding(content_hash: str) -> list[float] | None:
    """Check embedding cache by content hash."""
    with _get_conn() as conn:
        row = conn.execute("SELECT embedding FROM embedding_cache WHERE content_hash = ?", (content_hash,)).fetchone()
    if row:
        return _blob_to_embedding(row["embedding"]).tolist()
    return None


def cache_embedding(content_hash: str, embedding: list[float], provider: str, model: str):
    """Cache an embedding for a content hash."""
    now = int(time.time())
    with _get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO embedding_cache (content_hash, embedding, provider, model, created_at) VALUES (?, ?, ?, ?, ?)",
            (content_hash, _embedding_to_blob(embedding), provider, model, now),
        )
