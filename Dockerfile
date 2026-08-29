# FilingAgent API image (Lane G — PLAN.md Wave 1 / FR10.1).
#
# Design constraints this file must satisfy:
#   - Slim base, cached dependency layer (requirements.txt copied and
#     installed before source, so a code-only change doesn't reinstall
#     ~1GB of ML/vector-store dependencies).
#   - Non-root user at runtime.
#   - The index is BAKED IN at build time (FR6.2) — ingestion runs once,
#     here, against the filings already committed to data/raw/ and the
#     XBRL payloads committed to data/reference/ (no live network access
#     needed to build this image; see PLAN.md Lane A note: "filings are
#     already committed... treat live EDGAR fetch as a refresh path
#     only"). The container never ingests at boot: a multi-minute ingest
#     on container start times out free-tier hosting.
#
# Build: docker build -t filingagent-api .
# Run:   docker run -p 8000:8000 -e GEMINI_API_KEY=... filingagent-api
# (GEMINI_API_KEY is the default provider's key — see LLM_PROVIDER in
# .env.example. Never bake a key into the image; pass it at run time.)
# (docker-compose.yml is the preferred way to run this locally — it also
# mounts a persisted volume over CHROMA_DIR so the store baked in here
# survives container recreation; Docker copies this image's baked-in
# directory contents into a fresh named volume on first use.)

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# --- Cached dependency layer -------------------------------------------
# Only invalidated when requirements.txt changes, not on every source edit.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- Application source + committed corpus -------------------------------
COPY src/ ./src/
COPY data/ ./data/
COPY eval/ ./eval/

# --- Bake the index in at build time (FR6.2) ------------------------------
# CHROMA_DIR/TRACE_DB match .env.example's documented defaults, resolved
# relative to WORKDIR so the populated store and facts DB land inside this
# image layer, not in an ephemeral runtime volume.
ENV CHROMA_DIR=/app/chroma_db \
    TRACE_DB=/app/traces.sqlite \
    EDGAR_USER_AGENT="FilingAgent image-build contact@example.com"
RUN python -m src.ingest

# --- Non-root user ---------------------------------------------------------
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin filingagent \
    && chown -R filingagent:filingagent /app
USER filingagent

# --- Public-demo safety defaults (FR6.3) — overridable at `docker run`/
# compose time via -e or docker-compose.yml's environment block. The
# provider-side spend limit itself is set in the Anthropic console, not
# here; see .env.example.
ENV RATE_LIMIT_PER_MIN=10 \
    DAILY_REQUEST_CAP=200

EXPOSE 8000

# PORT is injected by most container hosts (Render, Fly, Railway, Cloud Run)
# and must be honored, or the platform's health probe never connects and the
# deploy is marked failed. Defaults to 8000 so local `docker run -p 8000:8000`
# and docker-compose.yml keep working unchanged.
ENV PORT=8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen(f\"http://localhost:{os.environ.get('PORT','8000')}/healthz\", timeout=3).status == 200 else 1)"

# Shell form, so $PORT is expanded at container start rather than baked in.
CMD uvicorn src.api:app --host 0.0.0.0 --port ${PORT}
