# Multi-stage build: keeps the final image lean by separating
# build dependencies (gcc for asyncpg) from the runtime image.

# ---- Build stage ----
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build tools needed for asyncpg C extension
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ---- Runtime stage ----
FROM python:3.11-slim

WORKDIR /app

# Runtime system deps only
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY app/      ./app/
COPY pipeline/ ./pipeline/
COPY data/     ./data/
COPY scripts/  ./scripts/

# Non-root user for security
RUN useradd -m -u 1000 appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

# Uvicorn with 2 workers; increase in production
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
