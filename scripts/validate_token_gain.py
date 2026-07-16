#!/usr/bin/env python3
"""Parse mlx_lm lora CPT logs into a mlx-clean vs mlx-clean-plus loss table.

The training itself is launched by run_cpt_validation.sh (so it streams to a log
the Bash background-runner can poll). This script just extracts:
  - Iter-1 Val loss  (the base anchor, per the recipe),
  - the Val-loss curve at each eval step,
  - the final Test loss (from `--test --test-batches 30` on the common held-out),
and writes data/dapt/mlx-clean-plus/val-compare.json + prints a table.

Usage:
    .venv-mlx/bin/python scripts/validate_token_gain.py \
        --clean-log <log> --plus-log <log>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(_REPO_ROOT, "data", "dapt", "mlx-clean-plus", "val-compare.json")

_VAL_RE = re.compile(r"Iter\s+(\d+):\s*Val loss\s+([\d.]+)")
_TEST_RE = re.compile(r"Test loss\s+([\d.]+)")


def parse_log(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        txt = f.read()
    vals: List[Dict[str, float]] = []
    for m in _VAL_RE.finditer(txt):
        vals.append({"iter": int(m.group(1)), "val_loss": float(m.group(2))})
    test_m = _TEST_RE.search(txt)
    iter1 = next((v["val_loss"] for v in vals if v["iter"] == 1), None)
    final_val = vals[-1]["val_loss"] if vals else None
    best_val = min((v["val_loss"] for v in vals), default=None)
    return {
        "log": os.path.relpath(path, _REPO_ROOT),
        "iter1_val_loss": iter1,           # base anchor
        "val_curve": vals,
        "final_val_loss": final_val,
        "best_val_loss": best_val,
        "test_loss": float(test_m.group(1)) if test_m else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean-log", required=True)
    ap.add_argument("--plus-log", required=True)
    args = ap.parse_args()

    clean = parse_log(args.clean_log)
    plus = parse_log(args.plus_log)

    def _delta(a: Optional[float], b: Optional[float]) -> Optional[float]:
        return round(b - a, 4) if (a is not None and b is not None) else None

    summary = {
        "recipe": "Qwen2.5-0.5B LoRA, num-layers -1, iters 400, lr 1e-5, "
                  "max-seq 2048, batch 2, seed 3407; test = mlx-clean test split "
                  "(common held-out, identical for both runs)",
        "mlx_clean": clean,
        "mlx_clean_plus": plus,
        "deltas_plus_minus_clean": {
            "iter1_val": _delta(clean["iter1_val_loss"], plus["iter1_val_loss"]),
            "final_val": _delta(clean["final_val_loss"], plus["final_val_loss"]),
            "best_val": _delta(clean["best_val_loss"], plus["best_val_loss"]),
            "test": _delta(clean["test_loss"], plus["test_loss"]),
        },
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    def fmt(x):
        return f"{x:.4f}" if isinstance(x, (int, float)) else "  -  "

    print("\n=== 0.5B CPT: mlx-clean vs mlx-clean-plus (common held-out) ===")
    print(f"{'metric':<22}{'mlx-clean':>12}{'mlx-clean-plus':>16}{'Δ(plus-clean)':>16}")
    rows = [
        ("iter-1 val (anchor)", clean["iter1_val_loss"], plus["iter1_val_loss"]),
        ("final val (iter 400)", clean["final_val_loss"], plus["final_val_loss"]),
        ("best val", clean["best_val_loss"], plus["best_val_loss"]),
        ("test (held-out)", clean["test_loss"], plus["test_loss"]),
    ]
    for name, a, b in rows:
        d = _delta(a, b)
        print(f"{name:<22}{fmt(a):>12}{fmt(b):>16}{fmt(d):>16}")
    print(f"\nwrote {os.path.relpath(OUT, _REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
