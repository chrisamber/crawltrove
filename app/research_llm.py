"""LLM steps for research runs: plan, select, take notes, assess, synthesize.

Every call goes through extract_llm.extract — the same local-preferred backend
waterfall as /api/extract. There is no separate LLM client (spec P0.5). Each
step is one schema-constrained call with retry + backoff; a step that exhausts
its retries raises, and the ResearchManager turns that into a failed run with
a partial report.
"""
import asyncio
import logging
import re
from typing import Any, Dict, List

from app import extract_llm

logger = logging.getLogger("research_llm")

RETRIES = 2
BACKOFF_BASE = 2.0

_STR_ARRAY = {"type": "array", "items": {"type": "string"}}
PLAN_SCHEMA = {"type": "object", "properties": {"queries": _STR_ARRAY},
               "required": ["queries"]}
SELECT_SCHEMA = {"type": "object", "properties": {"urls": _STR_ARRAY},
                 "required": ["urls"]}
NOTES_SCHEMA = {"type": "object", "properties": {
    "relevant": {"type": "boolean"},
    "notes": {"type": "string"},
    "key_facts": _STR_ARRAY,
}, "required": ["relevant", "notes", "key_facts"]}
ASSESS_SCHEMA = {"type": "object", "properties": {
    "enough": {"type": "boolean"},
    "reasoning": {"type": "string"},
    "new_queries": _STR_ARRAY,
}, "required": ["enough", "reasoning", "new_queries"]}
SYNTH_SCHEMA = {"type": "object", "properties": {
    "report_markdown": {"type": "string"},
    "insufficient": {"type": "boolean"},
}, "required": ["report_markdown", "insufficient"]}

_CITATION_RE = re.compile(r"\[(\d+)\]")


async def _call(doc: str, url: str, schema: Dict[str, Any], prompt: str) -> Dict[str, Any]:
    last: Exception = RuntimeError("unreachable")
    for attempt in range(RETRIES + 1):
        try:
            out = await extract_llm.extract(doc, url, schema, prompt=prompt)
            return out["data"]
        except Exception as e:
            last = e
            logger.warning("research llm call failed (attempt %d/%d): %s",
                           attempt + 1, RETRIES + 1, e)
            if attempt < RETRIES:
                await asyncio.sleep(BACKOFF_BASE * (attempt + 1))
    raise last


async def plan_queries(question: str, max_queries: int = 4) -> List[str]:
    data = await _call(
        f"Research question: {question}", "research://plan", PLAN_SCHEMA,
        f"You are planning autonomous web research. Produce up to {max_queries} "
        "distinct web search queries that together cover the research question "
        "from different angles. Return them in the queries array.")
    return [q.strip() for q in data.get("queries", []) if q.strip()][:max_queries]


async def select_urls(question: str, candidates: List[Dict[str, str]],
                      k: int) -> List[str]:
    listing = "\n".join(
        f"- {c['url']}\n  title: {c.get('title', '')}\n  snippet: {c.get('snippet', '')}"
        for c in candidates)
    data = await _call(
        f"Research question: {question}\n\nCandidate search results:\n{listing}",
        "research://select", SELECT_SCHEMA,
        f"Pick up to {k} candidate URLs most likely to help answer the research "
        "question. Prefer primary/authoritative sources over aggregators. Return "
        "the exact URLs from the candidate list in the urls array.")
    allowed = {c["url"] for c in candidates}
    return [u for u in data.get("urls", []) if u in allowed][:k]


async def take_notes(question: str, markdown: str, url: str) -> Dict[str, Any]:
    return await _call(
        markdown, url, NOTES_SCHEMA,
        f"Research question: {question}\n\nRead this page. Set relevant=false if "
        "it does not help answer the question. Otherwise write dense notes on "
        "what it contributes, and list the key facts (with numbers/names/versions "
        "verbatim) in key_facts. Only use information present in the page.")


async def assess(question: str, sources: List[Dict[str, Any]],
                 rounds_left: int) -> Dict[str, Any]:
    return await _call(
        f"Research question: {question}\n\nNotes gathered so far:\n{_sources_doc(sources)}",
        "research://assess", ASSESS_SCHEMA,
        f"You have {rounds_left} search rounds left. Decide whether the gathered "
        "notes are enough to answer the research question well. If enough, set "
        "enough=true. If not, set enough=false and propose up to 3 NEW search "
        "queries (different from angles already covered) in new_queries. Explain "
        "briefly in reasoning.")


async def synthesize(question: str, sources: List[Dict[str, Any]]) -> Dict[str, Any]:
    return await _call(
        f"Research question: {question}\n\nSource notes:\n{_sources_doc(sources)}",
        "research://synthesize", SYNTH_SCHEMA,
        "Write a well-structured Markdown research report answering the question "
        "from the source notes ONLY. Cite sources inline as [n] using the [n] "
        "indices shown. Every substantive claim needs at least one citation. If "
        "the notes cannot support a real answer, set insufficient=true and say "
        "what is missing instead of guessing.")


def _sources_doc(sources: List[Dict[str, Any]]) -> str:
    parts = []
    for s in sources:
        facts = "; ".join(s.get("key_facts") or [])
        parts.append(f"[{s['index']}] {s.get('title', '')} — {s['url']}\n"
                     f"Notes: {s.get('notes', '')}\nKey facts: {facts}")
    return "\n\n".join(parts) or "(no sources gathered)"


def validate_citations(report_md: str, n_sources: int) -> List[int]:
    """Cited indices in the report with no matching gathered source."""
    cited = {int(m) for m in _CITATION_RE.findall(report_md)}
    return sorted(i for i in cited if i < 1 or i > n_sources)
