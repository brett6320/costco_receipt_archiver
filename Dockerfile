# Costco Receipt Archiver — web app in a container.
# Includes headless Chromium (for PDF rendering) via Playwright.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Install Python deps, then the Chromium browser + its system libraries.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && python -m playwright install --with-deps chromium

COPY costco_archiver ./costco_archiver
COPY README.md ./

# Receipts, outputs, credentials all live here — mount a volume to persist.
VOLUME /app/data

# Port is configurable via the PORT env var (default 8000).
ENV PORT=8000
EXPOSE ${PORT}

# Shell form so $PORT is expanded; bind to all interfaces for host reachability.
CMD python -m costco_archiver web --host 0.0.0.0 --port "${PORT}"
