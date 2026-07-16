#!/usr/bin/env python3
"""Scrape WWDC audio/AV session transcripts into the DAPT corpus.

WWDC video pages (developer.apple.com/videos/play/wwdcYYYY/NNN/) are, unlike the
documentation SPA, served as *complete static HTML* by a cheap curl_cffi GET —
the transcript text lives in a `<li class="supplement transcript">` block as a
sequence of `<span class="sentence">` elements, no browser render required.

This collector:
  1. Enumerates candidate sessions from two static listing pages
     (the curated `/videos/audio-video/` topic + the full `/videos/all-videos/`
     catalog), keeping only audio-relevant titles (keyword filter for audio,
     AVFoundation, AVAudioEngine, spatial audio, MusicKit, AudioToolbox, ...).
  2. For each session, fetches the page, reconstructs the transcript, and
     emits one record {"text", "meta": {url,title,framework,source:"wwdc",...}}.

Pages that 403, lack a transcript (e.g. labs / "Available soon" 2026 sessions),
or return too-thin text are recorded in skipped[] rather than failing the run.

Output: data/dapt/extra-sources/wwdc.jsonl  (+ wwdc.fetch.json run report)

Usage (from repo root, with the project venv):
    .venv/bin/python scripts/scrape_wwdc_transcripts.py
    .venv/bin/python scripts/scrape_wwdc_transcripts.py --preview 3
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from bs4 import BeautifulSoup  # noqa: E402
from curl_cffi import requests as cffi_requests  # noqa: E402

HOST = "https://developer.apple.com"
OUT_DIR = os.path.join(_REPO_ROOT, "data", "dapt", "extra-sources")

LISTING_URLS = [
    f"{HOST}/videos/audio-video/",
    f"{HOST}/videos/all-videos/",
]

# Audio-relevance keywords matched (case-insensitive) against the session title.
# Deliberately a touch broad — a few off-topic hits get caught by the quality
# gate / dedup downstream; missing a genuine audio session is the costlier error.
AUDIO_KEYWORDS = [
    "audio", "avaudio", "avfoundation", "spatial audio", "musickit", "music",
    "audiotoolbox", "core audio", "coreaudio", "sound", "shazam", "midi",
    "voice", "speech", "airpods", "airplay", "phase", "haptic", "now playing",
    "recording", "microphone", "soundtrack", "dictation",
]
_KW_RE = re.compile("|".join(re.escape(k) for k in AUDIO_KEYWORDS), re.I)


def _keyword_regex(keywords: Optional[str]) -> "re.Pattern":
    """Build a session-title filter regex from a comma-separated keyword
    string, or fall back to the default audio keyword set when omitted."""
    if not keywords:
        return _KW_RE
    parts = [k.strip() for k in keywords.split(",") if k.strip()]
    return re.compile("|".join(re.escape(k) for k in parts), re.I)

# Map a session to a framework tag by keyword, best-effort (for corpus join).
_FRAMEWORK_RULES = [
    ("avfaudio", re.compile(r"avaudio|avfaudio|audio engine", re.I)),
    ("avfoundation", re.compile(r"avfoundation", re.I)),
    ("musickit", re.compile(r"musickit|apple music", re.I)),
    ("shazamkit", re.compile(r"shazam", re.I)),
    ("soundanalysis", re.compile(r"sound classif|soundanalysis", re.I)),
    ("coremidi", re.compile(r"\bmidi\b", re.I)),
    ("phase", re.compile(r"\bphase\b|physical audio spatial", re.I)),
    ("corehaptics", re.compile(r"haptic", re.I)),
    ("speech", re.compile(r"speech|dictation|voice control|voiceover", re.I)),
    ("audiotoolbox", re.compile(r"audiotoolbox|audio unit|audio workgroup", re.I)),
]


def _framework_for(title: str, default: str = "wwdc-audio") -> str:
    for fw, rx in _FRAMEWORK_RULES:
        if rx.search(title or ""):
            return fw
    return default


def _clean_title(t: str) -> str:
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"^\d{1,2}:\d{2}\s+", "", t)        # leading duration "62:45 "
    t = re.sub(r"\s*WWDC\d{2}\s*$", "", t).strip()  # trailing "WWDC26"
    t = re.sub(r"\s*Available soon\s*$", "", t, flags=re.I).strip()
    return t


def _get(url: str, retries: int = 3) -> Optional[str]:
    last = None
    for attempt in range(retries):
        try:
            r = cffi_requests.get(url, impersonate="chrome", timeout=40)
            if r.status_code == 200:
                return r.text
            last = f"HTTP {r.status_code}"
            if r.status_code in (404, 410):
                break  # not coming back
        except Exception as e:
            last = repr(e)
        time.sleep(0.6 * (attempt + 1))
    if last:
        print(f"  ! fetch failed {url}: {last}", file=sys.stderr)
    return None


def enumerate_sessions(
    keyword_re: "Optional[re.Pattern]" = None,
) -> "OrderedDict[Tuple[str, str], str]":
    """{(event, number): title} for keyword-relevant sessions, across listings.

    Defaults to the audio keyword set; pass `keyword_re` (see `_keyword_regex`)
    to target a different topic (e.g. MapKit) against the same listings.
    """
    kw_re = keyword_re or _KW_RE
    sessions: "OrderedDict[Tuple[str, str], str]" = OrderedDict()
    for url in LISTING_URLS:
        html = _get(url)
        if not html:
            print(f"  ! listing failed: {url}", file=sys.stderr)
            continue
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            m = re.search(r"/videos/play/(wwdc\d{4})/(\d+)", a.get("href", ""))
            if not m:
                continue
            key = (m.group(1), m.group(2))
            title = _clean_title(a.get_text(" ", strip=True))
            if key not in sessions or (not sessions[key] and title):
                sessions[key] = title
    # keep only keyword-relevant titles (and drop the few with empty titles)
    return OrderedDict(
        (k, v) for k, v in sessions.items() if v and kw_re.search(v)
    )


def extract_transcript(html: str) -> str:
    """Reconstruct clean transcript prose from the supplement block."""
    soup = BeautifulSoup(html, "html.parser")
    container = soup.find("li", class_="transcript") or soup.find(
        attrs={"class": re.compile(r"\btranscript\b", re.I)}
    )
    scope = container if container is not None else soup
    sentences = scope.find_all("span", class_="sentence")
    if not sentences:
        return ""
    text = " ".join(s.get_text(" ", strip=True) for s in sentences)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def session_url(event: str, number: str) -> str:
    return f"{HOST}/videos/play/{event}/{number}/"


def fetch_session(
    event: str, number: str, title: str, default_framework: str = "wwdc-audio"
) -> Dict[str, Any]:
    """Returns {ok, url, title, event, number, framework, text, reason}."""
    url = session_url(event, number)
    out = {
        "ok": False, "url": url, "title": title, "event": event,
        "number": number, "framework": _framework_for(title, default_framework),
        "text": "", "reason": "",
    }
    html = _get(url)
    if not html:
        out["reason"] = "fetch-failed-or-403"
        return out
    transcript = extract_transcript(html)
    if not transcript:
        out["reason"] = "no-transcript"
        return out
    # Some sessions only have a 1-2 sentence placeholder; treat very short as no-transcript.
    if len(transcript) < 400:
        out["reason"] = f"thin-transcript({len(transcript)}c)"
        return out
    out["ok"] = True
    out["text"] = transcript
    return out


def build_records(
    sessions, concurrency: int, default_framework: str = "wwdc-audio"
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Fetch all sessions concurrently -> (ok records, skipped[])."""
    results: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = {
            ex.submit(fetch_session, ev, num, title, default_framework): (ev, num)
            for (ev, num), title in sessions.items()
        }
        for fut in as_completed(futs):
            r = fut.result()
            if r["ok"]:
                results.append(r)
            else:
                skipped.append({"url": r["url"], "title": r["title"], "reason": r["reason"]})
    # stable order by event desc then number
    results.sort(key=lambda r: (r["event"], r["number"]))
    skipped.sort(key=lambda s: s["url"])
    return results, skipped


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--preview", type=int, default=0,
                    help="fetch N sessions, print transcript samples, no signals/save")
    ap.add_argument("--limit", type=int, default=0, help="cap session count (debug)")
    ap.add_argument("--keywords", default="",
                    help="comma-separated title-filter keywords, overriding the "
                         "default audio keyword set (e.g. for a different topic "
                         "batch sharing this same script)")
    ap.add_argument("--default-framework", default="wwdc-audio",
                    help="framework tag for sessions matching no _FRAMEWORK_RULES entry")
    ap.add_argument("--out", default="",
                    help="write records to this exact .jsonl path (and its "
                         "sibling .fetch.json report); default: "
                         "data/dapt/extra-sources/wwdc.jsonl")
    args = ap.parse_args()

    kw_re = _keyword_regex(args.keywords)
    print(f"enumerating {'audio-relevant' if not args.keywords else 'keyword-matched'} "
          "WWDC sessions ...", flush=True)
    sessions = enumerate_sessions(kw_re)
    if args.limit:
        sessions = OrderedDict(list(sessions.items())[: args.limit])
    print(f"  candidate sessions: {len(sessions)}", flush=True)

    if args.preview:
        for (ev, num), title in list(sessions.items())[: args.preview]:
            r = fetch_session(ev, num, title, args.default_framework)
            print("\n" + "=" * 78)
            print(f"# {ev}/{num}  {title}  (fw={r['framework']})  ok={r['ok']} {r['reason']}")
            print("=" * 78)
            print(r["text"][:1500])
        return 0

    results, skipped = build_records(sessions, args.concurrency, args.default_framework)
    print(f"  transcripts extracted: {len(results)}   skipped: {len(skipped)}", flush=True)

    # ---- corpus signals (license / quality / language) ----
    from app import dedup, lang, license_detect, quality

    out_path = args.out or os.path.join(OUT_DIR, "wwdc.jsonl")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    report_path = re.sub(r"\.jsonl$", "", out_path) + ".fetch.json"
    langs = Counter()
    qpass = 0
    total_chars = 0
    with open(out_path, "w", encoding="utf-8") as out:
        for r in results:
            text = r["text"]
            ql = quality.assess(text)
            lg = lang.detect(text)
            lic = license_detect.detect_text(text)
            dd = dedup.check_and_register(text, r["url"])
            rec = {
                "text": text,
                "meta": {
                    "url": r["url"],
                    "title": r["title"],
                    "framework": r["framework"],
                    "source": "wwdc",
                    "event": r["event"],
                    "session": r["number"],
                    "language": (lg or {}).get("lang", ""),
                    "language_prob": (lg or {}).get("prob"),
                    "quality_passed": ql.get("passed"),
                    "quality_failures": ql.get("failures", []),
                    "license": lic,
                    "content_hash": dd.get("content_hash", ""),
                    "exact_duplicate_of": dd.get("exact_duplicate_of"),
                    "near_duplicate_of": dd.get("near_duplicate_of"),
                },
            }
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            langs[(lg or {}).get("lang", "?")] += 1
            qpass += 1 if ql.get("passed") else 0
            total_chars += len(text)

    report = {
        "source": "wwdc",
        "jsonl": os.path.relpath(out_path, _REPO_ROOT),
        "candidates": len(sessions),
        "records": len(results),
        "skipped": skipped,
        "skipped_count": len(skipped),
        "total_chars": total_chars,
        "est_tokens": total_chars // 4,
        "quality_pass": qpass,
        "languages": dict(langs.most_common()),
        "by_event": dict(sorted(Counter(r["event"] for r in results).items())),
        "by_framework": dict(Counter(r["framework"] for r in results).most_common()),
    }
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print("\n=== WWDC transcript report ===")
    print(f"file:           {report['jsonl']}")
    print(f"records:        {report['records']}")
    print(f"skipped:        {report['skipped_count']}")
    print(f"est. tokens:    {report['est_tokens']:,}")
    print(f"quality pass:   {report['quality_pass']}/{report['records']}")
    print(f"languages:      {report['languages']}")
    print(f"by event:       {report['by_event']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
