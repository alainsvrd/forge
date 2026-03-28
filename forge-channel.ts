// forge-channel.ts — Parameterized MCP channel server for Forge agents
// Usage: bun forge-channel.ts --type pm|dev|review|qc

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { execSync } from "child_process";
import { readFileSync, unlinkSync } from "fs";

// ── Config ──────────────────────────────────────────────────────────────────

const AGENT_TYPE = process.argv.find((_, i, a) => a[i - 1] === "--type") || "dev";
const TASK_POLL_MS = 15_000;
const CHAT_POLL_MS = 5_000;
const HUB_URL = "http://localhost:8100";
const FORGE_SECRET = process.env.FORGE_SECRET || "forge-dev-secret";
const SCREEN_SESSION = `forge-${AGENT_TYPE}`;

const IDLE_STABLE_THRESHOLD = 3; // 3 polls before nudging
const IDLE_FORCE_THRESHOLD = 8; // Force-close after this many idle polls

let inflightTaskId: number | null = null;
let inflightTaskTitle = "";
let wasWorking = false;
let idleStableCount = 0;
let nudgeSent = false;

interface Task {
  id: number;
  title: string;
  description: string;
  priority: string;
  status: string;
  type: string;
  parent_id: number | null;
  created_by: string;
  note: string;
}

// ── HTTP helpers ────────────────────────────────────────────────────────────

const headers = {
  "Content-Type": "application/json",
  "X-Forge-Secret": FORGE_SECRET,
};

async function api(path: string, options?: RequestInit): Promise<any> {
  const res = await fetch(`${HUB_URL}${path}`, { ...options, headers });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`API ${path} failed (${res.status}): ${text}`);
  }
  return res.json();
}

// ── MCP Server ──────────────────────────────────────────────────────────────

const server = new Server(
  { name: `forge-${AGENT_TYPE}-channel`, version: "0.1.0" },
  {
    capabilities: {
      experimental: { "claude/channel": {} },
      tools: {},
    },
    instructions: `Tasks arrive as <channel> notifications. Process one at a time. When done, call task_update with the task_id, status ("done" or "failed"), and a note. Then call task_create to hand off to the next agent in the pipeline.`,
  }
);

// ── Tools ───────────────────────────────────────────────────────────────────

const COMMON_TOOLS = [
  {
    name: "task_update",
    description: "Mark the current task as done or failed.",
    inputSchema: {
      type: "object" as const,
      properties: {
        task_id: { type: "number", description: "The task ID" },
        status: {
          type: "string",
          enum: ["done", "failed"],
          description: "Outcome of the task",
        },
        note: {
          type: "string",
          description: "Summary of what was done or why it failed",
        },
      },
      required: ["task_id", "status", "note"],
    },
  },
  {
    name: "task_create",
    description:
      "Create a task for another agent. Use type to target: pm, dev, review, or qc. ALWAYS include a clear title AND a detailed description — the receiving agent has no other context.",
    inputSchema: {
      type: "object" as const,
      properties: {
        type: {
          type: "string",
          enum: ["pm", "dev", "review", "qc"],
          description: "Which agent should handle this task",
        },
        title: { type: "string", description: "Clear, descriptive task title (required)" },
        description: {
          type: "string",
          description: "Detailed description: what to do, acceptance criteria, relevant context. This is the ONLY info the receiving agent gets.",
        },
        priority: {
          type: "string",
          enum: ["low", "normal", "high", "urgent"],
          default: "normal",
        },
        parent_id: {
          type: "number",
          description: "ID of the parent task (for traceability)",
        },
      },
      required: ["type", "title", "description"],
    },
  },
];

