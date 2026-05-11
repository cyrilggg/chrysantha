#!/bin/bash
set -e

echo ">>> Starting executor-bridge..."
exec python /app/server.py
