"""Structure-aware Markdown chunking for the corpus pipeline (Epic 3 S2).

Splits a page's Markdown on its heading hierarchy, **never splitting inside a
fenced code block**, and packs the resulting blocks into a target token window
with a small overlap. Pure function, no LLM — deterministic so chunk
``content_hash`` (and therefore record ``id``) is stable across rebuilds.

Token counts are approximated by whitespace word count: deterministic, backend-
free, and close enough for windowing. Each chunk carries a ``heading_path``
breadcrumb (the active heading stack) so a retriever can show where it came from.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterator, List, Optional, Tuple

# Window defaults (word-count proxy for tokens; ~200-1200 token range).
TARGET_TOKENS = 250
MAX_TOKENS = 900
MIN_TOKENS = 40
OVERLAP_TOKENS = 40

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")


def _count_tokens(text: str) -> int:
    return len(text.split())


def _iter_blocks(markdown: str) -> Iterator[Tuple[str, str, Optional[int]]]:
    """Yield (kind, text, level) blocks. kind ∈ {heading, code, para}.

    A fenced code block is emitted whole (kind=code) so it is never split; its
    closing fence must match the opening fence character and be at least as long.
    """
    lines = markdown.splitlines()
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        fm = _FENCE.match(line)
        if fm:
            fence_char = fm.group(2)[0]
            fence_len = len(fm.group(2))
            close = re.compile(r"^\s*" + re.escape(fence_char) + "{" + str(fence_len) + r",}\s*$")
            buf = [line]
            i += 1
            while i < n and not close.match(lines[i]):
                buf.append(lines[i])
                i += 1
            if i < n:
                buf.append(lines[i])  # closing fence
                i += 1
            yield ("code", "\n".join(buf), None)
            continue
        hm = _HEADING.match(line)
        if hm:
            yield ("heading", hm.group(2).strip(), len(hm.group(1)))
            i += 1
            continue
        if not line.strip():
            i += 1
            continue
        # paragraph: consume until blank line / heading / code fence
        buf = [line]
        i += 1
        while i < n and lines[i].strip() and not _HEADING.match(lines[i]) and not _FENCE.match(lines[i]):
            buf.append(lines[i])
            i += 1
        yield ("para", "\n".join(buf), None)


def _tail_overlap(text: str, overlap_tokens: int) -> str:
    """Trailing ~overlap_tokens words of a chunk, for small inter-chunk overlap."""
    if overlap_tokens <= 0:
        return ""
    words = text.split()
    return " ".join(words[-overlap_tokens:]) if words else ""


def chunk_markdown(markdown: str, *, target_tokens: int = TARGET_TOKENS,
                   max_tokens: int = MAX_TOKENS, min_tokens: int = MIN_TOKENS,
                   overlap_tokens: int = OVERLAP_TOKENS) -> List[Dict[str, Any]]:
    """Chunk page Markdown into ``[{text, heading_path, chunk_index}]``.

    - Headings update a breadcrumb stack and are kept inline in the chunk text.
    - A chunk boundary is taken at a heading only once the current chunk already
      holds a full window (>= target), so tiny consecutive sections merge.
    - Code fences are atomic: a code block never straddles a boundary, even if it
      alone exceeds ``max_tokens``.
    - Consecutive chunks get a small word-tail overlap for retrieval recall.
    """
    markdown = markdown or ""
    heading_stack: List[Tuple[int, str]] = []
    chunks: List[Dict[str, Any]] = []
    cur_blocks: List[str] = []
    cur_tokens = 0
    cur_path: List[str] = []
    cur_has_body = False  # True once a para/code block joins the current chunk

    def path_now() -> List[str]:
        return [title for (_lvl, title) in heading_stack]

    def flush() -> None:
        nonlocal cur_blocks, cur_tokens, cur_has_body
        text = "\n\n".join(b for b in cur_blocks if b).strip()
        if text:
            chunks.append({"text": text, "heading_path": list(cur_path)})
        cur_blocks = []
        cur_tokens = 0
        cur_has_body = False

    for kind, text, level in _iter_blocks(markdown):
        if kind == "heading":
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, text))
            if cur_tokens >= target_tokens:
                flush()
            # Advance the breadcrumb through a run of headings with no body
            # between them, so the chunk's path reflects its deepest section.
            if not cur_has_body:
                cur_path = path_now()
            cur_blocks.append("#" * level + " " + text)
            cur_tokens += _count_tokens(text)
            continue

        toks = _count_tokens(text)
        if kind == "code" and toks > max_tokens:
            # Oversized code block: emit prior content, then the code alone.
            if cur_blocks:
                flush()
            cur_path = path_now()
            cur_blocks.append(text)
            cur_has_body = True
            flush()
            continue
        if cur_blocks and cur_tokens + toks > max_tokens:
            flush()
        if not cur_blocks:
            cur_path = path_now()
        cur_blocks.append(text)
        cur_tokens += toks
        cur_has_body = True
        if cur_tokens >= target_tokens:
            flush()

    flush()

    # Small inter-chunk overlap (deterministic; prepended to each chunk after
    # the first). Skipped when it would just duplicate a heading-only chunk.
    if overlap_tokens > 0:
        for idx in range(1, len(chunks)):
            tail = _tail_overlap(chunks[idx - 1]["text"], overlap_tokens)
            if tail and tail not in chunks[idx]["text"]:
                chunks[idx]["text"] = tail + "\n\n" + chunks[idx]["text"]

    for idx, c in enumerate(chunks):
        c["chunk_index"] = idx
    return chunks
