FROM mcr.microsoft.com/playwright/python:v1.60.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

WORKDIR /workspace

# OCR runtime: Tesseract + CJK language packs (eng + osd ship with the base
# tesseract-ocr package). Pinned ahead of the pip/source layers since system
# packages change least often, keeping this layer cache-friendly.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-chi-sim tesseract-ocr-chi-tra \
        tesseract-ocr-jpn tesseract-ocr-kor \
    && apt-get clean \
    && find /var/lib/apt/lists -mindepth 1 -delete

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY scripts/ ./scripts/

RUN mkdir -p /workspace/data && chown -R pwuser:pwuser /workspace

EXPOSE 8000

# Railway mounts volumes as root. Repair the mount point, then the entrypoint
# drops privileges before it executes the application command.
ENTRYPOINT ["python", "-m", "app.container_entrypoint"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
