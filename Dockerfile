# syntax=docker/dockerfile:1
FROM python:3.12-slim

# uv binary from the official image; pinned minor so builds are reproducible.
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Install dependencies first (cached layer), exactly as pinned in uv.lock.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY app ./app

RUN useradd --create-home appuser
USER appuser

# Render injects $PORT; default to 8000 for local/compose runs.
ENV PORT=8000
EXPOSE 8000

# Single worker so in-process Prometheus counters stay coherent.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --no-access-log"]
