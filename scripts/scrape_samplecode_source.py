#!/usr/bin/env python3
"""Pull actual sample-code *source* for the audio frameworks into the corpus.

The DAPT crawl already captured the sample-code *landing pages* (the prose
"Overview" articles) but not the downloadable Xcode projects behind them. Those
projects are real, idiomatic Swift/Obj-C using the audio APIs — dense, additive
signal that no documentation page duplicates.

Each sample-code page's DocC render JSON exposes the download:

    doc["sampleCodeDownload"]["action"]["identifier"]  -> a reference key
    references[<key>] = {"type":"download","url":".../Foo.zip", "checksum": ...}

This collector, for a set of audio sample-code doc paths:
  1. fetches render JSON, resolves the .zip URL,
  2. downloads + extracts it,
  3. emits one record per source file (.swift/.h/.m/.mm/.c/.cpp/.metal/.md)
     as {"text", "meta": {url,title,framework,source:"samplecode",file,...}},
     skipping VCS/asset/project-metadata cruft.

The per-file LICENSE.txt (Apple's sample projects ship an MIT-equivalent grant)
is detected once per project and attached to every file's meta as `repo_license`.

Pages that 403 or expose no download are recorded in skipped[].

Output: data/dapt/extra-sources/samplecode.jsonl (+ samplecode.fetch.json report)

Usage (from repo root, with the project venv):
    .venv/bin/python scripts/scrape_samplecode_source.py
    .venv/bin/python scripts/scrape_samplecode_source.py --preview 2
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import zipfile
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from curl_cffi import requests as cffi_requests  # noqa: E402

import scrape_apple_docs as col  # render-JSON fetch + path<->url helpers  # noqa: E402

OUT_DIR = os.path.join(_REPO_ROOT, "data", "dapt", "extra-sources")

# Source file extensions worth keeping as corpus text.
SOURCE_EXTS = (".swift", ".h", ".m", ".mm", ".c", ".cpp", ".cc", ".metal", ".md")
# Path fragments to drop wholesale (VCS / build / asset bundles / project files).
SKIP_PATH_RE = re.compile(
    r"(^|/)\.git(/|$)|\.xcodeproj/|\.xcworkspace/|\.xcassets/|"
    r"Preview Content/|\.lproj/|/build/|DerivedData/",
    re.I,
)
# Per-file size guard: skip empty and absurdly large (generated) files.
MIN_BYTES, MAX_BYTES = 8, 400_000

# The audio-framework sample-code doc paths (the 48 "Sample Code" pages found in
# the existing avfoundation-audio corpus). Kept explicit so the run is
# deterministic and reviewable; regenerate via _sample_paths_from_corpus().
DEFAULT_SAMPLE_PATHS = [
    "/documentation/avfaudio/adding-synthesized-speech-to-calls",
    "/documentation/avfaudio/building-a-signal-generator",
    "/documentation/avfaudio/building-an-audio-sequencer-to-arrange-and-play-clips",
    "/documentation/avfaudio/capturing-stereo-audio-from-built-in-microphones",
    "/documentation/avfaudio/creating-a-custom-speech-synthesizer",
    "/documentation/avfaudio/creating-custom-audio-effects",
    "/documentation/avfaudio/performing-offline-audio-processing",
    "/documentation/avfaudio/playing-custom-audio-with-your-own-player",
    "/documentation/avfaudio/using-voice-processing",
    "/documentation/audiotoolbox/encoding-and-decoding-audio",
    "/documentation/audiotoolbox/generating-spatial-audio-from-a-multichannel-audio-stream",
    "/documentation/audiotoolbox/incorporating-audio-effects-and-instruments",
    "/documentation/coreaudio/building-an-audio-server-plug-in-and-driver-extension",
    "/documentation/coreaudio/capturing-system-audio-with-core-audio-taps",
    "/documentation/coreaudio/creating-an-audio-server-driver-plug-in",
    "/documentation/coremidi/incorporating-midi-2-into-your-apps",
    "/documentation/audiodriverkit/creating-an-audio-device-driver",
    "/documentation/soundanalysis/classifying-live-audio-input-with-a-built-in-sound-classifier",
    "/documentation/shazamkit/building-a-custom-catalog-and-matching-audio",
    "/documentation/shazamkit/shazamkit-dance-finder-with-managed-session",
    "/documentation/avfoundation/capturing-spatial-audio-in-your-ios-app",
    "/documentation/avfoundation/debugging-avfoundation-audio-mixes-compositions-and-video-compositions",
    "/documentation/avfoundation/using-avfoundation-to-play-and-persist-http-live-streams",
    # AVFoundation capture/playback samples that ship substantial Swift source:
    "/documentation/avfoundation/avcam-building-a-camera-app",
    "/documentation/avfoundation/avcambarcode-detecting-barcodes-and-faces",
    "/documentation/avfoundation/avcamfilter-applying-filters-to-a-capture-stream",
    "/documentation/avfoundation/avmulticampip-capturing-from-multiple-cameras",
    "/documentation/avfoundation/build-a-responsive-camera-app-that-launches-quickly",
    "/documentation/avfoundation/capturing-cinematic-video",
    "/documentation/avfoundation/capturing-consistent-color-images",
    "/documentation/avfoundation/capturing-depth-using-the-lidar-camera",
    "/documentation/avfoundation/creating-a-seamless-multiview-playback-experience",
    "/documentation/avfoundation/editing-and-playing-hdr-video",
    "/documentation/avfoundation/enhancing-live-video-by-leveraging-truedepth-camera-data",
    "/documentation/avfoundation/integrating-airplay-for-long-form-video-apps",
    "/documentation/avfoundation/reading-multiview-3d-video-files",
    "/documentation/avfoundation/streaming-depth-data-from-the-truedepth-camera",
    "/documentation/avfoundation/supporting-coordinated-media-playback",
    "/documentation/avfoundation/using-hevc-video-with-alpha",
    "/documentation/avfoundation/writing-fragmented-mpeg-4-files-for-http-live-streaming",
]


def _sample_paths_from_corpus(corpus_file: str = "") -> List[str]:
    """Recover sample-code doc paths from a corpus .jsonl file (read-only helper)."""
    src = corpus_file or os.path.join(_REPO_ROOT, "data", "dapt", "avfoundation-audio.jsonl")
    paths: List[str] = []
    seen = set()
    with open(src, encoding="utf-8") as f:
        for line in f:
            try:
                m = json.loads(line)["meta"]
            except Exception:
                continue
            if m.get("symbolKind") == "Sample Code" or m.get("roleHeading") == "Sample Code":
                from urllib.parse import urlsplit
                p = urlsplit(m.get("url", "")).path.rstrip("/")
                if p and p not in seen:
                    seen.add(p)
                    paths.append(p)
    return paths


def resolve_zip_url(doc: Dict[str, Any]) -> Optional[str]:
    refs = doc.get("references", {})
    scd = doc.get("sampleCodeDownload") or {}
    ident = (scd.get("action") or {}).get("identifier")
    if ident and ident in refs and refs[ident].get("url"):
        return refs[ident]["url"]
    for r in refs.values():
        if r.get("type") == "download" and str(r.get("url", "")).endswith(".zip"):
            return r["url"]
    return None


def download_zip(url: str, retries: int = 3) -> Optional[bytes]:
    last = None
    for attempt in range(retries):
        try:
            r = cffi_requests.get(url, impersonate="chrome", timeout=120)
            if r.status_code == 200 and r.content:
                return r.content
            last = f"HTTP {r.status_code}"
        except Exception as e:
            last = repr(e)
    print(f"    ! zip download failed {url}: {last}", file=sys.stderr)
    return None


def _detect_repo_license(names_to_bytes: Dict[str, bytes]) -> Optional[Dict[str, Any]]:
    """Look for a LICENSE file in the archive; classify MIT-style grants."""
    for name, data in names_to_bytes.items():
        base = name.rsplit("/", 1)[-1].lower()
        if base in ("license", "license.txt", "license.md", "licence", "licence.txt"):
            try:
                txt = data.decode("utf-8", "replace")
            except Exception:
                continue
            low = txt.lower()
            lic_id = "unknown"
            if "permission is hereby granted, free of charge" in low and "without restriction" in low:
                lic_id = "MIT"  # Apple sample-code grant is the MIT text
            elif "apache license" in low:
                lic_id = "Apache-2.0"
            return {"id": lic_id, "source": "repo-license-file", "file": name,
                    "evidence": txt.strip()[:300]}
    return None


def extract_source_files(zip_bytes: bytes) -> Tuple[List[Tuple[str, str]], Optional[Dict[str, Any]]]:
    """Return ([(relpath, text), ...], repo_license). Filters cruft + bad sizes."""
    files: List[Tuple[str, str]] = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except Exception as e:
        print(f"    ! bad zip: {e!r}", file=sys.stderr)
        return files, None

    # license detection wants the raw LICENSE bytes regardless of extension
    license_blobs: Dict[str, bytes] = {}
    for info in zf.infolist():
        name = info.filename
        if info.is_dir():
            continue
        base = name.rsplit("/", 1)[-1].lower()
        if base.startswith("license") or base.startswith("licence"):
            try:
                license_blobs[name] = zf.read(info)
            except Exception:
                pass
    repo_license = _detect_repo_license(license_blobs)

    for info in zf.infolist():
        name = info.filename
        if info.is_dir():
            continue
        if SKIP_PATH_RE.search(name):
            continue
        if not name.lower().endswith(SOURCE_EXTS):
            continue
        if not (MIN_BYTES <= info.file_size <= MAX_BYTES):
            continue
        try:
            raw = zf.read(info)
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = raw.decode("latin-1")
            except Exception:
                continue
        except Exception:
            continue
        if not text.strip():
            continue
        files.append((name, text))
    return files, repo_license


def process_sample(doc_path: str) -> Dict[str, Any]:
    """Returns {ok, doc_path, url, title, framework, zip_url, files:[(name,text)], repo_license, reason}."""
    page_url = col.doc_url_to_page_url(doc_path)
    fw = doc_path.strip("/").split("/")[1] if "/" in doc_path.strip("/") else ""
    out = {"ok": False, "doc_path": doc_path, "url": page_url, "title": "",
           "framework": fw, "zip_url": None, "files": [], "repo_license": None, "reason": ""}

    doc = col.fetch_json(doc_path)
    if not doc:
        out["reason"] = "render-json-403-or-fail"
        return out
    out["title"] = doc.get("metadata", {}).get("title", "")
    zip_url = resolve_zip_url(doc)
    if not zip_url:
        out["reason"] = "no-download-ref"
        return out
    out["zip_url"] = zip_url

    zb = download_zip(zip_url)
    if not zb:
        out["reason"] = "zip-download-failed"
        return out
    files, repo_license = extract_source_files(zb)
    if not files:
        out["reason"] = "no-source-files"
        return out
    out["ok"] = True
    out["files"] = files
    out["repo_license"] = repo_license
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from-corpus", action="store_true",
                    help="derive sample-code paths from the existing corpus instead of the baked list")
    ap.add_argument("--corpus-file", default="",
                    help="path to a .jsonl corpus file; derive sample-code paths from it")
    ap.add_argument("--paths", default="",
                    help="comma-separated list of /documentation/... paths to process")
    ap.add_argument("--out", default="",
                    help="output filename stem under data/dapt/extra-sources/ (default: samplecode)")
    ap.add_argument("--preview", type=int, default=0,
                    help="process N samples, print file inventory, no signals/save")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    if args.paths:
        paths = [p.strip() for p in args.paths.split(",") if p.strip()]
    elif args.corpus_file:
        paths = _sample_paths_from_corpus(args.corpus_file)
    elif args.from_corpus:
        paths = _sample_paths_from_corpus()
    else:
        paths = DEFAULT_SAMPLE_PATHS
    if args.limit:
        paths = paths[: args.limit]
    print(f"sample-code pages to process: {len(paths)}", flush=True)

    if args.preview:
        for p in paths[: args.preview]:
            r = process_sample(p)
            print("\n" + "=" * 78)
            print(f"# {r['title']}  ({p})  ok={r['ok']} {r['reason']}")
            print(f"  zip: {r['zip_url']}")
            print(f"  repo_license: {r['repo_license']}")
            print(f"  source files: {len(r['files'])}")
            for name, text in r["files"][:12]:
                print(f"    {len(text):6d}c  {name}")
        return 0

    from app import dedup, lang, license_detect, quality

    os.makedirs(OUT_DIR, exist_ok=True)
    stem = args.out.strip() or "samplecode"
    out_path = os.path.join(OUT_DIR, f"{stem}.jsonl")

    skipped: List[Dict[str, str]] = []
    projects_ok = 0
    n_files = 0
    total_chars = 0
    langs = Counter()
    qpass = 0
    by_ext = Counter()
    by_fw = Counter()
    lic_counter = Counter()

    with open(out_path, "w", encoding="utf-8") as out:
        for p in paths:
            r = process_sample(p)
            if not r["ok"]:
                skipped.append({"url": r["url"], "title": r["title"], "reason": r["reason"]})
                print(f"  skip {p}: {r['reason']}", flush=True)
                continue
            projects_ok += 1
            repo_license = r["repo_license"]
            for name, text in r["files"]:
                ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
                ql = quality.assess(text)
                lg = lang.detect(text)
                # license: prefer an explicit repo LICENSE; else scan the file text for CC markers
                lic = repo_license or license_detect.detect_text(text)
                # per-file dedup key = page URL + in-zip path (unique, stable)
                file_key = f"{r['url']}#{name}"
                dd = dedup.check_and_register(text, file_key)
                rec = {
                    "text": text,
                    "meta": {
                        "url": r["url"],
                        "title": r["title"],
                        "framework": r["framework"],
                        "source": "samplecode",
                        "file": name,
                        "ext": ext,
                        "zip_url": r["zip_url"],
                        "language": (lg or {}).get("lang", ""),
                        "language_prob": (lg or {}).get("prob"),
                        "quality_passed": ql.get("passed"),
                        "quality_failures": ql.get("failures", []),
                        "license": lic,
                        "repo_license": (repo_license or {}).get("id") if repo_license else None,
                        "content_hash": dd.get("content_hash", ""),
                        "exact_duplicate_of": dd.get("exact_duplicate_of"),
                        "near_duplicate_of": dd.get("near_duplicate_of"),
                    },
                }
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n_files += 1
                total_chars += len(text)
                langs[(lg or {}).get("lang", "?")] += 1
                qpass += 1 if ql.get("passed") else 0
                by_ext[ext] += 1
                by_fw[r["framework"]] += 1
                if lic:
                    lic_counter[lic.get("id", "?")] += 1
            print(f"  ok   {p}: {len(r['files'])} files"
                  f" (license={(repo_license or {}).get('id') if repo_license else 'none'})", flush=True)

    report = {
        "source": "samplecode",
        "jsonl": os.path.relpath(out_path, _REPO_ROOT),
        "pages_processed": len(paths),
        "projects_ok": projects_ok,
        "files": n_files,
        "skipped": skipped,
        "skipped_count": len(skipped),
        "total_chars": total_chars,
        "est_tokens": total_chars // 4,
        "quality_pass": qpass,
        "languages": dict(langs.most_common()),
        "by_ext": dict(by_ext.most_common()),
        "by_framework": dict(by_fw.most_common()),
        "licenses": dict(lic_counter.most_common()),
    }
    with open(os.path.join(OUT_DIR, f"{stem}.fetch.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print("\n=== sample-code source report ===")
    print(f"file:           {report['jsonl']}")
    print(f"projects ok:    {projects_ok}/{len(paths)}  ({len(skipped)} skipped)")
    print(f"source files:   {n_files}")
    print(f"est. tokens:    {report['est_tokens']:,}")
    print(f"quality pass:   {qpass}/{n_files}")
    print(f"by ext:         {report['by_ext']}")
    print(f"languages:      {report['languages']}")
    print(f"licenses:       {report['licenses']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
