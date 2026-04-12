# Forge Setup Guide

Forge is an autonomous development platform that runs 4 Claude Code agents (PM, Dev, Review, QC) as `claude -p` subprocesses with a Django web UI for live monitoring and chat.

## Prerequisites

- Ubuntu 22.04+ (tested on 24.04 LXC container)
- Root access
- Anthropic API key or Claude Code OAuth login
- At least 4GB RAM, 2 CPU cores, 20GB disk

## Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/alainsvrd/forge.git /opt/forge
cd /opt/forge

# 2. Run first-time setup
bash setup.sh

# 3. Authenticate Claude Code
forge login

# 4. Start everything
forge start
```

The UI is at `http://localhost:8100` (login: admin / admin).

---

## What `setup.sh` Does

The setup script is idempotent -- safe to run multiple times.

1. **System packages**: PostgreSQL, Python 3, Node.js, Chrome, Nginx, screen, Xvfb
2. **Claude Code**: installed globally via npm
3. **Chrome + browser-use CLI**: for QC agent visual testing
4. **Forge user**: creates `forge` system user with sudo access
5. **Bun**: installed for the forge user (runs the MCP TypeScript server)
6. **PostgreSQL**: creates `forge` database (UTF-8) and user
7. **Python venv + Django**: installs dependencies, runs migrations, creates admin user
8. **MCP SDK**: `bun install` for the TypeScript MCP server
9. **Workspace**: initializes `/opt/forge/workspace/` with git repo and `CLAUDE.md`
10. **Config**: generates `.env` with secrets, sets up Claude Code permissions
11. **Systemd**: registers `forge.service` for auto-start on boot

---

## Architecture

```
Browser (user)
    |
    | HTTP (:8100)
    v
Nginx (reverse proxy, optional)
    |
    v
Django/Gunicorn (1 worker, uvicorn async)
    |
    |--- ClaudeCodeManager singleton
    |       |-- PM agent   (claude -p subprocess)
    |       |-- Dev agent  (claude -p subprocess)
    |       |-- Review agent (claude -p subprocess)
    |       '-- QC agent   (claude -p subprocess)
    |
    |--- PostgreSQL (forge DB)
    |       |-- Project, Task, ChatMessage
    |       '-- AgentSession (per-agent state, messages, tool_calls)
    |
    '--- MCP Server (forge-mcp-server.ts, spawned by each agent)
            |-- task_create, task_update, chat_reply
            |-- check_agents, nudge_agent, list_tasks (PM only)
            '-- Any external MCP servers (BorealHost, etc.)
```

### How it works

1. **User sends chat** via the web UI
2. Django API receives the message and writes it directly to PM agent's stdin
3. PM agent processes it, calls `chat_reply` (MCP tool) to respond, and `task_create` to delegate work
4. `task_create` API auto-delivers the task to the target agent's stdin
5. Agents hand off via `task_update` + `task_create`: Dev -> Review -> QC -> PM
6. The UI polls `/api/agents/status/` every 600ms for live updates

### Key files

| File | Purpose |
|------|---------|
| `ui/core/claude_manager.py` | Subprocess manager: spawns agents, parses stream-json events, manages lifecycle |
| `forge-mcp-server.ts` | MCP tool server: task_create, task_update, chat_reply + PM coordination tools |
| `ui/core/models.py` | Django models: Project, Task, ChatMessage, AgentSession |
| `ui/core/views.py` | API endpoints: agent status, task CRUD, chat, MCP activity |
| `ui/core/templates/core/dashboard.html` | Unified single-page UI: chat + agent panels + MCP activity + tasks |
| `prompts/{pm,dev,review,qc}.md` | System prompts for each agent role |
| `ui/gunicorn.conf.py` | Gunicorn config (must be 1 worker for singleton manager) |

---

## Adding MCP Servers

Forge agents can use any MCP server. External MCPs are configured in `ui/core/claude_manager.py` in the `start_agent()` method where the per-agent MCP config is generated.

### HTTP MCP servers (like BorealHost)

Add to the `mcp_config["mcpServers"]` dict in `claude_manager.py`:

```python
"borealhost": {
    "type": "http",
    "url": "https://borealhost.ai/mcp/",
    "headers": {
        "Authorization": f"Bearer {cls._get_borealhost_key()}",
    }
}
```

Store API keys in `/opt/forge/ui/.env`:

```
BOREALHOST_API_KEY=bh_your_key_here
```

### Stdio MCP servers (local processes)

```python
"my-server": {
    "command": "/path/to/binary",
    "args": ["--flag", "value"],
    "env": {
        "API_KEY": "...",
    }
}
```

### After adding an MCP server

1. Restart gunicorn and agents for changes to take effect
2. The new MCP tools will appear in agent tool lists automatically
3. Update agent prompts (`prompts/*.md`) to teach agents about the new tools
4. Optionally add tool names to `--allowedTools` if you want to restrict access

### Currently configured MCPs

| MCP | Type | Purpose |
|-----|------|---------|
| `forge-{agent}` | stdio | Task management, chat, coordination tools |
| `borealhost` | http | Web hosting, DNS, deployment, snapshots |

