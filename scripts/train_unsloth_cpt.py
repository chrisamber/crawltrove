#!/usr/bin/env python3
r"""Unsloth continued-pretraining (CPT/DAPT) recipe for the AVFoundation/audio corpus.

  ┌─────────────────────────────────────────────────────────────────────────┐
  │ THIS DOES NOT RUN ON THIS MACHINE.                                        │
  │ Unsloth requires an NVIDIA CUDA GPU. This host is Apple Silicon (arm64,   │
  │ no CUDA), so run this on a cloud GPU (Colab free T4 / RunPod / Lambda) or │
  │ any Linux+NVIDIA box. For a Mac-NATIVE local LoRA instead, use MLX-LM     │
  │ (`pip install mlx-lm`; `mlx_lm.lora ...`) — see the note at the bottom.   │
  └─────────────────────────────────────────────────────────────────────────┘

WHY CPT (not SFT) for this corpus
---------------------------------
The corpus (data/dapt/avfoundation-audio.jsonl) is RAW documentation text — one
`{text, meta}` record per page. That's exactly continued-pretraining input: the
model learns AVFoundation/CoreAudio vocabulary, symbol names, and Swift/C audio
idioms by next-token prediction over the docs. SFT (instruction tuning) needs
{instruction -> response} pairs, which this corpus does NOT contain; you'd have
to synthesize them first (see "Stage 2" below).

Unsloth's own guidance: for best results do CPT first, THEN a short SFT pass.
This script is Stage 1 (CPT). It uses UnslothTrainer/UnslothTrainingArguments,
which add `embedding_learning_rate` — the documented CPT recipe trains the
embeddings + lm_head at a lower LR than the LoRA adapters.

Reality check: ~6.2M tokens is small for CPT. Expect domain *adaptation* (better
terminology/recall/style), not new reasoning ability. Train 2-3 epochs; consider
pooling with WWDC transcripts / sample-code / forums for more signal.

Setup (on the GPU box):
    pip install unsloth
    # corpus: copy data/dapt/avfoundation-audio.jsonl up to the box / Colab

Run:
    python train_unsloth_cpt.py --corpus avfoundation-audio.jsonl \
        --model unsloth/Qwen2.5-Coder-7B --epochs 2 --out avfaudio-cpt
"""
from __future__ import annotations

import argparse


