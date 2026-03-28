#!/bin/bash
# Forge — First-run setup script
# Run this once inside a fresh container to set up the entire platform.
set -euo pipefail

FORGE_DIR="/opt/forge"
FORGE_USER="forge"
FORGE_DB="forge"
FORGE_DB_PASS="forge"
FORGE_DOMAIN="${FORGE_DOMAIN:-}"  # Set by installer or pass as env var

echo "=== Forge Setup ==="

# ── System dependencies ──
echo "[1/8] Installing system packages..."
apt update -qq
apt install -y -qq \
  postgresql postgresql-client \
  python3 python3-venv python3-pip \
  screen xvfb git curl unzip sudo \
  nginx \
  >/dev/null 2>&1

# ── Node.js + Bun + Claude Code ──
echo "[2/8] Installing Node.js, Bun, Claude Code..."
if ! command -v node &>/dev/null; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null 2>&1
  apt install -y -qq nodejs >/dev/null 2>&1
fi
if ! command -v bun &>/dev/null; then
  curl -fsSL https://bun.sh/install | bash >/dev/null 2>&1
fi
if ! command -v claude &>/dev/null; then
  npm install -g @anthropic-ai/claude-code >/dev/null 2>&1
fi

# ── Chrome + Playwright ──
echo "[3/8] Installing Chrome + Playwright..."
if ! command -v google-chrome-stable &>/dev/null; then
  curl -fsSL https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -o /tmp/chrome.deb
  apt install -y -qq /tmp/chrome.deb >/dev/null 2>&1 || true
  rm -f /tmp/chrome.deb
fi
npx playwright install chromium >/dev/null 2>&1 || true

# ── Create forge user ──
echo "[4/8] Creating forge user..."
if ! id "$FORGE_USER" &>/dev/null; then
  useradd -m -s /bin/bash "$FORGE_USER"
  echo "${FORGE_USER} ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/forge
fi

# Install Bun for forge user
if [ ! -f "/home/${FORGE_USER}/.bun/bin/bun" ]; then
  su - "$FORGE_USER" -c "curl -fsSL https://bun.sh/install | bash" >/dev/null 2>&1
fi

# Set ownership early — git clone runs as root, but forge user needs to write
chown -R "${FORGE_USER}:${FORGE_USER}" "${FORGE_DIR}"

# ── PostgreSQL ──
echo "[5/8] Setting up PostgreSQL..."
# Ensure running
systemctl start postgresql || pg_ctlcluster 17 main start || true

