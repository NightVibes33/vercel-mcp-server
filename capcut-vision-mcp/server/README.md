# CapCut Vision MCP Relay

Run locally:

```powershell
copy .env.example .env
npm install
$env:PAIRING_TOKEN="replace-with-a-long-random-token"
npm start
```

The relay exposes:

- `GET /health`
- `POST /agent/connect`
- `GET /agent/next`
- `POST /agent/result`
- `GET /api/agents`
- `POST /api/command`
- `POST /mcp`

The current command queue is held in memory. Run one server instance. A durable Redis-backed queue is the next deployment step before claiming production readiness.
