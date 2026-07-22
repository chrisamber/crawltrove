"""Command line entrypoint for the report-only acquisition benchmark."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from . import EvalError, load_cases, run_benchmark, write_report


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.acquisition")
    parser.add_argument("--crawltrove-url", default="http://127.0.0.1:8000")
    parser.add_argument("--cases", default=str(Path(__file__).with_name("cases.json")))
    parser.add_argument("--tmp-dir", default="tmp")
    parser.add_argument("--dry-run", action="store_true",
                        help="run the simple HTML case once per adapter and stop")
    args = parser.parse_args()
    api_key = os.environ.get("FIRECRAWL_API_KEY")
    crawltrove_api_key = os.environ.get("CRAWLTROVE_API_KEY")
    try:
        report = asyncio.run(run_benchmark(
            load_cases(args.cases), crawltrove_url=args.crawltrove_url,
            firecrawl_api_key=api_key or "", tmp_dir=args.tmp_dir, dry_run=args.dry_run,
            crawltrove_api_key=crawltrove_api_key,
        ))
        if args.dry_run:
            print("acquisition eval dry run passed")
        else:
            path = write_report(
                report, args.tmp_dir, secrets=(api_key or "", crawltrove_api_key or ""))
            print(path)
        return 0
    except EvalError as exc:
        print(f"acquisition eval unavailable: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
