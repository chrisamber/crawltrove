"""Turn a structured corpus RAG record into supervised fine-tuning pairs.

Two sources of pairs:
- deterministic_pairs(): free, high-precision facts pulled straight from the
  record's availability/deprecation/beta fields. No LLM.
- conceptual_pairs(): grounded usage/explanation Q&A from the local LLM.
"""
from __future__ import annotations

from typing import Any, Dict, List

from app import extract_llm


def _symbol(record: Dict[str, Any]) -> str:
    return (record.get("symbol") or record.get("title") or "").strip()


def deterministic_pairs(record: Dict[str, Any]) -> List[Dict[str, str]]:
    symbol = _symbol(record)
    if not symbol:
        return []
    avail = record.get("availability") or {}
    pairs: List[Dict[str, str]] = []

    introduced = avail.get("introduced")
    if introduced:
        pairs.append({
            "kind": "availability",
            "question": f"When was {symbol} introduced?",
            "answer": f"{symbol} was introduced in {introduced}.",
        })
    if avail.get("deprecated"):
        pairs.append({
            "kind": "deprecation",
            "question": f"Is {symbol} deprecated?",
            "answer": (f"Yes — {symbol} is marked deprecated. Prefer a "
                       f"non-deprecated replacement where one exists."),
        })
    if avail.get("beta"):
        pairs.append({
            "kind": "availability",
            "question": f"Is {symbol} a beta (prerelease) API?",
            "answer": (f"Yes — {symbol} is currently marked beta/prerelease. "
                       f"Its interface may change before final release."),
        })
    return pairs


PAIRS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "pairs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "answer": {"type": "string"},
                },
                "required": ["question", "answer"],
            },
        }
    },
    "required": ["pairs"],
}

GEN_PROMPT = (
    "The text above is a single Swift/Apple API reference card. Write {n} "
    "self-contained question/answer pairs a Swift developer would realistically "
    "ask about it. Use ONLY facts present in the card. Preserve every version, "
    "availability, deprecation, and concurrency detail exactly. Never invent "
    "APIs, parameters, or version numbers."
)


async def conceptual_pairs(record: Dict[str, Any], n: int = 2, *,
                           extractor=None) -> List[Dict[str, str]]:
    extractor = extractor or extract_llm.extract
    text = (record.get("text") or "").strip()
    if not text:
        return []
    result = await extractor(
        markdown=text,
        url=record.get("url", ""),
        schema=PAIRS_SCHEMA,
        prompt=GEN_PROMPT.format(n=n),
        temperature=0,
        seed=7,
    )
    raw = (result.get("data") or {}).get("pairs") or []
    out: List[Dict[str, str]] = []
    for p in raw:
        q, a = (p.get("question") or "").strip(), (p.get("answer") or "").strip()
        if q and a:
            out.append({"kind": "conceptual", "question": q, "answer": a})
    return out


async def generate_pairs(record: Dict[str, Any], *, n: int = 2,
                         extractor=None) -> Dict[str, Any]:
    det = deterministic_pairs(record)
    llm_skipped = False
    try:
        concept = await conceptual_pairs(record, n=n, extractor=extractor)
    except Exception:
        concept = []
        llm_skipped = True
    return {"pairs": det + concept, "llm_skipped": llm_skipped}
