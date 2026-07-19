import crypto from 'node:crypto';
import express from 'express';
import cors from 'cors';
import helmet from 'helmet';
import morgan from 'morgan';
import { z } from 'zod';

const app = express();
const port = Number(process.env.PORT || 8787);
const pairingToken = process.env.PAIRING_TOKEN || '';
const commandTimeoutMs = Math.max(5000, Number(process.env.COMMAND_TIMEOUT_MS || 45000));

if (!pairingToken || pairingToken.length < 24) {
  console.error('PAIRING_TOKEN must be set to a random value at least 24 characters long.');
  process.exit(1);
}

app.use(helmet({ crossOriginResourcePolicy: false }));
app.use(cors({ origin: '*', exposedHeaders: ['mcp-session-id'] }));
app.use(express.json({ limit: '16mb' }));
app.use(morgan('tiny'));

const agents = new Map();
const commands = new Map();
const results = new Map();
const waiters = new Map();

function now() {
  return Date.now();
}

function authorize(req, res, next) {
  const value = req.get('authorization') || '';
  const token = value.startsWith('Bearer ') ? value.slice(7) : '';
  const left = Buffer.from(token);
  const right = Buffer.from(pairingToken);
  if (left.length !== right.length || !crypto.timingSafeEqual(left, right)) {
    return res.status(401).json({ error: 'Unauthorized' });
  }
  next();
}

function createCommand(agentId, actions) {
  const commandId = crypto.randomUUID();
  const command = {
    command_id: commandId,
    agent_id: agentId,
    actions,
    created_at: now(),
    status: 'queued',
  };
  commands.set(commandId, command);
  const agent = agents.get(agentId);
  if (!agent) throw new Error('Agent is not connected.');
  agent.queue.push(commandId);
  const waiter = agent.waiter;
  if (waiter) {
    agent.waiter = null;
    waiter();
  }
  return command;
}

async function waitForResult(commandId, timeout = commandTimeoutMs) {
  if (results.has(commandId)) return results.get(commandId);
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      waiters.delete(commandId);
      reject(new Error('Command timed out.'));
    }, timeout);
    waiters.set(commandId, value => {
      clearTimeout(timer);
      resolve(value);
    });
  });
}

app.get('/health', (_req, res) => {
  res.json({ ok: true, service: 'CapCut Vision MCP Relay', agents: agents.size, version: '0.1.0' });
});

app.post('/agent/connect', authorize, (req, res) => {
  const body = z.object({
    agent_id: z.string().min(3),
    platform: z.string().default('windows'),
    hostname: z.string().default('Windows PC'),
    capabilities: z.array(z.string()).default([]),
  }).parse(req.body);

  const existing = agents.get(body.agent_id);
  agents.set(body.agent_id, {
    ...body,
    queue: existing?.queue || [],
    waiter: existing?.waiter || null,
    connected_at: existing?.connected_at || now(),
    last_seen: now(),
  });
  res.json({ ok: true, agent_id: body.agent_id });
});

app.get('/agent/next', authorize, async (req, res) => {
  const agentId = String(req.query.agent_id || '');
  const agent = agents.get(agentId);
  if (!agent) return res.status(404).json({ error: 'Agent is not connected.' });
  agent.last_seen = now();

  const take = () => {
    const id = agent.queue.shift();
    if (!id) return null;
    const command = commands.get(id);
    if (!command) return null;
    command.status = 'sent';
    return command;
  };

  let command = take();
  if (command) return res.json(command);

  await new Promise(resolve => {
    const timer = setTimeout(resolve, 25000);
    agent.waiter = () => {
      clearTimeout(timer);
      resolve();
    };
  });

  command = take();
  if (!command) return res.status(204).end();
  res.json(command);
});

app.post('/agent/result', authorize, (req, res) => {
  const body = z.object({
    agent_id: z.string().min(3),
    command_id: z.string().min(8),
    result: z.any().optional(),
    error: z.string().nullable().optional(),
  }).parse(req.body);

  const agent = agents.get(body.agent_id);
  if (agent) agent.last_seen = now();
  const command = commands.get(body.command_id);
  if (command) command.status = body.error ? 'failed' : 'completed';

  const value = {
    command_id: body.command_id,
    agent_id: body.agent_id,
    result: body.result ?? null,
    error: body.error ?? null,
    completed_at: now(),
  };
  results.set(body.command_id, value);
  const waiter = waiters.get(body.command_id);
  if (waiter) {
    waiters.delete(body.command_id);
    waiter(value);
  }
  res.json({ ok: true });
});

app.get('/api/agents', authorize, (_req, res) => {
  res.json({ agents: [...agents.values()].map(agent => ({
    agent_id: agent.agent_id,
    hostname: agent.hostname,
    platform: agent.platform,
    capabilities: agent.capabilities,
    last_seen: agent.last_seen,
    connected: now() - agent.last_seen < 60000,
  })) });
});

