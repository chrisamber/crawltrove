#!/usr/bin/env python3
"""Crawl + scrape an Apple developer-documentation subtree into the corpus.

Apple's developer docs (developer.apple.com/documentation/...) are a pure
client-side SPA: a `curl` of the HTML page returns ~160 chars of shell with
none of the content, so CrawlTrove's tier-1 HTTP fetch gets nothing and the
tier-2 Playwright render is the only HTML path -- and the browser tier needs
Docker, which isn't always available.

Apple, however, publishes the *same* content as DocC "render JSON" behind a
stable, no-auth API:

    page:  https://developer.apple.com/documentation/avfaudio/audio-engine
    json:  https://developer.apple.com/tutorials/data/documentation/avfaudio/audio-engine.json

That JSON is complete and structured (abstract, declaration, discussion,
parameters, topic groups, cross-references), so it is both the most reliable
way to *enumerate* the related-doc graph and to *extract* clean content -- no
browser, no anti-bot fight.

This script BFS-walks that graph from a root collection, converts each node's
render JSON to Markdown, runs the page through CrawlTrove's own corpus signal
pipeline (license / quality / language / dedup), and persists the whole thing
as a single crawl artifact via app.storage.save_crawl -- identical in shape to
what POST /api/crawl produces, so it shows up at /artifacts like any crawl.

Usage (from repo root, with the project venv):

    .venv/bin/python scripts/scrape_apple_docs.py \
        --root https://developer.apple.com/documentation/avfaudio/audio-engine

    # validate the converter on a few pages without saving / hitting the corpus:
    .venv/bin/python scripts/scrape_apple_docs.py --preview 3
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

# Make `app` importable when run as scripts/scrape_apple_docs.py from repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from curl_cffi import requests as cffi_requests  # noqa: E402  (same client tier-1 uses)

HOST = "https://developer.apple.com"
DATA_PREFIX = "/tutorials/data"

# Block / inline render-node types we explicitly handle. Anything outside these
# is still rendered best-effort, but recorded in UNKNOWN_TYPES so a silent
# format drift surfaces in the run report instead of quietly losing content.
UNKNOWN_TYPES: Counter = Counter()


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #
def doc_url_to_json_url(doc_path: str) -> str:
    """'/documentation/avfaudio/audio-engine' -> full render-JSON URL."""
    if doc_path.startswith("http"):
        doc_path = urlsplit(doc_path).path
    doc_path = "/" + doc_path.strip("/")
    return f"{HOST}{DATA_PREFIX}{doc_path}.json"


def doc_url_to_page_url(doc_path: str) -> str:
    if doc_path.startswith("http"):
        return doc_path
    return f"{HOST}/{doc_path.strip('/')}"


def fetch_json(doc_path: str, retries: int = 3) -> Optional[Dict[str, Any]]:
    """Fetch one render-JSON document with Chrome TLS impersonation + retry."""
    url = doc_url_to_json_url(doc_path)
    last = None
    for attempt in range(retries):
        try:
            r = cffi_requests.get(url, impersonate="chrome", timeout=30)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}"
        except Exception as e:  # transport / json error
            last = repr(e)
        time.sleep(0.6 * (attempt + 1))
    print(f"    ! fetch failed {doc_path}: {last}", file=sys.stderr)
    return None


# --------------------------------------------------------------------------- #
# Render-JSON -> Markdown
# --------------------------------------------------------------------------- #
def _abs(url: str) -> str:
    if not url:
        return url
    if url.startswith("http"):
        return url
    if url.startswith("doc://"):
        # cross-framework refs come in two shapes:
        #   doc://com.apple.uikit/documentation/UIKit/UIAppearance  (path has /documentation/)
        #   doc://com.apple.documentation/metal/mtldrawable/...      (path omits it)
        rest = url[len("doc://"):].split("/", 1)
        path = rest[1] if len(rest) > 1 else ""
        if not path:
            return url
        if not path.startswith("documentation/"):
            path = "documentation/" + path
        return f"{HOST}/{path}"
    return f"{HOST}{url}"


def _name_from_ident(ident: str) -> str:
    """Readable symbol name from a doc:// identifier when the ref isn't resolvable."""
    return ident.rsplit("/", 1)[-1] if ident else ident


