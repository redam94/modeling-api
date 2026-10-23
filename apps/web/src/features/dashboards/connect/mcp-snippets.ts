/**
 * The client configurations that "Connect a chat tool" offers to copy (v0.20, plan D14, D24):
 * the `mapi-mcp` stdio bridge, and streamable HTTP straight to `POST /v1/mcp`.
 *
 * The read token goes in the environment of the bridge (`MAPI_DASHBOARD_TOKEN`) or in the
 * `Authorization` header, never in the command line or the URL: the bridge refuses argv
 * credentials, and the endpoint refuses `?token=`. Until a token was just made, the snippets
 * hold {@link TOKEN_PLACEHOLDER}. Pure functions (no React, no browser state).
 */

/** What the snippets show where the secret goes when no new token is in hand. */
export const TOKEN_PLACEHOLDER = "<your read token>";

/** The command of the stdio bridge (`packages/mcp`, `[project.scripts]`). */
export const MCP_BRIDGE_COMMAND = "mapi-mcp";

/** The name a snippet gives the server when the dashboard title gives none. */
export const DEFAULT_SERVER_NAME = "mapi-dashboard";

export interface SnippetInput {
  /** The API base, for example `https://api.example.com` (no trailing slash needed). */
  apiUrl: string;
  /** The new token's secret, or `null` for {@link TOKEN_PLACEHOLDER}. */
  secret: string | null;
  /** The dashboard title (untrusted text: only a slug of it is used). */
  title?: string | null;
}

/** The streamable HTTP endpoint of an API base. */
export function mcpEndpoint(apiUrl: string): string {
  return `${apiUrl.replace(/\/+$/, "")}/v1/mcp`;
}

/**
 * A server name for the client's configuration: lower-case letters, digits and hyphens from
 * the title (at most 40), or {@link DEFAULT_SERVER_NAME}. Nothing else of the title reaches the
 * snippet.
 */
export function serverName(title: string | null | undefined): string {
  const slug = (title ?? "")
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40)
    .replace(/-+$/, "");
  return slug || DEFAULT_SERVER_NAME;
}

const json = (v: unknown) => JSON.stringify(v, null, 2);

/** The stdio configuration: the bridge with the API address and the token in its environment. */
export function stdioSnippet({ apiUrl, secret, title }: SnippetInput): string {
  return json({
    mcpServers: {
      [serverName(title)]: {
        command: MCP_BRIDGE_COMMAND,
        env: { MAPI_API_URL: apiUrl.replace(/\/+$/, ""), MAPI_DASHBOARD_TOKEN: secret ?? TOKEN_PLACEHOLDER },
      },
    },
  });
}

/** The streamable HTTP configuration: the endpoint with the token in the `Authorization` header. */
export function httpSnippet({ apiUrl, secret, title }: SnippetInput): string {
  return json({
    mcpServers: {
      [serverName(title)]: {
        type: "http",
        url: mcpEndpoint(apiUrl),
        headers: { Authorization: `Bearer ${secret ?? TOKEN_PLACEHOLDER}` },
      },
    },
  });
}

/** The two snippets in the order the dialog shows them. */
export const SNIPPET_KINDS = ["stdio", "http"] as const;
export type SnippetKind = (typeof SNIPPET_KINDS)[number];

export const SNIPPET_WORDS: Record<SnippetKind, { label: string; hint: string }> = {
  stdio: {
    label: "Desktop app (stdio)",
    hint: "For chat apps that start a local program. Install the mapi-mcp bridge on that computer first. The token goes in the environment, never on the command line.",
  },
  http: {
    label: "Web or remote (HTTP)",
    hint: "For chat tools that connect to a URL. The token goes in the Authorization header, never in the URL.",
  },
};

export function snippetOf(kind: SnippetKind, input: SnippetInput): string {
  return kind === "stdio" ? stdioSnippet(input) : httpSnippet(input);
}
