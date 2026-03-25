# Forge — Autonomous Closed-Loop Development Platform

Forge is a self-contained development platform that runs 4 specialized Claude Code
agents in a single container. You describe what you want to build, and the agents
collaboratively develop it — writing code, reviewing it, testing it visually, and
iterating until it works.

## Why Forge Exists

BorealHost.ai runs an internal multi-agent development system spread across 5+
containers (Kodiak for coding, Husky for E2E review, QC agent for visual testing,
Corvus for coordination, plus Django APIs and provisioner containers). It works,
but the complexity — inter-container communication via `pct exec`, shared-reports
mounts, MCP over screen sessions, webhooks, separate databases — makes it hard to
replicate or offer as a product.

Forge packages the same closed-loop pattern into **one container** that anyone can
deploy. The same development cycle (code → review → visual QC → iterate), but
simple enough to spin up for a client project.

## Architecture

```
Single LXC Container (~8 cores, 16GB RAM)
│
├── PostgreSQL (unix socket)
│   ├── "forge" database (tasks, chat, logs — Forge state)
│   └── User's app can create its own databases
│
├── Nginx (:80/:443) — reverse proxy
│   ├── forge.{project}.borealhost.ai → :8100 (Forge UI)
│   └── {project}.borealhost.ai       → user's app (any port)
│
├── Django + Gunicorn (:8100)
│   ├── Dashboard — live view of all tasks, statuses, agent responses
│   ├── PM Chat — interactive conversation with the PM agent
│   └── API — task CRUD, chat, SSE streaming (MCP channels poll this)
│
├── 4 Claude Code Sessions (each in a `screen`)
│   ├── forge-pm     — Project Manager
│   ├── forge-dev    — Developer
│   ├── forge-review — Code Reviewer
│   └── forge-qc    — QA Tester (has Chrome via Xvfb)
│
├── Xvfb :1 — virtual display for QC's Chrome
│
└── /opt/forge/workspace/ — shared project directory (all agents' CWD)
    ├── .mcp.json    — 4 MCP server entries
    └── CLAUDE.md    — living project wiki
```

## The 4 Agents

### PM (Project Manager)
- Interface between the user and the dev team
- Receives user messages via chat, asks questions, proposes plans
- Breaks accepted plans into dev tasks (one at a time)
- Tracks progress, reports back to user
- Curates the workspace CLAUDE.md (architecture, decisions, features)
- **Never writes code**

### Dev (Developer)
- Receives coding tasks, writes code in `/opt/forge/workspace/`
- Runs the app locally, writes and runs Playwright tests
- Iterates until tests pass, then hands off to Review
- Has embedded frontend-design skill for UI work
- Updates CLAUDE.md with technical patterns

### Review (Code Reviewer)
- Reviews code for quality, security (OWASP top 10), architecture
- Approves → creates QC task, or Rejects → creates dev fix task
- Provides specific feedback with file paths and line numbers
- **Never modifies code**

### QC (QA Tester)
- Visual verification in Chrome (via DISPLAY=:1)
- Checks layout, responsiveness, interactions, console errors
- Approves → creates PM "verified" task, or Rejects → creates dev fix task
- Saves screenshots to `.forge/screenshots/`
- **Never modifies code**

## How They Communicate

### MCP Channel (`forge-channel.ts`)

Each agent runs Claude Code in a `screen` session. Claude Code discovers MCP
servers from `.mcp.json` in the working directory. One file defines all 4 servers:

```json
{
  "mcpServers": {
    "forge-pm-channel":     { "command": "bun", "args": ["forge-channel.ts", "--type", "pm"] },
    "forge-dev-channel":    { "command": "bun", "args": ["forge-channel.ts", "--type", "dev"] },
    "forge-review-channel": { "command": "bun", "args": ["forge-channel.ts", "--type", "review"] },
    "forge-qc-channel":     { "command": "bun", "args": ["forge-channel.ts", "--type", "qc"] }
  }
}
```

Each agent loads only its own server via `--dangerously-load-development-channels`.
The channel server is a single TypeScript file parameterized by `--type`. It:

1. Polls `GET /api/tasks/?type={self}&status=pending` on the Django API
2. Checks the **sequential gate** (no active tasks anywhere) before picking up work
3. Pushes the task into Claude's context via `server.notification()` (MCP channel protocol)
4. Exposes tools: `task_update`, `task_create`, and `chat_reply` (PM only)
5. Monitors idle state via screen hardcopy, nudges agent if stuck

### Shared Context

All 4 agents work from the same directory (`/opt/forge/workspace/`):
- **Same CLAUDE.md** — PM curates it as a living project wiki
- **Same Claude Code memory** — same project dir = same memory automatically
- **Same codebase** — all agents see the same files
- **Same git repo** — full history of all changes

### Task Pipeline

Strict sequential — only one task in the pipeline at a time:

```
User describes requirements
  → PM creates type=dev task
    → Dev codes + tests, creates type=review task
      → Review checks, creates type=qc (approve) or type=dev (reject)
        → QC tests visually, creates type=pm (verified) or type=dev (reject)
          → PM reports to user, creates next dev task
```

This eliminates file conflicts (only one agent writes code at a time) and ensures
each change goes through the full quality pipeline.

