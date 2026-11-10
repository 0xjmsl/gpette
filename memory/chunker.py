import hashlib


def chunk_text(text: str, max_tokens: int = 400, overlap_tokens: int = 80) -> list[dict]:
    """Split text into overlapping chunks with metadata.

    Uses char-based estimation: 1 token ~ 4 chars.
    Returns list of {text, start_line, end_line, hash}.
    """
    max_chars = max_tokens * 4
    overlap_chars = overlap_tokens * 4
    lines = text.split("\n")

    chunks = []
    current_chars = 0
    current_lines = []
    start_line = 0

    for i, line in enumerate(lines):
        line_len = len(line) + 1  # +1 for newline
        current_lines.append(line)
        current_chars += line_len

        if current_chars >= max_chars:
            chunk_text_str = "\n".join(current_lines)
            chunks.append({
                "text": chunk_text_str,
                "start_line": start_line,
                "end_line": i,
                "hash": hashlib.sha256(chunk_text_str.encode()).hexdigest()[:16],
            })

            # Overlap: carry last N chars worth of lines into next chunk
            overlap_lines = []
            overlap_len = 0
            for ol in reversed(current_lines):
                overlap_len += len(ol) + 1
                overlap_lines.insert(0, ol)
                if overlap_len >= overlap_chars:
                    break

            current_lines = overlap_lines
            current_chars = sum(len(l) + 1 for l in current_lines)
            start_line = i - len(overlap_lines) + 1

    # Remaining text
    if current_lines:
        chunk_text_str = "\n".join(current_lines)
        chunks.append({
            "text": chunk_text_str,
            "start_line": start_line,
            "end_line": len(lines) - 1,
            "hash": hashlib.sha256(chunk_text_str.encode()).hexdigest()[:16],
        })

    return chunks
