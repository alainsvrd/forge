#!/bin/bash
# Forge CLI — manage the autonomous development platform
# Installed at /usr/local/bin/forge
set -euo pipefail

FORGE_DIR="/opt/forge"
FORGE_USER="forge"

case "${1:-help}" in
  pm|dev|review|qc)
    AGENT="$1"
    SESSION="forge-${AGENT}"
    # Check if session exists
    if ! su - "$FORGE_USER" -c "screen -ls" 2>&1 | grep -q "$SESSION"; then
      echo "Agent '${AGENT}' is not running."
      echo "Run 'forge start' first."
      exit 1
    fi
    echo "Attaching to ${AGENT} agent. Press Ctrl-A D to detach."
    exec su - "$FORGE_USER" -c "screen -r ${SESSION}"
    ;;

  status)
    echo "=== Forge Status ==="
    echo ""
    echo "Agents:"
    SCREENS=$(su - "$FORGE_USER" -c "screen -ls" 2>&1 || true)
    for agent in pm dev review qc django; do
      if echo "$SCREENS" | grep -q "forge-${agent}"; then
        echo "  forge-${agent}: running"
      else
        echo "  forge-${agent}: stopped"
      fi
    done
    echo ""
    # Show task status from API
    if curl -sf http://localhost:8100/api/status/ >/dev/null 2>&1; then
      echo "Tasks:"
      curl -sf http://localhost:8100/api/status/ | python3 -c "
import sys, json
d = json.load(sys.stdin)
counts = d.get('task_counts', {})
for status, count in counts.items():
    print(f'  {status}: {count}')
active = d.get('active_task')
if active:
    print(f'\nActive: [{active[\"type\"]}] #{active[\"id\"]} {active[\"title\"]}')
else:
    print('\nNo active task')
" 2>/dev/null || true
    else
      echo "Django not running — no task data available"
    fi
    ;;

  start)
    # Check Claude Code auth
    if [ ! -f "/home/${FORGE_USER}/.claude/.credentials.json" ] && \
       [ -z "${ANTHROPIC_API_KEY:-}" ]; then
      echo "Claude Code is not authenticated."
      echo ""
      echo "Run 'forge login' first to authenticate, then try 'forge start' again."
      exit 1
    fi
    exec "${FORGE_DIR}/start-all.sh"
    ;;

  stop)
    exec "${FORGE_DIR}/stop-all.sh"
    ;;

  login)
    echo "=== Claude Code Authentication ==="
    echo ""
    echo "This authenticates Claude Code for all Forge agents."
    echo "You can use either:"
    echo "  - A Claude Max/Pro subscription (OAuth login)"
    echo "  - An Anthropic API key"
    echo ""
    echo "Follow the prompts below."
    echo ""
    su - "$FORGE_USER" -c "claude login"
    echo ""
    if [ -f "/home/${FORGE_USER}/.claude/.credentials.json" ]; then
      echo "Authentication successful!"
      echo "You can now run 'forge start' to launch all agents."
    else
      echo "Authentication may have failed. Try again with 'forge login'."
    fi
    ;;

  logs)
    echo "=== Forge Logs ==="
    echo "(Press Ctrl-C to stop)"
    echo ""
    # Try Django screen log, fall back to journalctl
    if su - "$FORGE_USER" -c "screen -ls" 2>&1 | grep -q "forge-django"; then
      echo "Attaching to Django output... (Ctrl-A D to detach)"
      su - "$FORGE_USER" -c "screen -r forge-django"
    else
      journalctl -u forge -f 2>/dev/null || echo "No logs available. Is Forge running?"
    fi
    ;;

  help|--help|-h|*)
    cat <<'HELP'
Forge — Autonomous Development Platform

Usage: forge <command>

Getting Started:
  login     Authenticate Claude Code (run this first!)
  start     Start all agents + Django UI
  stop      Stop everything

Agent Sessions:
  pm        Attach to PM agent (Ctrl-A D to detach)
  dev       Attach to Dev agent
  review    Attach to Review agent
  qc        Attach to QC agent

Monitoring:
  status    Show running agents and task counts
  logs      View Django/Forge logs

Workflow:
  1. forge login        → authenticate with your Claude account
  2. forge start        → launch all services
  3. Open the web UI    → http://localhost:8100 (admin/admin)
  4. Talk to the PM     → describe what you want to build
  5. forge dev          → watch the dev agent work (optional)
HELP
    ;;
esac