# Switch to md5 auth for local connections
sed -i 's/^local   all             all                                     peer$/local   all             all                                     md5/' \
  /etc/postgresql/*/main/pg_hba.conf 2>/dev/null || true
systemctl reload postgresql || pg_ctlcluster 17 main reload || true

# Create user and database
sudo -u postgres psql -c "CREATE USER ${FORGE_USER} WITH PASSWORD '${FORGE_DB_PASS}';" 2>/dev/null || true
sudo -u postgres psql -c "CREATE DATABASE ${FORGE_DB} OWNER ${FORGE_USER};" 2>/dev/null || true

# ── Python venv + Django ──
echo "[6/8] Setting up Django..."
cd "$FORGE_DIR"
if [ ! -d "venv" ]; then
  python3 -m venv venv
fi
source venv/bin/activate
pip install -q django psycopg2-binary gunicorn uvicorn

# Run migrations
cd ui
FORGE_DB_PASSWORD="$FORGE_DB_PASS" python manage.py migrate --no-input

# Create admin user (skip if exists)
FORGE_DB_PASSWORD="$FORGE_DB_PASS" python manage.py shell -c "
from django.contrib.auth.models import User
if not User.objects.filter(username='admin').exists():
    User.objects.create_superuser('admin', 'admin@forge.local', 'admin')
    print('Created admin user (password: admin)')
else:
    print('Admin user already exists')
"

# Create default project
FORGE_DB_PASSWORD="$FORGE_DB_PASS" python manage.py shell -c "
from core.models import Project
if not Project.objects.exists():
    Project.objects.create(name='Forge Project', status='planning')
    print('Created default project')
else:
    print('Project already exists')
"

# ── Bun dependencies ──
echo "[7/8] Installing MCP SDK..."
cd "$FORGE_DIR"
su - "$FORGE_USER" -c "cd ${FORGE_DIR} && /home/${FORGE_USER}/.bun/bin/bun install" >/dev/null 2>&1

# ── Workspace init ──
echo "[8/9] Initializing workspace..."
mkdir -p "${FORGE_DIR}/workspace/.forge/screenshots"

# Ensure .mcp.json exists (may be missing if workspace was a git submodule)
if [ ! -f "${FORGE_DIR}/workspace/.mcp.json" ]; then
  cat > "${FORGE_DIR}/workspace/.mcp.json" <<'MCPEOF'
{
  "mcpServers": {
    "forge-pm-channel": { "command": "/home/forge/.bun/bin/bun", "args": ["/opt/forge/forge-channel.ts", "--type", "pm"] },
    "forge-dev-channel": { "command": "/home/forge/.bun/bin/bun", "args": ["/opt/forge/forge-channel.ts", "--type", "dev"] },
    "forge-review-channel": { "command": "/home/forge/.bun/bin/bun", "args": ["/opt/forge/forge-channel.ts", "--type", "review"] },
    "forge-qc-channel": { "command": "/home/forge/.bun/bin/bun", "args": ["/opt/forge/forge-channel.ts", "--type", "qc"] }
  }
}
MCPEOF
fi

# Ensure CLAUDE.md exists
if [ ! -f "${FORGE_DIR}/workspace/CLAUDE.md" ]; then
  cat > "${FORGE_DIR}/workspace/CLAUDE.md" <<'CLEOF'
# Project

## Forge Infrastructure — DO NOT MODIFY
- Forge UI: port 8100, database "forge", code in /opt/forge/
- Forge screens: forge-pm, forge-dev, forge-review, forge-qc
- Never bind to port 8100 or alter /opt/forge/.
CLEOF
fi

chown -R "${FORGE_USER}:${FORGE_USER}" "${FORGE_DIR}/workspace"

# Init git repo for workspace (separate from forge repo)
cd "${FORGE_DIR}/workspace"
if [ ! -d ".git" ] || [ -f ".git" ]; then
  rm -rf .git  # remove submodule pointer if present
  su - "$FORGE_USER" -c "cd ${FORGE_DIR}/workspace && git init && git add -A && git commit -m 'Initial workspace'" >/dev/null 2>&1
fi

# ── Generate secrets + config ──
FORGE_SECRET=$(openssl rand -hex 32)

# Auto-detect domain if not provided
if [ -z "$FORGE_DOMAIN" ]; then
  # Try to read from existing .env
  FORGE_DOMAIN=$(grep -oP 'FORGE_DOMAIN=\K.*' "${FORGE_DIR}/ui/.env" 2>/dev/null || true)
fi
if [ -z "$FORGE_DOMAIN" ]; then
  # Try hostname-based guess
  FORGE_DOMAIN=$(hostname -f 2>/dev/null || hostname 2>/dev/null || echo "localhost")
fi

cat > "${FORGE_DIR}/ui/.env" <<EOF
FORGE_SECRET=${FORGE_SECRET}
FORGE_DB_PASSWORD=${FORGE_DB_PASS}
FORGE_DOMAIN=${FORGE_DOMAIN}
EOF
chmod 600 "${FORGE_DIR}/ui/.env"

# ── Claude Code settings for forge user ──
mkdir -p "/home/${FORGE_USER}/.claude"
cat > "/home/${FORGE_USER}/.claude/settings.json" <<'EOF'
{
  "enableAllProjectMcpServers": true,
  "skipDangerousModePermissionPrompt": true,
  "permissions": {
    "allow": ["Bash(*)", "Read(*)", "Write(*)", "Edit(*)", "Glob(*)", "Grep(*)"]
  }
}
EOF
chown -R "${FORGE_USER}:${FORGE_USER}" "/home/${FORGE_USER}/.claude"

# ── Install forge CLI ──
echo "[9/9] Installing forge CLI..."
cp "${FORGE_DIR}/forge-cli.sh" /usr/local/bin/forge
chmod +x /usr/local/bin/forge

# ── Permissions ──
chown -R "${FORGE_USER}:${FORGE_USER}" "${FORGE_DIR}"

# ── Systemd (registered but NOT started — user must auth first) ──
cp "${FORGE_DIR}/forge.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable forge

echo ""
echo "=== Forge setup complete ==="
echo ""
echo "  Run: forge login   → authenticate Claude Code"
echo "  Run: forge start   → launch all services"
echo "  Run: forge help    → see all commands"
