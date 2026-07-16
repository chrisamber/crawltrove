#!/usr/bin/env bash
#
# smoke_vision.sh — end-to-end smoke test for the OCR stack (Tesseract +
# optional vision-LLM escalation) against a RUNNING CrawlTrove service.
#
# Run this where the full stack exists (the Docker container has Tesseract
# and the Playwright-pinned chromium baked in):
#
#   ALLOW_PRIVATE_NETWORKS=true docker compose up -d --build
#   scripts/smoke_vision.sh                      # tesseract-only smoke
#   OCR_VISION_ENABLED=true scripts/smoke_vision.sh   # + vision tier
#                                                 (service must have a vision-
#                                                  capable LLM backend set:
#                                                  LOCAL_LLM_BASE_URL or
#                                                  ANTHROPIC_API_KEY, plus
#                                                  OCR_VISION_ENABLED=true in
#                                                  the SERVICE environment)
#
#   BASE_URL   service under test    (default http://localhost:8000)
#   FIXTURE    image URL to scrape   (default a generated local fixture served
#                                     by a throwaway http.server on :8877)
#   FIXTURE_HOST host visible to the service (default host.docker.internal;
#                                             use 127.0.0.1 for a host service)
#
# The temporary ALLOW_PRIVATE_NETWORKS opt-in is required only because this
# smoke test deliberately scrapes its own local fixture. Do not use that opt-in
# on an instance accepting untrusted URLs.
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
WORKDIR="$(mktemp -d)"
trap 'kill "${HTTPD_PID:-}" 2>/dev/null || true; find "$WORKDIR" -mindepth 1 -delete; rmdir "$WORKDIR"' EXIT

echo "smoke_vision: checking service at $BASE_URL"
curl -sf "$BASE_URL/api/health" >/dev/null

if [[ -z "${FIXTURE:-}" ]]; then
  echo "smoke_vision: generating an image fixture with a text layer"
  python3 - "$WORKDIR" <<'EOF'
import sys
from PIL import Image, ImageDraw
img = Image.new("RGB", (600, 160), "white")
d = ImageDraw.Draw(img)
d.text((30, 60), "CRAWLTROVE VISION SMOKE TEST", fill="black")
img.save(f"{sys.argv[1]}/fixture.png")
EOF
  (cd "$WORKDIR" && python3 -m http.server 8877 --bind 0.0.0.0 >/dev/null 2>&1) &
  HTTPD_PID=$!
  sleep 1
  FIXTURE="http://${FIXTURE_HOST:-host.docker.internal}:8877/fixture.png"
fi

echo "smoke_vision: scraping $FIXTURE"
RESP="$(curl -sf -X POST "$BASE_URL/api/scrape" \
  -H 'Content-Type: application/json' \
  -d "{\"url\": \"$FIXTURE\"}")"

python3 - "$RESP" <<'EOF'
import json, sys
r = json.loads(sys.argv[1])
meta = r.get("metadata") or {}
ocr = meta.get("ocr")
extractor = meta.get("extractor")
print(f"  success   : {r.get('success')}")
print(f"  extractor : {extractor}")
print(f"  ocr block : {json.dumps(ocr) if ocr else None}")
assert r.get("success"), "scrape failed"
assert extractor and extractor.startswith("image+ocr"), (
    f"expected image+ocr extractor, got {extractor!r} — is Tesseract "
    "installed in the service container?")
assert ocr and ocr.get("engine"), "metadata.ocr missing"
if extractor.endswith("+vision"):
    print("  vision tier: ESCALATED (engine=%s)" % ocr.get("engine"))
else:
    print("  vision tier: not escalated (fine when confidence is high or "
          "OCR_VISION_ENABLED is off)")
print("smoke_vision: OK")
EOF
