#!/bin/bash
set -e

# Copy TradingAgents to writable location (volume is read-only)
if [ ! -d /tmp/TradingAgents ]; then
    cp -r /app/TradingAgents /tmp/TradingAgents
fi

# Install TradingAgents deps on first run (or if pyproject.toml changed)
HASH_FILE="/tmp/.ta_deps_hash"
CURRENT_HASH=$(cat /tmp/TradingAgents/pyproject.toml /tmp/TradingAgents/requirements.txt 2>/dev/null | md5sum | cut -d' ' -f1)

if [ ! -f "$HASH_FILE" ] || [ "$(cat $HASH_FILE)" != "$CURRENT_HASH" ]; then
    echo ">>> Installing system deps for curl_cffi..."
    apt-get update -qq && apt-get install -y -qq libcurl4-openssl-dev build-essential 2>&1 | tail -1
    echo ">>> Installing TradingAgents dependencies..."
    pip install --no-cache-dir -e /tmp/TradingAgents/.
    # Rebuild curl_cffi against system libcurl
    pip install --no-cache-dir --force-reinstall curl-cffi 2>&1 | tail -1
    echo "$CURRENT_HASH" > "$HASH_FILE"
    echo ">>> Done."
fi

exec python /app/server.py
