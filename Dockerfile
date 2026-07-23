FROM node:22-bookworm-slim AS dashboard

WORKDIR /dashboard

RUN corepack enable

COPY apps/app/package.json apps/app/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY apps/app/ ./
RUN pnpm build


FROM mcr.microsoft.com/playwright/python:v1.60.0-jammy AS core

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

WORKDIR /workspace

# OCR runtime: Tesseract + CJK language packs (eng + osd ship with the base
# tesseract-ocr package). Also refresh OpenSSL packages from the Ubuntu
# security pocket so image scans do not fail on fixed base CVEs.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-chi-sim tesseract-ocr-chi-tra \
        tesseract-ocr-jpn tesseract-ocr-kor \
    && apt-get install -y --only-upgrade --no-install-recommends \
        openssl libssl3 \
    && apt-get clean \
    && find /var/lib/apt/lists -mindepth 1 -delete

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY scripts/ ./scripts/
COPY --from=dashboard /dashboard/out/ ./app/static/dashboard/

RUN mkdir -p /workspace/data && chown -R pwuser:pwuser /workspace

EXPOSE 8000

# Railway mounts volumes as root. Repair the mount point, then the entrypoint
# drops privileges before it executes the application command.
ENTRYPOINT ["python", "-m", "app.container_entrypoint"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]


FROM python:3.11-slim AS worker-standard

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /workspace

RUN groupadd --gid 1000 pwuser \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin pwuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

USER 1000:1000

EXPOSE 8081

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/health/live', timeout=3)"

CMD ["python", "-m", "app.worker_main", "--config", "/run/crawltrove-workers/standard.json"]


FROM python:3.11-slim AS egress-agent

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /workspace

RUN groupadd --gid 1000 pwuser \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin pwuser

COPY app/__init__.py app/egress_agent.py ./app/

USER 1000:1000

EXPOSE 9443

CMD ["python", "-m", "app.egress_agent", "--bundle", "/run/crawltrove-egress/node.json"]


FROM core AS worker-browser

# A remote worker does not repair shared application volumes. It receives only
# its read-only enrollment bundle and uses the existing Playwright pwuser.
USER 1000:1000
ENTRYPOINT []

EXPOSE 8081

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/health/live', timeout=3)"

CMD ["sh", "-ec", "python -c 'from playwright.sync_api import sync_playwright; p = sync_playwright().start(); b = p.chromium.launch(headless=True); b.close(); p.stop()' && exec python -m app.worker_main --config /run/crawltrove-workers/browser.json"]


FROM worker-browser AS worker-captcha

# The browser image already inherits the core image's Tesseract language packs.
CMD ["sh", "-ec", "python -c 'from playwright.sync_api import sync_playwright; p = sync_playwright().start(); b = p.chromium.launch(headless=True); b.close(); p.stop()' && exec python -m app.worker_main --config /run/crawltrove-workers/captcha.json"]
