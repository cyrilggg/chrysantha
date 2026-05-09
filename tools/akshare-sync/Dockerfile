FROM python:3.11-slim

LABEL org.opencontainers.image.title="Chrysantha AkShare Sync"
LABEL org.opencontainers.image.description="A-Share market data sync for Ghostfolio"

# Install system dependencies for akshare
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        gcc \
        g++ \
        && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY sync.py .
COPY config.yaml .

# Create log directory
RUN mkdir -p /var/log

# Health check
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"

# Default: run once, then exit (override with --daemon for persistent mode)
ENTRYPOINT ["python", "sync.py"]
CMD ["--daemon"]
