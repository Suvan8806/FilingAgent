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
# Prefer `docker compose up --build` — see docker-compose.yml, which wires the
# host networking this image needs and is the documented entry point.
#
# Direct use, for reference:
#   docker build -t filingagent .
#   docker run -p 8000:8000 --add-host host.docker.internal:host-gateway filingagent
#
# The default provider is Ollama on the HOST (see the ENV block below), so no
# API key is required and no request leaves the machine. Inference runs on the
# host's GPU: passing an NVIDIA device into a container works on Linux but is
# unreliable on Docker Desktop, which is what most people cloning this have.
#
# To use a hosted provider instead, override LLM_PROVIDER, LLM_MODEL, and that
# provider's key at run time. Never bake a key into the image.
#
# Note: nothing is mounted over CHROMA_DIR. The index is a build artifact, not
# user data — a named volume there would be seeded once and then silently
# shadow every later rebuild. See docker-compose.yml for the full reasoning.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# onnxruntime and its BLAS backend size their thread pools from the host CPU
# count, and each worker thread carries its own memory arena. On a small
# shared instance that is pure overhead -- embedding one short query per
# request is not a parallel workload -- and it is a large share of resident
# memory on a 512MB tier. Pinning to one thread trades throughput we do not
# need for headroom we do.
ENV OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    ANONYMIZED_TELEMETRY=False

WORKDIR /app

# --- Cached dependency layer -------------------------------------------
# Only invalidated when requirements.txt changes, not on every source edit.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- Non-root user, created BEFORE the ingest (deliberate ordering) --------
# Chroma's default embedding function lazily downloads an ~79MB ONNX MiniLM
# bundle to $HOME/.cache/chroma on first use. Running the ingest as root and
# only then switching to `filingagent` cached that model under /root, which
# the runtime user cannot see -- so every cold start re-downloaded 79MB and
# held the tarball, its extraction, and onnxruntime resident at once. On a
# 512MB instance that is enough to get OOM-killed after the health check has
# already passed, which presents as a container that goes Live and then
# 502s. Creating the user first means the ingest below caches the model
# where the runtime user will actually look for it, and it is baked into
# the image alongside the index.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin filingagent \
    && chown filingagent:filingagent /app

# --- Application source + committed corpus -------------------------------
COPY --chown=filingagent:filingagent src/ ./src/
COPY --chown=filingagent:filingagent data/ ./data/
COPY --chown=filingagent:filingagent eval/ ./eval/

USER filingagent

# --- Bake the index AND the embedding model in at build time (FR6.2) ------
# CHROMA_DIR/TRACE_DB match .env.example's documented defaults, resolved
# relative to WORKDIR so the populated store and facts DB land inside this
# image layer, not in an ephemeral runtime volume.
ENV CHROMA_DIR=/app/chroma_db \
    TRACE_DB=/app/traces.sqlite \
    EDGAR_USER_AGENT="FilingAgent image-build contact@example.com"
RUN python -m src.ingest

# --- Public-demo safety defaults (FR6.3) — overridable at `docker run`/
# compose time via -e or docker-compose.yml's environment block. The
# provider-side spend limit itself is set in the Anthropic console, not
# here; see .env.example.
ENV RATE_LIMIT_PER_MIN=10 \
    DAILY_REQUEST_CAP=200

# Provider defaults, matching .env.example. Without these, src/llm.py falls
# back to LLM_PROVIDER=groq and a Groq model ID, so an image deployed to a
# host that does not set them comes up pointed at the wrong provider with no
# key for it -- a confusing runtime failure rather than an obvious one. Only
# GEMINI_API_KEY should need to be supplied per host; never bake that in.
ENV LLM_PROVIDER=ollama \
    LLM_MODEL=qwen3:8b \
    JUDGE_MODEL=qwen3:8b \
    LLM_BASE_URL=http://host.docker.internal:11434/v1

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