def render_inline(nodes: List[Dict[str, Any]], refs: Dict[str, Any]) -> str:
    out: List[str] = []
    for n in nodes or []:
        t = n.get("type")
        if t == "text":
            out.append(n.get("text", ""))
        elif t == "codeVoice":
            out.append(f"`{n.get('code', '')}`")
        elif t in ("emphasis", "newTerm"):
            out.append(f"*{render_inline(n.get('inlineContent', []), refs)}*")
        elif t in ("strong", "inlineHead"):
            out.append(f"**{render_inline(n.get('inlineContent', []), refs)}**")
        elif t == "reference":
            ident = n.get("identifier", "")
            ref = refs.get(ident, {})
            title = ref.get("title") or _name_from_ident(ident)
            url = _abs(ref.get("url", "") or ident)
            out.append(f"[{title}]({url})" if url else title)
        elif t == "link":
            out.append(f"[{n.get('title', n.get('text', ''))}]({_abs(n.get('destination', ''))})")
        elif t == "image":
            ref = refs.get(n.get("identifier", ""), {})
            variants = ref.get("variants", []) or ref.get("asset", {}).get("variants", [])
            src = _abs(variants[0].get("url", "")) if variants else ""
            out.append(f"![{ref.get('alt', '') or 'image'}]({src})" if src else "")
        elif t in ("subscript", "superscript"):
            out.append(render_inline(n.get("inlineContent", []), refs))
        else:
            UNKNOWN_TYPES[f"inline:{t}"] += 1
            if n.get("inlineContent"):
                out.append(render_inline(n["inlineContent"], refs))
            elif n.get("text"):
                out.append(n["text"])
    return "".join(out)


def render_blocks(blocks: List[Dict[str, Any]], refs: Dict[str, Any]) -> str:
    parts: List[str] = []
    for b in blocks or []:
        t = b.get("type")
        if t == "paragraph":
            parts.append(render_inline(b.get("inlineContent", []), refs))
        elif t == "heading":
            level = max(2, min(int(b.get("level", 2)), 6))
            parts.append("#" * level + " " + (b.get("text", "") or render_inline(b.get("inlineContent", []), refs)))
        elif t == "codeListing":
            lang = b.get("syntax") or ""
            code = "\n".join(b.get("code", []))
            parts.append(f"```{lang}\n{code}\n```")
        elif t in ("unorderedList", "orderedList"):
            marker = (lambda i: "- ") if t == "unorderedList" else (lambda i: f"{i}. ")
            lines = []
            for i, item in enumerate(b.get("items", []), 1):
                body = render_blocks(item.get("content", []), refs).strip()
                body = body.replace("\n\n", "\n").replace("\n", "\n  ")  # indent continuation
                lines.append(marker(i) + body)
            parts.append("\n".join(lines))
        elif t == "aside":
            name = (b.get("name") or b.get("style") or "Note").title()
            body = render_blocks(b.get("content", []), refs).strip()
            quoted = "\n".join("> " + ln for ln in body.splitlines())
            parts.append(f"> **{name}**\n{quoted}")
        elif t == "table":
            rows = b.get("rows", [])
            if rows:
                def cell(c):  # a cell is a list of blocks
                    return render_blocks(c, refs).replace("\n", " ").strip()
                header = [cell(c) for c in rows[0]]
                parts.append("| " + " | ".join(header) + " |")
                parts.append("| " + " | ".join("---" for _ in header) + " |")
                for row in rows[1:]:
                    parts.append("| " + " | ".join(cell(c) for c in row) + " |")
        elif t == "termList":
            for term in b.get("items", []):
                tname = render_inline(term.get("term", {}).get("inlineContent", []), refs)
                tdef = render_blocks(term.get("definition", {}).get("content", []), refs).strip()
                parts.append(f"**{tname}**\n: {tdef}")
        elif t == "links":
            for ident in b.get("items", []):
                ref = refs.get(ident, {})
                if ref.get("title"):
                    parts.append(f"- [{ref['title']}]({_abs(ref.get('url', ''))})")
        else:
            UNKNOWN_TYPES[f"block:{t}"] += 1
            if b.get("content"):
                parts.append(render_blocks(b["content"], refs))
            elif b.get("inlineContent"):
                parts.append(render_inline(b["inlineContent"], refs))
    return "\n\n".join(p for p in parts if p.strip())


