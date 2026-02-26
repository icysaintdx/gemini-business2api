FROM python:3.11-slim

WORKDIR /app

# Install system dependencies (Chromium + Xvfb + fonts)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    curl \
    chromium \
    xvfb \
    dbus \
    fonts-noto-cjk \
    fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy worker code
COPY worker/ worker/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# Create data directory
RUN mkdir -p /app/data
VOLUME /app/data

# Health check (optional, only if HEALTH_PORT is set)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:${HEALTH_PORT:-8080}/health || exit 1

ENTRYPOINT ["./entrypoint.sh"]
