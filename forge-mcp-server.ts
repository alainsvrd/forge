// forge-mcp-server.ts — Stateless MCP tool server for Forge agents
// Provides task_update, task_create, chat_reply tools.
// No polling, no idle detection, no channel notifications.
// Usage: bun forge-mcp-server.ts --type pm|dev|review|qc

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

// ── Config ──────────────────────────────────────────────────────────────────

const AGENT_TYPE = process.argv.find((_, i, a) => a[i - 1] === "--type") || "dev";
const HUB_URL = "http://localhost:8100";
const FORGE_SECRET = process.env.FORGE_SECRET || "forge-dev-secret";

const NEXT_AGENT: Record<string, string> = {
  dev: "review",
  review: "qc",
  qc: "pm",
};

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
  { name: `forge-${AGENT_TYPE}`, version: "0.2.0" },
  {
    capabilities: { tools: {} },
    instructions:
      "Tasks arrive as direct messages. Process one at a time. " +
      "When done, call task_update then task_create to hand off to the next agent.",
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
      "Create a task for another agent. Use type to target: pm, dev, review, or qc. " +
      "ALWAYS include a clear title AND a detailed description — the receiving agent has no other context.",
    inputSchema: {
      type: "object" as const,
      properties: {
        type: {
          type: "string",
          enum: ["pm", "dev", "review", "qc"],
          description: "Which agent should handle this task",
        },
        title: {
          type: "string",
          description: "Clear, descriptive task title (required)",
        },
        description: {
          type: "string",
          description:
            "Detailed description: what to do, acceptance criteria, relevant context. " +
            "This is the ONLY info the receiving agent gets.",
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

const PM_TOOLS = [
  {
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
  },
  {
    name: "check_agents",
    description:
      "Check the status of all agents (dev, review, qc). Returns each agent's status, " +
      "current task, recent tool calls, and whether they're alive. Use this to monitor " +
      "the pipeline and verify agents are making progress.",
    inputSchema: {
      type: "object" as const,
      properties: {},
    },
  },
  {
    name: "nudge_agent",
    description:
      "Send a direct message to another agent. Use this to remind a stuck agent to " +
      "complete their task, provide additional context, or ask for a status update.",
    inputSchema: {
      type: "object" as const,
      properties: {
        agent_type: {
          type: "string",
          enum: ["dev", "review", "qc"],
          description: "Which agent to message",
        },
        message: {
          type: "string",
          description: "Message to send to the agent",
        },
      },
      required: ["agent_type", "message"],
    },
  },
  {
    name: "list_tasks",
    description:
      "List recent tasks with their status. Use to verify the pipeline state — " +
      "check if tasks are stuck, see what's pending/active/done/failed.",
    inputSchema: {
      type: "object" as const,
      properties: {
        status: {
          type: "string",
          enum: ["pending", "active", "done", "failed"],
          description: "Filter by status (optional — omit for all)",
        },
      },
    },
  },
];

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: AGENT_TYPE === "pm" ? [...COMMON_TOOLS, ...PM_TOOLS] : COMMON_TOOLS,
}));

// ── Tool handlers ───────────────────────────────────────────────────────────

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

      const nextAgent = NEXT_AGENT[AGENT_TYPE];
      const handoffReminder = nextAgent
        ? `\n\nIMPORTANT: Now call task_create(type="${nextAgent}", ...) to hand off to the ${nextAgent} agent. The pipeline stalls if you skip this.`
        : "";

      return {
        content: [
          {
            type: "text",
            text: `Task #${task_id} marked as ${status}.${handoffReminder}`,
          },
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
        content: [
          {
            type: "text",
            text: "Error: title is required and must be at least 5 characters.",
          },
        ],
        isError: true,
      };
    }
    if (!description || description.trim().length < 10) {
      return {
        content: [
          {
            type: "text",
            text: "Error: description is required and must be at least 10 characters. The receiving agent needs detailed context.",
          },
        ],
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
        body: JSON.stringify({ role: "pm", content }),
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

  if (name === "check_agents" && AGENT_TYPE === "pm") {
    try {
      const data = await api("/api/agents/status/");
      const agents = data.agents || {};
      const lines: string[] = [];
      for (const [type, info] of Object.entries(agents) as [string, any][]) {
        if (type === "pm") continue; // Skip self
        const task = info.current_task
          ? `task=#${info.current_task.id} "${info.current_task.title}"`
          : "no task";
        const tools = (info.tool_calls || [])
          .slice(-3)
          .map((tc: any) => tc.tool)
          .join(", ");
        lines.push(
          `${type}: status=${info.status} alive=${info.is_alive} ${task} ` +
          `turns=${info.total_turns} cost=$${(info.total_cost_usd || 0).toFixed(4)} ` +
          `recent_tools=[${tools}]`
        );
      }
      lines.push(`\nactive_task: ${data.active_task ? `#${data.active_task.id} [${data.active_task.type}] ${data.active_task.title}` : "none"}`);
      return {
        content: [{ type: "text", text: lines.join("\n") }],
      };
    } catch (e: any) {
      return {
        content: [{ type: "text", text: `Error: ${e.message}` }],
        isError: true,
      };
    }
  }

  if (name === "nudge_agent" && AGENT_TYPE === "pm") {
    const { agent_type, message } = args as { agent_type: string; message: string };
    try {
      await api(`/api/agents/${agent_type}/nudge/`, {
        method: "POST",
        body: JSON.stringify({ message }),
      });
      return {
        content: [{ type: "text", text: `Message sent to ${agent_type} agent.` }],
      };
    } catch (e: any) {
      return {
        content: [{ type: "text", text: `Error nudging ${agent_type}: ${e.message}` }],
        isError: true,
      };
    }
  }

  if (name === "list_tasks" && AGENT_TYPE === "pm") {
    const { status } = args as { status?: string };
    try {
      const url = status ? `/api/tasks/?status=${status}` : "/api/tasks/";
      const tasks = await api(url);
      if (!tasks.length) {
        return { content: [{ type: "text", text: `No tasks${status ? ` with status=${status}` : ""}.` }] };
      }
      const lines = tasks.map((t: any) =>
        `#${t.id} [${t.type}/${t.status}] by=${t.created_by || "user"} | ${t.title}` +
        (t.note ? `\n  note: ${t.note.substring(0, 200)}` : "")
      );
      return {
        content: [{ type: "text", text: lines.join("\n") }],
      };
    } catch (e: any) {
      return {
        content: [{ type: "text", text: `Error: ${e.message}` }],
        isError: true,
      };
    }
  }

  return {
    content: [{ type: "text", text: `Unknown tool: ${name}` }],
    isError: true,
  };
});

// ── Start ───────────────────────────────────────────────────────────────────

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch((e) => {
  console.error("Fatal:", e);
  process.exit(1);
});
