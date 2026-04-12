#!/bin/bash
# Start all Forge services: Xvfb, Django, and 4 agents via ClaudeCodeManager.
set -euo pipefail

FORGE_DIR="/opt/forge"
USER="forge"

# ── Auth check ──
if [ ! -f "/home/${USER}/.claude/.credentials.json" ] && \
   [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "Error: Claude Code is not authenticated."
  echo ""
  echo "Run 'forge login' first, then try 'forge start' again."
  exit 1
fi

echo "=== Starting Forge ==="

# 1. Start Xvfb (virtual display for QC Chrome)
if ! pgrep -f "Xvfb :1" >/dev/null 2>&1; then
  echo "Starting Xvfb :1..."
  Xvfb :1 -screen 0 1920x1080x24 &
  sleep 1
fi

# 2. Start Django (gunicorn loads .env via gunicorn.conf.py)
echo "Starting Django on :8100..."
su - "$USER" -c "screen -dmS forge-django bash -c '\
  cd ${FORGE_DIR}/ui && \
  source ${FORGE_DIR}/venv/bin/activate && \
  exec gunicorn forge_ui.asgi:application -c gunicorn.conf.py'"
sleep 3

# 3. Start agents via ClaudeCodeManager (replaces screen-based agent startup)
echo "Starting agents via ClaudeCodeManager..."
su - "$USER" -c "screen -dmS forge-agents bash -c '\
  cd ${FORGE_DIR}/ui && \
  source ${FORGE_DIR}/venv/bin/activate && \
  exec python manage.py start_forge'"
sleep 5

echo "=== Forge fully started ==="
echo "UI: http://localhost:8100"
echo "Agents: managed by ClaudeCodeManager (screen -r forge-agents to monitor)"
echo "Dashboard: http://localhost:8100/ (live multi-panel view)"
