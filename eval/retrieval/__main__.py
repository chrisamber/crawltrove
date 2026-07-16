import argparse
import asyncio
import json
import os
import sys

import httpx

from app import vecindex
from app.main import API_KEYS, APP_PASSWORD, APP_USERNAME, app
from eval.retrieval import (
    EvalError, MODES, case_set_hash, evaluate_mode, gate_reports, load_cases,
    unresolved_case_ids,
)


def _print_report(report, k):
    print(f"\nmode={report['mode']}  k={k}")
    for row in report["cases"]:
        first = row["firstRelevantRank"] or "-"
        print(
            f"  {row['name']:34s} recall@{k}={row['recall']:.3f} "
            f"mrr={row['mrr']:.3f} ndcg={row['ndcg']:.3f} first={first}")
        if row["missingIds"]:
            print(f"      missing from top {k}: {', '.join(row['missingIds'])}")
    agg = report["aggregate"]
    print(
        f"  macro                              recall@{k}={agg['recall']:.3f} "
        f"mrr={agg['mrr']:.3f} ndcg={agg['ndcg']:.3f}")


def _client_security():
    if API_KEYS:
        return {"headers": {"x-api-key": sorted(API_KEYS)[0]}}
    if APP_PASSWORD:
        return {"auth": httpx.BasicAuth(APP_USERNAME, APP_PASSWORD)}
    return {}


async def run(args):
    cases = load_cases(args.cases)
    missing = unresolved_case_ids(cases, vecindex.identity_inventory)
    if missing:
        raise EvalError(
            "benchmark invalid; relevant targets are not indexed:\n  "
            + "\n  ".join(missing))
    modes = list(dict.fromkeys(args.mode or MODES))
    transport = httpx.ASGITransport(app=app)
    reports = {}
    async with httpx.AsyncClient(
            transport=transport, base_url="http://retrieval-eval",
            **_client_security()) as client:
        for mode in modes:
            reports[mode] = await evaluate_mode(cases, mode, args.k, client)
    result = {
        "caseSetHash": case_set_hash(cases),
        "caseCount": len(cases),
        "k": args.k,
        "index": vecindex.stats(),
        "reports": reports,
        "gate": "not-requested",
        "gateReasons": [],
    }
    if args.gate:
        reasons = gate_reports(reports)
        result["gate"] = "failed" if reasons else "passed"
        result["gateReasons"] = reasons
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(
            f"cases={len(cases)} caseSet={result['caseSetHash']} "
            f"indexTotal={result['index'].get('total', 0)}")
        for mode in modes:
            _print_report(reports[mode], args.k)
        print(f"\ngate={result['gate']}")
        for reason in result["gateReasons"]:
            print(f"  - {reason}")
    return 1 if result["gate"] == "failed" else 0


def main():
    parser = argparse.ArgumentParser(
        prog="python -m eval.retrieval",
        description="Score parent-level retrieval against checked-in labels.")
    parser.add_argument(
        "--cases", default=os.path.join(os.path.dirname(__file__), "cases"),
        help="directory of retrieval case JSON files")
    parser.add_argument("--mode", action="append", choices=MODES,
                        help="mode to score; repeatable, defaults to all")
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--gate", action="store_true",
                        help="fail if hybrid regresses semantic overall or on exact symbols")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.k <= 50:
        parser.error("--k must be between 1 and 50")
    try:
        return asyncio.run(run(args))
    except EvalError as exc:
        print(f"retrieval eval unavailable: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
