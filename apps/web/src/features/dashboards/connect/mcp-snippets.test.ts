import { describe, expect, it } from "vitest";

import { DEFAULT_SERVER_NAME, httpSnippet, MCP_BRIDGE_COMMAND, mcpEndpoint, serverName, snippetOf, stdioSnippet, TOKEN_PLACEHOLDER } from "./mcp-snippets";

const API = "https://api.example.com/";
const SECRET = "mapi_7k2m9q4x_abcdefghijklmnopqrstuvwxyz0123456789abc";

type Server = { command?: string; args?: unknown; env?: Record<string, string>; type?: string; url?: string; headers?: Record<string, string> };
const serverOf = (text: string): [string, Server] => {
  const servers = (JSON.parse(text) as { mcpServers: Record<string, Server> }).mcpServers;
  const entries = Object.entries(servers);
  expect(entries).toHaveLength(1);
  return entries[0]!;
};

describe("mcp snippets", () => {
  it("stdio: the bridge takes the API address and the token from its environment, never from argv", () => {
    const [, s] = serverOf(stdioSnippet({ apiUrl: API, secret: SECRET, title: "Weekly visits" }));
    expect(s.command).toBe(MCP_BRIDGE_COMMAND);
    expect(s.args).toBeUndefined();
    expect(s.env).toEqual({ MAPI_API_URL: "https://api.example.com", MAPI_DASHBOARD_TOKEN: SECRET });
    // the secret is nowhere but the environment
    expect(JSON.stringify({ command: s.command, args: s.args })).not.toContain(SECRET);
  });

  it("http: the endpoint is /v1/mcp and the token goes in the Authorization header, never the URL", () => {
    const [, s] = serverOf(httpSnippet({ apiUrl: API, secret: SECRET }));
    expect(s.type).toBe("http");
    expect(s.url).toBe("https://api.example.com/v1/mcp");
    expect(s.url).not.toContain(SECRET);
    expect(s.url).not.toMatch(/[?#]/);
    expect(s.headers).toEqual({ Authorization: `Bearer ${SECRET}` });
  });

  it("shows a placeholder when no new token is in hand", () => {
    for (const kind of ["stdio", "http"] as const) {
      const text = snippetOf(kind, { apiUrl: API, secret: null });
      expect(text).toContain(TOKEN_PLACEHOLDER);
      expect(text).not.toContain("mapi_");
    }
  });

  it("names the server with a slug of the title only", () => {
    expect(serverName("Weekly visits — EU (2026)")).toBe("weekly-visits-eu-2026");
    expect(serverName("Café crème")).toBe("cafe-creme");
    expect(serverName('"}, "evil": {"command": "rm -rf /')).toBe("evil-command-rm-rf");
    expect(serverName("<script>alert(1)</script>")).toBe("script-alert-1-script");
    expect(serverName("   ")).toBe(DEFAULT_SERVER_NAME);
    expect(serverName(null)).toBe(DEFAULT_SERVER_NAME);
    expect(serverName("x".repeat(80))).toHaveLength(40);
    const [name] = serverOf(stdioSnippet({ apiUrl: API, secret: null, title: '"}, "evil": {"command": "rm -rf /' }));
    expect(name).toMatch(/^[a-z0-9-]+$/);
  });

  it("builds the endpoint from a base with or without a trailing slash", () => {
    expect(mcpEndpoint("http://127.0.0.1:18000")).toBe("http://127.0.0.1:18000/v1/mcp");
    expect(mcpEndpoint("http://127.0.0.1:18000///")).toBe("http://127.0.0.1:18000/v1/mcp");
  });
});
