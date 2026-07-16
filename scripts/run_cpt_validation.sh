#!/usr/bin/env bash
# Run the 0.5B CPT A/B: mlx-clean vs mlx-clean-plus, each evaluated on the
# COMMON held-out (mlx-clean's test split, copied verbatim into mlx-clean-plus).
#
# Both runs use the identical recipe; the only difference is the train split.
# Each run trains then evaluates --test on its --data dir's test.jsonl (same set).
#
# Usage:  scripts/run_cpt_validation.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REPO/.venv-mlx/bin/python"
DAPT="$REPO/data/dapt"
LOGDIR="$DAPT/mlx-clean-plus"
mkdir -p "$LOGDIR"

run_one () {
  local name="$1" data="$2" adir="$3" log="$4"
  echo "=== [$name] train+test :: data=$data ===" | tee "$log"
  "$PY" -m mlx_lm lora \
    --model Qwen/Qwen2.5-0.5B --train \
    --data "$data" --fine-tune-type lora --num-layers -1 --batch-size 2 \
    --iters 400 --learning-rate 1e-5 --max-seq-length 2048 --grad-checkpoint \
    --val-batches 20 --steps-per-eval 100 --save-every 200 --seed 3407 \
    --adapter-path "$adir" 2>&1 | tee -a "$log"
  echo "=== [$name] evaluating on held-out test split ===" | tee -a "$log"
  "$PY" -m mlx_lm lora \
    --model Qwen/Qwen2.5-0.5B \
    --data "$data" --fine-tune-type lora --num-layers -1 \
    --test --test-batches 30 --max-seq-length 2048 --seed 3407 \
    --adapter-path "$adir" 2>&1 | tee -a "$log"
  echo "=== [$name] DONE ===" | tee -a "$log"
}

run_one "mlx-clean"      "$DAPT/mlx-clean"      "$LOGDIR/adapters-clean-05b"      "$LOGDIR/cpt-clean-05b.ab.log"
run_one "mlx-clean-plus" "$DAPT/mlx-clean-plus" "$LOGDIR/adapters-plus-05b"       "$LOGDIR/cpt-plus-05b.log"

echo "ALL_CPT_DONE"
