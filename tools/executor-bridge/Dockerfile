FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY models.py /app/
COPY chrysantha_client.py /app/
COPY vnpy_client.py /app/
COPY signal_parser.py /app/
COPY risk_engine.py /app/
COPY server.py /app/
COPY entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8001/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