def _topic_group(title: str, identifiers: List[str], refs: Dict[str, Any]) -> str:
    lines = [f"### {title}"] if title else []
    for ident in identifiers:
        ref = refs.get(ident, {})
        name = ref.get("title", ident)
        url = _abs(ref.get("url", ""))
        abstract = render_inline(ref.get("abstract", []), refs).strip()
        link = f"[{name}]({url})" if url else name
        lines.append(f"- {link}" + (f" — {abstract}" if abstract else ""))
    return "\n".join(lines)


def doc_to_markdown(doc: Dict[str, Any]) -> Tuple[str, str, str]:
    """Convert one render-JSON doc -> (title, abstract_text, markdown)."""
    refs = doc.get("references", {})
    meta = doc.get("metadata", {})
    title = meta.get("title", "")
    role = meta.get("roleHeading") or meta.get("role") or ""
    abstract = render_inline(doc.get("abstract", []), refs).strip()

    md: List[str] = []
    if role:
        md.append(f"*{role}*")

    # Availability line (platform / introduced-in), compact.
    plats = meta.get("platforms", []) or []
    avail = ", ".join(
        f"{p.get('name')} {p.get('introducedAt')}+" for p in plats if p.get("name") and p.get("introducedAt")
    )
    if avail:
        md.append(f"**Availability:** {avail}")

    if abstract:
        md.append(abstract)

    for sec in doc.get("primaryContentSections", []) or []:
        kind = sec.get("kind")
        if kind == "declarations":
            for decl in sec.get("declarations", []):
                tokens = "".join(tok.get("text", "") for tok in decl.get("tokens", []))
                langs = decl.get("languages", []) or []
                syntax = "swift" if (not langs or "swift" in langs) else (langs[0] if langs else "")
                if tokens.strip():
                    md.append(f"```{syntax}\n{tokens}\n```")
        elif kind == "parameters":
            params = sec.get("parameters", [])
            if params:
                md.append("### Parameters")
                for p in params:
                    pbody = render_blocks(p.get("content", []), refs).strip().replace("\n", " ")
                    md.append(f"- `{p.get('name', '')}` — {pbody}")
        elif kind == "content":
            body = render_blocks(sec.get("content", []), refs)
            if body.strip():
                md.append(body)
        elif kind == "mentions":
            continue  # "mentioned in" backlinks -- navigational, skip
        else:
            UNKNOWN_TYPES[f"primary:{kind}"] += 1

    # Topics (member symbols grouped) -> link lists.
    topic_sections = doc.get("topicSections", []) or []
    if topic_sections:
        md.append("## Topics")
        for grp in topic_sections:
            md.append(_topic_group(grp.get("title", ""), grp.get("identifiers", []), refs))

    rel = doc.get("relationshipsSections", []) or []
    if rel:
        md.append("## Relationships")
        for grp in rel:
            md.append(_topic_group(grp.get("title", ""), grp.get("identifiers", []), refs))

    see = doc.get("seeAlsoSections", []) or []
    if see:
        md.append("## See Also")
        for grp in see:
            md.append(_topic_group(grp.get("title", ""), grp.get("identifiers", []), refs))

    markdown = "\n\n".join(m for m in md if m and m.strip())
    return title, abstract, markdown


# --------------------------------------------------------------------------- #
# Graph walk
# --------------------------------------------------------------------------- #
def in_scope(url: str, scope_prefix: str) -> bool:
    """Keep the walk inside the target subtree; exclude the bare framework root
    and anything outside /documentation/<framework>/.

    Case-insensitive: Apple refs use lowercase paths even when the root URL is
    mixed-case (e.g. /documentation/AppIntents → refs are /documentation/appintents/).
    """
    if not url:
        return False
    path = urlsplit(url).path.rstrip("/").lower()
    prefix = scope_prefix.lower()
    return path.startswith(prefix) and path != prefix.rstrip("/")


