/**
 * The embed page (`/embed/<token>`): a read-only plot shared by a tokenised link (plan
 * §10.3 items 4-5). It is the only page of the plot sandbox that talks to the API, and only
 * to `GET <api>/v1/embeds/<token>` (the CSP's `connect-src` allows nothing else): no
 * credentials are sent, the answer is validated strictly, and only platform-recipe plots
 * with inline data are ever served there. Everything untrusted is written with
 * `textContent`; the PNG fallback is a plain link to a signed image URL.
 */
import { ArrowDecodeError, decodeArrowBase64 } from "./arrow";
import {
  findUnsafeConstructs,
  isPlainJson,
  LIMITS,
  type JsonObject,
  type PlotDatasets,
  type PlotKind,
  type Theme,
} from "./protocol";
import type { Rendered, Renderer } from "./renderers";

export const EMBED_PATH_RE = /^\/embed\/(mpe_[0-9a-hjkmnp-tv-z]{40})\/?$/;
const NAME_RE = /^[A-Za-z_][A-Za-z0-9_-]{0,63}$/;
const ORIGIN_RE = /^https:\/\/[a-z0-9.-]{1,253}(?::[0-9]{1,5})?$/;
const B64_RE = /^[A-Za-z0-9+/]*={0,2}$/;
const MAX_INLINE_B64 = 2 * 1024 * 1024;
const MAX_TEXT = 4000;

export interface EmbedPlot {
  id: string;
  kind: PlotKind;
  spec: JsonObject;
  datasets: Record<string, string>;
}

export interface EmbedView {
  title: string;
  caption: string | null;
  plot: EmbedPlot;
  expiresAt: string;
  allowedOrigins: string[];
  imageUrl: string | null;
}

export type EmbedResult = { ok: true; view: EmbedView } | { ok: false; status: number; message: string };

/** The token of an embed path, or `null`. */
export function tokenFromPath(pathname: string): string | null {
  const m = EMBED_PATH_RE.exec(pathname);
  return m?.[1] ?? null;
}

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v) && Object.getPrototypeOf(v) === Object.prototype;
}

function text(v: unknown, max = MAX_TEXT): string | null {
  return typeof v === "string" ? v.replace(/[\u0000-\u001f\u007f]/g, " ").slice(0, max) : null;
}

function isHttpUrl(v: unknown): v is string {
  if (typeof v !== "string" || v.length > 4096) return false;
  try {
    const u = new URL(v);
    return u.protocol === "https:" || u.protocol === "http:";
  } catch {
    return false;
  }
}

/**
 * Validate an `EmbedView` answer (`mapi_spec.sharing.EmbedView`). Returns `null` for
 * anything unexpected: an unknown kind, a non-inline dataset, a spec that is not plain JSON
 * or that the client-side safety check refuses.
 */
export function parseEmbedView(data: unknown): EmbedView | null {
  if (!isObject(data) || !isObject(data.plot)) return null;
  const plot = data.plot;
  const title = text(data.title, 300);
  const kind = plot.kind;
  if (title === null || (kind !== "vega-lite" && kind !== "plotly")) return null;
  if (typeof plot.id !== "string" || !/^[A-Za-z0-9_.-]{1,128}$/.test(plot.id)) return null;
  if (!isObject(plot.spec) || !isPlainJson(plot.spec)) return null;
  if (findUnsafeConstructs(kind, plot.spec).length) return null;
  const datasets: Record<string, string> = {};
  const given = plot.datasets ?? {};
  if (!isObject(given) || Object.keys(given).length > LIMITS.maxDatasets) return null;
  for (const [name, ref] of Object.entries(given)) {
    if (!NAME_RE.test(name) || !isObject(ref) || ref.kind !== "inline_arrow") return null;
    if (typeof ref.data !== "string" || ref.data.length > MAX_INLINE_B64 || !B64_RE.test(ref.data)) return null;
    datasets[name] = ref.data;
  }
  const origins = data.allowed_origins ?? [];
  if (!Array.isArray(origins) || origins.length > 10) return null;
  if (!origins.every((o): o is string => typeof o === "string" && ORIGIN_RE.test(o))) return null;
  const image = data.image_url ?? null;
  if (image !== null && !isHttpUrl(image)) return null;
  const expires = text(data.expires_at, 64);
  if (expires === null) return null;
  return {
    title,
    caption: data.caption_plain == null ? null : text(data.caption_plain),
    plot: { id: plot.id, kind, spec: plot.spec, datasets },
    expiresAt: expires,
    allowedOrigins: origins,
    imageUrl: image,
  };
}

/** Decode every dataset (Arrow IPC, base64) into rows, within the sandbox's row budget. */
export function decodeDatasets(datasets: Record<string, string>): PlotDatasets {
  const out: PlotDatasets = {};
  let budget: number = LIMITS.maxRowsTotal;
  for (const [name, b64] of Object.entries(datasets)) {
    const rows = decodeArrowBase64(b64, budget);
    budget -= rows.length;
    out[name] = rows;
  }
  return out;
}

export interface FrameContext {
  /** Is the page inside a frame (`window.parent !== window`)? */
  framed: boolean;
  /** `location.ancestorOrigins` where the browser has it (Chromium, Safari). */
  ancestorOrigins: readonly string[] | null;
  /** `document.referrer`: the framing page's URL or origin, when its policy sends one. */
  referrer: string;
}

