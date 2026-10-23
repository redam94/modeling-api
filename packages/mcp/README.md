# mapi-mcp

Connect a chat tool to one modeling-api dashboard over the
[Model Context Protocol](https://modelcontextprotocol.io) (MCP). The tools are the
read-only dashboard tools (`get_dashboard`, `get_latest_readout`, `get_readout`,
`compare_readouts`, `get_channel`, …): an agent can read readouts, panels and metrics, and
cannot change anything or open another dashboard.

`mapi-mcp` is a small stdio bridge. The chat tool starts it as a subprocess and writes
JSON-RPC messages to its stdin, one per line. The bridge sends each message to the API's
`POST /v1/mcp` with your credential and writes the answer to stdout. Clients that speak
streamable HTTP can call `POST /v1/mcp` directly instead.

## Credentials

Use one of these, from the environment of the chat tool (never from the command line):

| Variable | Value |
|---|---|
| `MAPI_API_URL` | The API address, for example `https://api.example.com` (`https://` is required, except for `localhost`) |
| `MAPI_DASHBOARD_TOKEN` | A dashboard read token. Make one on the dashboard: **Connect a chat tool** |
| `MAPI_SHARE_LINK` + `MAPI_SHARE_PASSCODE` | A share link and its passcode. The bridge opens a share session and refreshes it before it expires |

You can also put the same values in `~/.config/mapi/mcp.json` (`MAPI_MCP_CONFIG` names
another file). The file must be private (`chmod 600`), or the bridge does not start:

```json
{"api_url": "https://api.example.com", "dashboard_token": "mapi_…"}
```

Example client configuration (stdio):

```json
{
  "mcpServers": {
    "dashboard": {
      "command": "mapi-mcp",
      "env": {"MAPI_API_URL": "https://api.example.com", "MAPI_DASHBOARD_TOKEN": "mapi_…"}
    }
  }
}
```

## Behaviour

- stdout carries JSON-RPC messages only. Logs go to stderr, with tokens, share links,
  share sessions and passcodes redacted. `MAPI_MCP_LOG=INFO` shows one line per request.
- A malformed line gets a JSON-RPC error from the bridge and is not sent to the API.
- A revoked token, a removed dashboard grant or an ended share link stops the next call.
- Results larger than 64 KiB are trimmed by the server; a trimmed result has no
  `structuredContent`.

Supported protocol revisions: `2025-11-25`, `2025-06-18` (`2025-03-26` is not offered: it requires JSON-RPC batches, and the server takes one message per request).