def main() -> int:
    ap = argparse.ArgumentParser(description="Unsloth CPT on the AVFoundation/audio corpus")
    ap.add_argument("--corpus", default="avfoundation-audio.jsonl",
                    help="path to the DAPT JSONL ({text, meta} per line)")
    # Base (NOT instruct) model — CPT adapts a base model. Qwen2.5-Coder is a
    # strong fit for this code-heavy API corpus; Meta-Llama-3.1-8B is a solid
    # general alternative.
    ap.add_argument("--model", default="unsloth/Qwen2.5-Coder-7B")
    ap.add_argument("--max-seq-len", type=int, default=2048)
    ap.add_argument("--epochs", type=float, default=2.0)
    ap.add_argument("--lora-r", type=int, default=128)
    ap.add_argument("--out", default="avfaudio-cpt")
    ap.add_argument("--drop-low-quality", action="store_true",
                    help="drop pages where metadata.quality.passed is False (thin stubs)")
    ap.add_argument("--drop-near-dups", action="store_true",
                    help="drop pages flagged near_duplicate_of to cut boilerplate repetition")
    ap.add_argument("--export-gguf", default="q4_k_m",
                    help="GGUF quant for Ollama/llama.cpp serving; '' to skip")
    args = ap.parse_args()

    import torch
    from datasets import load_dataset
    from unsloth import (
        FastLanguageModel,
        UnslothTrainer,
        UnslothTrainingArguments,
        is_bfloat16_supported,
    )

    if not torch.cuda.is_available():
        raise SystemExit(
            "No CUDA GPU detected. Unsloth needs NVIDIA CUDA — run this on Colab/"
            "RunPod/Lambda or a Linux+NVIDIA box. For Mac-native LoRA use MLX-LM."
        )

    # --- model + LoRA (CPT: include embed_tokens & lm_head) ---
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=args.model,
        max_seq_length=args.max_seq_len,
        dtype=None,
        load_in_4bit=True,
    )
    model = FastLanguageModel.get_peft_model(
        model,
        r=args.lora_r,
        target_modules=[
            "q_proj", "k_proj", "v_proj", "o_proj",
            "gate_proj", "up_proj", "down_proj",
            "embed_tokens", "lm_head",          # <- the bit that makes it CPT
        ],
        lora_alpha=32,
        lora_dropout=0,
        bias="none",
        use_gradient_checkpointing="unsloth",
        random_state=3407,
        use_rslora=True,                        # rank-stabilized; recommended for CPT
    )

    # --- corpus ---
    ds = load_dataset("json", data_files=args.corpus, split="train")

    def keep(ex):
        if not (ex.get("text") or "").strip():
            return False
        meta = ex.get("meta", {}) or {}
        if args.drop_low_quality and meta.get("quality_passed") is False:
            return False
        if args.drop_near_dups and meta.get("near_duplicate_of"):
            return False
        return True

    ds = ds.filter(keep)
    eos = tokenizer.eos_token

    def add_eos(ex):
        return {"text": ex["text"] + eos}

    ds = ds.map(add_eos)
    print(f"training on {len(ds):,} documents")

    # --- train ---
    trainer = UnslothTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=ds,
        dataset_text_field="text",
        max_seq_length=args.max_seq_len,
        dataset_num_proc=2,
        args=UnslothTrainingArguments(
            per_device_train_batch_size=2,
            gradient_accumulation_steps=8,      # effective batch 16
            warmup_ratio=0.1,
            num_train_epochs=args.epochs,
            learning_rate=5e-5,
            embedding_learning_rate=5e-6,       # 10x lower for embeddings (CPT)
            fp16=not is_bfloat16_supported(),
            bf16=is_bfloat16_supported(),
            logging_steps=1,
            optim="adamw_8bit",
            weight_decay=0.01,
            lr_scheduler_type="linear",
            seed=3407,
            output_dir="outputs",
            report_to="none",
        ),
    )
    trainer.train()

    # --- save adapters + (optionally) export GGUF for Ollama/llama.cpp ---
    model.save_pretrained(args.out)
    tokenizer.save_pretrained(args.out)
    print(f"LoRA adapters saved -> {args.out}/")

    if args.export_gguf:
        model.save_pretrained_gguf(args.out + "-gguf", tokenizer,
                                   quantization_method=args.export_gguf)
        print(f"GGUF saved -> {args.out}-gguf/  (serve via Ollama:)")
        print(f"  echo 'FROM ./{args.out}-gguf/unsloth.{args.export_gguf.upper()}.gguf' > Modelfile")
        print("  ollama create avfaudio -f Modelfile && ollama run avfaudio")
    return 0


# ---------------------------------------------------------------------------
# STAGE 2 (recommended, separate run): SFT on synthesized Q&A
# ---------------------------------------------------------------------------
# CPT teaches knowledge; SFT teaches it to ANSWER. After CPT:
#   1. Generate {question, answer} / {task, swift_code} pairs FROM the corpus
#      (use Claude as the teacher, verify answers against the source page).
#   2. Format as chat `messages`, apply the model's chat template.
#   3. Re-run with UnslothTrainer over the CPT checkpoint, using
#      `train_on_responses_only(...)` so loss is computed on answers only.
# Ask the scraper repo's assistant to "generate a source-grounded SFT dataset"
# and it will emit chat JSONL ready for this stage.
#
# MAC-NATIVE ALTERNATIVE (no cloud GPU): MLX-LM runs LoRA on Apple Silicon.
#   pip install mlx-lm
#   mlx_lm.lora --model Qwen/Qwen2.5-Coder-7B --train \
#       --data ./mlx_data --iters 1000     # expects train.jsonl with {"text": ...}
#   mlx_lm.fuse --model ... --adapter-path ...   # then convert to GGUF for Ollama
# (MLX wants a dir with train.jsonl/valid.jsonl; split avfoundation-audio.jsonl
#  into those, keeping only the `text` field.)

if __name__ == "__main__":
    raise SystemExit(main())
