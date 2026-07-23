# Extraction evaluation suite

A tiny ground-truth evaluation suite for schema-constrained extraction. It answers the only
question that matters before reaching for fine-tuning: **is the model's *value*
accuracy actually a problem, and does a change move the number?**

Grammar-constrained decoding already guarantees *structure* (types, enums,
required, nulls, `additionalProperties`, `oneOf`). So this evaluation suite ignores
structure and scores what the grammar can't: whether the values are right.

## Run it

From the repo root, with a backend configured (Gemma via a local
OpenAI-compatible server, or Anthropic):

```bash
# Gemma 4 on Ollama / llama.cpp
LOCAL_LLM_BASE_URL=http://localhost:11434 LOCAL_LLM_MODEL=gemma4:12b \
    .venv/bin/python -m eval.score --runs 3

# Claude (teacher baseline)
ANTHROPIC_API_KEY=sk-... .venv/bin/python -m eval.score --model claude-opus-4-8
```

`--runs N` runs each case N times and reports best + worst field accuracy, so
you can see how much the values wander between runs (set `temperature: 0` on the
server to pin them).

## Metrics

- **field_accuracy** — of the leaf fields in `expected`, how many the model got
  exactly right (a correct `null` counts). This is the number to watch.
- **exact** — whole-tree match.
- **extra_keys** — keys the model emitted that weren't expected. Should be `0`
  once every object in the schema has `additionalProperties: false`.

## Adding cases

Drop a `cases/<name>.json` with:

```json
{
  "name": "...",
  "prompt": "...",            // optional extraction instruction
  "markdown": "...",          // the page text fed to the model
  "schema": { ... },          // the JSON Schema (close every object!)
  "expected": { ... }         // hand-labeled ground truth
}
```

Target ~20 cases spanning your real schema families. That's the set every future
"should we tune / did this prompt help / can a 4B match 12B" decision runs
against.

## Retrieval evaluation

`eval/retrieval/` is a separate parent-level benchmark for the live file index.
It drives the existing hybrid HTTP route and reports macro recall@k, MRR, and
binary nDCG for semantic, keyword, and hybrid modes:

```bash
# Report all modes. Requires the six labeled Swift/Apple pages to be indexed.
.venv/bin/python -m eval.retrieval --k 10

# Compare the current hybrid mode with semantic on the same index.
.venv/bin/python -m eval.retrieval \
  --mode semantic --mode hybrid --k 10 --gate

# Machine-readable report for a saved benchmark artifact.
.venv/bin/python -m eval.retrieval --k 10 --json
```

The command validates every `relevantId` against the index before scoring. A
missing labeled page, unavailable retrieval mode, or malformed case exits `2`
and prints no aggregate quality score. `--gate` exits `1` if hybrid regresses
semantic on any aggregate metric or on the exact-symbol subset. Without
`--gate`, the evaluation suite is report-only; no baseline score is implied or written.

Cases use stable `kind:url:<canonical-url>` identities where possible. Add one
JSON file per case under `eval/retrieval/cases/` with `name`, `query`, non-empty
`relevantIds`, optional `tags`, `filters`, and `notes`.

## Acquisition evaluation

`eval/acquisition/` compares CrawlTrove, Firecrawl, and Crawl4AI on the four
checked-in public fixtures: simple HTML, JavaScript-rendered quotes, plain
text, and PDF. It is report-only: a five-run comparison does not select an
overall winner or gate a release.

Install the optional isolated evaluator, start CrawlTrove locally, and provide
a Firecrawl key only in the shell running the command:

```bash
.venv/bin/python -m pip install -r requirements-eval.txt
FIRECRAWL_API_KEY=... .venv/bin/python -m eval.acquisition --dry-run
FIRECRAWL_API_KEY=... .venv/bin/python -m eval.acquisition
```

Before the first run, install the Chromium runtime required by Crawl4AI 0.9.2
using Crawl4AI's supported browser-setup command. Dependency installation alone
does not download that browser.

If the local API uses `API_KEYS`, set `CRAWLTROVE_API_KEY` in the same shell.

The dry run makes one fresh simple-HTML request per tool. The full run makes
five sequential, cache-disabled calls per tool/case and writes an ignored JSON
report under `tmp/`. Reports contain only outcomes, timing, output-byte counts,
correctness rates, and provider-native usage; they do not contain page bodies,
headers, or credentials.
