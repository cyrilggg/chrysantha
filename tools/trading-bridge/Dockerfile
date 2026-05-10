FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy bridge source
COPY chrysantha_vendor.py /app/
COPY server.py /app/
COPY requirements.txt /app/

# Install bridge deps
RUN pip install --no-cache-dir -r /app/requirements.txt

# TradingAgents source is mounted as a volume at runtime.
# Its dependencies are installed via an entrypoint script.
COPY entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
