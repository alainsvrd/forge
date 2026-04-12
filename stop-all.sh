#!/bin/bash
# Stop all Forge services gracefully.
set -euo pipefail

USER="forge"

echo "=== Stopping Forge ==="

# Stop claude -p agent subprocesses
pkill -f "claude.*stream-json" 2>/dev/null || true
echo "Stopped agents"

# Stop Django/gunicorn
pkill -f "gunicorn.*forge_ui" 2>/dev/null || true
su - "$USER" -c "screen -S forge-django -X quit" 2>/dev/null || true
su - "$USER" -c "screen -S forge-agents -X quit" 2>/dev/null || true
echo "Stopped Django"

# Stop Xvfb
pkill -f "Xvfb :1" 2>/dev/null || true
echo "Stopped Xvfb"

echo "=== Forge stopped ==="
