# Single image: React frontend built and served by the FastAPI backend.
# Build context is the repo root (not Backend/), because we need both.
#
#   docker build -t jewellery-bot .
#   docker run -p 8080:8080 -e GROQ_API_KEY=gsk_... jewellery-bot

# ---- Stage 1: build the React frontend ----------------------------------
FROM node:20-slim AS frontend

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./frontend/
RUN cd frontend && npm ci

COPY frontend/ ./frontend/
# vite.config.js writes to ../Backend/static -> /build/Backend/static
RUN cd frontend && npm run build


# ---- Stage 2: python backend + playwright -------------------------------
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY Backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --with-deps is required: python:3.11-slim lacks the shared libraries Chromium
# needs (libnss3, libatk, libgbm...). Without it `playwright install chromium`
# succeeds but the browser fails to launch at runtime, silently killing tier 4.
RUN playwright install --with-deps chromium && \
    rm -rf /var/lib/apt/lists/*

COPY Backend/ /app/
# Copy the freshly built frontend AFTER the backend, so it overwrites any stale
# committed bundle in Backend/static.
COPY --from=frontend /build/Backend/static /app/static

# Cloud Run injects PORT; default it so `docker run` works locally too.
ENV PORT=8080
EXPOSE 8080

CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT}