def child_paths(doc: Dict[str, Any], is_root: bool, scope_prefix: str) -> List[str]:
    """URLs to recurse into from this doc.

    Everywhere: topicSections members (the true child/member hierarchy) +
    relationships (stay in-subtree via the scope filter).
    Root only: also every in-scope reference, so the collection's directly
    linked sample-code articles and related collection-groups are captured.
    """
    refs = doc.get("references", {})
    idents: List[str] = []
    for grp in doc.get("topicSections", []) or []:
        idents += grp.get("identifiers", [])
    for grp in doc.get("relationshipsSections", []) or []:
        idents += grp.get("identifiers", [])

    urls: List[str] = []
    for ident in idents:
        ref = refs.get(ident, {})
        if ref.get("type") == "topic" and in_scope(ref.get("url", ""), scope_prefix):
            urls.append(ref["url"])
    if is_root:
        for ref in refs.values():
            if ref.get("type") == "topic" and in_scope(ref.get("url", ""), scope_prefix):
                urls.append(ref["url"])
    # de-dupe, preserve order
    seen, out = set(), []
    for u in urls:
        key = u.rstrip("/")
        if key not in seen:
            seen.add(key)
            out.append(u)
    return out


def crawl_graph(root_path: str, scope_prefix: str, max_pages: int,
                max_depth: int, concurrency: int) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """BFS the render-JSON graph. Returns ({doc_path: render_json}, failed_paths)."""
    docs: Dict[str, Dict[str, Any]] = {}
    failed: List[str] = []
    seen = {root_path.rstrip("/")}
    frontier = [root_path]
    depth = 0
    capped = False
    while frontier and depth <= max_depth:
        wave = frontier
        frontier = []
        print(f"  depth {depth}: fetching {len(wave)} page(s) "
              f"(have {len(docs)})", file=sys.stderr)
        results: Dict[str, Optional[Dict[str, Any]]] = {}
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futs = {ex.submit(fetch_json, p): p for p in wave}
            for fut in as_completed(futs):
                results[futs[fut]] = fut.result()
        for p in wave:
            doc = results.get(p)
            if not doc:
                failed.append(p)  # never returned HTTP 200 after retries
                continue
            docs[p] = doc
            if len(docs) >= max_pages:
                capped = True
                break
            for child in child_paths(doc, is_root=(p == root_path), scope_prefix=scope_prefix):
                key = child.rstrip("/")
                if key not in seen:
                    seen.add(key)
                    frontier.append(child)
        if capped:
            print(f"  ! hit --max-pages={max_pages}; stopping "
                  f"({len(frontier)} queued URLs dropped)", file=sys.stderr)
            break
        depth += 1
    return docs, failed


# --------------------------------------------------------------------------- #
# Signal pipeline + persistence (mirror of WebScraper._build_document_result)
# --------------------------------------------------------------------------- #
def build_result(doc_path: str, doc: Dict[str, Any], run_signals: bool) -> Dict[str, Any]:
    from app import lang, license_detect, quality

    title, abstract, markdown = doc_to_markdown(doc)
    page_url = doc_url_to_page_url(doc_path)
    meta = doc.get("metadata", {})
    metadata: Dict[str, Any] = {
        "title": title,
        "description": abstract,
        "url": page_url,
        "engine": "http-json",            # honest: render-JSON API, no browser
        "extractor": "appledocs-docc",
        "roleHeading": meta.get("roleHeading", ""),
        "symbolKind": meta.get("symbolKind", ""),
        "platforms": [
            {"name": p.get("name"), "introducedAt": p.get("introducedAt")}
            for p in (meta.get("platforms", []) or [])
        ],
    }
    if run_signals:
        metadata["license"] = license_detect.detect_text(markdown)
        metadata["quality"] = quality.assess(markdown)
        metadata["language"] = lang.detect(markdown)
    return {
        "success": True,
        "url": page_url,
        "title": title,
        "description": abstract,
        "markdown": markdown,
        "html": "",
        "metadata": metadata,
    }