/**
 * May the page render here? Top-level views and links without `allowed_origins` always may;
 * otherwise the framing page must be one of them (its origin from `ancestorOrigins`, else
 * from the referrer; no evidence at all fails closed).
 */
export function frameAllowed(allowed: readonly string[], frame: FrameContext): boolean {
  if (!frame.framed || allowed.length === 0) return true;
  let parent: string | null = frame.ancestorOrigins?.[0] ?? null;
  if (parent === null && frame.referrer) {
    try {
      parent = new URL(frame.referrer).origin;
    } catch {
      parent = null;
    }
  }
  return parent !== null && allowed.includes(parent);
}

const MESSAGES: Record<number, string> = {
  404: "This link does not exist. Ask the person who shared it for a new one.",
  410: "This link has expired or was revoked. Ask the person who shared it for a new one.",
};

/** Fetch an embed (no credentials, no referrer) and validate the answer. */
export async function loadEmbed(apiUrl: string, token: string, fetchFn: typeof fetch = fetch): Promise<EmbedResult> {
  let res: Response;
  try {
    res = await fetchFn(`${apiUrl}/v1/embeds/${token}`, {
      method: "GET",
      mode: "cors",
      credentials: "omit",
      referrerPolicy: "no-referrer",
      headers: { Accept: "application/json" },
    });
  } catch {
    return { ok: false, status: 0, message: "The plot could not be loaded. Check your connection and reload." };
  }
  if (res.status === 429) {
    const wait = Number.parseInt(res.headers.get("Retry-After") ?? "", 10);
    const when = Number.isFinite(wait) && wait > 0 ? `in ${Math.min(wait, 3600)} s` : "shortly";
    return { ok: false, status: 429, message: `This plot is being viewed a lot. Try again ${when}.` };
  }
  if (!res.ok) {
    return { ok: false, status: res.status, message: MESSAGES[res.status] ?? "The plot could not be loaded." };
  }
  let body: unknown;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  const view = parseEmbedView(body);
  if (view === null) return { ok: false, status: res.status, message: "The plot could not be shown safely." };
  return { ok: true, view };
}

export interface EmbedElements {
  title: HTMLElement;
  plot: HTMLElement;
  caption: HTMLElement;
  status: HTMLElement;
  png: HTMLAnchorElement;
  meta: HTMLElement;
}

export interface EmbedPageOptions {
  apiUrl: string;
  pathname: string;
  elements: EmbedElements;
  frame: FrameContext;
  theme: Theme;
  loadRenderer: (kind: PlotKind) => Promise<Renderer>;
  fetchFn?: typeof fetch;
}

function formatDate(iso: string): string {
  const d = new Date(iso);
  return Number.isFinite(d.getTime()) ? d.toISOString().slice(0, 10) : iso.slice(0, 10);
}

/** Load, check and render one embed into the page's elements. */
export class EmbedPage {
  rendered: Rendered | null = null;
  state: "loading" | "ready" | "error" = "loading";

  constructor(private readonly opts: EmbedPageOptions) {}

  private fail(message: string): void {
    this.state = "error";
    const { status, plot } = this.opts.elements;
    plot.replaceChildren();
    status.textContent = message;
    status.dataset.kind = "error";
  }

  async start(): Promise<void> {
    const els = this.opts.elements;
    const token = tokenFromPath(this.opts.pathname);
    if (token === null) {
      this.fail(MESSAGES[404] ?? "This link does not exist.");
      return;
    }
    els.status.textContent = "Loading the plot…";
    const result = await loadEmbed(this.opts.apiUrl, token, this.opts.fetchFn);
    if (!result.ok) {
      this.fail(result.message);
      return;
    }
    const view = result.view;
    if (!frameAllowed(view.allowedOrigins, this.opts.frame)) {
      this.fail("This plot can only be shown on the sites it was shared with.");
      return;
    }
    els.title.textContent = view.title;
    if (view.caption) els.caption.textContent = view.caption;
    if (view.imageUrl) {
      els.png.href = view.imageUrl;
      els.png.hidden = false;
    }
    els.meta.textContent = `Read-only view · link expires ${formatDate(view.expiresAt)}`;
    let datasets: PlotDatasets;
    try {
      datasets = decodeDatasets(view.plot.datasets);
    } catch (err) {
      this.fail(err instanceof ArrowDecodeError ? "The plot's data could not be read." : "The plot could not be shown.");
      return;
    }
    try {
      const renderer = await this.opts.loadRenderer(view.plot.kind);
      els.plot.replaceChildren();
      this.rendered = await renderer({
        container: els.plot,
        spec: view.plot.spec,
        datasets,
        theme: this.opts.theme,
        onSelect: () => undefined,
      });
    } catch {
      this.fail(
        view.imageUrl
          ? "The interactive plot could not be drawn here; the PNG link below still works."
          : "The plot could not be drawn.",
      );
      if (view.imageUrl) els.png.hidden = false;
      return;
    }
    els.status.textContent = "";
    delete els.status.dataset.kind;
    els.plot.setAttribute("aria-label", view.title);
    this.state = "ready";
  }
}