---

## Configuration

### Environment variables (`/opt/forge/ui/.env`)

```
FORGE_SECRET=<random 64-char hex>     # API auth between MCP server and Django
FORGE_DB_PASSWORD=forge               # PostgreSQL password
FORGE_DOMAIN=mysite.borealhost.ai     # Public domain (for CSRF, nginx)
BOREALHOST_API_KEY=bh_...             # Optional: BorealHost MCP key
```

### Gunicorn (`ui/gunicorn.conf.py`)

```python
bind = '0.0.0.0:8100'
workers = 1          # MUST be 1 — ClaudeCodeManager is a singleton
worker_class = 'uvicorn.workers.UvicornWorker'
```

### Nginx (optional reverse proxy)

To serve Forge on port 80 with a subdomain:

```nginx
server {
    listen 80;
    server_name forge.mysite.borealhost.ai;

    location / {
        proxy_pass http://127.0.0.1:8100;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
        proxy_buffering off;
    }
}
```

---

## CLI Commands

```bash
forge login          # Authenticate Claude Code (interactive OAuth)
forge start          # Start all services (Xvfb, Django, agents)
forge stop           # Stop everything
forge status         # Show agent status and task counts
forge pm|dev|review|qc  # Attach to agent screen session (legacy, for debugging)
```

---

## Automation Script

Below is a complete script to deploy Forge on a fresh Ubuntu container with BorealHost MCP configured:

```bash
#!/bin/bash
# Automated Forge deployment
# Usage: ANTHROPIC_API_KEY=sk-... BOREALHOST_API_KEY=bh_... bash deploy-forge.sh
set -euo pipefail

FORGE_DOMAIN="${FORGE_DOMAIN:-$(hostname -f)}"

# Clone and setup
git clone https://github.com/alainsvrd/forge.git /opt/forge
cd /opt/forge
bash setup.sh

# Add BorealHost API key if provided
if [ -n "${BOREALHOST_API_KEY:-}" ]; then
  echo "BOREALHOST_API_KEY=${BOREALHOST_API_KEY}" >> /opt/forge/ui/.env
  echo "BorealHost MCP configured"
fi

# Set Anthropic API key for non-interactive auth
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  mkdir -p /home/forge/.claude
  cat > /home/forge/.claude/.credentials.json <<EOF
{"apiKey":"${ANTHROPIC_API_KEY}"}
EOF
  chown -R forge:forge /home/forge/.claude
  echo "API key configured"
fi

# Setup nginx reverse proxy
cat > /etc/nginx/sites-available/forge <<EOF
server {
    listen 80;
    server_name ${FORGE_DOMAIN} forge.${FORGE_DOMAIN};

    location / {
        proxy_pass http://127.0.0.1:8100;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
        proxy_buffering off;
    }
}
EOF
ln -sf /etc/nginx/sites-available/forge /etc/nginx/sites-enabled/forge
nginx -t && nginx -s reload

# Start Forge
systemctl start forge

echo ""
echo "Forge is running!"
echo "  UI: http://${FORGE_DOMAIN}/"
echo "  Login: admin / admin"
echo "  Agents: starting up (check 'forge status')"
```

### Environment variables for the automation script

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes (or use `forge login`) | Claude API key for agent LLM calls |
| `BOREALHOST_API_KEY` | No | BorealHost MCP key for hosting/deploy tools |
| `FORGE_DOMAIN` | No | Public domain (defaults to hostname) |

---

## Restarting After Code Changes

The gunicorn worker caches Python modules. After editing backend files:

```bash
# Kill everything and restart clean
kill -9 $(pgrep -f "gunicorn|uvicorn|claude.*stream-json") 2>/dev/null
sleep 2

# Clear Python cache
find /opt/forge/ui -name "__pycache__" -exec rm -rf {} + 2>/dev/null

# Start gunicorn from /opt/forge/ui
cd /opt/forge/ui
/opt/forge/venv/bin/gunicorn forge_ui.asgi:application -c gunicorn.conf.py -D

# Restart agents via API
SECRET=$(grep -oP 'FORGE_SECRET=\K.*' /opt/forge/ui/.env)
for agent in pm dev review qc; do
  curl -s -X POST "http://localhost:8100/api/agents/$agent/start/" \
    -H "X-Forge-Secret: $SECRET" > /dev/null
done
```

For template-only changes, a hard browser refresh (Ctrl+Shift+R) is sufficient.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Agents show "stopped" after restart | Start them via UI buttons or API (`POST /api/agents/{type}/start/`) |
| Old template in browser | Hard refresh (Ctrl+Shift+R) or incognito window |
| gunicorn won't bind to :8100 | Check `cwd` is `/opt/forge/ui` when starting gunicorn |
| Chat messages don't appear | Check PM agent is alive, check browser console for polling errors |
| Task not delivered to agent | Verify agent is alive via `/api/agents/status/`, check gunicorn logs |
| SQL encoding errors | Ensure database is UTF-8: `SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname='forge'` |