const PM_CHAT_TOOL = {
  name: "chat_reply",
  description: "Send a message to the user in the PM Chat interface.",
  inputSchema: {
    type: "object" as const,
    properties: {
      content: {
        type: "string",
        description: "Message content (supports markdown)",
      },
    },
    required: ["content"],
  },
};

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: AGENT_TYPE === "pm" ? [...COMMON_TOOLS, PM_CHAT_TOOL] : COMMON_TOOLS,
}));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;

  if (name === "task_update") {
    const { task_id, status, note } = args as {
      task_id: number;
      status: string;
      note: string;
    };
    try {
      await api(`/api/tasks/${task_id}/`, {
        method: "PUT",
        body: JSON.stringify({ status, note }),
      });

      // Log the event
      await api("/api/tasks/", {
        method: "POST",
        body: JSON.stringify({
          // We log via the agent_log — but for now just track via task status
        }),
      }).catch(() => {});

      if (inflightTaskId === task_id) {
        inflightTaskId = null;
        inflightTaskTitle = "";
        idleStableCount = 0;
        nudgeSent = false;
      }

      return {
        content: [
          { type: "text", text: `Task #${task_id} marked as ${status}.` },
        ],
      };
    } catch (e: any) {
      return {
        content: [{ type: "text", text: `Error updating task: ${e.message}` }],
        isError: true,
      };
    }
  }

  if (name === "task_create") {
    const { type, title, description, priority, parent_id } = args as {
      type: string;
      title: string;
      description: string;
      priority?: string;
      parent_id?: number;
    };
    if (!title || title.trim().length < 5) {
      return {
        content: [{ type: "text", text: "Error: title is required and must be at least 5 characters." }],
        isError: true,
      };
    }
    if (!description || description.trim().length < 10) {
      return {
        content: [{ type: "text", text: "Error: description is required and must be at least 10 characters. The receiving agent needs detailed context." }],
        isError: true,
      };
    }
    try {
      const result = await api("/api/tasks/", {
        method: "POST",
        body: JSON.stringify({
          type,
          title: title.trim(),
          description: description.trim(),
          priority: priority || "normal",
          created_by: AGENT_TYPE,
          parent_id: parent_id || null,
        }),
      });
      return {
        content: [
          {
            type: "text",
            text: `Created task #${result.id} [${type}]: ${title}`,
          },
        ],
      };
    } catch (e: any) {
      return {
        content: [{ type: "text", text: `Error creating task: ${e.message}` }],
        isError: true,
      };
    }
  }

  if (name === "chat_reply" && AGENT_TYPE === "pm") {
    const { content } = args as { content: string };
    try {
      await api("/api/chat/", {
        method: "POST",
        body: JSON.stringify({
          role: "pm",
          content,
        }),
      });
      return {
        content: [{ type: "text", text: "Message sent to user." }],
      };
    } catch (e: any) {
      return {
        content: [
          { type: "text", text: `Error sending chat: ${e.message}` },
        ],
        isError: true,
      };
    }
  }

  return {
    content: [{ type: "text", text: `Unknown tool: ${name}` }],
    isError: true,
  };
});

// ── Idle Detection ──────────────────────────────────────────────────────────

function checkIfIdle(): boolean {
  try {
    execSync(
      `screen -S ${SCREEN_SESSION} -X hardcopy /tmp/forge-${AGENT_TYPE}-screen.txt`,
      { timeout: 5_000 }
    );
    const raw = readFileSync(
      `/tmp/forge-${AGENT_TYPE}-screen.txt`,
      "utf-8"
    );
    const lines = raw.split("\n").map((l) => l.trimEnd());

    const hasStatusBar = lines.some(
      (l) =>
        l.includes("bypass permissions") || l.includes("permissions on")
    );
    const hasIdleCursor = lines.some((l) => /^o[\s\x00-\xff]{0,3}$/.test(l));
    const isWorking = lines.some(
      (l) => l.includes("Churning") || l.includes("Running")
    );

    return hasStatusBar && hasIdleCursor && !isWorking;
  } catch {
    return false;
  }
}

