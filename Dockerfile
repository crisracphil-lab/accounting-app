# ── Build stage ────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Runtime stage ───────────────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY app/ ./app/
COPY requirements.txt .

# Create non-root user and the /data volume mountpoint (owned by that user
# so start.sh can create subdirs without root privileges).
RUN useradd -m -u 1000 bookpoint \
    && mkdir -p /data \
    && chown -R bookpoint:bookpoint /app /data

# Declare /data as a Docker volume so it is preserved across container
# recreations and is clearly visible as a persistence boundary.
VOLUME ["/data"]

EXPOSE 8000

# Use startup script so we can create /data dirs before uvicorn starts
COPY start.sh /start.sh
ENTRYPOINT ["sh", "/start.sh"]
