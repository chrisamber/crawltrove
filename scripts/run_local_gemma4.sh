#!/usr/bin/env bash
#
# Launch CrawlTrove wired to a local LM Studio (or any OpenAI-compatible)
# server running Gemma 4 12B. The app's extract_llm.backend() resolves to
# "local" whenever LOCAL_LLM_BASE_URL is set, routing /api/extract through the
# local model with grammar-constrained (json_schema) decoding.
#
# Usage:
#   scripts/run_local_gemma4.sh                 # defaults below
#   PORT=8001 scripts/run_local_gemma4.sh       # override app port
#   LOCAL_LLM_MODEL=other-id scripts/run_local_gemma4.sh
#
set -euo pipefail
cd "$(dirname "$0")/.."

export LOCAL_LLM_BASE_URL="${LOCAL_LLM_BASE_URL:-http://127.0.0.1:1234}"
export LOCAL_LLM_MODEL="${LOCAL_LLM_MODEL:-gemma-4-12b-it-optiq}"

echo "CrawlTrove -> local LLM at ${LOCAL_LLM_BASE_URL} (model: ${LOCAL_LLM_MODEL})"
echo "Listening on http://127.0.0.1:${PORT:-8000}"
exec .venv/bin/uvicorn app.main:app --port "${PORT:-8000}" "$@"
