#!/bin/bash
# Stop all Forge services gracefully.
set -euo pipefail

USER="forge"

echo "=== Stopping Forge ==="

# Stop agents
for agent in pm dev review qc; do
  su - "$USER" -c "screen -S forge-${agent} -X quit" 2>/dev/null || true
  echo "Stopped forge-${agent}"
done

# Stop Django
su - "$USER" -c "screen -S forge-django -X quit" 2>/dev/null || true
echo "Stopped forge-django"

# Stop Xvfb
pkill -f "Xvfb :1" 2>/dev/null || true
echo "Stopped Xvfb"

echo "=== Forge stopped ==="
