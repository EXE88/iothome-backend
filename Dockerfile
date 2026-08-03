# Production image. The same image runs the web process, the Celery worker
# and Celery beat — only the command differs.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Build deps for psycopg/cryptography wheels; kept in a single layer so the
# apt lists do not end up in the image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY iothome/ ./

# Never run as root in production.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/staticfiles \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD curl -fs http://localhost:8000/api/health/ || exit 1

# Daphne is already a dependency and is the reference ASGI server for
# Channels. It is single-process by design — scale out with replicas behind
# the load balancer rather than with worker threads.
CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "iothome.asgi:application"]
