/**
 * Pure helpers of the Integrations hub: webhook events and delivery states in words, form
 * validation that mirrors the `mapi_spec` contracts (the server still decides), and the
 * snippets the app writes itself (iframe embed code, "Open in notebook").
 */
import type { DeliveryStatus, SnippetLanguage, WebhookEventType } from "@/api/types";

import { py } from "./pyliteral";

export const ALIAS_RE = /^[a-z0-9](?:[a-z0-9_-]{0,126}[a-z0-9])?$/;

export const WEBHOOK_EVENTS: { type: WebhookEventType; label: string; description: string }[] = [
  { type: "run.succeeded", label: "Run succeeded", description: "A fit, score, ingest or render run finished." },
  { type: "run.failed", label: "Run failed", description: "A run failed or was cancelled." },
  { type: "model.version.ready", label: "Model version ready", description: "A fit produced a model version, with its scorecard verdict." },
  { type: "endpoint.promoted", label: "Endpoint promoted", description: "An alias points at a new version (a promotion or a rollback)." },
  { type: "score.completed", label: "Score completed", description: "A score run finished, with the endpoint when it came through one." },
  { type: "tenant.deletion.completed", label: "Organisation deleted", description: "The organisation was purged. The last event it ever sends." },
];

export const DELIVERY_META: Record<DeliveryStatus, { label: string; variant: "success" | "warning" | "danger" | "outline" | "info"; description: string }> = {
  pending: { label: "Pending", variant: "info", description: "Queued for its first try." },
  succeeded: { label: "Delivered", variant: "success", description: "Your receiver answered 2xx." },
  retrying: { label: "Retrying", variant: "warning", description: "The last try failed; another one is scheduled." },
  failed: { label: "Failed", variant: "danger", description: "Every retry failed (or the receiver answered 410 Gone)." },
  blocked: { label: "Blocked", variant: "danger", description: "The URL resolved to an address the platform never calls (SSRF protection)." },
};

/** The retry schedule after a failed delivery (`mapi_spec.webhooks`). */
export const RETRY_SCHEDULE = ["1 min", "5 min", "30 min", "2 h", "6 h", "12 h", "24 h"];

export const SNIPPET_LANGUAGES: { id: SnippetLanguage; label: string }[] = [
  { id: "python", label: "Python" },
  { id: "typescript", label: "TypeScript" },
  { id: "curl", label: "curl" },
  { id: "r", label: "R" },
  { id: "apps_script", label: "Apps Script" },
  { id: "atlas", label: "Atlas" },
];

export interface WebhookForm {
  url: string;
  description: string;
  events: WebhookEventType[];
}

/**
 * Syntax checks of a webhook before it is sent (`WebhookCreate`). The SSRF policy itself -
 * https on 443/8443, public addresses only - is the server's: its `WEBHOOK_URL_BLOCKED` answer
 * is shown next to the URL.
 */
export function validateWebhookForm(f: WebhookForm): Partial<Record<keyof WebhookForm, string>> {
  const errors: Partial<Record<keyof WebhookForm, string>> = {};
  const url = f.url.trim();
  if (url.length < 9 || url.length > 2048) errors.url = "Enter the full URL of your receiver, such as https://hooks.example.com/mapi.";
  else {
    let parsed: URL | null = null;
    try {
      parsed = new URL(url);
    } catch {
      parsed = null;
    }
    if (!parsed || (parsed.protocol !== "https:" && parsed.protocol !== "http:")) errors.url = "Enter an https:// URL.";
    else if (parsed.username || parsed.password) errors.url = "Leave credentials out of the URL; verify the signature instead.";
    else if (parsed.hash) errors.url = "A webhook URL cannot have a #fragment.";
  }
  if (!f.events.length) errors.events = "Pick at least one event.";
  if (f.description.length > 500) errors.description = "At most 500 characters.";
  return errors;
}

const ORIGIN_RE = /^https:\/\/[a-z0-9.-]{1,253}(?::[0-9]{1,5})?$/;

/** `PlotEmbedCreate.allowed_origins`: up to 10 https origins, one per line or comma separated. */
export function parseAllowedOrigins(text: string): { origins: string[]; error: string | null } {
  const origins = [...new Set(text.split(/[\s,]+/).map((o) => o.trim().replace(/\/+$/, "").toLowerCase()).filter(Boolean))];
  const bad = origins.find((o) => !ORIGIN_RE.test(o));
  if (bad) return { origins, error: `“${bad.slice(0, 60)}” is not an https origin (https://host or https://host:port).` };
  if (origins.length > 10) return { origins, error: "At most 10 sites." };
  return { origins, error: null };
}

/** HTML that frames an embed link (the link's own page decides whether the site may frame it). */
export function iframeSnippet(url: string, title: string): string {
  const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return `<iframe src="${esc(url)}" title="${esc(title)}" width="800" height="500" style="border:0" loading="lazy" referrerpolicy="no-referrer"></iframe>`;
}

/**
 * "Open in notebook": a Jupyter or marimo cell that scores the endpoint with the Python client,
 * from the endpoint's example payload. The token is read from `MAPI_TOKEN`, never embedded.
 */
export function notebookSnippet(o: { baseUrl: string; project: string; alias: string; example: unknown }): string {
  return `# pip install mapi-client
import os

from mapi_client import Client

api = Client(
    ${py(o.baseUrl)},
    token=os.environ["MAPI_TOKEN"],  # a score:${o.alias} token from the Tokens page
    project=${py(o.project)},
)

# The endpoint's example call; replace "data" with your own dataset:// reference or an
# xarray.Dataset converted with ds.to_dict().
call = ${py(o.example, 0)}

result = api.endpoints.score(${py(o.alias)}, call, wait=60)
print(result.run.state)
for link in result.outputs:
    print(link.name, link.size)
`;
}

/** Parse the "Try it" payload editor: a JSON object, or a message saying what is wrong. */
export function parseCallJson(text: string): { value: Record<string, unknown> | null; error: string | null } {
  if (new TextEncoder().encode(text).length > 5 * 1024 * 1024) return { value: null, error: "The payload is larger than the 5 MB an inline call may carry." };
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch (e) {
    return { value: null, error: `Not valid JSON: ${e instanceof Error ? e.message : String(e)}` };
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return { value: null, error: "The call must be a JSON object." };
  if ("model" in parsed) return { value: null, error: "Leave out “model”: the endpoint decides which version scores the call." };
  return { value: parsed as Record<string, unknown>, error: null };
}
