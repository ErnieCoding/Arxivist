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
RUN mkdir -p downloads .claude/skills

# The app listens on this port inside the container.
# docker-compose maps it to 127.0.0.1:5050 on the host.
EXPOSE 5050

ENTRYPOINT ["gunicorn", "--config", "gunicorn.conf.py", "app:app"]
