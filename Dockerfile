FROM python:3.11-slim

WORKDIR /app

# System dependencies (curl used by some SDK health checks; ca-certificates for HTTPS)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Ensure the persistent directories exist in the image so Docker can seed
# named volumes from them on first run.
RUN mkdir -p downloads .claude/skills uploads dashboards

# Make entrypoint executable (COPY preserves bits but be explicit for cross-host clones).
RUN chmod +x /app/entrypoint.sh

# Only gunicorn is exposed externally. The proxy bridge runs on the loopback
# interface (127.0.0.1:9999) inside the container; nothing outside needs to
# reach it directly.
EXPOSE 5050

# entrypoint.sh starts the proxy bridge in the background, then gunicorn.
ENTRYPOINT ["/app/entrypoint.sh"]
