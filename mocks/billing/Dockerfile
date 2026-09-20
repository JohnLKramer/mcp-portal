# --- Stage 1: builder ---
FROM python:3.14.7-slim AS builder

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src/ src/
RUN uv sync --frozen --no-dev

# --- Stage 2: runtime ---
FROM python:3.14.7-slim AS runner

RUN groupadd -r appuser && useradd -r -g appuser appuser

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER appuser

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')" || exit 1

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--worker-tmp-dir", "/dev/shm", "billing_mock.app:app"]
