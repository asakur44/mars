#!/usr/bin/env node
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { exec } from "child_process";
import { z } from "zod";

const CONNECTOR_MODE = (process.env.CONNECTOR_MODE ?? "readonly").toLowerCase();
const IS_AGENT_MODE = CONNECTOR_MODE === "agent";

const CodexParamsSchema = z.object({
  prompt: z.string().describe("The prompt to send to Codex"),
});

const TIMEOUT_MS = 600_000; // 10 minutes — agent tasks can take a while

function shellEscape(str: string): string {
  if (process.platform === "win32") {
    // Windows cmd.exe: use double quotes, escape internal double quotes
    return '"' + str.replace(/"/g, '\\"') + '"';
  }
  return "'" + str.replace(/'/g, "'\\''") + "'";
}

function runCodex(prompt: string): Promise<string> {
  return new Promise((resolve, reject) => {
    let cmd = `codex exec`;
    if (IS_AGENT_MODE) {
      cmd += ` --sandbox workspace-write`;
    } else {
      cmd += ` --sandbox read-only`;
    }
    cmd += ` ${shellEscape(prompt)}`;

    const proc = exec(cmd, {
      timeout: TIMEOUT_MS,
      killSignal: "SIGTERM",
      windowsHide: true,
    }, (error, stdout, stderr) => {
      if (error) {
        if (error.killed) {
          reject(new Error(
            `[CODEX TIMEOUT] Process killed after ${TIMEOUT_MS / 1000}s. ` +
            `The task was too long for the current timeout. ` +
            `Partial output:\n${stdout.trim().slice(-500) || "(none)"}`
          ));
        } else if (error.signal) {
          reject(new Error(
            `[CODEX CRASHED] Process terminated by signal ${error.signal}. ` +
            `stderr: ${stderr.trim().slice(-500) || "(none)"}`
          ));
        } else {
          reject(new Error(
            `[CODEX ERROR] Exit code ${error.code}. ` +
            `${stderr.trim().slice(-500) || error.message}`
          ));
        }
      } else {
        resolve(stdout.trim());
      }
    });

    // Close stdin so codex doesn't try to read from it
    proc.stdin?.end();

    proc.on("error", (err) => {
      reject(new Error(
        `[CODEX SPAWN FAILED] Could not start codex: ${err.message}`
      ));
    });
  });
}

const modeLabel = IS_AGENT_MODE ? "agent (workspace write access)" : "readonly";

const server = new Server(
  { name: "codex-mcp-server", version: "1.0.0" },
  { capabilities: { tools: {} } }
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "codex_prompt",
      description:
        `Send a prompt to OpenAI Codex CLI and return the response. Mode: ${modeLabel}. Set CONNECTOR_MODE=agent for full capabilities.`,
      inputSchema: {
        type: "object" as const,
        properties: {
          prompt: { type: "string", description: "The prompt to send to Codex" },
        },
        required: ["prompt"],
      },
    },
  ],
}));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;

  if (name === "codex_prompt") {
    const params = CodexParamsSchema.parse(args);

    try {
      const result = await runCodex(params.prompt);
      return {
        content: [{ type: "text" as const, text: result }],
      };
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      return {
        content: [{ type: "text" as const, text: `Error: ${message}` }],
        isError: true,
      };
    }
  }

  return { content: [{ type: "text" as const, text: `Unknown tool: ${name}` }], isError: true };
});

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error(`Codex MCP server started (mode: ${modeLabel})`);
}

main().catch((err) => {
  console.error("Fatal error:", err);
  process.exit(1);
});
