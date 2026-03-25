#!/bin/bash
# Deploy Forge onto a BorealHost VPS container.
#
# Usage:
#   deploy-borealhost.sh                           # Run inside the container
#   deploy-borealhost.sh --ssh admin@host -p PORT  # Run remotely via SSH
#
# This script:
#   1. Installs git if missing
#   2. Clones the Forge repo to /opt/forge
#   3. Runs setup.sh (PostgreSQL, Python, Bun, Chrome, Claude Code, etc.)
#   4. Prints next steps for the user
set -euo pipefail

SSH_TARGET=""
SSH_PORT=""

while [[ $# -gt 0 ]]; do
  case $1 in
    --ssh) SSH_TARGET="$2"; shift 2 ;;
    -p)    SSH_PORT="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: deploy-borealhost.sh [--ssh user@host] [-p port]"
      echo ""
      echo "  --ssh  Deploy remotely via SSH (e.g., admin@184.107.179.134)"
      echo "  -p     SSH port (default: 22)"
      echo ""
      echo "Without --ssh, runs directly inside the current container."
      exit 0
      ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

DEPLOY_SCRIPT='#!/bin/bash
set -euo pipefail

echo "================================================"
echo "  Forge — Autonomous Development Platform"
echo "  Deployment starting..."
echo "================================================"
echo ""

# Must run as root
if [ "$(id -u)" -ne 0 ]; then
  echo "Error: run as root (sudo)"
  exit 1
fi

# Install git if missing
if ! command -v git &>/dev/null; then
  echo "Installing git..."
  apt update -qq && apt install -y -qq git >/dev/null 2>&1
fi

# Clone or update Forge
if [ ! -d /opt/forge ]; then
  echo "Cloning Forge..."
  git clone https://github.com/alainsvrd/forge.git /opt/forge
else
  echo "Updating Forge..."
  cd /opt/forge && git pull --ff-only
fi

# Run setup
echo ""
bash /opt/forge/setup.sh

echo ""
echo "================================================"
echo "  Forge deployed successfully!"
echo "================================================"
echo ""
echo "  Next steps:"
echo ""
echo "  1. Run: forge login"
echo "     → Authenticate with your Claude account or API key"
echo ""
echo "  2. Run: forge start"
echo "     → Launches the web UI + all 4 agents"
echo ""
echo "  3. Open the web UI:"
echo "     → http://<your-ip>:8100  (admin / admin)"
echo ""
echo "  4. Describe your project in PM Chat"
echo "     → Or run: forge pm  (to talk directly in terminal)"
echo ""
echo "  Run: forge help   for all commands"
echo ""
'

if [ -n "$SSH_TARGET" ]; then
  SSH_OPTS="-o StrictHostKeyChecking=no"
  [ -n "$SSH_PORT" ] && SSH_OPTS="$SSH_OPTS -p $SSH_PORT"

  echo "Deploying Forge to ${SSH_TARGET}..."
  echo ""
  # shellcheck disable=SC2086
  ssh $SSH_OPTS "$SSH_TARGET" "bash -s" <<< "$DEPLOY_SCRIPT"
else
  eval "$DEPLOY_SCRIPT"
fi