def write_artifact(job: Dict[str, Any], out: str = "") -> str:
    """Persist the crawl job and return its .json path.

    With `out`, write JSON to that exact path (deterministic — used by the
    parallel drain so concurrent scrapes don't race on artifact detection);
    otherwise auto-stem via storage.save_crawl (which also writes the .md
    sidecar and lists it under /artifacts).
    """
    from app import storage
    if out:
        p = Path(out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        return str(p)
    stem = storage.save_crawl(job)
    return os.path.join(storage.CRAWLS_DIR, f"{stem}.json")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="https://developer.apple.com/documentation/avfaudio/audio-engine")
    ap.add_argument("--scope", default="",
                    help="path prefix to stay within (default: /documentation/<framework>/)")
    ap.add_argument("--max-pages", type=int, default=2000)
    ap.add_argument("--max-depth", type=int, default=6)
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--preview", type=int, default=0,
                    help="convert N pages and print samples; no signals, no save")
    ap.add_argument("--out", default="",
                    help="write the crawl artifact JSON to this exact path "
                         "(deterministic; used by the parallel drain). "
                         "Default: auto-stem via storage.save_crawl.")
    args = ap.parse_args()

    root_path = urlsplit(args.root).path.rstrip("/")
    if args.scope:
        scope_prefix = args.scope.lower()
    else:
        # /documentation/<framework>/  -> stay within that framework (lowercase to match refs)
        segs = root_path.lower().strip("/").split("/")
        scope_prefix = "/" + "/".join(segs[:2]) + "/"  # e.g. /documentation/avfaudio/

    print(f"root:  {root_path}")
    print(f"scope: {scope_prefix}")

    docs, failed = crawl_graph(root_path, scope_prefix, args.max_pages, args.max_depth, args.concurrency)
    print(f"\nfetched {len(docs)} render-JSON documents ({len(failed)} skipped)")

    if args.preview:
        for i, (p, doc) in enumerate(list(docs.items())[: args.preview]):
            title, _, md = doc_to_markdown(doc)
            print("\n" + "=" * 78)
            print(f"# {title}   ({p})")
            print("=" * 78)
            print(md[:2500])
        print("\nunknown render types:", dict(UNKNOWN_TYPES) or "none")
        return 0

    # Build results; root first, then by title for a readable concatenation.
    from app import dedup, storage

    ordered = sorted(docs.items(), key=lambda kv: (kv[0] != root_path, kv[0]))
    results: List[Dict[str, Any]] = []
    for p, doc in ordered:
        res = build_result(p, doc, run_signals=True)
        # dedup runs AFTER scrape (as in main.py / crawler.py), registers into
        # the shared corpus index.
        res["metadata"]["dedup"] = dedup.check_and_register(res["markdown"], res["url"])
        results.append(res)

    now = datetime.datetime.now().isoformat(timespec="seconds")
    job = {
        "jobId": None,
        "base_url": doc_url_to_page_url(root_path),
        "status": "completed",
        "engine": "http-json",
        "source": "appledocs-docc",
        "scope_prefix": scope_prefix,
        "total": len(results),
        "skipped": [doc_url_to_page_url(p) for p in failed],
        "start_time": now,
        "end_time": now,
        "results": results,
    }
    json_path = write_artifact(job, args.out)

    # ---- run report ----
    kinds = Counter(r["metadata"].get("symbolKind") or r["metadata"].get("roleHeading") or "?" for r in results)
    langs = Counter((r["metadata"].get("language") or {}).get("lang", "?") for r in results)
    near = sum(1 for r in results if (r["metadata"].get("dedup") or {}).get("near_duplicate_of"))
    exact = sum(1 for r in results if (r["metadata"].get("dedup") or {}).get("exact_duplicate_of"))
    total_chars = sum(len(r["markdown"]) for r in results)
    thin = sum(1 for r in results if len(r["markdown"]) < 200)

    print("\n=== run report ===")
    print(f"pages saved:        {len(results)}")
    print(f"pages skipped:      {len(failed)}" + (f" -> {job['skipped']}" if failed else ""))
    print(f"artifact:           {json_path}")
    print(f"total markdown:     {total_chars:,} chars")
    print(f"thin (<200 chars):  {thin}")
    print(f"dedup near/exact:   {near}/{exact}")
    print(f"languages:          {dict(langs)}")
    print(f"page kinds:         {dict(kinds.most_common())}")
    print(f"unknown render types: {dict(UNKNOWN_TYPES) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
