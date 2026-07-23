"""Extraction evaluation suite — scores a model's schema-constrained output against
hand-labeled ground truth.

Each case in eval/cases/*.json carries: a `markdown` page, the `schema`, an
optional `prompt`, and the `expected` JSON. We run the page through the
configured LLM backend (whatever extract_llm.backend() resolves — Gemma via
LOCAL_LLM_BASE_URL, or Anthropic) and report, per case and in aggregate:

  - exact          : output == expected (the whole tree)
  - field_accuracy : of the leaf fields we expect, how many are exactly right
                     (correct nulls count); this is the number to watch
  - extra_keys     : keys the model emitted that the schema/expected didn't ask
                     for — should be 0 once additionalProperties:false is set

Structural validity (types/enums/required) is NOT scored: grammar-constrained
decoding already guarantees it, so the evaluation suite measures the thing the grammar
can't — whether the *values* are right.

Usage (from the repo root):

    LOCAL_LLM_BASE_URL=http://localhost:11434 LOCAL_LLM_MODEL=gemma4:12b \
        python -m eval.score
    python -m eval.score --model gemma4:12b --cases eval/cases --runs 3
"""
import argparse
import asyncio
import glob
import json
import os
import sys

# Repo root on path so `from app import extract_llm` works when run as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import extract_llm  # noqa: E402


def flatten(obj, prefix=""):
    """Map a JSON value to {dotted.path: leaf_value}. Lists index by position so
    order matters; None is a real leaf so a correct null scores as a hit."""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = obj
    return out


def score_one(expected, actual):
    """Compare one extraction against ground truth. Returns a dict of metrics
    plus the specific paths that missed, for eyeballing."""
    exp, act = flatten(expected), flatten(actual)
    hits, misses = 0, []
    for path, want in exp.items():
        got = act.get(path, "<<absent>>")
        if got == want:
            hits += 1
        else:
            misses.append((path, want, got))
    extra = sorted(set(act) - set(exp))
    return {
        "exact": expected == actual,
        "field_accuracy": hits / len(exp) if exp else 1.0,
        "n_fields": len(exp),
        "misses": misses,
        "extra_keys": extra,
    }


def load_cases(cases_dir):
    cases = []
    for path in sorted(glob.glob(os.path.join(cases_dir, "*.json"))):
        with open(path) as f:
            cases.append(json.load(f))
    return cases


async def run_case(case, model):
    """Run one case through the LLM backend. Returns (data, error)."""
    try:
        result = await extract_llm.extract(
            case["markdown"], f"eval://{case['name']}", case["schema"],
            prompt=case.get("prompt", ""), model=model,
            examples=case.get("examples"),
        )
        return result["data"], None
    except Exception as e:  # noqa: BLE001 — surface the failure as a row, never crash the suite
        return None, str(e)


async def main():
    ap = argparse.ArgumentParser(description="Score schema-constrained extraction against ground truth.")
    ap.add_argument("--cases", default=os.path.join(os.path.dirname(__file__), "cases"),
                    help="directory of *.json case files")
    ap.add_argument("--model", default=extract_llm.DEFAULT_MODEL,
                    help="model id (local backend ignores claude-* and uses LOCAL_LLM_MODEL)")
    ap.add_argument("--runs", type=int, default=1,
                    help="runs per case; reports best+worst field_accuracy to expose nondeterminism")
    args = ap.parse_args()

    if not extract_llm.configured():
        print("No LLM backend configured. Set LOCAL_LLM_BASE_URL (+ LOCAL_LLM_MODEL) "
              "or ANTHROPIC_API_KEY.", file=sys.stderr)
        return 2

    # The local backend ignores a claude-* id and uses LOCAL_LLM_MODEL, so show
    # the model that will actually serve the request, not the requested default.
    effective_model = args.model
    if extract_llm.backend() == "local" and (not args.model or args.model.startswith("claude")):
        effective_model = os.environ.get("LOCAL_LLM_MODEL", "local")
    print(f"backend={extract_llm.backend()}  model={effective_model}  runs={args.runs}\n")
    cases = load_cases(args.cases)
    if not cases:
        print(f"No cases found in {args.cases}", file=sys.stderr)
        return 2

    agg = []
    for case in cases:
        run_scores = []
        last = None
        for _ in range(args.runs):
            data, err = await run_case(case, args.model)
            if err:
                print(f"  {case['name']:28s}  ERROR: {err}")
                run_scores = None
                break
            last = score_one(case["expected"], data)
            run_scores.append(last)
        if run_scores is None:
            agg.append(0.0)
            continue

        accs = [s["field_accuracy"] for s in run_scores]
        best, worst = max(accs), min(accs)
        agg.append(best)
        exact = sum(s["exact"] for s in run_scores)
        spread = "" if best == worst else f"  (worst {worst:5.1%})"
        flag = " EXTRA-KEYS" if last["extra_keys"] else ""
        print(f"  {case['name']:28s}  acc {best:5.1%}{spread}  "
              f"exact {exact}/{args.runs}  fields {last['n_fields']}{flag}")
        # Show what missed on the most recent run — the actionable part.
        for path, want, got in last["misses"]:
            print(f"      ✗ {path}: want {want!r}  got {got!r}")
        for k in last["extra_keys"]:
            print(f"      + unexpected key: {k}")

    print(f"\n  mean best-of-{args.runs} field_accuracy: {sum(agg) / len(agg):5.1%}  "
          f"across {len(agg)} cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
