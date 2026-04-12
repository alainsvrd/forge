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
DEBIAN_FRONTEND=noninteractive apt install -y -qq \
  postgresql postgresql-client \
  python3 python3-venv python3-pip python3-dev \
  libpq-dev build-essential \
  screen xvfb git curl unzip sudo \
  dbus-x11 \
  nginx \
  >/dev/null 2>&1

# ── Node.js + Claude Code ──
echo "[2/8] Installing Node.js, Claude Code..."
if ! command -v node &>/dev/null; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null 2>&1
  DEBIAN_FRONTEND=noninteractive apt install -y -qq nodejs >/dev/null 2>&1
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

# ── browser-use CLI (for QC agent) ──
echo "[3b/8] Installing browser-use CLI..."
if [ ! -d "${FORGE_DIR}/browser-use-venv" ]; then
  python3 -m venv "${FORGE_DIR}/browser-use-venv"
fi
"${FORGE_DIR}/browser-use-venv/bin/pip" install -q browser-use==0.12.2
chown -R "${FORGE_USER}:${FORGE_USER}" "${FORGE_DIR}/browser-use-venv"

# ── Create forge user ──
echo "[4/8] Creating forge user..."
if ! id "$FORGE_USER" &>/dev/null; then
  useradd -m -s /bin/bash "$FORGE_USER"
  echo "${FORGE_USER} ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/forge
fi

# Set ownership early — git clone runs as root, but forge user needs to write
chown -R "${FORGE_USER}:${FORGE_USER}" "${FORGE_DIR}"

# Install Bun for forge user (NOT root — .mcp.json references /home/forge/.bun/bin/bun)
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
sudo -u postgres psql -c "CREATE DATABASE ${FORGE_DB} OWNER ${FORGE_USER} ENCODING 'UTF8' LC_COLLATE='C.utf8' LC_CTYPE='C.utf8' TEMPLATE=template0;" 2>/dev/null || \
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

# Ensure CLAUDE.md exists
if [ ! -f "${FORGE_DIR}/workspace/CLAUDE.md" ]; then
  cat > "${FORGE_DIR}/workspace/CLAUDE.md" <<'CLEOF'
# Project

## Forge Infrastructure — DO NOT MODIFY
- Forge UI: port 8100, database "forge", code in /opt/forge/
- Agents managed by ClaudeCodeManager (not screen sessions)
- Never bind to port 8100 or alter /opt/forge/.
CLEOF
fi

chown -R "${FORGE_USER}:${FORGE_USER}" "${FORGE_DIR}/workspace"

# Init git repo for workspace (separate from forge repo)
cd "${FORGE_DIR}/workspace"
if [ ! -d ".git" ] || [ -f ".git" ]; then
  rm -rf .git  # remove submodule pointer if present
  su - "$FORGE_USER" -c "cd ${FORGE_DIR}/workspace && git init && git add -A && git commit -m 'Initial workspace'" >/dev/null 2>&1 || true
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

# ── BorealHost integration (auto-detect if running on BorealHost) ──
BOREALHOST_SLUG="${BOREALHOST_SLUG:-}"
BOREALHOST_API_KEY="${BOREALHOST_API_KEY:-}"
PUBLIC_IP=$(curl -sf --max-time 5 ifconfig.me 2>/dev/null || echo "")

# Auto-detect slug from domain (e.g. mysite.borealhost.ai -> mysite)
if [ -z "$BOREALHOST_SLUG" ] && echo "$FORGE_DOMAIN" | grep -q "borealhost.ai"; then
  BOREALHOST_SLUG=$(echo "$FORGE_DOMAIN" | sed 's/\..*//' | sed 's/.*\.//')
  echo "Detected BorealHost site: ${BOREALHOST_SLUG}"
fi

