/**
 * Pure helpers of the Integrations hub: webhook events and delivery states in words, form
 * validation that mirrors the `mapi_spec` contracts (the server still decides), and the iframe
 * code for an embed link. Code snippets for endpoints are always the server's
 * (`GET /v1/endpoints/{alias}/snippets`, `mapi_spec.snippets`); the app never writes its own.
 */
import type { DeliveryStatus, SnippetLanguage, WebhookEventType } from "@/api/types";

export const ALIAS_RE = /^[a-z0-9](?:[a-z0-9_-]{0,126}[a-z0-9])?$/;

export const WEBHOOK_EVENTS: { type: WebhookEventType; label: string; description: string }[] = [
  { type: "run.succeeded", label: "Run succeeded", description: "A fit, score, ingest or render run finished." },
  { type: "run.failed", label: "Run failed", description: "A run failed or a user cancelled it." },
  { type: "model.version.ready", label: "Model version ready", description: "A fit produced a model version, with its scorecard verdict." },
  { type: "endpoint.promoted", label: "Endpoint promoted", description: "An alias points to a new version after a promotion or a rollback." },
  { type: "score.completed", label: "Score completed", description: "A score run finished. The event includes the endpoint if the call used one." },
  { type: "tenant.deletion.completed", label: "Organisation deleted", description: "The platform deleted all data of the organisation. This is the last event that it sends." },
];

export const DELIVERY_META: Record<DeliveryStatus, { label: string; variant: "success" | "warning" | "danger" | "outline" | "info"; description: string }> = {
  pending: { label: "Pending", variant: "info", description: "The delivery waits for its first try." },
  succeeded: { label: "Delivered", variant: "success", description: "Your receiver sent a 2xx answer." },
  retrying: { label: "Retrying", variant: "warning", description: "The last try failed. The platform will try again." },
  failed: { label: "Failed", variant: "danger", description: "All retries failed, or the receiver sent a 410 Gone answer." },
  blocked: { label: "Blocked", variant: "danger", description: "The URL goes to an address that the platform never calls. This gives protection against SSRF." },
};

/** The retry schedule after a failed delivery (`mapi_spec.webhooks`). */
export const RETRY_SCHEDULE = ["1 min", "5 min", "30 min", "2 h", "6 h", "12 h", "24 h"];

export const SNIPPET_LANGUAGES: { id: SnippetLanguage; label: string }[] = [
  { id: "python", label: "Python" },
  { id: "typescript", label: "TypeScript" },
  { id: "curl", label: "curl" },
  { id: "r", label: "R" },
  { id: "apps_script", label: "Apps Script" },
  { id: "notebook", label: "Notebook" },
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
  if (url.length < 9 || url.length > 2048) errors.url = "Type the full URL of your receiver, for example https://hooks.example.com/mapi.";
  else {
    let parsed: URL | null = null;
    try {
      parsed = new URL(url);
    } catch {
      parsed = null;
    }
    if (!parsed || (parsed.protocol !== "https:" && parsed.protocol !== "http:")) errors.url = "Type an https:// URL.";
    else if (parsed.username || parsed.password) errors.url = "Remove the credentials from the URL. Verify the signature instead.";
    else if (parsed.hash) errors.url = "A webhook URL cannot have a #fragment. Remove the #fragment.";
  }
  if (!f.events.length) errors.events = "Select one or more events.";
  if (f.description.length > 500) errors.description = "The maximum is 500 characters.";
  return errors;
}

const ORIGIN_RE = /^https:\/\/[a-z0-9.-]{1,253}(?::[0-9]{1,5})?$/;

/** `PlotEmbedCreate.allowed_origins`: up to 10 https origins, one per line or comma separated. */
export function parseAllowedOrigins(text: string): { origins: string[]; error: string | null } {
  const origins = [...new Set(text.split(/[\s,]+/).map((o) => o.trim().replace(/\/+$/, "").toLowerCase()).filter(Boolean))];
  const bad = origins.find((o) => !ORIGIN_RE.test(o));
  if (bad) return { origins, error: `“${bad.slice(0, 60)}” is not an https origin. Type https://host or https://host:port.` };
  if (origins.length > 10) return { origins, error: "The maximum is 10 sites." };
  return { origins, error: null };
}

/** HTML that frames an embed link (the link's own page decides whether the site may frame it). */
export function iframeSnippet(url: string, title: string): string {
  const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return `<iframe src="${esc(url)}" title="${esc(title)}" width="800" height="500" style="border:0" loading="lazy" referrerpolicy="no-referrer"></iframe>`;
}

/** Parse the "Try it" payload editor: a JSON object, or a message saying what is wrong. */
export function parseCallJson(text: string): { value: Record<string, unknown> | null; error: string | null } {
  if (new TextEncoder().encode(text).length > 5 * 1024 * 1024) return { value: null, error: "The payload is larger than 5 MB. This is the maximum for an inline call." };
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch (e) {
    return { value: null, error: `Not valid JSON: ${e instanceof Error ? e.message : String(e)}` };
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return { value: null, error: "The call is not a JSON object. Type a JSON object." };
  if ("model" in parsed) return { value: null, error: "Remove “model”. The endpoint selects the version that scores the call." };
  return { value: parsed as Record<string, unknown>, error: null };
}