function nudgeAgent(taskId: number): void {
  try {
    const msg = `You have an active task #${taskId} but appear idle. Please call task_update with status "done" or "failed" and a note.`;
    const safe = msg.replace(/'/g, "'\\''");
    execSync(`screen -S ${SCREEN_SESSION} -p 0 -X stuff '${safe}\r'`, {
      timeout: 5_000,
    });
    console.error(`forge-${AGENT_TYPE}: sent idle nudge for task #${taskId}`);
  } catch (e: any) {
    console.error(`forge-${AGENT_TYPE}: nudge failed: ${e.message}`);
  }
}

function sendCompact(): void {
  try {
    execSync(
      `screen -S ${SCREEN_SESSION} -p 0 -X stuff '/compact\\r'`,
      { timeout: 5_000 }
    );
    console.error(`forge-${AGENT_TYPE}: compact sent`);
  } catch {
    // Non-fatal
  }
}

// ── Poll Loop ───────────────────────────────────────────────────────────────

async function pollLoop() {
  let lastChatPoll = 0;

  while (true) {
    await new Promise((r) => setTimeout(r, TASK_POLL_MS));
    const now = Date.now();

    // ── PM: Always check for user chat messages ──
    if (AGENT_TYPE === "pm" && now - lastChatPoll >= CHAT_POLL_MS) {
      lastChatPoll = now;
      try {
        const pendingMsg = await api("/api/chat/pending/");
        if (pendingMsg) {
          // Mark delivered
          await api(`/api/chat/${pendingMsg.id}/delivered/`, {
            method: "POST",
          });
          // Push to PM's Claude session via MCP notification
          await server.notification({
            method: "notifications/claude/channel",
            params: {
              content: `User message:\n${pendingMsg.content}`,
              meta: { source: "user_chat", message_id: String(pendingMsg.id) },
            },
          });
          console.error(
            `forge-pm: delivered user message #${pendingMsg.id}`
          );
        }
      } catch (e: any) {
        console.error(`forge-pm: chat poll error: ${e.message}`);
      }
    }

    // ── If task in-flight, monitor idle state ──
    if (inflightTaskId !== null) {
      const idle = checkIfIdle();
      if (idle) {
        idleStableCount++;
        if (!nudgeSent && idleStableCount >= IDLE_STABLE_THRESHOLD) {
          nudgeAgent(inflightTaskId);
          nudgeSent = true;
        } else if (nudgeSent && idleStableCount >= IDLE_FORCE_THRESHOLD) {
          console.error(
            `forge-${AGENT_TYPE}: force-closing task #${inflightTaskId} (idle timeout)`
          );
          try {
            await api(`/api/tasks/${inflightTaskId}/`, {
              method: "PUT",
              body: JSON.stringify({
                status: "failed",
                note: "Auto-closed: agent idle, no task_update called",
              }),
            });
          } catch {}
          inflightTaskId = null;
          inflightTaskTitle = "";
          idleStableCount = 0;
          nudgeSent = false;
        }
      } else {
        idleStableCount = 0;
        nudgeSent = false;
      }
      continue;
    }

    // ── Sequential gate: no new work if any task is active anywhere ──
    try {
      const activeTasks = await api("/api/tasks/?status=active");
      if (activeTasks.length > 0) continue;
    } catch {
      continue;
    }

    // ── Poll for pending tasks of our type ──
    try {
      const tasks = await api(
        `/api/tasks/?type=${AGENT_TYPE}&status=pending`
      );
      if (tasks.length === 0) {
        if (wasWorking) {
          wasWorking = false;
          sendCompact();
        }
        continue;
      }

      const task = tasks[0];
      inflightTaskId = task.id;
      inflightTaskTitle = task.title;
      wasWorking = true;
      idleStableCount = 0;
      nudgeSent = false;

      // Mark active
      await api(`/api/tasks/${task.id}/`, {
        method: "PUT",
        body: JSON.stringify({ status: "active" }),
      });

      // Push to agent via MCP channel
      const content = [
        `Task #${task.id}: ${task.title}`,
        task.description ? `\n${task.description}` : "",
      ]
        .filter(Boolean)
        .join("");

      await server.notification({
        method: "notifications/claude/channel",
        params: {
          content,
          meta: {
            task_id: String(task.id),
            priority: task.priority,
            parent_id: task.parent_id ? String(task.parent_id) : undefined,
          },
        },
      });

      console.error(
        `forge-${AGENT_TYPE}: dispatched task #${task.id}: ${task.title}`
      );
    } catch (e: any) {
      console.error(`forge-${AGENT_TYPE}: poll error: ${e.message}`);
    }
  }
}

// ── Main ────────────────────────────────────────────────────────────────────

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error(
    `forge-${AGENT_TYPE}-channel: connected, task poll ${TASK_POLL_MS}ms` +
      (AGENT_TYPE === "pm" ? `, chat poll ${CHAT_POLL_MS}ms` : "")
  );
  pollLoop();
}

main().catch((e) => {
  console.error(`forge-${AGENT_TYPE}-channel fatal:`, e);
  process.exit(1);
});
