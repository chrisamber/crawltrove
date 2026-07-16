"""Corpus quality heuristics: Gopher rules + FineWeb additions.

These are the exact heuristic filters used by the MassiveText/Gopher pipeline
(as implemented in HuggingFace datatrove) plus the three custom rules FineWeb
added on top. Documents are scored, not dropped — every scrape gets a quality
report in its metadata and the corpus consumer decides what to keep.
"""
import re
from collections import Counter
from typing import Any, Dict, List

STOP_WORDS = ("the", "be", "to", "of", "and", "that", "have", "with")
BULLET_CHARS = ("-", "*", "•", "‣", "▪")

# Gopher thresholds (datatrove defaults)
MIN_WORDS, MAX_WORDS = 50, 100_000
MIN_AVG_WORD_LEN, MAX_AVG_WORD_LEN = 3, 10
MAX_SYMBOL_WORD_RATIO = 0.1
MAX_BULLET_LINE_FRACTION = 0.9
MAX_ELLIPSIS_LINE_FRACTION = 0.3
MIN_ALPHA_WORD_FRACTION = 0.8
MIN_STOP_WORDS = 2

# FineWeb additions
MIN_PUNCT_LINE_FRACTION = 0.12   # docs with fewer punctuated line-endings are junk
MAX_DUP_LINE_CHAR_FRACTION = 0.10
MAX_SHORT_LINE_FRACTION = 0.67   # fraction of lines under 30 chars
SHORT_LINE_CHARS = 30


def _plain_lines(markdown: str) -> List[str]:
    """Strip markdown structure so prose-level rules see prose, not syntax."""
    lines = []
    for line in markdown.splitlines():
        line = re.sub(r"^\s*#{1,6}\s*", "", line)          # headings
        line = re.sub(r"^\s*(?:[-*•‣▪]|\d+\.)\s+", "", line)  # list markers
        line = re.sub(r"^\s*>\s*", "", line)                # blockquotes
        line = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", line)  # images
        line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)   # links
        line = line.strip()
        if line:
            lines.append(line)
    return lines


def assess(markdown: str) -> Dict[str, Any]:
    """Run all heuristics. Returns {passed, failures, signals}."""
    raw_lines = [l.strip() for l in markdown.splitlines() if l.strip()]
    lines = _plain_lines(markdown)
    text = "\n".join(lines)
    words = text.split()
    n_words = len(words)

    failures: List[str] = []
    signals: Dict[str, Any] = {"n_words": n_words, "n_lines": len(lines)}

    if not words or not lines:
        return {"passed": False, "failures": ["empty"], "signals": signals}

    # --- Gopher rules ---
    if not (MIN_WORDS <= n_words <= MAX_WORDS):
        failures.append("word_count")

    avg_len = sum(len(w) for w in words) / n_words
    signals["avg_word_length"] = round(avg_len, 2)
    if not (MIN_AVG_WORD_LEN <= avg_len <= MAX_AVG_WORD_LEN):
        failures.append("avg_word_length")

    symbols = text.count("#") + text.count("…") + text.count("...")
    signals["symbol_word_ratio"] = round(symbols / n_words, 3)
    if signals["symbol_word_ratio"] > MAX_SYMBOL_WORD_RATIO:
        failures.append("symbol_ratio")

    bullet_frac = sum(
        1 for l in raw_lines if l.startswith(BULLET_CHARS)
    ) / len(raw_lines)
    signals["bullet_line_fraction"] = round(bullet_frac, 3)
    if bullet_frac > MAX_BULLET_LINE_FRACTION:
        failures.append("bullet_lines")

    ellipsis_frac = sum(
        1 for l in lines if l.endswith(("...", "…"))
    ) / len(lines)
    signals["ellipsis_line_fraction"] = round(ellipsis_frac, 3)
    if ellipsis_frac > MAX_ELLIPSIS_LINE_FRACTION:
        failures.append("ellipsis_lines")

    alpha_frac = sum(1 for w in words if re.search(r"[a-zA-Z]", w)) / n_words
    signals["alpha_word_fraction"] = round(alpha_frac, 3)
    if alpha_frac < MIN_ALPHA_WORD_FRACTION:
        failures.append("alpha_words")

    lowered_words = set(w.lower().strip(".,!?;:\"'()") for w in words)
    stop_hits = sum(1 for s in STOP_WORDS if s in lowered_words)
    signals["stop_word_hits"] = stop_hits
    if stop_hits < MIN_STOP_WORDS:
        failures.append("stop_words")

    # --- FineWeb rules ---
    punct_frac = sum(
        1 for l in lines if l.endswith((".", "!", "?", '"', "”"))
    ) / len(lines)
    signals["punct_line_fraction"] = round(punct_frac, 3)
    if punct_frac < MIN_PUNCT_LINE_FRACTION:
        failures.append("unpunctuated_lines")

    counts = Counter(lines)
    dup_chars = sum(len(l) * n for l, n in counts.items() if n > 1)
    total_chars = sum(len(l) for l in lines) or 1
    signals["dup_line_char_fraction"] = round(dup_chars / total_chars, 3)
    if signals["dup_line_char_fraction"] > MAX_DUP_LINE_CHAR_FRACTION:
        failures.append("duplicate_lines")

    short_frac = sum(1 for l in lines if len(l) < SHORT_LINE_CHARS) / len(lines)
    signals["short_line_fraction"] = round(short_frac, 3)
    if short_frac > MAX_SHORT_LINE_FRACTION:
        failures.append("short_lines")

    return {"passed": not failures, "failures": failures, "signals": signals}
