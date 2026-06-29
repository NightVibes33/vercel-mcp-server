import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { SSEServerTransport } from "@modelcontextprotocol/sdk/server/sse.js";
import express from "express";
import fetch from "node-fetch";

const app = express();
const VERCEL_TOKEN = process.env.VERCEL_TOKEN;

if (!VERCEL_TOKEN) {
  console.error("VERCEL_TOKEN environment variable is required");
  process.exit(1);
}

const server = new Server(
  {
    name: "vercel-mcp-server",
    version: "1.0.0",
  },
  {
    capabilities: {
      tools: {},
    },
  }
);

// Define Tools
server.setRequestHandler("list_tools", async () => ({
  tools: [
    {
      name: "vercel_list_projects",
      description: "List all projects in a Vercel team",
      inputSchema: {
        type: "object",
        properties: {
          teamId: { type: "string" }
        },
        required: ["teamId"]
      }
    },
    {
      name: "vercel_create_project",
      description: "Create a new Vercel project and link a GitHub repository",
      inputSchema: {
        type: "object",
        properties: {
          name: { type: "string" },
          repository: { type: "string", description: "Format: owner/repo" },
          teamId: { type: "string" }
        },
        required: ["name", "repository", "teamId"]
      }
    },
    {
      name: "vercel_create_deployment",
      description: "Trigger a new deployment for a project",
      inputSchema: {
        type: "object",
        properties: {
          projectId: { type: "string" },
          teamId: { type: "string" }
        },
        required: ["projectId", "teamId"]
      }
    }
  ]
}));

server.setRequestHandler("call_tool", async (request) => {
  const { name, arguments: args } = request.params;

  const headers = {
    Authorization: `Bearer ${VERCEL_TOKEN}`,
    "Content-Type": "application/json",
  };

  try {
    switch (name) {
      case "vercel_list_projects": {
        const teamId = args?.teamId;
        const res = await fetch(`https://api.vercel.com/v9/projects?teamId=${teamId}`, { headers });
        const data = await res.json();
        return { content: [{ type: "text", text: JSON.stringify(data, null, 2) }] };
      }

      case "vercel_create_project": {
        const { name: projName, repository, teamId } = args as any;
        const body = {
          name: projName,
          gitRepository: {
            type: "github",
            repo: repository,
          },
        };
        const res = await fetch(`https://api.vercel.com/v9/projects?teamId=${teamId}`, {
          method: "POST",
          headers,
          body: JSON.stringify(body),
        });
        const data = await res.json();
        return { content: [{ type: "text", text: JSON.stringify(data, null, 2) }] };
      }

      case "vercel_create_deployment": {
        const { projectId, teamId } = args as any;
        const body = {
          name: "mcp-deployment",
          project: projectId,
        };
        const res = await fetch(`https://api.vercel.com/v13/deployments?teamId=${teamId}`, {
          method: "POST",
          headers,
          body: JSON.stringify(body),
        });
        const data = await res.json();
        return { content: [{ type: "text", text: JSON.stringify(data, null, 2) }] };
      }

      default:
        throw new Error(`Tool not found: ${name}`);
    }
  } catch (error: any) {
    return {
      content: [{ type: "text", text: `Error: ${error.message}` }],
      isError: true,
    };
  }
});

let transport: SSEServerTransport;

app.get("/sse", async (req, res) => {
  transport = new SSEServerTransport("/messages", res);
  await server.connect(transport);
});

app.post("/messages", async (req, res) => {
  if (transport) {
    await transport.handlePostMessage(req, res);
  } else {
    res.status(400).send("Not connected");
  }
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  console.log(`Vercel MCP Server running on port ${PORT}`);
});
