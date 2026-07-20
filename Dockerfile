# Container for webhook_server.py (the FastAPI SDLC-integration service).
# main.py's CLI mode doesn't need a container -- run it directly with Python.
FROM python:3.11-slim

# Standard container hygiene: don't write .pyc files, don't buffer stdout
# (so `docker logs` shows output immediately), don't cache pip's download cache.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies in their own layer so `docker build` doesn't reinstall
# everything just because a .py file changed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a non-root user rather than the container's default root.
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz', timeout=3).status == 200 else 1)"

# Single worker, deliberately. IdempotencyStore (see webhook_server.py's module
# docstring) is in-memory and per-process -- running multiple workers/replicas
# would let duplicate deliveries slip through, since each worker would have its
# own idempotency state. Swap IdempotencyStore for a shared backend (Redis, a
# DB row with a unique constraint) before scaling this past one process.
CMD ["uvicorn", "webhook_server:app", "--host", "0.0.0.0", "--port", "8000"]