### PM Chat

The user talks to PM at any time — not just during initial planning:
- Mid-development: request changes, give feedback, reprioritize
- Bug reports: signal something is broken
- New features: add requirements to the plan

The PM channel server polls `/api/chat/pending/` every 5 seconds, even while
other agents have active tasks. User messages are always delivered promptly.

## Forge / User App Isolation

Forge claims minimal resources. Everything else belongs to the user's app:

```
FORGE (do not touch):
  Port 8100, database "forge", /opt/forge/*, screen sessions forge-*

USER'S APP (anything goes):
  Any other port, any other database, /opt/forge/workspace/*
```

This is enforced by convention in all agent prompts and the workspace CLAUDE.md.
The user's app can run React on :3000, Django on :8000, multiple services — Forge
doesn't care as long as port 8100 stays free.

## Key Design Decisions

### Why one container (not multiple)?
The internal BorealHost setup uses 5+ containers because it serves the entire
platform. For a single project, there's no reason to separate. One container means:
- Shared filesystem (no SCP, no pct push, no shared-reports mounts)
- Shared memory (same project dir)
- Simple provisioning (one template, one `setup.sh`)
- Easy to snapshot, migrate, or back up as a unit

### Why strict sequential (not parallel)?
Two agents writing code simultaneously causes merge conflicts and inconsistent
state. Sequential means only Dev writes code, and only one task at a time. This
is slower but eliminates an entire class of coordination bugs. The tradeoff is
worth it — reliability > speed for autonomous agents.

### Why PostgreSQL (not SQLite)?
- Multiple processes access the task queue concurrently (4 MCP channels + Django)
- The PM might want complex queries ("show failed QC tasks from today")
- History and analytics on development velocity
- The user's app might also need PostgreSQL — it's already there

### Why MCP channels (not direct HTTP)?
Claude Code sessions are long-running interactive processes. You can't HTTP into
them. MCP (Model Context Protocol) is the mechanism for pushing data into a
running Claude session. The channel server bridges the Django API (HTTP) to
Claude's context (MCP notifications).

### Why screen sessions?
Claude Code needs a TTY. `screen` provides a detached terminal that:
- Survives SSH disconnects
- Can be attached to for debugging (`screen -r forge-dev`)
- Supports hardcopy for idle detection
- Supports keystroke injection for nudging stuck agents

### Why embed frontend-design skill in the prompt?
Claude Code's plugin system requires specific directory structures and settings.
Embedding the skill content directly in `prompts/dev.md` is simpler and more
portable — it works regardless of Claude Code version or plugin configuration.

## File Structure

```
/opt/forge/
├── forge-channel.ts      # MCP channel server (parameterized by --type)
├── package.json          # Bun deps (@modelcontextprotocol/sdk)
├── prompts/
│   ├── pm.md             # PM system prompt
│   ├── dev.md            # Dev prompt (includes frontend-design skill)
│   ├── review.md         # Review prompt
│   └── qc.md             # QC prompt
├── start-agent.sh        # Start one agent: start-agent.sh <pm|dev|review|qc>
├── start-all.sh          # Start everything (Xvfb, Django, 4 agents)
├── stop-all.sh           # Stop everything
├── forge.service          # systemd unit
├── setup.sh              # First-run provisioning
├── ui/                   # Django project
│   ├── forge_ui/         # Django settings, urls, asgi
│   └── core/             # Single app: models, views, templates
│       ├── models.py     # Project, Task, ChatMessage, AgentLog
│       ├── views.py      # Dashboard, PM chat, all API endpoints
│       └── templates/core/
│           ├── base.html       # Dark theme, nav, shared CSS
│           ├── login.html      # Auth
│           ├── dashboard.html  # Task viewer (Alpine.js, auto-refresh)
│           └── pm_chat.html    # Chat UI (Alpine.js + SSE)
└── workspace/            # Shared project dir (all agents' CWD)
    ├── .mcp.json         # 4 MCP server definitions
    └── CLAUDE.md         # Living project wiki
```

## Deployment

```bash
# 1. Create a container (Ubuntu 24.04, privileged for Chrome)
# 2. Clone the repo
git clone git@github.com:alainsvrd/forge.git /opt/forge

# 3. Run setup
bash /opt/forge/setup.sh

# 4. Authenticate Claude Code for the forge user
su - forge -c 'claude login'
# Or inject .credentials.json for Max subscription

# 5. Start
systemctl start forge
# Or: /opt/forge/start-all.sh

# 6. Access
# Forge UI: http://<container-ip>:8100  (admin/admin)
# Describe your project in PM Chat, watch it get built
```

## Debugging

```bash
# Check which agents are running
su - forge -c 'screen -ls'

# Attach to an agent session (detach: Ctrl-A D)
su - forge -c 'screen -r forge-dev'

# Check task queue
curl -s -H 'X-Forge-Secret: ...' http://localhost:8100/api/tasks/ | python3 -m json.tool

# Check agent status
curl -s http://localhost:8100/api/status/ | python3 -m json.tool

# Django logs
journalctl -u forge -f

# MCP channel logs (stderr goes to screen)
# Attach to the agent's screen session to see channel output
```