app.post('/api/command', authorize, async (req, res) => {
  try {
    const body = z.object({
      agent_id: z.string().min(3),
      actions: z.array(z.object({ type: z.string().min(1) }).passthrough()).min(1).max(100),
      wait: z.boolean().default(true),
    }).parse(req.body);

    const command = createCommand(body.agent_id, body.actions);
    if (!body.wait) return res.status(202).json(command);
    const result = await waitForResult(command.command_id);
    res.json(result);
  } catch (error) {
    res.status(error?.message === 'Agent is not connected.' ? 409 : 400).json({ error: error?.message || 'Command failed.' });
  }
});

app.get('/api/command/:id', authorize, (req, res) => {
  const result = results.get(req.params.id);
  if (result) return res.json(result);
  const command = commands.get(req.params.id);
  if (!command) return res.status(404).json({ error: 'Command not found.' });
  res.json(command);
});

const tools = [
  {
    name: 'pc_list_agents',
    description: 'List Windows desktop agents connected to the CapCut Vision relay.',
    inputSchema: { type: 'object', properties: {} },
  },
  {
    name: 'pc_screenshot',
    description: 'Capture the current Windows desktop so the assistant can visually inspect CapCut or other visible apps.',
    inputSchema: {
      type: 'object',
      properties: { agent_id: { type: 'string' }, monitor: { type: 'number', default: 0 } },
      required: ['agent_id'],
    },
  },
  {
    name: 'pc_execute_actions',
    description: 'Execute an ordered batch of visible mouse, keyboard, window, CapCut, or approved-file actions on the connected PC.',
    inputSchema: {
      type: 'object',
      properties: {
        agent_id: { type: 'string' },
        actions: { type: 'array', items: { type: 'object' }, minItems: 1, maxItems: 100 },
      },
      required: ['agent_id', 'actions'],
    },
  },
  {
    name: 'pc_list_windows',
    description: 'List visible Windows desktop windows and their screen rectangles.',
    inputSchema: { type: 'object', properties: { agent_id: { type: 'string' } }, required: ['agent_id'] },
  },
  {
    name: 'pc_search_assets',
    description: 'Search the local approved asset folders for image, video, and audio files.',
    inputSchema: {
      type: 'object',
      properties: {
        agent_id: { type: 'string' },
        query: { type: 'string' },
        extensions: { type: 'array', items: { type: 'string' } },
      },
      required: ['agent_id'],
    },
  },
];

async function callTool(name, args) {
  if (name === 'pc_list_agents') {
    return { agents: [...agents.values()].map(agent => ({ agent_id: agent.agent_id, hostname: agent.hostname, last_seen: agent.last_seen })) };
  }
  const agentId = String(args.agent_id || '');
  let actions;
  if (name === 'pc_screenshot') actions = [{ type: 'screenshot', monitor: Number(args.monitor || 0) }];
  else if (name === 'pc_list_windows') actions = [{ type: 'list_windows' }];
  else if (name === 'pc_search_assets') actions = [{ type: 'search_files', query: String(args.query || ''), extensions: args.extensions || ['png','jpg','jpeg','webp','gif','mp4','mov','mkv','webm','mp3','wav','m4a','aac'] }];
  else if (name === 'pc_execute_actions') actions = args.actions;
  else throw new Error('Unknown tool.');
  const command = createCommand(agentId, actions);
  const result = await waitForResult(command.command_id);
  if (result.error) throw new Error(result.error);
  return result.result;
}

app.all('/mcp', async (req, res) => {
  if (req.method === 'OPTIONS') return res.status(204).end();
  if (req.method === 'GET') return res.json({ service: 'CapCut Vision MCP', version: '0.1.0', health: 'ok' });
  try {
    authorize(req, res, async () => {
      const message = req.body || {};
      const id = message.id ?? null;
      if (message.method === 'initialize') {
        return res.json({ jsonrpc: '2.0', id, result: { protocolVersion: '2024-11-05', capabilities: { tools: {} }, serverInfo: { name: 'capcut-vision-mcp', version: '0.1.0' } } });
      }
      if (message.method === 'notifications/initialized') return res.status(204).end();
      if (message.method === 'tools/list') return res.json({ jsonrpc: '2.0', id, result: { tools } });
      if (message.method === 'tools/call') {
        const output = await callTool(message.params?.name, message.params?.arguments || {});
        return res.json({ jsonrpc: '2.0', id, result: { content: [{ type: 'text', text: JSON.stringify(output) }], structuredContent: output } });
      }
      return res.json({ jsonrpc: '2.0', id, error: { code: -32601, message: 'Method not found.' } });
    });
  } catch (error) {
    res.status(400).json({ error: error?.message || 'MCP request failed.' });
  }
});

setInterval(() => {
  const cutoff = now() - 10 * 60 * 1000;
  for (const [id, agent] of agents) if (agent.last_seen < cutoff) agents.delete(id);
  for (const [id, result] of results) if (result.completed_at < cutoff) results.delete(id);
}, 60000).unref();

app.listen(port, () => {
  console.log(`CapCut Vision MCP relay listening on :${port}`);
});
