#!/bin/bash
# Start a Forge agent in a detached screen session.
# Usage: start-agent.sh <pm|dev|review|qc>
set -euo pipefail

TYPE="${1:?Usage: start-agent.sh <pm|dev|review|qc>}"
SESSION="forge-${TYPE}"
WORKDIR="/opt/forge/workspace"
USER="forge"
FORGE_DIR="/opt/forge"

# Validate type
case "$TYPE" in
  pm|dev|review|qc) ;;
  *) echo "Invalid type: $TYPE (must be pm|dev|review|qc)"; exit 1 ;;
esac

# Kill existing session and orphan MCP channel processes
su - "$USER" -c "screen -S $SESSION -X quit" 2>/dev/null || true
pkill -f "forge-channel.ts --type $TYPE" 2>/dev/null || true
sleep 1

# Build environment
ENV_VARS="FORGE_SECRET=$(grep -oP 'FORGE_SECRET=\K.*' ${FORGE_DIR}/ui/.env 2>/dev/null || echo forge-dev-secret)"
if [ "$TYPE" = "qc" ]; then
  ENV_VARS="$ENV_VARS DISPLAY=:1 PATH=${FORGE_DIR}/browser-use-venv/bin:\$PATH"
fi

# Write per-agent MCP config (only this agent's channel server)
MCP_CONFIG="${FORGE_DIR}/mcp-${TYPE}.json"
cat > "$MCP_CONFIG" <<MCPEOF
{"mcpServers":{"forge-${TYPE}-channel":{"command":"/home/forge/.bun/bin/bun","args":["${FORGE_DIR}/forge-channel.ts","--type","${TYPE}"]}}}
MCPEOF
chown "$USER":"$USER" "$MCP_CONFIG"

# Launch Claude Code in a screen session
su - "$USER" -c "screen -dmS $SESSION bash -c '\
  cd $WORKDIR && \
  export $ENV_VARS && \
  exec claude \
    --permission-mode bypassPermissions \
    --model sonnet \
    --system-prompt ${FORGE_DIR}/prompts/${TYPE}.md \
    --mcp-config ${MCP_CONFIG} \
    --strict-mcp-config \
    --dangerously-load-development-channels server:forge-${TYPE}-channel \
    -n forge-${TYPE}'"

# Auto-accept prompts:
# 1. Bypass permissions prompt — select option 2 ("Yes, I accept") then Enter
#    (skipped if skipDangerousModePermissionPrompt=true in settings.json)
# 2. Development channels warning — press Enter to confirm
sleep 5
# Navigate to option 2 (down arrow) and confirm
su - "$USER" -c "screen -S $SESSION -p 0 -X stuff $'\033[B\r'" 2>/dev/null || true
sleep 5
# Accept dev channels warning (Enter)
su - "$USER" -c "screen -S $SESSION -p 0 -X stuff \"\r\""

# Send initial prompt to trigger MCP server lazy init
sleep 10
su - "$USER" -c "screen -S $SESSION -p 0 -X stuff \"You are Forge ${TYPE}. Your channel server will push tasks to you. Wait for them.\r\""

echo "Forge ${TYPE} started. Attach: su - ${USER} -c 'screen -r ${SESSION}'"
