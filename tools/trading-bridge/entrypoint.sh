#!/bin/bash
set -e

# Install TradingAgents deps on first run (or if pyproject.toml changed)
HASH_FILE="/app/.ta_deps_hash"
CURRENT_HASH=$(cat /app/TradingAgents/pyproject.toml /app/TradingAgents/requirements.txt 2>/dev/null | md5sum | cut -d' ' -f1)

if [ ! -f "$HASH_FILE" ] || [ "$(cat $HASH_FILE)" != "$CURRENT_HASH" ]; then
    echo ">>> Installing TradingAgents dependencies..."
    pip install --no-cache-dir -e /app/TradingAgents/.
    echo "$CURRENT_HASH" > "$HASH_FILE"
    echo ">>> Done."
fi

exec python /app/server.py
