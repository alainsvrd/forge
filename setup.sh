#!/bin/bash
# Forge — First-run setup script
# Run this once inside a fresh container to set up the entire platform.
set -euo pipefail

FORGE_DIR="/opt/forge"
FORGE_USER="forge"
FORGE_DB="forge"
FORGE_DB_PASS="forge"

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
echo "[8/8] Initializing workspace..."
cd "${FORGE_DIR}/workspace"
if [ ! -d ".git" ]; then
  su - "$FORGE_USER" -c "cd ${FORGE_DIR}/workspace && git init && git add -A && git commit -m 'Initial workspace'" >/dev/null 2>&1
fi

# Create .forge directory for screenshots etc.
mkdir -p "${FORGE_DIR}/workspace/.forge/screenshots"
chown -R "${FORGE_USER}:${FORGE_USER}" "${FORGE_DIR}/workspace"

# ── Generate secrets ──
FORGE_SECRET=$(openssl rand -hex 32)
cat > "${FORGE_DIR}/ui/.env" <<EOF
FORGE_SECRET=${FORGE_SECRET}
FORGE_DB_PASSWORD=${FORGE_DB_PASS}
EOF
chmod 600 "${FORGE_DIR}/ui/.env"

# ── Claude Code settings for forge user ──
mkdir -p "/home/${FORGE_USER}/.claude"
cat > "/home/${FORGE_USER}/.claude/settings.json" <<'EOF'
{
  "enableAllProjectMcpServers": true,
  "permissions": {
    "allow": ["Bash(*)", "Read(*)", "Write(*)", "Edit(*)", "Glob(*)", "Grep(*)"]
  }
}
EOF
chown -R "${FORGE_USER}:${FORGE_USER}" "/home/${FORGE_USER}/.claude"

# ── Permissions ──
chown -R "${FORGE_USER}:${FORGE_USER}" "${FORGE_DIR}"

# ── Systemd ──
cp "${FORGE_DIR}/forge.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable forge

echo ""
echo "=== Forge setup complete ==="
echo "  UI:       http://localhost:8100"
echo "  Login:    admin / admin"
echo "  Start:    systemctl start forge"
echo "  Manual:   ${FORGE_DIR}/start-all.sh"
echo ""
echo "IMPORTANT: Set up Claude Code authentication for the forge user:"
echo "  su - forge -c 'claude login'"
echo "  # Or inject .credentials.json for Max subscription"
