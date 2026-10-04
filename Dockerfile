FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.8.3 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    UV_NO_DEV=1 \
    UV_LINK_MODE=copy \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Keep dependency installation cached when application code changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY job_scraper ./job_scraper
RUN uv sync --locked --no-dev --no-editable \
    && playwright install --with-deps chromium \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /app/.local \
    && chown -R app:app /app /ms-playwright

USER app
EXPOSE 8080

CMD ["sh", "-c", "exec uvicorn job_scraper.api:app --host 0.0.0.0 --port ${PORT:-8080}"]