# Claim API key via challenge-response if on a BorealHost container
if [ -n "$BOREALHOST_SLUG" ] && [ -z "$BOREALHOST_API_KEY" ]; then
  echo "Claiming BorealHost API key for site '${BOREALHOST_SLUG}'..."
  # Step 1: Request claim token
  CLAIM_RESP=$(curl -sf --max-time 10 \
    -X POST "https://borealhost.ai/mcp/" \
    -H "Content-Type: application/json" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"request_api_key\",\"arguments\":{\"site_slug\":\"${BOREALHOST_SLUG}\"}}}" \
    2>/dev/null || true)

  if echo "$CLAIM_RESP" | grep -q "pending"; then
    sleep 2
    # Step 2: Read claim token from container
    CLAIM_TOKEN=$(cat /home/admin/.borealhost/.claim_token 2>/dev/null || cat ~/.borealhost/.claim_token 2>/dev/null || true)
    if [ -n "$CLAIM_TOKEN" ]; then
      # Step 3: Claim the key
      KEY_RESP=$(curl -sf --max-time 10 \
        -X POST "https://borealhost.ai/mcp/" \
        -H "Content-Type: application/json" \
        -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"claim_api_key\",\"arguments\":{\"claim_token\":\"${CLAIM_TOKEN}\"}}}" \
        2>/dev/null || true)
      BOREALHOST_API_KEY=$(echo "$KEY_RESP" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    content = d.get('result',{}).get('content',[{}])
    text = content[0].get('text','') if content else ''
    parsed = json.loads(text) if text.startswith('{') else {}
    print(parsed.get('api_key',''))
except: pass
" 2>/dev/null || true)
      if [ -n "$BOREALHOST_API_KEY" ]; then
        echo "BorealHost API key claimed: ${BOREALHOST_API_KEY:0:12}..."
      else
        echo "Warning: BorealHost key claim failed. Set BOREALHOST_API_KEY manually in .env"
      fi
    else
      echo "Warning: Could not read claim token. Set BOREALHOST_API_KEY manually in .env"
    fi
  else
    echo "Warning: BorealHost claim request failed. Set BOREALHOST_API_KEY manually in .env"
  fi
fi

# Append BorealHost key to .env if available
if [ -n "$BOREALHOST_API_KEY" ]; then
  echo "BOREALHOST_API_KEY=${BOREALHOST_API_KEY}" >> "${FORGE_DIR}/ui/.env"
fi

# Generate Claude Code memory files for BorealHost
if [ -n "$BOREALHOST_SLUG" ]; then
  MEMORY_DIR="/root/.claude/projects/-root/memory"
  mkdir -p "$MEMORY_DIR"

  cat > "${MEMORY_DIR}/reference_borealhost_mcp.md" <<BHEOF
---
name: BorealHost MCP Reference
description: BorealHost hosting platform MCP tools — 46 tools for DNS, deploy, files, snapshots. Site slug is "${BOREALHOST_SLUG}", domain is ${FORGE_DOMAIN}, public IP is ${PUBLIC_IP}.
type: reference
---

BorealHost MCP is connected and authenticated (API key in \`/opt/forge/ui/.env\` as \`BOREALHOST_API_KEY\`).

## This Container

- **Site slug**: \`${BOREALHOST_SLUG}\`
- **Domain**: \`${FORGE_DOMAIN}\`
- **Public IP**: \`${PUBLIC_IP}\`
- **Nginx**: reverse proxy on port 80, configs in \`/etc/nginx/sites-enabled/\`
- **Forge UI**: \`forge.${FORGE_DOMAIN}\` -> localhost:8100

## Key Tools by Use Case

### Serve an app on a subdomain
1. \`manage_dns(slug="${BOREALHOST_SLUG}", action="create", record_type="A", subdomain="myapp", value="${PUBLIC_IP}")\`
2. Add nginx server block for \`myapp.${FORGE_DOMAIN}\` -> localhost:<PORT>
3. \`nginx -t && nginx -s reload\`

### Deploy files to the web root
- Copy to \`/var/www/html/\` — served at \`https://${FORGE_DOMAIN}/\`

### Snapshots (backup before risky changes)
- \`create_snapshot(slug="${BOREALHOST_SLUG}", description="before deploy")\`
- \`rollback_snapshot(slug="${BOREALHOST_SLUG}", snapshot_id="...")\`

### DNS management
- \`manage_dns(slug="${BOREALHOST_SLUG}", action="create", record_type="A", subdomain="app", value="${PUBLIC_IP}")\`
- \`domain_detail(domain_name="${FORGE_DOMAIN}")\` — see all DNS records

### Monitoring
- \`get_logs(slug="${BOREALHOST_SLUG}", log_type="error")\`
- \`get_site_status(slug="${BOREALHOST_SLUG}")\`
- \`get_metrics(slug="${BOREALHOST_SLUG}")\`
BHEOF

  cat > "${MEMORY_DIR}/feedback_deploy_apps.md" <<'DEPEOF'
---
name: Auto-deploy apps to subdomains
description: When building web apps, proactively set up a subdomain and nginx proxy so the user can access it immediately.
type: feedback
---

When a Forge agent builds a web app, proactively make it accessible from the internet:

1. Create a DNS A record via BorealHost MCP
2. Create an nginx reverse proxy config on the container
3. Tell the user the live URL immediately

**Why:** The user should never need to ask "how can I see it?" — provide the URL proactively.

**How to apply:** For static HTML apps, copy to /var/www/html/. For apps on custom ports, create a subdomain + nginx proxy. Always include the URL in the chat_reply.
DEPEOF

  cat > "${MEMORY_DIR}/feedback_snapshots_before_changes.md" <<'SNAPEOF'
---
name: Snapshot before risky changes
description: Always create a BorealHost snapshot before destructive operations like overwriting web roots or changing nginx config.
type: feedback
---

Before any operation that could break the running site, create a snapshot via BorealHost MCP.

**Why:** Rollback is instant. Without a snapshot, recovery from a bad deploy requires manual repair.

**How to apply:** Snapshot before: overwriting /var/www/html/, changing nginx configs, database migrations, or any deploy that replaces live content.
SNAPEOF

  # Update MEMORY.md index
  cat > "${MEMORY_DIR}/MEMORY.md" <<'MEMEOF'
- [Forge Platform Overview](project_forge_overview.md) — Autonomous dev platform: 4 claude -p agents, ClaudeCodeManager, Django UI at :8100
- [BorealHost MCP Reference](reference_borealhost_mcp.md) — Hosting tools: DNS, deploy, files, snapshots with site-specific config
- [Auto-deploy apps](feedback_deploy_apps.md) — Proactively set up subdomains + nginx for web apps
- [Snapshot before changes](feedback_snapshots_before_changes.md) — Always snapshot before destructive operations
MEMEOF

  echo "BorealHost memory files generated for site '${BOREALHOST_SLUG}'"
fi

# Also update agent prompts with site-specific values
if [ -n "$BOREALHOST_SLUG" ] && [ -n "$PUBLIC_IP" ]; then
  # Patch prompts with actual site values
  sed -i "s/SITE_SLUG/${BOREALHOST_SLUG}/g" "${FORGE_DIR}/prompts/pm.md"
  sed -i "s/SITE_DOMAIN/${FORGE_DOMAIN}/g" "${FORGE_DIR}/prompts/pm.md"
  sed -i "s/SITE_IP/${PUBLIC_IP}/g" "${FORGE_DIR}/prompts/pm.md"
  sed -i "s/SITE_DOMAIN/${FORGE_DOMAIN}/g" "${FORGE_DIR}/prompts/dev.md"
fi

# ── Claude Code settings for forge user ──
mkdir -p "/home/${FORGE_USER}/.claude"
cat > "/home/${FORGE_USER}/.claude/settings.json" <<'EOF'
{
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
