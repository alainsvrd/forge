#!/bin/bash
# Start all Forge services: Xvfb, Django, and 4 agents.
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

# 2. Start Django (gunicorn with uvicorn workers for async SSE)
echo "Starting Django on :8100..."
su - "$USER" -c "screen -dmS forge-django bash -c '\
  cd ${FORGE_DIR}/ui && \
  source ${FORGE_DIR}/venv/bin/activate && \
  set -a && source ${FORGE_DIR}/ui/.env && set +a && \
  exec gunicorn forge_ui.asgi:application \
    -k uvicorn.workers.UvicornWorker \
    -b 0.0.0.0:8100 \
    --workers 2 \
    --timeout 120'"
sleep 3

# 3. Start agents (staggered — each takes ~20s for screen + auto-accept + MCP init)
for agent in pm dev review qc; do
  echo "Starting forge-${agent}..."
  "${FORGE_DIR}/start-agent.sh" "$agent"
  sleep 25
done

echo "=== Forge fully started ==="
echo "UI: http://localhost:8100"
echo "Agents: forge-pm, forge-dev, forge-review, forge-qc"
echo "Check: su - forge -c 'screen -ls'"
